"""价格均线统一用指数均线（EMA），和同花顺 EXPMA 同一类。"""

from datetime import datetime

import pandas as pd

from src.research.factors import factor_ma_gap
from src.strategy.dual_ma import DualMaStrategy
from src.strategy.indicators import ema_series
from src.strategy.types import StrategyAction


def test_ema_series_matches_expma() -> None:
    """span=N、不回溯加权，和常见软件里的 N 根 EXPMA 一致。"""
    close = pd.Series([10.0, 11.0, 12.0, 8.0, 9.0, 7.0])
    got = ema_series(close, 4)
    expect = close.ewm(span=4, adjust=False).mean()
    assert list(got.round(12)) == list(expect.round(12))


def test_ema_reacts_faster_than_sma_after_drop() -> None:
    """急跌后 EMA 比简单均线更靠近现价。"""
    close = pd.Series([100.0] * 20 + [80.0])
    ema = float(ema_series(close, 20).iloc[-1])
    sma = float(close.rolling(20).mean().iloc[-1])
    last = float(close.iloc[-1])
    assert abs(last - ema) < abs(last - sma)


def test_ma_gap_waits_for_twenty_bars() -> None:
    """扫描因子前 19 根仍空着，避免开头乱出偏离。"""
    close = pd.Series([float(i) for i in range(1, 25)])
    df = pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1.0,
        }
    )
    gap = factor_ma_gap(df)
    assert pd.isna(gap.iloc[18])
    assert pd.notna(gap.iloc[19])


def test_dual_ma_still_opens_on_clear_golden_cross() -> None:
    """急拉后短 EMA 仍能上穿长 EMA，开多逻辑还在。"""
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 94, 110]
    index = pd.date_range(end="2026-10-23 14:00", periods=len(closes), freq="15min")
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )
    cfg = {
        "strategy": {
            "engine": "dual_ma",
            "timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "dual_ma": {"short_period": 3, "long_period": 5},
        },
        "trade": {"symbol": "bu2612", "volume": 1},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
    }
    decision = DualMaStrategy(cfg).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 14, 0)
    )
    assert decision.action == StrategyAction.BUY_OPEN
