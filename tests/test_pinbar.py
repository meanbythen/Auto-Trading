"""影线识别单元测试。流动性耗尽仍用这套判断，不依赖已删除的旧策略。"""

from pandas import Series

from src.strategy.indicators import detect_pinbar, trend_bias


def test_bullish_pinbar_long_lower_wick() -> None:
    """长下影应识别为看涨 Pinbar。"""
    # open=10, close=11, high=11.2, low=5 → 长下影
    r = detect_pinbar(10, 11.2, 5, 11, wick_body_ratio=2.0, wick_range_ratio=0.6)
    assert r.side == "bull"
    assert r.wick_extreme == 5


def test_bearish_pinbar_long_upper_wick() -> None:
    """长上影应识别为看跌 Pinbar。"""
    r = detect_pinbar(10, 16, 9.8, 9.5, wick_body_ratio=2.0, wick_range_ratio=0.6)
    assert r.side == "bear"
    assert r.wick_extreme == 16


def test_trend_bias_down() -> None:
    closes = Series([100, 99, 98, 97, 96, 95, 94, 93, 92, 91])
    assert trend_bias(closes, lookback=8) == "down"
