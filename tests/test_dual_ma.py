"""双均线：短线上穿长线开多，下穿开空。"""

from datetime import datetime

import pandas as pd

from src.strategy.dual_ma import DualMaStrategy
from src.strategy.types import StrategyAction


def _cfg(short: int = 3, long: int = 5, **box) -> dict:
    return {
        "strategy": {
            "engine": "dual_ma",
            "timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "dual_ma": {"short_period": short, "long_period": long, **box},
        },
        "trade": {"symbol": "bu2612", "volume": 1},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
    }


def _frame(closes: list[float], end: str) -> pd.DataFrame:
    index = pd.date_range(end=end, periods=len(closes), freq="15min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


def test_golden_cross_opens_long() -> None:
    """
    短=3、长=5：前段短线在长线下方，最后一根急拉让短线上穿。
    手工验算最后两根的均线关系后，应开多。
    """
    # 10 根：前面偏低，最后一根跳高
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 94, 110]
    bars = _frame(closes, "2026-10-23 14:00")
    decision = DualMaStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 14, 0)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert "金叉" in decision.reason or "做多" in decision.reason


def test_death_cross_opens_short() -> None:
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 106, 90]
    bars = _frame(closes, "2026-10-23 14:00")
    decision = DualMaStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 14, 0)
    )
    assert decision.action == StrategyAction.SELL_OPEN
    assert "死叉" in decision.reason or "做空" in decision.reason


def test_swaps_when_short_not_less_than_long() -> None:
    s = DualMaStrategy(_cfg(short=80, long=20))
    assert s.short_period < s.long_period


def test_create_via_factory() -> None:
    from src.strategy.factory import create_strategy

    assert isinstance(create_strategy(_cfg()), DualMaStrategy)
    cfg = _cfg()
    cfg["strategy"]["engine"] = "ma_cross"
    assert isinstance(create_strategy(cfg), DualMaStrategy)


def test_flatten_follows_global_switch() -> None:
    on = _cfg()
    on["strategy"]["flatten_at_close"] = True
    assert DualMaStrategy(on).is_flatten_time(datetime(2026, 10, 23, 14, 55))
    off = _cfg()
    off["strategy"]["flatten_at_close"] = False
    assert DualMaStrategy(off).is_flatten_time(datetime(2026, 10, 23, 14, 55)) is False
