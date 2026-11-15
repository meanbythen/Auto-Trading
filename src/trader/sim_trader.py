"""
模拟交易执行器
功能：不连接任何真实软件，只在内存里记录「假装成交」。
原理：适合先跑通策略与风控；所有动作写入 data/sim_orders.jsonl。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from .base import DEFAULT_CAPABILITIES, Capabilities, OrderRequest, OrderResult, TraderBase
from .order_timeline import now_iso


class SimTrader(TraderBase):
    """纯模拟成交。"""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.record_file = self.data_dir / "sim_orders.jsonl"
        self.orders: list[dict[str, Any]] = []

    @property
    def capabilities(self) -> Capabilities:
        return DEFAULT_CAPABILITIES["sim"]

    def place_order(self, req: OrderRequest) -> OrderResult:
        order_id = f"SIM-{uuid.uuid4().hex[:8]}"
        simulated_at = now_iso()
        payload = {
            "order_id": order_id,
            "trace_id": req.trace_id,
            "bar_id": req.bar_id,
            "symbol": req.symbol,
            "side": req.side,
            "volume": req.volume,
            "filled_volume": int(req.volume),
            "price": req.price,
            "remark": req.remark,
            "simulated_fill": True,
            "simulated_at": simulated_at,
            "channel_result_at": simulated_at,
            "broker_confirmed": False,
        }
        self.orders.append(payload)

        # 追加写入文件，方便你用记事本查看历史
        with self.record_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        logger.info(
            "[模拟成交] {} {} {}手 order_id={}",
            req.side,
            req.symbol,
            req.volume,
            order_id,
        )
        return OrderResult(ok=True, message="模拟成交成功", order_id=order_id, raw=payload)
