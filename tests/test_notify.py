"""告警外发：配置解析、级别过滤、限流、队列丢弃、失败不影响本地写入。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from src.ops import notify
from src.ops.alerts import emit_alert, last_alert


@pytest.fixture(autouse=True)
def _fresh_notify():
    notify.reset()
    yield
    notify.reset()


def _cfg(**overrides: Any) -> dict[str, Any]:
    alerts = {
        "external": "webhook",
        "min_level": "error",
        "rate_limit_per_hour": 10,
        "webhook": {"kind": "feishu", "url": "http://example.test/hook", "timeout_sec": 5},
    }
    alerts.update(overrides)
    return {"runtime": {"alerts": alerts}}


def _wait(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_default_is_off() -> None:
    cfg = notify.NotifyConfig.from_cfg({})
    assert cfg.enabled is False
    assert notify.notify({"level": "error", "code": "halt", "message": "x"}) is False


def test_empty_url_disables_it() -> None:
    cfg = notify.NotifyConfig.from_cfg(_cfg(webhook={"kind": "feishu", "url": ""}))
    assert cfg.enabled is False
    assert "url" in cfg.problem


def test_unknown_channel_is_disabled() -> None:
    cfg = notify.NotifyConfig.from_cfg(_cfg(external="smtp"))
    assert cfg.enabled is False
    assert cfg.problem


def test_level_filter(monkeypatch) -> None:
    monkeypatch.setattr(notify, "_post_json", lambda *args, **kwargs: True)
    notify.configure(_cfg())
    assert notify.notify({"level": "warning", "code": "strategy_health", "message": "w"}) is False
    assert notify.notify({"level": "error", "code": "halt", "message": "e"}) is True


def test_rate_limit_per_code(monkeypatch) -> None:
    monkeypatch.setattr(notify, "_post_json", lambda *args, **kwargs: True)
    notify.configure(_cfg(rate_limit_per_hour=2))
    rows = [{"level": "error", "code": "halt", "message": f"m{i}"} for i in range(3)]
    assert [notify.notify(row) for row in rows] == [True, True, False]
    # 不同 code 各自计数。
    assert notify.notify({"level": "error", "code": "ctp_down", "message": "other"}) is True


def test_queue_full_drops_oldest(monkeypatch) -> None:
    notify.reset(queue_size=1)
    started = threading.Event()
    release = threading.Event()

    def slow(url, payload, timeout):
        started.set()
        release.wait(2)
        return True

    monkeypatch.setattr(notify, "_post_json", slow)
    notify.configure(_cfg())
    assert notify.notify({"level": "error", "code": "halt", "message": "1"}) is True
    assert started.wait(2)
    assert notify.notify({"level": "error", "code": "halt", "message": "2"}) is True
    assert notify.notify({"level": "error", "code": "halt", "message": "3"}) is True
    assert notify.stats()["dropped"] == 1
    release.set()


def test_feishu_payload(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake(url, payload, timeout):
        captured.update(url=url, payload=payload, timeout=timeout)
        return True

    monkeypatch.setattr(notify, "_post_json", fake)
    notify.configure(_cfg())
    row = {
        "level": "error",
        "code": "halt",
        "message": "当日亏损超限",
        "ts": "2026-11-15 09:30:00",
        "run_id": "20261115-093000-1234-abcd",
    }
    assert notify.notify(row) is True
    assert _wait(lambda: notify.stats()["sent"] == 1)
    payload = captured["payload"]
    assert payload["msg_type"] == "text"
    text = payload["content"]["text"]
    assert "halt" in text and "当日亏损超限" in text
    assert "abcd" in text


def test_delivery_failure_keeps_local_alert(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(notify, "_post_json", lambda *args, **kwargs: False)
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    notify.configure(_cfg())

    started = time.monotonic()
    assert emit_alert("ctp_down", "柜台断开", level="error", root=tmp_path) is True
    # 外发在后台线程，emit_alert 不能等网络。
    assert time.monotonic() - started < 1.0
    row = last_alert(tmp_path)
    assert row is not None and row["code"] == "ctp_down"

    assert _wait(lambda: notify.stats()["failed"] >= 1)
    lines = (tmp_path / "data" / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
    codes = [json.loads(line)["code"] for line in lines if line.strip()]
    assert "notify_failed" in codes
