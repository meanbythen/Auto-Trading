"""按周期和时段算应有K线、缺口与周期识别。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data.coverage import (
    PeriodMismatchError,
    coverage_report,
    detect_period,
    ensure_period,
    expected_bar_count,
    expected_bar_times,
    require_timestamps,
)


def _frame(stamps: list[str]) -> pd.DataFrame:
    index = pd.DatetimeIndex(pd.to_datetime(stamps))
    return pd.DataFrame(
        {
            "open": [1.0] * len(index),
            "high": [1.0] * len(index),
            "low": [1.0] * len(index),
            "close": [1.0] * len(index),
            "volume": [1.0] * len(index),
        },
        index=index,
    )


def test_five_minute_times_skip_lunch_and_break() -> None:
    """上午 09:00–09:15 有三根；跨午休只算 11:25、11:30、13:35。"""
    assert expected_bar_count(datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 9, 15), "5") == 3
    assert (
        expected_bar_count(datetime(2026, 11, 12, 11, 25), datetime(2026, 11, 12, 13, 35), "5")
        == 3
    )


def test_daily_counts_trading_days_only() -> None:
    """周一到周五五根，周末不生成。"""
    stamps = expected_bar_times(datetime(2026, 11, 9), datetime(2026, 11, 15, 23, 59), "D")
    assert len(stamps) == 5
    assert all(stamp.weekday() < 5 for stamp in stamps)


def test_coverage_reports_missing_range() -> None:
    """缺 09:10 一根，缺口从 09:10 到 09:10。"""
    bars = _frame(["2026-11-12 09:05", "2026-11-12 09:15"])
    report = coverage_report(bars, datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 9, 15), "5")
    assert report.expected_n == 3
    assert report.have_n == 2
    assert report.missing_n == 1
    assert report.missing_ranges == (("2026-11-12 09:10", "2026-11-12 09:10", 1),)


def test_coverage_is_complete_after_two_batches() -> None:
    """两批接上后覆盖完整，不再报缺口。"""
    bars = _frame(
        [
            "2026-11-12 09:05",
            "2026-11-12 09:10",
            "2026-11-12 09:15",
            "2026-11-12 09:20",
            "2026-11-12 09:25",
        ]
    )
    report = coverage_report(bars, datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 9, 25), "5")
    assert report.expected_n == 5
    assert report.missing_n == 0
    assert report.missing_ranges == ()


def test_detect_period_and_mismatch() -> None:
    """15 分钟的图不能被当成 5 分钟存进库。"""
    bars = _frame(["2026-11-12 09:15", "2026-11-12 09:30", "2026-11-12 09:45"])
    assert detect_period(bars) == "15"
    ensure_period(bars, "15")
    with pytest.raises(PeriodMismatchError):
        ensure_period(bars, "5")


def test_require_timestamps_rejects_empty_and_duplicate() -> None:
    """没有时间戳或时间重复的批次不落库。"""
    with pytest.raises(ValueError, match="没有读到"):
        require_timestamps(None)
    dup = _frame(["2026-11-12 09:05", "2026-11-12 09:05"])
    with pytest.raises(ValueError, match="重复"):
        require_timestamps(dup)
