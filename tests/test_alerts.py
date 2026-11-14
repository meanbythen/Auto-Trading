"""运行告警：熔断要落文件，相同内容短时间不刷第二遍。"""

from __future__ import annotations

from src.ops.alerts import emit_alert, last_alert
from src.risk.guardian import RiskGuardian


def test_emit_alert_writes_and_cools_down(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    assert emit_alert("halt", "当日亏损超限", level="error", root=tmp_path) is True
    assert emit_alert("halt", "当日亏损超限", level="error", root=tmp_path) is False
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "halt"
    assert "超限" in row["message"]


def test_guardian_halt_emits_alert(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    risk = RiskGuardian(max_daily_loss=100)
    risk.update_pnl(-120)
    assert risk.halted is True
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "halt"


def test_guardian_can_skip_writing_alerts(tmp_path, monkeypatch) -> None:
    """回测熔断只停开仓，不往运行台告警文件里写。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    risk = RiskGuardian(max_daily_loss=50, write_alerts=False)
    risk.update_pnl(-80)
    assert risk.halted is True
    assert last_alert(tmp_path) is None


def test_stale_halt_with_old_limit_is_deleted(tmp_path, monkeypatch) -> None:
    """配置页已经改成 3000 时，文件里「限额 100」的熔断行要删掉，红字改看后面那条。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("reconcile", "启动对账已校准", root=tmp_path)
    emit_alert("halt", "当日亏损 -150.00 已超过限额 100", level="error", root=tmp_path)
    leftover = last_alert(tmp_path, max_daily_loss=3000)
    assert leftover is not None
    assert leftover["code"] == "reconcile"
    from src.ops.alerts import alerts_path

    text = alerts_path(tmp_path).read_text(encoding="utf-8")
    assert "限额 100" not in text
    assert "启动对账" in text


def test_halt_matching_current_limit_is_kept(tmp_path, monkeypatch) -> None:
    """真按现在限额熔断的那条要留下，不能误删。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("halt", "当日亏损 -3100.00 已超过限额 3000", level="error", root=tmp_path)
    row = last_alert(tmp_path, max_daily_loss=3000)
    assert row is not None
    assert row["code"] == "halt"
    assert "3000" in row["message"]
