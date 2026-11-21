"""组合回测入口：逐腿跑、异常跳过、资金口径。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.backtest.portfolio import format_portfolio_lines, run_portfolio_backtest


def _result(days: list[str], values: list[float]) -> SimpleNamespace:
    return SimpleNamespace(equity=list(values), equity_times=list(days), trades=[], params_snapshot={})


def test_runner_receives_full_capital_per_leg_and_weights_are_applied():
    days = ["2024-01-02", "2024-01-03"]
    seen: dict[str, float] = {}

    def runner(cfg, *, symbol, initial_capital, **kwargs):
        seen[symbol] = initial_capital
        return _result(days, [initial_capital, initial_capital * (1.1 if symbol == "a" else 1.0)])

    out = run_portfolio_backtest(
        {"risk": {"capital": 50_000.0}},
        ["a", "b"],
        mode="equal",
        runner=runner,
    )
    assert seen == {"a": 50_000.0, "b": 50_000.0}
    assert out["weights"] == {"a": 0.5, "b": 0.5}
    assert out["equity"][0] == pytest.approx(50_000.0)
    # a 涨 10%，权重一半 → 组合期末 +5%。
    assert out["equity"][-1] == pytest.approx(52_500.0)


def test_failed_leg_is_skipped_and_reported():
    days = ["2024-01-02", "2024-01-03"]

    def runner(cfg, *, symbol, initial_capital, **kwargs):
        if symbol == "bad":
            raise RuntimeError("拉不到 K 线")
        return _result(days, [initial_capital, initial_capital])

    out = run_portfolio_backtest({"risk": {"capital": 10_000.0}}, ["ok", "bad"], runner=runner)
    assert out["symbols"] == ["ok"]
    assert out["weights"] == {"ok": 1.0}
    assert out["skipped"][0]["symbol"] == "bad"
    assert "拉不到 K 线" in out["skipped"][0]["reason"]
    assert "已跳过" in "\n".join(format_portfolio_lines(out))


def test_all_legs_failed_returns_empty_with_message():
    def runner(cfg, **kwargs):
        raise RuntimeError("没数据")

    out = run_portfolio_backtest({"risk": {"capital": 10_000.0}}, ["a", "b"], runner=runner)
    assert out["symbols"] == []
    assert len(out["skipped"]) == 2
    assert "没有可用腿" in format_portfolio_lines(out)[0]


def test_margin_by_lot_read_from_config():
    days = ["2024-01-02"]
    trade = SimpleNamespace(
        symbol="fu2701",
        entry_time="2024-01-02 09:00",
        exit_time="2024-01-02 15:00",
        entry_price=3000.0,
        volume=1,
        multiplier=10.0,
        pnl=0.0,
    )

    def runner(cfg, *, symbol, initial_capital, **kwargs):
        return SimpleNamespace(
            equity=[initial_capital],
            equity_times=days,
            trades=[trade],
            params_snapshot={},
        )

    cfg = {"risk": {"capital": 100_000.0, "contracts": {"fu": {"margin": 8830.0}}}}
    out = run_portfolio_backtest(cfg, ["fu2701"], runner=runner)
    assert out["margin_source"] == "配置每手保证金"
    assert out["margin_peak"] == pytest.approx(8_830.0)


def test_margin_rate_from_config_used_when_lot_margin_zero():
    days = ["2024-01-02"]
    trade = SimpleNamespace(
        symbol="jd2612",
        entry_time="2024-01-02 09:00",
        exit_time="2024-01-02 15:00",
        entry_price=3600.0,
        volume=1,
        multiplier=10.0,
        pnl=0.0,
    )

    def runner(cfg, *, symbol, initial_capital, **kwargs):
        return SimpleNamespace(equity=[initial_capital], equity_times=days, trades=[trade], params_snapshot={})

    cfg = {"risk": {"capital": 100_000.0, "contracts": {"jd": {"margin": 0.0, "margin_rate_pct": 7.0}}}}
    out = run_portfolio_backtest(cfg, ["jd2612"], runner=runner)
    assert out["margin_source"] == "配置保证金比例×开仓价×乘数"
    # 3600 × 10 × 7%
    assert out["margin_peak"] == pytest.approx(2_520.0)


def test_cancel_marks_remaining_legs_skipped():
    days = ["2024-01-02"]

    def runner(cfg, *, symbol, initial_capital, **kwargs):
        return _result(days, [initial_capital])

    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2

    out = run_portfolio_backtest({"risk": {"capital": 10_000.0}}, ["a", "b", "c"], runner=runner, cancel=cancel)
    assert out["symbols"] == ["a", "b"]
    assert out["skipped"][0]["reason"] == "已取消"
