"""
双周期通道突破。

功能：
  大周期用通道+ADX 定方向；小周期用唐奇安通道突破开仓，短通道和波动止损离场。
  大周期、小周期都可以在配置里改，不写死周线配日线。
原理：
  大周期收盘冲出前 N 根高点，只允许小周期做多；冲出低点只允许做空。
  夹在通道里或 ADX 太低，当作没有大趋势，空仓不开。
  小周期突破是「趋势已经走出来再上车」，信号少、回测稳，入场会偏晚。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import period_label

from .dual_cycle import DualCycleMixin, donchian_prior, htf_bias, last_atr
from .types import (
    PositionState,
    StrategyAction,
    StrategyDecision,
    forming_price_range,
    stop_triggered,
)


class DualDonchianStrategy(DualCycleMixin):
    """大周期通道定方向，小周期唐奇安突破跟随。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        box = self._bind_dual_cycle(cfg, "dual_donchian", "D")
        # 进场通道要长，才是一段行情而不是晃一下。
        self.entry_bars = max(5, int(box.get("entry_bars", 55) or 55))
        self.exit_bars = max(2, int(box.get("exit_bars", 20) or 20))
        if self.exit_bars >= self.entry_bars:
            self.exit_bars = max(2, self.entry_bars // 2)
        self.htf_donchian = max(5, int(box.get("htf_donchian", self.entry_bars) or self.entry_bars))
        self.adx_period = max(2, int(box.get("adx_period", 14) or 14))
        self.adx_min = max(0.0, float(box.get("adx_min", 22) or 22))
        self.atr_period = max(2, int(box.get("atr_period", 14) or 14))
        self.atr_mult = float(box.get("atr_mult", 2.5) or 2.5)
        if self.atr_mult <= 0:
            self.atr_mult = 2.5

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
        forming: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        del trail_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = max(self.entry_bars, self.exit_bars, self.atr_period) + 3
        if bars is None or len(bars) < need:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{period_label(self.timeframe)}K线不足，双周期通道还算不出来",
            )

        bias, bias_why = htf_bias(
            trend_bars,
            mode="donchian",
            donchian=self.htf_donchian,
            ema_len=20,
            adx_period=self.adx_period,
            adx_min=self.adx_min,
        )
        bar = bars.iloc[-1]
        trigger_mode = forming is not None and len(forming) > 0
        bar_id = str(forming.index[-1]) if trigger_mode else str(bar.name)
        if pos.side != "flat":
            return self._manage(symbol, bars, pos, bias, now, bar_id)

        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经看过通道突破")
        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if bias == "none":
            return StrategyDecision(StrategyAction.HOLD, symbol, bias_why)

        band = donchian_prior(bars, self.entry_bars)
        atr = last_atr(bars, self.atr_period)
        if band is None or atr is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "小周期通道或波动还没算稳")
        up, down = band
        close = float(bar["close"])
        if trigger_mode:
            price_range = forming_price_range(forming)
            if price_range is None:
                return StrategyDecision(
                    StrategyAction.HOLD, symbol, "未收线K线读不到最高最低，先等"
                )
            fh, fl = price_range
            if bias == "long" and fh >= up:
                stop = up - self.atr_mult * atr
                return self._open(
                    symbol, now, bar_id, up, "long", stop, up, trigger_price=up
                )
            if bias == "short" and fl <= down:
                stop = down + self.atr_mult * atr
                return self._open(
                    symbol, now, bar_id, down, "short", stop, down, trigger_price=down
                )
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"盘中还在通道里（{down:.2f}~{up:.2f}），等真正走出去",
            )
        if bias == "long" and close > up:
            stop = close - self.atr_mult * atr
            return self._open(symbol, now, bar_id, close, "long", stop, up)
        if bias == "short" and close < down:
            stop = close + self.atr_mult * atr
            return self._open(symbol, now, bar_id, close, "short", stop, down)
        side_txt = "多" if bias == "long" else "空"
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"{self.trend_name}偏{side_txt}，{period_label(self.timeframe)}收盘还在通道里",
        )

    def _open(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        entry: float,
        side: str,
        stop: float,
        broken: float,
        *,
        trigger_price: float | None = None,
    ) -> StrategyDecision:
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "通道止损距离异常")
        if side == "long":
            action = StrategyAction.BUY_OPEN
            text = (
                f"双周期通道做多 {self.trend_name}已向上，"
                f"{period_label(self.timeframe)}收盘升破前{self.entry_bars}根高点{broken:.2f}，止损{stop:.2f}"
            )
            signal = "dual_donchian_long"
        else:
            action = StrategyAction.SELL_OPEN
            text = (
                f"双周期通道做空 {self.trend_name}已向下，"
                f"{period_label(self.timeframe)}收盘跌破前{self.entry_bars}根低点{broken:.2f}，止损{stop:.2f}"
            )
            signal = "dual_donchian_short"
        self._pending[symbol] = {"time": now, "stop": stop, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        logger.info("{} {}", symbol, text)
        meta = {"signal": signal, "bar_id": bar_id}
        if trigger_price is not None:
            meta["intraday_entry"] = True
        return StrategyDecision(
            action,
            symbol,
            text,
            stop_price=stop,
            entry_price=entry,
            trigger_price=trigger_price,
            meta=meta,
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
            return self._close_now(symbol, now, bar_id, pos, "收盘清仓时间到，双周期通道强制平仓")
        if bias != "none" and (
            (pos.side == "long" and bias == "short") or (pos.side == "short" and bias == "long")
        ):
            return self._close_now(
                symbol, now, bar_id, pos, f"{self.trend_name}已经翻向，双周期通道平仓"
            )
        if self._is_entry_bar(symbol, bar_id):
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"双周期通道持有 side={pos.side} 止损={pos.initial_stop:.2f}",
                stop_price=pos.initial_stop,
                entry_price=pos.entry_price,
            )

        atr = last_atr(bars, self.atr_period)
        if atr is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "波动暂时算不出来，先持有")

        exit_band = donchian_prior(bars, self.exit_bars)
        if exit_band is not None:
            exit_up, exit_down = exit_band
            if pos.side == "long" and close < exit_down:
                return self._close_now(
                    symbol,
                    now,
                    bar_id,
                    pos,
                    f"{period_label(self.timeframe)}收盘跌破短通道{exit_down:.2f}，这段走完了",
                )
            if pos.side == "short" and close > exit_up:
                return self._close_now(
                    symbol,
                    now,
                    bar_id,
                    pos,
                    f"{period_label(self.timeframe)}收盘升破短通道{exit_up:.2f}，这段走完了",
                )

        stop = self._ratchet_stop(symbol, pos, bar, atr, self.atr_mult)
        if stop_triggered(pos.side, high=high, low=low, close=close, stop=stop, mode=self.stop_mode):
            return self._close_now(
                symbol, now, bar_id, pos, f"双周期通道止损 {stop:.2f}", stop_price=stop
            )
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"双周期通道持有 side={pos.side} 止损={stop:.2f}",
            stop_price=stop,
            entry_price=pos.entry_price,
        )
