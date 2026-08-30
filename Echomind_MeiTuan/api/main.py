import asyncio
import json
import logging
import os
import pathlib
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    def load_dotenv():
        return None

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
try:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
except Exception:  # pragma: no cover - optional dependency
    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"

    def generate_latest():
        return b""

from pydantic import BaseModel, Field

try:
    import uvicorn
except Exception:  # pragma: no cover - optional dependency
    uvicorn = None

from agents.agent_orchestrator import AgentOrchestrator, Request
from core.compat import create_task
from core.intent_recognizer import IntentRecognizer
from core.skill_loader import SkillManager
from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, EndToEndEvaluator, IntentTestCase
from memory.conversation_memory import MemoryManager, MsgRole
from mcp.knowledge_base import KnowledgeBase
from mcp.order_store import MockOrderStore
from mcp.tool_manager import MCPToolManager, Tool
from monitor.performance_monitor import PerformanceMonitor

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── 全局运行时组件 ────────────────────────────────────────────────────────────
# 这些对象在启动阶段统一创建，后面的路由只负责读取和编排。
_orchestrator = None
_memory = None
_tool_manager = None
_monitor = None
_evaluator = None
_skill_manager = None
_knowledge_base = None
_order_store = None


def _llm_cfg() -> Dict[str, Any]:
    # 当前项目统一适配 DeepSeek，这里只保留 DeepSeek 相关配置入口。
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key": api_key,
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
    }
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


async def _startup():
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager, _knowledge_base, _order_store

    cfg = _llm_cfg()
    logger.info("模型: %s base_url: %s", cfg["model"], cfg.get("base_url", "(local)"))

    # 意图识别器独立初始化，既给主流程用，也给评测器复用。
    recognizer = IntentRecognizer(api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"])

    # Skills 采用目录热加载，方便直接改业务规则而不重启服务。
    skills_dir = os.getenv("MEITUAN_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(root_dir=skills_dir, max_prompt_chars=int(os.getenv("MEITUAN_SKILLS_MAX_PROMPT_CHARS", "5000")))
    _skill_manager.load()

    # 知识库先加载本地示例文档，保证项目开箱即用。
    _knowledge_base = KnowledgeBase(data_dir=str(pathlib.Path(_ROOT) / "data" / "demo_docs"))
    if _knowledge_base.doc_count() == 0:
        _knowledge_base.seed_default_docs()
    logger.info("知识库已加载: %s 个片段", _knowledge_base.doc_count())

    # 订单仓库是当前 MVP 的查询数据源，后续可以直接替换成真实接口适配器。
    _order_store = MockOrderStore(source_path=str(pathlib.Path(_ROOT) / "data" / "mock_orders.json"))

    # MCP 工具管理器统一承接知识库检索、订单查询等能力。
    _tool_manager = MCPToolManager(api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"])

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的检索。请稍后重试，或转人工客服确认。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(
        Tool(
            name="knowledge_search",
            description="搜索外卖客服知识库",
            handler=_knowledge_base.search_handler,
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
            cache_ttl=300.0,
            supports_rerank=True,
            fallback=knowledge_fallback,
        )
    )

    _tool_manager.register(
        Tool(
            name="order_tracking",
            description="查询外卖订单配送状态",
            handler=_order_store.search_handler,
            schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "order_id": {"type": "string"},
                    "phone_last4": {"type": "string"},
                },
                "required": ["user_id"],
            },
            cache_ttl=30.0,
        )
    )

    # 记忆层同时管理工作记忆、会话摘要和用户画像。
    _memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", ""),
        chroma_host=os.getenv("CHROMA_HOST", ""),
        chroma_port=int(os.getenv("CHROMA_PORT", "0")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", str(pathlib.Path(_ROOT) / "data" / "chroma")),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # 编排器负责意图识别、路由、订单查询前置和 Agent 回复生成。
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
        tool_manager=_tool_manager,
        recognizer=recognizer,
    )

    # 监控模块持续采集 Agent/工具指标，并把异常写入告警和 Prometheus。
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    # 评测器用于跑意图识别和端到端对话样例。
    _evaluator = EndToEndEvaluator(
        orchestrator=_orchestrator,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        baseline_path=os.getenv("EVAL_BASELINE_PATH", str(pathlib.Path(_ROOT) / "data" / "eval" / "baseline.json")),
    )

    logger.info("Echomind_MeiTuan 已就绪")


async def _shutdown():
    global _monitor, _memory
    if _monitor is not None:
        await _monitor.stop()
    if _memory is not None:
        await _memory.close()
    logger.info("Echomind_MeiTuan 已关闭")

app = FastAPI(title="Echomind_MeiTuan", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    await _startup()


@app.on_event("shutdown")
async def on_shutdown():
    await _shutdown()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "name": "Echomind_MeiTuan",
        "docs": "/docs",
        "health": "/health",
        "chat": "/chat",
        "skills": "/skills",
        "monitor": "/monitor",
    }


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    conv_id: Optional[str] = None
    order_id: Optional[str] = None
    phone_last4: Optional[str] = None


class ChatResponse(BaseModel):
    conv_id: str
    response: str
    intent: str
    intent_group: str = "other"
    agent_type: str
    agent_types: List[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated: bool
    latency_ms: float
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    tracking_info: Dict[str, Any] = Field(default_factory=dict)


# ── 基础健康检查 ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "agents": _orchestrator.get_stats()}


# ── Skills 管理 ───────────────────────────────────────────────────────────────
@app.get("/skills", tags=["Skills"])
async def skills_summary():
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills():
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


# ── 知识库检索判定 ───────────────────────────────────────────────────────────
def _should_use_knowledge(message: str, intent=None) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    intent_value = getattr(intent, "value", intent)
    if intent_value in {"human_handoff", "other"}:
        return False
    if intent_value in {"order_status", "delivery_progress", "rider_contact", "merchant_delay", "refund", "cancel_order", "missing_item", "wrong_item", "address_change", "coupon_rule", "login_issue", "platform_error", "account_security", "complaint"}:
        return True
    keywords = ["外卖", "配送", "骑手", "订单", "退款", "取消", "优惠券", "登录", "报错"]
    return any(kw in msg for kw in keywords)


async def _build_knowledge_context(message: str, intent=None, top_k: int = 3) -> Tuple[str, bool]:
    if _tool_manager is None:
        return "", False
    if not _should_use_knowledge(message, intent=intent):
        return "", False
    try:
        result = await _tool_manager.search_with_rewrite("knowledge_search", message, top_k=top_k)
        if not result.success or not isinstance(result.data, list) or not result.data:
            return "", False
        parts = ["[知识库检索结果]"]
        used = False
        for idx, item in enumerate(result.data[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "未命名文档"))
            content = str(item.get("content", "")).strip()
            score = item.get("score", "")
            if not content:
                continue
            used = True
            parts.append(f"{idx}. 标题: {title}\n   相关度: {score}\n   内容: {content[:500]}")
        if not used:
            return "", False
        parts.append("请优先依据以上知识库内容回答；如果不足，再结合业务常识补充。")
        return "\n".join(parts), True
    except Exception as ex:
        logger.warning("构建知识库上下文失败: %s", ex)
        return "", False


# ── 对话主链路 ───────────────────────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    conv_id = req.conv_id or str(uuid.uuid4())
    # 先从记忆层取最近上下文，再交给意图识别和路由器决定是否需要查单。
    mem_ctx = await _memory.get_context(req.user_id, conv_id, query=req.message)
    history = [
        {"role": msg.role.value, "content": msg.content}
        for msg in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None

    intent_result = await _orchestrator.recognize_intent(req.message, history=history)
    knowledge_text, knowledge_used = await _build_knowledge_context(req.message, intent=intent_result.intent)
    context_parts = [mem_ctx.to_prompt_text()]
    if knowledge_text:
        context_parts.append(knowledge_text)
    full_context = "\n\n".join(part for part in context_parts if part)

    orch_req = Request(
        message=req.message,
        user_id=req.user_id,
        conv_id=conv_id,
        context=full_context,
        history=history,
        entities=intent_result.entities,
        intent=intent_result.intent,
        intent_group=intent_result.intent_group,
        urgency=intent_result.urgency,
        intent_confidence=intent_result.confidence,
        order_id=req.order_id,
        phone_last4=req.phone_last4,
    )

    result = await _orchestrator.run(orch_req)

    await _memory.add_message(req.user_id, conv_id, MsgRole.USER, req.message)
    await _memory.add_message(req.user_id, conv_id, MsgRole.ASSISTANT, result.response)
    create_task(_memory.update_profile(req.user_id, conv_id))

    return ChatResponse(
        conv_id=conv_id,
        response=result.response,
        intent=result.intent.value if result.intent else "other",
        intent_group=intent_result.intent_group,
        agent_type=result.agent_type.value,
        agent_types=[agent_type.value for agent_type in result.agent_types],
        primary_agent=result.primary_agent.value if result.primary_agent else result.agent_type.value,
        supporting_agents=[agent_type.value for agent_type in result.supporting_agents],
        routing_reason=result.routing_reason,
        routing_confidence=result.routing_confidence,
        escalated=result.escalated,
        latency_ms=round(result.latency_ms, 1),
        knowledge_used=knowledge_used,
        entities=intent_result.entities,
        intent_confidence=round(intent_result.confidence, 4),
        intent_source_scores=intent_result.source_scores,
        tracking_info=result.tracking_info,
    )


# ── 运行态观测 ───────────────────────────────────────────────────────────────
@app.get("/monitor")
async def monitor_summary():
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── 知识库维护 ───────────────────────────────────────────────────────────────
@app.post("/search")
async def search(query: str, top_k: int = 5):
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
    return {"query": query, "results": result.data, "reranked": result.reranked}


class DocInput(BaseModel):
    title: str
    content: str


class BatchDocInput(BaseModel):
    documents: List[DocInput]


class EvalRunInput(BaseModel):
    intent_cases: Optional[List[IntentTestCase]] = None
    dialog_cases: Optional[List[Dict[str, Any]]] = None


# ── 知识导入接口 ─────────────────────────────────────────────────────────────
@app.post("/knowledge/add", tags=["Knowledge"])
async def add_knowledge(body: BatchDocInput):
    if _knowledge_base is None:
        raise HTTPException(503, "知识库未初始化")
    docs = [doc.model_dump() if hasattr(doc, "model_dump") else doc.dict() for doc in body.documents]
    count = await _knowledge_base.add_documents_async(docs)
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": _knowledge_base.doc_count()}


@app.post("/knowledge/upload", tags=["Knowledge"])
async def upload_knowledge(file: UploadFile = File(...)):
    if _knowledge_base is None:
        raise HTTPException(503, "知识库未初始化")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")
    text = content.decode("utf-8", errors="ignore")
    filename = file.filename or "unknown"
    if filename.endswith(".json"):
        try:
            docs = json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式")
            payload = [{"title": str(item.get("title", filename)), "content": str(item.get("content", ""))} for item in docs if isinstance(item, dict)]
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        payload = [{"title": filename.rsplit(".", 1)[0] if "." in filename else filename, "content": text}]
    count = await _knowledge_base.add_documents_async(payload)
    return {"message": f"文件 {filename} 导入成功", "added_chunks": count, "total_chunks": _knowledge_base.doc_count()}


# ── 评测接口 ─────────────────────────────────────────────────────────────────
@app.get("/knowledge/stats", tags=["Knowledge"])
async def knowledge_stats():
    if _knowledge_base is None:
        raise HTTPException(503, "知识库未初始化")
    return {"total_chunks": _knowledge_base.doc_count()}


@app.post("/eval/run")
async def run_eval(body: Optional[EvalRunInput] = None):
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    if body and body.intent_cases is not None:
        intent_cases = [IntentTestCase(message=case.message, expected_intent=case.expected_intent, context=case.context) for case in body.intent_cases]
    else:
        intent_cases = DEFAULT_INTENT_CASES
    if body and body.dialog_cases is not None:
        dialog_cases = body.dialog_cases
    else:
        dialog_cases = DEFAULT_DIALOG_CASES
    report = await _evaluator.run(intent_cases=intent_cases, dialog_cases=dialog_cases)
    return {
        "pass_rate": report.pass_rate,
        "total": report.total,
        "passed": report.passed,
        "avg_scores": report.avg_scores,
        "regressions": report.regressions,
        "recommendations": report.recommendations,
        "results": [
            {
                "test_id": result.test_id,
                "passed": result.passed,
                "scores": result.scores,
                "detail": result.detail,
                "metadata": result.metadata,
            }
            for result in report.results
        ],
    }


if __name__ == "__main__":
    if uvicorn is None:
        raise RuntimeError("uvicorn 未安装，无法直接启动服务")
    uvicorn.run(
        "api.main:app",
        host=os.getenv("MEITUAN_API_HOST", "0.0.0.0"),
        port=int(os.getenv("MEITUAN_API_PORT", "8000")),
        reload=os.getenv("APP_ENV") == "development",
    )
