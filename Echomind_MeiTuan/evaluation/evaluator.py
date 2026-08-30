import json
import logging
import pathlib
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.deepseek_client import DeepSeekClient
from core.intent_recognizer import IntentCategory, IntentRecognizer

logger = logging.getLogger(__name__)


@dataclass
class IntentTestCase:
    # 单条意图样例，只关心输入、目标标签和必要上下文。
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


@dataclass
class QualityScores:
    # 这里把对话质量拆成四个维度，方便定位到底是相关性还是完整性出了问题。
    relevance: float
    accuracy: float
    completeness: float
    helpfulness: float
    judge_failed: bool = False
    error: Optional[str] = None

    @property
    def overall(self) -> float:
        return statistics.mean([self.relevance, self.accuracy, self.completeness, self.helpfulness])


@dataclass
class EvalResult:
    test_id: str
    passed: bool
    scores: Dict[str, float]
    detail: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    timestamp: str
    total: int
    passed: int
    pass_rate: float
    avg_scores: Dict[str, float]
    regressions: List[str]
    recommendations: List[str]
    results: List[EvalResult]


class LLMJudge:
    JUDGE_PROMPT = """你是一个外卖客服质量评估专家。请对以下响应打分。

用户问题: {question}
Agent 响应: {response}
{context_section}

请从以下四个维度评分（0.0-1.0），只返回 JSON：
- relevance
- accuracy
- completeness
- helpfulness
"""

    def __init__(self, client: Optional[DeepSeekClient], model: str):
        self._client = client
        self._model = model

    async def judge(self, question: str, response: str, context: Optional[str] = None) -> QualityScores:
        # 评审优先用模型打分，不可用时再退回启发式评分。
        if self._client is None:
            return self._heuristic(question, response, context)
        prompt = self.JUDGE_PROMPT.format(
            question=question,
            response=response,
            context_section=f"背景信息: {context}" if context else "",
        )
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = self._clean_text("".join(part.text for part in resp.content if getattr(part, "text", None)))
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            return QualityScores(
                relevance=float(data.get("relevance", 0.5)),
                accuracy=float(data.get("accuracy", 0.5)),
                completeness=float(data.get("completeness", 0.5)),
                helpfulness=float(data.get("helpfulness", 0.5)),
            )
        except Exception as ex:
            logger.warning("LLM judge failed: %s", ex)
            return self._heuristic(question, response, context, error=str(ex))

    def _heuristic(self, question: str, response: str, context: Optional[str], error: Optional[str] = None) -> QualityScores:
        q = question.lower()
        r = response.lower()
        relevance = 1.0 if any(word in r for word in q.split()[:3]) or any(key in r for key in ["订单", "配送", "退款", "转人工"]) else 0.7
        accuracy = 0.9 if "请提供订单号" in response or "已帮您查询到" in response or "请补充" in response else 0.8
        completeness = 0.9 if len(response) > 20 else 0.6
        helpfulness = 0.9 if any(key in response for key in ["请提供", "我帮您", "已帮您", "继续查询"]) else 0.7
        return QualityScores(relevance, accuracy, completeness, helpfulness, judge_failed=True, error=error)

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")


class IntentEvaluator:
    def __init__(self, recognizer: IntentRecognizer):
        self._recognizer = recognizer

    async def evaluate(self, cases: List[IntentTestCase]) -> Dict[str, Any]:
        # 先跑意图样例，再计算准确率、宏平均 F1 和每类指标。
        predictions, ground_truth = [], []
        case_details: List[Dict[str, Any]] = []
        for case in cases:
            result = await self._recognizer.recognize(case.message)
            predicted = result.intent.value
            predictions.append(predicted)
            ground_truth.append(case.expected_intent)
            case_details.append(
                {
                    "message": case.message,
                    "expected": case.expected_intent,
                    "predicted": predicted,
                    "confidence": result.confidence,
                    "reasoning": result.reasoning,
                }
            )
        correct = sum(p == g for p, g in zip(predictions, ground_truth))
        accuracy = correct / len(predictions) if predictions else 0.0
        labels = sorted(set(ground_truth + predictions))
        per_class: Dict[str, Dict[str, float]] = {}
        for label in labels:
            tp = sum(p == label and g == label for p, g in zip(predictions, ground_truth))
            fp = sum(p == label and g != label for p, g in zip(predictions, ground_truth))
            fn = sum(p != label and g == label for p, g in zip(predictions, ground_truth))
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            per_class[label] = {"precision": prec, "recall": rec, "f1": f1}
        macro_f1 = statistics.mean(v["f1"] for v in per_class.values()) if per_class else 0.0
        return {
            "accuracy": round(accuracy, 4),
            "macro_f1": round(macro_f1, 4),
            "per_class": per_class,
            "total": len(cases),
            "correct": correct,
            "cases": case_details,
        }


class EndToEndEvaluator:
    PASS_THRESHOLD = 0.75

    def __init__(
        self,
        orchestrator,
        recognizer: IntentRecognizer,
        api_key: str = "",
        base_url: Optional[str] = None,
        model: str = "deepseek-chat",
        baseline_path: Optional[str] = None,
    ):
        client = DeepSeekClient(api_key=api_key, base_url=base_url or "https://api.deepseek.com") if api_key else None
        if client is not None and not getattr(client, "available", False):
            client = None
        self._orchestrator = orchestrator
        self._judge = LLMJudge(client, model)
        self._intent_evaluator = IntentEvaluator(recognizer)
        self._history: List[EvalReport] = []
        self._baseline_path = pathlib.Path(baseline_path) if baseline_path else None
        self._baseline: Optional[EvalReport] = self._load_baseline()

    async def run(
        self,
        intent_cases: Optional[List[IntentTestCase]] = None,
        dialog_cases: Optional[List[Dict[str, Any]]] = None,
    ) -> EvalReport:
        # 端到端评测包含两段：意图识别和整轮对话质量。
        results: List[EvalResult] = []
        all_scores: Dict[str, List[float]] = {"relevance": [], "accuracy": [], "completeness": [], "helpfulness": []}

        intent_metrics: Dict[str, Any] = {}
        if intent_cases:
            intent_metrics = await self._intent_evaluator.evaluate(intent_cases)
            passed = intent_metrics["accuracy"] >= self.PASS_THRESHOLD
            results.append(
                EvalResult(
                    test_id="intent_recognition",
                    passed=passed,
                    scores={"accuracy": intent_metrics["accuracy"], "macro_f1": intent_metrics["macro_f1"]},
                    detail=f"准确率 {intent_metrics['accuracy']:.1%}，Macro-F1 {intent_metrics['macro_f1']:.3f}",
                    metadata={"total": intent_metrics.get("total", 0), "correct": intent_metrics.get("correct", 0), "cases": intent_metrics.get("cases", [])},
                )
            )

        if dialog_cases:
            for i, case in enumerate(dialog_cases):
                case_results = await self._evaluate_dialog_case(case, i)
                results.extend(case_results)
                for result in case_results:
                    for key in all_scores:
                        if key in result.scores:
                            all_scores[key].append(result.scores[key])

        avg_scores = {key: round(statistics.mean(values), 4) for key, values in all_scores.items() if values}
        if intent_metrics:
            avg_scores["intent_accuracy"] = intent_metrics["accuracy"]
        passed_count = sum(1 for result in results if result.passed)
        pass_rate = passed_count / len(results) if results else 0.0
        regressions = self._detect_regressions(avg_scores)
        recommendations = self._recommendations(avg_scores)
        report = EvalReport(
            timestamp=datetime.now().isoformat(),
            total=len(results),
            passed=passed_count,
            pass_rate=round(pass_rate, 4),
            avg_scores=avg_scores,
            regressions=regressions,
            recommendations=recommendations,
            results=results,
        )
        self._history.append(report)
        self._save_baseline(report)
        return report

    async def _evaluate_dialog_case(self, case: Dict[str, Any], case_idx: int) -> List[EvalResult]:
        from agents.agent_orchestrator import Request as OrcReq

        # 每个 case 按轮次喂给编排器，最后再由评审器打分。
        questions = self._dialog_turns(case)
        if not questions:
            return []
        conv_id = str(case.get("conv_id") or f"eval_{case_idx}")
        user_id = str(case.get("user_id") or "eval_user")
        history: List[Dict[str, str]] = []
        results: List[EvalResult] = []
        for turn_idx, question in enumerate(questions):
            context = self._history_context(history)
            orch_req = OrcReq(message=question, user_id=user_id, conv_id=conv_id, context=context, history=history[-6:] if history else None)
            orch_result = await self._orchestrator.run(orch_req)
            actual_answer = orch_result.response
            scores = await self._judge.judge(question, actual_answer, context=context or None)
            passed = scores.overall >= self.PASS_THRESHOLD
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": actual_answer})
            test_id = f"dialog_{case_idx}" if len(questions) == 1 else f"dialog_{case_idx}_turn_{turn_idx}"
            results.append(
                EvalResult(
                    test_id=test_id,
                    passed=passed,
                    scores={
                        "relevance": scores.relevance,
                        "accuracy": scores.accuracy,
                        "completeness": scores.completeness,
                        "helpfulness": scores.helpfulness,
                        "overall": scores.overall,
                    },
                    detail=f"Q: {question[:30]}... → 综合评分 {scores.overall:.3f}",
                    metadata={
                        "question": question,
                        "response": actual_answer,
                        "agent_type": orch_result.agent_type.value,
                        "intent": orch_result.intent.value if orch_result.intent else None,
                        "turn": turn_idx,
                        "conv_id": conv_id,
                        "judge_failed": scores.judge_failed,
                        "judge_error": scores.error,
                    },
                )
            )
        return results

    @staticmethod
    def _dialog_turns(case: Dict[str, Any]) -> List[str]:
        # 兼容单轮和多轮评测输入。
        turns = case.get("turns")
        if isinstance(turns, list):
            return [str(t) for t in turns if str(t).strip()]
        question = case.get("question")
        return [str(question)] if question else []

    @staticmethod
    def _history_context(history: List[Dict[str, str]]) -> str:
        # 把多轮上下文压成一个可读字符串给 LLM judge。
        if not history:
            return ""
        return "[评测多轮历史]\n" + "\n".join(f"{item['role']}: {item['content']}" for item in history[-8:])

    def _detect_regressions(self, current: Dict[str, float]) -> List[str]:
        # 和基线比一下，看看有没有明显退化。
        prev_report = self._history[-1] if self._history else self._baseline
        if prev_report is None:
            return []
        prev = prev_report.avg_scores
        regressions = []
        for metric, value in current.items():
            if metric in prev and prev[metric] > 0:
                delta = (value - prev[metric]) / prev[metric]
                if delta < -0.05:
                    regressions.append(f"{metric}: {prev[metric]:.3f} → {value:.3f} (退化 {abs(delta):.1%})")
        return regressions

    def _recommendations(self, scores: Dict[str, float]) -> List[str]:
        # 根据弱项给出下一步优化建议。
        recs = []
        if scores.get("intent_accuracy", 1.0) < 0.9:
            recs.append("意图识别准确率 < 90%：补充外卖场景示例和规则样本")
        if scores.get("relevance", 1.0) < 0.75:
            recs.append("相关性偏低：检查 Agent system prompt 和路由条件")
        if scores.get("completeness", 1.0) < 0.75:
            recs.append("完整性偏低：让回复包含状态、ETA 和下一步动作")
        if scores.get("helpfulness", 1.0) < 0.75:
            recs.append("有用性偏低：补充明确追问和处理指引")
        if not recs:
            recs.append("所有指标均达标，继续保持")
        return recs

    @property
    def history(self) -> List[EvalReport]:
        return self._history

    def _load_baseline(self) -> Optional[EvalReport]:
        if not self._baseline_path or not self._baseline_path.exists():
            return None
        try:
            data = json.loads(self._baseline_path.read_text(encoding="utf-8"))
            return self._report_from_dict(data)
        except Exception as ex:
            logger.warning("读取评测基线失败: %s", ex)
            return None

    def _save_baseline(self, report: EvalReport) -> None:
        if not self._baseline_path:
            return
        try:
            self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
            self._baseline_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
            self._baseline = report
        except Exception as ex:
            logger.warning("保存评测基线失败: %s", ex)

    @staticmethod
    def _report_from_dict(data: Dict[str, Any]) -> EvalReport:
        return EvalReport(
            timestamp=data.get("timestamp", ""),
            total=int(data.get("total", 0)),
            passed=int(data.get("passed", 0)),
            pass_rate=float(data.get("pass_rate", 0.0)),
            avg_scores=dict(data.get("avg_scores", {})),
            regressions=list(data.get("regressions", [])),
            recommendations=list(data.get("recommendations", [])),
            results=[
                EvalResult(
                    test_id=item.get("test_id", ""),
                    passed=bool(item.get("passed", False)),
                    scores=dict(item.get("scores", {})),
                    detail=item.get("detail", ""),
                    metadata=dict(item.get("metadata", {})),
                )
                for item in data.get("results", [])
            ],
        )


DEFAULT_INTENT_CASES: List[IntentTestCase] = [
    IntentTestCase("我的外卖还要多久到？", "delivery_progress"),
    IntentTestCase("帮我看看订单状态", "order_status"),
    IntentTestCase("订单号是 MT20260829001，帮我看一下", "order_status"),
    IntentTestCase("我要退款", "refund"),
    IntentTestCase("帮我取消订单", "cancel_order"),
    IntentTestCase("商家出餐太慢了", "merchant_delay"),
    IntentTestCase("骑手电话多少", "rider_contact"),
    IntentTestCase("我这单少送了一份米饭", "missing_item"),
    IntentTestCase("餐品送错了", "wrong_item"),
    IntentTestCase("我想修改收货地址", "address_change"),
    IntentTestCase("优惠券为什么不能用", "coupon_rule"),
    IntentTestCase("验证码一直收不到", "login_issue"),
    IntentTestCase("App 页面打不开", "platform_error"),
    IntentTestCase("转人工客服", "human_handoff"),
]

DEFAULT_DIALOG_CASES: List[Dict[str, Any]] = [
    {"user_id": "u1001", "question": "我的外卖还没到"},
    {"user_id": "u1001", "question": "订单号是 MT20260829001，帮我看一下"},
    {"user_id": "u1001", "question": "商家一直没出餐，我要不要退款？"},
    {"user_id": "u3003", "question": "订单号是 MT20260829004，为什么还没骑手接单"},
    {"user_id": "u4004", "question": "订单显示送达了，但我没收到"},
    {"user_id": "u5005", "question": "订单号 MT20260829006 地址定位错了怎么办"},
    {"user_id": "u1001", "turns": ["你好，我想查一下外卖进度", "订单号是 MT20260829002", "现在骑手到哪了？"]},
    {"question": "验证码一直收不到，怎么办？"},
]
