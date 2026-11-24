"""P2 集成：发件箱补记 → JSON 账本 → SQLite 投影 → 重启/重建。

验证的是整条持久化链，而不是单个函数：
成交事件先落发件箱，重启后补进权威 JSON 账本，再同步到可重建的 SQLite 投影；
重复投递、再次重启和投影库丢失都不能改变最终账实。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.account import outbox
from src.account.signal_book import (
    apply_outbox_event,
    load_book,
    new_book,
    open_position_rows,
    save_book,
)
from src.account.sqlite_ledger import load_sqlite_book
from src.domain.accounts import load_active_id

SYMBOL = "bu2601"
FILL_KEY = "ctp:1001:2"


def _db(root: Path) -> Path:
    return root / "data" / "trading.db"


def _fill_count(root: Path, book_id: str) -> int:
    with sqlite3.connect(_db(root)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE book_id = ?", (book_id,)
        ).fetchone()
    return int(row[0] if row else 0)


def _seed_pending_fill(root: Path, book_id: str) -> None:
    book = new_book(100_000, version="test", started_at="2026-11-24 08:00:00")
    save_book(root, book)
    outbox.enqueue(
        root,
        outbox.ORDER_FILLED,
        {
            "symbol": SYMBOL,
            "offset": "开仓",
            "direction": "多",
            "price": 3000.0,
            "volume": 2,
            "multiplier": 10.0,
            "commission": 5.0,
            "fill_key": FILL_KEY,
            "book_id": book_id,
        },
        event_id=f"fill:{book_id}:{FILL_KEY}",
        book_id=book_id,
        status="pending",
    )


def _handler(root: Path):
    return lambda event: apply_outbox_event(root, event)


def test_pending_fill_reaches_json_ledger_and_sqlite_projection(tmp_path: Path) -> None:
    book_id = str(load_active_id(tmp_path))
    _seed_pending_fill(tmp_path, book_id)

    assert outbox.recover_pending(tmp_path, _handler(tmp_path)) == 1

    book = load_book(tmp_path)
    assert book is not None
    assert [row["fill_key"] for row in book["trades"]] == [FILL_KEY]
    positions = open_position_rows(book)
    assert [(row["symbol"], row["side"], row["volume"]) for row in positions] == [
        (SYMBOL, "long", 2)
    ]

    projected = load_sqlite_book(tmp_path, book_id)
    assert projected is not None
    assert [row["fill_key"] for row in projected["trades"]] == [FILL_KEY]
    assert _fill_count(tmp_path, book_id) == 1


def test_restart_replays_pending_once_and_keeps_projection_consistent(tmp_path: Path) -> None:
    book_id = str(load_active_id(tmp_path))
    _seed_pending_fill(tmp_path, book_id)
    handler = _handler(tmp_path)

    assert outbox.recover_pending(tmp_path, handler) == 1
    # 模拟进程重启：同一份 pending 不在了，账本和投影都不能再变。
    assert outbox.recover_pending(tmp_path, handler) == 0

    book = load_book(tmp_path)
    assert book is not None
    assert len(book["trades"]) == 1
    assert _fill_count(tmp_path, book_id) == 1


def test_ready_duplicate_after_restart_does_not_double_book(tmp_path: Path) -> None:
    book_id = str(load_active_id(tmp_path))
    _seed_pending_fill(tmp_path, book_id)
    handler = _handler(tmp_path)
    assert outbox.recover_pending(tmp_path, handler) == 1

    # 柜台在重启后又推了一次同一笔成交，事件号不同但 fill_key 相同。
    outbox.enqueue(
        tmp_path,
        outbox.ORDER_FILLED,
        {
            "symbol": SYMBOL,
            "offset": "开仓",
            "direction": "多",
            "price": 3000.0,
            "volume": 2,
            "multiplier": 10.0,
            "commission": 5.0,
            "fill_key": FILL_KEY,
            "book_id": book_id,
        },
        event_id=f"fill:{book_id}:{FILL_KEY}:replay",
        book_id=book_id,
        status="ready",
    )
    assert outbox.process_ready(tmp_path, handler) >= 1
    assert outbox.ready_events(tmp_path) == []

    book = load_book(tmp_path)
    assert book is not None
    assert [row["fill_key"] for row in book["trades"]] == [FILL_KEY]
    assert _fill_count(tmp_path, book_id) == 1


def test_projection_can_be_rebuilt_from_json_after_db_loss(tmp_path: Path) -> None:
    book_id = str(load_active_id(tmp_path))
    _seed_pending_fill(tmp_path, book_id)
    assert outbox.recover_pending(tmp_path, _handler(tmp_path)) == 1

    for suffix in ("", "-wal", "-shm"):
        Path(str(_db(tmp_path)) + suffix).unlink(missing_ok=True)
    assert not _db(tmp_path).exists()

    book = load_book(tmp_path)
    assert book is not None
    save_book(tmp_path, book)

    rebuilt = load_sqlite_book(tmp_path, book_id)
    assert rebuilt is not None
    assert [row["fill_key"] for row in rebuilt["trades"]] == [FILL_KEY]
    assert _fill_count(tmp_path, book_id) == 1
