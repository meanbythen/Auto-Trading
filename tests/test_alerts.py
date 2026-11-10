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
