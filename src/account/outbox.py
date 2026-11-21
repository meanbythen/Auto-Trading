"""P2 发件箱：订单/成交事实先落 SQLite，再由后台线程补投影。

不是邮件，也不是外部消息队列。写入方只负责把「已经发生的事实」落库；
消费方（单线程）按 seq 顺序处理，崩溃后从 pending 继续，避免报了单没记账。

约定：
1. 成交事件先以 pending 落库，JSON 账本写成功后才置 ready；
2. ready 事件只做账本补偿、投影和审计，不改变交易结果；
3. 同一 event_id 只插一次，成交再用 fill_key 去重，重启不会重复入账。
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.account.trading_db import DB_LOCK, connect, connect_readonly, json_text, storage_settings

ORDER_SUBMITTED = "OrderSubmitted"
ORDER_FILLED = "OrderFilled"
ORDER_CANCELLED = "OrderCancelled"
ORDER_REJECTED = "OrderRejected"
POSITION_CHANGED = "PositionChanged"

MAX_ATTEMPTS = 5
_EVENT_TYPES = {ORDER_SUBMITTED, ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED, POSITION_CHANGED}


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _enabled(root: str | Path) -> bool:
    settings = storage_settings(root)
    return bool(settings.sqlite_enabled and settings.outbox_enabled)


def enqueue(
    root: str | Path,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str = "",
    account_id: str = "",
    book_id: str = "",
    status: str = "pending",
) -> bool:
    """插入一条事实；重复 event_id 直接忽略。SQLite 不可用时返回 False。"""
    if event_type not in _EVENT_TYPES or not _enabled(root):
        return False
    settings = storage_settings(root)
    key = str(event_id or "").strip() or f"{event_type}:{uuid.uuid4().hex}"
    stamp = _now()
    try:
        with DB_LOCK, closing(connect(settings.db_path)) as conn, conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_outbox (
                    event_id, type, account_id, book_id, payload_json,
                    status, attempts, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, '', ?, ?)
                """,
                (
                    key,
                    event_type,
                    str(account_id or ""),
                    str(book_id or ""),
                    json_text(payload),
                    status,
                    stamp,
                    stamp,
                ),
            )
            return cursor.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("发件箱写入失败（交易继续）: {}", exc)
        return False


def mark(
    root: str | Path,
    event_id: str,
    status: str,
    *,
    error: str = "",
    bump_attempts: bool = False,
) -> None:
    if not _enabled(root):
        return
    settings = storage_settings(root)
    try:
        with DB_LOCK, closing(connect(settings.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE event_outbox
                   SET status = ?,
                       attempts = attempts + ?,
                       last_error = ?,
                       updated_at = ?
                 WHERE event_id = ?
                """,
                (
                    status,
                    1 if bump_attempts else 0,
                    str(error or ""),
                    _now(),
                    str(event_id),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("发件箱状态更新失败 {} -> {}: {}", event_id, status, exc)


def mark_ready(root: str | Path, event_id: str) -> None:
    mark(root, event_id, "ready")


def _rows(root: str | Path, status: str, limit: int) -> list[dict[str, Any]]:
    if not _enabled(root):
        return []
    settings = storage_settings(root)
    conn = connect_readonly(settings.db_path)
    if conn is None:
        return []
    try:
        with closing(conn):
            rows = conn.execute(
                """
                SELECT * FROM event_outbox
                 WHERE status = ?
                 ORDER BY seq
                 LIMIT ?
                """,
                (status, max(1, int(limit))),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("发件箱读取失败: {}", exc)
        return []
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        events.append(
            {
                "seq": int(row["seq"]),
                "event_id": str(row["event_id"]),
                "type": str(row["type"]),
                "account_id": str(row["account_id"]),
                "book_id": str(row["book_id"]),
                "payload": payload if isinstance(payload, dict) else {},
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
                "last_error": str(row["last_error"]),
                "created_at": str(row["created_at"]),
            }
        )
    return events


def pending_events(root: str | Path, limit: int = 200) -> list[dict[str, Any]]:
    return _rows(root, "pending", limit)


def ready_events(root: str | Path, limit: int = 200) -> list[dict[str, Any]]:
    return _rows(root, "ready", limit)


def _fail(root: str | Path, event: dict[str, Any], exc: Exception, *, keep: str) -> None:
    attempts = int(event.get("attempts") or 0) + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else keep
    mark(root, str(event["event_id"]), status, error=str(exc)[:500], bump_attempts=True)
    logger.warning(
        "发件箱事件处理失败 {}/{} {} {}: {}",
        attempts,
        MAX_ATTEMPTS,
        event.get("type"),
        event.get("event_id"),
        exc,
    )
    if status == "failed":
        book = str(event.get("book_id") or event.get("account_id") or "")
        msg = (
            f"发件箱事件失败（已达最大重试 {MAX_ATTEMPTS} 次）："
            f"{event.get('type')} {event.get('event_id')} 账本 {book}：{str(exc)[:200]}"
        )
        logger.error(msg)
        try:
            from src.ops.alerts import emit_alert

            emit_alert("outbox_failed", msg, level="error")
        except Exception:  # noqa: BLE001 - 告警失败不影响事件流转
            pass


def report_failed(root: str | Path, limit: int = 200) -> int:
    """启动时汇总存量 failed 事件并告警，避免静默堆积。返回 failed 数量。"""
    failed = _rows(root, "failed", limit)
    if not failed:
        return 0
    preview = ", ".join(str(e["event_id"]) for e in failed[:5])
    msg = f"发件箱存在 {len(failed)} 条 failed 事件未处理，请人工核对后重投（首批：{preview}）"
    logger.error(msg)
    try:
        from src.ops.alerts import emit_alert

        emit_alert("outbox_failed_backlog", msg, level="error")
    except Exception:  # noqa: BLE001
        pass
    return len(failed)


def _active_book(root: str | Path) -> str:
    from src.domain.accounts import load_active_id

    return str(load_active_id(root) or "")


_ORDER_FIELDS = (
    "ts",
    "trace_id",
    "stage",
    "symbol",
    "direction",
    "offset",
    "volume",
    "order_price",
    "fill_price",
    "fill_volume",
    "filled",
    "status",
    "message",
    "order_ref",
    "order_sys_id",
    "trade_id",
    "bar_id",
)


def enqueue_order_stage(root: str | Path, row: dict[str, Any]) -> bool:
    """订单时间线只把「已发出」和终态拒单/撤单落发件箱；成交由入账路径另行落库。"""
    stage = str(row.get("stage") or "")
    trace_id = str(row.get("trace_id") or "").strip()
    if not trace_id:
        return False
    book_id = _active_book(root)
    if stage == "order_sent":
        event_type = ORDER_SUBMITTED
        event_id = f"order:{book_id}:{trace_id}:submitted"
    elif stage == "broker_report":
        status = str(row.get("status") or "")
        if status == "rejected":
            event_type = ORDER_REJECTED
        elif status == "cancelled":
            event_type = ORDER_CANCELLED
        else:
            return False
        event_id = f"order:{book_id}:{trace_id}:{status}"
    else:
        return False
    payload = {key: row.get(key) for key in _ORDER_FIELDS if row.get(key) not in (None, "")}
    payload["account_id"] = book_id
    payload["book_id"] = book_id
    return enqueue(
        root,
        event_type,
        payload,
        event_id=event_id,
        account_id=book_id,
        book_id=book_id,
        status="ready",
    )


def recover_pending(
    root: str | Path,
    handler: Callable[[dict[str, Any]], None],
    limit: int = 200,
) -> int:
    """启动时把 pending 补成 ready；补齐不了的保留 pending，等下次再试。"""
    done = 0
    for event in pending_events(root, limit):
        try:
            handler(event)
            mark_ready(root, str(event["event_id"]))
            done += 1
        except Exception as exc:  # noqa: BLE001
            _fail(root, event, exc, keep="pending")
    if done:
        logger.info("发件箱启动恢复完成：{} 条", done)
    return done


def process_ready(
    root: str | Path,
    handler: Callable[[dict[str, Any]], None],
    limit: int = 200,
) -> int:
    """后台单线程入口：按 seq 顺序消费 ready 事件。"""
    done = 0
    for event in ready_events(root, limit):
        try:
            handler(event)
            mark(root, str(event["event_id"]), "done")
            done += 1
        except Exception as exc:  # noqa: BLE001
            _fail(root, event, exc, keep="ready")
    return done


class OutboxDispatcher:
    """一个工作区的单消费线程；幂等启动，停止时把当前批次处理完。"""

    def __init__(
        self,
        root: str | Path,
        handler: Callable[[dict[str, Any]], None],
        *,
        interval: float = 0.5,
    ) -> None:
        self.root = Path(root)
        self.handler = handler
        self.interval = max(0.1, float(interval))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="outbox-consumer", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = process_ready(self.root, self.handler)
            except Exception as exc:  # noqa: BLE001
                logger.warning("发件箱消费异常: {}", exc)
                processed = 0
            if processed:
                continue
            self._wake.wait(self.interval)
            self._wake.clear()


_DISPATCH_LOCK = threading.Lock()
_DISPATCHERS: dict[str, OutboxDispatcher] = {}


def start_dispatcher(
    root: str | Path,
    handler: Callable[[dict[str, Any]], None],
) -> OutboxDispatcher | None:
    if not _enabled(root):
        return None
    key = str(Path(root).resolve())
    with _DISPATCH_LOCK:
        existing = _DISPATCHERS.get(key)
        if existing is not None:
            existing.wake()
            return existing
        dispatcher = OutboxDispatcher(root, handler)
        report_failed(root)
        dispatcher.start()
        _DISPATCHERS[key] = dispatcher
        return dispatcher


def stop_dispatcher(root: str | Path, timeout: float = 3.0) -> None:
    key = str(Path(root).resolve())
    with _DISPATCH_LOCK:
        dispatcher = _DISPATCHERS.pop(key, None)
    if dispatcher is not None:
        dispatcher.stop(timeout)


def wake_dispatcher(root: str | Path) -> None:
    key = str(Path(root).resolve())
    with _DISPATCH_LOCK:
        dispatcher = _DISPATCHERS.get(key)
    if dispatcher is not None:
        dispatcher.wake()
