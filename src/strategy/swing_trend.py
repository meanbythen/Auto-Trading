"""
长线趋势加减仓（大小周期自相似）。

功能：
  - 大周期判断方向（收盘在大周期 EMA 上方只做多，下方只做空）
  - 小周期回调靠近 EMA 后再顺势确认，才开第一笔
  - 趋势还在时，浮盈够了、又出现回调，可以加仓（有次数上限）
  - 价格离均线冲太远时先减仓锁定一部分；大周期翻向或止损触发再清掉剩余
原理：
  - 大周期定方向仍用本小节的搭配（日线或 60 分钟）
  - 回调进出跟配置页「信号周期」；跟踪止损跟「止损周期」
  - 大趋势不是「错过一根突破就整段放弃」，而是持续看阶段：回调可进、伸展可减
  - 仓位管理比信号更重要：有加有减，不是一路加到底
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any, Literal

import pandas as pd
from loguru import logger

from src.data.periods import canonical_period, period_label

from .base import StrategyBase
from .indicators import ema_series
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

Bias = Literal["long", "short", "flat"]

# 周期搭配：大周期定方向 + 小周期回调进出。名字给人看，值写进配置。
SCALE_PAIRS: dict[str, tuple[str, str]] = {
    "D_60": ("D", "60"),
    "60_15": ("60", "15"),
}
SCALE_LABELS: dict[str, str] = {
    "D_60": "日线定方向",
    "60_15": "60分钟定方向",
}
DEFAULT_SCALE = "D_60"


def resolve_scale_pair(box: dict[str, Any]) -> tuple[str, str, str]:
    """
    读出（搭配代号, 大周期, 小周期）。

    优先认 scale 代号；没有代号时，用已写的 trend/signal 周期反推；
    再不行就用默认日线+60。
    """
    raw = str(box.get("scale") or "").strip()
    if raw in SCALE_PAIRS:
        trend_tf, signal_tf = SCALE_PAIRS[raw]
        return raw, trend_tf, signal_tf
    trend_tf = str(box.get("trend_timeframe") or "").strip()
    signal_tf = str(box.get("signal_timeframe") or "").strip()
    for code, (t, s) in SCALE_PAIRS.items():
        if trend_tf == t and signal_tf == s:
            return code, t, s
    if trend_tf and signal_tf:
        # 允许高级用户手写别的配对，仍按自相似逻辑跑。
        return "custom", trend_tf, signal_tf
    t, s = SCALE_PAIRS[DEFAULT_SCALE]
    return DEFAULT_SCALE, t, s


def scale_label(code: str) -> str:
    """搭配代号换成界面上的中文。"""
    return SCALE_LABELS.get(code, code or DEFAULT_SCALE)


def _parse_hhmm(value: str) -> dtime:
    hour, minute = str(value).split(":")
    return dtime(int(hour), int(minute))


def _ema(series: pd.Series, period: int) -> float | None:
    """最近这段收盘的指数均线；K 线不够就返回 None。"""
    if series is None or len(series) < period:
        return None
    value = float(ema_series(series, period).iloc[-1])
    if pd.isna(value):
        return None
    return value


def _atr(bars: pd.DataFrame, period: int) -> float | None:
    """平均真实波幅，跳空也算进波动。"""
    if bars is None or len(bars) < period + 1:
        return None
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    value = float(tr.iloc[-period:].mean())
    if pd.isna(value) or value <= 0:
        return None
    return value


class SwingTrendStrategy(StrategyBase):
    """
    大周期定方向 + 信号周期回调进出，支持有限次加仓与伸展减仓。

    决策通过 meta.scale / meta.volume 告诉执行层：首开、加仓、减仓、全平。
    """

    uses_trend_bars = True

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        self.cfg = cfg
        st = cfg.get("strategy", {}) or {}
        box = st.get("swing_trend") or {}
        if not isinstance(box, dict):
            box = {}
        trade = cfg.get("trade", {}) or {}

        # 大周期定方向看搭配；回调看公共信号周期，止损看公共止损周期。
        # 没填公共周期时，退回搭配里的小周期（旧 yaml 仍是 60 分钟）。
        self.scale, self.trend_timeframe, scale_signal = resolve_scale_pair(box)
        raw_tf = st.get("timeframe")
        if raw_tf in (None, ""):
            self.timeframe = canonical_period(str(scale_signal or "60"))
        else:
            self.timeframe = canonical_period(str(raw_tf))
        raw_trail = st.get("trail_timeframe")
        if raw_trail in (None, ""):
            self.trail_timeframe = self.timeframe
        else:
            self.trail_timeframe = canonical_period(str(raw_trail))
        self.trend_name = period_label(self.trend_timeframe)

        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        # 止损触发方式（全策略共用）。收盘确认时调度层不会把未收线那根拼进来。
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH

        # daily_ma 名字沿用：其实是「大周期均线根数」，60+15 时也是这个键。
        self.daily_ma = max(2, int(box.get("daily_ma", 20) or 20))
        self.signal_ma = max(2, int(box.get("signal_ma", 20) or 20))
        self.atr_period = max(2, int(box.get("atr_period", 14) or 14))
        self.pullback_lookback = max(2, int(box.get("pullback_lookback", 8) or 8))
        self.touch_atr = max(0.1, float(box.get("touch_atr", 0.6) or 0.6))

        # 仓位：每笔单元手数；最多加几次；浮盈至少几倍 ATR 才允许加。
        self.unit_volume = max(1, int(box.get("unit_volume", 1) or 1))
        self.max_adds = max(0, int(box.get("max_adds", 2) or 2))
        self.add_min_atr = max(0.0, float(box.get("add_min_atr", 1.0) or 1.0))
        # 伸展减仓：离均线太远先减；至少留几手核心仓。
        self.reduce_extend_atr = max(0.5, float(box.get("reduce_extend_atr", 2.5) or 2.5))
        self.min_core_units = max(1, int(box.get("min_core_units", 1) or 1))
        self.stop_atr = max(0.5, float(box.get("stop_atr", 2.0) or 2.0))
        # 加减仓手数封顶：只用策略自己的「单元手数 ×（1+最多加仓次数）」。
        # 总仓位另由公共的仓位上限%、日最大亏损管，不再读最大持仓手数。
        self.lot_cap = max(1, self.unit_volume * (1 + self.max_adds))

        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        # 长线默认不听收盘清仓总开关；本小节单独控制。
        self.flatten_at_close = config_flag(box.get("flatten_at_close", False), default=False)
        # 长线自己的趋势过滤：大周期没趋势时加减仓没意义。
        self.market_filter = MarketFilter(trend_params_for_section(cfg, "swing_trend"))
        self.entry_sessions = session_tuples(cfg, live)
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {
            s: PositionState(symbol=s) for s in self.symbols
        }
        self._pending: dict[str, dict[str, Any]] = {}
        # 每个品种：已加仓次数、最近一次加减仓的 K 线、持仓以来最好价、是否刚做过伸展减仓。
        self._adds_done: dict[str, int] = {}
        self._best_price: dict[str, float] = {}
        self._reduced_on_extend: dict[str, bool] = {}
        self._units_held: dict[str, int] = {}

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
        """成交回报：首开/加仓改均价；减仓只减手数；全平清空记忆。"""
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        pending = self._pending.pop(symbol, None) or {}
        scale = str(pending.get("scale") or "")
        lots = max(0, int(volume or 0))

        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            side = "long" if action == StrategyAction.BUY_OPEN else "short"
            stop = float(initial_stop if initial_stop is not None else pending.get("stop") or price)
            when = pending.get("time") or datetime.now()
            bar_id = str(pending.get("bar_id") or "")

            if pos.side == side and pos.volume > 0 and scale == "add":
                # 同向加仓：手数累加，开仓价改成加权均价。
                old_v = int(pos.volume)
                old_p = float(pos.entry_price or price)
                new_v = old_v + lots
                pos.entry_price = (old_p * old_v + float(price) * lots) / max(1, new_v)
                pos.volume = new_v
                self._adds_done[symbol] = int(self._adds_done.get(symbol, 0)) + 1
                self._units_held[symbol] = int(self._units_held.get(symbol, 1)) + 1
            else:
                pos.side = side
                pos.volume = lots
                pos.entry_price = float(price)
                self._adds_done[symbol] = 0
                self._units_held[symbol] = 1
                self._reduced_on_extend[symbol] = False
                self._best_price[symbol] = float(price)

            pos.initial_stop = stop
            pos.trail_stop = stop
            pos.trail_armed = True
            pos.opened_at = when.isoformat(timespec="seconds") if hasattr(when, "isoformat") else str(when)
            if bar_id:
                self._last_entry_bar[symbol] = bar_id
                self._signaled_bar[symbol] = bar_id
            return

        if action == StrategyAction.CLOSE:
            if not pos.reduce_close(lots):
                # 还剩仓：减仓后更新单元计数，均价不变。
                units = max(self.min_core_units, int(self._units_held.get(symbol, 1)) - 1)
                self._units_held[symbol] = units
                if scale == "reduce":
                    self._reduced_on_extend[symbol] = True
                if pending:
                    self._pending[symbol] = pending
                return
            self.positions[symbol] = PositionState(symbol=symbol)
            self._adds_done.pop(symbol, None)
            self._best_price.pop(symbol, None)
            self._reduced_on_extend.pop(symbol, None)
            self._units_held.pop(symbol, None)

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        """先看大周期方向，再在信号周期上找回调开仓 / 加减仓 / 离场。"""
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = max(self.signal_ma, self.atr_period, self.pullback_lookback) + 3
        if self.market_filter.enabled:
            need = max(need, self.market_filter.params.need_bars)
        if bars is None or len(bars) < need:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{period_label(self.timeframe)}K线不足，长线趋势还算不出来",
            )

        bias = self._daily_bias(trend_bars)
        if bias == "flat":
            if pos.side != "flat":
                # 大周期看不清时，有仓先用止损管，不再加仓；这里不强制平。
                return self._manage(symbol, bars, pos, bias, now, trail_bars)
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{self.trend_name}方向不清，长线不开新仓",
            )

        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        # 有仓时先管止损、翻向、清仓。不能因为「这根已经开过/加过」就把后面几分钟的扫描跳掉。
        if pos.side != "flat":
            return self._manage(symbol, bars, pos, bias, now, trail_bars)

        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"这一根{period_label(self.timeframe)}已经处理过",
            )

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")

        # 文档第 1 条：行情不匹配直接停手，长线也不硬扛震荡。
        state = self.market_filter.check(bars)
        if not state.ok:
            return StrategyDecision(
                StrategyAction.HOLD, symbol, f"行情不匹配：{state.reason}"
            )

        return self._try_first_entry(symbol, bars, bias, now, bar_id)

    # ------------------------------------------------------------------ 方向与入场

    def _daily_bias(self, trend_bars: pd.DataFrame | None) -> Bias:
        """大周期收盘相对均线：上方偏多，下方偏空。"""
        if trend_bars is None or len(trend_bars) < self.daily_ma:
            return "flat"
        close = trend_bars["close"].astype(float)
        ma = _ema(close, self.daily_ma)
        if ma is None:
            return "flat"
        last = float(close.iloc[-1])
        if last > ma:
            return "long"
        if last < ma:
            return "short"
        return "flat"

    def _pullback_ready(self, bars: pd.DataFrame, bias: Bias, ma: float, atr: float) -> bool:
        """
        最近若干根里，价格曾经靠近或刺过小周期均线（回调），
        并且当前收盘又回到顺势一侧（确认）。
        """
        look = bars.iloc[-self.pullback_lookback :]
        closes = look["close"].astype(float)
        lows = look["low"].astype(float)
        highs = look["high"].astype(float)
        band = self.touch_atr * atr
        last = float(closes.iloc[-1])
        if bias == "long":
            touched = bool(((lows <= ma + band) | (closes <= ma + band)).any())
            resumed = last > ma
            return touched and resumed
        touched = bool(((highs >= ma - band) | (closes >= ma - band)).any())
        resumed = last < ma
        return touched and resumed

    def _try_first_entry(
        self,
        symbol: str,
        bars: pd.DataFrame,
        bias: Bias,
        now: datetime,
        bar_id: str,
    ) -> StrategyDecision:
        close = bars["close"].astype(float)
        ma = _ema(close, self.signal_ma)
        atr = _atr(bars, self.atr_period)
        if ma is None or atr is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "均线或波动还没算稳")
        if not self._pullback_ready(bars, bias, ma, atr):
            side_txt = "多" if bias == "long" else "空"
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{self.trend_name}偏{side_txt}，等{period_label(self.timeframe)}回调靠近均线后再顺势确认",
            )

        entry = float(close.iloc[-1])
        sig_name = period_label(self.timeframe)
        if bias == "long":
            stop = entry - self.stop_atr * atr
            action = StrategyAction.BUY_OPEN
            text = (
                f"长线首开多 {self.trend_name}偏多，{sig_name}回调后收盘回到均线上方"
                f"（均线{ma:.2f}），止损{stop:.2f}"
            )
        else:
            stop = entry + self.stop_atr * atr
            action = StrategyAction.SELL_OPEN
            text = (
                f"长线首开空 {self.trend_name}偏空，{sig_name}回调后收盘回到均线下方"
                f"（均线{ma:.2f}），止损{stop:.2f}"
            )
        unit = self._clamp_unit(self.unit_volume, held=0)
        if unit <= 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "已达策略加仓上限，无法首开")
        return self._open_decision(
            symbol, now, bar_id, action, entry, stop, text, scale="open", volume=unit
        )

    # ------------------------------------------------------------------ 持仓管理

    def _manage(
        self,
        symbol: str,
        bars: pd.DataFrame,
        pos: PositionState,
        bias: Bias,
        now: datetime,
        trail_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        close = float(bar["close"])
        # 加减仓用信号周期波动；止损 ATR 在止损周期不同时用止损那根 K 线。
        sig_atr = _atr(bars, self.atr_period)
        atr_src = trail_bars if trail_bars is not None and len(trail_bars) >= self.atr_period + 1 else bars
        stop_atr = _atr(atr_src, self.atr_period)
        if sig_atr is None:
            sig_atr = stop_atr
        if stop_atr is None:
            stop_atr = sig_atr
        ma = _ema(bars["close"].astype(float), self.signal_ma)
        if sig_atr is None or stop_atr is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "波动还算不出来，先持仓观望")

        # 刷新持仓以来最好价，后面加仓/止损都用。
        best = self._best_price.get(symbol, float(pos.entry_price or close))
        if pos.side == "long":
            best = max(best, close, float(bar["high"]))
        else:
            best = min(best, close, float(bar["low"]))
        self._best_price[symbol] = best

        # 1) 大周期翻向：趋势坏了，剩余全平。
        if bias != "flat" and (
            (pos.side == "long" and bias == "short") or (pos.side == "short" and bias == "long")
        ):
            return self._close_decision(
                symbol,
                now,
                bar_id,
                pos.volume,
                f"{self.trend_name}大方向翻了，长线清仓",
                scale="exit",
            )

        if self.is_flatten_time(now):
            return self._close_decision(
                symbol, now, bar_id, pos.volume, "收盘清仓时间到，长线清仓", scale="exit"
            )

        # 2) 止损：用初始止损与「最好价回撤 stop_atr」里更紧的一侧。
        stop = self._current_stop(pos, best, stop_atr)
        pos.trail_stop = stop
        pos.trail_armed = True
        if not self._is_entry_bar(symbol, bar_id):
            if stop_triggered(
                side=pos.side,
                stop=stop,
                close=close,
                high=float(bar["high"]),
                low=float(bar["low"]),
                mode=self.stop_mode,
            ):
                return self._close_decision(
                    symbol, now, bar_id, pos.volume, f"长线止损触发 {stop:.2f}", scale="exit"
                )

        # 这根已经开过/加过/减过：止损和清仓已经看过，不要同一根再加一次。
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"长线持仓中（{scale_label(self.scale)}）均价{pos.entry_price:.2f} "
                f"止损{stop:.2f} 已加仓{self._adds_done.get(symbol, 0)}次",
                stop_price=stop,
            )

        # 3) 伸展减仓：离均线太远，先减一单元，留下核心仓。
        if ma is not None and not self._reduced_on_extend.get(symbol, False):
            extend = (close - ma) / sig_atr if pos.side == "long" else (ma - close) / sig_atr
            core = self.min_core_units * self.unit_volume
            if extend >= self.reduce_extend_atr and int(pos.volume) > core:
                cut = min(self.unit_volume, int(pos.volume) - core)
                if cut > 0:
                    self._signaled_bar[symbol] = bar_id
                    return self._close_decision(
                        symbol,
                        now,
                        bar_id,
                        cut,
                        f"长线减仓 价格离均线约{extend:.1f}倍波动，先减{cut}手留核心",
                        scale="reduce",
                    )

        # 价格回到均线附近后，允许下一轮再减（伸展标记清掉）。
        if ma is not None:
            near = abs(close - ma) <= self.touch_atr * sig_atr
            if near:
                self._reduced_on_extend[symbol] = False

        # 4) 浮盈回调加仓：大周期方向仍同向，且又出现回调确认。
        if bias == pos.side and ma is not None and self.in_entry_session(now, symbol):
            add_decision = self._try_add(symbol, bars, pos, bias, ma, sig_atr, now, bar_id, close)
            if add_decision is not None:
                return add_decision

        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"长线持仓中（{scale_label(self.scale)}）均价{pos.entry_price:.2f} "
            f"止损{stop:.2f} 已加仓{self._adds_done.get(symbol, 0)}次",
            stop_price=stop,
        )

    def _try_add(
        self,
        symbol: str,
        bars: pd.DataFrame,
        pos: PositionState,
        bias: Bias,
        ma: float,
        atr: float,
        now: datetime,
        bar_id: str,
        close: float,
    ) -> StrategyDecision | None:
        if int(self._adds_done.get(symbol, 0)) >= self.max_adds:
            return None
        room = self.lot_cap - int(pos.volume)
        unit = self._clamp_unit(self.unit_volume, held=int(pos.volume))
        if unit <= 0 or room <= 0:
            return None

        # 浮盈要用「均价」衡量，避免刚加完就立刻再加。
        entry = float(pos.entry_price or close)
        if pos.side == "long":
            profit_atr = (close - entry) / atr
        else:
            profit_atr = (entry - close) / atr
        if profit_atr < self.add_min_atr:
            return None
        if not self._pullback_ready(bars, bias, ma, atr):
            return None

        stop = self._current_stop(pos, self._best_price.get(symbol, entry), atr)
        if pos.side == "long":
            action = StrategyAction.BUY_OPEN
            text = (
                f"长线加仓多 浮盈约{profit_atr:.1f}倍波动，{period_label(self.timeframe)}再次回调确认，加{unit}手"
            )
        else:
            action = StrategyAction.SELL_OPEN
            text = (
                f"长线加仓空 浮盈约{profit_atr:.1f}倍波动，{period_label(self.timeframe)}再次回调确认，加{unit}手"
            )
        return self._open_decision(
            symbol, now, bar_id, action, close, stop, text, scale="add", volume=unit
        )

    def _current_stop(self, pos: PositionState, best: float, atr: float) -> float:
        """初始止损与浮盈回撤止损取更紧的一侧（多单取更高止损价）。"""
        initial = float(pos.initial_stop or 0) or (
            float(pos.entry_price) - self.stop_atr * atr
            if pos.side == "long"
            else float(pos.entry_price) + self.stop_atr * atr
        )
        if pos.side == "long":
            trail = best - self.stop_atr * atr
            return max(initial, trail)
        trail = best + self.stop_atr * atr
        return min(initial, trail)

    def _clamp_unit(self, want: int, *, held: int) -> int:
        # 按策略加仓上限裁剪本笔手数，避免加仓超过单元×次数。
        room = max(0, self.lot_cap - max(0, held))
        return max(0, min(int(want), room))

    def _is_entry_bar(self, symbol: str, bar_id: str) -> bool:
        entry_bar = self._last_entry_bar.get(symbol)
        if not entry_bar or not bar_id:
            return False
        try:
            return pd.Timestamp(bar_id) == pd.Timestamp(entry_bar)
        except (TypeError, ValueError):
            return str(bar_id) == str(entry_bar)

    def _open_decision(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        action: StrategyAction,
        entry: float,
        stop: float,
        text: str,
        *,
        scale: str,
        volume: int,
    ) -> StrategyDecision:
        self._pending[symbol] = {
            "time": now,
            "stop": stop,
            "bar_id": bar_id,
            "scale": scale,
            "volume": int(volume),
        }
        self._signaled_bar[symbol] = bar_id
        logger.info("{} {}", symbol, text)
        return StrategyDecision(
            action,
            symbol,
            text,
            stop_price=stop,
            entry_price=entry,
            meta={
                "signal": f"swing_trend_{scale}",
                "bar_id": bar_id,
                "scale": scale,
                "volume": int(volume),
            },
        )

    def _close_decision(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        volume: int,
        text: str,
        *,
        scale: str,
    ) -> StrategyDecision:
        lots = max(1, int(volume))
        self._pending[symbol] = {
            "time": now,
            "bar_id": bar_id,
            "scale": scale,
            "volume": lots,
        }
        self._signaled_bar[symbol] = bar_id
        logger.info("{} {}", symbol, text)
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            text,
            meta={
                "signal": f"swing_trend_{scale}",
                "bar_id": bar_id,
                "scale": scale,
                "volume": lots,
                "close_volume": lots,
            },
        )
