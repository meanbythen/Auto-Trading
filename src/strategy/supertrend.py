"""
SuperTrend 趋势跟随。

功能：收盘相对 ATR 通道线翻转时开仓/反手；止损就是当前 SuperTrend 线；
     默认不按 14:55/22:55 清仓，适合拿一段趋势（可隔夜）。
原理：通道半宽 = multiplier × ATR。比「双均线交叉」少在窄幅里来回抽打，
     和海龟通道突破（trend_follow）不是同一套：一个看波动带翻转，一个看前 N 根高低点。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any

import pandas as pd
from loguru import logger

from .base import StrategyBase
from .indicators import supertrend as calc_supertrend
from .market_state import MarketFilter, trend_params_for_section
from .session_scope import scope_allows, scope_from_cfg
from .types import (
    STOP_MODE_TOUCH,
    PositionState,
    StrategyAction,
    StrategyDecision,
    config_flag,
    stop_mode_from_cfg,
    stop_triggered,
)


def _parse_hhmm(value: str) -> dtime:
    hour, minute = str(value).split(":")
    return dtime(int(hour), int(minute))


class SuperTrendStrategy(StrategyBase):
    """SuperTrend 翻转开仓；线本身当移动止损；默认可隔夜。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        self.cfg = cfg
        st = cfg.get("strategy", {}) or {}
        box = st.get("supertrend") or {}
        trade = cfg.get("trade", {}) or {}

        self.timeframe = str(st.get("timeframe", "5"))
        self.trail_timeframe = self.timeframe
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        # 止损触发方式（全策略共用）：touch 影线碰到止损线就平，close 等收盘确认。
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH

        # 常用默认：ATR 10 根、倍数 3（TradingView / 通达信常见）
        self.atr_period = max(2, int(box.get("atr_period", 10)))
        self.multiplier = float(box.get("multiplier", 3.0))
        if self.multiplier <= 0:
            self.multiplier = 3.0
        # 翻向时是否反手（平旧仓并开新方向）。关掉则只平仓、等下一根再开。
        self.reverse_on_flip = config_flag(box.get("reverse_on_flip", True), default=True)
        # SuperTrend 自己的趋势过滤：震荡里通道会来回翻。
        self.market_filter = MarketFilter(trend_params_for_section(cfg, "supertrend"))

        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        # 与 trend_follow 一样：默认不听总开关，到点不平，才能拿住趋势
        self.flatten_at_close = config_flag(box.get("flatten_at_close", False), default=False)
        self.entry_sessions = session_tuples(cfg, live)
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {
            s: PositionState(symbol=s) for s in self.symbols
        }
        self._pending: dict[str, dict[str, Any]] = {}
        self._close_side: dict[str, str] = {}
        self._close_entry: dict[str, float] = {}

    def is_flatten_time(self, now: datetime | None = None) -> bool:
        if not self.flatten_at_close:
            return False
        from src.utils.session_clock import in_flatten_window

        return in_flatten_window(self.flatten_times, now or datetime.now())

    def in_entry_session(self, now: datetime, symbol: str | None = None) -> bool:
        if str(self.timeframe).upper() in {"D", "W"}:
            return True
        if not scope_allows(self.session_scope, now):
            return False
        from src.utils.session_clock import in_session_windows, parse_hhmm, sessions_for_symbol

        windows = self.entry_sessions
        if symbol:
            rows = sessions_for_symbol(self.cfg, symbol)
            if rows:
                windows = [(parse_hhmm(a), parse_hhmm(b)) for a, b in rows]
        if not windows:
            return True
        return in_session_windows(windows, now)

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        pending = self._pending.pop(symbol, None) or {}
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            pos.side = "long" if action == StrategyAction.BUY_OPEN else "short"
            pos.volume = volume
            pos.entry_price = price
            stop = float(initial_stop if initial_stop is not None else pending.get("stop") or price)
            pos.initial_stop = stop
            pos.trail_stop = stop
            pos.trail_armed = True
            when = pending.get("time") or datetime.now()
            pos.opened_at = when.isoformat(timespec="seconds")
            opened_bar = str(pending.get("bar_id") or "")
            if opened_bar:
                self._last_entry_bar[symbol] = opened_bar
        elif action == StrategyAction.CLOSE:
            if not pos.reduce_close(volume):
                if pending:
                    self._pending[symbol] = pending
                return
            self.positions[symbol] = PositionState(symbol=symbol)
            self._close_side.pop(symbol, None)
            self._close_entry.pop(symbol, None)

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        _ = trail_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = self.atr_period + 5
        if self.market_filter.enabled:
            need = max(need, self.market_filter.params.need_bars)
        if bars is None or len(bars) < need:
            return StrategyDecision(StrategyAction.HOLD, symbol, "K线不足，SuperTrend 还算不出来")

        st_line, direction = calc_supertrend(
            bars["high"],
            bars["low"],
            bars["close"],
            period=self.atr_period,
            multiplier=self.multiplier,
        )
        if len(st_line) < 2 or pd.isna(st_line.iloc[-1]) or pd.isna(direction.iloc[-1]):
            return StrategyDecision(StrategyAction.HOLD, symbol, "SuperTrend 尚未稳定")

        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        close = float(bar["close"])
        st_now = float(st_line.iloc[-1])
        dir_now = int(direction.iloc[-1])
        dir_prev = int(direction.iloc[-2])

        if pos.side != "flat" and self.is_flatten_time(now):
            return self._close_decision(symbol, pos, now, bar_id, "收盘清仓时间到，SuperTrend 强制平仓")

        # 持仓：方向反了 → 平或反手；否则把止损收到当前线上
        if pos.side != "flat":
            return self._manage(
                symbol,
                pos,
                now,
                bar_id,
                close,
                st_now,
                dir_now,
                dir_prev,
                high=float(bar["high"]),
                low=float(bar["low"]),
            )

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经处理过 SuperTrend")

        # 文档第 1 条：行情不匹配直接停手。ADX 低或 ATR 低分位时不开新仓。
        state = self.market_filter.check(bars)
        if not state.ok:
            return StrategyDecision(
                StrategyAction.HOLD, symbol, f"行情不匹配：{state.reason}", stop_price=st_now
            )

        # 空仓：只在「本根刚翻转」时开仓，避免中途切入旧趋势
        if dir_now == dir_prev:
            side_txt = "多" if dir_now > 0 else "空"
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"SuperTrend 仍偏{side_txt} ST={st_now:.2f}，等翻转再开",
                stop_price=st_now,
            )

        if dir_now > 0 and dir_prev < 0:
            return self._open(symbol, now, bar_id, close, "long", st_now)
        if dir_now < 0 and dir_prev > 0:
            return self._open(symbol, now, bar_id, close, "short", st_now)
        return StrategyDecision(StrategyAction.HOLD, symbol, "SuperTrend 方向未变")

    def _manage(
        self,
        symbol: str,
        pos: PositionState,
        now: datetime,
        bar_id: str,
        close: float,
        st_now: float,
        dir_now: int,
        dir_prev: int,
        *,
        high: float = 0.0,
        low: float = 0.0,
    ) -> StrategyDecision:
        pos.trail_stop = st_now
        pos.trail_armed = True

        flipped = dir_now != dir_prev and (
            (pos.side == "long" and dir_now < 0) or (pos.side == "short" and dir_now > 0)
        )
        if flipped:
            if self.reverse_on_flip and self.in_entry_session(now, symbol) and not self.is_flatten_time(now):
                new_side = "long" if dir_now > 0 else "short"
                return self._open(
                    symbol,
                    now,
                    bar_id,
                    close,
                    new_side,
                    st_now,
                    reverse_from=pos.side,
                )
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"SuperTrend 翻向，平{'多' if pos.side == 'long' else '空'} ST={st_now:.2f}",
            )

        # 止损触发方式（全策略共用）：touch 时影线碰到 SuperTrend 线就平；close 等收盘确认。
        # 翻向优先，避免反手被止损抢走。
        if self.stop_on_touch and stop_triggered(
            side=pos.side, stop=st_now, close=close, high=high, low=low, mode=self.stop_mode
        ):
            return self._close_decision(
                symbol, pos, now, bar_id, f"盘中触及 SuperTrend 线 {st_now:.2f}，先平"
            )

        # 未翻向：收盘打穿当前线也平（与线同步的保险）
        if pos.side == "long" and close < st_now:
            return self._close_decision(
                symbol, pos, now, bar_id, f"多单收盘跌破 SuperTrend {st_now:.2f}"
            )
        if pos.side == "short" and close > st_now:
            return self._close_decision(
                symbol, pos, now, bar_id, f"空单收盘站上 SuperTrend {st_now:.2f}"
            )

        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"SuperTrend 持有 side={pos.side} ST={st_now:.2f} 现价≈{close:.0f}",
            stop_price=st_now,
            entry_price=pos.entry_price,
        )

    def _open(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        entry: float,
        side: str,
        stop: float,
        *,
        reverse_from: str | None = None,
    ) -> StrategyDecision:
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0:
            # 翻转当根止损可能贴着价，稍退一点保证手数可算
            pad = max(abs(entry) * 0.001, 1.0)
            stop = entry - pad if side == "long" else entry + pad
            risk = pad
        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        if reverse_from:
            text = (
                f"SuperTrend 反手{'多' if side == 'long' else '空'} "
                f"原{reverse_from}→{side} entry={entry:.2f} ST止损{stop:.2f}"
            )
        else:
            text = (
                f"SuperTrend 做{'多' if side == 'long' else '空'} "
                f"通道翻转 entry={entry:.2f} ST止损{stop:.2f}"
            )
        self._pending[symbol] = {"time": now, "stop": stop, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        # 反手时引擎若先 CLOSE 再 OPEN，需带上平仓标记；多数回测把 BUY/SELL_OPEN 当反手
        if reverse_from:
            self._close_side[symbol] = reverse_from
            pos = self.positions.get(symbol)
            self._close_entry[symbol] = float(pos.entry_price or 0.0) if pos else 0.0
        logger.info("{} {}", symbol, text)
        return StrategyDecision(
            action,
            symbol,
            text,
            stop_price=stop,
            entry_price=entry,
            meta={
                "signal": "supertrend",
                "bar_id": bar_id,
                "reverse": bool(reverse_from),
            },
        )

    def _close_decision(
        self,
        symbol: str,
        pos: PositionState,
        now: datetime,
        bar_id: str,
        reason: str,
    ) -> StrategyDecision:
        self._pending[symbol] = {"time": now, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        self._close_side[symbol] = pos.side
        self._close_entry[symbol] = float(pos.entry_price or 0.0)
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            reason,
            stop_price=pos.trail_stop or pos.initial_stop,
            entry_price=pos.entry_price,
        )
