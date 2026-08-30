import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from core.compat import create_task
from core.deepseek_client import DeepSeekClient
from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    ORDER_STATUS = "order_status"
    DELIVERY_PROGRESS = "delivery_progress"
    RIDER_CONTACT = "rider_contact"
    MERCHANT_DELAY = "merchant_delay"
    REFUND = "refund"
    CANCEL_ORDER = "cancel_order"
    ADDRESS_CHANGE = "address_change"
    COUPON_RULE = "coupon_rule"
    MISSING_ITEM = "missing_item"
    WRONG_ITEM = "wrong_item"
    COMPLAINT = "complaint"
    ACCOUNT_SECURITY = "account_security"
    LOGIN_ISSUE = "login_issue"
    PLATFORM_ERROR = "platform_error"
    HUMAN_HANDOFF = "human_handoff"
    OTHER = "other"


class UrgencyLevel(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class IntentResult:
    intent: IntentCategory
    confidence: float
    urgency: UrgencyLevel
    intent_group: str
    entities: Dict[str, List[str]]
    reasoning: str
    latency_ms: float
    source_scores: Dict[str, float] = field(default_factory=dict)


_TEMPLATES: Dict[IntentCategory, List[str]] = {
    IntentCategory.ORDER_STATUS: ["我的订单到哪了", "订单状态怎么样", "外卖什么时候送到", "订单显示送达但我没收到"],
    IntentCategory.DELIVERY_PROGRESS: ["外卖还要多久", "配送进度怎么样", "骑手到哪了", "显示已送达但没收到"],
    IntentCategory.RIDER_CONTACT: ["联系骑手", "骑手电话多少", "找不到骑手"],
    IntentCategory.MERCHANT_DELAY: ["商家出餐太慢", "商家一直没出餐", "餐还没做好"],
    IntentCategory.REFUND: ["我要退款", "申请赔付", "少送了要补偿"],
    IntentCategory.CANCEL_ORDER: ["取消订单", "帮我取消外卖", "我不想要了"],
    IntentCategory.ADDRESS_CHANGE: ["修改收货地址", "改地址", "配送地址填错了"],
    IntentCategory.COUPON_RULE: ["优惠券怎么用", "满减规则是什么", "红包还能用吗"],
    IntentCategory.MISSING_ITEM: ["少送了", "漏送了一份", "餐品缺少"],
    IntentCategory.WRONG_ITEM: ["送错了", "餐品不对", "拿错了"],
    IntentCategory.COMPLAINT: ["我要投诉", "太慢了", "服务很差"],
    IntentCategory.ACCOUNT_SECURITY: ["账号异常登录", "密码被改了", "账号被盗"],
    IntentCategory.LOGIN_ISSUE: ["登录失败", "验证码收不到", "401错误"],
    IntentCategory.PLATFORM_ERROR: ["App报错", "页面打不开", "系统崩了"],
    IntentCategory.HUMAN_HANDOFF: ["转人工", "找客服", "人工处理"],
}

_INTENT_GROUPS: Dict[IntentCategory, str] = {
    IntentCategory.ORDER_STATUS: "delivery",
    IntentCategory.DELIVERY_PROGRESS: "delivery",
    IntentCategory.RIDER_CONTACT: "delivery",
    IntentCategory.MERCHANT_DELAY: "delivery",
    IntentCategory.ADDRESS_CHANGE: "delivery",
    IntentCategory.COUPON_RULE: "refund",
    IntentCategory.REFUND: "refund",
    IntentCategory.CANCEL_ORDER: "refund",
    IntentCategory.MISSING_ITEM: "refund",
    IntentCategory.WRONG_ITEM: "refund",
    IntentCategory.LOGIN_ISSUE: "platform",
    IntentCategory.PLATFORM_ERROR: "platform",
    IntentCategory.ACCOUNT_SECURITY: "platform",
    IntentCategory.COMPLAINT: "escalation",
    IntentCategory.HUMAN_HANDOFF: "escalation",
}

_URGENCY_KEYWORDS = {
    UrgencyLevel.CRITICAL: ["马上", "立刻", "紧急", "急死了", "报警"],
    UrgencyLevel.HIGH: ["尽快", "今天", "马上", "快点"],
    UrgencyLevel.MEDIUM: ["有点慢", "稍后", "等很久"],
}


class IntentRecognizer:
    def __init__(
        self,
        api_key: str = "",
        base_url: Optional[str] = None,
        model: str = "deepseek-chat",
        confidence_threshold: float = 0.5,
    ):
        self.model = model
        self.threshold = confidence_threshold
        self._embedding_enabled = True
        self._tpl_embeddings: Dict[IntentCategory, List[List[float]]] = {}
        self._cache: Dict[str, IntentResult] = {}
        self.cache_hits = 0
        self.cache_misses = 0

        self.client: Optional[DeepSeekClient] = None
        if api_key:
            client = DeepSeekClient(api_key=api_key, base_url=base_url or "https://api.deepseek.com")
            self.client = client if client.available else None

    async def recognize(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> IntentResult:
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        self.cache_misses += 1

        t0 = time.monotonic()
        entities = self._extract_entities(message)
        rule_task = create_task(self._rule_recognize(message, history))
        llm_task = create_task(self._llm_recognize(message, history)) if self.client else None
        emb_task = create_task(self._embedding_recognize(message)) if self._embedding_enabled else None

        rule = await rule_task
        llm = await llm_task if llm_task else {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "local"}
        emb = await emb_task if emb_task else {"intent": IntentCategory.OTHER, "confidence": 0.0}

        intent, confidence, source_scores = self._vote(llm, emb, rule)
        clean_message = self._clean_text(message)
        order_query_terms = ("查", "看", "状态", "进度", "到哪", "还没到", "配送", "送达", "没收到", "未收到", "没有收到", "没拿到")
        if entities.get("order_id") and any(kw in clean_message for kw in order_query_terms):
            if not any(kw in clean_message for kw in ("退款", "退钱", "取消", "少送", "错送", "漏送")):
                intent = IntentCategory.ORDER_STATUS
                confidence = max(confidence, rule.get("confidence", 0.0), 0.6)
        if intent == IntentCategory.OTHER:
            intent = rule["intent"]
            confidence = max(confidence, rule["confidence"])

        result = IntentResult(
            intent=intent,
            confidence=confidence,
            urgency=self._urgency(message, intent),
            intent_group=self._intent_group(intent),
            entities=entities,
            reasoning=str(llm.get("reasoning", rule.get("reasoning", ""))),
            latency_ms=(time.monotonic() - t0) * 1000,
            source_scores=source_scores,
        )

        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
        self._cache[key] = result
        return result

    async def _rule_recognize(self, message: str, history: Optional[List[Dict[str, str]]]) -> Dict[str, Any]:
        msg = self._clean_text(message).lower()
        scores = {
            IntentCategory.ORDER_STATUS: ["订单状态", "订单号", "查订单", "查一下", "帮我看", "看看订单", "到哪", "送到", "已送达", "显示送达", "出发了吗"],
            IntentCategory.DELIVERY_PROGRESS: ["配送", "外卖", "没到", "还没到", "没收到", "未收到", "没有收到", "没拿到", "还要多久", "多久到", "骑手到哪", "催单"],
            IntentCategory.RIDER_CONTACT: ["骑手电话", "联系骑手", "找骑手", "联系不上骑手"],
            IntentCategory.MERCHANT_DELAY: ["出餐慢", "没出餐", "还没做", "商家慢"],
            IntentCategory.REFUND: ["退款", "赔付", "补偿", "退钱"],
            IntentCategory.CANCEL_ORDER: ["取消订单", "不想要了", "取消外卖"],
            IntentCategory.ADDRESS_CHANGE: ["改地址", "修改地址", "收货地址", "配送地址"],
            IntentCategory.COUPON_RULE: ["优惠券", "红包", "满减", "活动", "券"],
            IntentCategory.MISSING_ITEM: ["少送", "漏送", "缺少", "少了一份"],
            IntentCategory.WRONG_ITEM: ["送错", "餐品不对", "拿错", "错送"],
            IntentCategory.COMPLAINT: ["投诉", "太慢", "服务差", "差评", "不满意"],
            IntentCategory.ACCOUNT_SECURITY: ["账号异常", "被盗", "密码被改", "安全"],
            IntentCategory.LOGIN_ISSUE: ["登录", "验证码", "401", "密码错误"],
            IntentCategory.PLATFORM_ERROR: ["报错", "崩了", "打不开", "闪退", "异常"],
            IntentCategory.HUMAN_HANDOFF: ["转人工", "人工客服", "找客服"],
        }
        if "商家" in msg and any(kw in msg for kw in ("慢", "没出餐", "未出餐", "还没做", "不出餐")):
            return {"intent": IntentCategory.MERCHANT_DELAY, "confidence": 0.7, "reasoning": "rule matched merchant_delay"}
        best = IntentCategory.OTHER
        best_score = 0.0
        for cat, kws in scores.items():
            hits = sum(1 for kw in kws if kw in msg)
            if not hits:
                continue
            score = min(1.0, 0.5 + 0.2 * (hits - 1))
            if score > best_score:
                best = cat
                best_score = score
        if best == IntentCategory.OTHER:
            if any(ch in msg for ch in ("?", "？")) or len(msg) >= 4:
                best = IntentCategory.DELIVERY_PROGRESS if any(kw in msg for kw in ["外卖", "配送", "骑手"]) else IntentCategory.OTHER
                best_score = 0.35 if best != IntentCategory.OTHER else 0.0

        return {"intent": best, "confidence": best_score, "reasoning": f"rule matched {best.value}"}

    async def _llm_recognize(self, message: str, history: Optional[List[Dict[str, str]]]) -> Dict[str, Any]:
        if not self.client:
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "no client"}
        examples = "\n".join(
            f'  消息: "{tpl}" -> 意图: {cat.value}'
            for cat, tpl_list in _TEMPLATES.items()
            for tpl in tpl_list[:1]
        )
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )
        prompt = f"""你是外卖客服意图分析专家。根据示例判断用户意图，返回 JSON。
如果用户问的是配送进度、订单状态、骑手联系、出餐慢、退款、取消订单，请优先返回细粒度意图。

示例:
{examples}

{ctx}
用户消息: "{self._clean_text(message)}"

返回格式:
{{"intent":"<意图值>","confidence":0-1,"reasoning":"一句话说明"}}

可选意图: {", ".join(c.value for c in IntentCategory)}"""
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=256,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            try:
                data["intent"] = IntentCategory(data["intent"])
            except Exception:
                data["intent"] = IntentCategory.OTHER
            return data
        except Exception as ex:
            logger.warning("LLM recognize failed: %s", ex)
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM failed", "failed": True}

    async def _embedding_recognize(self, message: str) -> Dict[str, Any]:
        try:
            await self._load_template_embeddings()
            msg_vec = await self._embed_text(message)
            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in self._tpl_embeddings.items():
                score = max(self._cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat
            return {"intent": best_cat, "confidence": best_score}
        except Exception as ex:
            logger.warning("Embedding recognize failed: %s", ex)
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _vote(self, llm: Dict[str, Any], emb: Dict[str, Any], rule: Dict[str, Any]) -> Tuple[IntentCategory, float, Dict[str, float]]:
        source_scores = {
            "llm": float(llm.get("confidence", 0.0) or 0.0),
            "embedding": float(emb.get("confidence", 0.0) or 0.0),
            "pattern": float(rule.get("confidence", 0.0) or 0.0),
        }
        if llm.get("failed"):
            if rule.get("intent") != IntentCategory.OTHER and source_scores["pattern"] >= 0.5:
                return rule["intent"], source_scores["pattern"], source_scores
            if emb.get("intent") != IntentCategory.OTHER:
                return emb["intent"], source_scores["embedding"], source_scores
            return rule.get("intent", IntentCategory.OTHER), source_scores["pattern"], source_scores

        weights = [(llm, 0.7), (emb, 0.2), (rule, 0.1)] if self._embedding_enabled else [(llm, 0.85), (rule, 0.15)]
        scores: Dict[IntentCategory, float] = {}
        for result, weight in weights:
            cat = result.get("intent", IntentCategory.OTHER)
            conf = float(result.get("confidence", 0.0) or 0.0)
            scores[cat] = scores.get(cat, 0.0) + weight * conf
        if not scores:
            return IntentCategory.OTHER, 0.0, source_scores
        best = max(scores, key=scores.get)
        best_score = scores[best]
        if best_score < self.threshold:
            return IntentCategory.OTHER, best_score, source_scores
        return best, best_score, source_scores

    def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        msg = self._clean_text(message)
        return {
            "order_id": self._unique(re.findall(r"(?:订单号?|订单编号|单号|order(?:_id)?)\s*(?:是|为|:|：|#)?\s*([A-Za-z0-9_-]{4,32})", msg, re.I)),
            "phone_last4": self._unique(re.findall(r"(?:尾号|后四位|手机号后四位)\s*([0-9]{4})", msg)),
            "amount": self._unique(re.findall(r"((?:¥|￥)\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*(?:元|块|rmb|cny))", msg, re.I)),
        }

    async def _load_template_embeddings(self) -> None:
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return
        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx : idx + n]
            idx += n

    async def _embed_text(self, text: str) -> List[float]:
        if self.client is not None:
            embeddings = getattr(self.client, "embeddings", None)
            if embeddings is not None:
                try:
                    resp = await embeddings.create(model="voyage-3-lite", input=[text])
                    return list(resp.data[0].embedding)
                except Exception as ex:
                    logger.warning("remote embedding failed, fallback local: %s", ex)
        return self._local_embedding(text)

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
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        for level, kws in _URGENCY_KEYWORDS.items():
            if any(kw in msg for kw in kws):
                return level
        if intent in (IntentCategory.HUMAN_HANDOFF, IntentCategory.COMPLAINT):
            return UrgencyLevel.HIGH
        return UrgencyLevel.LOW

    @staticmethod
    def _intent_group(intent: IntentCategory) -> str:
        return _INTENT_GROUPS.get(intent, "other")

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @staticmethod
    def _unique(values: List[str]) -> List[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        payload = {"message": self._clean_text(message)[:200]}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", ""))[:160],
                }
                for item in history[-3:]
            ]
        return hashlib.md5(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
