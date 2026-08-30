import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from core.deepseek_client import DeepSeekClient
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_utils import extract_text_content
from mcp.tool_manager import MCPToolManager

logger = logging.getLogger(__name__)


class AgentType(Enum):
    # 这里按美团外卖客服的业务域拆分 Agent，便于后续扩展更多专属能力。
    DELIVERY_SERVICE = "delivery_service"
    REFUND_SUPPORT = "refund_support"
    PLATFORM_SUPPORT = "platform_support"
    ESCALATION = "escalation"


@dataclass
class AgentStats:
    total: int = 0
    success: int = 0
    total_ms: float = 0.0
    monitor_penalty: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0

    def routing_score(self) -> float:
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


@dataclass
class AgentResponse:
    agent_type: AgentType
    content: str
    success: bool
    confidence: float = 1.0
    latency_ms: float = 0.0
    escalate: bool = False


@dataclass
class Request:
    # 编排层内部统一使用的请求结构，承载意图、实体和查询结果。
    message: str
    user_id: str
    conv_id: str
    context: str = ""
    history: Optional[List[Dict[str, str]]] = None
    entities: Dict[str, List[str]] = field(default_factory=dict)
    intent: Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency: Optional[UrgencyLevel] = None
    intent_confidence: float = 1.0
    order_id: Optional[str] = None
    phone_last4: Optional[str] = None
    tracking_info: Dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id: str
    response: str
    agent_type: AgentType
    intent: Optional[IntentCategory]
    escalated: bool = False
    latency_ms: float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    tracking_info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingDecision:
    # 一次请求最终落到哪个主 Agent、是否需要辅助 Agent。
    primary_agent: AgentType
    supporting_agents: List[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0

    @property
    def agent_types(self) -> List[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


class BaseAgent:
    agent_type: AgentType
    system_prompt: str

    def __init__(self, client: Optional[DeepSeekClient], model: str, skill_manager: Optional[Any] = None):
        self._client = client
        self._model = model
        self._skill_manager = skill_manager
        self.stats = AgentStats()

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            content = await self._call_llm(req)
            ms = (time.monotonic() - t0) * 1000
            self.stats.success += 1
            self.stats.total_ms += ms
            return AgentResponse(
                agent_type=self.agent_type,
                content=content,
                success=True,
                latency_ms=ms,
                escalate=self._needs_escalation(content),
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error("%s 处理失败: %s", self.agent_type.value, ex)
            return AgentResponse(
                agent_type=self.agent_type,
                content=self._generate_local_reply(req),
                success=False,
                latency_ms=ms,
            )

    async def _call_llm(self, req: Request) -> str:
        if self._client is None or not getattr(self._client, "available", False):
            return self._generate_local_reply(req)

        def clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        messages = []
        if req.context:
            messages.append({"role": "user", "content": f"[背景信息]\n{clean(req.context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        if req.tracking_info:
            messages.append({"role": "user", "content": f"[订单查询结果]\n{clean(json.dumps(req.tracking_info, ensure_ascii=False))}"})
            messages.append({"role": "assistant", "content": "好的，我会结合订单查询结果回复。"})
        if req.entities:
            messages.append({"role": "user", "content": f"[结构化实体]\n{clean(json.dumps(req.entities, ensure_ascii=False))}"})
            messages.append({"role": "assistant", "content": "好的，我会结合这些实体处理。"})
        messages.append({"role": "user", "content": clean(req.message)})

        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=self._build_system_prompt(req),
            messages=messages,
        )
        text = extract_text_content(resp.content).strip()
        if not text:
            return self._generate_local_reply(req)
        return text

    def _build_system_prompt(self, req: Request) -> str:
        prompt = self.system_prompt
        if self._skill_manager is not None:
            skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
            if skill_prompt:
                prompt = f"{prompt}\n\n[动态 Skills]\n{skill_prompt}"
        return prompt

    def _generate_local_reply(self, req: Request) -> str:
        return self._local_reply(req)

    def _local_reply(self, req: Request) -> str:
        if self.agent_type == AgentType.DELIVERY_SERVICE:
            return self._delivery_reply(req)
        if self.agent_type == AgentType.REFUND_SUPPORT:
            return self._refund_reply(req)
        if self.agent_type == AgentType.PLATFORM_SUPPORT:
            return self._platform_reply(req)
        return "我这边已经帮您升级人工处理，请稍等客服同学接入。"

    def _delivery_reply(self, req: Request) -> str:
        info = req.tracking_info or {}
        msg = req.message.lower()
        if info.get("clarify_needed"):
            return info.get("message") or "请补充订单号或手机号后四位，我帮您继续查询。"
        order = info.get("order")
        if not order:
            return "我还需要确认一下具体订单。请提供订单号或手机号后四位，我帮您继续查询配送进度。"
        status = order.get("status")
        status_label = order.get("status_label") or status
        eta = order.get("eta_minutes")
        lines = [f"已帮您查询到订单 {order.get('order_id')}，当前状态是：{status_label}。"]
        if eta is not None:
            lines.append(f"预计还需要 {eta} 分钟左右。")
        merchant_status = order.get("merchant_status")
        if merchant_status == "preparing":
            lines.append("商家还在备餐中，我会优先按当前进度帮您关注。")
        elif merchant_status in ("ready", "picked_up"):
            lines.append("商家侧已完成出餐。")
        if order.get("rider_name"):
            lines.append(f"骑手是 {order.get('rider_name')}，我也帮您记录了当前进度。")
        if order.get("abnormal_reason"):
            lines.append(f"当前异常说明：{order.get('abnormal_reason')}。")
        if status == "waiting_dispatch":
            lines.append("如果等待时间继续变长，建议转人工确认派单状态。")
        if status == "exception":
            lines.append("这个订单已出现配送异常，建议转人工继续确认地址或配送记录。")
        if status == "delivered":
            if any(kw in msg for kw in ["没收到", "未收到", "没有收到", "没拿到"]):
                lines.append("订单显示已送达但您未收到，我建议先查看门口、前台或取餐柜记录；我也会建议转人工核验骑手送达轨迹和凭证。")
            else:
                lines.append("如果您实际上还没有收到餐，请告诉我，我继续帮您排查。")
        if any(kw in msg for kw in ["改地址", "修改地址", "收货地址", "配送地址"]):
            if status in ("merchant_preparing", "waiting_dispatch"):
                lines.append("当前仍可尝试修改地址，请在订单页提交修改；如果页面不支持，我建议转人工确认。")
            else:
                lines.append("当前进度下修改地址需要进一步确认，建议转人工处理，避免影响配送。")
        return "".join(lines)

    def _refund_reply(self, req: Request) -> str:
        info = req.tracking_info or {}
        order = info.get("order") or {}
        msg = req.message.lower()
        if info.get("clarify_needed"):
            return info.get("message") or "请补充订单号或手机号后四位，我先帮您核验订单状态，再继续处理退款或取消。"
        if any(kw in msg for kw in ["优惠券", "红包", "满减", "活动"]):
            return "优惠券和红包通常受有效期、适用商家、最低消费和叠加规则影响。请提供券名称或订单号，我帮您核验为什么不能使用。"
        if not order:
            return "可以帮您处理退款或取消，但需要先核验订单。请提供订单号、问题原因，以及少送/错送/餐品问题的照片信息。"
        status_label = order.get("status_label") or order.get("status")
        order_id = order.get("order_id")
        if any(kw in msg for kw in ["取消", "不想要"]):
            if order.get("can_cancel"):
                return f"订单 {order_id} 当前状态是 {status_label}，页面上通常可以尝试取消。我建议您先在订单页提交取消；如果失败，我再帮您转人工确认。"
            return f"订单 {order_id} 当前状态是 {status_label}，暂不适合直接取消。可以继续核验是否符合退款或补偿规则。"
        if any(kw in msg for kw in ["少送", "漏送", "缺少", "错送", "餐品不对", "质量"]):
            return f"订单 {order_id} 当前状态是 {status_label}。请保留餐品和包装照片，并说明具体少送/错送的餐品，我会按核验结果协助处理补送、退款或补偿。"
        if order.get("can_refund") is False:
            return f"订单 {order_id} 当前状态是 {status_label}，暂不支持直接退款。我建议先核对实际收餐情况，再判断是否需要转人工。"
        return f"订单 {order_id} 当前状态是 {status_label}，可以继续按退款规则核验。请补充退款原因，例如取消、少送、错送、餐品质量或配送超时。"

    def _platform_reply(self, req: Request) -> str:
        msg = req.message.lower()
        if any(kw in msg for kw in ["验证码", "短信"]):
            return "验证码问题我先帮您排查：请确认手机号是否正确、短信是否被拦截、网络是否稳定，并避免短时间频繁获取验证码。仍收不到的话，请提供发生时间和页面提示，我帮您转人工继续处理。"
        if any(kw in msg for kw in ["被盗", "异常登录", "密码被改", "账号安全"]):
            return "账号安全问题建议优先处理：请不要泄露验证码、密码或支付信息，先尝试修改密码并退出其他设备。我也建议为您升级人工进一步核验。"
        if any(kw in msg for kw in ["登录", "密码"]):
            return "登录问题我先帮您确认：请检查网络、手机号、验证码或密码是否正确。如果页面有错误码，请把错误码和发生时间告诉我，我继续帮您排查。"
        return "这个问题看起来更像 App 或页面异常。请补充错误码、报错截图、发生页面、App 版本和发生时间，我继续帮您排查。"

    def _needs_escalation(self, content: str) -> bool:
        keywords = ["转人工", "人工客服", "无法处理", "升级", "人工处理"]
        return any(kw in content for kw in keywords)


class DeliveryAgent(BaseAgent):
    agent_type = AgentType.DELIVERY_SERVICE
    system_prompt = "你是美团外卖配送客服，负责订单状态、配送进度、骑手联系和出餐进度。"


class RefundAgent(BaseAgent):
    agent_type = AgentType.REFUND_SUPPORT
    system_prompt = "你是美团外卖退款客服，负责退款、取消订单、漏送、错送和补偿说明。"


class PlatformAgent(BaseAgent):
    agent_type = AgentType.PLATFORM_SUPPORT
    system_prompt = "你是美团外卖平台客服，负责登录异常、App 报错、账号安全和基础排障。"


class EscalationAgent(BaseAgent):
    agent_type = AgentType.ESCALATION
    system_prompt = "你是升级分流客服，负责将复杂或高风险问题转人工。"


class AgentOrchestrator:
    # 这张表决定了不同意图会优先进入哪个业务域的 Agent。
    _INTENT_ROUTING: Dict[IntentCategory, AgentType] = {
        IntentCategory.ORDER_STATUS: AgentType.DELIVERY_SERVICE,
        IntentCategory.DELIVERY_PROGRESS: AgentType.DELIVERY_SERVICE,
        IntentCategory.RIDER_CONTACT: AgentType.DELIVERY_SERVICE,
        IntentCategory.MERCHANT_DELAY: AgentType.DELIVERY_SERVICE,
        IntentCategory.REFUND: AgentType.REFUND_SUPPORT,
        IntentCategory.CANCEL_ORDER: AgentType.REFUND_SUPPORT,
        IntentCategory.MISSING_ITEM: AgentType.REFUND_SUPPORT,
        IntentCategory.WRONG_ITEM: AgentType.REFUND_SUPPORT,
        IntentCategory.ADDRESS_CHANGE: AgentType.DELIVERY_SERVICE,
        IntentCategory.COUPON_RULE: AgentType.REFUND_SUPPORT,
        IntentCategory.LOGIN_ISSUE: AgentType.PLATFORM_SUPPORT,
        IntentCategory.PLATFORM_ERROR: AgentType.PLATFORM_SUPPORT,
        IntentCategory.ACCOUNT_SECURITY: AgentType.PLATFORM_SUPPORT,
        IntentCategory.HUMAN_HANDOFF: AgentType.ESCALATION,
        IntentCategory.COMPLAINT: AgentType.ESCALATION,
    }

    def __init__(
        self,
        api_key: str = "",
        base_url: Optional[str] = None,
        model: str = "deepseek-chat",
        skill_manager: Optional[Any] = None,
        tool_manager: Optional[MCPToolManager] = None,
        recognizer: Optional[IntentRecognizer] = None,
    ):
        client = DeepSeekClient(api_key=api_key, base_url=base_url or "https://api.deepseek.com") if api_key else None
        if client is not None and not getattr(client, "available", False):
            client = None
        self._intent_recognizer = recognizer or IntentRecognizer(api_key=api_key, base_url=base_url, model=model)
        self._skill_manager = skill_manager
        self._tool_manager = tool_manager
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.DELIVERY_SERVICE: [DeliveryAgent(client, model, skill_manager)],
            AgentType.REFUND_SUPPORT: [RefundAgent(client, model, skill_manager)],
            AgentType.PLATFORM_SUPPORT: [PlatformAgent(client, model, skill_manager)],
            AgentType.ESCALATION: [EscalationAgent(client, model, skill_manager)],
        }

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    async def recognize_intent(self, message: str, history: Optional[List[Dict[str, str]]] = None):
        return await self._intent_recognizer.recognize(message, history=history)

    async def run(self, req: Request) -> OrchestratorResult:
        t0 = time.monotonic()
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence
            req.entities = intent_result.entities

        self._hydrate_entity_hints(req)

        # 先处理需要追问的场景，避免一上来就进入错误的 Agent 分流。
        if self._needs_clarification(req):
            return OrchestratorResult(
                request_id=req.request_id,
                response="我还需要确认一下您要查询的是哪一单。请提供订单号，或告诉我手机号后四位。",
                agent_type=AgentType.DELIVERY_SERVICE,
                intent=req.intent,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.DELIVERY_SERVICE],
                primary_agent=AgentType.DELIVERY_SERVICE,
                routing_reason="低置信度意图，先追问订单信息",
                routing_confidence=req.intent_confidence,
            )

        # 订单类问题会优先走查单工具，确保 Agent 拿到结构化结果后再回复。
        if self._needs_order_tracking(req):
            req.tracking_info = await self._maybe_track_order(req)
        else:
            req.tracking_info = {}

        if req.intent in (
            IntentCategory.ORDER_STATUS,
            IntentCategory.DELIVERY_PROGRESS,
            IntentCategory.RIDER_CONTACT,
            IntentCategory.MERCHANT_DELAY,
        ) and req.tracking_info.get("clarify_needed"):
            return OrchestratorResult(
                request_id=req.request_id,
                response=req.tracking_info.get("message", "请补充订单号或手机号后四位，我帮您继续查询。"),
                agent_type=AgentType.DELIVERY_SERVICE,
                intent=req.intent,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.DELIVERY_SERVICE],
                primary_agent=AgentType.DELIVERY_SERVICE,
                routing_reason="订单查询信息不足",
                routing_confidence=req.intent_confidence,
                tracking_info=req.tracking_info,
            )

        decision = self._route_decision(req)
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        response = await self._execute(req, decision.primary_agent)
        escalated = response.escalate or req.urgency == UrgencyLevel.CRITICAL or req.intent == IntentCategory.HUMAN_HANDOFF
        return OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            tracking_info=req.tracking_info,
        )

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        t0 = time.monotonic()
        tasks = [self._execute(req, agent_type) for agent_type in decision.agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        parts = []
        used_agents = []
        for response in responses:
            if isinstance(response, AgentResponse) and response.success:
                role = "主处理" if response.agent_type == decision.primary_agent else "辅助处理"
                parts.append(f"[{response.agent_type.value} - {role}]\n{response.content}")
                used_agents.append(response.agent_type)
        combined = "\n\n".join(parts) if parts else "抱歉，暂时无法处理您的请求。"
        escalated = any(isinstance(r, AgentResponse) and r.escalate for r in responses)
        return OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=used_agents or decision.agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            tracking_info=req.tracking_info,
        )

    def _route_decision(self, req: Request) -> RoutingDecision:
        # 先看是否要直接升级，再根据意图和关键词做域路由。
        if req.urgency == UrgencyLevel.CRITICAL:
            return RoutingDecision(primary_agent=AgentType.ESCALATION, reason="紧急度为 CRITICAL", confidence=1.0)
        if req.intent in (IntentCategory.HUMAN_HANDOFF, IntentCategory.COMPLAINT):
            return RoutingDecision(primary_agent=AgentType.ESCALATION, reason=f"意图为 {req.intent.value}", confidence=max(req.intent_confidence, 0.8))

        collaboration = self._collaboration_targets(req)
        if len(collaboration) > 1:
            primary_agent = collaboration[0]
            supporting_agents = collaboration[1:]
            scores = self._domain_scores(req)
            return RoutingDecision(
                primary_agent=primary_agent,
                supporting_agents=supporting_agents,
                reason=self._routing_reason(req, scores, primary_agent, supporting_agents),
                confidence=round(min(scores.get(primary_agent, 0.5), 1.0), 3),
            )

        scores = self._domain_scores(req)
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        primary_agent, primary_score = ordered[0]
        supporting_agents = [
            agent_type
            for agent_type, score in ordered[1:]
            if agent_type != AgentType.ESCALATION and score >= 0.5 and score >= primary_score * 0.6
        ]
        reason = self._routing_reason(req, scores, primary_agent, supporting_agents)
        return RoutingDecision(
            primary_agent=primary_agent,
            supporting_agents=supporting_agents,
            reason=reason,
            confidence=round(min(primary_score, 1.0), 3),
        )

    def _domain_scores(self, req: Request) -> Dict[AgentType, float]:
        msg = req.message.lower()
        # 分数由意图命中和关键词命中共同构成，保证规则和语义都能参与决策。
        scores = {
            AgentType.DELIVERY_SERVICE: 0.1,
            AgentType.REFUND_SUPPORT: 0.1,
            AgentType.PLATFORM_SUPPORT: 0.1,
            AgentType.ESCALATION: 0.0,
        }
        if req.intent in (
            IntentCategory.ORDER_STATUS,
            IntentCategory.DELIVERY_PROGRESS,
            IntentCategory.RIDER_CONTACT,
            IntentCategory.MERCHANT_DELAY,
            IntentCategory.ADDRESS_CHANGE,
        ):
            scores[AgentType.DELIVERY_SERVICE] += 0.8
        if req.intent in (
            IntentCategory.REFUND,
            IntentCategory.CANCEL_ORDER,
            IntentCategory.MISSING_ITEM,
            IntentCategory.WRONG_ITEM,
            IntentCategory.COUPON_RULE,
        ):
            scores[AgentType.REFUND_SUPPORT] += 0.8
        if req.intent in (IntentCategory.LOGIN_ISSUE, IntentCategory.PLATFORM_ERROR, IntentCategory.ACCOUNT_SECURITY):
            scores[AgentType.PLATFORM_SUPPORT] += 0.8
        if any(kw in msg for kw in ["外卖", "配送", "骑手", "送到", "多久到", "订单"]):
            scores[AgentType.DELIVERY_SERVICE] += 0.3
        if any(kw in msg for kw in ["退款", "取消", "少送", "错送", "赔付", "优惠券", "红包", "满减"]):
            scores[AgentType.REFUND_SUPPORT] += 0.3
        if any(kw in msg for kw in ["登录", "报错", "崩溃", "401", "验证码"]):
            scores[AgentType.PLATFORM_SUPPORT] += 0.3
        if any(kw in msg for kw in ["投诉", "人工", "转人工"]):
            scores[AgentType.ESCALATION] += 0.7
        return {k: round(v, 3) for k, v in scores.items()}

    @staticmethod
    def _routing_reason(req: Request, scores: Dict[AgentType, float], primary_agent: AgentType, supporting_agents: List[AgentType]) -> str:
        score_text = ", ".join(f"{agent.value}={score:.2f}" for agent, score in sorted(scores.items(), key=lambda item: item[1], reverse=True))
        support_text = ", ".join(agent.value for agent in supporting_agents) or "none"
        intent = req.intent.value if req.intent else "unknown"
        return f"intent={intent}, primary={primary_agent.value}, supporting={support_text}, scores=[{score_text}]"

    def _collaboration_targets(self, req: Request) -> List[AgentType]:
        targets: List[AgentType] = []
        msg = req.message.lower()
        if req.intent in (
            IntentCategory.ORDER_STATUS,
            IntentCategory.DELIVERY_PROGRESS,
            IntentCategory.RIDER_CONTACT,
            IntentCategory.MERCHANT_DELAY,
        ) or any(kw in msg for kw in ["外卖", "配送", "骑手", "送到"]):
            targets.append(AgentType.DELIVERY_SERVICE)
        if req.intent in (
            IntentCategory.REFUND,
            IntentCategory.CANCEL_ORDER,
            IntentCategory.MISSING_ITEM,
            IntentCategory.WRONG_ITEM,
            IntentCategory.COUPON_RULE,
        ) or any(kw in msg for kw in ["退款", "取消", "少送", "错送"]):
            targets.append(AgentType.REFUND_SUPPORT)
        if req.intent in (IntentCategory.LOGIN_ISSUE, IntentCategory.PLATFORM_ERROR, IntentCategory.ACCOUNT_SECURITY):
            targets.append(AgentType.PLATFORM_SUPPORT)
        deduped = list(dict.fromkeys(targets))
        return [agent_type for agent_type in deduped if self._pool.get(agent_type)]

    @staticmethod
    def _needs_clarification(req: Request) -> bool:
        if req.intent != IntentCategory.OTHER:
            return False
        if req.order_id or req.phone_last4 or req.entities.get("order_id") or req.entities.get("phone_last4"):
            return False
        text = (req.message or "").strip()
        return len(text) > 2 and req.intent_confidence < 0.5

    @staticmethod
    def _needs_order_tracking(req: Request) -> bool:
        if req.order_id or req.phone_last4:
            return True
        if req.entities.get("order_id") or req.entities.get("phone_last4"):
            return True
        if req.intent in (
            IntentCategory.ORDER_STATUS,
            IntentCategory.DELIVERY_PROGRESS,
            IntentCategory.RIDER_CONTACT,
            IntentCategory.MERCHANT_DELAY,
            IntentCategory.REFUND,
            IntentCategory.CANCEL_ORDER,
            IntentCategory.MISSING_ITEM,
            IntentCategory.WRONG_ITEM,
            IntentCategory.ADDRESS_CHANGE,
        ):
            return True
        text = req.message.lower()
        return any(kw in text for kw in ["外卖", "订单", "配送", "骑手", "退款", "取消", "少送", "错送"])

    @staticmethod
    def _hydrate_entity_hints(req: Request) -> None:
        if req.order_id and req.phone_last4:
            return
        texts = []
        if req.history:
            for item in reversed(req.history[-8:]):
                texts.append(str(item.get("content", "")))
        if req.context:
            texts.append(req.context)
        if not texts:
            return
        joined = "\n".join(texts)
        if not req.order_id and not req.entities.get("order_id"):
            order_ids = re.findall(r"(?:订单号?|订单编号|单号|order(?:_id)?)\s*(?:是|为|:|：|#)?\s*([A-Za-z0-9_-]{4,32})", joined, re.I)
            if order_ids:
                req.entities["order_id"] = [order_ids[-1]]
        if not req.phone_last4 and not req.entities.get("phone_last4"):
            phones = re.findall(r"(?:尾号|后四位|手机号后四位)\s*([0-9]{4})", joined)
            if phones:
                req.entities["phone_last4"] = [phones[-1]]

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        agent = self._best_agent(agent_type)
        if agent is None:
            return AgentResponse(agent_type=AgentType.ESCALATION, content="暂时无法处理，请稍后重试。", success=False)
        response = await agent.handle(req)
        if not response.success and agent_type != AgentType.ESCALATION:
            fallback = self._best_agent(AgentType.ESCALATION)
            if fallback:
                response = await fallback.handle(req)
        return response

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        return max(agents, key=lambda agent: agent.stats.routing_score())

    async def _maybe_track_order(self, req: Request) -> Dict[str, Any]:
        if self._tool_manager is None:
            return {}
        # 查单参数优先取显式输入，其次取意图识别提取出的实体。
        order_id = req.order_id or (req.entities.get("order_id", [None])[0] if req.entities.get("order_id") else None)
        phone_last4 = req.phone_last4 or (req.entities.get("phone_last4", [None])[0] if req.entities.get("phone_last4") else None)
        params = {"user_id": req.user_id}
        if order_id:
            params["order_id"] = order_id
        if phone_last4:
            params["phone_last4"] = phone_last4
        result = await self._tool_manager.call("order_tracking", params, use_cache=False)
        if not result.success or not isinstance(result.data, dict):
            return {"clarify_needed": False, "message": "暂时无法查询该订单，请稍后再试。", "tool_error": result.error}
        data = dict(result.data)
        if data.get("clarify_needed"):
            data["clarify_needed"] = True
        return data

    def get_stats(self) -> Dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                result[key] = {
                    "total": agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms": round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                }
        return result

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                agent.stats.monitor_penalty = min(max(penalties.get(key, 0.0), 0.0), 0.9)
