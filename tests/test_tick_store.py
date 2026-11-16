"""tick 入库底座：字段映射、时区、幂等。全程不联网。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data.tick import vnpy_store
from src.data.tick.vnpy_store import (
    load_tick_frame,
    load_ticks,
    reset_database,
    save_ticks,
    split_instrument,
    tick_overview,
    ticks_from_frame,
)

pytest.importorskip("vnpy")

TZ = vnpy_store.TZ


@pytest.fixture(autouse=True)
def _clean_database() -> None:
    reset_database()
    yield
    reset_database()


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [
                datetime(2026, 11, 16, 9, 1, 0, 500000),
                datetime(2026, 11, 16, 9, 1, 1, 0),
            ],
            "last_price": [3100.0, 3101.0],
            "volume": [100.0, 110.0],
            "open_interest": [50000.0, 50010.0],
            "bid_price1": [3099.0, 3100.0],
            "bid_volume1": [5.0, 6.0],
            "ask_price1": [3101.0, 3102.0],
            "ask_volume1": [7.0, 8.0],
            "highest": [3110.0, 3111.0],
            "lowest": [3090.0, 3091.0],
        }
    )


def test_split_instrument() -> None:
    from vnpy.trader.constant import Exchange

    assert split_instrument("SHFE.fu2701") == ("fu2701", Exchange.SHFE)
    assert split_instrument("CZCE.SR701") == ("SR701", Exchange.CZCE)
    with pytest.raises(ValueError):
        split_instrument("fu2701")


def test_frame_to_ticks_keeps_beijing_time() -> None:
    ticks = ticks_from_frame(_frame(), instrument="SHFE.fu2701")
    assert len(ticks) == 2
    assert ticks[0].symbol == "fu2701"
    first = ticks[0]
    # 数值不变，只附加上海时区
    assert first.datetime.tzinfo is not None
    assert first.datetime.utcoffset().total_seconds() == 8 * 3600
    assert first.datetime.replace(tzinfo=None) == datetime(2026, 11, 16, 9, 1, 0, 500000)
    assert first.last_price == 3100.0
    assert first.bid_price_1 == 3099.0
    assert first.ask_volume_1 == 7.0
    assert first.turnover == 0.0
    assert first.bid_price_2 == 0.0


def test_save_and_load_roundtrip(tmp_path) -> None:
    database = tmp_path / "vnpy_data.db"
    ticks = ticks_from_frame(_frame(), instrument="SHFE.fu2701")
    assert save_ticks(ticks, database=database) == 2

    loaded = load_ticks("fu2701", ticks[0].exchange, database=database)
    assert [tick.last_price for tick in loaded] == [3100.0, 3101.0]
    assert all(tick.datetime.tzinfo is not None for tick in loaded)
    assert loaded[0].datetime.replace(tzinfo=None) == datetime(2026, 11, 16, 9, 1, 0, 500000)

    frame = load_tick_frame("fu2701", ticks[0].exchange, database=database)
    assert list(frame.columns)[:2] == ["last_price", "volume"]
    assert frame.index.tz is None
    assert frame["volume"].tolist() == [100.0, 110.0]


def test_duplicate_timestamp_replace(tmp_path) -> None:
    database = tmp_path / "vnpy_data.db"
    first = ticks_from_frame(_frame().iloc[[0]], instrument="SHFE.fu2701")
    save_ticks(first, database=database)

    changed = _frame().iloc[[0]].copy()
    changed.loc[changed.index[0], "last_price"] = 3105.0
    second = ticks_from_frame(changed, instrument="SHFE.fu2701")
    save_ticks(second, database=database)

    loaded = load_ticks("fu2701", first[0].exchange, database=database)
    assert len(loaded) == 1
    assert loaded[0].last_price == 3105.0


def test_empty_save_creates_nothing(tmp_path) -> None:
    database = tmp_path / "vnpy_data.db"
    assert save_ticks([], database=database) == 0
    assert not database.exists()


def test_overview_counts_rows(tmp_path) -> None:
    database = tmp_path / "vnpy_data.db"
    ticks = ticks_from_frame(_frame(), instrument="SHFE.fu2701")
    save_ticks(ticks, database=database)
    overview = tick_overview(database=database)
    assert len(overview) == 1
    assert overview[0]["symbol"] == "fu2701"
    assert overview[0]["exchange"] == "SHFE"
    assert overview[0]["count"] == 2
