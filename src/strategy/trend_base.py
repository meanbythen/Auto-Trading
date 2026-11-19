"""
趋势类策略公共骨架

功能：把「会话、收盘清仓、持仓记账、开平单、止损触发」这些所有趋势策略都要写的部分收在一处。
原理：布林突破 / SAR / 多因子三套都是从 vnpy CtaStrategy 模板翻译过来的，
    差别只在「什么时候开、什么时候走」，其余骨架完全一样。
    子类只实现 prepare / entry_signal / exit_reason / trail_stop 几个钩子。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any

import pandas as pd
from loguru import logger

from .base import StrategyBase
from .indicators import average_true_range
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


class TrendStrategyBase(StrategyBase):
    """趋势类策略公共骨架。子类继承并实现四个钩子。"""

    #: 配置里策略自己那一节的键名，也用来写日志
    section = "trend"
    label = "趋势策略"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        st = cfg.get("strategy", {}) or {}
        self.cfg = cfg
        self.box: dict[str, Any] = st.get(self.section) or {}
        trade = cfg.get("trade", {}) or {}

        self.timeframe = str(st.get("timeframe", "5"))
        self.trail_timeframe = str(st.get("trail_timeframe", self.timeframe))
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))

        # 止损触发方式（全策略共用）：touch 影线碰到止损位就平，close 等收盘确认。
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH
        # 开仓触发方式在 runner 里生效，这里只记一份，方便日志和回测快照。
        self.entry_mode = str(st.get("entry_mode") or "close")

        # 本策略自己的趋势过滤，不看已删除的公共行情过滤开关。
        self.market_filter = MarketFilter(trend_params_for_section(cfg, self.section))

        # ATR：止损和仓位估算都用它，默认 14 根。
        self.atr_period = max(2, int(self.box.get("atr_period", 14) or 14))

        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        # 布林 / SAR / 多因子 / 凯利 / 以损定开：收盘清仓听交易配置总开关。
        # 总开关写过就用总开关；没写过才看自己那一节（旧 yaml）。
        if "flatten_at_close" in st:
            self.flatten_at_close = config_flag(st.get("flatten_at_close"), default=True)
        else:
            self.flatten_at_close = config_flag(
                self.box.get("flatten_at_close", False), default=False
            )
        self.entry_sessions = session_tuples(cfg, live)
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {
            s: PositionState(symbol=s) for s in self.symbols
        }
        self._pending: dict[str, dict[str, Any]] = {}
        self._close_side: dict[str, str] = {}
        self._close_entry: dict[str, float] = {}

    # ---------- 会话 ----------
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

    # ---------- 记账 ----------
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

    # ---------- 子类钩子 ----------
    def need_bars(self) -> int:
        """至少多少根 K 线才能算指标。"""
        return self.atr_period + 5

    @property
    def required_bars(self) -> int:
        """实际要等多少根：指标窗口和行情过滤回看窗口取大的那个。"""
        need = self.need_bars()
        if self.market_filter.enabled:
            need = max(need, self.market_filter.params.need_bars)
        return need

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        """算指标。返回 None 表示还没算稳，这一轮不动。"""
        raise NotImplementedError

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        """空仓时判断要不要开。返回 (方向, 止损价, 说明, meta) 或 None。"""
        raise NotImplementedError

    def exit_reason(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> str | None:
        """有仓时判断要不要主动平（不算止损）。返回原因或 None。"""
        return None

    def trail_stop(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> float:
        """更新移动止损位。只会往有利方向收。"""
        return float(pos.trail_stop or pos.initial_stop)

    # ---------- 主流程 ----------
    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        _ = trail_bars
        # 多因子等策略会用另一套更大周期（通常是日线）做方向过滤。
        self._trend_bars = trend_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        if bars is None or len(bars) < self.required_bars:
            return StrategyDecision(
                StrategyAction.HOLD, symbol, f"K线不足，{self.label}指标还算不出来"
            )
        ind = self.prepare(bars)
        self._last_prepare = ind
        if ind is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, f"{self.label}指标还没算稳")

        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        close = float(bar["close"])

        if pos.side != "flat" and self.is_flatten_time(now):
            return self._close_decision(
                symbol, pos, now, bar_id, f"收盘清仓时间到，{self.label}强制平仓"
            )
        if pos.side != "flat":
            return self._manage(symbol, bars, bar, bar_id, pos, ind, now)

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, f"这一根已经处理过{self.label}")

        state = self.market_filter.check(bars)
        if not state.ok:
            return StrategyDecision(
                StrategyAction.HOLD, symbol, f"行情不匹配：{state.reason}", stop_price=close
            )

        signal = self.entry_signal(bars, bar, ind)
        if signal is None:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{self.label}空仓等待  {state.describe()}",
                stop_price=close,
            )
        side, stop, reason, meta = signal
        risk = close - stop if side == "long" else stop - close
        if not (risk > 0):
            pad = max(abs(close) * 0.001, 1.0)
            stop = close - pad if side == "long" else close + pad
        self._pending[symbol] = {"time": now, "stop": stop, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        logger.debug("{} {} {}", symbol, self.label, reason)
        return StrategyDecision(
            action,
            symbol,
            reason,
            stop_price=stop,
            entry_price=close,
            meta={"signal": self.section, "bar_id": bar_id, **meta},
        )

    def _manage(
        self,
        symbol: str,
        bars: pd.DataFrame,
        bar: pd.Series,
        bar_id: str,
        pos: PositionState,
        ind: dict[str, Any],
        now: datetime,
    ) -> StrategyDecision:
        close = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])

        reason = self.exit_reason(bars, bar, pos, ind)
        if reason:
            return self._close_decision(symbol, pos, now, bar_id, reason)

        prev_stop = float(pos.trail_stop or pos.initial_stop or 0.0)
        stop = self.trail_stop(bars, bar, pos, ind)
        # 本根K线自己算出的移动止损只从下一根生效；判定和成交都用上一根已知的止损，
        # 否则大阳线把止损推上去的同时，会把这根自己的影线判成已止损。
        judge_stop = prev_stop or stop
        # 开仓那根自己的高低点是进场前就印出来的，不能拿来判已经被止损打掉。
        if not self._is_entry_bar(symbol, bar_id) and stop_triggered(
            side=pos.side, stop=judge_stop, close=close, high=high, low=low, mode=self.stop_mode
        ):
            return self._close_decision(
                symbol, pos, now, bar_id, f"{self.label}止损 {judge_stop:.2f} 离场", stop_price=judge_stop
            )

        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"{self.label}持有 side={pos.side} 止损={stop:.2f} 现价≈{close:.0f}",
            stop_price=stop,
            entry_price=pos.entry_price,
        )

    def _is_entry_bar(self, symbol: str, bar_id: str) -> bool:
        entry_bar = self._last_entry_bar.get(symbol)
        if not entry_bar or not bar_id:
            return False
        try:
            return pd.Timestamp(bar_id) == pd.Timestamp(entry_bar)
        except (TypeError, ValueError):
            return str(bar_id) == str(entry_bar)

    def _close_decision(
        self,
        symbol: str,
        pos: PositionState,
        now: datetime,
        bar_id: str,
        reason: str,
        *,
        stop_price: float | None = None,
    ) -> StrategyDecision:
        self._pending[symbol] = {"time": now, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        self._close_side[symbol] = pos.side
        self._close_entry[symbol] = float(pos.entry_price or 0.0)
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            reason,
            stop_price=stop_price if stop_price is not None else (pos.trail_stop or pos.initial_stop),
            entry_price=pos.entry_price,
        )

    def _atr(self, bars: pd.DataFrame) -> float | None:
        """最近一根的 ATR；算不出来返回 None。"""
        if len(bars) < self.atr_period + 1:
            return None
        value = float(
            average_true_range(
                bars["high"], bars["low"], bars["close"], period=self.atr_period
            ).iloc[-1]
        )
        if pd.isna(value) or value <= 0:
            return None
        return value
