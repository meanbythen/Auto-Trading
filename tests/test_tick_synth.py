"""tick 合成 K 线：累计量差分、右标签、交易日归属、幂等。不联网。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.tick.clean import clean_tick_frame
from src.data.tick.synthesize import (
    build_tick_bars,
    minutes_to_daily,
    synthesize_symbol,
    ticks_to_minutes,
)
from src.data.tick.vnpy_store import reset_database, save_ticks, ticks_from_frame

pytest.importorskip("vnpy")


@pytest.fixture(autouse=True)
def _clean_database() -> None:
    reset_database()
    yield
    reset_database()


def _ticks() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [
                datetime(2020, 1, 2, 9, 1, 5),
                datetime(2020, 1, 2, 9, 1, 35),
                datetime(2020, 1, 2, 9, 2, 10),
                datetime(2020, 1, 2, 9, 2, 50),
            ],
            "last_price": [100.0, 101.0, 101.0, 102.0],
            "volume": [100.0, 110.0, 150.0, 190.0],
            "open_interest": [500.0, 501.0, 502.0, 503.0],
        }
    ).set_index("datetime")


def test_ticks_to_minutes_uses_cumulative_difference() -> None:
    bars = ticks_to_minutes(_ticks())
    # 右标签：09:01:05 落在 09:02 这根（09:01:00-09:02:00）
    assert list(bars.index) == [pd.Timestamp("2020-01-02 09:02:00"), pd.Timestamp("2020-01-02 09:03:00")]
    first, second = bars.iloc[0], bars.iloc[1]
    assert (first["open"], first["high"], first["low"], first["close"]) == (100.0, 101.0, 100.0, 101.0)
    # 累计量 110-100；190-150，不能把 190 直接当成交量
    assert first["volume"] == 10.0
    assert second["volume"] == 40.0
    assert second["oi"] == 503.0


def test_volume_reset_is_clamped_to_zero() -> None:
    frame = pd.DataFrame(
        {
            "datetime": [datetime(2020, 1, 2, 9, 1, 5), datetime(2020, 1, 2, 9, 1, 40)],
            "last_price": [100.0, 101.0],
            "volume": [900.0, 5.0],
            "open_interest": [500.0, 500.0],
        }
    ).set_index("datetime")
    bars = ticks_to_minutes(frame)
    assert bars["volume"].iloc[0] == 0.0


def test_night_session_belongs_to_next_trading_day() -> None:
    frame = pd.DataFrame(
        {
            "datetime": [
                datetime(2020, 1, 2, 21, 0, 30),
                datetime(2020, 1, 2, 21, 0, 50),
                datetime(2020, 1, 2, 21, 1, 30),
            ],
            "last_price": [100.0, 101.0, 102.0],
            "volume": [10.0, 30.0, 40.0],
            "open_interest": [500.0, 501.0, 502.0],
        }
    ).set_index("datetime")
    minutes = ticks_to_minutes(frame)
    daily = minutes_to_daily(minutes)
    assert list(daily.index) == [pd.Timestamp("2020-01-03")]
    assert daily["close"].iloc[0] == 102.0
    assert daily["volume"].iloc[0] == 20.0


def test_build_tick_bars_right_labels_and_complete_only() -> None:
    bars = build_tick_bars(_ticks(), periods=("1", "D"))
    assert set(bars) == {"1", "D"}
    minutes = bars["1"]
    # 右标签：09:01 这根代表 09:01:00-09:02:00，09:01:05 应落在 09:02
    assert minutes.index[0] == pd.Timestamp("2020-01-02 09:02:00")
    assert len(minutes) == 2
    assert bars["D"].index[0] == pd.Timestamp("2020-01-02")


def test_synthesize_symbol_end_to_end_and_idempotent(tmp_path) -> None:
    database = tmp_path / "vnpy_data.db"
    bar_path = tmp_path / "tick_bars.sqlite"
    ticks = ticks_from_frame(_ticks().reset_index(), instrument="SHFE.fu2701")
    save_ticks(ticks, database=database)

    report = synthesize_symbol("fu2701", database=database, bar_store=bar_path, periods=("1", "D"))
    assert report["symbol"] == "fu2701"
    assert report["ticks"] == 4
    assert report["kept"] == 4
    assert report["bars"] == {"1": 2, "D": 1}

    store = BarStore(bar_path)
    first = store.load("fu2701@tick", "1")
    assert first is not None
    assert list(first.index) == [pd.Timestamp("2020-01-02 09:02:00"), pd.Timestamp("2020-01-02 09:03:00")]
    assert first["volume"].tolist() == [10.0, 40.0]
    daily = store.load("fu2701@tick", "D")
    assert daily is not None and len(daily) == 1

    again = synthesize_symbol("fu2701", database=database, bar_store=bar_path, periods=("1", "D"))
    assert again["bars"] == {"1": 2, "D": 1}
    assert len(store.load("fu2701@tick", "1")) == 2


def test_clean_then_build_skips_dirty_ticks() -> None:
    raw = _ticks().reset_index()
    raw.loc[len(raw)] = {
        "datetime": datetime(2020, 1, 2, 9, 2, 55),
        "last_price": 0.0,
        "volume": 200.0,
        "open_interest": 503.0,
    }
    cleaned, stats = clean_tick_frame(raw)
    assert stats.kept == 4
    bars = build_tick_bars(cleaned, periods=("1",))
    assert bars["1"]["close"].tolist() == [101.0, 102.0]
