"""run_id 贯穿：事件、运行快照、状态文件。"""

from __future__ import annotations

import json

from src.scheduler.runner import TradingBot
from src.utils.state_store import default_state_path, load_state


def _cfg(root, *, executor: str = "sim", dry_run: bool = True) -> dict:
    return {
        "dry_run": dry_run,
        "trade": {"symbol": "bu2612", "executor": executor, "volume": 1, "gui": {}},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(root),
    }


def _events(root) -> list[dict]:
    path = root / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_events_snapshot_and_state_carry_run_id(tmp_path) -> None:
    bot = TradingBot(_cfg(tmp_path))
    bot._record_event("scan_tick", symbol="bu2612")
    bot._persist_state()

    rows = _events(tmp_path)
    assert rows and rows[-1]["run_id"] == bot.run_id
    assert rows[-1]["event"] == "scan_tick"
    assert rows[-1]["symbol"] == "bu2612"

    snap = bot.status_snapshot()
    assert snap["run_id"] == bot.run_id
    assert snap["started_at"] == bot.started_at

    state = load_state(default_state_path(tmp_path))
    assert state["run_id"] == bot.run_id
    assert state["started_at"] == bot.started_at
    assert int(state["pid"]) > 0


def test_run_id_format_and_uniqueness(tmp_path) -> None:
    """形如 20261115-093000-1234-abcd；同秒建两个实例也不能撞号。"""
    first = TradingBot(_cfg(tmp_path / "a"))
    second = TradingBot(_cfg(tmp_path / "b"))
    assert first.run_id.count("-") == 3
    assert first.run_id != second.run_id


def test_alerts_carry_run_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    from src.ops import alerts

    alerts.set_run_id("20261115-093000-1234-abcd")
    try:
        assert alerts.emit_alert("ctp_down", "柜台断开", level="error", root=tmp_path) is True
    finally:
        alerts.set_run_id("")
    row = alerts.last_alert(tmp_path)
    assert row is not None
    assert row["run_id"] == "20261115-093000-1234-abcd"
