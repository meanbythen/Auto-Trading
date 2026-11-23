"""K 线补齐：只补缺、断点续传、上限与交易时段保护。"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.data.bar_store import BarStore
from src.data.factor_backfill import (
    load_manifest,
    manifest_path,
    plan_kline_backfill,
    run_kline_backfill,
)
from src.data.quote_check import storage_symbol


def _frame(n: int, *, oi: bool = True) -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(datetime.now().date()), periods=n, freq="1D")
    data = {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
    }
    if oi:
        data["oi"] = 1000.0
    return pd.DataFrame(data, index=index)


class FakeProvider:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, str, bool]] = []

    def fetch(self, symbol: str, period: str, *, keep_history: bool = False):
        self.calls.append((symbol, period, keep_history))
        return self.frame


def _cfg() -> dict:
    return {"data": {"source": "tq", "factor_backfill": {"pause_when_trading": False}}}


def test_plan_marks_ready_symbols_ok(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol("FG2605", "tq"), "D", _frame(1084))
    tasks = plan_kline_backfill(store, ["FG2605"], ["D"], mode="standard")
    assert len(tasks) == 1
    assert tasks[0].state == "ok"
    assert not tasks[0].pending


def test_run_skips_when_data_is_enough(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol("FG2605", "tq"), "D", _frame(1084))
    provider = FakeProvider(_frame(1084))
    report = run_kline_backfill(
        _cfg(),
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="standard",
        store=store,
        provider=provider,
        pause_when_trading=False,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert report["status"] == "done"
    assert "不需要补齐" in report["message"]
    assert provider.calls == []


def test_run_fills_gap_once_and_is_idempotent(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol("FG2605", "tq"), "D", _frame(100))
    provider = FakeProvider(_frame(700))
    first = run_kline_backfill(
        _cfg(),
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        store=store,
        provider=provider,
        pause_when_trading=False,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert first["done"] == 1
    assert first["failed"] == 0
    assert store.count(storage_symbol("FG2605", "tq"), "D") == 700
    assert len(provider.calls) == 1

    second = run_kline_backfill(
        _cfg(),
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        store=store,
        provider=provider,
        pause_when_trading=False,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert "不需要补齐" in second["message"]
    assert len(provider.calls) == 1


def test_cap_reached_is_not_retried(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol("FG2605", "tq"), "D", _frame(100))
    provider = FakeProvider(_frame(10000, oi=False))
    first = run_kline_backfill(
        _cfg(),
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        require_oi=True,
        store=store,
        provider=provider,
        pause_when_trading=False,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert first["capped"] == 1
    assert len(provider.calls) == 1
    assert store.count(storage_symbol("FG2605", "tq"), "D") == 10000
    manifest = load_manifest(manifest_path(tmp_path))
    assert manifest["tasks"][f"{storage_symbol('FG2605', 'tq')}|D"]["state"] == "capped"

    second = run_kline_backfill(
        _cfg(),
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        require_oi=True,
        store=store,
        provider=provider,
        pause_when_trading=False,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert len(provider.calls) == 1
    assert second["capped"] == 1


def test_source_without_oi_is_unsupported(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol("FG2605", "akshare"), "D", _frame(100))
    provider = FakeProvider(_frame(700, oi=False))
    cfg = {"data": {"source": "akshare", "factor_backfill": {"pause_when_trading": False}}}
    first = run_kline_backfill(
        cfg,
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        require_oi=True,
        source="akshare",
        store=store,
        provider=provider,
        pause_when_trading=False,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert first["skipped"] == 1
    assert len(provider.calls) == 1
    second = run_kline_backfill(
        cfg,
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        require_oi=True,
        source="akshare",
        store=store,
        provider=provider,
        pause_when_trading=False,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert len(provider.calls) == 1
    assert second["skipped"] == 1


def test_pauses_during_trading_session(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol("FG2605", "tq"), "D", _frame(100))
    provider = FakeProvider(_frame(700))
    report = run_kline_backfill(
        _cfg(),
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        store=store,
        provider=provider,
        pause_when_trading=True,
        now=datetime(2026, 11, 20, 10, 0),
        sleep=lambda _sec: None,
    )
    assert report["status"] == "paused"
    assert report["paused_by"] == "FG2605"
    assert provider.calls == []


def test_cancel_stops_before_fetch(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol("FG2605", "tq"), "D", _frame(100))
    provider = FakeProvider(_frame(700))
    cancel = threading.Event()
    cancel.set()
    report = run_kline_backfill(
        _cfg(),
        tmp_path,
        ["FG2605"],
        ["D"],
        mode="quick",
        store=store,
        provider=provider,
        pause_when_trading=False,
        cancel=cancel,
        now=datetime.now(),
        sleep=lambda _sec: None,
    )
    assert report["status"] == "canceled"
    assert provider.calls == []
