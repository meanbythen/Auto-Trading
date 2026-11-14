"""
观察结束顺大方向。

功能：开盘观察窗口走完后，EMA60 在 EMA120 上面就做多，下面就做空。
     不等箱子突破，也不等 MACD 循环。错了靠止损认赔。
原理：大方向用均线排列；窗口多长由生命周期管。倾角门槛出厂 0，只看排列。
"""

from __future__ import annotations

from typing import Any

from .direction import DIRECTION_ETH_TREND, DIRECTION_LABELS, DirectionContext, DirectionPick
from .macd_cycle import normalize_macd_timeframe
from .trend_gate import TrendAlignGate, gate_minutes_from_host


class EthTrendDirection:
    """
    观察时间一过，顺着 EMA60/120 开仓。

    不要箱子、不要红绿柱一轮。弱反转交给止损，不在这里再挡一层。
    """

    key = DIRECTION_ETH_TREND
    label = DIRECTION_LABELS[DIRECTION_ETH_TREND]
    needs_opening_box = False
    needs_eth_wait = True
    uses_trend_gate = True
    bar_minutes = 5

    def __init__(self) -> None:
        self.gate = TrendAlignGate({"trend_align_enabled": True, "trend_slope_min_deg": 0})
        self.gate.enabled = True

    def configure(self, params: dict[str, Any] | None = None) -> None:
        """读配置页的均线根数和倾角。这一套方向本身就是大方向，开关视为开。"""
        box = dict(params or {})
        box["trend_align_enabled"] = True
        self.gate = TrendAlignGate(box)
        self.gate.enabled = True
        tf = normalize_macd_timeframe(box.get("macd_timeframe"))
        self.bar_minutes = 1 if tf == "1" else 5

    def pick(self, ctx: DirectionContext) -> DirectionPick:
        """观察已由外面等完。这里只回答均线现在偏多还是偏空。"""
        minutes = self.bar_minutes
        host = ctx.host
        if host is not None:
            minutes = gate_minutes_from_host(host) or minutes
        snap = self.gate.snapshot(
            bars=ctx.bars,
            now=ctx.now,
            timeframe=ctx.timeframe,
            macd_minutes=minutes,
            force=True,
        )
        if snap.allow in {"long", "short"} and snap.ok:
            want = "做多" if snap.allow == "long" else "做空"
            return DirectionPick(
                side=snap.allow,
                reason=f"观察结束，{snap.reason}，顺着{want}",
                meta={"direction": self.key},
            )
        return DirectionPick(
            side="none",
            reason=snap.reason or "大方向还没排好，先等",
            meta={"direction": self.key},
        )
