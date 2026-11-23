"""
趋势跟随（捕捉一整段行情）

研究里反复出现的信号，不是某一根针，而是「这个品种自己最近一直在往一个方向走」。
期货上，过去大约 1 到 12 个月的涨跌方向，后面往往还接着走（时间序列动量）。
海龟交易把这件事做成：收盘冲破前 N 根的最高价就做多，跌破前 N 根的最低价就做空，
再用大约 2 倍平均波动当保护，趋势没坏就继续拿。

这一套默认不按收盘钟点平仓，仓可以留过午休、隔夜和周末。另外两套策略仍按原来的钟点清仓。

1. 进场看较长的一段（默认 36 根，5 分钟上大约 3 小时）。收盘创出这段新高做多，创出新低做空。
   不看成交量。成交量过滤会把已经走出来的单边行情挡在外面。
2. 出场看较短的一段（默认 12 根）。多单收盘跌破这段低点、空单收盘升破这段高点，说明这段走完了。
3. 保护止损用平均真实波幅 ATR。开仓时放在 2 倍 ATR 外面。之后只往有利方向收紧：
   多单跟着持仓以来的最高价下移 2 倍 ATR，空单跟着最低价上移。止损不会放宽。
4. 默认不按 14:55、22:55 清仓，仓可以留过午休、隔夜和周末，靠止损和更短的那段高低点离场。
   这一套不跟配置页那个总开关走。总开关仍管日内实体、均线等。
   如果某次确实要到点平，把本策略的 flatten_at_close 写成 true。
5. 开仓这根自己的高低点不算已经打到止损。

实盘用哪一套，仍看配置里的 strategy.engine。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any

import pandas as pd
from loguru import logger

from .base import StrategyBase
from .market_state import MarketFilter, trend_params_for_section
from .session_scope import scope_allows, scope_from_cfg
from .slope import SlopeGate, load_slope_gate
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


class TrendFollowStrategy(StrategyBase):
    """收盘突破最近一段高低点就跟上，用更短的一段和 ATR 止损结束这段行情。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        self.cfg = cfg
        st = cfg.get("strategy", {})
        # 参数放在自己的小节里，不和别的策略混写。
        box = st.get("trend_follow", {}) or {}
        self.timeframe = str(st.get("timeframe", "5"))
        self.trail_timeframe = str(st.get("trail_timeframe", self.timeframe))
        trade = cfg.get("trade", {})
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        # 止损触发方式（全策略共用）。收盘确认时调度层不会把未收线那根拼进来。
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH

        # 进场通道要长一些，才是「一段行情」，不是刚晃一下。
        self.entry_bars = max(5, int(box.get("entry_bars", 36)))
        # 出场通道更短。价格收回来，说明原来那段已经断了。
        self.exit_bars = max(2, int(box.get("exit_bars", 12)))
        if self.exit_bars >= self.entry_bars:
            self.exit_bars = max(2, self.entry_bars // 2)
        # ATR 用多少根已经收盘的 K 线估计平时波动。
        self.atr_period = max(2, int(box.get("atr_period", 14)))
        # 止损放在几倍波动外面。2 是海龟那套常用的距离。
        self.atr_mult = float(box.get("atr_mult", 2.0))
        if self.atr_mult <= 0:
            self.atr_mult = 2.0
        # 海龟自己的趋势过滤：假突破多发生在没趋势、波动缩着的时候。
        self.market_filter = MarketFilter(trend_params_for_section(cfg, "trend_follow"))

        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        # 可选斜率环境门：默认关闭，开启后只做均线斜率同向的突破。
        self.slope_gate = SlopeGate(load_slope_gate(box))

        # 不读 strategy.flatten_at_close。那个总开关管日内实体/均线。
        # 这里默认关掉：到点不平仓，才能把一段行情拿过收盘。
        self.flatten_at_close = config_flag(box.get("flatten_at_close", False), default=False)
        self.entry_sessions = session_tuples(cfg, live)
        self.session_scope = scope_from_cfg(cfg)

        self.positions: dict[str, PositionState] = {s: PositionState(symbol=s) for s in self.symbols}
        self._pending: dict[str, dict[str, Any]] = {}
        # 持仓以来顺着走得最远的价格。多单记最高价，空单记最低价，用来收紧止损。
        self._extreme: dict[str, float] = {}
        # 同一根 K 线实盘会反复扫描，突破信号只报一次（状态由公共基类保存）。

    def is_flatten_time(self, now: datetime | None = None) -> bool:
        """本策略自己的收盘清仓窗口。默认关着，所以到点也不会平。"""
        if not self.flatten_at_close:
            return False
        from src.utils.session_clock import in_flatten_window

        return in_flatten_window(self.flatten_times, now or datetime.now())

    def in_entry_session(self, now: datetime, symbol: str | None = None) -> bool:
        """
        分钟线才看钟点。

        日线、周线在数据库里的时间是 00:00，那不是「半夜不许开仓」，
        只是这一根代表整天或整周。这种周期不拿交易时段去拦。
        """
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
        """成交回报。只平掉一部分时，剩下的仓继续用已经收紧过的止损。"""
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        pending = self._pending.pop(symbol, None) or {}
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            pos.side = "long" if action == StrategyAction.BUY_OPEN else "short"
            pos.volume = volume
            pos.entry_price = price
            pos.initial_stop = float(initial_stop or pending.get("stop") or price)
            pos.trail_stop = pos.initial_stop
            pos.trail_armed = True
            when = pending.get("time") or datetime.now()
            pos.opened_at = when.isoformat(timespec="seconds")
            opened_bar = str(pending.get("bar_id") or "")
            if opened_bar:
                self._last_entry_bar[symbol] = opened_bar
            extreme = pending.get("extreme")
            self._extreme[symbol] = float(extreme if extreme is not None else price)
        elif action == StrategyAction.CLOSE:
            if not pos.reduce_close(volume):
                if pending:
                    self._pending[symbol] = pending
                return
            self.positions[symbol] = PositionState(symbol=symbol)
            self._extreme.pop(symbol, None)

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        """只看信号周期上已经收盘的 K 线。通道用前面的 K 线，不用当前这根自己。"""
        _ = trail_bars
        now = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        need = max(self.entry_bars, self.exit_bars, self.atr_period) + 2
        if self.market_filter.enabled:
            need = max(need, self.market_filter.params.need_bars)
        if self.slope_gate.enabled:
            need = max(need, self.slope_gate.need_bars)
        if bars is None or len(bars) < need:
            return StrategyDecision(StrategyAction.HOLD, symbol, "K线不足，趋势通道还算不出来")

        bar = bars.iloc[-1]
        bar_id = str(bar.name)
        if pos.side != "flat":
            return self._manage(symbol, bars, bar, now, bar_id)

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不再开新仓")
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "非交易时段")
        if self._signaled_bar.get(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经看过趋势突破")

        # 文档第 1 条：行情不匹配直接停手。区间震荡、突破后快速跌回通道时不开新仓。
        state = self.market_filter.check(bars)
        if not state.ok:
            return StrategyDecision(
                StrategyAction.HOLD, symbol, f"行情不匹配：{state.reason}"
            )

        atr = self._atr(bars)
        if atr is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "波动还算不出来")
        # 通道不含当前这根。当前收盘才能拿来判断有没有冲出去。
        past = bars.iloc[:-1]
        entry_high = float(past["high"].iloc[-self.entry_bars :].max())
        entry_low = float(past["low"].iloc[-self.entry_bars :].min())
        close = float(bar["close"])
        if close > entry_high:
            allowed, why = self.slope_gate.check(bars, "long", symbol=symbol)
            if not allowed:
                return StrategyDecision(StrategyAction.HOLD, symbol, why)
            stop = close - self.atr_mult * atr
            self._signaled_bar[symbol] = bar_id
            # 极值从进场价起算，不用这根突破 K 线自己的影线，避免一开仓止损就被拉到贴着最高价。
            return self._open(symbol, now, bar_id, close, "long", stop, close, entry_high)
        if close < entry_low:
            allowed, why = self.slope_gate.check(bars, "short", symbol=symbol)
            if not allowed:
                return StrategyDecision(StrategyAction.HOLD, symbol, why)
            stop = close + self.atr_mult * atr
            self._signaled_bar[symbol] = bar_id
            return self._open(symbol, now, bar_id, close, "short", stop, close, entry_low)
        return StrategyDecision(StrategyAction.HOLD, symbol, "收盘还在最近这段高低点里面")

    def _atr(self, bars: pd.DataFrame) -> float | None:
        """
        平均真实波幅。

        每根的波动取三个数里最大的：最高减最低、最高离上一根收盘、最低离上一根收盘。
        再对最近 atr_period 根取平均。这样跳空也算进波动，不会把止损放得太近。
        """
        if len(bars) < self.atr_period + 1:
            return None
        high = bars["high"].astype(float)
        low = bars["low"].astype(float)
        close = bars["close"].astype(float)
        prev_close = close.shift(1)
        true_range = pd.concat(
            [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        value = float(true_range.iloc[-self.atr_period :].mean())
        if pd.isna(value) or value <= 0:
            return None
        return value

    def _open(
        self,
        symbol: str,
        now: datetime,
        bar_id: str,
        entry: float,
        side: str,
        stop: float,
        extreme: float,
        broken: float,
    ) -> StrategyDecision:
        """顺着突破方向开仓。止损放在进场价外面若干倍波动的位置。"""
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "趋势止损距离异常")
        if side == "long":
            action = StrategyAction.BUY_OPEN
            text = f"趋势跟随做多 收盘升破前{self.entry_bars}根高点{broken:.2f}，止损{stop:.2f}"
            signal = "trend_follow_long"
        else:
            action = StrategyAction.SELL_OPEN
            text = f"趋势跟随做空 收盘跌破前{self.entry_bars}根低点{broken:.2f}，止损{stop:.2f}"
            signal = "trend_follow_short"
        self._pending[symbol] = {
            "time": now,
            "stop": stop,
            "bar_id": bar_id,
            "extreme": extreme,
        }
        logger.info("{} {}", symbol, text)
        return StrategyDecision(
            action,
            symbol,
            text,
            stop_price=stop,
            entry_price=entry,
            meta={"signal": signal, "bar_id": bar_id},
        )

    def _is_entry_bar(self, symbol: str, bar_id: str) -> bool:
        """开仓这根的高低点是进场前就印出来的，不能拿来当已经打到止损。"""
        entry_bar = self._last_entry_bar.get(symbol)
        if not entry_bar or not bar_id:
            return False
        try:
            return pd.Timestamp(bar_id) == pd.Timestamp(entry_bar)
        except (TypeError, ValueError):
            return str(bar_id) == str(entry_bar)

    def _ratchet(self, symbol: str, pos: PositionState, bar: pd.Series, atr: float) -> float:
        """
        止损只往有利的方向移。

        多单记住持仓以来的最高价，止损放在最高价下面若干倍波动。
        空单记住最低价，止损放在最低价上面。新算出来的如果更松，就仍用原来的。
        """
        stop = float(pos.trail_stop or pos.initial_stop)
        if pos.side == "long":
            extreme = max(self._extreme.get(symbol, float(bar["high"])), float(bar["high"]))
            self._extreme[symbol] = extreme
            tighter = extreme - self.atr_mult * atr
            stop = max(stop, tighter)
        else:
            extreme = min(self._extreme.get(symbol, float(bar["low"])), float(bar["low"]))
            self._extreme[symbol] = extreme
            tighter = extreme + self.atr_mult * atr
            stop = min(stop, tighter)
        pos.trail_stop = stop
        return stop

    def _manage(
        self,
        symbol: str,
        bars: pd.DataFrame,
        bar: pd.Series,
        now: datetime,
        bar_id: str,
    ) -> StrategyDecision:
        """有仓时先看收盘清仓，再看短通道是不是已经走坏，最后看只收紧的波动止损。"""
        pos = self.positions[symbol]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        def _close(reason: str, *, stop_price: float | None = None) -> StrategyDecision:
            self._pending[symbol] = {"time": now, "bar_id": bar_id}
            self._signaled_bar[symbol] = bar_id
            return StrategyDecision(
                StrategyAction.CLOSE,
                symbol,
                reason,
                stop_price=stop_price if stop_price is not None else (pos.trail_stop or pos.initial_stop),
                entry_price=None,
            )

        if self.is_flatten_time(now):
            return _close("收盘清仓时间到，趋势跟随强制平仓")

        atr = self._atr(bars)
        if atr is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "波动暂时算不出来，先持有")

        # 开仓这根先拿住。它自己的最高最低是进场前就有的，不能拿来收紧止损。
        if self._is_entry_bar(symbol, bar_id):
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"趋势跟随持有 side={pos.side} 止损={pos.initial_stop:.2f}",
                stop_price=pos.initial_stop,
                entry_price=pos.entry_price,
            )

        past = bars.iloc[:-1]
        exit_high = float(past["high"].iloc[-self.exit_bars :].max())
        exit_low = float(past["low"].iloc[-self.exit_bars :].min())
        if pos.side == "long" and close < exit_low:
            return _close(f"趋势跟随平多 收盘跌破前{self.exit_bars}根低点{exit_low:.2f}")
        if pos.side == "short" and close > exit_high:
            return _close(f"趋势跟随平空 收盘升破前{self.exit_bars}根高点{exit_high:.2f}")

        prev_stop = float(pos.trail_stop or pos.initial_stop or 0.0)
        stop = self._ratchet(symbol, pos, bar, atr)
        # 本根新收紧的止损下一根才用；止损判定和成交都用上一根已知的线。
        judge_stop = prev_stop or stop
        if stop_triggered(
            side=pos.side, stop=judge_stop, close=close, high=high, low=low, mode=self.stop_mode
        ):
            text = "跌破波动止损" if pos.side == "long" else "升破波动止损"
            return _close(f"趋势跟随止损 {text} {judge_stop:.2f}", stop_price=judge_stop)

        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"趋势跟随持有 side={pos.side} 止损={stop:.2f} 现价≈{close:.0f}",
            stop_price=stop,
            entry_price=pos.entry_price,
        )
