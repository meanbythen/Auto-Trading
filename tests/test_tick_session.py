"""tick 自动采集：交易时段状态机、进程锁、收盘清理。不联网。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from src.data.tick import recorder as recorder_module
from src.data.tick.recorder import TickConfigError, TickRecorder


def _cfg(tmp_path: Path, symbols: list[str], **tick) -> dict:
    box = {
        "symbols": symbols,
        "session_only": True,
        "idle_poll_seconds": 5,
        "database": "data/ticks/vnpy_data.db",
        "status_file": "data/ticks/recorder_status.json",
        "lock_file": "data/ticks/recorder.lock",
        "synth_state_file": "data/ticks/synth_state.json",
    }
    box.update(tick)
    return {"_project_root": str(tmp_path), "data": {"tq": {}, "tick": box}}


def test_friday_night_extends_into_saturday(tmp_path) -> None:
    recorder = TickRecorder(_cfg(tmp_path, ["ag2701"]))
    # 2026-11-27 是周五，白银夜盘 21:00～次日 02:30。
    assert recorder._in_session(datetime(2026, 11, 27, 23, 0)) is True
    assert recorder._in_session(datetime(2026, 11, 28, 1, 0)) is True
    # 周六凌晨不是交易日；周一凌晨属于周日，也不能算。
    assert recorder._in_session(datetime(2026, 11, 29, 1, 0)) is False
    assert recorder._in_session(datetime(2026, 11, 30, 1, 0)) is False
    assert recorder._in_session(datetime(2026, 11, 30, 9, 30)) is True


def test_next_open_skips_weekend(tmp_path) -> None:
    recorder = TickRecorder(_cfg(tmp_path, ["fu2701"]))
    assert recorder._format_next_open(datetime(2026, 11, 23, 20, 0)) == "2026-11-23T21:00"
    assert recorder._format_next_open(datetime(2026, 11, 27, 23, 30)) == "2026-11-30T09:00"


def test_lock_blocks_second_process(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, ["fu2701"])
    lock = tmp_path / "data/ticks/recorder.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 999999, "created": "abc"}), encoding="utf-8")
    monkeypatch.setattr(recorder_module, "_process_created", lambda _pid: "abc")

    recorder = TickRecorder(cfg)
    with pytest.raises(TickConfigError, match="PID 999999"):
        recorder._acquire_lock()


def test_lock_released_only_by_owner(tmp_path) -> None:
    cfg = _cfg(tmp_path, ["fu2701"])
    recorder = TickRecorder(cfg)
    recorder._acquire_lock()
    lock = tmp_path / "data/ticks/recorder.lock"
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    recorder._release_lock()
    assert not lock.exists()


def test_cleanup_retention_rebuilds_overview(tmp_path) -> None:
    cfg = _cfg(tmp_path, ["fu2701"], retention_days=30)
    recorder = TickRecorder(cfg)
    path = Path(cfg["_project_root"]) / "data/ticks/vnpy_data.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE dbtickdata (symbol TEXT, exchange TEXT, datetime TEXT)"
        )
        conn.execute(
            "CREATE TABLE dbtickoverview "
            "(symbol TEXT, exchange TEXT, count INTEGER, start TEXT, end TEXT)"
        )
        conn.execute(
            "INSERT INTO dbtickdata VALUES ('fu2701', 'SHFE', '2020-01-02 09:00:00')"
        )
        conn.execute(
            "INSERT INTO dbtickdata VALUES ('fu2701', 'SHFE', ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO dbtickoverview VALUES ('fu2701', 'SHFE', 2, '', '')"
        )
        conn.commit()
    finally:
        conn.close()

    recorder._cleanup_retention()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM dbtickdata").fetchone()[0] == 1
        rows = conn.execute(
            "SELECT symbol, exchange, count FROM dbtickoverview"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("fu2701", "SHFE", 1)]
