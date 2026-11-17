"""
双周期长线共用件。

功能：
  大周期只回答「现在允许多、空、还是停手」；小周期才开仓、平仓、放止损。
  周期从配置读，不写死周线+日线。资料里的简单平均一律改成指数平均。
原理：
  顺大逆小：大趋势向上时，只在小周期的回调或突破里做多，反向信号丢掉。
  大周期 ADX 太低，当作没有趋势，空仓不开新仓；已有仓仍用小周期规则管。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import pandas as pd

from src.data.periods import canonical_period, period_label

from .base import StrategyBase
from .indicators import ema_series, macd_lines
from .market_state import adx_series
from .session_scope import scope_allows, scope_from_cfg
from .types import (
    STOP_MODE_TOUCH,
    PositionState,
    StrategyAction,
    StrategyDecision,
    config_flag,
    stop_mode_from_cfg,
)

Bias = Literal["long", "short", "none"]

# 界面上可选的大周期。小周期仍跟左边「信号周期」。
HTF_MENU = ("周线", "日线", "60分钟", "30分钟", "15分钟")
HTF_BY_LABEL = {
    "周线": "W",
    "日线": "D",
    "60分钟": "60",
    "30分钟": "30",
    "15分钟": "15",
}
FRACTAL_MODE_LABELS = {
    "ema": "指数均线",
    "donchian": "通道突破",
}
FRACTAL_MODE_BY_LABEL = {v: k for k, v in FRACTAL_MODE_LABELS.items()}


def htf_label(code: str) -> str:
    """大周期代码换成界面上的中文。"""
    period = canonical_period(str(code or "D"))
    for name, value in HTF_BY_LABEL.items():
        if value == period:
            return name
    return period_label(period)


def parse_htf(text: str, fallback: str = "D") -> str:
    """界面中文或 D/W/60 都收成程序用的周期代码。"""
    raw = str(text or "").strip()
    if raw in HTF_BY_LABEL:
        return HTF_BY_LABEL[raw]
    if raw in FRACTAL_MODE_BY_LABEL:
        return fallback
    return canonical_period(raw or fallback)


def parse_fractal_mode(text: str) -> str:
    """大周期方向算法：指数均线 或 通道突破。"""
    raw = str(text or "").strip()
    if raw in FRACTAL_MODE_BY_LABEL:
        return FRACTAL_MODE_BY_LABEL[raw]
    if raw in FRACTAL_MODE_LABELS:
        return raw
    return "ema"


def donchian_prior(bars: pd.DataFrame, length: int) -> tuple[float, float] | None:
    """
    不含当前这根的通道上下沿。

    当前收盘才能拿来判断有没有冲出去，否则会用这根自己的最高价跟自己比。
    """
    n = max(2, int(length))
    if bars is None or len(bars) < n + 1:
        return None
    past = bars.iloc[:-1]
    if len(past) < n:
        return None
    high = float(past["high"].iloc[-n:].max())
    low = float(past["low"].iloc[-n:].min())
    if pd.isna(high) or pd.isna(low):
        return None
    return high, low


def last_ema(bars: pd.DataFrame, length: int) -> float | None:
    """最近一根收盘的指数均线；根数不够就没有。"""
    n = max(2, int(length))
    if bars is None or len(bars) < n:
        return None
    close = bars["close"].astype(float)
    value = float(ema_series(close, n).iloc[-1])
    if pd.isna(value):
        return None
    return value


def last_adx(bars: pd.DataFrame, period: int) -> float | None:
    """大周期最后一根的 ADX。太低说明没有方向。"""
    n = max(2, int(period))
    need = n * 3
    if bars is None or len(bars) < need:
        return None
    adx, _plus, _minus = adx_series(
        bars["high"], bars["low"], bars["close"], period=n
    )
    value = float(adx.iloc[-1])
    if pd.isna(value):
        return None
    return value


def last_atr(bars: pd.DataFrame, period: int) -> float | None:
    """平均真实波幅。跳空也算进波动。"""
    n = max(2, int(period))
    if bars is None or len(bars) < n + 1:
        return None
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)
    value = float(true_range.iloc[-n:].mean())
    if pd.isna(value) or value <= 0:
        return None
    return value


def last_macd_dif(
    bars: pd.DataFrame,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> float | None:
    """当前收盘的 MACD DIF（快慢两条指数均线的差）。"""
    if bars is None or len(bars) < max(slow, fast) + signal:
        return None
    lines = macd_lines(
        bars["close"].astype(float), fast=fast, slow=slow, signal=signal
    )
    value = float(lines["dif"].iloc[-1])
    if pd.isna(value):
        return None
    return value


def htf_bias(
    trend_bars: pd.DataFrame | None,
    *,
    mode: str,
    donchian: int,
    ema_len: int,
    adx_period: int,
    adx_min: float,
) -> tuple[Bias, str]:
    """
    大周期方向。

    通道突破：收盘站上前 N 根高点才偏多，跌破前 N 根低点才偏空，夹在中间当震荡。
    指数均线：收盘在均线上方偏多、下方偏空（资料里的简单平均已换成指数平均）。
    ADX 低于门槛时一律停手，不顺着均线硬开。
    """
    if trend_bars is None or len(trend_bars) < 5:
        return "none", "大周期K线不足"
    adx = last_adx(trend_bars, adx_period)
    if adx is None:
        return "none", "大周期方向强度还算不出来"
    if adx < float(adx_min):
        return "none", f"大周期没有趋势（ADX {adx:.1f} < {adx_min:g}）"
    close = float(trend_bars["close"].iloc[-1])
    kind = str(mode or "ema").strip().lower()
    if kind == "donchian":
        band = donchian_prior(trend_bars, donchian)
        if band is None:
            return "none", "大周期通道还没满"
        up, down = band
        if close > up:
            return "long", f"大周期收盘升破通道上沿 {up:.2f}"
        if close < down:
            return "short", f"大周期收盘跌破通道下沿 {down:.2f}"
        return "none", "大周期还在通道里面"
    ma = last_ema(trend_bars, ema_len)
    if ma is None:
        return "none", "大周期均线还算不出来"
    if close > ma:
        return "long", f"大周期收盘在指数均线 {ma:.2f} 上方"
    if close < ma:
        return "short", f"大周期收盘在指数均线 {ma:.2f} 下方"
    return "none", "大周期收盘贴着均线"


def strong_bottom_fractal(bars: pd.DataFrame) -> bool:
    """
    强底分型：中间一根最低，第三根最高价升破第一根最高价。

    只看高低点位置，不看阴阳线。第三根还没走完时不要调用。
    """
    if bars is None or len(bars) < 3:
        return False
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    h1, l1 = float(high.iloc[-3]), float(low.iloc[-3])
    l2 = float(low.iloc[-2])
    h3, l3 = float(high.iloc[-1]), float(low.iloc[-1])
    return l2 < l1 and l2 < l3 and h3 > h1


def strong_top_fractal(bars: pd.DataFrame) -> bool:
    """强顶分型：中间一根最高，第三根最低价跌破第一根最低价。"""
    if bars is None or len(bars) < 3:
        return False
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    h1, l1 = float(high.iloc[-3]), float(low.iloc[-3])
    h2 = float(high.iloc[-2])
    h3, l3 = float(high.iloc[-1]), float(low.iloc[-1])
    return h2 > h1 and h2 > h3 and l3 < l1


class DualCycleMixin(StrategyBase):
    """
    双周期策略共用：读周期、交易时段、止损成交、ATR 跟踪。

    子类自己写开仓和平仓条件，这里不决定哪一根会开仓。
    """

    uses_trend_bars = True

    def _bind_dual_cycle(self, cfg: dict[str, Any], section: str, default_htf: str) -> dict[str, Any]:
        """把配置里这一节的公共字段接到实例上，返回这一节字典给子类继续读。"""
        super().__init__()
        self.cfg = cfg
        st = cfg.get("strategy", {}) or {}
        box = st.get(section) or {}
        if not isinstance(box, dict):
            box = {}
        trade = cfg.get("trade", {}) or {}

        raw_tf = st.get("timeframe")
        self.timeframe = canonical_period(str(raw_tf or "D"))
        raw_trail = st.get("trail_timeframe")
        if raw_trail in (None, ""):
            self.trail_timeframe = self.timeframe
        else:
            self.trail_timeframe = canonical_period(str(raw_trail))
        self.trend_timeframe = parse_htf(str(box.get("trend_timeframe") or default_htf), default_htf)
        self.trend_name = period_label(self.trend_timeframe)

        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH

        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        self.flatten_at_close = config_flag(box.get("flatten_at_close", False), default=False)
        self.entry_sessions = session_tuples(cfg, live)
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {
            s: PositionState(symbol=s) for s in self.symbols
        }
        self._pending: dict[str, dict[str, Any]] = {}
        self._extreme: dict[str, float] = {}
        return box

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
        """开仓记下止损和极值；平仓清掉记忆。"""
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        pending = self._pending.pop(symbol, None) or {}
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            side = "long" if action == StrategyAction.BUY_OPEN else "short"
            stop = float(initial_stop if initial_stop is not None else pending.get("stop") or price)
            when = pending.get("time") or datetime.now()
            bar_id = str(pending.get("bar_id") or "")
            pos.side = side
            pos.volume = max(1, int(volume or 1))
            pos.entry_price = float(price)
            pos.initial_stop = stop
            pos.trail_stop = stop
            pos.trail_armed = True
            pos.opened_at = when.isoformat(timespec="seconds") if hasattr(when, "isoformat") else str(when)
            self._extreme[symbol] = float(price)
            if bar_id:
                self._last_entry_bar[symbol] = bar_id
                self._signaled_bar[symbol] = bar_id
            return
        if action == StrategyAction.CLOSE:
            self.positions[symbol] = PositionState(symbol=symbol)
            self._extreme.pop(symbol, None)

    def _is_entry_bar(self, symbol: str, bar_id: str) -> bool:
        """开仓这根自己的高低点不能拿来当已经打到止损。"""
        entry_bar = self._last_entry_bar.get(symbol)
        if not entry_bar or not bar_id:
            return False
        try:
            return pd.Timestamp(bar_id) == pd.Timestamp(entry_bar)
        except (TypeError, ValueError):
            return str(bar_id) == str(entry_bar)

    def _ratchet_stop(
        self,
        symbol: str,
        pos: PositionState,
        bar: pd.Series,
        atr: float,
        atr_mult: float,
    ) -> float:
        """止损只往有利方向收。多单跟着新高下移，空单跟着新低上移。"""
        stop = float(pos.trail_stop or pos.initial_stop)
        if pos.side == "long":
            extreme = max(self._extreme.get(symbol, float(bar["high"])), float(bar["high"]))
            self._extreme[symbol] = extreme
            stop = max(stop, extreme - atr_mult * atr)
        else:
            extreme = min(self._extreme.get(symbol, float(bar["low"])), float(bar["low"]))
            self._extreme[symbol] = extreme
            stop = min(stop, extreme + atr_mult * atr)
        pos.trail_stop = stop
        return stop

    def _close_now(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        pos: PositionState,
        reason: str,
        *,
        stop_price: float | None = None,
    ) -> StrategyDecision:
        self._pending[symbol] = {"time": now, "bar_id": bar_id}
        self._signaled_bar[symbol] = bar_id
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            reason,
            stop_price=stop_price if stop_price is not None else (pos.trail_stop or pos.initial_stop),
        )
