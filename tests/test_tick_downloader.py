"""专业版历史 tick 下载：分段、断点续传、权限提示、下载后入库合成。不联网。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.tick.downloader import (
    DownloadPermissionError,
    download_settings,
    plan_chunks,
    run_download,
    run_download_auto_resume,
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
                    "chunk_pause_sec": 0,
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


def test_run_download_pauses_between_chunks(tmp_path, monkeypatch) -> None:
    from src.data.tick import downloader as downloader_module

    cfg = _cfg(tmp_path)
    cfg["data"]["tq"]["history_tick"]["chunk_pause_sec"] = 3
    settings = download_settings(cfg)
    sleeps: list[float] = []
    monkeypatch.setattr(
        downloader_module.time, "sleep", lambda seconds: sleeps.append(seconds)
    )
    monkeypatch.setattr(
        downloader_module, "synthesize_symbol", lambda *a, **k: {"kept": 0, "bars": {}}
    )

    summary = run_download(
        settings,
        api_factory=_FakeApi,
        downloader_factory=lambda api, chunk: _write_csv(chunk),
    )

    assert summary["total"] == 2
    # 两块之间停一次，最后一块后面不停。
    assert sleeps == [3.0]


def test_download_settings_backfill_defaults(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg["data"]["tq"]["history_tick"].pop("chunk_pause_sec")
    settings = download_settings(cfg)
    assert settings["lookback_days"] == 120
    assert settings["chunk_pause_sec"] == 3.0
    assert settings["policy"] == "off_hours_only"
    assert settings["auto_resume"] is True
    assert settings["resume_poll_sec"] == 60.0


def _use_day_session(settings: dict) -> None:
    settings["policy"] = "off_hours_only"
    settings["trading_sessions"] = [["09:00", "11:30"]]
    settings["calendar"] = None


def test_trading_session_pauses_before_creating_api(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))
    _use_day_session(settings)
    created = False

    def api_factory():
        nonlocal created
        created = True
        return _FakeApi()

    summary = run_download(
        settings,
        api_factory=api_factory,
        downloader_factory=lambda api, chunk: pytest.fail("盘中不应下载"),
        clock=lambda: datetime(2026, 10, 14, 9, 30),
    )

    assert created is False
    assert summary["status"] == "paused"
    assert summary["paused"] is True
    status = json.loads(
        (tmp_path / "data/ticks/history/download_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "paused"


def test_open_market_pauses_and_next_run_resumes(tmp_path, monkeypatch) -> None:
    from src.data.tick import downloader as downloader_module

    settings = download_settings(_cfg(tmp_path))
    _use_day_session(settings)
    monkeypatch.setattr(
        downloader_module, "synthesize_symbol", lambda *a, **k: {"kept": 0, "bars": {}}
    )
    calls: list[str] = []

    def fake_download(api, chunk) -> None:
        calls.append(chunk.key)
        _write_csv(chunk)

    moments = iter(
        [
            datetime(2026, 10, 14, 8, 59),  # 建立连接前
            datetime(2026, 10, 14, 8, 59),  # 第一块开始前
            datetime(2026, 10, 14, 9, 0),   # 第二块开始前已开盘
        ]
    )
    paused = run_download(
        settings,
        api_factory=_FakeApi,
        downloader_factory=fake_download,
        clock=lambda: next(moments),
    )

    assert paused["status"] == "paused"
    assert paused["downloaded"] == 1
    assert paused["remaining"] == 1
    assert len(calls) == 1
    manifest_path = tmp_path / "data/ticks/history/download_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert list(manifest["chunks"]) == calls
    assert next(iter(manifest["chunks"].values()))["status"] == "done"

    resumed = run_download(
        settings,
        api_factory=_FakeApi,
        downloader_factory=fake_download,
        clock=lambda: datetime(2026, 10, 14, 15, 30),
    )

    assert resumed["status"] == "done"
    assert resumed["skipped"] == 1
    assert resumed["downloaded"] == 1
    assert len(calls) == 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["chunks"]) == 2
    assert all(item["status"] == "done" for item in manifest["chunks"].values())


class _StepClock:
    """按顺序返回时间，用完后停在最后一个值。"""

    def __init__(self, *times: datetime) -> None:
        self._times = list(times)
        self._last = times[-1]

    def __call__(self) -> datetime:
        if self._times:
            self._last = self._times.pop(0)
        return self._last


def test_auto_resume_waits_for_close_then_finishes(tmp_path, monkeypatch) -> None:
    from src.data.tick import downloader as downloader_module

    settings = download_settings(_cfg(tmp_path))
    _use_day_session(settings)
    assert settings["auto_resume"] is True
    assert settings["resume_poll_sec"] == 60.0
    monkeypatch.setattr(
        downloader_module, "synthesize_symbol", lambda *a, **k: {"kept": 0, "bars": {}}
    )
    calls: list[str] = []
    sleeps: list[float] = []
    events: list[str] = []

    def fake_download(api, chunk) -> None:
        calls.append(chunk.key)
        _write_csv(chunk)

    summary = run_download_auto_resume(
        settings,
        api_factory=_FakeApi,
        downloader_factory=fake_download,
        clock=_StepClock(
            datetime(2026, 10, 14, 9, 5),    # 首轮建立连接前：盘中
            datetime(2026, 10, 14, 9, 6),    # 写等待状态
            datetime(2026, 10, 14, 9, 7),    # 等待循环：仍盘中，睡一轮
            datetime(2026, 10, 14, 15, 1),   # 收盘后自动恢复
        ),
        sleep=sleeps.append,
        on_pause=lambda message: events.append("pause"),
        on_wake=lambda: events.append("wake"),
    )

    assert sleeps == [60.0]
    assert events == ["pause", "wake"]
    assert summary["rounds"] == 2
    assert summary["status"] == "done"
    assert summary["downloaded"] == 2
    assert len(calls) == 2
    status = json.loads(
        (tmp_path / "data/ticks/history/download_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "done"


def test_auto_resume_stop_keeps_breakpoint(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))
    _use_day_session(settings)
    created = False

    def api_factory():
        nonlocal created
        created = True
        return _FakeApi()

    summary = run_download_auto_resume(
        settings,
        api_factory=api_factory,
        downloader_factory=lambda *a, **k: pytest.fail("停止后不应下载"),
        clock=lambda: datetime(2026, 10, 14, 9, 30),
        sleep=lambda seconds: None,
        stop=lambda: True,
    )

    assert created is False
    assert summary["status"] == "paused"
    assert summary["resume_stopped"] is True
    status = json.loads(
        (tmp_path / "data/ticks/history/download_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "paused"
    assert "断点" in status["message"]


def test_auto_resume_disabled_returns_paused_immediately(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))
    _use_day_session(settings)
    settings["auto_resume"] = False
    sleeps: list[float] = []

    summary = run_download_auto_resume(
        settings,
        api_factory=_FakeApi,
        downloader_factory=lambda *a, **k: pytest.fail("盘中不应下载"),
        clock=lambda: datetime(2026, 10, 14, 9, 30),
        sleep=sleeps.append,
    )

    assert summary["status"] == "paused"
    assert "resume_stopped" not in summary
    assert sleeps == []


def test_auto_resume_dry_run_does_not_wait(tmp_path) -> None:
    settings = download_settings(_cfg(tmp_path))
    _use_day_session(settings)
    sleeps: list[float] = []

    summary = run_download_auto_resume(
        settings,
        api_factory=_FakeApi,
        dry_run=True,
        clock=lambda: datetime(2026, 10, 14, 9, 30),
        sleep=sleeps.append,
    )

    assert summary["status"] == "dry_run"
    assert sleeps == []


def test_wait_update_permission_error_is_persistent(tmp_path, monkeypatch) -> None:
    import tqsdk.tools

    class _Task:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def is_finished(self) -> bool:
            return False

    class _DeniedApi(_FakeApi):
        def wait_update(self) -> None:
            raise RuntimeError("当前账号不支持下载历史数据，请检查 tq_dl 权限")

    monkeypatch.setattr(tqsdk.tools, "DataDownloader", _Task)
    settings = download_settings(_cfg(tmp_path))

    with pytest.raises(DownloadPermissionError, match="专业版"):
        run_download(settings, api_factory=_DeniedApi)

    status = json.loads(
        (tmp_path / "data/ticks/history/download_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "permission_denied"


class _Var:
    value: object = ""

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class _Label:
    def __init__(self) -> None:
        self.options: dict = {}

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)


def _ui_app(tmp_path) -> tuple:
    from src.ui.data_tab import DataTabMixin

    app = DataTabMixin()
    app.cfg = _cfg(tmp_path)
    app.tick_status_var = _Var()
    app.tick_inventory_var = _Var()
    app.tick_backfill_var = _Var()
    app.tick_backfill_label = _Label()
    app.tick_toggle_btn = None
    app._tick_recorder = None
    app._tick_download_busy = False
    return app, download_settings(_cfg(tmp_path))


def test_permission_warning_survives_tick_card_refresh(tmp_path) -> None:
    from src.data.tick.downloader import _write_json
    from src.ui import theme as T

    app, settings = _ui_app(tmp_path)
    _write_json(
        settings["out_dir"] / "download_status.json",
        {"state": "permission_denied", "message": "需要专业版"},
    )

    app._refresh_tick_card()
    app._refresh_tick_card()

    assert "免费账号请保持实时 tick 累积" in app.tick_backfill_var.value
    assert app.tick_backfill_label.options["text_color"] == T.WARN


def test_tick_auto_switch_persists_enabled(tmp_path, monkeypatch) -> None:
    from src.utils import config_loader

    app, _settings = _ui_app(tmp_path)
    app.tick_auto_var = _Var()
    app._dm_write = lambda text: None
    app._refresh_tick_card = lambda: None
    started: list[bool] = []
    app._auto_start_tick_if_enabled = lambda: started.append(True)
    saved: list[bool] = []

    def fake_save(cfg):
        saved.append(bool(cfg["data"]["tick"]["enabled"]))
        return tmp_path / "config.yaml"

    monkeypatch.setattr(config_loader, "save_config", fake_save)

    app.tick_auto_var.set(True)
    app._on_toggle_tick_auto()
    app.tick_auto_var.set(False)
    app._on_toggle_tick_auto()

    assert saved == [True, False]
    assert started == [True]


def test_waiting_resume_hint_after_restart(tmp_path) -> None:
    from src.data.tick.downloader import _write_json
    from src.ui import theme as T

    app, settings = _ui_app(tmp_path)
    _write_json(
        settings["out_dir"] / "download_status.json",
        {"state": "waiting_resume", "message": "已安排收盘后自动继续"},
    )

    app._refresh_tick_card()

    assert "点「补齐历史 tick」继续" in app.tick_backfill_var.value
    assert app.tick_backfill_label.options["text_color"] == T.WARN
