"""
双周期回调分型。

功能：
  大周期用指数均线或通道+ADX 定方向；小周期等回调里的强分型，并要 MACD 的 DIF 不创新低/新高。
  周期可改。资料里的简单平均改成指数平均。不做缠论中枢递归。
原理：
  顺大逆小：大趋势向上时，只在小周期下跌回调结束时做多。
  强底分型=中间一根最低、第三根最高价升破第一根最高价。
  DIF 背离是工程简化：价格创新低而 DIF 没有创新低，用来代替人工看的动能衰竭。
  入场比通道突破更早，假信号也会更多。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import period_label

from .dual_cycle import (
    DualCycleMixin,
    htf_bias,
    last_atr,
    last_macd_dif,
    parse_fractal_mode,
    strong_bottom_fractal,
    strong_top_fractal,
)
from .types import PositionState, StrategyAction, StrategyDecision, stop_triggered


class DualFractalStrategy(DualCycleMixin):
    """大周期定方向，小周期强分型 + DIF 简化背离入场。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        box = self._bind_dual_cycle(cfg, "dual_fractal", "D")
        self.trend_mode = parse_fractal_mode(str(box.get("trend_mode") or "ema"))
        self.trend_ema = max(2, int(box.get("trend_ema", 20) or 20))
        self.htf_donchian = max(5, int(box.get("htf_donchian", 55) or 55))
        self.adx_period = max(2, int(box.get("adx_period", 14) or 14))
        self.adx_min = max(0.0, float(box.get("adx_min", 22) or 22))
        self.macd_fast = max(2, int(box.get("macd_fast", 12) or 12))
        self.macd_slow = max(self.macd_fast + 1, int(box.get("macd_slow", 26) or 26))
        self.macd_signal = max(2, int(box.get("macd_signal", 9) or 9))
        self.atr_period = max(2, int(box.get("atr_period", 14) or 14))
        self.atr_mult = float(box.get("atr_mult", 2.8) or 2.8)
        if self.atr_mult <= 0:
            self.atr_mult = 2.8
        # 每个品种记住上一笔价格极值对应的 DIF，用来判断有没有背离。
        self._swing: dict[str, dict[str, float]] = {}

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        del trail_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = max(self.macd_slow + self.macd_signal, self.atr_period, 8)
        if bars is None or len(bars) < need:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{period_label(self.timeframe)}K线不足，分型还认不出来",
            )

        bias, bias_why = htf_bias(
            trend_bars,
            mode=self.trend_mode,
            donchian=self.htf_donchian,
            ema_len=self.trend_ema,
            adx_period=self.adx_period,
            adx_min=self.adx_min,
        )
        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        if pos.side != "flat":
            return self._manage(symbol, bars, pos, bias, now, bar_id)

        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经看过分型")
        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if bias == "none":
            return StrategyDecision(StrategyAction.HOLD, symbol, bias_why)

        atr = last_atr(bars, self.atr_period)
        dif = last_macd_dif(
            bars, fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal
        )
        if atr is None or dif is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "波动或 DIF 还没算稳")

        close = float(bar["close"])
        if bias == "long":
            div = self._update_div(symbol, float(bar["low"]), dif, side="low")
            if div and strong_bottom_fractal(bars):
                stop = close - self.atr_mult * atr
                return self._open(symbol, now, bar_id, close, "long", stop)
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{self.trend_name}偏多，等待小周期底背离+强底分型",
            )
        div = self._update_div(symbol, float(bar["high"]), dif, side="high")
        if div and strong_top_fractal(bars):
            stop = close + self.atr_mult * atr
            return self._open(symbol, now, bar_id, close, "short", stop)
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"{self.trend_name}偏空，等待小周期顶背离+强顶分型",
        )

    def _update_div(self, symbol: str, price: float, dif: float, *, side: str) -> bool:
        """
        简化背离：价格创新低而 DIF 没有创新低（底），或价格创新高而 DIF 没有创新高（顶）。

        不是缠论里比两段力度的标准背驰，只用来过滤没有动能衰竭的分型。
        """
        mem = self._swing.setdefault(symbol, {})
        key_p = "last_low_price" if side == "low" else "last_high_price"
        key_d = "last_low_dif" if side == "low" else "last_high_dif"
        last_p = float(mem.get(key_p, 0.0) or 0.0)
        last_d = float(mem.get(key_d, 0.0) or 0.0)
        hit = False
        if last_p > 0:
            if side == "low" and price < last_p and dif > last_d:
                hit = True
            if side == "high" and price > last_p and dif < last_d:
                hit = True
        if last_p <= 0:
            mem[key_p] = price
            mem[key_d] = dif
        elif side == "low" and price < last_p:
            mem[key_p] = price
            mem[key_d] = dif
        elif side == "high" and price > last_p:
            mem[key_p] = price
            mem[key_d] = dif
        return hit

    def _open(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        entry: float,
        side: str,
        stop: float,
    ) -> StrategyDecision:
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "分型止损距离异常")
        if side == "long":
            action = StrategyAction.BUY_OPEN
            text = (
                f"双周期分型做多 {self.trend_name}偏多，"
                f"{period_label(self.timeframe)}底背离+强底分型，止损{stop:.2f}"
            )
            signal = "dual_fractal_long"
        else:
            action = StrategyAction.SELL_OPEN
            text = (
                f"双周期分型做空 {self.trend_name}偏空，"
                f"{period_label(self.timeframe)}顶背离+强顶分型，止损{stop:.2f}"
            )
            signal = "dual_fractal_short"
        self._pending[symbol] = {"time": now, "stop": stop, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        logger.info("{} {}", symbol, text)
        return StrategyDecision(
            action,
            symbol,
            text,
            stop_price=stop,
            entry_price=entry,
            meta={"signal": signal, "bar_id": bar_id},
        )

    def _manage(
        self,
        symbol: str,
        bars: pd.DataFrame,
        pos: PositionState,
        bias: str,
        now: datetime,
        bar_id: str,
    ) -> StrategyDecision:
        bar = bars.iloc[-1]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        if self.is_flatten_time(now):
            return self._close_now(symbol, now, bar_id, pos, "收盘清仓时间到，双周期分型强制平仓")
        if bias != "none" and (
            (pos.side == "long" and bias == "short") or (pos.side == "short" and bias == "long")
        ):
            return self._close_now(
                symbol, now, bar_id, pos, f"{self.trend_name}已经翻向，双周期分型平仓"
            )
        if self._is_entry_bar(symbol, bar_id):
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"双周期分型持有 side={pos.side} 止损={pos.initial_stop:.2f}",
                stop_price=pos.initial_stop,
                entry_price=pos.entry_price,
            )

        if pos.side == "long" and strong_top_fractal(bars):
            return self._close_now(
                symbol, now, bar_id, pos, f"{period_label(self.timeframe)}出现强顶分型，多单离场"
            )
        if pos.side == "short" and strong_bottom_fractal(bars):
            return self._close_now(
                symbol, now, bar_id, pos, f"{period_label(self.timeframe)}出现强底分型，空单离场"
            )

        atr = last_atr(bars, self.atr_period)
        if atr is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "波动暂时算不出来，先持有")
        stop = self._ratchet_stop(symbol, pos, bar, atr, self.atr_mult)
        if stop_triggered(pos.side, high=high, low=low, close=close, stop=stop, mode=self.stop_mode):
            return self._close_now(
                symbol, now, bar_id, pos, f"双周期分型止损 {stop:.2f}", stop_price=stop
            )
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"双周期分型持有 side={pos.side} 止损={stop:.2f}",
            stop_price=stop,
            entry_price=pos.entry_price,
        )
