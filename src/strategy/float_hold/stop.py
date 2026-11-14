"""
日内浮盈飘单 · 止损控制。

功能：开仓时从进场价往回留若干倍开盘前 ATR；浮盈够了把止损提到开仓价保本。
原理：ATR 在开仓那一刻冻住，后面不再跟着盘中波动变宽变窄。
     方向和开仓窗口不在这里改。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import last_ohlc_price
from src.strategy.types import PositionState, STOP_MODE_CLOSE


def atr_width_stop(
    *,
    side: str,
    entry: float,
    atr: float | None,
    mult: float,
    price_tick: float = 1.0,
) -> float:
    """
    按 ATR 宽度算止损：从进场价往回留出「几倍开盘前波动」。

    拿不到 ATR 时至少留 1 跳，避免止损贴在进场价上。
    结果按最小跳动收齐，保证多单止损在进场价下面、空单在上面。
    """
    tick = max(float(price_tick), 1e-12)
    entry_px = float(entry)
    width = tick
    if atr is not None and float(atr) > 1e-12 and float(mult) > 0:
        width = max(tick, float(atr) * float(mult))
    if side == "long":
        stop = entry_px - width
        return tick * round(stop / tick)
    if side == "short":
        stop = entry_px + width
        return tick * round(stop / tick)
    return entry_px


class AtrWidthStop:
    """
    ATR 宽度止损 + 浮盈保本。

    host 上记着 stop_atr_mult / be_atr_mult，以及每笔冻住的 ATR。
    """

    key = "atr_width"
    label = "开盘前ATR宽度止损"

    def initial_stop(
        self,
        host: Any,
        *,
        side: str,
        entry: float,
        atr: float | None,
    ) -> float:
        """开仓时的结构止损。箱子只用来定方向，不决定止损远近。"""
        entry_px = float(entry or 0)
        tick = max(float(getattr(host, "price_tick", 1.0) or 1.0), 1e-12)
        if entry_px <= 0:
            return 0.0
        stop = atr_width_stop(
            side=side,
            entry=entry_px,
            atr=atr,
            mult=float(host.stop_atr_mult),
            price_tick=tick,
        )
        host._last_stop_atr = float(atr) if atr is not None and atr > 1e-12 else None
        host._last_stop_width = abs(entry_px - stop)
        return stop

    def effective_stop(self, pos: PositionState) -> float:
        """
        保本之后看跟踪止损。

        多单：跟踪止损只会往上挪，取更高的那个。
        空单：跟踪止损只会往下挪，取更低的那个。
        """
        init = float(pos.initial_stop or 0)
        trail = float(pos.trail_stop or 0)
        parts = [x for x in (init, trail) if x > 0]
        if not parts:
            return 0.0
        if pos.side == "long":
            return max(parts)
        if pos.side == "short":
            return min(parts)
        return parts[0]

    def frozen_atr(self, host: Any, pos: PositionState) -> float | None:
        """这笔单开仓时冻住的 ATR。没有就用止损宽度反推。"""
        stored = host._entry_atr.get(pos.symbol)
        if stored is not None and stored > 1e-12:
            return float(stored)
        entry = float(pos.entry_price or 0)
        init = float(pos.initial_stop or 0)
        mult = float(host.stop_atr_mult or 0)
        if entry > 0 and init > 0 and mult > 0:
            return abs(entry - init) / mult
        return None

    def maybe_breakeven(self, host: Any, pos: PositionState, bars: pd.DataFrame) -> None:
        """收盘浮盈达到 be_atr_mult 倍开仓 ATR，就把跟踪止损提到开仓价。"""
        if float(host.be_atr_mult) <= 0:
            return
        entry = float(pos.entry_price or 0)
        atr = self.frozen_atr(host, pos)
        if entry <= 0 or atr is None or atr <= 1e-12:
            return
        close = last_ohlc_price(bars, "close")
        if close is None:
            return
        target = float(host.be_atr_mult) * atr
        if pos.side == "long":
            if float(close) < entry + target:
                return
            if float(pos.trail_stop or 0) < entry:
                pos.trail_stop = entry
                pos.trail_armed = True
                logger.info(
                    "{} 浮盈够 {} 倍ATR（{:.2f} 点），止损提到开仓价 {:.2f} 保本",
                    pos.symbol,
                    host.be_atr_mult,
                    target,
                    entry,
                )
            return
        if pos.side == "short":
            if float(close) > entry - target:
                return
            trail = float(pos.trail_stop or 0)
            if trail <= 0 or trail > entry:
                pos.trail_stop = entry
                pos.trail_armed = True
                logger.info(
                    "{} 浮盈够 {} 倍ATR（{:.2f} 点），止损提到开仓价 {:.2f} 保本",
                    pos.symbol,
                    host.be_atr_mult,
                    target,
                    entry,
                )

    def hold_extra(self, pos: PositionState) -> str:
        """已经保本时，观望原因里补一句。"""
        entry = float(pos.entry_price or 0)
        trail = float(pos.trail_stop or 0)
        init = float(pos.initial_stop or 0)
        if entry <= 0 or trail <= 0:
            return ""
        if pos.side == "long" and trail >= entry - 1e-12 and trail > init:
            return "已经保本。"
        if pos.side == "short" and trail <= entry + 1e-12 and (init <= 0 or trail < init):
            return "已经保本。"
        return ""

    def stop_hit_text(self, host: Any, pos: PositionState, stop: float) -> str:
        verb = "收盘跌破" if pos.side == "long" else "收盘站上"
        if getattr(host, "stop_mode", "") == STOP_MODE_CLOSE:
            return f"{verb}ATR止损 {stop:.2f}"
        return f"触及ATR止损 {stop:.2f}"

    def open_stop_text(self, host: Any) -> str:
        atr = host._last_stop_atr
        width = host._last_stop_width
        if atr is None:
            return f"开盘前ATR暂时算不出，止损先按最小跳动留 {width:.2f} 点，开仓后冻结"
        return (
            f"止损离进场价 {width:.2f} 点（{host.stop_atr_mult:g} 倍开盘前ATR {atr:.2f}），"
            f"开仓后冻结；浮盈达到 {host.be_atr_mult:g} 倍ATR再保本，否则拿到收盘"
        )
