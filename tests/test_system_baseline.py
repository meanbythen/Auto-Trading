"""P0.3：信号/账本/回测对照必须能分类，GUI 假定成交不能冒充柜台成交。"""

from __future__ import annotations

from pathlib import Path

from src.review.gap import GapReport
from src.review.system_baseline import (
    BaselineCase,
    build_case,
    classify_difference,
    compare_cases,
    fill_kind_label,
    load_pack,
)
from src.trader.order_timeline import OrderTimeline


FIXTURE = Path(__file__).parent / "fixtures" / "system_baseline_pack.json"


def test_assumed_fill_is_never_channel_fill() -> None:
    assert fill_kind_label("assumed_fill") == "GUI假定成交"
    assert fill_kind_label("channel_fill") == "柜台真实成交"
    assert fill_kind_label("simulated_fill") == "Sim模拟成交"
    assert fill_kind_label("assumed_fill") != fill_kind_label("channel_fill")


def test_timeline_keeps_assumed_fill_stage() -> None:
    timeline = OrderTimeline(None)
    row = timeline.record("assumed_fill", trace_id="OT-demo", symbol="fu2701", side="long", volume=1)
    assert row["stage"] == "assumed_fill"
    assert fill_kind_label(row["stage"]) != fill_kind_label("channel_fill")


def test_classify_empty_day_is_matched() -> None:
    assert classify_difference(replay_opens=0, event_orders=0, book_fills=0) == ["matched"]


def test_classify_assumed_fill_not_real_anomaly() -> None:
    cats = classify_difference(
        replay_opens=1,
        event_orders=1,
        book_fills=1,
        event_codes=["assumed_fill"],
        fill_kinds=["assumed_fill"],
    )
    assert cats == ["assumed_fill"]
    assert "real_anomaly" not in cats


def test_classify_catch_up_and_risk() -> None:
    cats = classify_difference(
        replay_opens=1,
        event_orders=0,
        book_fills=0,
        event_codes=["catch_up_open", "risk_block"],
    )
    assert cats == ["catch_up", "risk_block"]


def test_frozen_pack_regresses_categories() -> None:
    pack = load_pack(FIXTURE)
    by_id = {item["case_id"]: item for item in pack["cases"]}
    for case_id, frozen in by_id.items():
        gap = GapReport(
            replay_opens=int(frozen["replay_opens"]),
            event_orders=int(frozen["event_orders"]),
            book_fills=int(frozen["book_fills"]),
            alignment=str(frozen["alignment"]),
            lines=["对照样本"],
            notes=[],
        )
        current = build_case(
            case_id=case_id,
            symbol=frozen["symbol"],
            engine=frozen["engine"],
            trade_date=frozen["trade_date"],
            gap=gap,
            event_codes=list(frozen["event_codes"]),
            fill_kinds=list(frozen["fill_kinds"]),
        )
        diffs = compare_cases(frozen, current)
        assert diffs == [], f"{case_id} 不应漂移: {diffs}"


def test_engine_change_lists_replay_count() -> None:
    frozen = BaselineCase(
        case_id="empty_aligned",
        symbol="fu2701",
        engine="ema_swing",
        trade_date="2026-11-10",
        categories=["matched"],
        alignment="none",
    )
    changed = BaselineCase(
        case_id="empty_aligned",
        symbol="fu2701",
        engine="ema_swing",
        trade_date="2026-11-10",
        replay_opens=2,
        categories=["config"],
        alignment="signal_only",
    )
    fields = {item.field for item in compare_cases(frozen, changed)}
    assert "replay_opens" in fields
    assert "categories" in fields
