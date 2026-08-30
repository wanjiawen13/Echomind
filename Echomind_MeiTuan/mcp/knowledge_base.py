import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.compat import async_to_thread

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeChunk:
    title: str
    content: str
    chunk: int = 0


class KnowledgeBase:
    COLLECTION_NAME = "knowledge_base"

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self._docs: List[KnowledgeChunk] = []

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        count = 0
        for doc in documents:
            title = str(doc.get("title", "")).strip() or "未命名文档"
            content = str(doc.get("content", "")).strip()
            if not content:
                continue
            for idx, chunk in enumerate(self._chunk_text(content)):
                self._docs.append(KnowledgeChunk(title=title, content=chunk, chunk=idx))
                count += 1
        return count

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        return await async_to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        query_terms = self._tokenize(query)
        scored: List[Tuple[float, KnowledgeChunk]] = []
        for doc in self._docs:
            score = self._score_doc(query_terms, doc)
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, doc in scored[:top_k]:
            results.append({
                "title": doc.title,
                "content": doc.content,
                "score": round(score, 4),
                "chunk": doc.chunk,
            })
        return results

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return await async_to_thread(self.search, query, top_k)

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict[str, Any]]:
        return await self.search_async(str(params.get("query", "")), int(params.get("top_k", 5)))

    def doc_count(self) -> int:
        return len(self._docs)

    async def doc_count_async(self) -> int:
        return await async_to_thread(self.doc_count)

    def load_seed_documents_from_dir(self, seed_dir: str) -> int:
        directory = Path(seed_dir)
        if not directory.exists():
            return 0
        docs: List[Dict[str, str]] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() == ".json":
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            docs.append({
                                "title": str(item.get("title", path.stem)),
                                "content": str(item.get("content", "")),
                            })
            elif path.suffix.lower() in {".md", ".txt"}:
                docs.append({"title": path.stem, "content": path.read_text(encoding="utf-8")})
        return self.add_documents(docs)

    def seed_default_docs(self) -> int:
        return self.load_seed_documents_from_dir(str(self.data_dir))

    def _score_doc(self, query_terms: List[str], doc: KnowledgeChunk) -> float:
        text = f"{doc.title} {doc.content}".lower()
        if not query_terms:
            return 0.0
        matches = sum(1 for term in query_terms if term in text)
        title_bonus = sum(1 for term in query_terms if term in doc.title.lower()) * 0.25
        phrase_bonus = 0.35 if any(term and term in text for term in query_terms[:2]) else 0.0
        return matches * 0.2 + title_bonus + phrase_bonus

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        lowered = str(text).lower()
        raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", lowered)
        if not raw_terms:
            return [lowered.strip()] if lowered.strip() else []
        terms = []
        for term in raw_terms:
            terms.append(term)
            if len(term) > 4:
                terms.append(term[:3])
        return list(dict.fromkeys(terms))

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 500) -> List[str]:
        if len(text) <= chunk_size:
            return [text] if text.strip() else []
        chunks: List[str] = []
        current = ""
        sentences = re.split(r"[。.!?\n]+", text)
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent
        if current:
            chunks.append(current)
        return chunks
