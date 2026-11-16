"""专业版历史 tick 下载：分段、断点续传、权限提示、下载后入库合成。不联网。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.tick.downloader import (
    DownloadPermissionError,
    download_settings,
    plan_chunks,
    run_download,
)
from src.data.tick.vnpy_store import reset_database, tick_overview

pytest.importorskip("vnpy")


@pytest.fixture(autouse=True)
def _clean_database() -> None:
    reset_database()
    yield
    reset_database()


class _FakeApi:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query_his_cont_quotes(self, symbol: str, n: int = 200) -> pd.DataFrame:
        self.queries.append(symbol)
        return pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
                ),
                symbol: ["SHFE.fu2701", "SHFE.fu2701", "SHFE.fu2702", "SHFE.fu2702"],
            }
        )


def _cfg(tmp_path: Path, *, start: str = "2026-10-01", end: str = "2026-10-06") -> dict:
    return {
        "_project_root": str(tmp_path),
        "data": {
            "tq": {
                "user": "u",
                "password": "p",
                "history_tick": {
                    "enabled": True,
                    "main_cont_list": ["KQ.m@SHFE.fu"],
                    "start": start,
                    "end": end,
                    "chunk_days": 2,
                    "out_dir": "data/ticks/history",
                },
            },
            "tick": {
                "database": "data/ticks/vnpy_data.db",
                "bar_store": "data/klines/tick_bars.sqlite",
                "synth_periods": ["1"],
            },
        },
    }


def _write_csv(chunk) -> None:
    chunk.path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "datetime": ["2026-10-01 09:00:05", "2026-10-01 09:00:35"],
            "last_price": [100.0, 101.0],
            "average": [100.0, 100.5],
            "highest": [100.0, 101.0],
            "lowest": [100.0, 100.0],
            "ask_price1": [101.0, 102.0],
            "ask_volume1": [1.0, 2.0],
            "bid_price1": [99.0, 100.0],
            "bid_volume1": [1.0, 2.0],
            "volume": [100.0, 120.0],
            "open_interest": [500.0, 501.0],
        }
    ).to_csv(chunk.path, index=False)


def test_plan_chunks_splits_by_contract_and_days(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))
    chunks = plan_chunks(_FakeApi(), settings)

    assert [(c.contract, str(c.start), str(c.end)) for c in chunks] == [
        ("SHFE.fu2701", "2026-10-01", "2026-10-02"),
        ("SHFE.fu2702", "2026-10-05", "2026-10-06"),
    ]
    assert chunks[0].path.parent.name == "SHFE.fu2701"
    assert chunks[0].path.parent.parent.name == "fu"


def test_run_download_ingests_and_resumes(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))
    calls: list[str] = []

    def fake_download(api, chunk) -> None:
        calls.append(chunk.key)
        _write_csv(chunk)

    summary = run_download(settings, api_factory=_FakeApi, downloader_factory=fake_download)
    assert summary["total"] == 2
    assert summary["downloaded"] == 2
    assert len(calls) == 2
    assert tick_overview(database=tmp_path / "data/ticks/vnpy_data.db")[0]["count"] == 2

    bars = BarStore(tmp_path / "data/klines/tick_bars.sqlite").load("fu2701@tick", "1")
    assert bars is not None and len(bars) == 1
    assert bars["volume"].iloc[0] == 20.0

    manifest = json.loads(
        (tmp_path / "data/ticks/history/download_manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["chunks"]) == 2
    assert all(item["status"] == "done" for item in manifest["chunks"].values())

    def should_not_run(api, chunk) -> None:
        raise AssertionError("已完成的块不应重新下载")

    again = run_download(settings, api_factory=_FakeApi, downloader_factory=should_not_run)
    assert again["skipped"] == 2
    assert again["downloaded"] == 0


def test_permission_error_is_explicit(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))

    def denied(api, chunk) -> None:
        raise DownloadPermissionError("需要天勤专业版或试用权限才能下载历史 tick")

    with pytest.raises(DownloadPermissionError):
        run_download(settings, api_factory=_FakeApi, downloader_factory=denied)
    status = json.loads(
        (tmp_path / "data/ticks/history/download_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "permission_denied"


def test_dry_run_lists_without_downloading(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))
    lines: list[str] = []
    summary = run_download(
        settings,
        api_factory=_FakeApi,
        downloader_factory=lambda api, chunk: pytest.fail("dry-run 不应下载"),
        on_progress=lines.append,
        dry_run=True,
    )
    assert summary["dry_run"] is True
    assert summary["total"] == 2
    assert len(lines) == 2
    assert not (tmp_path / "data/ticks/history/download_manifest.json").exists()
