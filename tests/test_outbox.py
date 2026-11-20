"""P2 发件箱：落库、去重、恢复补账与消费状态。"""

from __future__ import annotations

from pathlib import Path

from src.account import outbox
from src.account.signal_book import apply_outbox_event, load_book, new_book, save_book
from src.domain.accounts import load_active_id


def test_enqueue_is_idempotent_and_flows_to_done(tmp_path: Path) -> None:
    first = outbox.enqueue(
        tmp_path,
        outbox.ORDER_SUBMITTED,
        {"symbol": "bu2601"},
        event_id="order:acc:1:submitted",
        status="ready",
    )
    again = outbox.enqueue(
        tmp_path,
        outbox.ORDER_SUBMITTED,
        {"symbol": "bu2601"},
        event_id="order:acc:1:submitted",
        status="ready",
    )
    assert first is True
    assert again is False

    seen: list[dict] = []
    assert outbox.process_ready(tmp_path, seen.append) == 1
    assert [row["event_id"] for row in seen] == ["order:acc:1:submitted"]
    assert outbox.ready_events(tmp_path) == []


def test_recover_pending_fill_writes_ledger_once(tmp_path: Path) -> None:
    book = new_book(100_000, version="test")
    save_book(tmp_path, book)
    book_id = str(load_active_id(tmp_path))
    payload = {
        "symbol": "bu2601",
        "offset": "开仓",
        "direction": "多",
        "price": 3000.0,
        "volume": 2,
        "multiplier": 10.0,
        "commission": 5.0,
        "fill_key": "ctp:1001:2",
        "book_id": book_id,
    }
    outbox.enqueue(
        tmp_path,
        outbox.ORDER_FILLED,
        payload,
        event_id=f"fill:{book_id}:ctp:1001:2",
        book_id=book_id,
        status="pending",
    )

    handler = lambda event: apply_outbox_event(tmp_path, event)  # noqa: E731
    assert outbox.recover_pending(tmp_path, handler) == 1
    saved = load_book(tmp_path)
    assert saved is not None
    assert [row["fill_key"] for row in saved["trades"]] == ["ctp:1001:2"]

    outbox.process_ready(tmp_path, handler)
    assert outbox.ready_events(tmp_path) == []


def test_outbox_can_be_disabled(tmp_path: Path) -> None:
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("storage:\n  sqlite_enabled: true\n  outbox_enabled: false\n", encoding="utf-8")

    assert outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}) is False
    assert outbox.pending_events(tmp_path) == []
    assert outbox.start_dispatcher(
        tmp_path, lambda event: apply_outbox_event(tmp_path, event)
    ) is None


def test_failed_event_stops_after_max_attempts(tmp_path: Path) -> None:
    outbox.enqueue(
        tmp_path,
        outbox.ORDER_SUBMITTED,
        {},
        event_id="order:acc:1:submitted",
        status="ready",
    )

    def _boom(_event: dict) -> None:
        raise RuntimeError("投影失败")

    for _ in range(outbox.MAX_ATTEMPTS):
        outbox.process_ready(tmp_path, _boom)
    assert outbox.ready_events(tmp_path) == []
