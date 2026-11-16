"""tick 清洗：脏数据过滤、去重、排序。不联网。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from src.data.tick.clean import clean_tick_frame


def _dirty_frame() -> pd.DataFrame:
    rows = [
        (datetime(2020, 1, 2, 9, 1, 5), 100.0, 99.0, 101.0, 10.0, 500.0),
        (datetime(2020, 1, 2, 9, 1, 10), float("nan"), 99.0, 101.0, 10.0, 500.0),
        (datetime(2020, 1, 2, 9, 1, 15), 0.0, 99.0, 101.0, 10.0, 500.0),
        (datetime(2020, 1, 2, 9, 1, 20), 101.0, 102.0, 100.0, 10.0, 500.0),
        (datetime(2020, 1, 2, 9, 1, 25), 101.0, -1.0, 101.0, 10.0, 500.0),
        (datetime(2020, 1, 2, 9, 1, 30), 100.0, 99.0, 101.0, -5.0, 500.0),
        (datetime(2020, 1, 2, 9, 1, 35), 100.0, 99.0, 101.0, 10.0, -1.0),
        (datetime(2020, 1, 2, 9, 2, 0), 100.5, 100.0, 101.0, 20.0, 510.0),
        (datetime(2020, 1, 2, 9, 2, 0), 100.6, 100.0, 101.0, 21.0, 511.0),
    ]
    frame = pd.DataFrame(
        rows,
        columns=["datetime", "last_price", "bid_price1", "ask_price1", "volume", "open_interest"],
    )
    # 故意打乱顺序，验证清洗后会按时间排好
    return frame.iloc[[7, 0, 3, 5, 2, 8, 6, 1, 4]].reset_index(drop=True)


def test_clean_drops_dirty_and_sorts() -> None:
    cleaned, stats = clean_tick_frame(_dirty_frame())

    assert stats.raw == 9
    assert stats.kept == 2
    assert stats.dropped == 7
    assert stats.reasons["bad_price"] == 2
    assert stats.reasons["crossed_book"] == 1
    assert stats.reasons["bad_book"] == 1
    assert stats.reasons["negative_volume"] == 1
    assert stats.reasons["negative_oi"] == 1
    assert stats.reasons["duplicate"] == 1

    assert list(cleaned.index) == [pd.Timestamp("2020-01-02 09:01:05"), pd.Timestamp("2020-01-02 09:02:00")]
    # 重复时间戳保留第一条
    assert cleaned.loc[pd.Timestamp("2020-01-02 09:02:00"), "last_price"] == 100.5
    assert "bid_price_1" in cleaned.columns
    assert "ask_price_1" in cleaned.columns


def test_clean_empty() -> None:
    cleaned, stats = clean_tick_frame(None)
    assert cleaned.empty
    assert stats.raw == 0
    assert stats.kept == 0


def test_clean_keeps_valid_rows_untouched() -> None:
    frame = _dirty_frame().iloc[[1]].reset_index(drop=True)
    cleaned, stats = clean_tick_frame(frame)
    assert stats.kept == 1
    assert cleaned["last_price"].iloc[0] == 100.0
    assert cleaned["volume"].iloc[0] == 10.0


def test_clean_accepts_missing_book() -> None:
    frame = pd.DataFrame(
        {
            "datetime": [datetime(2020, 1, 2, 9, 1, 5)],
            "last_price": [100.0],
            "volume": [10.0],
            "open_interest": [500.0],
            "bid_price_1": [0.0],
            "ask_price_1": [0.0],
        }
    )
    cleaned, stats = clean_tick_frame(frame)
    assert stats.kept == 1
    assert len(cleaned) == 1
