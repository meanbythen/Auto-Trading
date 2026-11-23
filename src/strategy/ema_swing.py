"""
均线波段（可隔夜）。

功能：方向均线金叉后只做多、死叉后只做空，没再叉就不改方向。
     开仓看三条信号均线有没有按方向排好，且价格在多空分界线正确一侧。
     不要求抓住交叉那一根（跳空错过也可以开）。
     倾角均线走平或斜反了，不开新仓。
     「倾角不够就平仓」开关打开时，手里的仓也会平掉（不反手）。出厂关掉。
     空仓时，日盘/夜盘收盘前若干分钟不再开新仓（出厂 30，可改；午休 11:30 不算收盘）。
     止损跟着止损均线走。不用 MACD 柱子或 DIF/DEA。
原理：慢均线定大方向；快线排列给出场点。
     多空分界线、止损、倾角各看一根均线，出厂都是 120，根数可自己填。
     临近收盘再开，剩下的波动不够赔跳空和滑点。
     仓位和信号周期用配置页公共项。
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any

import pandas as pd
from loguru import logger

from .base import StrategyBase
from .indicators import ema_series
from .session_scope import scope_allows, scope_from_cfg
from .slope import (
    adaptive_threshold_from_bars,
    latest_atr,
    measure_slope,
)
from .types import (
    STOP_MODE_TOUCH,
    PositionState,
    StrategyAction,
    StrategyDecision,
    config_flag,
    stop_mode_from_cfg,
    stop_triggered,
)


class EmaSwingStrategy(StrategyBase):
    """均线波段：方向均线定多空，信号均线排好就开，止损跟指定均线。

    引擎名、配置节、下单记录都用 ema_swing / 「均线波段」。
    """

    SIGNAL_NAME = "ema_swing"  # 成交记录、回测表上的引擎名
    SECTION = "ema_swing"  # 配置文件里这一节的名字
    # 空仓时，离日盘/夜盘收盘不到这么多分钟就不开。已有仓继续拿。出厂 30。
    NO_OPEN_BEFORE_CLOSE_MIN = 30

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        self.cfg = cfg
        st = cfg.get("strategy", {}) or {}
        box = st.get(self.SECTION) or {}
        trade = cfg.get("trade", {}) or {}

        self.timeframe = str(st.get("timeframe", "5"))
        # 止损周期跟左边公共项，不再强行抄成信号周期。
        self.trail_timeframe = str(st.get("trail_timeframe") or self.timeframe)
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH
        self.price_tick = float(trade.get("price_tick", 1) or 1)

        # 方向选择器：两根均线根数自己填，出厂 60 和 120。兼容旧键 ema_fast / ema_slow。
        self.fast_ma, self.slow_ma = parse_dir_pair(
            box.get("dir_ema_pair"),
            box.get("dir_ema_fast", box.get("ema_fast", 60)),
            box.get("dir_ema_slow", box.get("ema_slow", 120)),
        )
        self.sig_a, self.sig_b, self.sig_c = parse_sig_triple(
            box.get("sig_ema_triple"),
            box.get("sig_ema_a", 5),
            box.get("sig_ema_b", 10),
            box.get("sig_ema_c", 30),
        )
        # 止损、多空分界线、倾角默认同一根。旧配置只写了 stop_ema 时，另外两根跟着它。
        self.stop_ema = parse_ema_len(box.get("stop_ema", 120), 120)
        self.side_ema = parse_ema_len(box.get("side_ema", self.stop_ema), self.stop_ema)
        self.slope_ema = parse_ema_len(box.get("slope_ema", self.stop_ema), self.stop_ema)

        # 只看倾角均线自己斜不斜。0=关掉。
        try:
            self.slope_min_deg = float(box.get("slope_min_deg", 3) or 0)
        except (TypeError, ValueError):
            self.slope_min_deg = 3.0
        if self.slope_min_deg < 0:
            self.slope_min_deg = 0.0
        try:
            self.slope_lookback = int(float(box.get("slope_lookback", 20) or 20))
        except (TypeError, ValueError):
            self.slope_lookback = 20
        self.slope_lookback = max(2, min(120, self.slope_lookback))
        # 斜率估计方式：reg=最近 N 根线性回归（抗端点毛刺，默认），diff=首尾两点差（旧口径）。
        method = str(box.get("slope_method", "reg") or "reg").strip().lower()
        self.slope_method = method if method in {"reg", "diff"} else "reg"
        # 斜率的 t 值门槛：0=关（默认）。角度管陡不陡，t 值管这条线可不可信。
        try:
            self.slope_min_t = abs(float(box.get("slope_min_t", 0.0) or 0))
        except (TypeError, ValueError):
            self.slope_min_t = 0.0
        # 自适应阈值：0 关闭时完全走固定角度；打开后阈值 = 过去窗口 |角度| 的分位数，
        # 夹在基准的 0.7~1.3 倍内，每天不跳、逐根都能算。
        self.slope_adaptive = config_flag(box.get("slope_adaptive", False), default=False)
        try:
            self.slope_adaptive_quantile = float(box.get("slope_adaptive_quantile", 0.80) or 0.80)
        except (TypeError, ValueError):
            self.slope_adaptive_quantile = 0.80
        self.slope_adaptive_quantile = min(max(self.slope_adaptive_quantile, 0.05), 0.95)
        try:
            self.slope_adaptive_window = int(float(box.get("slope_adaptive_window", 500) or 500))
        except (TypeError, ValueError):
            self.slope_adaptive_window = 500
        self.slope_adaptive_window = max(50, min(5000, self.slope_adaptive_window))
        # 日线开盘冻结：同一交易日只算一次阈值；夜盘 21:00 起算下一交易日。
        self.slope_adaptive_daily = config_flag(box.get("slope_adaptive_daily", True), default=True)
        self._adaptive_cache: dict[str, tuple[str, float]] = {}
        # 出厂关：倾角只拦新开仓。打开后倾角不够也会平掉已有仓，方便对照收益。
        self.slope_exit = config_flag(box.get("slope_exit", False), default=False)
        # 尾盘空仓不开：填 0 等于关掉这一条。
        self.no_open_before_close_min = parse_close_guard_min(
            box.get("no_open_before_close_min", self.NO_OPEN_BEFORE_CLOSE_MIN),
            self.NO_OPEN_BEFORE_CLOSE_MIN,
        )

        self.flatten_at_close = config_flag(box.get("flatten_at_close", False), default=False)
        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        self.entry_sessions = session_tuples(cfg, live)
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {
            s: PositionState(symbol=s) for s in self.symbols
        }
        self._pending: dict[str, dict[str, Any]] = {}
        self._close_side: dict[str, str] = {}
        self._close_entry: dict[str, float] = {}
        # 换月移仓：上一段结束时还拿着仓，新主力沿用这个方向，不再等三线重新排。
        self._carry_side: str | None = None
        self._carry_volume: int = 1

    @property
    def required_bars(self) -> int:
        """均线最长根数 + 倾角回看，少了算不热。"""
        return (
            max(self.slow_ma, self.stop_ema, self.side_ema, self.slope_ema, self.sig_c)
            + max(2, self.slope_lookback)
            + 2
        )

    def set_rollover_carry(self, side: str, volume: int = 1) -> None:
        """回测换月：下一根空仓时按这个方向在新主力开仓。"""
        text = str(side or "").strip().lower()
        self._carry_side = text if text in {"long", "short"} else None
        try:
            self._carry_volume = max(1, int(volume or 1))
        except (TypeError, ValueError):
            self._carry_volume = 1

    def is_flatten_time(self, now: datetime | None = None) -> bool:
        if not self.flatten_at_close:
            return False
        from src.utils.session_clock import in_flatten_window

        return in_flatten_window(self.flatten_times, now or datetime.now())

    def _too_close_to_session_close(self, now: datetime, symbol: str | None = None) -> bool:
        """空仓专用：日盘 15:00、夜盘 23:00/02:30 前若干分钟不开。午休不算。填 0 关掉。"""
        if self.no_open_before_close_min <= 0:
            return False
        if str(self.timeframe).upper() in {"D", "W"}:
            return False
        from src.utils.session_clock import near_session_close, sessions_for_symbol

        live = symbol or (self.symbols[0] if self.symbols else "")
        return near_session_close(
            now,
            sessions_for_symbol(self.cfg, live),
            minutes=self.no_open_before_close_min,
        )

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
        """空仓看信号均线排好没有；有仓时止损跟着止损均线，方向均线反向交叉则平。"""
        _ = trail_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = max(self.slow_ma, self.stop_ema, self.side_ema, self.slope_ema, self.sig_c) + max(
            2, self.slope_lookback
        ) + 2
        if bars is None or len(bars) < need:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"K线不足，算不出 EMA{self.slow_ma}",
            )
        if "close" not in bars.columns:
            return StrategyDecision(StrategyAction.HOLD, symbol, "K线没有收盘价")

        close_s = pd.to_numeric(bars["close"], errors="coerce")
        ema_a = ema_series(close_s, self.sig_a)
        ema_b = ema_series(close_s, self.sig_b)
        ema_c = ema_series(close_s, self.sig_c)
        ema_fast = ema_series(close_s, self.fast_ma)
        ema_slow = ema_series(close_s, self.slow_ma)
        ema_stop = ema_series(close_s, self.stop_ema)
        ema_side = ema_stop if self.side_ema == self.stop_ema else ema_series(close_s, self.side_ema)
        if self.slope_ema == self.stop_ema:
            ema_slope = ema_stop
        elif self.slope_ema == self.side_ema:
            ema_slope = ema_side
        else:
            ema_slope = ema_series(close_s, self.slope_ema)
        series_ok = (ema_a, ema_b, ema_c, ema_fast, ema_slow, ema_stop, ema_side, ema_slope)
        if any(pd.isna(s.iloc[i]) for s in series_ok for i in (-1, -2)):
            return StrategyDecision(StrategyAction.HOLD, symbol, "均线还没算稳")

        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        close = float(close_s.iloc[-1])
        e_fast, e_fast_p = float(ema_fast.iloc[-1]), float(ema_fast.iloc[-2])
        e_slow, e_slow_p = float(ema_slow.iloc[-1]), float(ema_slow.iloc[-2])
        e_stop = float(ema_stop.iloc[-1])
        e_side = float(ema_side.iloc[-1])
        va, vb, vc = float(ema_a.iloc[-1]), float(ema_b.iloc[-1]), float(ema_c.iloc[-1])
        last_cross = _last_cross_side(ema_fast, ema_slow)
        cross_up = e_fast_p <= e_slow_p and e_fast > e_slow
        cross_down = e_fast_p >= e_slow_p and e_fast < e_slow
        stacked = _fast_stacked(last_cross, va, vb, vc)

        if pos.side != "flat" and self.is_flatten_time(now):
            return self._close_decision(symbol, pos, now, bar_id, "收盘清仓时间到，均线波段强制平仓")

        if pos.side != "flat":
            return self._manage(
                symbol,
                pos,
                now,
                bar_id,
                close,
                high=float(bar.get("high", close)),
                low=float(bar.get("low", close)),
                stop_line=e_stop,
                cross_up=cross_up,
                cross_down=cross_down,
                bars=bars,
                close_s=close_s,
                ema_slope=ema_slope,
            )

        # 换月移仓：不再等三线排列和倾角。大方向已经反了，或站到分界线错边，就取消移仓，改走普通开仓。
        if self._carry_side in {"long", "short"}:
            wait_side = self._carry_side
            if self.is_flatten_time(now):
                return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，换月移仓等下一根")
            if not self.in_entry_session(now, symbol):
                return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段，换月移仓等下一根")
            if not last_cross or last_cross != wait_side or _broke_stop_ma(wait_side, close, e_side):
                self._carry_side = None
            else:
                lots = max(1, int(self._carry_volume or 1))
                self._carry_side = None
                return self._open(
                    symbol,
                    now,
                    bar_id,
                    close,
                    wait_side,
                    e_stop,
                    va,
                    vb,
                    vc,
                    rollover=True,
                    volume=lots,
                )

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if self._too_close_to_session_close(now, symbol):
            mins = self.no_open_before_close_min
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"空仓且收盘前{mins}分钟，不再开新仓（风险大于这段能赚的）",
            )
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经处理过")

        if not last_cross:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"还没有 EMA{self.fast_ma}/{self.slow_ma} 金叉或死叉，"
                f"EMA{self.fast_ma}={e_fast:.2f} EMA{self.slow_ma}={e_slow:.2f}",
            )

        broken = _broke_stop_ma(last_cross, close, e_side)
        wait = "空" if last_cross == "short" else "多"
        if broken:
            if last_cross == "short":
                msg = (
                    f"大方向仍是空（EMA{self.fast_ma}/{self.slow_ma} 没金叉）。"
                    f"价格 {close:.2f} 还在多空分界线 EMA{self.side_ema} {e_side:.2f} 上面，"
                    f"站错边，等跌破再开空"
                )
            else:
                msg = (
                    f"大方向仍是多（EMA{self.fast_ma}/{self.slow_ma} 没死叉）。"
                    f"价格 {close:.2f} 还在多空分界线 EMA{self.side_ema} {e_side:.2f} 下面，"
                    f"站错边，等站上再开多"
                )
            return StrategyDecision(StrategyAction.HOLD, symbol, msg)
        if not stacked:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                (
                    f"大方向{wait}，等 EMA{self.sig_a}/{self.sig_b}/{self.sig_c} "
                    f"{'多头' if last_cross == 'long' else '空头'}排列"
                    f"（现在 {va:.2f}/{vb:.2f}/{vc:.2f}）"
                ),
            )
        blocked, slope_msg = _stop_ma_too_flat(
            last_cross,
            ema_slope,
            bars,
            close_s,
            lookback=self.slope_lookback,
            min_deg=self.slope_min_deg,
            period=self.slope_ema,
            method=self.slope_method,
            min_t=self.slope_min_t,
            adaptive=self.slope_adaptive,
            adaptive_quantile=self.slope_adaptive_quantile,
            adaptive_window=self.slope_adaptive_window,
            adaptive_daily=self.slope_adaptive_daily,
            adaptive_cache=self._adaptive_cache,
            symbol=symbol,
        )
        if blocked:
            return StrategyDecision(StrategyAction.HOLD, symbol, slope_msg)
        return self._open(symbol, now, bar_id, close, last_cross, e_stop, va, vb, vc)

    def _manage(
        self,
        symbol: str,
        pos: PositionState,
        now: datetime,
        bar_id: str,
        close: float,
        *,
        high: float,
        low: float,
        stop_line: float,
        cross_up: bool,
        cross_down: bool,
        bars: pd.DataFrame,
        close_s: pd.Series,
        ema_slope: pd.Series,
    ) -> StrategyDecision:
        # 本根算出的 EMA 止损下一根才用来判止损；判定和成交都用上一根已知的线。
        prev_stop = float(pos.trail_stop or pos.initial_stop or 0.0)
        judge_stop = prev_stop or float(stop_line)
        pos.trail_stop = float(stop_line)
        opposite = (pos.side == "long" and cross_down) or (pos.side == "short" and cross_up)
        if opposite:
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"EMA{self.fast_ma}/{self.slow_ma}"
                f"{'死叉' if cross_down else '金叉'}，大方向反了，平"
                f"{'多' if pos.side == 'long' else '空'}",
            )

        if judge_stop > 0 and stop_triggered(
            side=pos.side, stop=judge_stop, close=close, high=high, low=low, mode=self.stop_mode
        ):
            return self._close_decision(
                symbol,
                pos,
                now,
                bar_id,
                f"碰到 EMA{self.stop_ema} 止损 {judge_stop:.2f}，平仓",
                stop_price=judge_stop,
            )

        if self.slope_exit:
            blocked, slope_msg = _stop_ma_too_flat(
                pos.side,
                ema_slope,
                bars,
                close_s,
                lookback=self.slope_lookback,
                min_deg=self.slope_min_deg,
                period=self.slope_ema,
                method=self.slope_method,
                min_t=self.slope_min_t,
                adaptive=self.slope_adaptive,
                adaptive_quantile=self.slope_adaptive_quantile,
                adaptive_window=self.slope_adaptive_window,
                adaptive_daily=self.slope_adaptive_daily,
                adaptive_cache=self._adaptive_cache,
                symbol=symbol,
            )
            if blocked:
                return self._close_decision(
                    symbol,
                    pos,
                    now,
                    bar_id,
                    f"倾角不够就平仓：{slope_msg}",
                )

        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"均线波段持有{'多' if pos.side == 'long' else '空'}，"
            f"止损跟着 EMA{self.stop_ema}={stop_line:.2f}",
            stop_price=stop_line,
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
        e_a: float,
        e_b: float,
        e_c: float,
        *,
        rollover: bool = False,
        volume: int | None = None,
    ) -> StrategyDecision:
        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        if rollover:
            text = (
                f"换月移仓：上一段还是{'多' if side == 'long' else '空'}，"
                f"新主力同方向开{'多' if side == 'long' else '空'}，"
                f"止损改挂 EMA{self.stop_ema}={stop:.2f}"
            )
        else:
            text = (
                f"均线波段快线{'多头' if side == 'long' else '空头'}排列开"
                f"{'多' if side == 'long' else '空'} "
                f"（EMA{self.sig_a}={e_a:.2f} {self.sig_b}={e_b:.2f} {self.sig_c}={e_c:.2f}），"
                f"止损 EMA{self.stop_ema}={stop:.2f}"
            )
        self._pending[symbol] = {"time": now, "stop": stop, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        logger.info("{} {}", symbol, text)
        meta: dict[str, Any] = {"signal": self.SIGNAL_NAME, "bar_id": bar_id}
        if rollover:
            meta["rollover_carry"] = True
            try:
                meta["volume"] = max(1, int(volume or 1))
            except (TypeError, ValueError):
                meta["volume"] = 1
        return StrategyDecision(
            action,
            symbol,
            text,
            stop_price=stop,
            entry_price=entry,
            meta=meta,
        )

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
        self._close_side[symbol] = pos.side
        self._close_entry[symbol] = float(pos.entry_price or 0.0)
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            reason,
            entry_price=pos.entry_price,
            stop_price=stop_price if stop_price is not None else (pos.trail_stop or pos.initial_stop),
        )


def _period(value: Any, default: int, lo: int, hi: int) -> int:
    """均线根数。写错就用出厂值。"""
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    return max(int(lo), min(int(hi), out))


def parse_close_guard_min(value: Any, default: int = 30) -> int:
    """尾盘空仓不开的分钟数。0=关掉。最多 180（三个小时）。"""
    return _period(value, default, 0, 180)


def parse_ema_len(value: Any, default: int = 120) -> int:
    """认「120」「EMA120」「EMA(21)」这种写法。算均线时就用这个根数。"""
    text = str(value or "").upper()
    # 去掉 EMA、括号和空格，只留下数字。不算斜杠，免得 60/120 被糊成一个数。
    for junk in ("EMA", "(", ")", "（", "）", " "):
        text = text.replace(junk, "")
    if not text:
        return _period(default, 120, 2, 240)
    return _period(text, default, 2, 240)


def parse_ema_pair_label(value: Any, *, default_fast: int = 5, default_slow: int = 20) -> tuple[int, int]:
    """认「EMA5 / EMA20」「5/20」。认不出就用出厂短线和长线。"""
    text = str(value or "").strip().upper().replace(" ", "")
    text = text.replace("EMA", "")
    for sep in ("/", "-", ",", "对"):
        if sep in text:
            left, right = text.split(sep, 1)
            try:
                return _ema_pair_ints(int(left), int(right), default_fast, default_slow)
            except (TypeError, ValueError):
                break
    return _ema_pair_ints(default_fast, default_slow, default_fast, default_slow)


def _ema_pair_ints(fast: Any, slow: Any, default_fast: int, default_slow: int) -> tuple[int, int]:
    """短均线必须小于长均线。"""
    try:
        a = int(float(fast))
    except (TypeError, ValueError):
        a = int(default_fast)
    try:
        b = int(float(slow))
    except (TypeError, ValueError):
        b = int(default_slow)
    a = max(2, min(240, a))
    b = max(3, min(240, b))
    if a == b:
        b = a + 1
    if a > b:
        a, b = b, a
    return a, b


def parse_dir_pair(label: Any, fast: Any, slow: Any) -> tuple[int, int]:
    """方向均线对。认「EMA(21)/EMA(122)」或两个数字。短的在前、长的在后。"""
    if label:
        text = str(label)
        for junk in ("(", ")", "（", "）"):
            text = text.replace(junk, "")
        return parse_ema_pair_label(text, default_fast=60, default_slow=120)
    return parse_ema_pair_label(f"{fast}/{slow}", default_fast=60, default_slow=120)


def parse_sig_triple(
    label: Any,
    a: Any = 5,
    b: Any = 10,
    c: Any = 30,
) -> tuple[int, int, int]:
    """
    信号三线。认「5/10/30」「EMA(5)/EMA(10)/EMA(30)」或三个分开的数字。
    读完按从短到长排好：前面必须比后面小。做多时短>中>长才算多头排列。
    """
    text = str(label or "").upper().replace("EMA", " ")
    nums: list[int] = []
    buf = ""
    for ch in text + "/":
        if ch.isdigit():
            buf += ch
        else:
            if buf:
                nums.append(int(buf))
                buf = ""
    if len(nums) < 3:
        nums = [_period(a, 5, 2, 120), _period(b, 10, 3, 180), _period(c, 30, 4, 240)]
    x, y, z = nums[0], nums[1], nums[2]
    x, y, z = _period(x, 5, 2, 120), _period(y, 10, 3, 180), _period(z, 30, 4, 240)
    ordered = tuple(sorted({x, y, z}))
    if len(ordered) < 3:
        return (5, 10, 30)
    return ordered[0], ordered[1], ordered[2]


def format_sig_triple(a: int, b: int, c: int) -> str:
    """写成 5/10/30，给优化网格和配置对照。"""
    x, y, z = parse_sig_triple(f"{a}/{b}/{c}")
    return f"{x}/{y}/{z}"


def _stop_ma_too_flat(
    side: str,
    ema_stop: pd.Series,
    bars: pd.DataFrame,
    close_s: pd.Series,
    *,
    lookback: int,
    min_deg: float,
    period: int,
    method: str = "reg",
    min_t: float = 0.0,
    adaptive: bool = False,
    adaptive_quantile: float = 0.80,
    adaptive_window: int = 500,
    adaptive_daily: bool = True,
    adaptive_cache: dict[str, tuple[str, float]] | None = None,
    symbol: str = "",
) -> tuple[bool, str]:
    """倾角均线走平或斜反了，拦新开仓。已经持仓的不管。

    method="reg" 默认：最近 N+1 根做一元线性回归，用拟合斜率换算 N 根累计涨幅；
    method="diff" 旧口径：只看首尾两点。两种都按 N × ATR 归一化。
    min_t 是斜率 t 值的下限：角度够陡但拟合不可信（噪声大）时仍按震荡处理。
    adaptive=True 时阈值改成过去 window 根 |角度| 的分位数，夹在基准 0.7~1.3 倍内。
    """
    if min_deg <= 0 and min_t <= 0:
        return False, ""
    n = max(2, int(lookback))
    if len(ema_stop) <= n:
        return True, f"EMA{period} 还不够 {n} 根，算不出倾角，先不开"
    atr = latest_atr(bars, close_s)
    measure = measure_slope(ema_stop, atr, lookback=n, method=method)
    if measure is None:
        return True, f"EMA{period} 倾角还没算稳，先不开"
    ang = measure.angle_deg
    threshold = min_deg
    note = ""
    if adaptive and min_deg > 0:
        threshold = adaptive_threshold_from_bars(
            bars,
            ema_stop,
            base_deg=min_deg,
            lookback=n,
            method=method,
            quantile=adaptive_quantile,
            window=adaptive_window,
            daily=adaptive_daily,
            cache=adaptive_cache,
            key=symbol,
        )
        if abs(threshold - min_deg) > 1e-9:
            note = f"（自适应阈值 {threshold:.1f}°，基准 {min_deg:g}°）"
    if min_deg > 0:
        if side == "long" and ang < threshold:
            return True, (
                f"EMA{period} 近 {n} 根倾角 {ang:.1f}°，不够往上（要 ≥ {threshold:g}°），"
                f"走平或在往下，当震荡不开多{note}"
            )
        if side == "short" and ang > -threshold:
            return True, (
                f"EMA{period} 近 {n} 根倾角 {ang:.1f}°，不够往下（要 ≤ -{threshold:g}°），"
                f"走平或在往上，当震荡不开空{note}"
            )
    if min_t > 0:
        t_val = measure.t_stat
        want = "多" if side == "long" else "空"
        if math.isnan(t_val):
            return True, (
                f"EMA{period} 近 {n} 根斜率 t 值算不出（{method} 口径），"
                f"这条线不够可信，当震荡不开{want}"
            )
        if abs(t_val) < min_t:
            return True, (
                f"EMA{period} 近 {n} 根斜率 t 值 {t_val:.2f}，绝对值低于 {min_t:g}，"
                f"这条线不够可信，当震荡不开{want}"
            )
    msg = f"EMA{period} 倾角 {ang:.1f}°{note}"
    if min_t > 0:
        msg += f"，t 值 {measure.t_stat:.2f}"
    return False, msg


def _fast_stacked(side: str, e_a: float, e_b: float, e_c: float) -> bool:
    """三条信号均线已经按大方向排好（现在的状态，不要求本根刚交叉）。"""
    if side == "long":
        return e_a > e_b > e_c
    if side == "short":
        return e_a < e_b < e_c
    return False


def _broke_stop_ma(side: str, close: float, stop_ma: float) -> bool:
    """多空分界线：做多必须收盘高于这根均线，做空必须低于。贴着也不开。"""
    if side == "long":
        return close <= stop_ma
    return close >= stop_ma


def _broke_ema120(side: str, close: float, ema120: float) -> bool:
    """旧名字，测试还在用。"""
    return _broke_stop_ma(side, close, ema120)


def _last_cross_side(fast: pd.Series, slow: pd.Series) -> str:
    """从历史里找出最近一次金叉/死叉。金叉后一直算多，直到再死叉。"""
    side = ""
    n = min(len(fast), len(slow))
    for i in range(1, n):
        f0, f1 = fast.iloc[i - 1], fast.iloc[i]
        s0, s1 = slow.iloc[i - 1], slow.iloc[i]
        if pd.isna(f0) or pd.isna(f1) or pd.isna(s0) or pd.isna(s1):
            continue
        if float(f0) <= float(s0) and float(f1) > float(s1):
            side = "long"
        elif float(f0) >= float(s0) and float(f1) < float(s1):
            side = "short"
    return side
