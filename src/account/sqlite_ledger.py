"""信号账本和订单时间线的 SQLite 投影。

JSON 仍是默认权威来源。本模块不改成交、不另算持仓或盈亏；整本同步时复用
signal_book 的 FIFO 持仓和 domain.ledger 的权益口径，SQLite 读取后也交回原统计函数。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.account.trading_db import DB_LOCK, connect, json_text, resolve_db_path, storage_settings
from src.domain.accounts import load_active_id
from src.domain.ledger import ctp_history_equity, ensure_changes, equity_points

PositionRows = Callable[[dict[str, Any]], list[dict[str, Any]]]
RealizedPnl = Callable[[dict[str, Any], date | str | None], float]


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _book_id(root: str | Path, book: dict[str, Any]) -> str:
    return str(book.get("account_id") or load_active_id(root))


def _fill_id(book: dict[str, Any], row: dict[str, Any], index: int) -> str:
    marked = str(row.get("fill_key") or "").strip()
    if marked:
        return marked
    # 老账没有成交号：账期起点 + 原顺序 + 原行内容生成稳定 ID，重复同步不重复插行。
    identity = json_text([book.get("started_at"), book.get("period_started_at"), index, row])
    return "json-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class SQLiteLedgerStore:
    """一本账的无损快照 + 可查询规范化表。"""

    def __init__(self, root: str | Path, db_path: str | Path | None = None) -> None:
        self.root = Path(root)
        self.db_path = resolve_db_path(root, db_path)

    def sync_book(self, book: dict[str, Any], *, position_rows: PositionRows) -> None:
        book_id = _book_id(self.root, book)
        account_id = str(book.get("account_id") or book_id)
        stamp = _now()
        positions = position_rows(book)
        if str(book.get("funding") or "") == "ctp":
            source = "ctp"
            points = ctp_history_equity(book)
        else:
            source = "changes"
            points = equity_points(book)
        with DB_LOCK, closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO books (
                        book_id, account_id, started_at, period_started_at,
                        initial_capital, funding, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(book_id) DO UPDATE SET
                        account_id=excluded.account_id,
                        started_at=excluded.started_at,
                        period_started_at=excluded.period_started_at,
                        initial_capital=excluded.initial_capital,
                        funding=excluded.funding,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        book_id,
                        account_id,
                        str(book.get("started_at") or ""),
                        str(book.get("period_started_at") or ""),
                        _float(book.get("initial_capital")),
                        str(book.get("funding") or ""),
                        json_text(book),
                        stamp,
                    ),
                )
                for table in ("fills", "cashflows", "positions", "equity_snapshots"):
                    conn.execute(f"DELETE FROM {table} WHERE book_id = ?", (book_id,))
                for index, raw in enumerate(book.get("trades") or []):
                    if not isinstance(raw, dict):
                        continue
                    row = dict(raw)
                    conn.execute(
                        """
                        INSERT INTO fills (
                            book_id, fill_id, account_id, time, symbol, offset, direction,
                            price, volume, multiplier, commission, bar_id, reason, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            book_id,
                            _fill_id(book, row, index),
                            account_id,
                            str(row.get("time") or ""),
                            str(row.get("symbol") or ""),
                            str(row.get("offset") or ""),
                            str(row.get("direction") or ""),
                            _float(row.get("price")),
                            _int(row.get("volume")),
                            _float(row.get("multiplier")),
                            _float(row.get("commission")),
                            str(row.get("bar_id") or ""),
                            str(row.get("reason") or ""),
                            json_text(row),
                        ),
                    )
                for index, raw in enumerate(book.get("changes") or []):
                    if not isinstance(raw, dict):
                        continue
                    conn.execute(
                        "INSERT INTO cashflows (book_id, seq, time, kind, amount, note) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            book_id,
                            index,
                            str(raw.get("time") or ""),
                            str(raw.get("kind") or ""),
                            _float(raw.get("amount")),
                            str(raw.get("note") or ""),
                        ),
                    )
                for row in positions:
                    conn.execute(
                        """
                        INSERT INTO positions (
                            book_id, symbol, side, volume, entry_price, opened_at, stop_price, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            book_id,
                            str(row.get("symbol") or ""),
                            str(row.get("side") or ""),
                            _int(row.get("volume")),
                            _float(row.get("entry_price")),
                            str(row.get("opened_at") or ""),
                            _float(row.get("stop_price")),
                            stamp,
                        ),
                    )
                for index, (moment, equity) in enumerate(points):
                    conn.execute(
                        "INSERT INTO equity_snapshots (book_id, source, seq, time, equity) VALUES (?, ?, ?, ?, ?)",
                        (book_id, source, index, str(moment), _float(equity)),
                    )

    def sync_order(self, row: dict[str, Any]) -> None:
        """订单 JSONL 每追加一个阶段，就把同一 trace_id 的最新状态 upsert 到 orders。"""
        account_id = str(row.get("account_id") or load_active_id(self.root))
        order_id = str(row.get("trace_id") or row.get("order_id") or "").strip()
        if not order_id:
            return
        stamp = str(row.get("ts") or _now())
        price = row.get("fill_price", row.get("order_price"))
        allowed = row.get("allowed")
        with DB_LOCK, closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orders (
                        account_id, order_id, symbol, side, volume, bar_id, stage,
                        allowed, price, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, order_id) DO UPDATE SET
                        symbol=excluded.symbol,
                        side=excluded.side,
                        volume=excluded.volume,
                        bar_id=excluded.bar_id,
                        stage=excluded.stage,
                        allowed=excluded.allowed,
                        price=COALESCE(excluded.price, orders.price),
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        account_id,
                        order_id,
                        str(row.get("symbol") or ""),
                        str(row.get("side") or ""),
                        _int(row.get("volume")),
                        str(row.get("bar_id") or ""),
                        str(row.get("stage") or ""),
                        None if allowed is None else int(bool(allowed)),
                        None if price in (None, "") else _float(price),
                        json_text(row),
                        stamp,
                        stamp,
                    ),
                )

    def load_book(self, book_id: str) -> dict[str, Any] | None:
        """读 SQLite 中的无损快照，后续仍调用原 signal_book/signal_stats 统计。"""
        with closing(connect(self.db_path)) as conn:
            row = conn.execute("SELECT payload_json FROM books WHERE book_id = ?", (str(book_id),)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("cashflows", [])
        data.setdefault("trades", [])
        ensure_changes(data)
        return data

    def reconcile_day(
        self,
        book: dict[str, Any],
        day: date | str,
        *,
        realized_pnl: RealizedPnl,
    ) -> dict[str, Any]:
        """同日成交笔数与净盈亏对账；净盈亏两侧都调用同一个原实现。"""
        book_id = _book_id(self.root, book)
        stored = self.load_book(book_id)
        target = str(day)[:10]
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute("SELECT time FROM fills WHERE book_id = ?", (book_id,)).fetchall()
        json_fills = sum(
            1 for row in book.get("trades") or [] if str(row.get("time") or "")[:10] == target
        )
        sqlite_fills = sum(1 for row in rows if str(row["time"] or "")[:10] == target)
        json_pnl = realized_pnl(book, target)
        sqlite_pnl = realized_pnl(stored, target) if stored is not None else 0.0
        return {
            "book_id": book_id,
            "day": target,
            "json_fills": json_fills,
            "sqlite_fills": sqlite_fills,
            "json_net_pnl": json_pnl,
            "sqlite_net_pnl": sqlite_pnl,
            "ok": stored is not None and json_fills == sqlite_fills and json_pnl == sqlite_pnl,
        }


def mirror_book_to_sqlite(
    root: str | Path,
    book: dict[str, Any],
    *,
    position_rows: PositionRows,
) -> bool:
    settings = storage_settings(root)
    if not settings.sqlite_enabled:
        return False
    SQLiteLedgerStore(root, settings.db_path).sync_book(book, position_rows=position_rows)
    return True


def mirror_order_to_sqlite(root: str | Path, row: dict[str, Any]) -> bool:
    settings = storage_settings(root)
    if not settings.sqlite_enabled:
        return False
    SQLiteLedgerStore(root, settings.db_path).sync_order(row)
    return True


def load_sqlite_book(root: str | Path, book_id: str | None = None) -> dict[str, Any] | None:
    """供对账/只读查询使用；开关关闭时不拿 SQLite 替代 JSON。"""
    settings = storage_settings(root)
    if not settings.sqlite_enabled:
        return None
    wanted = str(book_id or load_active_id(root))
    return SQLiteLedgerStore(root, settings.db_path).load_book(wanted)
