"""账号注册表 > SQLite：建表、导入、重跑和校验拦截。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from src.account.registry import AccountRegistry
from src.account.sqlite_migrate import migrate
from src.domain.trading_account import BrokerProfileTemplate, TradingAccount


def _profile(profile_id: str = "huatai_sim") -> BrokerProfileTemplate:
    return BrokerProfileTemplate(
        profile_id=profile_id,
        label="华泰仿真",
        broker="华泰期货",
        environment="sim",
        broker_id="8080",
        td_fronts=["tcp://59.36.3.115:27226"],
        md_fronts=["tcp://59.36.3.115:27236"],
        password_env="CTP_PASSWORD",
        auth_code_env="CTP_AUTH_CODE",
        password_ref="secret://ctp/sim/password",
        auth_code_ref="secret://ctp/auth_code",
        auto_cancel_on_timeout=False,
    )


def _account(account_id: str = "acc_huatai_sim_01", book_id: str = "ctp_sim") -> TradingAccount:
    return TradingAccount(
        account_id=account_id,
        display_name="华泰仿真-28077",
        broker="华泰期货",
        channel="ctp",
        environment="sim",
        status="enabled",
        profile_id="huatai_sim",
        investor_id="28077",
        book_id=book_id,
        created_at="2026-11-20T10:00:00",
        updated_at="2026-11-20T10:00:00",
    )


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_migrate_writes_accounts_and_profiles(tmp_path: Path) -> None:
    AccountRegistry(accounts=[_account()], profiles=[_profile()]).save(tmp_path)
    db = tmp_path / "data" / "trading.db"

    result = migrate(tmp_path, db)

    assert result["problems"] == []
    assert result["written"] == 2
    assert db.exists()
    account = _rows(
        db,
        "SELECT account_id, channel, environment, status, profile_id, investor_id, book_id, created_at "
        "FROM accounts",
    )
    assert account == [
        ("acc_huatai_sim_01", "ctp", "sim", "enabled", "huatai_sim", "28077", "ctp_sim", "2026-11-20T10:00:00")
    ]
    profile = _rows(
        db,
        "SELECT td_fronts, md_fronts, password_ref, auth_code_ref, auto_cancel_on_timeout "
        "FROM broker_profiles",
    )
    assert profile == [
        ("tcp://59.36.3.115:27226", "tcp://59.36.3.115:27236", "secret://ctp/sim/password",
         "secret://ctp/auth_code", 0)
    ]


def test_migrate_is_repeatable(tmp_path: Path) -> None:
    AccountRegistry(accounts=[_account()], profiles=[_profile()]).save(tmp_path)
    db = tmp_path / "data" / "trading.db"

    migrate(tmp_path, db)
    migrate(tmp_path, db)

    assert _rows(db, "SELECT COUNT(*) FROM accounts") == [(1,)]
    assert _rows(db, "SELECT COUNT(*) FROM broker_profiles") == [(1,)]
    # 重跑不覆盖首次写库时间
    assert _rows(db, "SELECT created_at FROM accounts") == [("2026-11-20T10:00:00",)]


def test_falls_back_to_legacy_ctp_profiles(tmp_path: Path) -> None:
    cfg = {
        "account": {"broker": "华泰期货", "user_id": "20318052"},
        "trade": {
            "ctp": {
                "app_id": "app-1",
                "ctp_profiles": {
                    "huatai_sim": {
                        "kind": "sim",
                        "label": "华泰仿真",
                        "broker_id": "8080",
                        "td_front": "tcp://sim-td:41205",
                        "md_front": "tcp://sim-md:41213",
                        "user_id": "28077",
                    }
                },
            }
        },
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    db = tmp_path / "data" / "trading.db"

    result = migrate(tmp_path, db)

    assert result["problems"] == []
    assert "config.yaml" in result["source"]
    assert not (tmp_path / "config" / "accounts.yaml").exists()
    rows = _rows(db, "SELECT channel, environment, investor_id FROM accounts ORDER BY account_id")
    assert rows == [("gui", "live", "20318052"), ("ctp", "sim", "28077")]
    ref = _rows(db, "SELECT password_ref FROM broker_profiles")
    assert ref == [("secret://ctp/sim/password",)]


def test_invalid_registry_blocks_write(tmp_path: Path) -> None:
    registry = AccountRegistry(
        accounts=[_account("acc_a_01", "ctp_sim"), _account("acc_b_01", "ctp_sim")],
        profiles=[_profile()],
    )
    # 两个账号共用模板和账本：save 不拦，迁移前校验要拦。
    registry.save(tmp_path)
    db = tmp_path / "data" / "trading.db"

    result = migrate(tmp_path, db)

    assert result["problems"]
    assert result["written"] == 0
    assert not db.exists()
