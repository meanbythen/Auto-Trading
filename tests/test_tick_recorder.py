"""tick 常驻采集：订阅解析、去重、flush、心跳、停止。全程不联网。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.data.tick import recorder as recorder_module
from src.data.tick.recorder import TickRecorder, read_status, tick_settings
from src.data.tick.vnpy_store import reset_database, tick_overview

pytest.importorskip("vnpy")


@pytest.fixture(autouse=True)
def _clean_database() -> None:
    reset_database()
    yield
    reset_database()


def _cfg(tmp_path: Path, symbols: list[str], *, resolve_main: bool = True) -> dict:
    return {
        "_project_root": str(tmp_path),
        "data": {
            "tq": {"user": "", "password": ""},
            "tick": {
                "symbols": symbols,
                "resolve_main": resolve_main,
                "flush_seconds": 0.1,
                "batch_size": 2,
                "database": "data/ticks/vnpy_data.db",
                "status_file": "data/ticks/recorder_status.json",
            },
        },
    }


class _FakeApi:
    def __init__(self) -> None:
        self.calls = 0
        self.frames: dict[str, pd.DataFrame] = {}
        self.closed = False
        self.owner: TickRecorder | None = None

    def get_tick_serial(self, instrument: str, data_length: int = 2) -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "datetime": ["2020-01-02 09:01:00.000000"],
                "last_price": [100.0],
                "volume": [10.0],
                "open_interest": [500.0],
                "bid_price1": [99.0],
                "ask_price1": [101.0],
                "highest": [100.0],
                "lowest": [100.0],
            }
        )
        self.frames[instrument] = frame
        return frame

    def wait_update(self, deadline: float | None = None) -> None:
        self.calls += 1
        frame = self.frames["SHFE.fu2701"]
        if self.calls == 1:
            frame.loc[0, "datetime"] = "2020-01-02 09:01:05.000000"
            frame.loc[0, "last_price"] = 100.0
            frame.loc[0, "volume"] = 10.0
        elif self.calls == 2:
            frame.loc[0, "datetime"] = "2020-01-02 09:01:06.000000"
            frame.loc[0, "last_price"] = 101.0
            frame.loc[0, "volume"] = 20.0
        else:
            assert self.owner is not None
            self.owner._stop.set()

    def is_changed(self, serial: pd.DataFrame) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def test_tick_settings_defaults(tmp_path) -> None:
    settings = tick_settings(_cfg(tmp_path, []))
    assert settings["database"] == tmp_path / "data/ticks/vnpy_data.db"
    assert settings["status_file"] == tmp_path / "data/ticks/recorder_status.json"
    assert settings["flush_seconds"] == 0.1


def test_resolve_instruments_and_main_contract(tmp_path, monkeypatch) -> None:
    recorder = TickRecorder(_cfg(tmp_path, ["fu", "fu2701"]))
    monkeypatch.setattr(recorder_module, "main_contract", lambda *_a, **_k: "fu2701")
    instruments = recorder._resolve_instruments(object())
    assert instruments == ["SHFE.fu2701"]


def test_product_without_resolve_is_rejected(tmp_path) -> None:
    recorder = TickRecorder(_cfg(tmp_path, ["fu"], resolve_main=False))
    with pytest.raises(RuntimeError, match="真实合约"):
        recorder._resolve_instruments(object())


def test_append_dedupes_and_flush(tmp_path) -> None:
    recorder = TickRecorder(_cfg(tmp_path, ["fu2701"]))
    frame = _FakeApi().get_tick_serial("SHFE.fu2701")
    recorder._append_serial("SHFE.fu2701", frame)
    recorder._append_serial("SHFE.fu2701", frame)
    assert recorder.status()["buffered"] == 1

    assert recorder.flush() == 1
    assert recorder.status()["buffered"] == 0
    overview = tick_overview(database=tmp_path / "data/ticks/vnpy_data.db")
    assert overview[0]["count"] == 1


def test_thread_collects_flushes_and_stops(tmp_path) -> None:
    api = _FakeApi()
    recorder = TickRecorder(_cfg(tmp_path, ["fu2701"]), api_factory=lambda: api)
    api.owner = recorder

    recorder.start()
    assert recorder._thread is not None
    recorder._thread.join(timeout=15)
    assert not recorder._thread.is_alive()
    assert api.closed is True

    overview = tick_overview(database=tmp_path / "data/ticks/vnpy_data.db")
    # 连上时写当前这一笔，之后 wait_update 再写两笔。
    assert overview[0]["count"] == 3
    status = read_status(tmp_path / "data/ticks/recorder_status.json")
    assert status["state"] == "stopped"
    assert status["symbols"] == ["SHFE.fu2701"]


def test_read_status_missing_returns_empty(tmp_path) -> None:
    assert read_status(tmp_path / "nope.json") == {}
