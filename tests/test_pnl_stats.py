"""平仓盈亏统计的口径测试。"""

from __future__ import annotations

import math

from src.domain.pnl_stats import summarize_pnl


def test_zero_as_loss_counts_zero_in_denominator() -> None:
    pnls = [10.0, -5.0, 0.0]
    strict = summarize_pnl(pnls, zero_as_loss=True)
    assert strict.wins == 1
    assert strict.losses == 2
    assert strict.win_rate == 1 / 3
    assert strict.gross_profit == 10.0
    assert strict.gross_loss == 5.0
    assert strict.profit_factor == 2.0

    loose = summarize_pnl(pnls, zero_as_loss=False)
    assert loose.wins == 1
    assert loose.losses == 1
    assert loose.win_rate == 0.5
    assert loose.profit_factor == 2.0


def test_no_loss_with_gain_reports_inf() -> None:
    stats = summarize_pnl([1.0, 2.0])
    assert stats.losses == 0
    assert stats.win_rate == 1.0
    assert stats.profit_factor is not None
    assert math.isinf(stats.profit_factor)


def test_no_trades_reports_none() -> None:
    stats = summarize_pnl([])
    assert stats.trades == 0
    assert stats.win_rate == 0.0
    assert stats.profit_factor is None


def test_all_zero_reports_none() -> None:
    stats = summarize_pnl([0.0, 0.0], zero_as_loss=True)
    assert stats.win_rate == 0.0
    assert stats.gross_profit == 0.0
    assert stats.gross_loss == 0.0
    assert stats.profit_factor is None
