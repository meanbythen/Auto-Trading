"""
账号注册表落地到 SQLite：建表与旧配置导入。

功能：
  1. 按 src/account/schema.sql 在目标库（默认 data/trading.db）建 accounts / broker_profiles；
  2. 把 config/accounts.yaml 写进库；YAML 不存在时回退旧 config.yaml 的 ctp_profiles 现算一份；
  3. 按主键 upsert，可重复执行，不会插重复行。

原理：只搬账号身份、连接参数和账本名，密码/认证码仍然只有环境变量名与引用，
      明文一律不落库。YAML 注册表仍是权威来源，库删掉重跑即可重建。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.account.registry import AccountRegistry, REGISTRY_REL, migrate_from_cfg
from src.domain.trading_account import BrokerProfileTemplate, TradingAccount

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_REL = Path("data") / "trading.db"

PROFILE_COLUMNS = (
    "profile_id",
    "label",
    "broker",
    "environment",
    "broker_id",
    "td_fronts",
    "md_fronts",
    "app_id",
    "flow_dir",
    "password_env",
    "auth_code_env",
    "password_ref",
    "auth_code_ref",
    "query_interval_sec",
    "login_timeout_sec",
    "order_wait_sec",
    "cancel_wait_sec",
    "order_count_warn",
    "cancel_count_warn",
    "close_split_products",
    "price_tick",
    "max_order_volume",
    "auto_cancel_on_timeout",
)

ACCOUNT_COLUMNS = (
    "account_id",
    "display_name",
    "broker",
    "channel",
    "environment",
    "status",
    "profile_id",
    "investor_id",
    "book_id",
    "risk_policy_id",
    "password_ref",
    "auth_code_ref",
    "max_order_volume",
    "notes",
    "created_at",
    "updated_at",
)

# 重跑时保留第一次写库的时间，不被 YAML 里后来的值覆盖。
_KEEP_ON_CONFLICT = {"created_at"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_schema() -> str:
    """读建表语句；sql 文件缺失时直接报错，不返回空串。"""
    return SCHEMA_PATH.read_text(encoding="utf-8")


def read_legacy_cfg(root: str | Path) -> dict[str, Any]:
    """读旧 config.yaml 原文。不调用 load_config，避免首次加载顺手创建注册表。"""
    path = Path(root) / "config" / "config.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def resolve_registry(root: str | Path) -> tuple[AccountRegistry, str]:
    """
    选迁移来源：YAML 注册表优先，没有才用旧 ctp_profiles。

    注册表存在但读不出来时直接报错，不能装作没有而拿旧配置覆盖。
    """
    root = Path(root)
    path = root / REGISTRY_REL
    if path.exists():
        registry = AccountRegistry.load(root)
        if registry is None:
            raise ValueError(f"{path} 读不出来，先修好或删掉再迁移")
        return registry, REGISTRY_REL.as_posix()
    return migrate_from_cfg(read_legacy_cfg(root)), "config/config.yaml（旧 ctp_profiles）"


def _fronts_text(values: Iterable[str]) -> str:
    """多台前置压成 ; 分隔的字符串，读回时与 YAML 一样拆得开。"""
    return ";".join(str(item).strip() for item in values if str(item).strip())


def _text_or_none(value: str) -> str | None:
    """空字符串存 NULL：唯一索引只拦真正填了值的重复。"""
    text = str(value or "").strip()
    return text or None


def _profile_row(profile: BrokerProfileTemplate) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "label": profile.label,
        "broker": profile.broker,
        "environment": profile.environment,
        "broker_id": profile.broker_id,
        "td_fronts": _fronts_text(profile.td_fronts),
        "md_fronts": _fronts_text(profile.md_fronts),
        "app_id": profile.app_id,
        "flow_dir": profile.flow_dir,
        "password_env": profile.password_env,
        "auth_code_env": profile.auth_code_env,
        "password_ref": profile.password_ref,
        "auth_code_ref": profile.auth_code_ref,
        "query_interval_sec": profile.query_interval_sec,
        "login_timeout_sec": profile.login_timeout_sec,
        "order_wait_sec": profile.order_wait_sec,
        "cancel_wait_sec": profile.cancel_wait_sec,
        "order_count_warn": profile.order_count_warn,
        "cancel_count_warn": profile.cancel_count_warn,
        "close_split_products": ",".join(profile.close_split_products),
        "price_tick": profile.price_tick,
        "max_order_volume": profile.max_order_volume,
        "auto_cancel_on_timeout": 1 if profile.auto_cancel_on_timeout else 0,
    }


def _account_row(account: TradingAccount) -> dict[str, Any]:
    now = _now()
    return {
        "account_id": account.account_id,
        "display_name": account.display_name,
        "broker": account.broker,
        "channel": account.channel,
        "environment": account.environment,
        "status": account.status,
        "profile_id": _text_or_none(account.profile_id),
        "investor_id": account.investor_id,
        "book_id": _text_or_none(account.book_id),
        "risk_policy_id": account.risk_policy_id,
        "password_ref": account.password_ref,
        "auth_code_ref": account.auth_code_ref,
        "max_order_volume": account.max_order_volume,
        "notes": account.notes,
        "created_at": account.created_at or now,
        "updated_at": account.updated_at or now,
    }


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    key: str,
    rows: list[dict[str, Any]],
) -> int:
    """按主键 upsert，返回受影响行数（新增 + 更新）。"""
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in columns if name != key and name not in _KEEP_ON_CONFLICT
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}"
    )
    before = conn.total_changes
    conn.executemany(sql, [tuple(row[name] for name in columns) for row in rows])
    return conn.total_changes - before


def _connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    """老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate(
    root: str | Path,
    db_path: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    建表并导入注册表。

    dry_run 用内存库试跑，能验证建表语句、外键和唯一索引，但不碰磁盘。
    注册表校验不通过时不写库，problems 原样返回给调用方打印。
    """
    root = Path(root)
    target = Path(db_path) if db_path is not None else root / DEFAULT_DB_REL
    registry, source = resolve_registry(root)
    problems = registry.validate()
    result: dict[str, Any] = {
        "source": source,
        "db": str(target),
        "dry_run": dry_run,
        "profiles": len(registry.profiles),
        "accounts": len(registry.accounts),
        "written": 0,
        "problems": problems,
    }
    if problems:
        return result

    conn_path = ":memory:" if dry_run else str(target)
    with closing(_connect(conn_path)) as conn:
        conn.executescript(load_schema())
        _ensure_columns(
            conn,
            "accounts",
            {
                "password_ref": "TEXT NOT NULL DEFAULT ''",
                "auth_code_ref": "TEXT NOT NULL DEFAULT ''",
            },
        )
        with conn:
            written = _upsert(
                conn,
                "broker_profiles",
                PROFILE_COLUMNS,
                "profile_id",
                [_profile_row(item) for item in registry.profiles],
            )
            written += _upsert(
                conn,
                "accounts",
                ACCOUNT_COLUMNS,
                "account_id",
                [_account_row(item) for item in registry.accounts],
            )
    result["written"] = written
    return result
