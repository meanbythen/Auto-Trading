"""tick 配置：空名单跟关注品种，配置错误不要反复重连。不需要 vnpy。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.data.tick.downloader import main_cont_from_symbol, main_cont_list_from_symbols
from src.data.tick.recorder import TickConfigError, TickRecorder, tick_settings
from src.data.tick.vnpy_store import tick_overview


def _cfg(tmp_path: Path, symbols: list[str]) -> dict:
    return {
        "_project_root": str(tmp_path),
        "data": {
            "tq": {"user": "", "password": ""},
            "tick": {
                "symbols": symbols,
                "database": "data/ticks/vnpy_data.db",
                "status_file": "data/ticks/recorder_status.json",
            },
        },
    }


def test_empty_tick_symbols_use_watch_list(tmp_path) -> None:
    cfg = _cfg(tmp_path, [])
    cfg["strategy"] = {"symbols": ["fu2701", "c2701"]}
    assert tick_settings(cfg)["symbols"] == ["fu2701", "c2701"]


def test_main_cont_from_watch_symbols() -> None:
    assert main_cont_from_symbol("fu2701") == "KQ.m@SHFE.fu"
    assert main_cont_list_from_symbols(["fu2701", "c2701", "fu"]) == ["KQ.m@SHFE.fu", "KQ.m@DCE.c"]


def test_overview_without_file_is_empty(tmp_path) -> None:
    assert tick_overview(database=tmp_path / "missing.db") == []


def test_config_error_stops_without_reconnect(tmp_path) -> None:
    recorder = TickRecorder(_cfg(tmp_path, []))
    recorder._run()
    assert recorder.status()["state"] == "stopped"
    assert "没有要采集的合约" in recorder.status()["error"]


def test_lock_rejects_same_live_process(tmp_path) -> None:
    """同一个进程（PID + 创建时间都对上）才算真的在跑，要拦住。"""
    from src.data.tick import recorder as recorder_module

    recorder = TickRecorder(_cfg(tmp_path, ["fu2701"]))
    pid = os.getpid()
    created = recorder_module._process_created(pid) or ""
    recorder.lock_file.parent.mkdir(parents=True, exist_ok=True)
    recorder.lock_file.write_text(json.dumps({"pid": pid, "created": created}), encoding="utf-8")

    with pytest.raises(TickConfigError):
        recorder._acquire_lock()


def test_lock_takes_over_reused_pid(tmp_path) -> None:
    """PID 还在但创建时间对不上（被系统复用），按陈旧锁接管，不能误拦。"""
    recorder = TickRecorder(_cfg(tmp_path, ["fu2701"]))
    recorder.lock_file.parent.mkdir(parents=True, exist_ok=True)
    recorder.lock_file.write_text(
        json.dumps({"pid": os.getpid(), "created": "1"}), encoding="utf-8"
    )

    recorder._acquire_lock()

    payload = json.loads(recorder.lock_file.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["created"] != "1"


def test_lock_takes_over_legacy_int_lock(tmp_path) -> None:
    """旧格式（纯 PID）无法确认身份，按陈旧锁接管，否则一次误判会停一整天。"""
    recorder = TickRecorder(_cfg(tmp_path, ["fu2701"]))
    recorder.lock_file.parent.mkdir(parents=True, exist_ok=True)
    recorder.lock_file.write_text("24372", encoding="utf-8")

    recorder._acquire_lock()

    assert json.loads(recorder.lock_file.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_format_tick_time_converts_nanoseconds() -> None:
    """状态文件里的 last_tick_at 要可读时间，不能再是 1.795e+18 那种。"""
    from src.data.tick.vnpy_store import format_tick_time

    assert format_tick_time(1795584608372000000) == "2026-11-25 13:30:08"
    assert format_tick_time("2026-11-25 13:30:05") == "2026-11-25 13:30:05"
    assert format_tick_time(None) == ""


def test_release_lock_keeps_other_owner(tmp_path) -> None:
    recorder = TickRecorder(_cfg(tmp_path, ["fu2701"]))
    recorder.lock_file.parent.mkdir(parents=True, exist_ok=True)
    recorder.lock_file.write_text(
        json.dumps({"pid": 999999, "created": "x"}), encoding="utf-8"
    )
    recorder._locked = True

    recorder._release_lock()

    assert recorder.lock_file.exists()
