"""tick 增删查：按时段删除、抽查汇总、清掉续传后才能重下。不改逐笔内容。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.data.tick.downloader import forget_downloaded_chunks
from src.data.tick.manage import (
    format_tick_inspect,
    inspect_ticks,
    parse_tick_contract,
    preview_purge_ticks,
    purge_ticks,
)
from src.data.tick.vnpy_store import load_ticks, reset_database, save_ticks, ticks_from_frame, tick_overview

pytest.importorskip("vnpy")


@pytest.fixture(autouse=True)
def _clean_database() -> None:
    reset_database()
    yield
    reset_database()


def _ticks_on(days: list[datetime], *, instrument: str = "SHFE.fu2701"):
    frame = pd.DataFrame(
        {
            "datetime": days,
            "last_price": [float(3100 + i) for i in range(len(days))],
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


def test_parse_tick_contract() -> None:
    assert parse_tick_contract("fu2701") == ("fu2701", None)
    assert parse_tick_contract("SHFE.fu2701") == ("fu2701", "SHFE")
    with pytest.raises(ValueError):
        parse_tick_contract("  ")


def test_purge_one_day_then_inspect_and_redownload_resume(tmp_path: Path) -> None:
    """删中间一天，头尾还在；csv 和 manifest 交叉块清掉，下次才会重下。"""
    database = tmp_path / "data" / "ticks" / "vnpy_data.db"
    history = tmp_path / "data" / "ticks" / "history"
    history.mkdir(parents=True)
    csv_path = history / "fu" / "SHFE.fu2701_20261113_20261116.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("datetime,last_price\n", encoding="utf-8")
    other_csv = history / "fu" / "SHFE.fu2701_20261125_20261125.csv"
    other_csv.write_text("datetime,last_price\n", encoding="utf-8")
    (history / "download_manifest.json").write_text(
        json.dumps(
            {
                "chunks": {
                    "SHFE.fu2701|2026-11-13|2026-11-16": {
                        "status": "done",
                        "contract": "SHFE.fu2701",
                        "start": "2026-11-13",
                        "end": "2026-11-16",
                    },
                    "SHFE.fu2701|2026-11-25|2026-11-25": {
                        "status": "done",
                        "contract": "SHFE.fu2701",
                        "start": "2026-11-25",
                        "end": "2026-11-25",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ticks = _ticks_on(
        [
            datetime(2026, 11, 13, 9, 1, 0),
            datetime(2026, 11, 16, 9, 1, 0),
            datetime(2026, 11, 16, 9, 1, 1),
            datetime(2026, 11, 25, 9, 1, 0),
        ]
    )
    save_ticks(ticks, database=database)

    seen = preview_purge_ticks(
        database=database, symbol="fu2701", ts_from="2026-11-16", ts_to="2026-11-16"
    )
    assert seen["count"] == 2
    assert seen["exchange"] == "SHFE"

    book = inspect_ticks(
        database=database, symbol="SHFE.fu2701", ts_from="2026-11-16", ts_to="2026-11-16"
    )
    assert book["count"] == 2
    assert "最早几笔" in format_tick_inspect(book)

    result = purge_ticks(
        tmp_path,
        database=database,
        history_dir=history,
        symbol="fu2701",
        ts_from="2026-11-16",
        ts_to="2026-11-16",
        also_synth_bars=False,
    )
    assert result["deleted"] == 2
    assert result["manifest_cleared"] == 1
    assert not csv_path.exists()
    assert other_csv.exists()

    remaining = load_ticks("fu2701", ticks[0].exchange, database=database)
    assert [item.datetime.replace(tzinfo=None) for item in remaining] == [
        datetime(2026, 11, 13, 9, 1, 0),
        datetime(2026, 11, 25, 9, 1, 0),
    ]
    overview = tick_overview(database=database)
    assert overview[0]["count"] == 2

    manifest = json.loads((history / "download_manifest.json").read_text(encoding="utf-8"))
    assert "SHFE.fu2701|2026-11-13|2026-11-16" not in manifest["chunks"]
    assert "SHFE.fu2701|2026-11-25|2026-11-25" in manifest["chunks"]


def test_forget_chunks_without_database(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    csv_path = history / "SHFE.au2612_20260101_20260107.csv"
    csv_path.write_text("x\n", encoding="utf-8")
    (history / "download_manifest.json").write_text(
        json.dumps(
            {
                "chunks": {
                    "SHFE.au2612|2026-01-01|2026-01-07": {
                        "status": "done",
                        "contract": "SHFE.au2612",
                        "start": "2026-01-01",
                        "end": "2026-01-07",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    out = forget_downloaded_chunks(
        history, contract="au2612", start=datetime(2026, 1, 1).date(), end=datetime(2026, 1, 7).date()
    )
    assert out["manifest_cleared"] == 1
    assert not csv_path.exists()
