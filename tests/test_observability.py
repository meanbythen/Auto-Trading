"""P4 可观测性：trace 上下文、信号审计链路、结构化日志与指标。"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime

from loguru import logger

from src.account.trading_db import connect
from src.kernel.trace import bind_trace, ensure_trace_id, get_trace_id, new_trace_id, set_trace_id
from src.ops import journal
from src.utils.logger import setup_logger


def test_trace_context_roundtrip() -> None:
    set_trace_id("")
    assert get_trace_id() == ""
    generated = ensure_trace_id()
    assert generated.startswith("OT-")
    assert get_trace_id() == generated
    assert ensure_trace_id() == generated
    with bind_trace("OT-scope") as scoped:
        assert scoped == "OT-scope"
        assert get_trace_id() == "OT-scope"
    assert get_trace_id() == generated
    assert new_trace_id().startswith("OT-")
    set_trace_id("")


def test_signal_to_fill_trace(tmp_path) -> None:
    trace_id = new_trace_id()
    assert journal.record_signal(
        trace_id=trace_id,
        strategy_id="trend_follow",
        symbol="bu2612",
        bar_time="2026-11-21 09:15",
        action="buy_open",
        price=4000.0,
        reason="突破",
        root=tmp_path,
    )
    db_path = journal.db_path_for(tmp_path)
    with closing(connect(db_path)) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO risk_decisions (
                    decision_id, account_id, trading_day, ts, gate, symbol, side,
                    volume, allowed, code, reason, trace_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{trace_id}-risk",
                    "acc_test",
                    "2026-11-21",
                    datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "pretrade",
                    "bu2612",
                    "long",
                    1,
                    1,
                    "ok",
                    "允许开仓",
                    trace_id,
                ),
            )
    assert journal.record_audit(
        "order_submitted",
        trace_id=trace_id,
        symbol="bu2612",
        order_id="",
        message="已报单",
        root=tmp_path,
    )
    assert journal.record_audit(
        "order_filled",
        trace_id=trace_id,
        symbol="bu2612",
        order_id="ORD-1",
        message="成交",
        payload={"fill_price": 4001.0},
        root=tmp_path,
    )

    events = journal.trace_events(trace_id, root=tmp_path)
    assert [item["event"] for item in events] == [
        "signal_generated",
        "risk_decision",
        "order_submitted",
        "order_filled",
    ]
    assert events[0]["bar_time"] == "2026-11-21 09:15"
    assert events[3]["order_id"] == "ORD-1"


def test_trace_events_empty_for_unknown_id(tmp_path) -> None:
    assert journal.trace_events("OT-not-found", root=tmp_path) == []


def test_structured_log_contains_trace_id(tmp_path) -> None:
    setup_logger(str(tmp_path), level="INFO")
    try:
        with bind_trace("OT-log-test"):
            logger.info("结构化日志测试")
        logger.complete()
    finally:
        logger.remove()
        setup_logger("logs", level="INFO")

    files = list(tmp_path.glob("structured_*.jsonl"))
    assert files, "没有生成结构化日志文件"
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    record = json.loads(lines[-1])["record"]
    assert record["message"] == "结构化日志测试"
    assert record["extra"]["trace_id"] == "OT-log-test"


def test_metrics_snapshot_empty_and_with_timeline(tmp_path) -> None:
    empty = journal.metrics_snapshot(root=tmp_path)
    assert empty["orders_sent"] == 0
    assert empty["fills"] == 0
    assert empty["reject_rate"] == 0.0

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = [
        {"ts": "2026-11-21T09:15:00.000+08:00", "stage": "order_sent", "decision_to_send_ms": 40},
        {"ts": "2026-11-21T09:15:01.000+08:00", "stage": "accounted", "stage_ms": 20, "slip_ticks": -1},
        {"ts": "2026-11-21T09:15:02.000+08:00", "stage": "risk_intent", "allowed": False},
    ]
    (data_dir / "order_timeline.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    with (data_dir / "alerts.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": "2026-11-21T09:16:00+08:00", "code": "risk_freeze"}, ensure_ascii=False) + "\n")

    snapshot = journal.metrics_snapshot("2026-11-21", root=tmp_path)
    assert snapshot["orders_sent"] == 1
    assert snapshot["fills"] == 1
    assert snapshot["rejected"] == 1
    assert snapshot["reject_rate"] == 1.0
    assert snapshot["avg_send_ms"] == 40.0
    assert snapshot["avg_fill_ms"] == 20.0
    assert snapshot["avg_slippage"] == 1.0
    assert snapshot["freeze_alerts"] == 1
