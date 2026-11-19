"""
SAR 波段趋势（抛物线转向 + MA20 方向过滤）

功能：SAR 翻多且价格站上 MA20 做多；SAR 翻空且价格跌破 MA20 做空；SAR 反向时离场。
原理：对应《策略基础逻辑》第 4 套。SAR 本身就是移动止损，不需要再叠固定止损，
     所以这里把 SAR 线直接当 trail_stop。EMA 过滤用来挡掉一半的震荡翻转。
注意：跳空大的品种（燃油一类）SAR 基点会错位，靠市场状态过滤（ADX/ATR 分位）兜底。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .indicators import ema_series, parabolic_sar
from .trend_base import TrendStrategyBase
from .types import PositionState


class SarTrendStrategy(TrendStrategyBase):
    """SAR 翻转 + MA20 方向的波段策略。"""

    section = "sar_trend"
    label = "SAR波段"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        # 收盘清仓、信号/止损周期都在 TrendStrategyBase 里跟公共配置，这里只读 SAR 参数。
        self.af_start = float(self.box.get("af_start", 0.02) or 0.02)
        self.af_step = float(self.box.get("af_step", 0.02) or 0.02)
        self.af_max = float(self.box.get("af_max", 0.2) or 0.2)
        self.ma_filter = max(2, int(self.box.get("ma_filter", 20) or 20))

    def need_bars(self) -> int:
        return max(self.ma_filter, self.atr_period) + 5

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        close = pd.to_numeric(bars["close"], errors="coerce")
        sar = parabolic_sar(
            bars["high"],
            bars["low"],
            af_start=self.af_start,
            af_step=self.af_step,
            af_max=self.af_max,
        )
        ma = ema_series(close, self.ma_filter)
        if len(sar) < 2 or pd.isna(sar.iloc[-1]) or pd.isna(ma.iloc[-1]):
            return None
        close_now = float(close.iloc[-1])
        sar_now = float(sar.iloc[-1])
        sar_prev = float(sar.iloc[-2])
        bull_now = close_now > sar_now
        bull_prev = float(close.iloc[-2]) > sar_prev
        return {
            "sar": sar_now,
            "sar_prev": sar_prev,
            "ma": float(ma.iloc[-1]),
            "bull": bull_now,
            "flipped_up": bull_now and not bull_prev,
            "flipped_down": (not bull_now) and bull_prev,
        }

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        close = float(bar["close"])
        ma = float(ind["ma"])
        sar = float(ind["sar"])
        if ind["flipped_up"] and close > ma:
            stop = min(sar, close - 1e-9)
            return (
                "long",
                stop,
                f"SAR翻多做多 收盘{close:.2f}>MA{self.ma_filter}={ma:.2f} SAR止损{stop:.2f}",
                {"sar": sar, "ma": ma},
            )
        if ind["flipped_down"] and close < ma:
            stop = max(sar, close + 1e-9)
            return (
                "short",
                stop,
                f"SAR翻空做空 收盘{close:.2f}<MA{self.ma_filter}={ma:.2f} SAR止损{stop:.2f}",
                {"sar": sar, "ma": ma},
            )
        return None

    def exit_reason(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> str | None:
        close = float(bar["close"])
        sar = float(ind["sar"])
        if pos.side == "long" and not ind["bull"]:
            return f"SAR波段平多 SAR翻空 收盘{close:.2f}<SAR{sar:.2f}"
        if pos.side == "short" and ind["bull"]:
            return f"SAR波段平空 SAR翻多 收盘{close:.2f}>SAR{sar:.2f}"
        return None

    def trail_stop(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> float:
        """SAR 线当移动止损，只往有利方向收。"""
        sar = float(ind["sar"])
        current = float(pos.trail_stop or pos.initial_stop)
        stop = max(current, sar) if pos.side == "long" else min(current, sar)
        pos.trail_stop = stop
        return stop
