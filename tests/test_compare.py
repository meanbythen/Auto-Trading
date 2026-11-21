"""A/B 对比：两份回测结果的逐项差异。"""

from __future__ import annotations

from types import SimpleNamespace

from src.backtest.compare import compare_results, format_compare_lines, summarize


def _result(values: list[float]) -> SimpleNamespace:
    days = [f"2024-01-{index:02d}" for index in range(2, 2 + len(values))]
    return SimpleNamespace(equity=list(values), equity_times=days, trades=[], params_snapshot={})


def test_summarize_return_and_drawdown():
    summary = summarize(_result([100.0, 110.0, 105.0, 120.0]))
    assert abs(summary["return_pct"] - 0.2) < 1e-12
    assert summary["max_drawdown_pct"] > 0


def test_compare_marks_winner_per_dimension():
    left = _result([100.0, 120.0, 110.0, 130.0])
    right = _result([100.0, 101.0, 100.5, 103.0])
    report = compare_results(left, right, names=("参数1", "参数2"))
    rows = {row["key"]: row for row in report["rows"]}
    assert rows["return_pct"]["winner"] == "参数1"
    assert rows["max_drawdown_pct"]["winner"] == "参数2"
    assert report["correlation"] is not None
    assert "参数1" in report["conclusion"]


def test_compare_format_lines():
    report = compare_results(_result([100.0, 110.0]), _result([100.0, 105.0]))
    text = "\n".join(format_compare_lines(report))
    assert "A/B 对比" in text
    assert "结论" in text
