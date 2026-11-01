"""
早盘定方向 + 浮盈保本 + 拿收盘（A/B 两套方向判定共用一套开平仓）。

功能：开盘后半小时只观察；再往后一小时窗口开仓；方向本段只定一次；
     回踩箱体边界入场；初始止损在箱体另一侧外；浮盈达箱体高度比例后移损到成本保本；
     否则拿到本段收盘。每段最多两笔。假突破本段封仓。
原理：开盘半小时是博弈陷阱区；半小时后脱离平衡区的一方是本段主方向。
     日盘：09:00 观察起、09:30 定方向窗口起、10:30 停新开；清仓 14:55。
     夜盘：21:00 / 21:30 / 22:30 一一对应；清仓 22:55（不是 23:00）。
     临清仓 15 分钟不新开。
     方案 A：箱体放量突破。方案 B：半小时高低递进。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Literal

import pandas as pd
from loguru import logger

from .session_scope import scope_allows, scope_block_reason, scope_from_cfg
from .types import PositionState, StrategyAction, StrategyDecision, config_flag


def _parse_hhmm(value: str) -> dtime:
    hour, minute = str(value).split(":")
    return dtime(int(hour), int(minute))


def _naive(moment: datetime) -> datetime:
    stamp = pd.Timestamp(moment)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.to_pydatetime().replace(microsecond=0)


def _with_clock(bars: pd.DataFrame) -> pd.DataFrame:
    out = bars.copy()
    index = pd.DatetimeIndex(pd.to_datetime(out.index))
    if index.tz is not None:
        index = index.tz_localize(None)
    out.index = index
    return out


Direction = Literal["long", "short", "flat", "pending", "aborted"]
SessionKind = Literal["day", "night"]


@dataclass(frozen=True)
class _SessionClock:
    """一段交易（日盘或夜盘）的钟点。"""

    kind: SessionKind
    label: str
    session_open: dtime
    observe_end: dtime
    entry_end: dtime
    session_close: dtime


@dataclass
class _DayState:
    """某一段（日盘或夜盘）的状态：箱体、方向、名额、假突破。"""

    day: date
    kind: SessionKind
    session_open: dtime
    observe_end: dtime
    entry_end: dtime
    session_close: dtime
    label: str = ""
    h30: float = 0.0
    l30: float = 0.0
    box_ready: bool = False
    direction: Direction = "pending"
    direction_text: str = ""
    break_bar_index: int | None = None
    fake_check_left: int = 0
    trades_done: int = 0
    opened_today: int = 0
    be_armed: bool = False
    best_price: float = 0.0


class MorningEdgeBase(ABC):
    """三套方案共用：日盘+夜盘时段、回踩入场、止损保本、收盘清仓、每段两笔。"""

    config_key: str = "morning_edge"
    scheme_name: str = "早盘"

    def __init__(self, cfg: dict[str, Any]) -> None:
        st = cfg.get("strategy", {}) or {}
        box = st.get(self.config_key) or {}
        trade = cfg.get("trade", {}) or {}
        ctp = trade.get("ctp") or {}

        self.timeframe = str(st.get("timeframe", "5"))
        self.trail_timeframe = self.timeframe
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        # 公共设置：全时段 / 只做日盘 / 只做夜盘。只拦开新仓，持仓照旧。
        self.session_scope = scope_from_cfg(cfg)

        # 日盘默认；可被配置覆盖
        self.day_clock = _SessionClock(
            kind="day",
            label="日盘",
            session_open=_parse_hhmm(str(box.get("session_open", "09:00"))),
            observe_end=_parse_hhmm(str(box.get("observe_end", "09:30"))),
            entry_end=_parse_hhmm(str(box.get("entry_end", "10:30"))),
            session_close=_parse_hhmm(str(box.get("session_close", "15:00"))),
        )
        # 夜盘：21:00≈09:00，21:30≈09:30，22:30≈10:30，23:00≈15:00
        self.enable_night = config_flag(box.get("enable_night", True), default=True)
        self.night_clock = _SessionClock(
            kind="night",
            label="夜盘",
            session_open=_parse_hhmm(str(box.get("night_session_open", "21:00"))),
            observe_end=_parse_hhmm(str(box.get("night_observe_end", "21:30"))),
            entry_end=_parse_hhmm(str(box.get("night_entry_end", "22:30"))),
            session_close=_parse_hhmm(str(box.get("night_session_close", "23:00"))),
        )

        # 临清仓（日盘 14:55 / 夜盘 22:55）前 N 分钟不新开
        self.no_new_before_close_minutes = max(
            0, int(box.get("no_new_before_close_minutes", 15))
        )
        self.vol_confirm_bars = max(1, int(box.get("vol_confirm_bars", 3)))
        self.fake_break_bars = max(1, int(box.get("fake_break_bars", 2)))
        self.pullback_tol_pct = float(box.get("pullback_tol_pct", 0.0015))
        self.stop_buffer_ticks = max(1, int(box.get("stop_buffer_ticks", 2)))
        self.price_tick = float(box.get("price_tick") or ctp.get("price_tick") or 1.0)
        self.be_box_mult = float(box.get("be_box_mult", 0.9))
        self.max_trades_per_day = max(1, int(box.get("max_trades_per_day", 2)))

        # 强制平仓时刻：日盘 14:55、夜盘 22:55（夜盘不要用 23:00）
        self.flatten_times = [
            _parse_hhmm(x) for x in st.get("flatten_times", ["14:55", "22:55"])
        ]
        self.flatten_at_close = config_flag(st.get("flatten_at_close", True), default=True)

        self.positions: dict[str, PositionState] = {
            s: PositionState(symbol=s) for s in self.symbols
        }
        # symbol -> 当前段状态（换日盘/夜盘会换新状态）
        self._day: dict[str, _DayState] = {}
        self._last_entry_bar: dict[str, str] = {}
        self._close_side: dict[str, str] = {}
        self._close_entry: dict[str, float] = {}

    # ------------------------------------------------------------------ 时段

    def _resolve_clock(self, now: datetime) -> _SessionClock | None:
        """
        当前落在哪一段。

        日盘：开盘～日盘收盘（含午后持仓到 15:00 前）。
        夜盘：夜盘开盘～23:00。
        两段之间（例如下午收盘后到 21:00 前）返回 None。
        """
        t = now.time().replace(second=0, microsecond=0)
        if self.enable_night and self.night_clock.session_open <= t <= self.night_clock.session_close:
            return self.night_clock
        if self.day_clock.session_open <= t <= self.day_clock.session_close:
            return self.day_clock
        return None

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

    def _in_observe(self, clock: _SessionClock, now: datetime) -> bool:
        t = now.time().replace(second=0, microsecond=0)
        return clock.session_open <= t <= clock.observe_end

    def _in_entry_window(self, clock: _SessionClock, now: datetime) -> bool:
        t = now.time().replace(second=0, microsecond=0)
        return clock.observe_end < t <= clock.entry_end

    def _near_flatten(self, now: datetime) -> bool:
        """
        临清仓前几分钟不新开。

        日盘对照 14:55，夜盘对照 22:55。默认提前 15 分钟。
        """
        if self.no_new_before_close_minutes <= 0:
            return False
        t = now.time().replace(second=0, microsecond=0)
        cur = datetime.combine(now.date(), t)
        for ft in self.flatten_times:
            close_at = datetime.combine(now.date(), ft)
            begin = close_at - timedelta(minutes=self.no_new_before_close_minutes)
            if begin <= cur < close_at:
                return True
        return False

    def _before_session_open(self, clock: _SessionClock, now: datetime) -> bool:
        t = now.time().replace(second=0, microsecond=0)
        return t < clock.session_open

    # ------------------------------------------------------------------ 成交

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        st = self._day.get(symbol)
        if action == StrategyAction.BUY_OPEN:
            pos.side = "long"
            pos.volume = volume
            pos.entry_price = price
            pos.initial_stop = float(initial_stop if initial_stop is not None else price)
            pos.trail_stop = pos.initial_stop
            pos.trail_armed = False
            pos.opened_at = datetime.now().isoformat(timespec="seconds")
            if st:
                st.opened_today += 1
                st.be_armed = False
                st.best_price = price
        elif action == StrategyAction.SELL_OPEN:
            pos.side = "short"
            pos.volume = volume
            pos.entry_price = price
            pos.initial_stop = float(initial_stop if initial_stop is not None else price)
            pos.trail_stop = pos.initial_stop
            pos.trail_armed = False
            pos.opened_at = datetime.now().isoformat(timespec="seconds")
            if st:
                st.opened_today += 1
                st.be_armed = False
                st.best_price = price
        elif action == StrategyAction.CLOSE:
            if st is not None:
                st.trades_done += 1
                st.be_armed = False
            self.positions[symbol] = PositionState(symbol=symbol)
            self._close_side.pop(symbol, None)
            self._close_entry.pop(symbol, None)

    # ------------------------------------------------------------------ 评估

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        _ = trail_bars
        now = _naive(now or datetime.now())
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        if bars is None or len(bars) < 6:
            return StrategyDecision(StrategyAction.HOLD, symbol, "K线不足")

        df = _with_clock(bars)
        df = df.loc[df.index <= pd.Timestamp(now)]
        if df.empty:
            return StrategyDecision(StrategyAction.HOLD, symbol, "没有可用K线")

        last = df.iloc[-1]
        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])
        bar_id = str(last.name)
        idx = len(df) - 1

        clock = self._resolve_clock(now)

        # 收盘清仓优先（日盘 14:55 / 夜盘 22:55）
        if pos.side != "flat" and self.is_flatten_time(now):
            return self._close_decision(symbol, pos, "收盘清仓时间到，强制平仓")

        # 持仓：即使已过开仓窗口，只要还在本段收盘前，继续管止损/保本
        if pos.side != "flat":
            st = self._day.get(symbol)
            if st is None:
                # 没有状态时用当前钟点凑一个，只为管仓
                if clock is None:
                    return StrategyDecision(
                        StrategyAction.HOLD,
                        symbol,
                        "持仓中，但不在日盘/夜盘时段",
                        stop_price=pos.trail_stop,
                        entry_price=pos.entry_price,
                    )
                st = self._ensure_session(symbol, now.date(), clock, df, now)
            return self._manage_open(symbol, st, pos, high, low, close)

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if self._near_flatten(now):
            return StrategyDecision(
                StrategyAction.HOLD, symbol, "临收盘15分钟内，不再开新仓"
            )

        if clock is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "非日盘/夜盘交易时段")
        if not scope_allows(self.session_scope, now):
            return StrategyDecision(StrategyAction.HOLD, symbol, scope_block_reason(self.session_scope))

        st = self._ensure_session(symbol, now.date(), clock, df, now)

        # 观察期：只维护箱体
        if self._in_observe(clock, now) or self._before_session_open(clock, now):
            if self._before_session_open(clock, now):
                return StrategyDecision(
                    StrategyAction.HOLD, symbol, f"未到{clock.label}开盘，等待"
                )
            self._refresh_box(st, df, now)
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{self.scheme_name}{clock.label}观察期，记录箱体"
                + (f" H={st.h30:.0f} L={st.l30:.0f}" if st.h30 > st.l30 else ""),
            )

        if not st.box_ready:
            self._finalize_box(st, df, now)
        if not st.box_ready:
            return StrategyDecision(
                StrategyAction.HOLD, symbol, f"{clock.label}箱体还没定出来"
            )

        self._update_direction(symbol, st, df, now, idx)

        if st.direction == "aborted":
            return StrategyDecision(StrategyAction.HOLD, symbol, "假突破，本段封仓")
        if st.direction == "flat":
            return StrategyDecision(
                StrategyAction.HOLD, symbol, st.direction_text or "判定震荡，本段不做"
            )
        if st.direction == "pending":
            t = now.time().replace(second=0, microsecond=0)
            if t > clock.entry_end:
                st.direction = "flat"
                st.direction_text = f"{clock.label}窗口结束仍无有效突破，本段不做"
                return StrategyDecision(StrategyAction.HOLD, symbol, st.direction_text)
            return StrategyDecision(
                StrategyAction.HOLD, symbol, st.direction_text or "方向未定，等待确认"
            )

        if not self._in_entry_window(clock, now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "已过开仓窗口，不再新开")
        if st.trades_done >= self.max_trades_per_day or st.opened_today >= self.max_trades_per_day:
            return StrategyDecision(StrategyAction.HOLD, symbol, "本段开仓名额已用完")
        if self._last_entry_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经下过开仓单")
        if st.break_bar_index is not None and idx <= st.break_bar_index:
            return StrategyDecision(StrategyAction.HOLD, symbol, "方向刚确认，等回踩再进")

        return self._try_pullback_entry(symbol, st, close, high, low, bar_id)

    # ------------------------------------------------------------------ 箱体

    def _ensure_session(
        self,
        symbol: str,
        day: date,
        clock: _SessionClock,
        df: pd.DataFrame,
        now: datetime,
    ) -> _DayState:
        cur = self._day.get(symbol)
        if cur is None or cur.day != day or cur.kind != clock.kind:
            cur = _DayState(
                day=day,
                kind=clock.kind,
                session_open=clock.session_open,
                observe_end=clock.observe_end,
                entry_end=clock.entry_end,
                session_close=clock.session_close,
                label=clock.label,
            )
            self._day[symbol] = cur
            self._refresh_box(cur, df, now)
        return cur

    def _session_slice(
        self, df: pd.DataFrame, day: date, start: dtime, end: dtime
    ) -> pd.DataFrame:
        day_ts = pd.Timestamp(day)
        a = day_ts + pd.Timedelta(hours=start.hour, minutes=start.minute)
        b = day_ts + pd.Timedelta(hours=end.hour, minutes=end.minute)
        return df.loc[(df.index > a) & (df.index <= b)]

    def _refresh_box(self, st: _DayState, df: pd.DataFrame, now: datetime) -> None:
        window = self._session_slice(df, st.day, st.session_open, st.observe_end)
        if window.empty or not {"high", "low"}.issubset(window.columns):
            return
        h = float(pd.to_numeric(window["high"], errors="coerce").max())
        l = float(pd.to_numeric(window["low"], errors="coerce").min())
        if pd.isna(h) or pd.isna(l) or h <= l:
            return
        st.h30, st.l30 = h, l
        t = now.time().replace(second=0, microsecond=0)
        if t >= st.observe_end:
            st.box_ready = True

    def _finalize_box(self, st: _DayState, df: pd.DataFrame, now: datetime) -> None:
        self._refresh_box(st, df, now)
        if st.h30 > st.l30 > 0:
            st.box_ready = True

    # ------------------------------------------------------------------ 方向

    @abstractmethod
    def _update_direction(
        self,
        symbol: str,
        st: _DayState,
        df: pd.DataFrame,
        now: datetime,
        idx: int,
    ) -> None:
        """根据方案更新 st.direction。"""

    def _check_fake_breakout(self, st: _DayState, df: pd.DataFrame, idx: int) -> None:
        if st.direction not in ("long", "short"):
            return
        if st.fake_check_left <= 0 or st.break_bar_index is None:
            return
        if idx <= st.break_bar_index:
            return
        close = float(df["close"].iloc[-1])
        if st.direction == "long" and close < st.h30:
            st.direction = "aborted"
            st.direction_text = "向上突破后快速收回箱体，假突破，本段封仓"
            st.fake_check_left = 0
            logger.info("{} {}", self.scheme_name, st.direction_text)
            return
        if st.direction == "short" and close > st.l30:
            st.direction = "aborted"
            st.direction_text = "向下突破后快速收回箱体，假突破，本段封仓"
            st.fake_check_left = 0
            logger.info("{} {}", self.scheme_name, st.direction_text)
            return
        if idx > st.break_bar_index:
            passed = idx - st.break_bar_index
            st.fake_check_left = max(0, self.fake_break_bars - passed)

    # ------------------------------------------------------------------ 入场 / 持仓

    def _try_pullback_entry(
        self,
        symbol: str,
        st: _DayState,
        close: float,
        high: float,
        low: float,
        bar_id: str,
    ) -> StrategyDecision:
        buf = self.stop_buffer_ticks * self.price_tick
        height = st.h30 - st.l30
        if height <= 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "箱体高度异常")

        if st.direction == "long":
            near = st.h30 * (1.0 + self.pullback_tol_pct)
            far = st.h30 * (1.0 - self.pullback_tol_pct)
            if not (low <= near and close >= far and close >= st.h30 * 0.999):
                return StrategyDecision(
                    StrategyAction.HOLD,
                    symbol,
                    f"多头已定，等回踩H30={st.h30:.2f}企稳",
                )
            entry = close
            stop = st.l30 - buf
            if stop >= entry:
                return StrategyDecision(StrategyAction.HOLD, symbol, "多单止损距离异常")
            self._last_entry_bar[symbol] = bar_id
            return StrategyDecision(
                StrategyAction.BUY_OPEN,
                symbol,
                f"{self.scheme_name}{st.label}多 回踩H30={st.h30:.2f}企稳，止损{stop:.2f}",
                stop_price=stop,
                entry_price=entry,
                meta={"h30": st.h30, "l30": st.l30, "scheme": self.config_key, "kind": st.kind},
            )

        if st.direction == "short":
            near = st.l30 * (1.0 - self.pullback_tol_pct)
            far = st.l30 * (1.0 + self.pullback_tol_pct)
            if not (high >= near and close <= far and close <= st.l30 * 1.001):
                return StrategyDecision(
                    StrategyAction.HOLD,
                    symbol,
                    f"空头已定，等反弹L30={st.l30:.2f}承压",
                )
            entry = close
            stop = st.h30 + buf
            if stop <= entry:
                return StrategyDecision(StrategyAction.HOLD, symbol, "空单止损距离异常")
            self._last_entry_bar[symbol] = bar_id
            return StrategyDecision(
                StrategyAction.SELL_OPEN,
                symbol,
                f"{self.scheme_name}{st.label}空 反弹L30={st.l30:.2f}承压，止损{stop:.2f}",
                stop_price=stop,
                entry_price=entry,
                meta={"h30": st.h30, "l30": st.l30, "scheme": self.config_key, "kind": st.kind},
            )

        return StrategyDecision(StrategyAction.HOLD, symbol, "方向不明，不开仓")

    def _manage_open(
        self,
        symbol: str,
        st: _DayState,
        pos: PositionState,
        high: float,
        low: float,
        close: float,
    ) -> StrategyDecision:
        height = max(st.h30 - st.l30, 0.0)
        be_need = height * self.be_box_mult

        if pos.side == "long":
            st.best_price = max(st.best_price or pos.entry_price, high)
            float_pnl = st.best_price - pos.entry_price
            if (not st.be_armed) and be_need > 0 and float_pnl >= be_need:
                st.be_armed = True
                pos.trail_armed = True
                pos.trail_stop = float(pos.entry_price)
                logger.info("{} 多单浮盈达标，止损移到开仓价保本", symbol)
            stop = float(pos.entry_price if st.be_armed else pos.initial_stop)
            pos.trail_stop = stop
            if low <= stop:
                kind = "保本" if st.be_armed else "初始止损"
                return self._close_decision(symbol, pos, f"多单{kind}触发 stop={stop:.4f}")
        else:
            if not st.best_price:
                st.best_price = low
            else:
                st.best_price = min(st.best_price, low)
            float_pnl = pos.entry_price - st.best_price
            if (not st.be_armed) and be_need > 0 and float_pnl >= be_need:
                st.be_armed = True
                pos.trail_armed = True
                pos.trail_stop = float(pos.entry_price)
                logger.info("{} 空单浮盈达标，止损移到开仓价保本", symbol)
            stop = float(pos.entry_price if st.be_armed else pos.initial_stop)
            pos.trail_stop = stop
            if high >= stop:
                kind = "保本" if st.be_armed else "初始止损"
                return self._close_decision(symbol, pos, f"空单{kind}触发 stop={stop:.4f}")

        tag = "保本中" if st.be_armed else "守初始止损"
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"持仓中 side={pos.side} {tag} stop={float(pos.trail_stop):.4f}",
            stop_price=pos.trail_stop,
            entry_price=pos.entry_price,
        )

    def _close_decision(
        self, symbol: str, pos: PositionState, reason: str
    ) -> StrategyDecision:
        self._close_side[symbol] = pos.side
        self._close_entry[symbol] = float(pos.entry_price or 0.0)
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            reason,
            stop_price=pos.trail_stop,
            entry_price=pos.entry_price,
        )

    def _volume_ok(self, df: pd.DataFrame) -> bool:
        if "volume" not in df.columns or len(df) < self.vol_confirm_bars + 1:
            return False
        vols = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        avg = float(vols.iloc[-(self.vol_confirm_bars + 1) : -1].mean())
        cur = float(vols.iloc[-1])
        return avg > 0 and cur > avg

    def _in_entry_window_st(self, st: _DayState, now: datetime) -> bool:
        t = now.time().replace(second=0, microsecond=0)
        return st.observe_end < t <= st.entry_end


# ====================================================================== 方案 A


class MorningBoxAStrategy(MorningEdgeBase):
    """方案A：开盘30分钟平衡箱体，放量收盘突破确认（日盘+夜盘）。"""

    config_key = "morning_box_a"
    scheme_name = "早盘箱体A"

    def _update_direction(
        self,
        symbol: str,
        st: _DayState,
        df: pd.DataFrame,
        now: datetime,
        idx: int,
    ) -> None:
        if st.direction in ("aborted", "flat"):
            return
        if st.direction in ("long", "short"):
            self._check_fake_breakout(st, df, idx)
            return
        if not self._in_entry_window_st(st, now):
            return
        close = float(df["close"].iloc[-1])
        if close > st.h30 and self._volume_ok(df):
            st.direction = "long"
            st.direction_text = f"{st.label}放量收盘站上H30={st.h30:.2f}，本段只做多"
            st.break_bar_index = idx
            st.fake_check_left = self.fake_break_bars
            logger.info("{} {}", self.scheme_name, st.direction_text)
            return
        if close < st.l30 and self._volume_ok(df):
            st.direction = "short"
            st.direction_text = f"{st.label}放量收盘跌破L30={st.l30:.2f}，本段只做空"
            st.break_bar_index = idx
            st.fake_check_left = self.fake_break_bars
            logger.info("{} {}", self.scheme_name, st.direction_text)
            return
        st.direction_text = f"箱体[{st.l30:.2f},{st.h30:.2f}]内等待放量突破"


# ====================================================================== 方案 B


class MorningStructureBStrategy(MorningEdgeBase):
    """方案B：开盘半小时高低点递进结构定方向（日盘+夜盘）。"""

    config_key = "morning_structure_b"
    scheme_name = "早盘结构B"

    def _update_direction(
        self,
        symbol: str,
        st: _DayState,
        df: pd.DataFrame,
        now: datetime,
        idx: int,
    ) -> None:
        if st.direction in ("long", "short", "flat", "aborted"):
            if st.direction in ("long", "short"):
                close = float(df["close"].iloc[-1])
                if st.direction == "long" and close < st.l30 and self._volume_ok(df):
                    st.direction = "aborted"
                    st.direction_text = "多头结构后反向跌破箱体，本段封仓"
                elif st.direction == "short" and close > st.h30 and self._volume_ok(df):
                    st.direction = "aborted"
                    st.direction_text = "空头结构后反向升破箱体，本段封仓"
            return

        t = now.time().replace(second=0, microsecond=0)
        if t < st.observe_end:
            return
        window = self._session_slice(df, st.day, st.session_open, st.observe_end)
        side, text = _structure_bias(window)
        if side == "long":
            st.direction = "long"
            st.direction_text = f"{st.label}{text}"
            st.break_bar_index = idx
            st.fake_check_left = 0
        elif side == "short":
            st.direction = "short"
            st.direction_text = f"{st.label}{text}"
            st.break_bar_index = idx
            st.fake_check_left = 0
        else:
            st.direction = "flat"
            st.direction_text = text or f"{st.label}半小时无递进结构，本段不做"
        logger.info("{} {}", self.scheme_name, st.direction_text)


def _structure_bias(window: pd.DataFrame) -> tuple[str, str]:
    if window is None or len(window) < 4:
        return "flat", "半小时K线太少，看不清结构"
    n = len(window)
    mid = max(2, n // 2)
    first = window.iloc[:mid]
    second = window.iloc[mid:]
    f_hi = float(first["high"].max())
    f_lo = float(first["low"].min())
    s_hi = float(second["high"].max())
    s_lo = float(second["low"].min())
    if s_lo > f_lo and s_hi > f_hi:
        return "long", "半小时低点抬高且高点抬高，本段只做多"
    if s_hi < f_hi and s_lo < f_lo:
        return "short", "半小时高点降低且低点降低，本段只做空"
    return "flat", "半小时高低点穿插，判定震荡"
