"""触发即开仓：trigger 模式用未收线 K 线的最高/最低判触发。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.backtest.engine import _trigger_entry_price
from src.scheduler.runner import TradingBot
from src.strategy.bollinger_break import BollingerBreakStrategy
from src.strategy.daily_body import DailyBodyStrategy
from src.strategy.dual_donchian import DualDonchianStrategy
from src.strategy.dual_ma import DualMaStrategy
from src.strategy.kelly_break import KellyBreakStrategy
from src.strategy.sar_trend import SarTrendStrategy
from src.strategy.supertrend import SuperTrendStrategy
from src.strategy.swing_box import SwingBoxStrategy, form_box
from src.strategy.trend_follow import TrendFollowStrategy
from src.strategy.types import (
    StrategyAction,
    entry_touched,
    forming_price_range,
    pick_breakout_side,
)

SYMBOL = "bu2612"
NOW = datetime(2026, 10, 20, 21, 45)


def _forming(high: float, low: float, close: float | None = None) -> pd.DataFrame:
    """未收线的一根；high/low 是盘中已经走出来的极值。"""
    price = close if close is not None else (high + low) / 2.0
    return pd.DataFrame(
        {"open": [price], "high": [high], "low": [low], "close": [price], "volume": [100.0]},
        index=[pd.Timestamp("2026-10-20 21:45")],
    )


def _frame(rows: list[tuple[float, float, float]], freq: str = "5min") -> pd.DataFrame:
    """每行是最高、最低、收盘。"""
    index = pd.date_range("2026-10-20 21:00", periods=len(rows), freq=freq)
    frame = pd.DataFrame(rows, columns=["high", "low", "close"], index=index)
    frame["open"] = frame["close"]
    frame["volume"] = 100
    return frame


def _quiet(n: int, close: float = 100.0) -> list[tuple[float, float, float]]:
    return [(close + 1, close - 1, close)] * n


# ---------------------------------------------------------------- 公共工具


def test_entry_helpers() -> None:
    assert forming_price_range(_forming(105, 99)) == (105, 99)
    assert forming_price_range(None) is None
    assert forming_price_range(pd.DataFrame()) is None

    assert entry_touched(side="long", trigger=100, high=100, low=98) is True
    assert entry_touched(side="long", trigger=100, high=99.9, low=98) is False
    assert entry_touched(side="short", trigger=100, high=102, low=100) is True
    assert entry_touched(side="short", trigger=100, high=102, low=100.1) is False
    assert entry_touched(side="long", trigger=None, high=200, low=0) is False
    assert entry_touched(side="long", trigger=0, high=200, low=0) is False


def test_pick_breakout_side() -> None:
    assert pick_breakout_side(close=100, high_level=101, low_level=99, high=102, low=99.5) == "long"
    assert pick_breakout_side(close=100, high_level=101, low_level=99, high=100.5, low=98) == "short"
    # 同时碰到上下沿：最新价还在上沿之外做多，夹在中间放弃。
    assert pick_breakout_side(close=103, high_level=101, low_level=99, high=103, low=98) == "long"
    assert pick_breakout_side(close=100, high_level=101, low_level=99, high=103, low=98) is None


def test_trigger_entry_price_handles_gap() -> None:
    bar = pd.Series({"open": 108.0, "high": 115.0, "low": 107.0})
    assert _trigger_entry_price(110.0, bar) == 110.0
    # 触发价在当根区间之外（跳空直接穿过）时按开盘价。
    assert _trigger_entry_price(105.0, bar) == 108.0


# ---------------------------------------------------------------- 趋势跟随


def _trend_cfg(**box: object) -> dict:
    section = {"entry_bars": 5, "exit_bars": 3, "atr_period": 3, "atr_mult": 2.0}
    section.update(box)
    return {
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "5",
            "symbols": [SYMBOL],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "trend_follow": section,
        },
        "trade": {"volume": 1, "symbol": SYMBOL},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def test_trend_follow_trigger_uses_forming_high() -> None:
    bars = _frame(_quiet(9))
    strategy = TrendFollowStrategy(_trend_cfg())
    decision = strategy.evaluate(
        SYMBOL, bars, now=NOW, forming=_forming(105, 99, 104)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.trigger_price == 101.0
    assert decision.entry_price == 101.0
    assert decision.meta["intraday_entry"] is True


def test_trend_follow_close_mode_unchanged() -> None:
    bars = _frame(_quiet(9))
    strategy = TrendFollowStrategy(_trend_cfg())
    decision = strategy.evaluate(SYMBOL, bars, now=NOW)
    assert decision.action == StrategyAction.HOLD


# ---------------------------------------------------------------- 布林 / 凯利


def _simple_cfg(engine: str, **box: object) -> dict:
    return {
        "strategy": {
            "engine": engine,
            "timeframe": "5",
            "symbols": [SYMBOL],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": False,
            "market_filter": {"enabled": False},
            engine: box,
        },
        "trade": {"volume": 1, "symbol": SYMBOL},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def test_bollinger_trigger_uses_upper_band() -> None:
    closes = [99.0 if i % 2 == 0 else 101.0 for i in range(30)]
    bars = _frame([(c + 1, c - 1, c) for c in closes])
    strategy = BollingerBreakStrategy(_simple_cfg("bollinger"))
    decision = strategy.evaluate(SYMBOL, bars, now=NOW, forming=_forming(112, 99, 111))
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.trigger_price is not None
    assert decision.entry_price == decision.trigger_price


def test_bollinger_trigger_not_touched_holds() -> None:
    closes = [99.0 if i % 2 == 0 else 101.0 for i in range(30)]
    bars = _frame([(c + 1, c - 1, c) for c in closes])
    strategy = BollingerBreakStrategy(_simple_cfg("bollinger"))
    decision = strategy.evaluate(SYMBOL, bars, now=NOW, forming=_forming(100.2, 99.8, 100))
    assert decision.action == StrategyAction.HOLD


def test_kelly_trigger_uses_prior_high() -> None:
    rows = [(101, 99, 100)] * 20
    bars = _frame(rows)
    strategy = KellyBreakStrategy(_simple_cfg("kelly_break", lookback=5))
    decision = strategy.evaluate(SYMBOL, bars, now=NOW, forming=_forming(105, 99.5, 104))
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.trigger_price == 101.0


# ---------------------------------------------------------------- SuperTrend / SAR


def _downtrend(n: int = 24) -> pd.DataFrame:
    closes = [110 - i * 0.5 for i in range(n)]
    return _frame([(c + 0.6, c - 0.6, c) for c in closes])


def test_supertrend_trigger_crosses_line() -> None:
    from src.strategy.indicators import supertrend as calc

    bars = _downtrend()
    line, direction = calc(bars["high"], bars["low"], bars["close"], period=3, multiplier=3.0)
    assert int(direction.iloc[-1]) < 0
    st_line = float(line.iloc[-1])
    strategy = SuperTrendStrategy(
        _simple_cfg("supertrend", atr_period=3, multiplier=3.0)
    )
    decision = strategy.evaluate(
        SYMBOL, bars, now=NOW, forming=_forming(st_line + 2, st_line - 1, st_line + 1)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.trigger_price == st_line


def test_sar_trigger_crosses_sar_and_ma() -> None:
    bars = _downtrend()
    strategy = SarTrendStrategy(_simple_cfg("sar_trend", ma_filter=5))
    ind = strategy.prepare(bars)
    assert ind is not None
    assert ind["bull"] is False
    trigger = max(float(ind["sar"]), float(ind["ma"]))
    decision = strategy.evaluate(
        SYMBOL, bars, now=NOW, forming=_forming(trigger + 2, trigger - 1, trigger + 1)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.trigger_price == trigger


# ---------------------------------------------------------------- 长线箱体 / 双周期通道


def _swing_bars() -> pd.DataFrame:
    start = datetime(2024, 1, 2)
    rows: list[tuple[float, float, float, float]] = []
    for i in range(50):
        cycle = i % 8
        if cycle == 3:
            rows.append((110, 120, 108, 112))
        elif cycle == 7:
            rows.append((90, 92, 80, 88))
        else:
            rows.append((100, 105, 95, 100))
    rows.append((100, 105, 95, 100))
    idx = [start + timedelta(days=i) for i in range(len(rows))]
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [1000] * len(rows),
        },
        index=pd.DatetimeIndex(idx),
    )


def test_swing_box_trigger_uses_box_edge() -> None:
    bars = _swing_bars()
    formed = form_box(bars, lookback=40, peak_look=2, min_gap=0, atr_period=14,
                      height_atr=0, adx_period=14, adx_min=0)
    assert formed is not None
    strategy = SwingBoxStrategy(
        _simple_cfg("swing_box", adx_min=0, height_atr=0, lookback=40, vol_mult=0)
    )
    forming = pd.DataFrame(
        {
            "open": [formed.high + 1],
            "high": [formed.high + 5],
            "low": [formed.high - 1],
            "close": [formed.high + 4],
            "volume": [1000],
        },
        index=[pd.Timestamp("2024-03-01")],
    )
    decision = strategy.evaluate(SYMBOL, bars, forming=forming)
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.trigger_price == formed.high


def test_dual_donchian_trigger_uses_channel() -> None:
    # 大周期明确向上：最后一根收盘高于前面高点，ADX 也够长。
    trend_closes = [60 + i for i in range(60)]
    trend_idx = pd.date_range("2026-01-01", periods=len(trend_closes), freq="D")
    trend = pd.DataFrame(
        {
            "open": trend_closes,
            "high": [c + 0.5 for c in trend_closes],
            "low": [c - 0.5 for c in trend_closes],
            "close": trend_closes,
            "volume": [100] * len(trend_closes),
        },
        index=trend_idx,
    )
    bars = _frame(_quiet(10))
    cfg = _simple_cfg(
        "dual_donchian",
        entry_bars=5,
        exit_bars=3,
        atr_period=3,
        adx_min=0,
        htf_donchian=5,
    )
    strategy = DualDonchianStrategy(cfg)
    decision = strategy.evaluate(
        SYMBOL, bars, now=NOW, trend_bars=trend, forming=_forming(110, 99.5, 109)
    )
    assert decision.action == StrategyAction.BUY_OPEN, decision.reason
    assert decision.trigger_price == 101.0


# ---------------------------------------------------------------- 日内实体


def _daily_body_cfg() -> dict:
    return {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": [SYMBOL],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "market_filter": {"enabled": False},
            "daily_body": {
                "eth_minutes": 20,
                "regime_filter": False,
                "skip_gap_atr_mult": 0,
                "max_box_atr_mult": 0,
                "max_chase_atr_mult": 0,
                "filter_atr_period": 4,
            },
        },
        "trade": {"symbol": SYMBOL, "volume": 1},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def _daily_body_bars(end: str, close: float = 100.0) -> pd.DataFrame:
    end_ts = pd.Timestamp(end)
    index = pd.date_range(end_ts.normalize() + pd.Timedelta(hours=7), end_ts, freq="5min")
    opened = end_ts.normalize() + pd.Timedelta(hours=9)
    eth_end = opened + pd.Timedelta(minutes=20)
    rows = []
    for stamp in index:
        if opened < stamp <= eth_end:
            rows.append((100.0, 101.0, 99.0, 100.0))
        else:
            rows.append((100.0, 100.4, 99.6, 100.0))
    rows[-1] = (close, max(100.4, close + 0.2), min(99.6, close - 0.2), close)
    return pd.DataFrame(
        {"open": [r[0] for r in rows], "high": [r[1] for r in rows],
         "low": [r[2] for r in rows], "close": [r[3] for r in rows],
         "volume": [100.0] * len(rows)},
        index=index,
    )


def test_daily_body_trigger_after_eth() -> None:
    bars = _daily_body_bars("2026-10-23 09:45", 100.0)
    forming = pd.DataFrame(
        {"open": [101.0], "high": [105.0], "low": [100.0], "close": [104.0], "volume": [100.0]},
        index=[pd.Timestamp("2026-10-23 09:50")],
    )
    decision = DailyBodyStrategy(_daily_body_cfg()).evaluate(
        SYMBOL, bars, now=datetime(2026, 10, 23, 9, 50), forming=forming
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.trigger_price == 101.0


def test_daily_body_trigger_waits_for_eth() -> None:
    bars = _daily_body_bars("2026-10-23 09:10", 100.0)
    forming = pd.DataFrame(
        {"open": [101.0], "high": [105.0], "low": [100.0], "close": [104.0], "volume": [100.0]},
        index=[pd.Timestamp("2026-10-23 09:15")],
    )
    decision = DailyBodyStrategy(_daily_body_cfg()).evaluate(
        SYMBOL, bars, now=datetime(2026, 10, 23, 9, 15), forming=forming
    )
    assert decision.action == StrategyAction.HOLD
    assert "ETH" in decision.reason


def test_daily_body_trigger_no_duplicate_on_same_forming() -> None:
    """同一根 forming 反复扫描：第一次触发后，第二次必须 HOLD。"""
    bars = _daily_body_bars("2026-10-23 09:45", 100.0)
    forming = pd.DataFrame(
        {"open": [101.0], "high": [105.0], "low": [100.0], "close": [104.0], "volume": [100.0]},
        index=[pd.Timestamp("2026-10-23 09:50")],
    )
    strategy = DailyBodyStrategy(_daily_body_cfg())
    first = strategy.evaluate(
        SYMBOL, bars, now=datetime(2026, 10, 23, 9, 50), forming=forming
    )
    assert first.action == StrategyAction.BUY_OPEN
    assert first.meta.get("intraday_entry") is True
    # 同一根 forming 再次扫描（订单可能还没成交），不能再次触发。
    second = strategy.evaluate(
        SYMBOL, bars, now=datetime(2026, 10, 23, 9, 50), forming=forming
    )
    assert second.action == StrategyAction.HOLD
    assert "已经判断过" in second.reason


# ---------------------------------------------------------------- 回退与 runner


def test_dual_ma_has_no_forming_parameter() -> None:
    """没有自然触发价的策略不接受 forming，runner 自然回退收盘确认。"""
    import inspect

    params = inspect.signature(DualMaStrategy.evaluate).parameters
    assert "forming" not in params


class _FakeBot:
    """只借用 TradingBot 的两个方法，不启动交易循环。"""

    _evaluate_accepts = TradingBot._evaluate_accepts
    _evaluate_symbol = TradingBot._evaluate_symbol

    def __init__(self, strategy) -> None:
        self.strategy = strategy

    def _strategy_for(self, symbol: str):
        return self.strategy


def test_runner_passes_forming_only_when_strategy_accepts() -> None:
    bars = _frame(_quiet(9))
    bot = _FakeBot(TrendFollowStrategy(_trend_cfg()))
    decision = bot._evaluate_symbol(
        SYMBOL, bars, now=NOW, forming=_forming(105, 99, 104)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.meta["intraday_entry"] is True

    # 双均线不接受 forming：传了也不会进 kwargs，照旧按收盘评估。
    long_bars = _frame(_quiet(70))
    dual = _FakeBot(DualMaStrategy(_simple_cfg("dual_ma")))
    assert dual._evaluate_accepts("forming", dual.strategy) is False
    decision = dual._evaluate_symbol(SYMBOL, long_bars, now=NOW, forming=_forming(105, 99, 104))
    assert decision.action == StrategyAction.HOLD
