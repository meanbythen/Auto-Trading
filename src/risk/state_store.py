"""风控状态端口：JSON 为主，SQLite 为可重建副本和决策查询端。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from src.account.trading_db import DB_LOCK, connect, connect_readonly, json_text, storage_settings

_LOCK = threading.RLock()

# os.replace 在 Windows 上撞到杀软/索引器的瞬时锁会报 WinError 5（拒绝访问）。
# 这类锁通常在几十毫秒内释放，重试几次基本能吃掉；仍失败才向上抛。
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SEC = 0.02


def _replace_with_retry(src: Path, dst: Path) -> None:
    """重试受瞬时文件锁影响的 os.replace，其余错误立即抛出。"""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SEC * (attempt + 1))


class RiskStateStore(Protocol):
    def load(self, account_id: str) -> dict[str, Any] | None: ...

    def save(self, account_id: str, snapshot: dict[str, Any]) -> None: ...


def _safe_id(account_id: str) -> str:
    text = "".join(ch for ch in str(account_id or "live") if ch.isalnum() or ch in {"_", "-"})
    return text or "live"


class JsonRiskStateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, account_id: str) -> Path:
        return self.root / "data" / f"risk_state_{_safe_id(account_id)}.json"

    @property
    def decisions_path(self) -> Path:
        return self.root / "data" / "risk_decisions.jsonl"

    def load(self, account_id: str) -> dict[str, Any] | None:
        path = self.path(account_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("风控 JSON 状态读取失败 {}: {}", path, exc)
            return None
        return data if isinstance(data, dict) else None

    def save(self, account_id: str, snapshot: dict[str, Any]) -> None:
        path = self.path(account_id)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            try:
                with temp.open("w", encoding="utf-8") as fh:
                    json.dump(snapshot, fh, ensure_ascii=False, indent=2, default=str)
                    fh.flush()
                    os.fsync(fh.fileno())
                _replace_with_retry(temp, path)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError as exc:  # 临时文件被外部占用时别把原始异常盖掉
                    logger.debug("风控状态临时文件清理失败: {}", exc)

    def append_decision(self, record: dict[str, Any]) -> None:
        path = self.decisions_path
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json_text(record) + "\n")
                fh.flush()

    def list_decisions(
        self,
        account_id: str,
        *,
        trading_day: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        path = self.decisions_path
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or str(row.get("account_id") or "") != str(account_id):
                continue
            if trading_day and str(row.get("trading_day") or "") != str(trading_day):
                continue
            rows.append(row)
        return rows[-max(1, int(limit)) :]


class SQLiteRiskStateStore:
    def __init__(self, root: str | Path, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def load(self, account_id: str) -> dict[str, Any] | None:
        conn = connect_readonly(self.db_path)
        if conn is None:
            return None
        with closing(conn):
            row = conn.execute(
                "SELECT payload_json FROM risk_state WHERE account_id = ?", (str(account_id),)
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, account_id: str, snapshot: dict[str, Any]) -> None:
        with DB_LOCK, closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO risk_state (account_id, trading_day, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        trading_day=excluded.trading_day,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(account_id),
                        str(snapshot.get("trading_day") or ""),
                        json_text(snapshot),
                        str(snapshot.get("updated_at") or ""),
                    ),
                )

    def append_decision(self, record: dict[str, Any]) -> None:
        with DB_LOCK, closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO risk_decisions (
                        decision_id, account_id, trading_day, ts, gate, symbol, side, volume,
                        allowed, code, reason, trace_id, intent_json, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(record.get("decision_id") or ""),
                        str(record.get("account_id") or ""),
                        str(record.get("trading_day") or ""),
                        str(record.get("ts") or ""),
                        str(record.get("gate") or ""),
                        str(record.get("symbol") or ""),
                        str(record.get("side") or ""),
                        int(record.get("volume") or 0),
                        int(bool(record.get("allowed"))),
                        str(record.get("code") or ""),
                        str(record.get("reason") or ""),
                        str(record.get("trace_id") or ""),
                        json_text(record.get("intent") or {}),
                        json_text(record.get("state") or {}),
                    ),
                )

    def list_decisions(
        self,
        account_id: str,
        *,
        trading_day: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM risk_decisions WHERE account_id = ?"
        params: list[Any] = [str(account_id)]
        if trading_day:
            sql += " AND trading_day = ?"
            params.append(str(trading_day))
        sql += " ORDER BY ts DESC, decision_id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        conn = connect_readonly(self.db_path)
        if conn is None:
            return []
        with closing(conn):
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            item["allowed"] = bool(item["allowed"])
            for key in ("intent_json", "state_json"):
                target = key.removesuffix("_json")
                try:
                    item[target] = json.loads(str(item.pop(key)))
                except json.JSONDecodeError:
                    item[target] = {}
            out.append(item)
        return out


class DualRiskStateStore:
    """每次保存都先尝试 JSON；SQLite 开关只控制副本，不改变主账本。"""

    def __init__(self, root: str | Path, cfg: dict[str, Any] | None = None) -> None:
        self.root = Path(root)
        self.json = JsonRiskStateStore(root)
        self.settings = storage_settings(root, cfg)
        self.sqlite = SQLiteRiskStateStore(root, self.settings.db_path)

    def load(self, account_id: str) -> dict[str, Any] | None:
        data = self.json.load(account_id)
        if data is not None or not self.settings.sqlite_enabled:
            return data
        try:
            return self.sqlite.load(account_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("风控 SQLite 状态读取失败: {}", exc)
            return None

    def save(self, account_id: str, snapshot: dict[str, Any]) -> None:
        try:
            self.json.save(account_id, snapshot)
        except OSError as exc:
            logger.error("风控 JSON 状态保存失败: {}", exc)
        if not self.settings.sqlite_enabled:
            return
        try:
            self.sqlite.save(account_id, snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("风控 SQLite 状态保存失败（JSON 仍为准）: {}", exc)

    def append_decision(self, record: dict[str, Any]) -> None:
        try:
            self.json.append_decision(record)
        except OSError as exc:
            logger.warning("风控决策 JSONL 写入失败: {}", exc)
        if not self.settings.sqlite_enabled:
            return
        try:
            self.sqlite.append_decision(record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("风控决策 SQLite 写入失败: {}", exc)

    def list_decisions(
        self,
        account_id: str,
        *,
        trading_day: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.settings.sqlite_enabled:
            try:
                return self.sqlite.list_decisions(account_id, trading_day=trading_day, limit=limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("风控决策查询失败，回退 JSONL: {}", exc)
        return self.json.list_decisions(account_id, trading_day=trading_day, limit=limit)


# 旧键迁移时不参与内容比对的字段：account_id 必然变，updated_at/version 是元数据，
# decisions 体积大且新键已在持续写入。
_LEGACY_COMPARE_IGNORE = {"account_id", "updated_at", "decisions", "version"}


def _legacy_key_map(root: str | Path) -> dict[str, str]:
    """从账号审计日志的 book_migrate 事件重建 旧账本键→新账号键（取最新一条）。"""
    path = Path(root) / "data" / "accounts_audit.jsonl"
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("action") == "book_migrate":
                old = str(evt.get("old_book") or "").strip()
                new = str(evt.get("account_id") or evt.get("new_book") or "").strip()
                if old and new:
                    mapping[old] = new
    except OSError as exc:
        logger.warning("读取账号审计日志失败，无法重建风控旧键映射: {}", exc)
    return mapping


def _normalize_state(data: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (data or {}).items() if k not in _LEGACY_COMPARE_IGNORE}


def _current_account_ids(root: str | Path) -> set[str]:
    try:
        from src.account.registry import load_registry

        registry = load_registry(Path(root))
    except Exception:  # noqa: BLE001
        return set()
    if registry is None:
        return set()
    return {a.account_id for a in registry.accounts}


def _sqlite_keys(root: str | Path, cfg: dict[str, Any] | None) -> list[str]:
    settings = storage_settings(root, cfg)
    if not settings.sqlite_enabled:
        return []
    conn = connect_readonly(settings.db_path)
    if conn is None:
        return []
    with closing(conn):
        try:
            return [str(r["account_id"]) for r in conn.execute("SELECT account_id FROM risk_state")]
        except sqlite3.Error:
            return []


def _json_keys(root: str | Path) -> list[str]:
    out: list[str] = []
    prefix = "risk_state_"
    for path in (Path(root) / "data").glob(prefix + "*.json"):
        name = path.name
        if name.endswith(".tmp"):
            continue
        out.append(name[len(prefix) : -len(".json")])
    return out


def migrate_legacy_keys(
    root: str | Path, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把 risk_state 里的旧账本键迁移/清理到账号键。幂等，可重复执行。

    返回 {migrated, deleted, conflicts, orphans}。
    冲突时不覆盖新键、不删旧键，并在新键上写入熔断（停开新仓）+ 告警，等人工核对。
    """
    mapping = _legacy_key_map(root)
    current = _current_account_ids(root) or set(mapping.values())
    if not mapping:
        return {"migrated": [], "deleted": [], "conflicts": [], "orphans": []}

    store = DualRiskStateStore(root, cfg)
    all_keys = set(_json_keys(root)) | set(_sqlite_keys(root, cfg))

    migrated: list[str] = []
    deleted: list[str] = []
    conflicts: list[str] = []
    orphans: list[str] = []

    for old in sorted(all_keys):
        if old in current:
            continue
        if old not in mapping:
            orphans.append(old)
            continue
        new = mapping[old]
        old_state = store.load(old)
        new_state = store.load(new)
        if old_state is None:
            # 旧键只剩空壳（一侧有另一侧没有），直接清掉。
            _delete_key(root, cfg, old)
            deleted.append(old)
            continue
        if new_state is None:
            # 新键还没建：把旧状态搬过去（account_id 改成新键），再删旧键。
            payload = dict(old_state)
            payload["account_id"] = new
            store.save(new, payload)
            _delete_key(root, cfg, old)
            migrated.append(old)
            continue
        if _normalize_state(old_state) == _normalize_state(new_state):
            # 内容一致：旧键是陈旧副本，直接清理。
            _delete_key(root, cfg, old)
            deleted.append(old)
            continue
        # 内容不一致：冲突，不猜测合并。
        conflicts.append(f"{old}→{new}")
        msg = (
            f"风控状态旧键 {old} 与新键 {new} 内容不一致，已停止开新仓，请人工核对后清理旧键"
        )
        forced = dict(new_state)
        forced["halted"] = True
        forced["halt_kind"] = "risk_state_migration_conflict"
        forced["halt_reason"] = msg
        forced["halt_alerted"] = False
        forced["account_id"] = new
        store.save(new, forced)
        logger.error(msg)
        try:
            from src.ops.alerts import emit_alert

            emit_alert("risk_state_migration_conflict", msg, level="error")
        except Exception:  # noqa: BLE001
            pass

    if conflicts:
        logger.error("风控状态键迁移发现 {} 个冲突，已暂停开新仓: {}", len(conflicts), conflicts)
    if migrated or deleted:
        logger.info(
            "风控状态旧键迁移完成：迁移 {} 个，清理 {} 个，冲突 {} 个，孤儿 {} 个",
            len(migrated), len(deleted), len(conflicts), len(orphans),
        )
    return {
        "migrated": migrated,
        "deleted": deleted,
        "conflicts": conflicts,
        "orphans": orphans,
    }


def _delete_key(root: str | Path, cfg: dict[str, Any] | None, account_id: str) -> None:
    """删除 JSON 文件与 SQLite 行；不存在则忽略。"""
    json_path = JsonRiskStateStore(root).path(account_id)
    try:
        json_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除旧风控状态 JSON 失败 {}: {}", json_path, exc)
    settings = storage_settings(root, cfg)
    if not settings.sqlite_enabled:
        return
    try:
        with DB_LOCK, closing(connect(settings.db_path)) as conn, conn:
            conn.execute("DELETE FROM risk_state WHERE account_id = ?", (str(account_id),))
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除旧风控状态 SQLite 行失败 {}: {}", account_id, exc)
