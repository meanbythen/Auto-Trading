"""BarCursor：逐根视图、下一根开盘价、止损/大周期对齐。"""

from __future__ import annotations

import pandas as pd

from src.backtest.bar_cursor import BarCursor
from src.backtest.engine import _trend_frame_for_signal


def _frame(times: list[str], closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    index = pd.to_datetime(times)
    open_values = opens if opens is not None else closes
    return pd.DataFrame(
        {
            "open": open_values,
            "high": [max(o, c) for o, c in zip(open_values, closes)],
            "low": [min(o, c) for o, c in zip(open_values, closes)],
            "close": closes,
        },
        index=index,
    )


def _bars() -> pd.DataFrame:
    times = [f"2026-10-30 09:{minute:02d}" for minute in range(0, 55, 5)]
    return _frame(times, [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0])


def test_indices_and_length_respect_start() -> None:
    cursor = BarCursor(_bars(), timeframe="5", start=3)
    assert len(cursor) == 8
    assert list(cursor.indices()) == list(range(3, 11))


def test_at_returns_window_and_current_bar() -> None:
    bars = _bars()
    cursor = BarCursor(bars, timeframe="5", start=0)
    view = cursor.at(2)
    assert view is not None
    assert view.index == 2
    assert len(view.signal) == 3
    assert view.now == bars.index[2].to_pydatetime()
    assert view.px == 102.0
    assert view.is_last is False


def test_next_open_is_next_bar_and_none_on_last() -> None:
    bars = _bars()
    cursor = BarCursor(bars, timeframe="5", start=0)
    first = cursor.at(0)
    assert first is not None
    assert first.next_open == 101.0
    last = cursor.at(len(bars) - 1)
    assert last is not None
    assert last.next_open is None
    assert last.is_last is True


def test_next_open_disabled_gives_none() -> None:
    cursor = BarCursor(_bars(), timeframe="5", fill_next_open=False)
    view = cursor.at(0)
    assert view is not None
    assert view.next_open is None


def test_trail_never_contains_future_bars() -> None:
    bars = _bars()
    cursor = BarCursor(bars, trail=bars, timeframe="5", start=0)
    view = cursor.at(4)
    assert view is not None
    assert view.trail is not None
    assert len(view.trail) == 5
    assert view.trail.index[-1] == bars.index[4]


def test_trail_none_when_absent_or_empty() -> None:
    bars = _bars()
    empty = pd.DataFrame({"open": [], "high": [], "low": [], "close": []}, index=pd.DatetimeIndex([]))
    no_trail = BarCursor(bars, trail=None).at(3)
    assert no_trail is not None
    assert no_trail.trail is None
    empty_trail = BarCursor(bars, trail=empty).at(3)
    assert empty_trail is not None
    assert empty_trail.trail is None


def test_trend_matches_legacy_helper() -> None:
    bars = _bars()
    daily = _frame(["2026-10-28", "2026-10-29", "2026-10-30"], [90.0, 91.0, 92.0])
    cursor = BarCursor(bars, trend=daily, timeframe="5", trend_timeframe="D", start=0)
    view = cursor.at(2)
    assert view is not None
    expected = _trend_frame_for_signal(daily, bars.index[2], "D")
    pd.testing.assert_frame_equal(view.trend, expected)


def test_missing_close_returns_none() -> None:
    bars = _bars()
    bars.loc[bars.index[0], "close"] = float("nan")
    bars.loc[bars.index[1], "close"] = float("nan")
    cursor = BarCursor(bars, timeframe="5", start=0)
    assert cursor.at(1) is None
    assert cursor.at(2) is not None


def test_at_matches_legacy_loop_expressions() -> None:
    """逐根对照旧主循环首段的取值表达式，保证等价。"""
    bars = _bars()
    daily = _frame(["2026-10-28", "2026-10-29", "2026-10-30"], [90.0, 91.0, 92.0])
    cursor = BarCursor(bars, trail=bars, trend=daily, timeframe="5", trend_timeframe="D")
    for i in cursor.indices():
        view = cursor.at(i)
        assert view is not None
        last_ts = bars.index[i]
        pd.testing.assert_frame_equal(view.signal, bars.iloc[: i + 1])
        assert view.now == last_ts.to_pydatetime()
        assert view.px == float(bars["close"].iloc[i])
        pd.testing.assert_frame_equal(view.trail, bars[bars.index <= last_ts])
        pd.testing.assert_frame_equal(view.trend, _trend_frame_for_signal(daily, last_ts, "D"))
        expected_open = float(bars["open"].iloc[i + 1]) if i + 1 < len(bars) else None
        assert view.next_open == expected_open


def test_out_of_range_returns_none() -> None:
    cursor = BarCursor(_bars(), timeframe="5", start=0)
    assert cursor.at(-1) is None
    assert cursor.at(len(cursor.signal)) is None
