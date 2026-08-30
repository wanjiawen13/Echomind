import asyncio
import hashlib
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.compat import async_to_thread

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ToolResult:
    success: bool
    data: Any
    tool_name: str
    error: Optional[str] = None
    cached: bool = False
    latency_ms: float = 0.0
    reranked: bool = False


@dataclass
class ToolStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    total_latency_ms: float = 0.0
    consecutive_fails: int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total else 0.0


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0):
        self.threshold = failure_threshold
        self.recovery_s = recovery_s
        self.state = CircuitState.CLOSED
        self.fail_count = 0
        self.opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.opened_at and time.monotonic() - self.opened_at >= self.recovery_s:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable
    schema: Dict[str, Any]
    cache_ttl: float = 0.0
    timeout_s: float = 30.0
    supports_rerank: bool = False
    fallback: Optional[Callable] = None
    stats: ToolStats = field(default_factory=ToolStats, init=False)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker, init=False)


class MCPToolManager:
    def __init__(self, api_key: str = "", base_url: Optional[str] = None, model: str = "deepseek-chat"):
        self._tools: Dict[str, Tool] = {}
        self._cache: Dict[str, tuple] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    async def call(
        self,
        name: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        *,
        use_cache: bool = True,
        rerank_top_k: int = 0,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, data=None, tool_name=name, error=f"工具不存在: {name}")

        cache_rerank_top_k = rerank_top_k if rerank_top_k > 0 and tool.supports_rerank else 0
        if use_cache and tool.cache_ttl > 0:
            cached = self._get_cache(name, params, cache_rerank_top_k)
            if cached is not None:
                cached_data, cached_reranked = cached
                tool.stats.total += 1
                tool.stats.success += 1
                return ToolResult(success=True, data=cached_data, tool_name=name, cached=True, reranked=cached_reranked)

        if not tool.breaker.allow():
            error = f"工具熔断中: {name}"
            return await self._fallback_result(tool, params, context, error)

        tool.stats.total += 1
        t0 = time.monotonic()
        try:
            self._validate_params(tool, params)
            data = await asyncio.wait_for(self._run_handler(tool, params, context), timeout=tool.timeout_s)
            latency = (time.monotonic() - t0) * 1000
            tool.stats.success += 1
            tool.stats.total_latency_ms += latency
            tool.stats.consecutive_fails = 0
            tool.breaker.record_success()

            reranked = False
            if rerank_top_k > 0 and tool.supports_rerank and isinstance(data, list):
                data = await self._rerank(params.get("query", ""), data, rerank_top_k)
                reranked = True

            if tool.cache_ttl > 0:
                self._set_cache(name, params, data, tool.cache_ttl, cache_rerank_top_k, reranked)

            return ToolResult(success=True, data=data, tool_name=name, latency_ms=latency, reranked=reranked)
        except asyncio.TimeoutError:
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            return await self._fallback_result(tool, params, context, "执行超时")
        except Exception as ex:
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            return await self._fallback_result(tool, params, context, str(ex))

    async def search_with_rewrite(
        self,
        tool_name: str,
        query: str,
        top_k: int = 5,
        context: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        queries = self.rewrite_query(query, n=3)
        recall_k = max(top_k, 5)
        tasks = [self.call(tool_name, {"query": q, "top_k": recall_k}, context, use_cache=True) for q in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        seen, merged = set(), []
        for result in results:
            if isinstance(result, ToolResult) and result.success and isinstance(result.data, list):
                for item in result.data:
                    key = hashlib.md5(str(item).encode("utf-8")).hexdigest()
                    if key not in seen:
                        seen.add(key)
                        merged.append(item)
        if not merged:
            return ToolResult(success=False, data=[], tool_name=tool_name, error="所有子查询均无结果")
        reranked = await self._rerank(query, merged, top_k)
        return ToolResult(success=True, data=reranked, tool_name=tool_name, reranked=True)

    def rewrite_query(self, query: str, n: int = 3) -> List[str]:
        base = str(query).strip()
        if not base:
            return [base]
        variants = [
            base,
            f"{base} 订单状态",
            f"{base} 配送进度",
            f"{base} 处理规则",
        ]
        return list(dict.fromkeys(variants[: max(1, n + 1)]))

    async def _rerank(self, query: str, items: List[Any], top_k: int) -> List[Any]:
        def score(item: Any) -> float:
            if isinstance(item, dict):
                return float(item.get("score", 0.0) or 0.0)
            return 0.0

        if len(items) <= top_k:
            return sorted(items, key=score, reverse=True)
        return sorted(items, key=score, reverse=True)[:top_k]

    async def _fallback_result(
        self,
        tool: Tool,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
        error: str,
    ) -> ToolResult:
        if tool.fallback is None:
            return ToolResult(success=False, data=None, tool_name=tool.name, error=error)
        try:
            data = tool.fallback(params, context, error)
            if asyncio.iscoroutine(data):
                data = await data
            return ToolResult(success=True, data=data, tool_name=tool.name, error=error)
        except Exception as ex:
            return ToolResult(success=False, data=None, tool_name=tool.name, error=f"{error}; fallback失败: {ex}")

    async def _run_handler(self, tool: Tool, params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Any:
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(params, context)
        result = await async_to_thread(tool.handler, params, context)
        if inspect.isawaitable(result):
            return await result
        return result

    _TYPE_MAP = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}

    def _validate_params(self, tool: Tool, params: Dict[str, Any]) -> None:
        schema = tool.schema
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        for field in required:
            if field not in params or params[field] in (None, ""):
                raise ValueError(f"工具 {tool.name} 缺少必需参数: {field}")
        for key, value in params.items():
            if value in (None, ""):
                continue
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type and expected_type in self._TYPE_MAP:
                    if not isinstance(value, self._TYPE_MAP[expected_type]):
                        raise ValueError(f"工具 {tool.name} 参数 {key} 类型错误")

    def _cache_key(self, name: str, params: Dict[str, Any], rerank_top_k: int = 0) -> str:
        payload = {"params": params, "rerank_top_k": rerank_top_k}
        return f"{name}:{hashlib.md5(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()}"

    def _get_cache(self, name: str, params: Dict[str, Any], rerank_top_k: int = 0) -> Optional[Tuple[Any, bool]]:
        key = self._cache_key(name, params, rerank_top_k)
        if key in self._cache:
            data, expire_at, reranked = self._cache[key]
            if time.monotonic() < expire_at:
                return data, reranked
            del self._cache[key]
        return None

    def _set_cache(
        self,
        name: str,
        params: Dict[str, Any],
        data: Any,
        ttl: float,
        rerank_top_k: int = 0,
        reranked: bool = False,
    ) -> None:
        if len(self._cache) >= 5000:
            for k in list(self._cache)[:1250]:
                del self._cache[k]
        self._cache[self._cache_key(name, params, rerank_top_k)] = (data, time.monotonic() + ttl, reranked)

    def get_stats(self) -> Dict[str, Any]:
        return {
            name: {
                "total": tool.stats.total,
                "success_rate": round(tool.stats.success_rate, 3),
                "avg_latency_ms": round(tool.stats.avg_latency_ms, 1),
                "consecutive_fails": tool.stats.consecutive_fails,
                "circuit_state": tool.breaker.state.value,
            }
            for name, tool in self._tools.items()
        }
