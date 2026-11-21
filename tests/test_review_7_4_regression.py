"""§7.4 第四批回归：自测账命名、快照 halt_kind、复盘交易日、旧账本归档、日志轮转与实验索引。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import src.ops.alerts as alerts
import src.review.shadow as shadow
from src.account import registry as reg
from src.account.registry import AccountRegistry
from src.backtest.experiment import rebuild_index
from src.domain.accounts import ctp_book_ids, normalize_id
from src.domain.trading_account import TradingAccount
from src.review.session import previous_trading_day
from src.scheduler.runner import TradingBot


def _cfg(root: Path) -> dict:
    return {
        "dry_run": True,
        "trade": {"symbol": "bu2612", "executor": "sim", "volume": 1},
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


def test_normalize_id_keeps_self_test() -> None:
    """self_test 不在 acc_/book_ 前缀里，必须显式保留，不能回落成 GUI 实盘。"""
    assert normalize_id("self_test") == "self_test"
    assert normalize_id("SELF_TEST") == "self_test"
    assert normalize_id("acc_demo") == "acc_demo"
    assert normalize_id("book_demo") == "book_demo"
    assert normalize_id("认不出来") == "live"


def test_status_snapshot_includes_halt_kind(tmp_path: Path) -> None:
    bot = TradingBot(_cfg(tmp_path))
    assert bot.status_snapshot()["halt_kind"] == ""
    bot.risk.halted = True
    bot.risk.halt_kind = "holiday_calendar_missing"
    assert bot.status_snapshot()["halt_kind"] == "holiday_calendar_missing"


def test_previous_trading_day_skips_holidays() -> None:
    # 2026 国庆休市 10/1~10/7；10/8 是节后首日，前一个交易日应回到 9/30。
    assert previous_trading_day(date(2026, 10, 8)) == date(2026, 9, 30)
    # 普通周一仍回落到上周五。
    assert previous_trading_day(date(2026, 11, 23)) == date(2026, 11, 20)


def _account(book_id: str, *, account_id: str = "acc_demo_sim") -> TradingAccount:
    return TradingAccount(
        account_id=account_id,
        display_name=account_id,
        environment="sim",
        book_id=book_id,
    )


def test_archive_duplicate_legacy_book_only_when_identical(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    # 新账号账本（acc_ 前缀会被派生规则去掉）。
    target = data / "signal_account_demo_sim.json"
    target.write_text('{"trades": []}\n', encoding="utf-8")
    legacy = data / "signal_account_ctp_sim.json"
    legacy.write_text('{"trades": []}\n', encoding="utf-8")

    registry = AccountRegistry(accounts=[_account("acc_demo_sim")])
    archived = reg.archive_duplicate_legacy_books(tmp_path, registry)

    assert archived == ["signal_account_ctp_sim.json→signal_account_ctp_sim.json"]
    assert not legacy.exists()
    assert (data / "archive" / "legacy_books" / "signal_account_ctp_sim.json").exists()
    assert target.read_text(encoding="utf-8") == '{"trades": []}\n'
    # 审计留痕
    audit = (data / "accounts_audit.jsonl").read_text(encoding="utf-8")
    assert "legacy_book_archive" in audit


def test_archive_keeps_legacy_book_when_content_differs(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "signal_account_demo_sim.json").write_text('{"trades": [1]}\n', encoding="utf-8")
    legacy = data / "signal_account_ctp_sim.json"
    legacy.write_text('{"trades": []}\n', encoding="utf-8")

    registry = AccountRegistry(accounts=[_account("acc_demo_sim")])
    assert reg.archive_duplicate_legacy_books(tmp_path, registry) == []
    assert legacy.exists()


def test_archive_keeps_book_still_in_use(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "signal_account_ctp_sim.json").write_text('{"trades": []}\n', encoding="utf-8")
    registry = AccountRegistry(accounts=[_account("ctp_sim")])
    assert reg.archive_duplicate_legacy_books(tmp_path, registry) == []
    assert (data / "signal_account_ctp_sim.json").exists()


def test_ctp_book_ids_and_shadow_resolution(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "accounts.yaml").write_text(
        "version: 2\n"
        "accounts:\n"
        "  - account_id: acc_demo_sim\n"
        "    book_id: acc_demo_sim\n"
        "    channel: ctp\n"
        "    environment: sim\n"
        "  - account_id: acc_demo_live\n"
        "    book_id: acc_demo_live\n"
        "    channel: ctp\n"
        "    environment: live\n",
        encoding="utf-8",
    )
    assert ctp_book_ids(tmp_path) == {"sim": "acc_demo_sim", "live": "acc_demo_live"}

    seen: list[str] = []

    def fake_side(root, account_id, *, symbol=""):
        seen.append(account_id)
        return shadow.BookSide(key=account_id, label=account_id)

    monkeypatch.setattr(shadow, "_side", fake_side)
    shadow.shadow_report(tmp_path)
    assert seen == ["acc_demo_sim", "acc_demo_live"]


def test_accounts_audit_rotates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(reg, "AUDIT_MAX_BYTES", 10)
    data = tmp_path / "data"
    data.mkdir()
    path = data / "accounts_audit.jsonl"
    path.write_text("x" * 32, encoding="utf-8")
    reg.record_audit(tmp_path, "unit_test", note="rotate")
    assert (data / "accounts_audit.jsonl.1").read_text(encoding="utf-8") == "x" * 32
    assert "unit_test" in path.read_text(encoding="utf-8")
    assert not path.read_text(encoding="utf-8").startswith("x")


def test_alerts_rotate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(alerts, "ALERTS_MAX_BYTES", 10)
    data = tmp_path / "data"
    data.mkdir()
    path = data / "alerts.jsonl"
    path.write_text("x" * 32, encoding="utf-8")
    alerts._last_emit.clear()
    assert alerts.emit_alert("rotate_test", "轮转测试", root=tmp_path) is True
    assert (data / "alerts.jsonl.1").read_text(encoding="utf-8") == "x" * 32
    assert "rotate_test" in path.read_text(encoding="utf-8")
    assert not path.read_text(encoding="utf-8").startswith("x")


def test_rebuild_index(tmp_path: Path) -> None:
    # 空目录：返回 0，且不创建 index.json。
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    assert rebuild_index(tmp_path) == 0
    assert not (exp_dir / "index.json").exists()

    # 两个实验目录：索引两条，原目录不动。
    for name, created in (("e1", "2026-11-01T09:00:00"), ("e2", "2026-11-02T09:00:00")):
        folder = exp_dir / name
        folder.mkdir()
        manifest = {
            "experiment_id": name,
            "kind": "backtest",
            "created_at": created,
            "details": {"symbol": "bu2612"},
            "code": {"version": "49.22.0"},
        }
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "report.json").write_text("{}", encoding="utf-8")

    assert rebuild_index(tmp_path) == 2
    index = json.loads((exp_dir / "index.json").read_text(encoding="utf-8"))
    assert [row["experiment_id"] for row in index["experiments"]] == ["e1", "e2"]
    assert index["experiments"][0]["report"] == "e1/report.json"
    assert sorted(p.name for p in (exp_dir / "e1").iterdir()) == ["manifest.json", "report.json"]
