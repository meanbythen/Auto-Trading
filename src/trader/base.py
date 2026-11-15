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

from src.trader.order_timeline import new_trace_id


Side = Literal["buy_open", "sell_open", "close"]


@dataclass(frozen=True)
class Capabilities:
    """通道能力。风控、调度和界面只看这里，不看具体是哪一家。"""

    can_query_positions: bool = False
    can_query_orders: bool = False
    confirms_fill: bool = False
    supports_symbol_switch: bool = False
    supports_price_mode: bool = False
    supports_cancel: bool = False
    # 靠桌面窗口点击下单：需要打开软件、登录、标定坐标、改价格方式。
    window_driven: bool = False

    @property
    def orders_via_broker(self) -> bool:
        """委托直接进柜台报单表，并且能查柜台委托（CTP 这一类）。"""
        return self.confirms_fill and self.can_query_orders


# 各通道的默认能力。未知通道按「只能下单、不能查询」处理。
DEFAULT_CAPABILITIES: dict[str, Capabilities] = {
    "sim": Capabilities(
        confirms_fill=True,
        supports_symbol_switch=True,
        supports_price_mode=True,
    ),
    "gui": Capabilities(
        supports_symbol_switch=True,
        supports_price_mode=True,
        window_driven=True,
    ),
    "ctp": Capabilities(
        can_query_positions=True,
        can_query_orders=True,
        confirms_fill=True,
        supports_symbol_switch=True,
        supports_price_mode=True,
        supports_cancel=True,
    ),
}


def capabilities_for(executor: Any) -> Capabilities:
    """按通道名取默认能力。认不出来的通道按「只能下单、不能查询」处理。"""
    name = str(executor or "").strip().lower()
    return DEFAULT_CAPABILITIES.get(name, Capabilities())


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
    # 本地时间线关联号和价格证据；不会作为柜台编号或柜台成交证明。
    trace_id: str = field(default_factory=new_trace_id)
    meta: dict[str, Any] = field(default_factory=dict)


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

    @property
    def capabilities(self) -> Capabilities:
        """通道能力。默认只能下单：不查柜台、不确认成交、不依赖窗口。"""
        return Capabilities()

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
