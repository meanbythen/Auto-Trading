"""P2.2：1 分钟只复核已有仓的止损，不能用来提前开仓。"""

from __future__ import annotations

import pandas as pd

from src.backtest.minute_review import (
    EVIDENCE_MINUTE,
    EVIDENCE_NONE,
    format_minute_review,
    infer_stop_price,
    review_trades_on_minutes,
    trades_from_backtest_file,
)
from src.domain.trade import TradeRecord


def _trade(**kwargs) -> TradeRecord:
    payload = dict(
        symbol="fu2701",
        side="long",
        entry_time="2026-11-10 10:00:00",
        exit_time="2026-11-10 10:15:00",
        entry_price=2200.0,
        exit_price=2190.0,
        volume=1,
        pnl=-10.0,
        reason_exit="碰到 EMA120 止损",
    )
    payload.update(kwargs)
    return TradeRecord(**payload)


def _minutes(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    index = pd.to_datetime([item[0] for item in rows])
    return pd.DataFrame(
        {
            "open": [item[1] for item in rows],
            "high": [item[2] for item in rows],
            "low": [item[3] for item in rows],
            "close": [item[1] for item in rows],
            "volume": [10.0 for _ in rows],
        },
        index=index,
    )


def test_trades_from_missing_report_are_empty(tmp_path) -> None:
    assert trades_from_backtest_file(tmp_path / "nope.json") == []


def test_format_mentions_no_new_opens() -> None:
    report = review_trades_on_minutes([], None)
    text = format_minute_review(report)
    assert "不会自己找开仓点" in text
    assert "没有1分钟" in text


def test_missing_minutes_stay_conservative() -> None:
    report = review_trades_on_minutes([_trade()], None)
    assert report.evidence_level == EVIDENCE_NONE
    assert report.earlier_stops == 0
    assert report.items[0].reviewed_exit_time == "2026-11-10 10:15:00"


def test_one_minute_can_exit_earlier_but_not_open_earlier() -> None:
    minutes = _minutes(
        [
            ("2026-11-10 09:58:00", 2205, 2206, 2204),  # 开仓前，禁止拿来提前开仓
            ("2026-11-10 10:01:00", 2198, 2199, 2188),  # 盘中先碰到 2190 止损
            ("2026-11-10 10:15:00", 2191, 2192, 2190),
        ]
    )
    report = review_trades_on_minutes([_trade()], minutes, stop_mode="touch")
    assert report.evidence_level == EVIDENCE_MINUTE
    assert report.earlier_stops == 1
    item = report.items[0]
    assert item.earlier_stop is True
    assert "10:01" in item.reviewed_exit_time
    assert "09:58" not in item.reviewed_exit_time
    assert "没有在 1 分钟上寻找新的开仓" in report.notes[0]


def test_without_stop_reason_minutes_do_not_invent_exits() -> None:
    trade = _trade(reason_exit="回测结束强平", exit_price=2210.0)
    assert infer_stop_price(trade) is None
    minutes = _minutes(
        [
            ("2026-11-10 10:01:00", 2100, 2210, 2090),
            ("2026-11-10 10:15:00", 2210, 2211, 2209),
        ]
    )
    report = review_trades_on_minutes([trade], minutes)
    assert report.earlier_stops == 0
    assert report.skipped_no_stop == 1
    assert report.items[0].reviewed_exit_price == 2210.0
