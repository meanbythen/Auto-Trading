"""本地 tick 库存统计：按日聚合后再拼连续时段，不导出逐笔。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from src.data.tick.inventory import (
    collect_tick_inventory,
    merge_day_groups,
    tick_inventory_message,
    write_tick_inventory,
)
from src.data.tick.vnpy_store import reset_database, save_ticks, ticks_from_frame

pytest.importorskip("vnpy")


@pytest.fixture(autouse=True)
def _clean_database() -> None:
    reset_database()
    yield
    reset_database()


def test_merge_keeps_weekend_and_splits_long_gap() -> None:
    """周五到周一不断档；空出一周以上拆成两段。"""
    days = [
        {"日历日": date(2026, 11, 13), "起": "2026-11-13 09:00:00", "止": "2026-11-13 15:00:00", "根数": 10},
        {"日历日": date(2026, 11, 16), "起": "2026-11-16 09:00:00", "止": "2026-11-16 15:00:00", "根数": 20},
        {"日历日": date(2026, 11, 25), "起": "2026-11-25 09:00:00", "止": "2026-11-25 15:00:00", "根数": 5},
    ]
    spans = merge_day_groups(days, gap_days=5)
    assert len(spans) == 2
    assert spans[0]["根数"] == 30
    assert spans[0]["有数天数"] == 2
    assert spans[0]["日历日起"] == date(2026, 11, 13)
    assert spans[0]["日历日止"] == date(2026, 11, 16)
    assert spans[1]["根数"] == 5
    assert spans[1]["日历日起"] == date(2026, 11, 25)


def _ticks_on(days: list[datetime], *, instrument: str = "SHFE.fu2701"):
    frame = pd.DataFrame(
        {
            "datetime": days,
            "last_price": [3100.0] * len(days),
            "volume": [1.0] * len(days),
            "open_interest": [1.0] * len(days),
            "bid_price1": [3099.0] * len(days),
            "bid_volume1": [1.0] * len(days),
            "ask_price1": [3101.0] * len(days),
            "ask_volume1": [1.0] * len(days),
            "highest": [3110.0] * len(days),
            "lowest": [3090.0] * len(days),
        }
    )
    return ticks_from_frame(frame, instrument=instrument)


def test_collect_spans_source_and_excel_not_kline_book(tmp_path: Path) -> None:
    """连续时段、来源、根数进独立 Excel；不写 K线库存.xlsx。"""
    database = tmp_path / "data" / "ticks" / "vnpy_data.db"
    history = tmp_path / "data" / "ticks" / "history" / "fu"
    history.mkdir(parents=True)
    csv_path = history / "SHFE.fu2701_20261113_20261116.csv"
    csv_path.write_text("datetime,last_price\n", encoding="utf-8")

    ticks = _ticks_on(
        [
            datetime(2026, 11, 13, 9, 1, 0),
            datetime(2026, 11, 13, 9, 1, 1),
            datetime(2026, 11, 16, 9, 1, 0),
            datetime(2026, 11, 25, 9, 1, 0),
        ]
    )
    save_ticks(ticks, database=database)

    book = collect_tick_inventory(tmp_path, database=database, history_dir=history.parent)
    assert book["contracts_n"] == 1
    assert book["ticks"] == 4
    assert book["spans_n"] == 2
    assert book["gaps_n"] == 1
    contract = book["by_contract"][0]
    assert contract["合约"] == "fu2701"
    assert contract["交易所"] == "SHFE"
    assert contract["数据源"] == "天勤"
    assert contract["入库途径"] == "含历史下载CSV"
    assert contract["历史CSV块数"] == 1
    assert contract["连续段数"] == 2
    first, second = book["spans"]
    assert first["根数"] == 3
    assert first["日历日起"] == "2026-11-13"
    assert first["日历日止"] == "2026-11-16"
    assert second["根数"] == 1
    assert book["gaps"][0]["合约"] == "fu2701"
    assert "逐笔仍在数据库" in tick_inventory_message(book)

    path = write_tick_inventory(tmp_path, book)
    wb = load_workbook(path)
    assert wb.sheetnames == ["说明", "总览", "按数据源", "按合约", "连续时段", "缺口", "按日"]
    headers = [cell.value for cell in wb["连续时段"][1]]
    assert "合约" in headers
    assert "起" in headers
    assert "止" in headers
    assert "根数" in headers
    assert "数据源" in headers
    assert (tmp_path / "data" / "TICK库存.xlsx").exists()
    assert not (tmp_path / "data" / "K线库存.xlsx").exists()


def test_empty_inventory_does_not_require_database(tmp_path: Path) -> None:
    book = collect_tick_inventory(tmp_path)
    assert book["contracts_n"] == 0
    assert "还没有存下的 tick" in tick_inventory_message(book)
    path = write_tick_inventory(tmp_path, book)
    assert path.exists()
