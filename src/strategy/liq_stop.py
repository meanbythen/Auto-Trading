"""
公共止损流动性回流（以损定开）

功能：用最近 N 根 K 线的高低点拐点，估算当前周期多数人的止损聚集区（公共止损区）。
     价格先击穿这个区间、把止损单吃掉，再收回区间外侧，才顺着趋势方向进场；
     止损放在公共止损区外侧 K 倍 ATR，止盈按固定盈亏比，收盘到价平仓。
原理：显著高低点外侧是散户止损最密集的地方，也是主力/做市商获取被动流动性的位置。
     扫完止损后如果原趋势还在，价格容易回流；没有回流的震荡市由 ADX/DI 过滤掉。
     只认收盘突破/收回，不用影线预判；手数继续交给公共凯利 / 每笔风险仓位。
     参数：止损区回看 20 根、ATR 20、ADX 14/20、进场偏移 0.5ATR、止损外侧 1ATR、盈亏比 2。
"""

from __future__ import annotations

import math
import statistics
from typing import Any

import pandas as pd

from .indicators import average_true_range, price_1d
from .market_state import adx_series
from .swing_box import find_pivots
from .trend_base import TrendStrategyBase
from .types import PositionState, config_flag


def _zone(levels: list[float], fallback: float, tol: float) -> tuple[float, float]:
    """把拐点聚类成区间。拐点不够时用最近极值兜底，区间宽度按相对容忍度算。"""
    center = statistics.median(levels) if levels else fallback
    pad = max(abs(center) * tol, 1e-9)
    return center - pad, center + pad


class LiquidityStopStrategy(TrendStrategyBase):
    """以损定开：击穿公共止损区、回流确认后顺着趋势进场。"""

    section = "liq_stop"
    label = "以损定开"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        # 是否隔夜听公共「收盘清仓」总开关，不在本策略小节单独加键。
        st = cfg.get("strategy", {}) or {}
        self.flatten_at_close = config_flag(st.get("flatten_at_close", True), default=True)

        self.zone_lookback = max(10, int(self.box.get("zone_lookback", 20) or 20))
        self.zone_tol = min(0.2, max(0.0005, float(self.box.get("zone_tol", 0.005) or 0.005)))
        self.pivot_span = max(1, int(self.box.get("pivot_span", 2) or 2))
        self.adx_period = max(2, int(self.box.get("adx_period", 14) or 14))
        self.adx_min = max(0.0, float(self.box.get("adx_min", 20.0) or 0.0))
        self.break_window = max(1, int(self.box.get("break_window", 3) or 3))
        self.entry_offset = max(0.0, float(self.box.get("entry_offset", 0.5) or 0.5))
        self.stop_atr = float(self.box.get("stop_atr", 1.0) or 1.0)
        if self.stop_atr <= 0:
            self.stop_atr = 1.0
        self.take_profit = config_flag(self.box.get("take_profit", True), default=True)
        self.reward_risk = float(self.box.get("reward_risk", 2.0) or 2.0)
        if self.reward_risk <= 0:
            self.reward_risk = 2.0
        self.vol_mult = max(0.0, float(self.box.get("vol_mult", 0.0) or 0.0))

    def need_bars(self) -> int:
        return max(
            self.zone_lookback + self.pivot_span * 2 + self.break_window + 2,
            self.atr_period + self.break_window + 2,
            self.adx_period * 2 + self.break_window + 2,
        )

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        if bars is None or len(bars) < self.need_bars():
            return None
        # 止损区只用「当前这根之前」的 K 线画，避免把当根算进区间。
        hist = bars.iloc[-(self.zone_lookback + 1) : -1]
        if len(hist) < self.zone_lookback:
            return None
        highs = [float(x) for x in price_1d(hist["high"]).tolist()]
        lows = [float(x) for x in price_1d(hist["low"]).tolist()]
        peaks, valleys = find_pivots(highs, lows, self.pivot_span)

        # 多头止损区：支撑下方，散户多单止损埋在这里。
        long_fallback = min(lows) if lows else 0.0
        zone_l, zone_h = _zone([float(v) for _, v in valleys], long_fallback, self.zone_tol)

        # 空头止损区：压力上方，散户空单止损埋在这里。
        short_fallback = max(highs) if highs else 0.0
        short_l, short_h = _zone([float(p) for _, p in peaks], short_fallback, self.zone_tol)

        atr_s = average_true_range(
            bars["high"], bars["low"], bars["close"], period=self.atr_period
        )
        atr = float(atr_s.iloc[-1]) if len(atr_s) else 0.0
        if not math.isfinite(atr) or atr <= 0:
            return None

        # 趋势方向也用「击穿之前」的 K 线算：扫止损那根自己的反向长影线
        # 会把 -DI/+DI 瞬间拉偏，不能拿它来否掉刚出现的回流信号。
        adx_s, plus_s, minus_s = adx_series(
            hist["high"], hist["low"], hist["close"], period=self.adx_period
        )
        adx = float(adx_s.iloc[-1]) if len(adx_s) else 0.0
        plus_di = float(plus_s.iloc[-1]) if len(plus_s) else 0.0
        minus_di = float(minus_s.iloc[-1]) if len(minus_s) else 0.0
        if not math.isfinite(adx):
            adx = 0.0

        # 击穿窗口之前的均量，用来判断扫止损有没有放量。数据不够就不拦。
        vol_base = 0.0
        if self.vol_mult > 0 and "volume" in bars:
            vol = price_1d(bars["volume"])
            end = len(vol) - self.break_window - 1
            start = end - self.zone_lookback + 1
            if start >= 0:
                avg = float(vol.iloc[start : end + 1].mean())
                if math.isfinite(avg) and avg > 0:
                    vol_base = avg

        return {
            "zone_l": zone_l,
            "zone_h": zone_h,
            "short_l": short_l,
            "short_h": short_h,
            "atr": atr,
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "vol_base": vol_base,
        }

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        atr = float(ind["atr"])
        adx = float(ind["adx"])
        if self.adx_min > 0 and adx < self.adx_min:
            return None
        plus_di = float(ind["plus_di"])
        minus_di = float(ind["minus_di"])

        if self.vol_mult > 0 and float(ind["vol_base"]) > 0:
            vol = price_1d(bars["volume"])
            break_vol = float(vol.iloc[-self.break_window :].max())
            if break_vol < float(ind["vol_base"]) * self.vol_mult:
                return None

        close = float(bar["close"])
        recent_low = float(price_1d(bars["low"]).iloc[-self.break_window :].min())
        recent_high = float(price_1d(bars["high"]).iloc[-self.break_window :].max())
        zone_l = float(ind["zone_l"])
        zone_h = float(ind["zone_h"])
        short_l = float(ind["short_l"])
        short_h = float(ind["short_h"])
        meta = {
            "zone_l": zone_l,
            "zone_h": zone_h,
            "atr": atr,
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
        }

        # 多头：先向下跌穿多头止损区（吃掉止损），再收回区间上沿之外。
        if plus_di > minus_di and recent_low < zone_l:
            trigger = zone_h + self.entry_offset * atr if self.entry_offset > 0 else zone_h
            if close > trigger:
                stop = zone_l - self.stop_atr * atr
                return (
                    "long",
                    stop,
                    f"以损定开做多 击穿止损区{zone_l:.2f}~{zone_h:.2f}后收回{close:.2f} 止损{stop:.2f}",
                    meta,
                )

        # 空头：先向上击穿空头止损区，再跌回区间下沿之外。
        if minus_di > plus_di and recent_high > short_h:
            trigger = short_l - self.entry_offset * atr if self.entry_offset > 0 else short_l
            if close < trigger:
                stop = short_h + self.stop_atr * atr
                return (
                    "short",
                    stop,
                    f"以损定开做空 击穿止损区{short_l:.2f}~{short_h:.2f}后跌回{close:.2f} 止损{stop:.2f}",
                    meta,
                )
        return None

    def exit_reason(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> str | None:
        if not self.take_profit:
            return None
        entry = float(pos.entry_price or 0.0)
        stop = float(pos.initial_stop or 0.0)
        if entry <= 0 or stop <= 0:
            return None
        close = float(bar["close"])
        risk = entry - stop if pos.side == "long" else stop - entry
        if risk <= 0:
            return None
        if pos.side == "long":
            target = entry + risk * self.reward_risk
        else:
            target = entry - risk * self.reward_risk
        if pos.side == "long" and close >= target:
            return f"以损定开止盈平多 收盘{close:.2f}>=目标{target:.2f}"
        if pos.side == "short" and close <= target:
            return f"以损定开止盈平空 收盘{close:.2f}<=目标{target:.2f}"
        return None
