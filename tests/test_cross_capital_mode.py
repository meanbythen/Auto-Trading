"""交叉测试资金口径：按保证金效率排名的开关。"""

from __future__ import annotations

from src.screen.picker import _assign_ranks, mark_relative_better


def _row(code: str, *, trades: int, score: float, net_pnl: float, margin: float | None) -> dict:
    return {
        "symbol": code,
        "trades": trades,
        "score": score,
        "net_pnl": net_pnl,
        "profit_factor": 1.5,
        "margin_used": margin or 0.0,
        "return_rate": (net_pnl / margin) if margin else None,
        "error": "",
        "picked": "",
    }


def test_score_mode_keeps_existing_order():
    rows = [
        _row("a", trades=10, score=90.0, net_pnl=900.0, margin=100_000.0),
        _row("b", trades=10, score=10.0, net_pnl=100.0, margin=1_000.0),
    ]
    _assign_ranks(rows)
    assert [row["rank"] for row in rows] == [1, 2]


def test_capital_mode_ranks_by_return_rate():
    rows = [
        _row("a", trades=10, score=90.0, net_pnl=900.0, margin=100_000.0),
        _row("b", trades=10, score=10.0, net_pnl=100.0, margin=1_000.0),
    ]
    _assign_ranks(rows, rank_by="return_rate")
    by_symbol = {row["symbol"]: row["rank"] for row in rows}
    # b 的资金效率 10% 高于 a 的 0.9%。
    assert by_symbol == {"a": 2, "b": 1}


def test_capital_mode_puts_missing_return_rate_last():
    rows = [
        _row("no_margin", trades=10, score=999.0, net_pnl=500.0, margin=None),
        _row("has_margin", trades=10, score=1.0, net_pnl=100.0, margin=1_000.0),
    ]
    _assign_ranks(rows, rank_by="return_rate")
    by_symbol = {row["symbol"]: row["rank"] for row in rows}
    assert by_symbol == {"has_margin": 1, "no_margin": 2}


def test_thin_sample_is_not_ranked():
    rows = [
        _row("thin", trades=2, score=999.0, net_pnl=900.0, margin=1_000.0),
        _row("ok", trades=10, score=1.0, net_pnl=100.0, margin=1_000.0),
    ]
    _assign_ranks(rows, rank_by="return_rate")
    assert rows[0]["picked"] == "未入选"
    assert rows[0]["rank"] == 0
    assert rows[1]["rank"] == 1


def test_mark_relative_better_passes_rank_mode_through():
    rows = [
        _row("a", trades=10, score=90.0, net_pnl=900.0, margin=100_000.0),
        _row("b", trades=10, score=10.0, net_pnl=100.0, margin=1_000.0),
    ]
    mark_relative_better(rows, rank_by="return_rate")
    by_symbol = {row["symbol"]: row["rank"] for row in rows}
    assert by_symbol == {"a": 2, "b": 1}
    assert {row["picked"] for row in rows} <= {"较好", "一般"}
