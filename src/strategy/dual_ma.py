"""
双均线趋势（经典金叉/死叉）。

功能：短均线上穿长均线做多，下穿做空；止损跟着长均线走；默认可反手。
     是否 14:55/22:55 清仓，听配置页「收盘清仓」总开关。
原理：教科书级双均线交叉，不加成交量、影线、ATR 倍数等拟合门槛。
     默认短 20、长 60（常见搭配）；配置页可改。和「价格穿单均线」不是同一套。
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Any

import pandas as pd
from loguru import logger

from .session_scope import scope_allows, scope_from_cfg
from .types import PositionState, StrategyAction, StrategyDecision, config_flag


def _parse_hhmm(value: str) -> dtime:
    hour, minute = str(value).split(":")
    return dtime(int(hour), int(minute))


class DualMaStrategy:
    """短均线与长均线交叉开仓；长均线当移动止损。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        st = cfg.get("strategy", {}) or {}
        box = st.get("dual_ma") or {}
        trade = cfg.get("trade", {}) or {}

        self.timeframe = str(st.get("timeframe", "15"))
        self.trail_timeframe = self.timeframe
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))

        try:
            short = max(2, int(box.get("short_period", 20) or 20))
        except (TypeError, ValueError):
            short = 20
        try:
            long = max(3, int(box.get("long_period", 60) or 60))
        except (TypeError, ValueError):
            long = 60
        # 写反了就自动对调，避免短线比长线还慢
        if short >= long:
            short, long = min(short, long - 1), max(short + 1, long)
            if short < 2:
                short = 2
            if long <= short:
                long = short + 1
        self.short_period = short
        self.long_period = long
        self.reverse_on_cross = config_flag(box.get("reverse_on_cross", True), default=True)

        self.flatten_times = [
            _parse_hhmm(x) for x in st.get("flatten_times", ["14:55", "22:55"])
        ]
        self.flatten_at_close = config_flag(st.get("flatten_at_close", True), default=True)
        sessions = cfg.get("schedule", {}).get("trading_sessions") or []
        self.entry_sessions = [(_parse_hhmm(a), _parse_hhmm(b)) for a, b in sessions]
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {
            s: PositionState(symbol=s) for s in self.symbols
        }
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_entry_bar: dict[str, str] = {}
        self._signaled_bar: dict[str, str] = {}
        self._close_side: dict[str, str] = {}
        self._close_entry: dict[str, float] = {}

    def is_flatten_time(self, now: datetime | None = None) -> bool:
        if not self.flatten_at_close:
            return False
        now = now or datetime.now()
        t = now.time().replace(second=0, microsecond=0)
        cur = datetime.combine(now.date(), t)
        for ft in self.flatten_times:
            start = datetime.combine(now.date(), ft)
            end = start + timedelta(minutes=10)
            if start <= cur <= end:
                return True
        return False

    def in_entry_session(self, now: datetime) -> bool:
        if str(self.timeframe).upper() in {"D", "W"}:
            return True
        if not scope_allows(self.session_scope, now):
            return False
        if not self.entry_sessions:
            return True
        t = now.time()
        return any(a <= t <= b for a, b in self.entry_sessions)

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
        """空仓等金叉/死叉；有仓时止损跟长均线，交叉反向则平或反手。"""
        _ = trail_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = self.long_period + 2
        if bars is None or len(bars) < need:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"K线不足，算不出 {self.short_period}/{self.long_period} 双均线",
            )
        if "close" not in bars.columns:
            return StrategyDecision(StrategyAction.HOLD, symbol, "K线没有收盘价")

        close_s = pd.to_numeric(bars["close"], errors="coerce")
        short = close_s.rolling(self.short_period).mean()
        long = close_s.rolling(self.long_period).mean()
        if (
            pd.isna(short.iloc[-1])
            or pd.isna(short.iloc[-2])
            or pd.isna(long.iloc[-1])
            or pd.isna(long.iloc[-2])
        ):
            return StrategyDecision(StrategyAction.HOLD, symbol, "双均线还没算稳")

        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        close = float(close_s.iloc[-1])
        s_now, s_prev = float(short.iloc[-1]), float(short.iloc[-2])
        l_now, l_prev = float(long.iloc[-1]), float(long.iloc[-2])

        # 金叉 / 死叉：短线相对长线刚换边
        cross_up = s_prev <= l_prev and s_now > l_now
        cross_down = s_prev >= l_prev and s_now < l_now

        if pos.side != "flat" and self.is_flatten_time(now):
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"收盘清仓时间到，双均线{self.short_period}/{self.long_period}强制平仓",
            )

        if pos.side != "flat":
            return self._manage(
                symbol,
                pos,
                now,
                bar_id,
                close,
                l_now,
                s_now,
                cross_up=cross_up,
                cross_down=cross_down,
            )

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经处理过双均线交叉")

        if cross_up:
            stop = float(pd.to_numeric(bar["low"], errors="coerce"))
            return self._open(symbol, now, bar_id, close, "long", stop, s_now, l_now)
        if cross_down:
            stop = float(pd.to_numeric(bar["high"], errors="coerce"))
            return self._open(symbol, now, bar_id, close, "short", stop, s_now, l_now)

        side_txt = "多" if s_now > l_now else ("空" if s_now < l_now else "纠缠")
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            (
                f"双均线偏{side_txt} "
                f"短{self.short_period}={s_now:.2f} 长{self.long_period}={l_now:.2f}，等金叉/死叉"
            ),
            stop_price=l_now,
        )

    def _manage(
        self,
        symbol: str,
        pos: PositionState,
        now: datetime,
        bar_id: str,
        close: float,
        long_now: float,
        short_now: float,
        *,
        cross_up: bool,
        cross_down: bool,
    ) -> StrategyDecision:
        # 止损跟长均线，只往有利方向收
        if pos.side == "long":
            trail = long_now
            if pos.trail_stop and pos.trail_stop > 0:
                trail = max(float(pos.trail_stop), long_now)
            pos.trail_stop = trail
        else:
            trail = long_now
            if pos.trail_stop and pos.trail_stop > 0:
                trail = min(float(pos.trail_stop), long_now)
            pos.trail_stop = trail
        pos.trail_armed = True

        opposite = (pos.side == "long" and cross_down) or (pos.side == "short" and cross_up)
        if opposite:
            if (
                self.reverse_on_cross
                and self.in_entry_session(now)
                and not self.is_flatten_time(now)
            ):
                new_side = "long" if cross_up else "short"
                pad = max(abs(close) * 0.001, 1.0)
                new_stop = close - pad if new_side == "long" else close + pad
                return self._open(
                    symbol,
                    now,
                    bar_id,
                    close,
                    new_side,
                    new_stop,
                    short_now,
                    long_now,
                    reverse_from=pos.side,
                )
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                (
                    f"双均线{'死叉' if cross_down else '金叉'}，"
                    f"平{'多' if pos.side == 'long' else '空'} "
                    f"长均={long_now:.2f}"
                ),
            )

        # 短线已回到长线错误一侧（未等到完整交叉判定时的保险）
        if pos.side == "long" and short_now < long_now:
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"多单短均已落到长均下方 {short_now:.2f}<{long_now:.2f}",
            )
        if pos.side == "short" and short_now > long_now:
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"空单短均已升到长均上方 {short_now:.2f}>{long_now:.2f}",
            )

        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            (
                f"双均线持有 side={pos.side} "
                f"短{self.short_period}={short_now:.2f} 长{self.long_period}={long_now:.2f}"
            ),
            stop_price=pos.trail_stop or long_now,
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
        short_now: float,
        long_now: float,
        *,
        reverse_from: str | None = None,
    ) -> StrategyDecision:
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0 or pd.isna(stop):
            pad = max(abs(entry) * 0.001, 1.0)
            stop = entry - pad if side == "long" else entry + pad
        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        tag = f"{self.short_period}/{self.long_period}"
        if reverse_from:
            text = (
                f"双均线{tag}反手{'多' if side == 'long' else '空'} "
                f"原{reverse_from}→{side} 短={short_now:.2f} 长={long_now:.2f} 止损{stop:.2f}"
            )
        else:
            kind = "金叉" if side == "long" else "死叉"
            text = (
                f"双均线{tag}{kind}做{'多' if side == 'long' else '空'} "
                f"短={short_now:.2f} 长={long_now:.2f} entry={entry:.2f} 止损{stop:.2f}"
            )
        self._pending[symbol] = {"time": now, "stop": stop, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
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
                "signal": "dual_ma",
                "bar_id": bar_id,
                "reverse": bool(reverse_from),
                "short_ma": short_now,
                "long_ma": long_now,
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
