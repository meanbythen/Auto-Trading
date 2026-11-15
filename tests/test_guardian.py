"""冷备守护：只在「主进程真的没了」且满足全部条件时平仓，其余情况只告警或不动。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from src.utils.state_store import default_state_path
from tools import guardian

NOW = datetime(2026, 11, 16, 10, 0)


def _cfg(
    tmp_path: Path,
    *,
    enabled: bool = True,
    sessions: list[list[str]] | None = None,
    ctp: bool = True,
) -> dict:
    trade: dict = {"symbol": "bu2612", "executor": "ctp"}
    if ctp:
        trade["ctp"] = {
            "td_front": "tcp://127.0.0.1:1",
            "broker_id": "9999",
            "user_id": "test",
            "resolved_password": "pw",
            "app_id": "client_AutoTrading_1.0",
            "resolved_auth_code": "auth",
        }
    return {
        "trade": trade,
        "schedule": {"trading_sessions": sessions or [["00:00", "23:59"]]},
        "runtime": {
            "guardian": {
                "enabled": enabled,
                "poll_sec": 10,
                "stale_after_sec": 180,
                "max_close_attempts": 3,
            }
        },
        "_project_root": str(tmp_path),
    }


def _write_state(
    tmp_path: Path,
    *,
    heartbeat: datetime | None = None,
    pid: int = 12345,
    positions: dict | None = None,
    run_id: str = "20261116-093000-1234-abcd",
) -> None:
    stamp = heartbeat or (NOW - timedelta(seconds=600))
    path = default_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": pid,
                "heartbeat_at": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "positions": {"bu2612": {"side": "long", "volume": 1}}
                if positions is None
                else positions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_guard(tmp_path: Path, cfg: dict, *, alive: bool = False, now: datetime = NOW):
    calls: list[list[str]] = []
    alerts: list[tuple[str, str]] = []

    def fake_flatten(symbols: list[str]) -> dict[str, str]:
        calls.append(list(symbols))
        return {symbol: "已按对手价报单" for symbol in symbols}

    guard = guardian.Guardian(
        cfg,
        tmp_path,
        pid_alive_fn=lambda pid: alive,
        flatten_fn=fake_flatten,
        now_fn=lambda: now,
        alert_fn=lambda code, message: alerts.append((code, message)),
    )
    return guard, calls, alerts


def _actions(tmp_path: Path) -> list[dict]:
    path = tmp_path / "data" / guardian.ACTIONS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_config_defaults_and_overrides() -> None:
    default = guardian.GuardianConfig.from_cfg({})
    assert default.enabled is False
    assert default.poll_sec == 10.0
    assert default.stale_after_sec == 180.0
    assert default.max_close_attempts == 3

    parsed = guardian.GuardianConfig.from_cfg(
        {"runtime": {"guardian": {"enabled": "true", "poll_sec": 0, "max_close_attempts": 0}}}
    )
    assert parsed.enabled is True
    assert parsed.poll_sec == 1.0
    assert parsed.max_close_attempts == 1


def test_disabled_does_nothing(tmp_path: Path) -> None:
    _write_state(tmp_path)
    guard, calls, _ = _make_guard(tmp_path, _cfg(tmp_path, enabled=False))
    assert guard.check_once() == "disabled"
    assert calls == []


def test_fresh_heartbeat_does_nothing(tmp_path: Path) -> None:
    _write_state(tmp_path, heartbeat=NOW - timedelta(seconds=10))
    guard, calls, _ = _make_guard(tmp_path, _cfg(tmp_path))
    assert guard.check_once() == "fresh"
    assert calls == []


def test_flat_positions_do_nothing(tmp_path: Path) -> None:
    _write_state(tmp_path, positions={})
    guard, calls, _ = _make_guard(tmp_path, _cfg(tmp_path))
    assert guard.check_once() == "flat"
    assert calls == []


def test_alive_main_only_alerts_once(tmp_path: Path) -> None:
    """PID 还在但心跳停了：只能告警，绝不能连柜台把主进程踢下线。"""
    _write_state(tmp_path)
    guard, calls, alerts = _make_guard(tmp_path, _cfg(tmp_path), alive=True)
    assert guard.check_once() == "stale_main"
    assert calls == []
    assert alerts and alerts[0][0] == "guardian_stale_main"
    assert len(alerts) == 1
    assert guard.check_once() == "stale_main"
    assert len(alerts) == 1


def test_off_session_does_nothing(tmp_path: Path) -> None:
    _write_state(tmp_path)
    cfg = _cfg(tmp_path, sessions=[["09:00", "10:00"]])
    guard, calls, _ = _make_guard(tmp_path, cfg, now=datetime(2026, 11, 16, 12, 0))
    assert guard.check_once() == "off_session"
    assert calls == []


def test_missing_ctp_config_does_nothing(tmp_path: Path) -> None:
    _write_state(tmp_path)
    guard, calls, _ = _make_guard(tmp_path, _cfg(tmp_path, ctp=False))
    assert guard.check_once() == "no_ctp"
    assert calls == []


def test_pid_gone_flattens_once(tmp_path: Path) -> None:
    _write_state(tmp_path)
    guard, calls, alerts = _make_guard(tmp_path, _cfg(tmp_path))
    assert guard.check_once() == "flattened"
    assert calls == [["bu2612"]]
    assert alerts[-1][0] == "guardian_flatten"
    rows = _actions(tmp_path)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "20261116-093000-1234-abcd"
    assert rows[0]["action"] == "flatten"
    # 同一轮运行只平一次。
    assert guard.check_once() == "already_handled"
    assert len(calls) == 1
    assert len(_actions(tmp_path)) == 1


def test_guardian_restart_does_not_reflatten(tmp_path: Path) -> None:
    """守护自己重启后，看到动作记录里的 run_id 就不再重复发单。"""
    _write_state(tmp_path)
    first, calls, _ = _make_guard(tmp_path, _cfg(tmp_path))
    assert first.check_once() == "flattened"

    second, calls2, _ = _make_guard(tmp_path, _cfg(tmp_path))
    assert second.check_once() == "already_handled"
    assert calls2 == []
    assert len(_actions(tmp_path)) == 1


def test_pid_alive_self() -> None:
    assert guardian.pid_alive(os.getpid()) is True
    assert guardian.pid_alive(-1) is False
