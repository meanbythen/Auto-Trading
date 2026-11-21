"""
运行审计：信号、订单链路和研究运行清单统一落 SQLite。

功能：把策略信号、审计事件、研究运行清单写到 data/trading.db。
原理：短连接 + 进程内锁，写失败只记日志，绝不阻断交易或研究主流程。
     订单阶段证据仍留在 data/order_timeline.jsonl，这里只做可查询的索引。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.account.trading_db import DB_LOCK, connect, connect_readonly, json_text, storage_settings


def db_path_for(root: str | Path | None = None) -> Path:
    """按工作区配置取交易库路径；测试传临时目录时只在该目录下建库。"""
    base = _root(root)
    return storage_settings(base).db_path


def _root(root: str | Path | None) -> Path:
    if root is not None:
        return Path(root)
    from src.utils.paths import project_root

    return Path(project_root())


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _write(root: str | Path | None, sql: str, params: tuple[Any, ...]) -> bool:
    try:
        path = db_path_for(root)
        with DB_LOCK, closing(connect(path)) as conn:
            with conn:
                conn.execute(sql, params)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计写入失败 {}: {}", sql.split()[0], exc)
        return False


def record_signal(
    *,
    trace_id: str,
    account_id: str = "",
    strategy_id: str = "",
    symbol: str,
    bar_time: str = "",
    action: str,
    price: float = 0.0,
    score: float = 0.0,
    reason: str = "",
    payload: dict[str, Any] | None = None,
    root: str | Path | None = None,
) -> bool:
    """记一条策略信号；同一 trace/品种/K线/动作重复写只保留一条。"""
    signal_id = f"{trace_id}:{symbol}:{bar_time}:{action}"
    created_at = _now()
    body = dict(payload or {})
    body.setdefault("signal_id", signal_id)
    sql = """
        INSERT OR REPLACE INTO signals (
            signal_id, trace_id, account_id, strategy_id, symbol, bar_time,
            action, price, score, reason, created_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    return _write(
        root,
        sql,
        (
            signal_id,
            str(trace_id or ""),
            str(account_id or ""),
            str(strategy_id or ""),
            str(symbol or ""),
            str(bar_time or ""),
            str(action or ""),
            float(price or 0.0),
            float(score or 0.0),
            str(reason or ""),
            created_at,
            json_text(body),
        ),
    )


def record_audit(
    event: str,
    *,
    message: str = "",
    trace_id: str = "",
    account_id: str = "",
    strategy_id: str = "",
    symbol: str = "",
    order_id: str = "",
    payload: dict[str, Any] | None = None,
    root: str | Path | None = None,
) -> bool:
    """追加一条审计事件，按发生顺序保留，不覆盖。"""
    sql = """
        INSERT INTO audit_log (
            ts, trace_id, account_id, strategy_id, symbol, order_id,
            event, message, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    return _write(
        root,
        sql,
        (
            _now(),
            str(trace_id or ""),
            str(account_id or ""),
            str(strategy_id or ""),
            str(symbol or ""),
            str(order_id or ""),
            str(event or ""),
            str(message or ""),
            json_text(payload or {}),
        ),
    )


def _ensure_research_runs_column(root: str | Path | None) -> None:
    """旧库补 data_sha256 列；库不存在或表缺失时交给正式写入去建表。"""
    path = db_path_for(root)
    if not Path(path).exists():
        return
    try:
        with DB_LOCK, closing(connect(path)) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(research_runs)")}
            if cols and "data_sha256" not in cols:
                with conn:
                    conn.execute(
                        "ALTER TABLE research_runs ADD COLUMN data_sha256 TEXT NOT NULL DEFAULT ''"
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("research_runs 补列跳过: {}", exc)


def record_research_run(manifest: dict[str, Any], *, root: str | Path | None = None) -> bool:
    """把研究运行清单落库；同一 run_id 重跑时覆盖。"""
    if not manifest:
        return False
    _ensure_research_runs_column(root)
    strategy = manifest.get("strategy") or {}
    commit = manifest.get("commit") or {}
    sql = """
        INSERT OR REPLACE INTO research_runs (
            run_id, trace_id, source, status, started_at, finished_at, duration_ms,
            strategy_id, strategy_version, params_sha256, data_sha256, period, symbols_json,
            commit_hash, message, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    return _write(
        root,
        sql,
        (
            str(manifest.get("run_id") or ""),
            str(manifest.get("trace_id") or ""),
            str(manifest.get("source") or ""),
            str(manifest.get("status") or ""),
            str(manifest.get("started_at") or ""),
            str(manifest.get("finished_at") or ""),
            float(manifest.get("duration_ms") or 0.0),
            str(strategy.get("id") or strategy.get("engine") or ""),
            str(strategy.get("version") or ""),
            str(manifest.get("params_sha256") or ""),
            str(manifest.get("data_sha256") or ""),
            str(manifest.get("period") or ""),
            json_text(manifest.get("symbols") or []),
            str(commit.get("git_commit") or ""),
            str(manifest.get("message") or ""),
            json_text(manifest),
        ),
    )


def trace_events(trace_id: str, *, root: str | Path | None = None) -> list[dict[str, Any]]:
    """按 trace_id 把信号、风控决策、审计事件合成一条时间线。"""
    if not str(trace_id or ""):
        return []
    rows: list[dict[str, Any]] = []
    try:
        conn = connect_readonly(db_path_for(root))
        if conn is None:
            return []
        with closing(conn):
            for row in conn.execute(
                "SELECT * FROM signals WHERE trace_id = ?", (str(trace_id),)
            ):
                rows.append(
                    {
                        "ts": str(row["created_at"]),
                        "event": "signal_generated",
                        "trace_id": str(row["trace_id"]),
                        "symbol": str(row["symbol"]),
                        "bar_time": str(row["bar_time"]),
                        "action": str(row["action"]),
                        "price": float(row["price"] or 0),
                        "reason": str(row["reason"]),
                    }
                )
            for row in conn.execute(
                "SELECT * FROM risk_decisions WHERE trace_id = ?", (str(trace_id),)
            ):
                rows.append(
                    {
                        "ts": str(row["ts"]),
                        "event": "risk_decision",
                        "trace_id": str(row["trace_id"]),
                        "symbol": str(row["symbol"]),
                        "gate": str(row["gate"]),
                        "allowed": bool(row["allowed"]),
                        "code": str(row["code"]),
                        "reason": str(row["reason"]),
                    }
                )
            for row in conn.execute(
                "SELECT * FROM audit_log WHERE trace_id = ?", (str(trace_id),)
            ):
                rows.append(
                    {
                        "ts": str(row["ts"]),
                        "event": str(row["event"]),
                        "trace_id": str(row["trace_id"]),
                        "symbol": str(row["symbol"]),
                        "order_id": str(row["order_id"]),
                        "message": str(row["message"]),
                    }
                )
    except (OSError, sqlite3.Error) as exc:
        logger.debug("追踪查询失败 {}: {}", trace_id, exc)
        return []
    rows.sort(key=lambda item: str(item.get("ts") or ""))
    return rows


def metrics_snapshot(day: str = "", *, root: str | Path | None = None) -> dict[str, Any]:
    """从订单时间线和告警文件聚合关键指标；文件缺失按零处理。"""
    base = _root(root)
    timeline_path = base / "data" / "order_timeline.jsonl"
    alerts_path = base / "data" / "alerts.jsonl"
    prefix = str(day or "")

    orders_sent = 0
    fills = 0
    rejected = 0
    send_ms: list[float] = []
    fill_ms: list[float] = []
    slips: list[float] = []
    for row in _read_jsonl(timeline_path):
        if prefix and not str(row.get("ts") or "").startswith(prefix):
            continue
        stage = str(row.get("stage") or "")
        if stage == "order_sent":
            orders_sent += 1
            value = row.get("decision_to_send_ms") or row.get("risk_to_send_ms")
            if isinstance(value, (int, float)):
                send_ms.append(float(value))
        elif stage in ("accounted", "assumed_fill"):
            fills += 1
            value = row.get("stage_ms")
            if isinstance(value, (int, float)):
                fill_ms.append(float(value))
            slip = row.get("slip_ticks")
            if isinstance(slip, (int, float)):
                slips.append(abs(float(slip)))
        elif stage == "risk_intent" and row.get("allowed") is False:
            rejected += 1

    freeze_alerts = 0
    reconcile_alerts = 0
    for row in _read_jsonl(alerts_path):
        if prefix and not str(row.get("ts") or "").startswith(prefix):
            continue
        code = str(row.get("code") or "").lower()
        message = str(row.get("message") or "")
        if "freeze" in code or "冻结" in message:
            freeze_alerts += 1
        if "reconcile" in code or "对账" in message:
            reconcile_alerts += 1

    return {
        "orders_sent": orders_sent,
        "fills": fills,
        "rejected": rejected,
        "reject_rate": round(rejected / orders_sent, 4) if orders_sent else 0.0,
        "avg_send_ms": round(sum(send_ms) / len(send_ms), 3) if send_ms else 0.0,
        "avg_fill_ms": round(sum(fill_ms) / len(fill_ms), 3) if fill_ms else 0.0,
        "avg_slippage": round(sum(slips) / len(slips), 3) if slips else 0.0,
        "freeze_alerts": freeze_alerts,
        "reconcile_alerts": reconcile_alerts,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError as exc:
        logger.debug("读取 {} 失败: {}", path.name, exc)
    return rows
