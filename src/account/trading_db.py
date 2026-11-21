"""P1 交易 SQLite 的连接、路径和双写开关。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from src.account.sqlite_migrate import (
    ACCOUNT_COLUMNS,
    _account_row,
    init_schema,
    resolve_registry,
)

DEFAULT_DB_REL = Path("data") / "trading.db"
DB_LOCK = threading.RLock()


@dataclass(frozen=True)
class StorageSettings:
    sqlite_enabled: bool
    db_path: Path
    outbox_enabled: bool = True


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "关", "关闭"}
    return bool(value)


def storage_settings(root: str | Path, cfg: dict[str, Any] | None = None) -> StorageSettings:
    """只读指定工作区的配置；临时测试目录不会回退去读真实项目配置。"""
    base = Path(root)
    data = cfg
    if data is None:
        path = base / "config" / "config.yaml"
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, yaml.YAMLError):
            loaded = {}
        data = loaded if isinstance(loaded, dict) else {}
    raw = data.get("storage") if isinstance(data, dict) else {}
    storage = raw if isinstance(raw, dict) else {}
    db_value = Path(str(storage.get("database") or DEFAULT_DB_REL.as_posix()))
    db_path = db_value if db_value.is_absolute() else base / db_value
    return StorageSettings(
        sqlite_enabled=_bool(storage.get("sqlite_enabled"), True),
        db_path=db_path,
        outbox_enabled=_bool(storage.get("outbox_enabled"), True),
    )


def resolve_db_path(root: str | Path, db_path: str | Path | None = None) -> Path:
    if db_path is None:
        return storage_settings(root).db_path
    path = Path(db_path)
    return path if path.is_absolute() else Path(root) / path


def connect(db_path: str | Path) -> sqlite3.Connection:
    """写连接：建库、WAL、按版本初始化 schema；调用方用事务提交，进程内由 DB_LOCK 串行写入。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    """只读连接：mode=ro，不建库、不写 WAL、不跑 schema。库文件不存在返回 None。

    读路径统一走这里，避免每次查询都触发写锁或 DDL。
    """
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


# 启动漂移比对只看这些与 YAML 一一对应的字段；created_at/updated_at 不参与。
_ACCOUNT_DIFF_FIELDS = (
    "display_name",
    "channel",
    "environment",
    "status",
    "profile_id",
    "investor_id",
    "book_id",
    "risk_policy_id",
    "password_ref",
    "auth_code_ref",
    "captcha_mode",
    "skip_auto_login",
    "max_order_volume",
    "notes",
)


def _norm(value: Any) -> str:
    return str(value or "")


def diff_accounts(
    root: str | Path, cfg: dict[str, Any] | None = None
) -> list[str]:
    """比对 YAML 注册表与 SQLite accounts 投影，返回差异描述；空列表表示一致。

    放在 trading_db 是因为它同时需要 connect_readonly/storage_settings（本模块）
    与注册表行构造（sqlite_migrate），而 trading_db 已单向依赖 sqlite_migrate，
    不会引入新的导入环。
    """
    settings = storage_settings(root, cfg)
    if not settings.sqlite_enabled:
        return []
    conn = connect_readonly(settings.db_path)
    if conn is None:
        return ["SQLite 账号投影库不存在（data/trading.db）"]
    try:
        registry, _source = resolve_registry(root)
    except Exception as exc:  # noqa: BLE001
        return [f"账号注册表读不出来，无法比对投影：{exc}"]
    problems = registry.validate()
    if problems:
        return [f"注册表校验失败：{'; '.join(problems)}"]
    expected = {row["account_id"]: row for row in (_account_row(a) for a in registry.accounts)}
    # 只读连接不跑 schema 迁移；老库可能缺列，先探表结构，只选存在的列，
    # 缺列本身也算漂移（提示需要运行迁移工具）。
    with closing(conn):
        existing = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        if not existing:
            return ["accounts 表不存在（投影未初始化，需运行迁移工具）"]
        present = [col for col in ACCOUNT_COLUMNS if col in existing]
        missing_cols = [col for col in _ACCOUNT_DIFF_FIELDS if col not in existing]
        actual_rows = conn.execute(
            f"SELECT {', '.join(present)} FROM accounts"
        ).fetchall()
    actual = {str(r["account_id"]): dict(r) for r in actual_rows}
    diffs: list[str] = []
    for aid, exp in expected.items():
        act = actual.get(aid)
        if act is None:
            diffs.append(f"账号 {aid} 在 SQLite 投影中缺失")
            continue
        for field in missing_cols:
            diffs.append(f"账号 {aid} 字段 {field} 缺失（投影 schema 过旧，需迁移）")
        for field in _ACCOUNT_DIFF_FIELDS:
            if field in missing_cols:
                continue
            if _norm(exp.get(field)) != _norm(act.get(field)):
                diffs.append(
                    f"账号 {aid} 字段 {field} 不一致："
                    f"YAML={_norm(exp.get(field))!r} SQLite={_norm(act.get(field))!r}"
                )
    for aid in actual:
        if aid not in expected:
            diffs.append(f"SQLite 投影存在多余账号 {aid}（YAML 已删除/改名）")
    return diffs


def check_account_projection(
    root: str | Path, cfg: dict[str, Any] | None = None
) -> list[str]:
    """启动时检查账号投影漂移；有差异则告警，不阻断启动。返回差异列表。"""
    diffs = diff_accounts(root, cfg)
    if not diffs:
        return diffs
    preview = "；".join(diffs[:5])
    if len(diffs) > 5:
        preview += f"（共 {len(diffs)} 项）"
    message = f"账号表过期：SQLite 投影与 config/accounts.yaml 不一致 —— {preview}"
    logger.warning("账号投影漂移：{}", message)
    try:
        from src.ops.alerts import emit_alert

        emit_alert("accounts_projection_stale", message, level="warning")
    except Exception:  # noqa: BLE001 - 告警失败不影响启动
        pass
    return diffs
