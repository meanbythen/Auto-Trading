"""
日内浮盈飘单 · 方向选择器。

功能：只回答「今天做多、做空，还是先等」。
     现在有：观察结束顺大方向、昨日密集区、开盘箱体收盘突破、MACD一轮循环。后面要试别的，在这里加一类、登记进表即可。
原理：开仓窗口、手数、止损都不在这里算。选择器看完 K 线，把方向和一句话原因交回去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Protocol

import pandas as pd

from src.strategy.daily_body import judge_opening_break

# 配置页 / yaml 里写的键。
DIRECTION_ETH_TREND = "eth_trend"
DIRECTION_DAY_CLUSTER = "day_cluster"
DIRECTION_OPENING_BOX = "opening_box"
DIRECTION_MACD_CYCLE = "macd_cycle"

DIRECTION_LABELS: dict[str, str] = {
    DIRECTION_ETH_TREND: "观察结束顺大方向",
    DIRECTION_DAY_CLUSTER: "观察结束看昨日密集区",
    DIRECTION_OPENING_BOX: "开盘箱体突破",
    DIRECTION_MACD_CYCLE: "MACD一轮循环",
}


def normalize_direction_key(value: Any) -> str:
    """把配置或界面文字收成选择器键。不认识的退回观察结束顺大方向。"""
    text = str(value or "").strip()
    if text in DIRECTION_LABELS:
        return text
    if text in {"观察结束顺大方向", "eth_trend", "eth-trend"}:
        return DIRECTION_ETH_TREND
    if text in {
        "观察结束看昨日密集区",
        "昨日密集区",
        "成交密集区",
        "day_cluster",
        "day-cluster",
    }:
        return DIRECTION_DAY_CLUSTER
    if text in {"5分钟MACD一轮循环", "MACD一轮循环", "macd_cycle"}:
        return DIRECTION_MACD_CYCLE
    for key, label in DIRECTION_LABELS.items():
        if text == label:
            return key
    return DIRECTION_ETH_TREND


def direction_label(value: Any) -> str:
    """配置键换成配置页上的中文名。"""
    return DIRECTION_LABELS[normalize_direction_key(value)]


@dataclass
class DirectionPick:
    """一次定方向的结果。side 只能是 long / short / none。"""

    side: str
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DirectionContext:
    """选择器看盘时需要的现场。host 是策略本体，用来读开盘前 ATR、跳空等已有工具。"""

    symbol: str
    bars: pd.DataFrame
    now: datetime
    anchor: dtime
    eth_minutes: int
    timeframe: str
    second_try: bool = False
    host: Any = None


class DirectionSelector(Protocol):
    """定方向模块的接口。新方案只要实现 pick，并登记到 DIRECTION_REGISTRY。"""

    key: str
    label: str

    def pick(self, ctx: DirectionContext) -> DirectionPick:
        """根据当前 K 线给出方向。没有把握时 side 必须是 none。"""


class OpeningBoxDirection:
    """
    现在正在用的定方向办法：观察窗口走完后，收盘站上箱子做多，跌破做空。

    影线刺出又收回来不算。窗口多长由生命周期模块管，这里只看已经画好的箱子。
    """

    key = DIRECTION_OPENING_BOX
    label = DIRECTION_LABELS[DIRECTION_OPENING_BOX]
    needs_opening_box = True

    def pick(self, ctx: DirectionContext) -> DirectionPick:
        side, reason = judge_opening_break(
            ctx.bars, ctx.now, ctx.anchor, ctx.eth_minutes, ctx.timeframe
        )
        if side not in {"long", "short"}:
            side = "none"
        return DirectionPick(side=side, reason=reason, meta={"direction": self.key})


def make_direction(key: Any, params: dict[str, Any] | None = None) -> DirectionSelector:
    """按配置键做出选择器。写错名字时退回观察结束顺大方向。"""
    # 登记表放函数里，避免和 macd_cycle / eth_trend 互相 import。
    from .day_cluster import DayClusterDirection
    from .eth_trend import EthTrendDirection
    from .macd_cycle import MacdCycleDirection

    registry: dict[str, type] = {
        DIRECTION_ETH_TREND: EthTrendDirection,
        DIRECTION_DAY_CLUSTER: DayClusterDirection,
        DIRECTION_OPENING_BOX: OpeningBoxDirection,
        DIRECTION_MACD_CYCLE: MacdCycleDirection,
    }
    name = normalize_direction_key(key)
    maker = registry.get(name) or EthTrendDirection
    inst = maker()
    configure = getattr(inst, "configure", None)
    if callable(configure):
        configure(params or {})
    return inst


# 给外面列名单用。真正创建走 make_direction。
DIRECTION_REGISTRY: dict[str, str] = {
    DIRECTION_ETH_TREND: "EthTrendDirection",
    DIRECTION_DAY_CLUSTER: "DayClusterDirection",
    DIRECTION_OPENING_BOX: "OpeningBoxDirection",
    DIRECTION_MACD_CYCLE: "MacdCycleDirection",
}
