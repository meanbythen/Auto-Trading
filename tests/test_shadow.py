# -*- coding: utf-8 -*-
"""影子盘：仿真账与真实账并排、粗对齐。"""
from src.account.signal_book import append_trade, new_book, save_named_book
from src.account.workspace import CTP_LIVE, CTP_SIM
from src.review.shadow import format_shadow_lines, shadow_report


def _book(root, account: str, rows: list[tuple[str, str, float, str]]) -> None:
    book = new_book(100000, version="test")
    book["funding"] = "ctp"
    for offset, direction, price, when in rows:
        append_trade(
            book,
            symbol="fu2701",
            offset=offset,
            direction=direction,
            price=price,
            volume=1,
            multiplier=10,
            when=when,
        )
    save_named_book(root, account, book)


def test_shadow_pairs_same_day_and_direction(tmp_path):
    _book(
        tmp_path,
        CTP_SIM,
        [
            ("开仓", "多", 3000.0, "2026-11-18 10:00:00"),
            ("平仓", "多", 3010.0, "2026-11-18 10:30:00"),
        ],
    )
    _book(
        tmp_path,
        CTP_LIVE,
        [
            ("开仓", "多", 3002.0, "2026-11-18 10:01:00"),
            ("平仓", "多", 3010.0, "2026-11-18 10:31:00"),
        ],
    )

    report = shadow_report(tmp_path, symbol="fu2701")
    assert report.sim.trades == 1 and report.sim.pnl == 100.0
    assert report.live.trades == 1 and report.live.pnl == 80.0
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group["sim_n"] == 1 and group["live_n"] == 1
    assert group["entry_gap"] == 2.0
    assert group["pnl_gap"] == -20.0

    text = "\n".join(format_shadow_lines(report))
    assert "逐笔对齐" in text and "对齐 1 组" in text
    assert "开仓均价差 +2.00" in text


def test_shadow_marks_one_side_only(tmp_path):
    _book(
        tmp_path,
        CTP_LIVE,
        [
            ("开仓", "空", 3900.0, "2026-11-19 21:10:00"),
            ("平仓", "空", 3880.0, "2026-11-19 21:40:00"),
        ],
    )

    report = shadow_report(tmp_path)
    assert report.sim.trades == 0
    assert report.live.trades == 1
    text = "\n".join(format_shadow_lines(report))
    assert "实盘独有 1 笔" in text
    assert "仿真账还没有平仓" in text or "样本偏少" in text


def test_shadow_empty_books_say_so(tmp_path):
    report = shadow_report(tmp_path)
    assert report.both_empty
    text = "\n".join(format_shadow_lines(report))
    assert "两边都还没有已平仓成交" in text
