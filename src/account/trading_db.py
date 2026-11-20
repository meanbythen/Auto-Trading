"""P1 交易 SQLite 的连接、路径和双写开关。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.account.sqlite_migrate import SCHEMA_PATH, _ensure_columns

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
    """短连接 + WAL；调用方用事务提交，进程内由 DB_LOCK 串行写入。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # 老库的 risk_decisions 没有 trace_id；建表语句不会补列，这里顺手补齐。
    _ensure_columns(conn, "risk_decisions", {"trace_id": "TEXT NOT NULL DEFAULT ''"})
    return conn


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
