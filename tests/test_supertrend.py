"""SuperTrend：通道翻转开仓，收盘打穿线离场。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from src.strategy.indicators import supertrend
from src.strategy.supertrend import SuperTrendStrategy
from src.strategy.types import PositionState, StrategyAction


def _cfg() -> dict:
    return {
        "strategy": {
            "engine": "supertrend",
            "timeframe": "D",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "supertrend": {
                "atr_period": 5,
                "multiplier": 2.0,
                "reverse_on_flip": True,
                "flatten_at_close": False,
            },
        },
        "trade": {"symbol": "bu2612", "volume": 1},
        "schedule": {"trading_sessions": []},
    }


def _trend_up_then_down() -> pd.DataFrame:
    """先单边上行再拐头下行，方便看到翻转。"""
    rows = []
    t0 = datetime(2026, 1, 5)
    price = 3000.0
    for i in range(30):
        o = price
        c = price + 15
        h = c + 5
        low = o - 3
        rows.append((t0 + pd.Timedelta(days=i), o, h, low, c, 1000.0))
        price = c
    for i in range(20):
        o = price
        c = price - 18
        h = o + 3
        low = c - 5
        rows.append((t0 + pd.Timedelta(days=30 + i), o, h, low, c, 1000.0))
        price = c
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")


def test_factory_loads_supertrend() -> None:
    from src.strategy import create_strategy

    assert type(create_strategy(_cfg())).__name__ == "SuperTrendStrategy"


def test_indicator_has_direction() -> None:
    df = _trend_up_then_down()
    line, direction = supertrend(df["high"], df["low"], df["close"], period=5, multiplier=2.0)
    assert len(line) == len(df)
    assert set(int(x) for x in direction.dropna().unique()).issubset({-1, 1})


def test_opens_on_flip_and_can_hold() -> None:
    """扫完整段：至少出现过开仓；持仓时止损价跟着 ST 线。"""
    strat = SuperTrendStrategy(_cfg())
    df = _trend_up_then_down()
    saw_open = False
    for i in range(10, len(df)):
        part = df.iloc[: i + 1]
        d = strat.evaluate("bu2612", part, now=part.index[-1].to_pydatetime())
        if d.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            saw_open = True
            assert d.stop_price is not None
            strat.on_filled("bu2612", d.action, float(d.entry_price or 0), 1, d.stop_price)
        elif d.action == StrategyAction.CLOSE:
            strat.on_filled("bu2612", d.action, float(part["close"].iloc[-1]), 1)
    assert saw_open


def test_trail_timeframe_follows_public_stop_period() -> None:
    """止损周期跟左边公共项，不再抄成信号周期。"""
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "15"
    cfg["strategy"]["trail_timeframe"] = "5"
    s = SuperTrendStrategy(cfg)
    assert s.timeframe == "15"
    assert s.trail_timeframe == "5"


def test_close_decision_accepts_explicit_stop_price() -> None:
    """翻转/止损离场会带上当根 ST 线；以前少这个参数会直接抛 TypeError。"""
    strat = SuperTrendStrategy(_cfg())
    pos = PositionState(symbol="bu2612")
    pos.side = "long"
    pos.entry_price = 3000.0
    pos.initial_stop = 2950.0
    decision = strat._close_decision(
        "bu2612", pos, datetime(2026, 1, 5, 15, 0), "2026-01-05", "测试离场", stop_price=2960.0
    )
    assert decision.action == StrategyAction.CLOSE
    assert decision.stop_price == 2960.0
