"""
整体大方向过滤：EMA60 / EMA120 排列 + 倾角。

功能：可开关。多头排列（60 在 120 上面）只允许做多，空头排列只允许做空。
     两条线的倾角都小于门槛时，当行情走平，多空都不开。
原理：倾角用「这几根里均线走了多少 ATR」换算成角度，换品种时尺度差不多。
     多头要两条线都往上够陡，空头要两条线都往下够陡。已经拿着的仓不走这里。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from src.data.periods import ohlc_series
from src.strategy.indicators import average_true_range, ema_series
from src.strategy.types import config_flag

from .macd_cycle import macd_period_view


def _int_param(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    return max(int(lo), min(int(hi), out))


def _float_param(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    if out != out:
        return float(default)
    return max(float(lo), min(float(hi), out))


def ema_angle_deg(now: float, prev: float, bars: int, scale: float) -> float:
    """
    均线自己的倾角（度）。正数往上、负数往下。

    竖直方向用「现在的均线值 − 回看那根的均线值」，不用 K 线收盘涨跌。
    水平方向用「回看根数 × 当时 ATR」，只是把时间换成和波动同一把尺。
    均线走了 1 倍 ATR、横过 5 根，大约 11 度。
    """
    n = max(1, int(bars))
    step = float(scale) if scale is not None and float(scale) > 1e-12 else 1.0
    rise = float(now) - float(prev)
    return math.degrees(math.atan(rise / (n * step)))


@dataclass
class TrendGateResult:
    """大方向过滤的结果。ok=False 时不要开新仓。"""

    ok: bool
    reason: str
    allow: str = ""  # long / short / 空


class TrendAlignGate:
    """
    EMA60 在 EMA120 上方只做多，下方只做空。
    倾角不够陡，两边都不做。
    """

    key = "ema60_120"
    label = "EMA60/120大方向"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        box = params or {}
        self.enabled = config_flag(box.get("trend_align_enabled"), default=False)
        self.fast = _int_param(box.get("trend_align_fast"), 60, 10, 200)
        self.slow = _int_param(box.get("trend_align_slow"), 120, 20, 400)
        if self.slow <= self.fast:
            self.slow = self.fast + 1
        self.lookback = _int_param(box.get("trend_slope_lookback"), 5, 2, 60)
        # 0=只看排列、不看斜。出厂 8 度，大约「5 根里走了 0.7 倍波动」。
        self.min_deg = _float_param(box.get("trend_slope_min_deg"), 8.0, 0.0, 80.0)
        self.atr_period = _int_param(box.get("trend_slope_atr_period"), 14, 5, 60)

    def snapshot(
        self,
        *,
        bars: pd.DataFrame,
        now: datetime,
        timeframe: str,
        macd_minutes: int,
        force: bool = False,
    ) -> TrendGateResult:
        """
        只看 EMA60/120 现在怎么排、斜不斜。不和某次开仓方向比。

        force=True 时不管开关，给「观察结束顺大方向」当方向用。
        """
        if not force and not self.enabled:
            return TrendGateResult(True, "")
        tag = "1分钟" if int(macd_minutes) <= 1 else "5分钟"
        view, wait = macd_period_view(bars, now, timeframe, macd_minutes)
        if view is None or wait:
            return TrendGateResult(False, wait or f"大方向还没有{tag}K线，先等")
        closes = ohlc_series(view, "close").dropna()
        need = self.slow + self.lookback + 5
        if len(closes) < need:
            return TrendGateResult(
                False,
                f"大方向要用 EMA{self.slow}，{tag}K线还不够（大约要 {need} 根，现在 {len(closes)} 根）",
            )
        fast_ma = ema_series(closes, self.fast)
        slow_ma = ema_series(closes, self.slow)
        e_fast = float(fast_ma.iloc[-1])
        e_slow = float(slow_ma.iloc[-1])
        if e_fast > e_slow + 1e-12:
            allow = "long"
            align = f"EMA{self.fast} 在 EMA{self.slow} 上方，只做多"
        elif e_fast < e_slow - 1e-12:
            allow = "short"
            align = f"EMA{self.fast} 在 EMA{self.slow} 下方，只做空"
        else:
            return TrendGateResult(
                False,
                f"EMA{self.fast} 和 EMA{self.slow} 贴在一起，大方向还没排好，先不开",
            )

        if self.min_deg <= 0:
            return TrendGateResult(True, align, allow=allow)

        highs = ohlc_series(view, "high")
        lows = ohlc_series(view, "low")
        # ATR 只当尺子：均线走了多少点，除以「根数×波动」再换成角度。斜边仍是均线。
        atr_s = average_true_range(highs, lows, closes, self.atr_period).dropna()
        atr = float(atr_s.iloc[-1]) if len(atr_s) else 0.0
        if atr <= 1e-12:
            atr = max(abs(e_fast) * 0.001, 1e-6)
        prev_fast = float(fast_ma.iloc[-1 - self.lookback])
        prev_slow = float(slow_ma.iloc[-1 - self.lookback])
        ang_fast = ema_angle_deg(e_fast, prev_fast, self.lookback, atr)
        ang_slow = ema_angle_deg(e_slow, prev_slow, self.lookback, atr)
        slope_text = (
            f"EMA{self.fast} 近 {self.lookback} 根倾角 {ang_fast:.1f}°、"
            f"EMA{self.slow} 近 {self.lookback} 根倾角 {ang_slow:.1f}°（门槛 {self.min_deg:g}°）"
        )
        if allow == "long":
            if ang_fast < self.min_deg or ang_slow < self.min_deg:
                return TrendGateResult(
                    False,
                    f"{align}，但{slope_text}，走得不够陡，当震荡先不开",
                    allow=allow,
                )
        else:
            if ang_fast > -self.min_deg or ang_slow > -self.min_deg:
                return TrendGateResult(
                    False,
                    f"{align}，但{slope_text}，走得不够陡，当震荡先不开",
                    allow=allow,
                )
        return TrendGateResult(True, f"{align}。{slope_text}", allow=allow)

    def check(
        self,
        *,
        side: str,
        bars: pd.DataFrame,
        now: datetime,
        timeframe: str,
        macd_minutes: int,
    ) -> TrendGateResult:
        """side 是方向选择器刚给出的多/空。过滤关掉时直接放行。"""
        if not self.enabled:
            return TrendGateResult(True, "")
        if side not in {"long", "short"}:
            return TrendGateResult(True, "")
        snap = self.snapshot(
            bars=bars,
            now=now,
            timeframe=timeframe,
            macd_minutes=macd_minutes,
        )
        if snap.allow and side != snap.allow:
            got = "多" if side == "long" else "空"
            return TrendGateResult(
                False,
                f"{snap.reason}，这次想做{got}，和大方向不一致，先不开",
                allow=snap.allow,
            )
        return snap


def gate_minutes_from_host(host: Any) -> int:
    """过滤和 MACD 用同一档分钟，避免两条均线看的图和柱子不是一张。"""
    minutes = int(getattr(getattr(host, "direction", None), "bar_minutes", 0) or 0)
    if minutes in (1, 5):
        return minutes
    return 5
