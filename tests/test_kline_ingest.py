"""读图批次记录、两批接续，以及库存 Excel 的读取记录页。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.inventory import collect_kline_inventory, write_kline_inventory


def _frame(stamps: list[str], close: float) -> pd.DataFrame:
    index = pd.DatetimeIndex(pd.to_datetime(stamps))
    return pd.DataFrame(
        {
            "open": [close] * len(index),
            "high": [close] * len(index),
            "low": [close] * len(index),
            "close": [close] * len(index),
            "volume": [1.0] * len(index),
        },
        index=index,
    )


def test_two_batches_continue_in_time_order(tmp_path: Path) -> None:
    """第一批读到 09:25、第二批从 09:15 接到 09:35，库里正好 7 根不重复。"""
    store = BarStore(tmp_path / "market.sqlite")
    first = [f"2026-11-12 09:{m:02d}" for m in (5, 10, 15, 20, 25)]
    second = [f"2026-11-12 09:{m:02d}" for m in (15, 20, 25, 30, 35)]
    store.upsert("fu2701@ths", "5", _frame(first, 100.0))
    store.upsert("fu2701@ths", "5", _frame(second, 200.0))
    bars = store.load("fu2701@ths", "5")
    assert len(bars) == 7
    assert bars.index[0].strftime("%H:%M") == "09:05"
    assert bars.index[-1].strftime("%H:%M") == "09:35"
    assert bars.loc[pd.Timestamp("2026-11-12 09:20"), "close"] == 200.0


def test_record_and_list_ingest(tmp_path: Path) -> None:
    """每批读图记一行，最新在前。"""
    store = BarStore(tmp_path / "market.sqlite")
    store.record_ingest(
        source="ths",
        symbol="fu2701@ths",
        period="5",
        requested_from="2026-11-12 09:00:00",
        requested_to="2026-11-12 10:15:00",
        read_from="2026-11-12 09:05:00",
        read_to="2026-11-12 09:25:00",
        read_n=5,
        added_n=5,
        expected_n=15,
        missing_n=10,
        note="时段内仍缺 10 根",
    )
    store.record_ingest(source="ths", symbol="fu2701@ths", period="5", read_n=3, added_n=0)
    rows = store.list_ingests()
    assert len(rows) == 2
    assert rows[0]["read_n"] == 3
    assert rows[1]["missing_n"] == 10


def test_inventory_excel_has_ingest_sheet(tmp_path: Path) -> None:
    """库存 Excel 必须带「读取记录」页，并写进批次数字。"""
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook

    store = BarStore(tmp_path / "data" / "klines" / "market.sqlite")
    store.upsert("fu2701@ths", "5", _frame(["2026-11-12 09:05", "2026-11-12 09:10"], 100.0))
    store.record_ingest(
        source="ths",
        symbol="fu2701@ths",
        period="5",
        requested_from="2026-11-12 09:00:00",
        requested_to="2026-11-12 09:15:00",
        read_n=2,
        added_n=2,
        expected_n=3,
        missing_n=1,
    )
    book = collect_kline_inventory(tmp_path)
    assert len(book["ingests"]) == 1
    path = write_kline_inventory(tmp_path, book)
    wb = load_workbook(path, read_only=True)
    assert "读取记录" in wb.sheetnames
    ws = wb["读取记录"]
    values = [str(cell.value) for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    assert "缺失" in values
    assert "请求时段" in values
