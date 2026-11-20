"""风控状态端口：JSON 为主，SQLite 为可重建副本和决策查询端。"""

from __future__ import annotations

import json
import os
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from src.account.trading_db import DB_LOCK, connect, json_text, storage_settings

_LOCK = threading.RLock()


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
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)

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
        with closing(connect(self.db_path)) as conn:
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
        with closing(connect(self.db_path)) as conn:
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
