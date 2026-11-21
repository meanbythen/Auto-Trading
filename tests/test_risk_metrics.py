"""组合风险：VaR/CVaR、极值窗口、相关性聚类。"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from src.backtest.portfolio import combine_results, format_portfolio_lines
from src.backtest.risk_metrics import (
    MIN_RISK_DAYS,
    cluster_symbols,
    daily_returns,
    format_risk_lines,
    historical_cvar,
    historical_var,
    risk_summary,
    worst_window,
)


def test_daily_returns_uses_last_value_per_day():
    equity = [100.0, 101.0, 102.0, 103.0]
    times = ["2024-01-02 09:00", "2024-01-02 15:00", "2024-01-03 15:00", "2024-01-04 15:00"]
    rows = daily_returns(equity, times)
    # 首日只作基准，没有收益。
    assert [item[0] for item in rows] == ["2024-01-03", "2024-01-04"]
    assert abs(rows[0][1] - (102.0 / 101.0 - 1.0)) < 1e-12


def test_var_and_cvar_on_known_sample():
    returns = [-0.05, -0.04, -0.03, -0.02, -0.01] + [0.0] * 10 + [0.01] * 5
    assert len(returns) == MIN_RISK_DAYS
    assert abs(historical_var(returns, 0.95) - 0.0405) < 1e-9
    # 20 个样本、5% 尾部只有最差 1 天，所以 CVaR 就是 -0.05。
    assert abs(historical_cvar(returns, 0.95) - 0.05) < 1e-9
    assert historical_cvar(returns, 0.95) >= historical_var(returns, 0.95)


def test_var_requires_enough_samples():
    assert historical_var([0.01] * 5) is None
    assert historical_cvar([0.01] * 5) is None


def test_worst_window_finds_lowest_stretch():
    rows = [
        ("2024-01-02", 0.01),
        ("2024-01-03", -0.02),
        ("2024-01-04", 0.03),
        ("2024-01-05", -0.04),
        ("2024-01-08", 0.01),
    ]
    worst = worst_window(rows, 2)
    assert worst is not None
    assert abs(worst["ret"] - (-0.03)) < 1e-12
    assert worst["start"] == "2024-01-05"


def test_cluster_symbols_groups_high_positive_correlation():
    correlation = {
        "a": {"a": 1.0, "b": 0.8, "c": 0.1},
        "b": {"a": 0.8, "b": 1.0, "c": 0.2},
        "c": {"a": 0.1, "b": 0.2, "c": 1.0},
    }
    assert cluster_symbols(correlation) == [["a", "b"], ["c"]]


def test_risk_summary_marks_short_samples():
    summary = risk_summary([100.0, 101.0, 102.0], ["2024-01-02", "2024-01-03", "2024-01-04"])
    assert summary["var"]["95"] is None
    assert any("不足" in note for note in summary["notes"])


def _leg(symbol: str, values: list[float], days: list[str]) -> SimpleNamespace:
    result = SimpleNamespace(equity=values, equity_times=days, trades=[], params_snapshot={})
    return SimpleNamespace(symbol=symbol, result=result, weight=0.5)


def test_combine_outputs_risk_and_margin_stress():
    pattern = [0.01, -0.008, 0.015, -0.003, 0.006, -0.012, 0.004, 0.009, -0.005, 0.011]
    rets = (pattern * 3)[:24]

    def _curve(scale: float) -> list[float]:
        values = [100.0]
        for item in rets:
            values.append(values[-1] * (1.0 + item * scale))
        return values

    days = [(date(2024, 1, 2) + timedelta(days=index)).strftime("%Y-%m-%d") for index in range(25)]
    out = combine_results(
        [_leg("a", _curve(1.0), days), _leg("b", _curve(1.2), days)],
        initial_capital=83_000.0,
        weights={"a": 0.5, "b": 0.5},
        max_margin_usage=50.0,
    )
    assert out["risk"]["var"]["95"] is not None
    assert out["risk"]["clusters"] == [["a", "b"]]
    text = "\n".join(format_portfolio_lines(out))
    assert "风险" in text and "VaR95" in text
    assert "聚类" in text


def test_margin_stress_compares_against_usage_limit():
    days = ["2024-01-02", "2024-01-03", "2024-01-04"]
    trade = SimpleNamespace(
        entry_time="2024-01-02 09:00",
        exit_time="2024-01-03 14:00",
        volume=1,
        entry_price=1000.0,
        multiplier=10,
    )
    result = SimpleNamespace(equity=[100.0, 100.0, 100.0], equity_times=days, trades=[trade], params_snapshot={})
    out = combine_results(
        [SimpleNamespace(symbol="a", result=result, weight=1.0)],
        initial_capital=83_000.0,
        weights={"a": 1.0},
        margin_rates={"a": 0.1},
        max_margin_usage=50.0,
    )
    assert out["margin_stress_pct"] > 0
    assert out["margin_stress_ok"] is True
    assert "压力" in "\n".join(format_portfolio_lines(out))


def test_format_risk_lines_without_summary_is_empty():
    assert format_risk_lines(None) == []
