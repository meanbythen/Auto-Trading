"""期权对冲策略：给期货持仓买黑天鹅保护。

功能：在配置好的虚值 put/call 上，等权利金回落到近期区间偏低位置时买入，
     持有到止损或到期前强平。
原理：保护性期权是「买入保险」，不需要期货那边的保证金；
     买贵了会长期磨损，所以只在权利金不追高时进场。
     哪条期货腿对应哪张期权，由 strategy.legs / 组合额度配置决定，
     本策略只负责期权这一腿的开平。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .option_long import OptionLongStrategy


class OptionHedgeStrategy(OptionLongStrategy):
    """黑天鹅保护：权利金低位买入，持有到期或止损。"""

    section = "option_hedge"
    label = "期权黑天鹅对冲"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self.lookback = max(3, int(self.box.get("lookback", 30) or 30))
        pct = float(self.box.get("premium_stop_pct", 50.0) or 50.0)
        self.premium_stop_pct = pct if 0 < pct < 100 else 50.0
        # 权利金低于近期区间中值的这个比例才买；1.0 = 只要不高于中值就买。
        ratio = float(self.box.get("entry_ratio", 1.0) or 1.0)
        self.entry_ratio = min(max(ratio, 0.1), 1.0)
        # 保护比例只用于说明文字，真正的数量由组合额度和算仓决定。
        self.protection_pct = max(0.0, float(self.box.get("protect_pct", 100.0) or 100.0))

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        close = float(bar["close"])
        if close <= 0 or close < self.min_premium:
            return None
        mid_n = float(ind["mid_n"])
        if close > mid_n * self.entry_ratio:
            return None
        stop = max(close * (1.0 - self.premium_stop_pct / 100.0), 0.01)
        return (
            "long",
            stop,
            (
                f"{self.label} 权利金{close:g}<=区间中值{mid_n:g}×{self.entry_ratio:g}"
                f"，买入保护（目标保护比例{self.protection_pct:g}%），止损{stop:g}"
            ),
            {
                "high_n": float(ind["high_n"]),
                "low_n": float(ind["low_n"]),
                "mid_n": mid_n,
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
        """保护单不设止盈，只等止损或到期强平。"""
        return None
