"""P1：JSON 账本/订单与 SQLite 投影一致，投影故障不影响 JSON。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import yaml

from src.account.signal_book import append_trade, book_path, new_book, save_book, today_realized_pnl
from src.account.signal_stats import analyze_book
from src.account.sqlite_ledger import SQLiteLedgerStore, load_sqlite_book
from src.trader.order_timeline import OrderTimeline


def _rows(db: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def _book() -> dict:
    book = new_book(10_000, version="49.18.2", started_at="2026-11-20 18:00:00")
    assert append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=1,
        multiplier=10,
        fill_key="open-1",
        when="2026-11-20 09:00:00",
    )
    assert append_trade(
        book,
        symbol="bu2612",
        offset="平仓",
        direction="多",
        price=3520,
        volume=1,
        multiplier=10,
        commission=2,
        fill_key="close-1",
        when="2026-11-20 09:05:00",
    )
    return book


def test_book_and_order_dual_write_share_stats_and_reconcile(tmp_path: Path) -> None:
    book = _book()
    save_book(tmp_path, book)
    timeline = OrderTimeline(tmp_path)
    timeline.record("risk_intent", trace_id="OT-test", symbol="bu2612", side="buy_open", volume=1, allowed=True)
    timeline.record(
        "order_sent",
        trace_id="OT-test",
        symbol="bu2612",
        side="buy_open",
        volume=1,
        order_price=3501,
    )

    db = tmp_path / "data" / "trading.db"
    assert _rows(db, "SELECT COUNT(*), SUM(volume) FROM fills") == [(2, 2)]
    assert _rows(db, "SELECT kind, amount FROM cashflows ORDER BY seq") == [
        ("initial", 10_000.0),
        ("commission", -2.0),
        ("pnl", 200.0),
    ]
    assert _rows(db, "SELECT source, equity FROM equity_snapshots ORDER BY seq")[-1] == ("changes", 10_198.0)
    assert _rows(db, "SELECT COUNT(*) FROM positions") == [(0,)]
    assert _rows(db, "SELECT COUNT(*), stage, price FROM orders") == [(1, "order_sent", 3501.0)]

    store = SQLiteLedgerStore(tmp_path)
    loaded = load_sqlite_book(tmp_path, "live")
    assert loaded is not None
    assert analyze_book(loaded, start=date(2026, 11, 20), end=date(2026, 11, 20)).account.net_pnl == 200
    assert store.reconcile_day(book, "2026-11-20", realized_pnl=today_realized_pnl) == {
        "book_id": "live",
        "day": "2026-11-20",
        "json_fills": 2,
        "sqlite_fills": 2,
        "json_net_pnl": 198.0,
        "sqlite_net_pnl": 198.0,
        "ok": True,
    }


def test_open_position_and_ctp_equity_use_existing_book_rules(tmp_path: Path) -> None:
    book = new_book(20_000, version="49.18.2", started_at="2026-11-20 08:00:00")
    assert append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="空",
        price=3500,
        volume=2,
        multiplier=10,
        fill_key="open-short",
        when="2026-11-20 09:00:00",
    )
    book["funding"] = "ctp"
    book["ctp_history"] = [{"time": "2026-11-20 09:01:00", "balance": 20_123.0}]
    save_book(tmp_path, book)

    db = tmp_path / "data" / "trading.db"
    assert _rows(db, "SELECT symbol, side, volume, entry_price FROM positions") == [
        ("bu2612", "short", 2, 3500.0)
    ]
    assert _rows(db, "SELECT source, equity FROM equity_snapshots") == [("ctp", 20_123.0)]


def test_sqlite_failure_does_not_undo_json(tmp_path: Path, monkeypatch) -> None:
    import src.account.sqlite_ledger as sqlite_ledger

    def fail(_root, _book, **_kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(sqlite_ledger, "mirror_book_to_sqlite", fail)
    book = _book()
    save_book(tmp_path, book)

    saved = json.loads(book_path(tmp_path).read_text(encoding="utf-8"))
    assert len(saved["trades"]) == 2


def test_switch_off_keeps_json_and_does_not_create_or_read_sqlite(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        yaml.safe_dump({"storage": {"sqlite_enabled": False}}), encoding="utf-8"
    )
    book = _book()
    save_book(tmp_path, book)
    OrderTimeline(tmp_path).record("order_sent", trace_id="OT-off", symbol="bu2612", volume=1)

    assert book_path(tmp_path).exists()
    assert (tmp_path / "data" / "order_timeline.jsonl").exists()
    assert not (tmp_path / "data" / "trading.db").exists()
    assert load_sqlite_book(tmp_path, "live") is None
