"""
凯利突破

功能：收盘价向上突破最近 N 根（不含当根）最高价做多，向下跌破最低价做空；
     止损放在进场价外 ATR×K；止盈按止损距离×盈亏比，收盘到价平仓。
原理：开平仓只依赖价格突破和 ATR 止损距离，不预判行情方向；
     固定盈亏比保证「赚大亏小」，手数交给公共凯利 / 每笔风险仓位。
     参数：N=20、ATR=14、K=1.2、盈亏比=2.5。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .trend_base import TrendStrategyBase
from .types import PositionState, config_flag


class KellyBreakStrategy(TrendStrategyBase):
    """凯利突破：极简突破开仓，ATR 止损，固定盈亏比止盈。"""

    section = "kelly_break"
    label = "凯利突破"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        # 是否隔夜听公共「收盘清仓」总开关，不在本策略小节单独加键。
        st = cfg.get("strategy", {}) or {}
        self.flatten_at_close = config_flag(st.get("flatten_at_close", True), default=True)

        self.lookback = max(5, int(self.box.get("lookback", 20) or 20))
        self.stop_atr = float(self.box.get("stop_atr", 1.2) or 1.2)
        if self.stop_atr <= 0:
            self.stop_atr = 1.2
        self.take_profit = config_flag(self.box.get("take_profit", True), default=True)
        self.reward_risk = float(self.box.get("reward_risk", 2.5) or 2.5)
        if self.reward_risk <= 0:
            self.reward_risk = 2.5
        self.min_atr_ratio = max(0.0, float(self.box.get("min_atr_ratio", 0.0) or 0.0))

    def need_bars(self) -> int:
        return max(self.lookback + 1, self.atr_period + 1) + 5

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        close = pd.to_numeric(bars["close"], errors="coerce")
        if len(bars) < self.lookback + 1 or pd.isna(close.iloc[-1]):
            return None
        atr = self._atr(bars)
        if atr is None:
            return None
        prior = bars.iloc[-(self.lookback + 1) : -1]
        high_n = float(pd.to_numeric(prior["high"], errors="coerce").max())
        low_n = float(pd.to_numeric(prior["low"], errors="coerce").min())
        if pd.isna(high_n) or pd.isna(low_n):
            return None
        price = float(close.iloc[-1])
        ratio = atr / price if price > 0 else 0.0
        return {
            "high_n": high_n,
            "low_n": low_n,
            "atr": atr,
            "atr_ratio": ratio,
        }

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        atr = float(ind["atr"])
        ratio = float(ind["atr_ratio"])
        if self.min_atr_ratio > 0 and ratio < self.min_atr_ratio:
            return None
        close = float(bar["close"])
        high_n = float(ind["high_n"])
        low_n = float(ind["low_n"])
        pad = self.stop_atr * atr
        meta = {"high_n": high_n, "low_n": low_n, "atr": atr, "atr_ratio": ratio}
        if close > high_n:
            stop = close - pad
            return (
                "long",
                stop,
                f"凯利突破做多 收盘{close:.2f}>前{self.lookback}根高点{high_n:.2f} 止损{stop:.2f}",
                meta,
            )
        if close < low_n:
            stop = close + pad
            return (
                "short",
                stop,
                f"凯利突破做空 收盘{close:.2f}<前{self.lookback}根低点{low_n:.2f} 止损{stop:.2f}",
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
        target = entry + risk * self.reward_risk if pos.side == "long" else entry - risk * self.reward_risk
        if pos.side == "long" and close >= target:
            return f"凯利突破止盈平多 收盘{close:.2f}>=目标{target:.2f}"
        if pos.side == "short" and close <= target:
            return f"凯利突破止盈平空 收盘{close:.2f}<=目标{target:.2f}"
        return None
