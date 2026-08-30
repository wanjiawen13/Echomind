import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.compat import async_to_thread

logger = logging.getLogger(__name__)


ACTIVE_STATUSES = {"merchant_preparing", "rider_assigned", "in_delivery", "waiting_dispatch"}


@dataclass
class OrderRecord:
    order_id: str
    user_id: str
    phone_last4: str
    status: str
    merchant_status: str
    rider_name: str
    rider_phone_masked: str
    eta_minutes: int
    last_update: str
    abnormal_reason: str = ""
    can_cancel: bool = False
    can_refund: bool = True
    restaurant_name: str = ""
    address: str = ""


class MockOrderStore:
    def __init__(self, source_path: str):
        self.source_path = Path(source_path)
        self._orders: List[OrderRecord] = []
        self.load()

    def load(self) -> None:
        if not self.source_path.exists():
            self._orders = []
            return
        raw = json.loads(self.source_path.read_text(encoding="utf-8"))
        self._orders = [OrderRecord(**item) for item in raw if isinstance(item, dict)]

    def _persist(self) -> None:
        self.source_path.parent.mkdir(parents=True, exist_ok=True)
        self.source_path.write_text(
            json.dumps([asdict(order) for order in self._orders], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def search(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        order_id = str(params.get("order_id") or "").strip()
        user_id = str(params.get("user_id") or "").strip()
        phone_last4 = str(params.get("phone_last4") or "").strip()

        if order_id:
            matches = [order for order in self._orders if order.order_id == order_id]
        else:
            matches = [order for order in self._orders if (not user_id or order.user_id == user_id)]
            if phone_last4:
                matches = [order for order in matches if order.phone_last4 == phone_last4]
            matches = sorted(matches, key=lambda x: x.last_update, reverse=True)

        if not matches:
            return {
                "found": False,
                "ambiguous": False,
                "matches": 0,
                "order": None,
                "candidates": [],
                "clarify_needed": True,
                "message": "暂时没有匹配到订单，请提供订单号，或补充手机号后四位继续查询。",
            }

        if len(matches) > 1 and not order_id:
            candidates = [self._to_public(order) for order in matches[:3]]
            return {
                "found": True,
                "ambiguous": True,
                "matches": len(matches),
                "order": self._to_public(matches[0]),
                "candidates": candidates,
                "clarify_needed": True,
                "message": "找到多个可能相关的订单，请补充具体订单号或手机号后四位，我帮您精确查询。",
            }

        order = matches[0]
        return {
            "found": True,
            "ambiguous": False,
            "matches": len(matches),
            "order": self._to_public(order),
            "candidates": [self._to_public(order)],
            "clarify_needed": False,
            "message": "查询成功",
        }

    async def search_handler(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        return await async_to_thread(self.search, params, context)

    def add_order(self, record: OrderRecord) -> None:
        self._orders = [order for order in self._orders if order.order_id != record.order_id]
        self._orders.append(record)
        self._persist()

    def seed(self, records: List[Dict[str, Any]]) -> None:
        self._orders = [OrderRecord(**record) for record in records]
        self._persist()

    @property
    def orders(self) -> List[OrderRecord]:
        return list(self._orders)

    def _to_public(self, order: OrderRecord) -> Dict[str, Any]:
        data = asdict(order)
        data["status_label"] = self._status_label(order.status)
        data["is_active"] = order.status in ACTIVE_STATUSES
        return data

    @staticmethod
    def _status_label(status: str) -> str:
        labels = {
            "merchant_preparing": "商家备餐中",
            "rider_assigned": "骑手已接单",
            "in_delivery": "配送中",
            "delivered": "已送达",
            "canceled": "已取消",
            "waiting_dispatch": "等待派单",
            "exception": "配送异常",
        }
        return labels.get(status, status)
