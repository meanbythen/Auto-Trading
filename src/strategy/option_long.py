"""期权买方策略：末日期权投机。

功能：在期权自己的权利金 K 线上做突破买入，权利金回撤止损，盈利目标止盈；
     到期前由 option_lifecycle 强制平仓。
原理：期权买方最大亏损就是权利金，所以把权利金当价格、把回撤比例当止损，
     可直接复用趋势策略骨架和「以损定开」算仓，不需要期货保证金公式。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from src.account.option_lifecycle import force_close_reason

from .trend_base import TrendStrategyBase


class OptionLongStrategy(TrendStrategyBase):
    """末日期权买方：权利金突破买入，权利金回撤止损。"""

    section = "option_long"
    label = "末日期权买方"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self.lookback = max(3, int(self.box.get("lookback", 20) or 20))
        pct = float(self.box.get("premium_stop_pct", 30.0) or 30.0)
        self.premium_stop_pct = pct if 0 < pct < 100 else 30.0
        reward = float(self.box.get("reward_risk", 2.0) or 2.0)
        self.reward_risk = reward if reward > 0 else 2.0
        self.min_premium = max(0.0, float(self.box.get("min_premium", 0.0) or 0.0))

    def need_bars(self) -> int:
        return self.lookback + 2

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        close = pd.to_numeric(bars["close"], errors="coerce")
        if len(bars) < self.lookback + 1 or pd.isna(close.iloc[-1]):
            return None
        prior = bars.iloc[-(self.lookback + 1) : -1]
        high_n = float(pd.to_numeric(prior["high"], errors="coerce").max())
        low_n = float(pd.to_numeric(prior["low"], errors="coerce").min())
        if pd.isna(high_n) or pd.isna(low_n):
            return None
        return {
            "high_n": high_n,
            "low_n": low_n,
            "mid_n": (high_n + low_n) / 2.0,
            "close": float(close.iloc[-1]),
        }

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        close = float(bar["close"])
        if close <= 0 or close < self.min_premium:
            return None
        high_n = float(ind["high_n"])
        if close <= high_n:
            return None
        stop = max(close * (1.0 - self.premium_stop_pct / 100.0), 0.01)
        return (
            "long",
            stop,
            (
                f"{self.label} 权利金收盘{close:g}>前{self.lookback}根高点{high_n:g}"
                f"，止损{stop:g}（回撤{self.premium_stop_pct:g}%）"
            ),
            {
                "high_n": high_n,
                "low_n": float(ind["low_n"]),
                "premium_stop_pct": self.premium_stop_pct,
            },
        )

    def exit_reason(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: Any,
        ind: dict[str, Any],
    ) -> str | None:
        entry = float(pos.entry_price or 0.0)
        stop = float(pos.initial_stop or 0.0)
        if entry <= 0 or stop <= 0:
            return None
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + risk * self.reward_risk
        close = float(bar["close"])
        if close >= target:
            return f"{self.label}止盈 权利金收盘{close:g}>=目标{target:g}"
        return None

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
        forming: pd.DataFrame | None = None,
    ):
        """到期前强平优先于普通信号；其余完全交给趋势骨架。"""
        pos = self.positions.get(symbol)
        if pos is not None and pos.side != "flat":
            reason = force_close_reason(self.cfg, symbol, now)
            if reason:
                bar_id = str(bars.index[-1]) if bars is not None and len(bars) else ""
                return self._close_decision(
                    symbol, pos, now or datetime.now(), bar_id, reason
                )
        return super().evaluate(
            symbol,
            bars,
            now=now,
            trail_bars=trail_bars,
            trend_bars=trend_bars,
            forming=forming,
        )
