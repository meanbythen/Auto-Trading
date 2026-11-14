"""
长线箱体突破。

功能：在你选的信号周期上找一段震荡箱子，收盘真正走出箱子才顺着做趋势。
     开仓后这口箱子冻住，止损先守箱子外侧一点点；假突破收回箱子就走。
     浮盈够一个箱子高度之后，改成跟着新高/新低的 ATR 跟踪，把一段大行情拿住。
     信号周期、止损周期跟配置页/回测页左边那两格走。没填时默认日线。
     默认不收盘清仓，仓可以隔夜、隔周。
原理：
     日内开盘箱子适合抓一天；这套改抓「横了很久再选择方向」的波段。
     日线太粗时很多品种走不出大单边，所以周期交给你对照，不写死。
     从日内实体学到的：只认收盘、不认影线；箱子开仓后不能重算；假突破回箱立刻认输。
     从「确认扩张」对照里学到的：ADX 只用来确认还有动能（别在完全没方向时乱打），
     不要等 ADX 已经很高再追，否则进场太晚、止损被箱子拉得很宽。
     第一版不做加仓、不做周线共振、不按长假日历停手，避免一套规则叠很多开关。
     手数仍走公共的每笔风险%，不另写一套 1.5% 资金规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import canonical_period

from .base import StrategyBase
from .indicators import average_true_range
from .market_state import adx_series
from .session_scope import scope_allows, scope_from_cfg
from .types import (
    PositionState,
    StrategyAction,
    StrategyDecision,
    config_flag,
    stop_mode_from_cfg,
    stop_triggered,
)


@dataclass(frozen=True)
class FormedBox:
    """一口已经合格的历史箱子。高低点只用开仓前的 K 线。"""

    high: float
    low: float
    height: float
    atr: float
    reason: str


def _as_float_list(series: pd.Series) -> list[float]:
    return [float(x) for x in series.astype(float).tolist()]


def find_pivots(
    highs: list[float],
    lows: list[float],
    peak_look: int,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """
    找局部拐点。左右各 peak_look 根都比它低/高，才算有效高/低点。

    窗口两端各留 peak_look 根，不看「后面还没走出来的 K」，避免未来函数。
    """
    look = max(1, int(peak_look))
    n = min(len(highs), len(lows))
    peaks: list[tuple[int, float]] = []
    valleys: list[tuple[int, float]] = []
    if n < look * 2 + 1:
        return peaks, valleys
    for i in range(look, n - look):
        h = highs[i]
        if all(h > highs[i - j] and h > highs[i + j] for j in range(1, look + 1)):
            peaks.append((i, h))
        lo = lows[i]
        if all(lo < lows[i - j] and lo < lows[i + j] for j in range(1, look + 1)):
            valleys.append((i, lo))
    return peaks, valleys


def _mean_two_with_gap(
    points: list[tuple[int, float]],
    *,
    take_high: bool,
    min_gap: int,
) -> float | None:
    """
    取最强的两个拐点做平均。两个拐点至少隔 min_gap 根，挤在一起的小波不算箱子沿。
    """
    if len(points) < 2:
        return None
    gap = max(1, int(min_gap))
    ordered = sorted(points, key=lambda item: item[1], reverse=take_high)
    first = ordered[0]
    for other in ordered[1:]:
        if abs(other[0] - first[0]) >= gap:
            return (first[1] + other[1]) / 2.0
    return None


def form_box(
    bars: pd.DataFrame,
    *,
    lookback: int,
    peak_look: int,
    min_gap: int,
    atr_period: int,
    height_atr: float,
    adx_period: int,
    adx_min: float,
) -> FormedBox | None:
    """
    用「当前这根之前」的日线画箱子。当前根只用来判断有没有突破，不参与画框。

    不合格时返回 None（拐点不够、箱子太矮、ADX 太低）。
    """
    if bars is None or len(bars) < lookback + peak_look + 2:
        return None
    hist = bars.iloc[-(lookback + 1) : -1]
    if len(hist) < lookback:
        return None
    highs = _as_float_list(hist["high"])
    lows = _as_float_list(hist["low"])
    peaks, valleys = find_pivots(highs, lows, peak_look)
    box_high = _mean_two_with_gap(peaks, take_high=True, min_gap=min_gap)
    box_low = _mean_two_with_gap(valleys, take_high=False, min_gap=min_gap)
    if box_high is None or box_low is None or box_high <= box_low:
        return None
    atr_series = average_true_range(hist["high"], hist["low"], hist["close"], period=atr_period)
    atr = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    if atr != atr or atr <= 0:
        return None
    height = box_high - box_low
    if height_atr > 0 and height < height_atr * atr:
        return None
    adx, _, _ = adx_series(hist["high"], hist["low"], hist["close"], period=adx_period)
    adx_now = float(adx.iloc[-1]) if len(adx) else 0.0
    if adx_min > 0 and (adx_now != adx_now or adx_now < adx_min):
        return None
    return FormedBox(
        high=box_high,
        low=box_low,
        height=height,
        atr=atr,
        reason=(
            f"箱子 {box_low:.2f}~{box_high:.2f} 高{height:.2f} "
            f"ATR{atr:.2f} ADX{adx_now:.1f}"
        ),
    )


class SwingBoxStrategy(StrategyBase):
    """震荡箱子收盘突破后顺着拿趋势。周期跟配置走。"""

    SIGNAL_NAME = "swing_box"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        self.cfg = cfg
        st = cfg.get("strategy", {}) or {}
        box = st.get("swing_box") or {}
        if not isinstance(box, dict):
            box = {}
        trade = cfg.get("trade", {}) or {}

        # 跟左边公共的信号周期、止损周期。没写时仍用日线，避免旧 yaml 突然变成 5 分钟。
        raw_tf = st.get("timeframe")
        if raw_tf in (None, ""):
            self.timeframe = "D"
        else:
            self.timeframe = canonical_period(str(raw_tf))
        raw_trail = st.get("trail_timeframe")
        if raw_trail in (None, ""):
            self.trail_timeframe = self.timeframe
        else:
            self.trail_timeframe = canonical_period(str(raw_trail))
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = False  # 下面仍读公共 stop_mode；这里只占位，工厂会再写一遍

        self.lookback = max(20, int(box.get("lookback", 40) or 40))
        self.peak_look = max(1, int(box.get("peak_look", 2) or 2))
        self.min_gap = max(2, int(box.get("min_gap", 5) or 5))
        self.atr_period = max(5, int(box.get("atr_period", 20) or 20))
        self.height_atr = max(0.0, float(box.get("height_atr", 1.2) or 0.0))
        self.adx_period = max(5, int(box.get("adx_period", 14) or 14))
        self.adx_min = max(0.0, float(box.get("adx_min", 20) or 0.0))
        self.stop_atr = max(0.0, float(box.get("stop_atr", 0.5) or 0.0))
        self.trail_atr = max(0.5, float(box.get("trail_atr", 2.0) or 2.0))
        self.trail_start_h = max(0.1, float(box.get("trail_start_h", 1.0) or 1.0))
        self.vol_mult = max(0.0, float(box.get("vol_mult", 0) or 0))
        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        self.flatten_at_close = config_flag(box.get("flatten_at_close", False), default=False)
        self.entry_sessions = session_tuples(cfg, live)
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {s: PositionState(symbol=s) for s in self.symbols}
        self._pending: dict[str, dict[str, Any]] = {}
        self._extreme: dict[str, float] = {}
        # 上一笔走完后，要先回到箱子里，再等下一次突破，避免趋势途中反复追同一口破位。
        self._need_inside: dict[str, bool] = {}

    @property
    def required_bars(self) -> int:
        """
        日线至少要：回看窗口 + 当前这根 + 拐点确认留白。
        ATR/ADX 都在回看窗口里算，不必再额外加一截，否则刚够画箱子时还在说「K线不够」。
        """
        return self.lookback + self.peak_look + 2

    def is_flatten_time(self, now: datetime | None = None) -> bool:
        if not self.flatten_at_close:
            return False
        from src.utils.session_clock import in_flatten_window

        return in_flatten_window(self.flatten_times, now or datetime.now())

    def in_entry_session(self, now: datetime, symbol: str | None = None) -> bool:
        """日线/周线时间戳经常是 00:00，不拿分钟交易时段去拦；分钟周期才看交易时段。"""
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
        """成交后把这口箱子冻在持仓上。重启也能从磁盘把箱沿读回来。"""
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        pending = self._pending.pop(symbol, None) or {}
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            pos.side = "long" if action == StrategyAction.BUY_OPEN else "short"
            pos.volume = volume
            pos.entry_price = price
            pos.initial_stop = float(initial_stop or pending.get("stop") or price)
            pos.trail_stop = pos.initial_stop
            pos.trail_armed = False
            when = pending.get("time") or datetime.now()
            pos.opened_at = when.isoformat(timespec="seconds")
            pos.box_high = float(pending.get("box_high") or 0)
            pos.box_low = float(pending.get("box_low") or 0)
            pos.box_height = float(pending.get("box_height") or 0)
            opened_bar = str(pending.get("bar_id") or "")
            if opened_bar:
                self._last_entry_bar[symbol] = opened_bar
            self._extreme[symbol] = float(price)
            self._need_inside[symbol] = False
        elif action == StrategyAction.CLOSE:
            if not pos.reduce_close(volume):
                if pending:
                    self._pending[symbol] = pending
                return
            self.positions[symbol] = PositionState(symbol=symbol)
            self._extreme.pop(symbol, None)
            self._need_inside[symbol] = True

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        # 已经有仓时只需要几根 K 线算 ATR 和收盘是否收回箱子，不必等满回看窗口。
        if pos.side != "flat":
            if bars is None or len(bars) < 3:
                return StrategyDecision(StrategyAction.HOLD, symbol, "K线不够，没法管仓")
            bar = bars.iloc[-1]
            return self._manage(symbol, bars, bar, now, str(bar.name), trail_bars)
        if bars is None or len(bars) < self.required_bars:
            return StrategyDecision(StrategyAction.HOLD, symbol, "K线不够，箱子还画不出来")

        bar = bars.iloc[-1]
        bar_id = str(bar.name)

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根日线已经判断过")

        formed = form_box(
            bars,
            lookback=self.lookback,
            peak_look=self.peak_look,
            min_gap=self.min_gap,
            atr_period=self.atr_period,
            height_atr=self.height_atr,
            adx_period=self.adx_period,
            adx_min=self.adx_min,
        )
        if formed is None:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                "还没有合格的箱子（要有两个分开的高点和低点，箱子不能太矮，ADX 不能太低）",
            )

        close = float(bar["close"])
        if self._need_inside.get(symbol):
            if formed.low <= close <= formed.high:
                self._need_inside[symbol] = False
                return StrategyDecision(
                    StrategyAction.HOLD,
                    symbol,
                    "上一笔已经走完，价格回到箱子里了，等下一次收盘突破",
                )
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                "上一笔走完了，先等价格回到箱子里，再找新的突破",
            )

        if self.vol_mult > 0 and "volume" in bars.columns:
            vol = float(bar.get("volume") or 0)
            hist_vol = bars["volume"].iloc[-(self.lookback + 1) : -1]
            avg_vol = float(hist_vol.mean()) if len(hist_vol) else 0.0
            if avg_vol > 0 and vol < avg_vol * self.vol_mult:
                return StrategyDecision(
                    StrategyAction.HOLD,
                    symbol,
                    f"{formed.reason}；突破这根量不够（{vol:.0f}<均量{avg_vol:.0f}×{self.vol_mult:g}）",
                )

        if close > formed.high:
            stop = formed.high - self.stop_atr * formed.atr
            return self._open(symbol, now, bar_id, close, "long", stop, formed)
        if close < formed.low:
            stop = formed.low + self.stop_atr * formed.atr
            return self._open(symbol, now, bar_id, close, "short", stop, formed)
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"{formed.reason}；收盘还在箱子里，等真正走出去",
        )

    def _open(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        entry: float,
        side: str,
        stop: float,
        formed: FormedBox,
    ) -> StrategyDecision:
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "箱沿止损距离异常，这笔不开")
        if side == "long":
            action = StrategyAction.BUY_OPEN
            text = (
                f"长线箱体做多 日线收盘{entry:.2f}站上箱子{formed.high:.2f}，"
                f"止损{stop:.2f}（箱顶外{self.stop_atr:g}倍ATR）"
            )
            signal = "swing_box_long"
        else:
            action = StrategyAction.SELL_OPEN
            text = (
                f"长线箱体做空 日线收盘{entry:.2f}跌破箱子{formed.low:.2f}，"
                f"止损{stop:.2f}（箱底外{self.stop_atr:g}倍ATR）"
            )
            signal = "swing_box_short"
        self._signaled_bar[symbol] = bar_id
        self._pending[symbol] = {
            "time": now,
            "stop": stop,
            "bar_id": bar_id,
            "box_high": formed.high,
            "box_low": formed.low,
            "box_height": formed.height,
        }
        logger.info("{} {}", symbol, text)
        return StrategyDecision(
            action,
            symbol,
            text,
            stop_price=stop,
            entry_price=entry,
            meta={
                "signal": signal,
                "bar_id": bar_id,
                "box_high": formed.high,
                "box_low": formed.low,
                "box_height": formed.height,
            },
        )

    def _is_entry_bar(self, symbol: str, bar_id: str) -> bool:
        entry_bar = self._last_entry_bar.get(symbol)
        if not entry_bar or not bar_id:
            return False
        try:
            return pd.Timestamp(bar_id) == pd.Timestamp(entry_bar)
        except (TypeError, ValueError):
            return str(bar_id) == str(entry_bar)

    def _manage(
        self,
        symbol: str,
        bars: pd.DataFrame,
        bar: pd.Series,
        now: datetime,
        bar_id: str,
        trail_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        pos = self.positions[symbol]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        box_high = float(pos.box_high or 0)
        box_low = float(pos.box_low or 0)
        height = float(pos.box_height or (box_high - box_low))

        def _close(reason: str) -> StrategyDecision:
            self._pending[symbol] = {"time": now, "bar_id": bar_id}
            self._signaled_bar[symbol] = bar_id
            return StrategyDecision(
                StrategyAction.CLOSE,
                symbol,
                reason,
                stop_price=pos.trail_stop or pos.initial_stop,
                entry_price=None,
            )

        if self.is_flatten_time(now):
            return _close("收盘清仓时间到，长线箱体强制平仓")
        if self._is_entry_bar(symbol, bar_id):
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"长线箱体持有 刚开仓 止损{pos.initial_stop:.2f}",
                stop_price=pos.initial_stop,
                entry_price=pos.entry_price,
            )

        # 假突破：收盘又走回箱子里面，逻辑失效，立刻走。只看收盘，盘中刺回来不算。
        if box_high > box_low:
            if pos.side == "long" and close <= box_high:
                return _close(f"收盘{close:.2f}回到箱子上沿{box_high:.2f}以内，突破失效")
            if pos.side == "short" and close >= box_low:
                return _close(f"收盘{close:.2f}回到箱子下沿{box_low:.2f}以内，突破失效")

        # 止损周期和信号周期不一样时，跟踪 ATR 用止损那根 K 线。
        atr_src = trail_bars if trail_bars is not None and len(trail_bars) >= self.atr_period + 1 else bars
        atr_series = average_true_range(
            atr_src["high"], atr_src["low"], atr_src["close"], period=self.atr_period
        )
        atr = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
        if atr != atr or atr <= 0:
            atr = abs(pos.entry_price - pos.initial_stop) / max(self.stop_atr, 0.1)

        # 浮盈够一个箱子高度才开始跟踪，避免趋势正常回撤就把止损推到成本上。
        if pos.side == "long":
            extreme = max(self._extreme.get(symbol, high), high)
            self._extreme[symbol] = extreme
            profit = close - pos.entry_price
            if height > 0 and profit >= self.trail_start_h * height:
                pos.trail_armed = True
            stop = float(pos.initial_stop)
            if pos.trail_armed:
                stop = max(stop, extreme - self.trail_atr * atr)
            pos.trail_stop = stop
        else:
            extreme = min(self._extreme.get(symbol, low), low)
            self._extreme[symbol] = extreme
            profit = pos.entry_price - close
            if height > 0 and profit >= self.trail_start_h * height:
                pos.trail_armed = True
            stop = float(pos.initial_stop)
            if pos.trail_armed:
                stop = min(stop, extreme + self.trail_atr * atr)
            pos.trail_stop = stop

        if stop_triggered(
            side=pos.side, stop=pos.trail_stop, close=close, high=high, low=low, mode=self.stop_mode
        ):
            kind = "跟踪止盈" if pos.trail_armed else "箱沿止损"
            return _close(f"长线箱体{kind} {pos.trail_stop:.2f}")

        stage = "已跟踪" if pos.trail_armed else "未满1箱高度，止损不动"
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"长线箱体持有 {pos.side} {stage} 止损{pos.trail_stop:.2f} 现价≈{close:.0f}",
            stop_price=pos.trail_stop,
            entry_price=pos.entry_price,
        )
