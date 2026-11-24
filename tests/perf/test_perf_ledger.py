"""PERF-09：成交入账到 SQLite 投影可见的单笔延迟。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.account.signal_book import append_trade, load_book, new_book, save_book
from src.account.sqlite_ledger import load_sqlite_book
from src.domain.accounts import load_active_id

pytestmark = pytest.mark.perf

FILLS = 200
WARMUP = 20


def _fill_count(root: Path, book_id: str) -> int:
    with sqlite3.connect(root / "data" / "trading.db") as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE book_id = ?", (book_id,)
        ).fetchone()
    return int(row[0] if row else 0)


def test_perf_fill_booking_latency(perf_recorder, perf_workspace) -> None:
    book_id = str(load_active_id(perf_workspace))
    book = new_book(1_000_000, version="perf", started_at="2026-11-24 08:00:00")
    save_book(perf_workspace, book)

    samples: list[float] = []
    booked = 0
    for index in range(FILLS):
        offset = "开仓" if index % 2 == 0 else "平仓"
        started = time.perf_counter()
        booked += int(
            bool(
                append_trade(
                    book,
                    symbol="bu2612",
                    offset=offset,
                    direction="多",
                    price=3800.0 + index * 0.2,
                    volume=1,
                    multiplier=10.0,
                    commission=1.5,
                    fill_key=f"perf:{index}",
                )
            )
        )
        save_book(perf_workspace, book)
        samples.append((time.perf_counter() - started) * 1000.0)

    assert booked == FILLS
    reloaded = load_book(perf_workspace)
    assert reloaded is not None and len(reloaded["trades"]) == FILLS
    assert _fill_count(perf_workspace, book_id) == FILLS
    assert load_sqlite_book(perf_workspace, book_id) is not None

    perf_recorder.record(
        "PERF-09",
        samples[WARMUP:],
        unit="ms",
        threshold=200,
        relative_floor=5,
        note="每笔 append_trade + save_book（含 SQLite 投影），共 200 笔",
    )
