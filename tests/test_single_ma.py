"""
单均线系统（Single_ma）：收盘穿越 SMA 开仓；穿回则平或反手。
"""

from datetime import datetime

import pandas as pd

from src.strategy.single_ma import SingleMaStrategy
from src.strategy.types import StrategyAction


def _cfg(**box) -> dict:
    return {
        "strategy": {
            "engine": "Single_ma",
            "timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "single_ma": dict(box),
        },
        "trade": {"symbol": "bu2612", "volume": 1},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def _climb_then_cross_up(end: str) -> pd.DataFrame:
    index = pd.date_range(end=end, periods=30, freq="15min")
    closes = [100.0] * 29 + [108.0]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    highs[-1] = 109.0
    lows[-1] = 107.0
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0] * 30,
        },
        index=index,
    )


def _drop_then_cross_down(end: str) -> pd.DataFrame:
    index = pd.date_range(end=end, periods=30, freq="15min")
    closes = [110.0] * 29 + [100.0]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    highs[-1] = 101.0
    lows[-1] = 99.0
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0] * 30,
        },
        index=index,
    )


def test_cross_above_opens_long() -> None:
    bars = _climb_then_cross_up("2026-10-23 10:00")
    decision = SingleMaStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 10, 0)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert "穿越" in decision.reason
    assert decision.stop_price is not None
    assert decision.stop_price < decision.entry_price


def test_cross_below_opens_short() -> None:
    bars = _drop_then_cross_down("2026-10-23 10:00")
    decision = SingleMaStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 10, 0)
    )
    assert decision.action == StrategyAction.SELL_OPEN
    assert "穿越" in decision.reason
    assert decision.stop_price > decision.entry_price


def test_no_cross_holds() -> None:
    index = pd.date_range(end="2026-10-23 10:00", periods=30, freq="15min")
    closes = [100 + i * 0.1 for i in range(30)]
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [100.0] * 30,
        },
        index=index,
    )
    decision = SingleMaStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 10, 0)
    )
    assert decision.action == StrategyAction.HOLD
    assert "等穿越" in decision.reason


def test_opposite_cross_closes_or_reverses() -> None:
    strategy = SingleMaStrategy(_cfg(reverse_on_cross=True))
    strategy.positions["bu2612"].side = "long"
    strategy.positions["bu2612"].entry_price = 105.0
    strategy.positions["bu2612"].volume = 1
    strategy.positions["bu2612"].initial_stop = 100.0
    strategy.positions["bu2612"].trail_stop = 100.0
    bars = _drop_then_cross_down("2026-10-23 11:00")
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 11, 0))
    assert decision.action == StrategyAction.SELL_OPEN
    assert decision.meta.get("reverse") is True


def test_create_via_factory() -> None:
    from src.strategy.factory import create_strategy

    assert isinstance(create_strategy(_cfg()), SingleMaStrategy)
    for name in ("Single_ma", "single_ma"):
        cfg = _cfg()
        cfg["strategy"]["engine"] = name
        assert isinstance(create_strategy(cfg), SingleMaStrategy)


def test_ma_period_is_configurable() -> None:
    assert SingleMaStrategy(_cfg()).ma_period == 20
    assert SingleMaStrategy(_cfg(ma_period=60)).ma_period == 60
    assert SingleMaStrategy(_cfg(ma_period=120)).ma_period == 120


def test_flatten_follows_global_switch() -> None:
    on = _cfg()
    on["strategy"]["flatten_at_close"] = True
    on["strategy"]["single_ma"] = {"flatten_at_close": False}
    s_on = SingleMaStrategy(on)
    assert s_on.flatten_at_close is True
    assert s_on.is_flatten_time(datetime(2026, 10, 23, 14, 55))

    off = _cfg()
    off["strategy"]["flatten_at_close"] = False
    off["strategy"]["single_ma"] = {"flatten_at_close": True}
    s_off = SingleMaStrategy(off)
    assert s_off.flatten_at_close is False
    assert s_off.is_flatten_time(datetime(2026, 10, 23, 14, 55)) is False
