"""运行告警：熔断要落文件，相同内容短时间不刷第二遍；界面读告警不能改文件。"""

from __future__ import annotations

from src.ops.alerts import (
    active_alert,
    alerts_path,
    emit_alert,
    emit_recovery,
    last_alert,
    prune_stale_halt_alerts,
)
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


def test_stale_halt_hidden_without_touching_file(tmp_path, monkeypatch) -> None:
    """last_alert 只过滤旧限额熔断行，不删文件；清理交给显式 prune。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("reconcile", "启动对账已校准", root=tmp_path)
    emit_alert("halt", "当日亏损 -150.00 已超过限额 100", level="error", root=tmp_path)
    path = alerts_path(tmp_path)
    before = path.read_text(encoding="utf-8")
    before_mtime = path.stat().st_mtime_ns

    leftover = last_alert(tmp_path, max_daily_loss=3000)
    assert leftover is not None
    assert leftover["code"] == "reconcile"
    assert path.read_text(encoding="utf-8") == before
    assert path.stat().st_mtime_ns == before_mtime

    assert prune_stale_halt_alerts(tmp_path, 3000) == 1
    text = path.read_text(encoding="utf-8")
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


def test_ctp_down_alert_marked_recovered(tmp_path, monkeypatch) -> None:
    """CTP 连上后，最近告警还是那条断线，但必须标成已恢复，文件里两条都在。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("ctp_down", "CTP 没有连上：前置连不上", level="error", root=tmp_path)
    assert last_alert(tmp_path).get("recovered") is None

    assert emit_recovery("ctp_up", "CTP 已连上：登录成功", root=tmp_path) is True
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "ctp_down"
    assert row["recovered"] is True
    text = alerts_path(tmp_path).read_text(encoding="utf-8")
    assert '"code": "ctp_up"' in text
    assert '"code": "ctp_down"' in text


def test_ctp_down_after_recovery_shows_again(tmp_path, monkeypatch) -> None:
    """恢复之后再断线，新告警不能带已恢复标记。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("ctp_down", "CTP 没有连上：前置连不上", level="error", root=tmp_path)
    emit_recovery("ctp_up", "CTP 已连上：登录成功", root=tmp_path)
    emit_alert("ctp_down", "CTP 没有连上：柜台断线", level="error", root=tmp_path)
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "ctp_down"
    assert "柜台断线" in row["message"]
    assert not row.get("recovered")


def test_recovery_only_affects_its_own_codes(tmp_path, monkeypatch) -> None:
    """恢复 CTP 只管 CTP；之后新来的对账告警照常显示为未恢复。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("ctp_down", "CTP 没有连上：前置连不上", level="error", root=tmp_path)
    emit_recovery("ctp_up", "CTP 已连上：登录成功", root=tmp_path)
    emit_alert("reconcile", "启动对账已校准", root=tmp_path)
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "reconcile"
    assert not row.get("recovered")


def test_window_lost_alert_marked_recovered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("window_lost", "交易软件窗口丢失，本轮跳过扫描", level="error", root=tmp_path)
    emit_recovery("window_up", "交易软件窗口已恢复", root=tmp_path)
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "window_lost"
    assert row["recovered"] is True


def test_leg_margin_alert_marked_recovered(tmp_path, monkeypatch) -> None:
    """保证金比例补齐、分配恢复后，缺比例告警标成已恢复。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("leg_margin_missing", "缺保证金比例，lh2703 开仓被拒绝", root=tmp_path)
    emit_recovery("leg_alloc_ok", "组合保证金分配已恢复：fu2701 2手 / lh2703 1手", root=tmp_path)
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "leg_margin_missing"
    assert row["recovered"] is True


def test_default_root_keeps_script_alerts_out_of_live_dir(tmp_path, monkeypatch) -> None:
    """没显式传 root 的告警写本进程默认目录，不再落到项目实盘数据里。"""
    from src.ops import alerts as alerts_module

    decoy = tmp_path / "project"
    decoy.mkdir()
    monkeypatch.setattr("src.utils.paths.project_root", lambda: decoy)
    bot_root = tmp_path / "bot"
    alerts_module.set_default_root(bot_root)
    assert alerts_module.emit_alert("ctp_down", "CTP 没有连上：演示", level="error") is True
    assert (bot_root / "data" / "alerts.jsonl").exists()
    assert not (decoy / "data" / "alerts.jsonl").exists()


def test_last_alert_only_reads_given_run(tmp_path, monkeypatch) -> None:
    """别的运行（脚本/测试/旧进程）写的告警和恢复行都不参与本次显示。"""
    from src.ops import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "_last_emit", {})
    alerts_module.set_run_id("run-a")
    emit_alert("exit_with_positions", "退出时仍有持仓或挂单", level="error", root=tmp_path)
    alerts_module.set_run_id("run-b")
    emit_recovery("reconcile_ok", "启动对账完成", root=tmp_path)

    assert last_alert(tmp_path, run_id="run-b") is None
    row = last_alert(tmp_path, run_id="run-a")
    assert row is not None
    assert row["code"] == "exit_with_positions"
    assert not row.get("recovered")


def test_active_alert_finds_unresolved_alert_behind_newer_recovery(tmp_path, monkeypatch) -> None:
    """更晚的 CTP 恢复不影响更早、仍未恢复的缺比例告警被找到。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    emit_alert("leg_margin_missing", "缺保证金比例，lh2703 开仓被拒绝", root=tmp_path)
    emit_alert("ctp_down", "CTP 没有连上：前置连不上", level="error", root=tmp_path)
    emit_recovery("ctp_up", "CTP 已连上：登录成功", root=tmp_path)

    row = active_alert(tmp_path, codes=("leg_margin_missing", "leg_alloc_overflow"))
    assert row is not None
    assert row["code"] == "leg_margin_missing"

    emit_recovery("leg_alloc_ok", "组合保证金分配已恢复", root=tmp_path)
    assert active_alert(tmp_path, codes=("leg_margin_missing", "leg_alloc_overflow")) is None


def test_halt_alert_written_once_per_episode(tmp_path, monkeypatch) -> None:
    """同一段熔断里盈亏怎么变，都只在文件里留一条告警。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    risk = RiskGuardian(max_daily_loss=100)
    risk.update_pnl(-120)
    risk.set_daily_pnl(-500)
    risk.update_pnl(-10)

    assert risk.halted is True
    text = alerts_path(tmp_path).read_text(encoding="utf-8")
    assert text.count('"code": "halt"') == 1


def test_set_max_daily_loss_clears_halt_when_no_longer_breached(tmp_path, monkeypatch) -> None:
    """把限额从 100 调到 3000 后，当日亏损 -150 不再超限，熔断要解除。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    risk = RiskGuardian(max_daily_loss=100)
    risk.set_daily_pnl(-150)
    assert risk.halted is True

    risk.set_max_daily_loss(3000)
    assert risk.max_daily_loss == 3000
    assert risk.halted is False
    assert risk.halt_reason == ""
    assert risk.halt_alerted is False


def test_set_max_daily_loss_keeps_halt_when_still_breached(tmp_path, monkeypatch) -> None:
    """新限额仍不够覆盖亏损时保持熔断，但不补写第二条告警。"""
    monkeypatch.setattr("src.ops.alerts._last_emit", {})
    monkeypatch.setattr("src.utils.paths.project_root", lambda: tmp_path)
    risk = RiskGuardian(max_daily_loss=100)
    risk.set_daily_pnl(-3500)
    risk.set_max_daily_loss(3000)

    assert risk.halted is True
    assert "3000" in risk.halt_reason
    text = alerts_path(tmp_path).read_text(encoding="utf-8")
    assert text.count('"code": "halt"') == 1
