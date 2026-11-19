"""
布林带趋势突破（顺中轨方向，只做突破，不做轨道反转）

功能：价格在中轨（EMA）上方且收盘突破上轨做多；中轨下方且跌破下轨做空；收盘回落中轨离场。
原理：对应《策略基础逻辑》第 3 套。布林轨道自带波动率自适应：
     波动放大时通道变宽，不容易被正常波动扫掉；缩窄时的假突破交给市场状态过滤拦。
止损：开仓价外 1.5 倍 ATR（文档推荐口径，比「跌破中轨」更稳）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .indicators import ema_series
from .trend_base import TrendStrategyBase
from .types import PositionState


class BollingerBreakStrategy(TrendStrategyBase):
    """布林通道顺势突破。"""

    section = "bollinger"
    label = "布林突破"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        # 收盘清仓、信号/止损周期都在 TrendStrategyBase 里跟公共配置，这里只读轨道参数。
        self.boll_window = max(10, int(self.box.get("boll_window", 20) or 20))
        self.boll_dev = float(self.box.get("boll_dev", 2.0) or 2.0)
        if self.boll_dev <= 0:
            self.boll_dev = 2.0
        self.atr_stop_mult = float(self.box.get("atr_stop_mult", 1.5) or 1.5)
        if self.atr_stop_mult <= 0:
            self.atr_stop_mult = 1.5

    def need_bars(self) -> int:
        return max(self.boll_window, self.atr_period) + 5

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        close = pd.to_numeric(bars["close"], errors="coerce")
        mid = ema_series(close, self.boll_window)
        std = close.rolling(self.boll_window).std(ddof=0)
        if pd.isna(mid.iloc[-1]) or pd.isna(std.iloc[-1]):
            return None
        atr = self._atr(bars)
        if atr is None:
            return None
        mid_now = float(mid.iloc[-1])
        std_now = float(std.iloc[-1])
        return {
            "mid": mid_now,
            "up": mid_now + self.boll_dev * std_now,
            "down": mid_now - self.boll_dev * std_now,
            "atr": atr,
        }

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        close = float(bar["close"])
        mid = float(ind["mid"])
        up = float(ind["up"])
        down = float(ind["down"])
        atr = float(ind["atr"])
        pad = self.atr_stop_mult * atr
        if close > mid and close > up:
            stop = close - pad
            return (
                "long",
                stop,
                f"布林突破做多 收盘{close:.2f}>上轨{up:.2f} 中轨{mid:.2f} 止损{stop:.2f}",
                {"mid": mid, "up": up, "atr": atr},
            )
        if close < mid and close < down:
            stop = close + pad
            return (
                "short",
                stop,
                f"布林突破做空 收盘{close:.2f}<下轨{down:.2f} 中轨{mid:.2f} 止损{stop:.2f}",
                {"mid": mid, "down": down, "atr": atr},
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
        mid = float(ind["mid"])
        if pos.side == "long" and close < mid:
            return f"布林突破平多 收盘{close:.2f}跌回中轨{mid:.2f}之下"
        if pos.side == "short" and close > mid:
            return f"布林突破平空 收盘{close:.2f}站回中轨{mid:.2f}之上"
        return None
