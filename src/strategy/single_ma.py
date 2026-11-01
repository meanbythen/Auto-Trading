"""
单均线系统（Single_ma）。

功能：用可设置根数的收盘均线当分界（默认 20，也可 60/80/120 等）；
     收盘从线下穿到线上做多，从线上穿到线下做空；止损跟着均线走；反向可反手。
     是否 14:55/22:55 清仓，听配置页「收盘清仓」总开关。
原理：不叠成交量、影线比例、ATR 倍数等拟合门槛。行情用收盘相对均线说话。
     根数写在 strategy.single_ma.ma_period，配置页也可改。
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


class SingleMaStrategy:
    """
    收盘穿越单根 SMA 开仓/离场；均线本身当移动止损。

    界面引擎名显示为 Single_ma。根数可改，默认 20。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        st = cfg.get("strategy", {}) or {}
        box = st.get("single_ma") or {}
        if not isinstance(box, dict):
            box = {}
        trade = cfg.get("trade", {}) or {}

        self.timeframe = str(st.get("timeframe", "15"))
        self.trail_timeframe = self.timeframe
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))

        # 默认 20；配置页或 yaml 可改成 60 / 80 / 120 等
        try:
            self.ma_period = max(2, int(box.get("ma_period", 20) or 20))
        except (TypeError, ValueError):
            self.ma_period = 20
        # 穿越反向时是否反手。关掉则只平仓，等下一次同向穿越再开。
        self.reverse_on_cross = config_flag(box.get("reverse_on_cross", True), default=True)

        self.flatten_times = [
            _parse_hhmm(x) for x in st.get("flatten_times", ["14:55", "22:55"])
        ]
        # 听配置页「收盘清仓」总开关
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
        """收盘清仓窗口。开着才在 14:55/22:55 附近平仓并不再开新仓。"""
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
        """分钟线才看交易时段；日线/周线时间戳常是 0 点，不拿钟点拦。"""
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
        """成交回报：记下仓位和止损；平仓时清状态。"""
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
        """
        空仓：等收盘刚穿越均线再开。
        有仓：止损收到当前均线；收盘穿回对面则平仓或反手。
        """
        _ = trail_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = self.ma_period + 2
        if bars is None or len(bars) < need:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"K线不足，算不出 {self.ma_period} 均线",
            )
        if "close" not in bars.columns:
            return StrategyDecision(StrategyAction.HOLD, symbol, "K线没有收盘价")

        close_s = pd.to_numeric(bars["close"], errors="coerce")
        ma = close_s.rolling(self.ma_period).mean()
        if pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-2]):
            return StrategyDecision(StrategyAction.HOLD, symbol, f"{self.ma_period} 均线还没算稳")

        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        close = float(close_s.iloc[-1])
        prev_close = float(close_s.iloc[-2])
        ma_now = float(ma.iloc[-1])
        ma_prev = float(ma.iloc[-2])

        # 穿越：上一根在均线一侧，本根收盘到另一侧（贴着均线不算穿）
        cross_up = prev_close <= ma_prev and close > ma_now
        cross_down = prev_close >= ma_prev and close < ma_now

        if pos.side != "flat" and self.is_flatten_time(now):
            return self._close_decision(
                symbol, pos, now, bar_id, f"收盘清仓时间到，均线{self.ma_period}强制平仓"
            )

        if pos.side != "flat":
            return self._manage(
                symbol,
                pos,
                now,
                bar_id,
                close,
                ma_now,
                cross_up=cross_up,
                cross_down=cross_down,
            )

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经处理过均线穿越")

        if cross_up:
            stop = float(pd.to_numeric(bar["low"], errors="coerce"))
            return self._open(symbol, now, bar_id, close, "long", stop, ma_now)
        if cross_down:
            stop = float(pd.to_numeric(bar["high"], errors="coerce"))
            return self._open(symbol, now, bar_id, close, "short", stop, ma_now)

        side_txt = "多" if close > ma_now else ("空" if close < ma_now else "贴着")
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"价格在{self.ma_period}均线{side_txt}侧 MA={ma_now:.2f}，等穿越再开",
            stop_price=ma_now,
        )

    def _manage(
        self,
        symbol: str,
        pos: PositionState,
        now: datetime,
        bar_id: str,
        close: float,
        ma_now: float,
        *,
        cross_up: bool,
        cross_down: bool,
    ) -> StrategyDecision:
        """持仓：止损跟均线；反向穿越则平或反手。"""
        if pos.side == "long":
            trail = ma_now
            if pos.trail_stop and pos.trail_stop > 0:
                trail = max(float(pos.trail_stop), ma_now)
            pos.trail_stop = trail
        else:
            trail = ma_now
            if pos.trail_stop and pos.trail_stop > 0:
                trail = min(float(pos.trail_stop), ma_now)
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
                    ma_now,
                    reverse_from=pos.side,
                )
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"收盘穿回{self.ma_period}均线，平{'多' if pos.side == 'long' else '空'} MA={ma_now:.2f}",
            )

        if pos.side == "long" and close < ma_now:
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"多单收盘跌破{self.ma_period}均线 {ma_now:.2f}",
            )
        if pos.side == "short" and close > ma_now:
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"空单收盘站上{self.ma_period}均线 {ma_now:.2f}",
            )

        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"均线{self.ma_period}持有 side={pos.side} MA={ma_now:.2f} 现价≈{close:.0f}",
            stop_price=pos.trail_stop or ma_now,
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
        ma_now: float,
        *,
        reverse_from: str | None = None,
    ) -> StrategyDecision:
        """顺着穿越方向开仓。止损先用信号端点，并保证风险距离为正。"""
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0 or pd.isna(stop):
            pad = max(abs(entry) * 0.001, 1.0)
            stop = entry - pad if side == "long" else entry + pad
        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        if reverse_from:
            text = (
                f"均线{self.ma_period}反手{'多' if side == 'long' else '空'} "
                f"原{reverse_from}→{side} 收盘穿越 MA={ma_now:.2f} 止损{stop:.2f}"
            )
        else:
            text = (
                f"均线{self.ma_period}做{'多' if side == 'long' else '空'} "
                f"收盘穿越 MA={ma_now:.2f} entry={entry:.2f} 止损{stop:.2f}"
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
                "signal": "Single_ma",
                "bar_id": bar_id,
                "reverse": bool(reverse_from),
                "ma": ma_now,
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
