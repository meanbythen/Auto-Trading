"""
runner.py 启动检查、健康闸门与看门狗回调的单元测试。

这些方法只读配置和少量运行状态，用轻量 Fake 宿主直接调用，
不启动主循环、不连交易通道。
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.scheduler.runner import TradingBot


class _FakeRisk:
    def __init__(self) -> None:
        self.halted = False
        self.halt_kind = ""
        self.halt_reason = ""
        self.calls: list[tuple[str, str]] = []

    def set_halt(self, kind: str, reason: str) -> None:
        self.halted = True
        self.halt_kind = kind
        self.halt_reason = reason
        self.calls.append(("set", kind))

    def clear_halt(self, kind: str) -> None:
        self.halted = False
        self.halt_kind = ""
        self.calls.append(("clear", kind))


class _Calendar:
    def __init__(self, covered: date) -> None:
        self._covered = covered

    def covered_until(self) -> date:
        return self._covered


def _cfg(root: Path, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "dry_run": True,
        "trade": {"symbol": "bu2612", "executor": "sim", "volume": 1},
        "strategy": {"engine": "daily_body", "timeframe": "5", "symbols": ["bu2612"]},
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(root),
    }
    cfg.update(overrides)
    return cfg


def _live_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TradingBot:
    """按 CTP 实盘口径调用检查，但用 sim 通道构造，避免加载柜台 DLL。"""
    bot = TradingBot(_cfg(tmp_path))
    bot.cfg["trade"]["executor"] = "ctp"
    bot.cfg["dry_run"] = False
    bot.risk = _FakeRisk()
    return bot


def _events(tmp_path: Path) -> str:
    path = tmp_path / "data" / "events.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_session_skip_logged_once_per_pause(tmp_path, monkeypatch) -> None:
    """同一段连续暂停只记一次「本轮不拉K线」，不再每分钟刷屏。"""
    from loguru import logger

    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr(bot, "_maybe_run_session_audit", lambda: None)
    monkeypatch.setattr(bot, "_scan_session_active", lambda: False)
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    try:
        for _ in range(3):
            bot.scan_once()
    finally:
        logger.remove(sink)
    hits = [m for m in messages if "本轮不拉K线" in m]
    assert len(hits) == 1
    assert bot._session_skip_active is True


def test_session_skip_resets_after_reentering_session(tmp_path, monkeypatch) -> None:
    """重新进入交易时段后重置标志，下一段暂停时再记一次。"""
    from loguru import logger

    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr(bot, "_maybe_run_session_audit", lambda: None)
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    try:
        # 第一段暂停：记一次。
        monkeypatch.setattr(bot, "_scan_session_active", lambda: False)
        bot.scan_once()
        assert bot._session_skip_active is True
        # 进入交易时段：走扫描分支并重置标志；mock 登录避免连通道。
        monkeypatch.setattr(bot, "_scan_session_active", lambda: True)
        monkeypatch.setattr(bot, "do_login", lambda: None)
        bot.scan_once()
        assert bot._session_skip_active is False
        # 第二段暂停：应再记一次。
        monkeypatch.setattr(bot, "_scan_session_active", lambda: False)
        bot.scan_once()
    finally:
        logger.remove(sink)
    hits = [m for m in messages if "本轮不拉K线" in m]
    assert len(hits) == 2


def test_holiday_min_cover_days_falls_back_on_bad_config(tmp_path) -> None:
    bot = TradingBot(_cfg(tmp_path))
    assert bot._holiday_min_cover_days() == 60

    bot.cfg["schedule"]["holiday_min_cover_days"] = 45
    assert bot._holiday_min_cover_days() == 45

    bot.cfg["schedule"]["holiday_min_cover_days"] = -3
    assert bot._holiday_min_cover_days() == 0

    bot.cfg["schedule"]["holiday_min_cover_days"] = "abc"
    assert bot._holiday_min_cover_days() == 60


def test_holiday_check_skips_dry_run_and_missing_calendar(tmp_path, monkeypatch) -> None:
    bot = _live_bot(tmp_path, monkeypatch)
    assert bot._check_holiday_calendar() is True  # 实盘口径但默认 dry_run
    assert isinstance(bot.risk, _FakeRisk)

    bot.cfg["dry_run"] = False
    bot.holiday_calendar = None
    assert bot._check_holiday_calendar() is True


def test_holiday_check_clears_old_halt_when_covered(tmp_path, monkeypatch) -> None:
    bot = _live_bot(tmp_path, monkeypatch)
    bot.holiday_calendar = _Calendar(date.today().replace(year=date.today().year + 1))
    bot.risk.halted = True
    bot.risk.halt_kind = "holiday_calendar_missing"
    monkeypatch.setattr("src.scheduler.runner.calendar_coverage_ok", lambda cal, min_days: True)

    assert bot._check_holiday_calendar() is True
    assert bot.risk.calls == [("clear", "holiday_calendar_missing")]


def test_holiday_check_halts_once_and_refreshes_reason(tmp_path, monkeypatch) -> None:
    bot = _live_bot(tmp_path, monkeypatch)
    bot.holiday_calendar = _Calendar(date(2026, 12, 31))
    monkeypatch.setattr("src.scheduler.runner.calendar_coverage_ok", lambda cal, min_days: False)
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.ops.alerts.emit_alert",
        lambda name, message, level="info": alerts.append((name, level)),
    )

    assert bot._check_holiday_calendar() is False
    assert bot.risk.calls == [("set", "holiday_calendar_missing")]
    assert alerts == [("holiday_calendar_missing", "error")]
    assert '"holiday_calendar_missing"' in _events(tmp_path)

    # 同一条限制再来一次：只刷新原因，不重复写事件和告警。
    bot.risk.halt_reason = "旧原因"
    assert bot._check_holiday_calendar() is False
    assert bot.risk.halt_reason != "旧原因"
    assert bot.risk.calls == [("set", "holiday_calendar_missing")]
    assert alerts == [("holiday_calendar_missing", "error")]


def test_holiday_check_survives_alert_failure(tmp_path, monkeypatch) -> None:
    bot = _live_bot(tmp_path, monkeypatch)
    bot.holiday_calendar = _Calendar(date(2026, 12, 31))
    monkeypatch.setattr("src.scheduler.runner.calendar_coverage_ok", lambda cal, min_days: False)
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("alert")))

    assert bot._check_holiday_calendar() is False


def test_guardian_actions_ignores_stale_and_broken_files(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    bot.state_path = str(tmp_path / "state.json")
    Path(bot.state_path).write_text("{}", encoding="utf-8")
    actions = tmp_path / "data" / "guardian_actions.jsonl"
    actions.parent.mkdir(parents=True, exist_ok=True)

    bot._check_guardian_actions()  # 文件不存在

    actions.write_text('{"ts": "2026-11-24 09:00", "run_id": "r1"}\n', encoding="utf-8")
    os.utime(actions, (1_000_000_000, 1_000_000_000))
    os.utime(bot.state_path, (2_000_000_000, 2_000_000_000))
    bot._check_guardian_actions()  # 动作记录比状态文件旧
    assert "guardian_flatten_seen" not in _events(tmp_path)

    actions.write_text("not json\n", encoding="utf-8")
    os.utime(bot.state_path, (1_000_000_000, 1_000_000_000))
    os.utime(actions, (2_000_000_000, 2_000_000_000))
    bot._check_guardian_actions()  # 坏行只记日志
    assert "guardian_flatten_seen" not in _events(tmp_path)


def test_guardian_actions_reports_recent_flatten(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    bot.state_path = str(tmp_path / "state.json")
    Path(bot.state_path).write_text("{}", encoding="utf-8")
    actions = tmp_path / "data" / "guardian_actions.jsonl"
    actions.parent.mkdir(parents=True, exist_ok=True)
    actions.write_text(
        json.dumps({"ts": "2026-11-24 09:00:00", "run_id": "20261124-090000-1-abcd"}) + "\n",
        encoding="utf-8",
    )
    os.utime(bot.state_path, (1_000_000_000, 1_000_000_000))
    os.utime(actions, (2_000_000_000, 2_000_000_000))
    alerts: list[str] = []
    monkeypatch.setattr(
        "src.ops.alerts.emit_alert", lambda name, message, level="info": alerts.append(name)
    )

    bot._check_guardian_actions()

    assert '"guardian_flatten_seen"' in _events(tmp_path)
    assert alerts == ["guardian_flatten"]


def test_health_gate_updates_block_flag(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    report = SimpleNamespace(as_dict=lambda: {"score": 30, "level": "bad"})
    monkeypatch.setattr("src.ops.health.maybe_emit_health", lambda *a, **k: report)
    monkeypatch.setattr(
        "src.ops.health.entry_block_decision", lambda cfg, rep: (True, "健康分过低")
    )

    bot._maybe_check_health()

    assert bot._health_blocked is True
    assert bot._health_block_reason == "健康分过低"
    assert bot._health_report == {
        "score": 30,
        "level": "bad",
        "entry_blocked": True,
        "message": "健康分过低",
    }


def test_health_gate_allows_when_decision_passes(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    report = SimpleNamespace(as_dict=lambda: {"score": 90})
    monkeypatch.setattr("src.ops.health.maybe_emit_health", lambda *a, **k: report)
    monkeypatch.setattr("src.ops.health.entry_block_decision", lambda cfg, rep: (False, ""))

    bot._maybe_check_health()

    assert bot._health_blocked is False
    assert "message" not in (bot._health_report or {})


def test_health_gate_swallows_errors(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    bot._health_report = {"old": True}
    monkeypatch.setattr(
        "src.ops.health.maybe_emit_health", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )

    bot._maybe_check_health()

    assert bot._health_report == {"old": True}


def test_watchdog_snapshot_shape(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    bot.last_scan_at = "2026-11-24 10:00:00"
    bot.sessions = [["09:00", "15:00"]]
    monkeypatch.setattr("src.scheduler.runner.snapshot_positions", lambda strategy: [{"symbol": "bu"}])

    snap = bot._watchdog_snapshot()

    assert snap["last_scan_at"] == "2026-11-24 10:00:00"
    assert snap["positions"] == [{"symbol": "bu"}]
    assert isinstance(snap["in_session"], bool)


def test_watchdog_stale_pauses_and_alerts(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    paused: list[bool] = []
    monkeypatch.setattr(bot, "set_orders_paused", paused.append)
    bot.risk = _FakeRisk()
    flags: list[str] = []
    monkeypatch.setattr(bot, "_write_unprotected_flag", flags.append)
    alerts: list[str] = []
    monkeypatch.setattr(
        "src.ops.alerts.emit_alert", lambda name, message, level="info": alerts.append(name)
    )

    bot._on_watchdog_stale({"age_sec": 42})

    assert paused == [True]
    assert bot.risk.calls == [("set", "stale_heartbeat")]
    assert flags == ["watchdog_stale"]
    assert alerts == ["heartbeat_stale"]
    assert '"watchdog_stale"' in _events(tmp_path)


def test_watchdog_stale_survives_alert_failure(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr(bot, "set_orders_paused", lambda value: None)
    bot.risk = _FakeRisk()
    monkeypatch.setattr(bot, "_write_unprotected_flag", lambda reason: None)
    monkeypatch.setattr(
        "src.ops.alerts.emit_alert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("alert"))
    )

    bot._on_watchdog_stale({})

    assert bot.risk.calls == [("set", "stale_heartbeat")]
    assert '"watchdog_stale"' in _events(tmp_path)
