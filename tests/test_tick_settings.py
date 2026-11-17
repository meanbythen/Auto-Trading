"""tick 配置：空名单跟关注品种，配置错误不要反复重连。不需要 vnpy。"""

from __future__ import annotations

from pathlib import Path

from src.data.tick.downloader import main_cont_from_symbol, main_cont_list_from_symbols
from src.data.tick.recorder import TickRecorder, tick_settings
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
