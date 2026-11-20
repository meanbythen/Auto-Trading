"""P3 数据目录：合约快照、K 线目录、关闭 SQLite 时静默。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook

from src.data import catalog


def _write_universe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.append(["合约代码", "中文名", "保证金比例%", "每手数量", "启用"])
    sheet.append(["fu2701", "燃料油", "10", "10", "是"])
    book.save(path)
    return path


def _write_bars(root: Path) -> None:
    from src.data.bar_store import BarStore

    store = BarStore(root / catalog.KLINE_MARKET)
    frame = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [2.0, 3.0],
            "low": [0.5, 1.5],
            "close": [1.5, 2.5],
            "volume": [10.0, 20.0],
        },
        index=pd.to_datetime(["2026-11-01 09:00:00", "2026-11-01 09:05:00"]),
    )
    store.upsert("fu2701@tq", "5", frame)


@pytest.fixture(autouse=True)
def _close_bar_connections():
    yield
    from src.data import bar_store

    for conn in bar_store._CONNECTIONS.values():  # noqa: SLF001
        conn.close()
    bar_store._CONNECTIONS.clear()


def test_sqlite_disabled_writes_nothing(tmp_path: Path) -> None:
    cfg = {"storage": {"sqlite_enabled": False}}
    universe = _write_universe(tmp_path / "config" / "品种与合约.xlsx")

    assert catalog.refresh_instruments(tmp_path, cfg, universe) == ""
    assert catalog.refresh_kline_catalog(tmp_path, cfg) == 0
    assert catalog.refresh_tick_catalog(tmp_path, cfg) == 0
    assert catalog.catalog_reference(tmp_path, cfg) == {}
    assert not (tmp_path / "data" / "trading.db").exists()


def test_instrument_snapshot_is_idempotent(tmp_path: Path) -> None:
    universe = _write_universe(tmp_path / "config" / "品种与合约.xlsx")
    cfg: dict = {}

    first = catalog.refresh_instruments(tmp_path, cfg, universe)
    second = catalog.refresh_instruments(tmp_path, cfg, universe)

    assert first
    assert first == second
    rows = catalog.list_instruments(tmp_path, cfg=cfg)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "fu2701"
    assert rows[0]["product"] == "fu"
    assert int(rows[0]["enabled"]) == 1
    assert catalog.latest_instrument_snapshot(tmp_path, cfg)["snapshot_id"] == first


def test_kline_catalog_upsert_and_reference(tmp_path: Path) -> None:
    universe = _write_universe(tmp_path / "config" / "品种与合约.xlsx")
    cfg: dict = {}
    snapshot = catalog.refresh_instruments(tmp_path, cfg, universe)
    _write_bars(tmp_path)

    assert catalog.refresh_kline_catalog(tmp_path, cfg) == 1
    assert catalog.refresh_kline_catalog(tmp_path, cfg) == 1
    assert catalog.refresh_tick_catalog(tmp_path, cfg) == 0

    reference = catalog.catalog_reference(tmp_path, cfg)
    assert reference["instruments_snapshot"] == snapshot
    assert reference["kline_digest"]

    from src.account.trading_db import connect, storage_settings

    with connect(storage_settings(tmp_path, cfg).db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM data_catalog WHERE kind = 'kline'").fetchone()[0]
        row = conn.execute("SELECT symbol, source, period FROM data_catalog").fetchone()
    assert count == 1
    assert tuple(row) == ("fu2701", "tq", "5")
