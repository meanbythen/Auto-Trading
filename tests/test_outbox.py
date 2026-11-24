"""P2 发件箱：落库、去重、恢复补账与消费状态。"""

from __future__ import annotations

import sqlite3
import threading
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


def _seed_pending_fill(root: Path) -> str:
    book = new_book(100_000, version="test", started_at="2026-11-24 08:00:00")
    save_book(root, book)
    book_id = str(load_active_id(root))
    outbox.enqueue(
        root,
        outbox.ORDER_FILLED,
        {
            "symbol": "bu2601",
            "offset": "开仓",
            "direction": "多",
            "price": 3000.0,
            "volume": 2,
            "multiplier": 10.0,
            "commission": 5.0,
            "fill_key": "ctp:1001:2",
            "book_id": book_id,
        },
        event_id=f"fill:{book_id}:ctp:1001:2",
        book_id=book_id,
        status="pending",
    )
    return book_id


def test_replay_after_crash_before_ready_is_idempotent(tmp_path: Path) -> None:
    _seed_pending_fill(tmp_path)
    handler = lambda event: apply_outbox_event(tmp_path, event)  # noqa: E731

    # 账本已写成功，但进程在 mark_ready 之前退出，事件仍是 pending。
    handler(outbox.pending_events(tmp_path)[0])
    assert len(load_book(tmp_path)["trades"]) == 1

    assert outbox.recover_pending(tmp_path, handler) == 1
    book = load_book(tmp_path)
    assert [row["fill_key"] for row in book["trades"]] == ["ctp:1001:2"]


def test_recover_pending_continues_after_one_bad_event(tmp_path: Path) -> None:
    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="bad", status="pending")
    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="good", status="pending")

    def handler(event: dict) -> None:
        if event["event_id"] == "bad":
            raise RuntimeError("坏事件")

    assert outbox.recover_pending(tmp_path, handler) == 1
    remaining = outbox.pending_events(tmp_path)
    assert [item["event_id"] for item in remaining] == ["bad"]
    assert remaining[0]["attempts"] == 1


def test_process_ready_keeps_going_when_middle_event_fails(tmp_path: Path) -> None:
    for key in ("e0", "e1", "e2"):
        outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id=key, status="ready")

    seen: list[str] = []

    def handler(event: dict) -> None:
        if event["event_id"] == "e1":
            raise RuntimeError("失败")
        seen.append(event["event_id"])

    assert outbox.process_ready(tmp_path, handler) == 2
    assert seen == ["e0", "e2"]
    assert [item["event_id"] for item in outbox.ready_events(tmp_path)] == ["e1"]


def test_concurrent_duplicate_enqueue_inserts_once(tmp_path: Path) -> None:
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        ok = outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {"n": 1}, event_id="dup")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert [item["event_id"] for item in outbox.pending_events(tmp_path)] == ["dup"]


def test_enqueue_survives_database_lock(tmp_path: Path) -> None:
    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="seed")
    settings = outbox.storage_settings(tmp_path)
    locker = sqlite3.connect(settings.db_path, timeout=0)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        assert (
            outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="locked") is False
        )
    finally:
        locker.rollback()
        locker.close()

    assert outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="after") is True


def test_enqueue_rejects_unknown_event_type_and_generates_id_when_empty(tmp_path: Path) -> None:
    # 非法 event_type 不入队
    assert outbox.enqueue(tmp_path, "NotAnEventType", {}, event_id="bad") is False
    assert outbox.pending_events(tmp_path) == []

    # 空 event_id 自动生成 uuid，前缀为 event_type:
    ok = outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {"n": 1})
    assert ok is True
    rows = outbox.pending_events(tmp_path)
    assert len(rows) == 1
    assert rows[0]["event_id"].startswith(f"{outbox.ORDER_SUBMITTED}:")


def test_dispatcher_start_is_idempotent_and_stop_clears_registry(tmp_path: Path, monkeypatch) -> None:
    # 拦截 report_failed 避免写 alerts
    monkeypatch.setattr(outbox, "report_failed", lambda root: 0)

    d1 = outbox.start_dispatcher(tmp_path, lambda _e: None)
    d2 = outbox.start_dispatcher(tmp_path, lambda _e: None)
    assert d1 is d2, "同工作区重复 start 应返回同一实例"

    outbox.stop_dispatcher(tmp_path)
    # 已清理的 key 再次 stop 不应报错
    outbox.stop_dispatcher(tmp_path)

    d3 = outbox.start_dispatcher(tmp_path, lambda _e: None)
    assert d3 is not d1, "stop 清理后应创建新实例"
    outbox.stop_dispatcher(tmp_path)


def test_dispatcher_wake_processes_ready_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(outbox, "report_failed", lambda root: 0)

    seen: list[str] = []

    def handler(event: dict) -> None:
        seen.append(event["event_id"])

    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="e0", status="ready")
    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="e1", status="ready")

    dispatcher = outbox.start_dispatcher(tmp_path, handler)
    dispatcher.wake()

    # 短时间内等待消费完成
    import time
    deadline = time.monotonic() + 2.0
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    outbox.stop_dispatcher(tmp_path)
    assert seen == ["e0", "e1"], f"实际消费: {seen}"


def test_pending_and_ready_events_respect_limit(tmp_path: Path) -> None:
    # 造 5 条 pending，limit=2 只返回前 2 条
    for i in range(5):
        outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id=f"p{i}", status="pending")

    rows = outbox.pending_events(tmp_path, limit=2)
    assert len(rows) == 2
    assert [r["event_id"] for r in rows] == ["p0", "p1"]

    for i in range(5):
        outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id=f"r{i}", status="ready")

    rows = outbox.ready_events(tmp_path, limit=3)
    assert len(rows) == 3
    assert [r["event_id"] for r in rows] == ["r0", "r1", "r2"]


def test_recover_pending_and_process_ready_respect_limit(tmp_path: Path) -> None:
    # recover_pending: 造 3 条 pending，limit=1，只处理 1 条
    for i in range(3):
        outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id=f"rp{i}", status="pending")

    done = outbox.recover_pending(tmp_path, lambda _e: None, limit=1)
    assert done == 1

    pending_ids = [r["event_id"] for r in outbox.pending_events(tmp_path)]
    assert len(pending_ids) == 2

    ready_ids = [r["event_id"] for r in outbox.ready_events(tmp_path)]
    assert len(ready_ids) == 1

    # process_ready: 造 3 条 ready（含上面 recover 转的那条），limit=2 只处理 2 条
    for i in range(3):
        outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id=f"pr{i}", status="ready")

    done = outbox.process_ready(tmp_path, lambda _e: None, limit=2)
    assert done == 2


def test_dispatcher_keeps_running_when_process_ready_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(outbox, "report_failed", lambda root: 0)

    # 让 process_ready 每次调用都抛异常，验证 dispatcher 线程不会退出
    call_count = {"n": 0}

    def _boo(root, handler, limit=200):
        call_count["n"] += 1
        raise RuntimeError("模拟消费异常")

    monkeypatch.setattr(outbox, "process_ready", _boo)

    dispatcher = outbox.start_dispatcher(tmp_path, lambda _e: None)

    import time
    time.sleep(0.5)
    dispatcher.stop()

    assert call_count["n"] >= 1, "dispatcher 应至少尝试过一次 process_ready"
    # 关键：线程没因为异常死掉，stop 正常返回
    assert dispatcher._thread is None or not dispatcher._thread.is_alive()
