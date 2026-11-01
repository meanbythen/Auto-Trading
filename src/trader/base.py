"""
交易执行抽象层
功能：定义「下单请求 / 回报」统一接口，方便在 模拟、GUI点击、CTP 之间切换。
原理：策略只依赖 TraderBase，不关心底层怎么成交（依赖倒置 / 策略模式）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Side = Literal["buy_open", "sell_open", "close"]


@dataclass
class OrderRequest:
    """一笔下单请求。"""

    symbol: str
    side: Side
    volume: int
    price: float | None = None   # None 表示市价/跟盘（GUI 场景常不填）
    # opponent：启动常驻用的价格方式，不改价格框，沿用对手价。
    # limit：下单模拟器把 price 打进价格框，用来挂一张远离现价的委托。
    gui_price: Literal["opponent", "limit"] = "opponent"
    remark: str = ""
    # 平仓时告诉 CTP 现在是多仓还是空仓。多仓卖平，空仓买平。GUI 点击不使用这两个字段。
    position_side: str | None = None
    stop_price: float | None = None
    bar_id: str = ""


@dataclass
class OrderResult:
    """下单结果。"""

    ok: bool
    message: str
    order_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    # True=仅表示「指令已发出」，并非交易所成交确认（GUI 点击路径典型如此）
    assumed_fill: bool = False


class TraderBase(ABC):
    """所有交易执行器的基类。"""

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResult:
        """提交报单。"""

    def buy_open(self, symbol: str, volume: int, price: float | None = None) -> OrderResult:
        """买开（做多开仓）。"""
        return self.place_order(OrderRequest(symbol, "buy_open", volume, price))

    def sell_open(self, symbol: str, volume: int, price: float | None = None) -> OrderResult:
        """卖开（做空开仓）。"""
        return self.place_order(OrderRequest(symbol, "sell_open", volume, price))

    def close_position(self, symbol: str, volume: int, price: float | None = None) -> OrderResult:
        """平仓。"""
        return self.place_order(OrderRequest(symbol, "close", volume, price))
