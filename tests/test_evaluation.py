"""策略评估：门槛、结论、ADX 分组与格式化。"""

from __future__ import annotations

from typing import Any

from src.backtest.evaluation import (
    EvaluationThresholds,
    apply_stress,
    drawdown_duration,
    evaluate_backtest,
    evaluate_day,
    format_evaluation_lines,
    max_consecutive_losses,
    verdict_text,
)
from src.backtest.metrics import TradeRecord, calc_performance


def _trade(pnl: float, index: int, adx: float | None = None) -> TradeRecord:
    day = 1 + index // 60
    minute = index % 60
    return TradeRecord(
        symbol="bu",
        side="long",
        entry_time=f"2026-10-{day:02d} 09:{minute:02d}:00",
        exit_time=f"2026-10-{day:02d} 10:{minute:02d}:00",
        entry_price=100,
        exit_price=100 + pnl,
        volume=1,
        pnl=pnl,
        adx_at_entry=adx,
    )


class _Result:
    """只带评估需要的字段，模拟一次回测结果。"""

    def __init__(
        self,
        trades: list[TradeRecord],
        *,
        capital: float = 10_000.0,
        equity: list[float] | None = None,
        times: list[str] | None = None,
    ) -> None:
        self.trades = trades
        self.equity = equity or _curve(trades, capital)
        self.equity_times = times or [t.exit_time for t in trades]
        self.params_snapshot = {"capital": capital, "margin_used": capital}
        self.performance = calc_performance(self.trades, self.equity, min_trades=1)


def _curve(trades: list[TradeRecord], capital: float) -> list[float]:
    total = capital
    out = [total]
    for trade in trades:
        total += trade.pnl
        out.append(total)
    return out


def test_max_consecutive_losses_counts_longest_run() -> None:
    trades = [
        _trade(10, 1),
        _trade(-5, 2),
        _trade(-5, 3),
        _trade(-5, 4),
        _trade(8, 5),
        _trade(-1, 6),
    ]
    assert max_consecutive_losses(trades) == 3


def test_drawdown_duration_uses_peak_to_trough_days() -> None:
    equity = [100, 120, 90, 95, 130]
    times = [
        "2026-10-01 09:00:00",
        "2026-10-03 09:00:00",
        "2026-10-08 09:00:00",
        "2026-10-09 09:00:00",
        "2026-10-10 09:00:00",
    ]
    assert drawdown_duration(equity, times) == 5


def test_thresholds_from_cfg_clamps_bad_values() -> None:
    th = EvaluationThresholds.from_cfg(
        {
            "evaluation": {
                "profit_factor_min": -5,
                "max_drawdown_pct": 5,
                "perturbation_step_pct": 9,
                "trades_min": -3,
                "enabled": "false",
            }
        }
    )
    assert th.profit_factor_min == 0.0
    assert th.max_drawdown_pct == 1.0
    assert th.perturbation_step_pct == 0.5
    assert th.trades_min == 0
    assert th.enabled is False


def test_weak_backtest_is_rejected_and_names_the_red_lines() -> None:
    trades = [_trade(20, 1)] + [_trade(-10, i) for i in range(2, 12)]
    report = evaluate_backtest(_Result(trades))
    assert report.conclusion == "reject"
    failed = report.failed_text()
    assert "盈利因子" in failed
    # 样本不足不再判红线，只把结论压到观察。
    assert "样本" not in failed
    sample = next(c for c in report.checks if c.key == "sample")
    assert sample.status == "watch"
    assert verdict_text(report) == "淘汰"


def test_small_sample_is_observation_not_red_line() -> None:
    """20 笔但各项指标合格：结论应为观察，而不是淘汰。"""
    trades = [_trade(10 if i % 5 else -5, i) for i in range(1, 21)]
    report = evaluate_backtest(_Result(trades))
    sample = next(c for c in report.checks if c.key == "sample")
    assert sample.status == "watch"
    assert "统计显著性不足" in sample.note
    assert report.conclusion == "watch"
    assert verdict_text(report) == "观察"
    assert "样本" not in report.failed_text()


def test_missing_adx_skips_range_group() -> None:
    trades = [_trade(50, i) for i in range(1, 6)]
    report = evaluate_backtest(_Result(trades))
    range_check = next(c for c in report.checks if c.key == "range_group")
    assert range_check.status == "skip"
    assert "ADX" in range_check.note


def test_adx_groups_split_trend_and_range() -> None:
    trades = [_trade(60, 1, adx=30), _trade(-30, 2, adx=10), _trade(40, 3, adx=25)]
    report = evaluate_backtest(_Result(trades))
    assert report.metrics["trend_trades"] == 2
    assert report.metrics["range_trades"] == 1
    assert report.metrics["range_share"] > 0


def test_day_profile_skips_sample_and_capital_gates() -> None:
    trades = [_trade(30, i) for i in range(1, 4)] + [_trade(-10, 4)]
    report = evaluate_day(trades)
    sample = next(c for c in report.checks if c.key == "sample")
    calmar = next(c for c in report.checks if c.key == "calmar")
    assert sample.status == "skip"
    assert calmar.status == "skip"
    assert all(c.status != "fail" for c in report.checks)


def test_apply_stress_can_turn_a_pass_into_reject() -> None:
    trades = [_trade(90, i) for i in range(1, 151)] + [_trade(-20, 151)]
    report = evaluate_backtest(_Result(trades))
    before = report.conclusion
    apply_stress(
        report,
        slippage={"profit_factor": 0.90, "base_ticks": 0.0, "stress_ticks": 1.0},
        thresholds=EvaluationThresholds(),
    )
    assert before != "reject"
    assert report.conclusion == "reject"
    assert "滑点压力" in report.failed_text()


def test_apply_stress_keeps_earlier_slippage_check() -> None:
    """先加滑点再加扰动，不能把滑点检查项删掉。"""
    trades = [_trade(50, i) for i in range(1, 6)]
    report = evaluate_backtest(_Result(trades))
    report = apply_stress(
        report,
        slippage={
            "profit_factor": 1.1,
            "base_ticks": 0.0,
            "stress_ticks": 1.0,
            "net_pnl": 300.0,
            "trades": 5,
        },
        thresholds=EvaluationThresholds(),
    )
    report = apply_stress(
        report,
        perturbation={"cliff": False, "worst_pf": 1.2, "points_count": 4},
        thresholds=EvaluationThresholds(),
    )

    keys = {check.key for check in report.checks}
    assert {"slippage", "perturbation"} <= keys
    assert report.metrics["slippage_base_ticks"] == 0.0
    assert report.metrics["slippage_stress_ticks"] == 1.0
    assert report.metrics["slippage_net_pnl"] == 300.0
    assert report.metrics["slippage_trades"] == 5.0


def test_format_lines_accepts_serialized_dict() -> None:
    trades = [_trade(50, i) for i in range(1, 6)]
    payload: dict[str, Any] = evaluate_backtest(_Result(trades)).to_dict()
    lines = format_evaluation_lines(payload, title="回测评估")
    assert lines[0].startswith("—— 回测评估")
    assert any("盈利因子" in line for line in lines)


def test_profit_factor_does_not_judge_with_too_few_losers() -> None:
    """只亏过一笔时 PF 是哨兵值，不能拿它当 PASS/FAIL，应该跳过。"""
    trades = [_trade(30, i) for i in range(1, 8)] + [_trade(-5, 8)]
    report = evaluate_backtest(_Result(trades))
    check = next(item for item in report.checks if item.key == "profit_factor")
    assert check.status == "skip"
    assert "亏损样本" in check.note


def test_zero_drawdown_has_no_calmar_instead_of_zero() -> None:
    """一路向上没有回撤时，Calmar 标成不可计算，不能显示成 0.00 再判不合格。"""
    trades = [_trade(30, i) for i in range(1, 6)]
    report = evaluate_backtest(_Result(trades))
    assert report.metrics["calmar"] is None
    check = next(item for item in report.checks if item.key == "calmar")
    assert check.status == "skip"
    lines = format_evaluation_lines(report.to_dict(), title="回测评估")
    assert any("卡尔玛" in line for line in lines)
