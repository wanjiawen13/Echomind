import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

try:
    import redis.asyncio as redis
except Exception:  # pragma: no cover - optional dependency
    redis = None

try:
    import chromadb
except Exception:  # pragma: no cover - optional dependency
    chromadb = None

from core.compat import async_to_thread

logger = logging.getLogger(__name__)


class MsgRole(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass
class Message:
    role: MsgRole
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    recent_messages: List[Message]
    relevant_history: List[str]
    user_profile: Dict[str, Any]
    summary: str

    @staticmethod
    def _clean(text: str) -> str:
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        parts: List[str] = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            parts.append(f"[用户画像]\n{json.dumps(self.user_profile, ensure_ascii=False)}")
        if self.recent_messages:
            parts.append("[最近对话]")
            for msg in self.recent_messages:
                parts.append(f"{msg.role.value}: {self._clean(msg.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    WORKING_MAX = 20
    COMPRESS_AT = 15
    HISTORY_TOP_K = 5

    def __init__(
        self,
        redis_url: str = "",
        chroma_host: str = "",
        chroma_port: int = 0,
        chroma_path: str = "./data/chroma",
        api_key: str = "",
        base_url: Optional[str] = None,
        model: str = "deepseek-chat",
    ):
        self._redis = redis.from_url(redis_url, decode_responses=True) if redis_url and redis is not None else None
        self._working: Dict[str, List[Dict[str, Any]]] = {}
        self._summary: Dict[str, str] = {}
        self._episodic: List[Dict[str, Any]] = []
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._episodic_collection = None
        self._profile_collection = None
        self._init_chroma(chroma_host=chroma_host, chroma_port=chroma_port, chroma_path=chroma_path)

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role: MsgRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        key = self._wm_key(user_id, conv_id)
        payload = {
            "role": role.value,
            "content": self._safe_text(content),
            "ts": datetime.now().isoformat(),
            "metadata": self._safe_metadata_value(metadata or {}),
        }
        self._working.setdefault(key, []).append(payload)
        self._working[key] = self._working[key][-self.WORKING_MAX :]
        await self._mirror_to_redis_list(key, payload)
        if len(self._working[key]) >= self.COMPRESS_AT:
            await self._compress(user_id, conv_id)

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return
        recent_text = "\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:])
        profile = {
            "preferences": self._extract_preferences(recent_text),
            "common_issue": self._extract_common_issue(recent_text),
            "updated_at": datetime.now().isoformat(),
        }
        self._profiles[user_id] = profile
        await self._store_profile(user_id, conv_id, profile)

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)
        recent = await self._get_working_memory(user_id, conv_id)
        history = await self._search_episodic(user_id, query or (recent[-1].content if recent else ""))
        profile = await self._get_profile(user_id)
        summary = self._summary.get(self._summary_key(user_id, conv_id), "")
        return MemoryContext(recent_messages=recent, relevant_history=history, user_profile=profile, summary=summary)

    async def _compress(self, user_id: str, conv_id: str) -> None:
        key = self._wm_key(user_id, conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if len(messages) < self.COMPRESS_AT:
            return
        to_compress = messages[:-5]
        keep = messages[-5:]
        summary = self._summarize_messages(to_compress)
        skey = self._summary_key(user_id, conv_id)
        old_summary = self._summary.get(skey, "")
        self._summary[skey] = (old_summary + "\n" + summary).strip()
        full_text = "\n".join(f"{m.role.value}: {m.content}" for m in to_compress)[:500]
        item = {
            "user_id": user_id,
            "conv_id": conv_id,
            "summary": summary,
            "full_text": full_text,
            "ts": datetime.now().isoformat(),
        }
        self._episodic.append(item)
        await self._store_episodic(item)
        self._working[key] = [self._message_to_dict(m) for m in keep]
        await self._mirror_summary_to_redis(skey, self._summary[skey])
        await self._mirror_working_memory_to_redis(key, self._working[key])

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        key = self._wm_key(user_id, conv_id)
        raw_items = self._working.get(key, [])
        if not raw_items and self._redis is not None:
            try:
                raws = await self._redis.lrange(key, 0, self.WORKING_MAX - 1)
                raw_items = [json.loads(raw) for raw in reversed(raws)]
                self._working[key] = raw_items
            except Exception:
                self._redis = None
        return [self._dict_to_message(item) for item in raw_items]

    async def _search_episodic(self, user_id: str, query: str) -> List[str]:
        if not query.strip():
            return []
        chroma_hits = await self._search_chroma_episodic(user_id, query)
        if chroma_hits:
            return chroma_hits
        query_terms = set(self._tokenize(query))
        scored: List[Tuple[int, str]] = []
        for item in self._episodic:
            if item.get("user_id") != user_id:
                continue
            text = f"{item.get('summary', '')} {item.get('full_text', '')}".lower()
            score = sum(1 for term in query_terms if term and term in text)
            if score:
                scored.append((score, str(item.get("summary", ""))))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [text for _, text in scored[: self.HISTORY_TOP_K]]

    async def _get_profile(self, user_id: str) -> Dict[str, Any]:
        if user_id in self._profiles:
            return self._profiles[user_id]
        profile = await self._get_chroma_profile(user_id)
        if profile:
            self._profiles[user_id] = profile
            return profile
        return {}

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def _mirror_to_redis_list(self, key: str, payload: Dict[str, Any]) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.lpush(key, json.dumps(payload, ensure_ascii=False))
            await self._redis.expire(key, 86400)
        except Exception:
            self._redis = None

    async def _mirror_working_memory_to_redis(self, key: str, items: List[Dict[str, Any]]) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(key)
            for item in reversed(items):
                await self._redis.lpush(key, json.dumps(item, ensure_ascii=False))
            await self._redis.expire(key, 86400)
        except Exception:
            self._redis = None

    async def _mirror_summary_to_redis(self, key: str, value: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(key, 86400, value)
        except Exception:
            self._redis = None

    def _summarize_messages(self, messages: List[Message]) -> str:
        if not messages:
            return ""
        texts = [f"{m.role.value}: {m.content}" for m in messages]
        return "；".join(texts[:3])[:600]

    def _extract_preferences(self, text: str) -> List[str]:
        prefs = []
        lowered = text.lower()
        if "简短" in lowered or "简洁" in lowered:
            prefs.append("简短回复")
        if "电话" in lowered or "联系" in lowered:
            prefs.append("偏好联系信息明确")
        if "外卖" in lowered or "配送" in lowered:
            prefs.append("高频配送咨询")
        return list(dict.fromkeys(prefs))

    def _extract_common_issue(self, text: str) -> str:
        lowered = text.lower()
        for keyword in ["配送", "退款", "取消", "地址", "骑手", "少送", "错送", "验证码", "登录"]:
            if keyword in lowered:
                return keyword
        return ""

    def _init_chroma(self, chroma_host: str, chroma_port: int, chroma_path: str) -> None:
        if chromadb is None:
            return
        try:
            client = None
            if chroma_host and chroma_port:
                try:
                    client = chromadb.HttpClient(
                        host=chroma_host,
                        port=chroma_port,
                        settings=chromadb.Settings(anonymized_telemetry=False),
                    )
                    client.heartbeat()
                except Exception:
                    client = None
            if client is None:
                client = chromadb.PersistentClient(
                    path=chroma_path,
                    settings=chromadb.Settings(anonymized_telemetry=False),
                )
            self._episodic_collection = client.get_or_create_collection("meituan_episodic")
            self._profile_collection = client.get_or_create_collection("meituan_user_profile")
            logger.info("ChromaDB 记忆已启用")
        except Exception as ex:
            self._episodic_collection = None
            self._profile_collection = None
            logger.info("ChromaDB 记忆不可用，使用内存兜底: %s", ex)

    async def _store_episodic(self, item: Dict[str, Any]) -> None:
        if self._episodic_collection is None:
            return
        doc = self._safe_text(item.get("summary", ""))
        if not doc:
            return
        metadata = {
            "user_id": self._safe_text(item.get("user_id", "")),
            "conv_id": self._safe_text(item.get("conv_id", "")),
            "ts": self._safe_text(item.get("ts", "")),
            "full_text": self._safe_text(item.get("full_text", "")),
        }
        doc_id = hashlib.md5(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        try:
            await async_to_thread(
                self._episodic_collection.upsert,
                ids=[doc_id],
                documents=[doc],
                embeddings=[self._local_embedding(doc)],
                metadatas=[metadata],
            )
        except Exception as ex:
            logger.warning("情景记忆持久化失败: %s", ex)

    async def _search_chroma_episodic(self, user_id: str, query: str) -> List[str]:
        if self._episodic_collection is None:
            return []
        try:
            result = await async_to_thread(
                self._episodic_collection.query,
                query_embeddings=[self._local_embedding(query)],
                n_results=self.HISTORY_TOP_K,
                where={"user_id": user_id},
            )
            docs = result.get("documents", [[]])[0] if isinstance(result, dict) else []
            return [self._safe_text(doc) for doc in docs if str(doc).strip()]
        except Exception as ex:
            logger.warning("情景记忆检索失败: %s", ex)
            return []

    async def _store_profile(self, user_id: str, conv_id: str, profile: Dict[str, Any]) -> None:
        if self._profile_collection is None:
            return
        doc = json.dumps(profile, ensure_ascii=False)
        doc_id = f"{user_id}_profile"
        try:
            await async_to_thread(
                self._profile_collection.upsert,
                ids=[doc_id],
                documents=[doc],
                embeddings=[self._local_embedding(doc)],
                metadatas=[{"user_id": user_id, "conv_id": conv_id, "updated_at": profile.get("updated_at", "")}],
            )
        except Exception as ex:
            logger.warning("用户画像持久化失败: %s", ex)

    async def _get_chroma_profile(self, user_id: str) -> Dict[str, Any]:
        if self._profile_collection is None:
            return {}
        try:
            result = await async_to_thread(self._profile_collection.get, ids=[f"{user_id}_profile"])
            docs = result.get("documents", []) if isinstance(result, dict) else []
            if docs:
                return json.loads(docs[0])
        except Exception as ex:
            logger.warning("读取用户画像失败: %s", ex)
        return {}

    @staticmethod
    def _local_embedding(text: str, dims: int = 128) -> List[float]:
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i : i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)
        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    @staticmethod
    def _message_to_dict(message: Message) -> Dict[str, Any]:
        return {
            "role": message.role.value,
            "content": message.content,
            "ts": message.timestamp.isoformat(),
            "metadata": message.metadata,
        }

    @staticmethod
    def _dict_to_message(data: Dict[str, Any]) -> Message:
        return Message(
            role=MsgRole(data["role"]),
            content=data["content"],
            timestamp=MemoryManager._parse_datetime(data.get("ts")),
            metadata=data.get("metadata", {}),
        )

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        lowered = text.lower()
        return list(dict.fromkeys([term for term in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", lowered)]))

    @staticmethod
    def _safe_text(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value

    @staticmethod
    def _wm_key(user_id: str, conv_id: str) -> str:
        return f"wm:{user_id}:{conv_id}"

    @staticmethod
    def _summary_key(user_id: str, conv_id: str) -> str:
        return f"summary:{user_id}:{conv_id}"

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return datetime.now()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text[:26], fmt) if "%f" in fmt else datetime.strptime(text[:19], fmt)
            except Exception:
                continue
        return datetime.now()
