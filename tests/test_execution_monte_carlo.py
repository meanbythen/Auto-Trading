"""成交层蒙特卡洛：执行噪声重跑只做对照，不参与写入闸。"""

from __future__ import annotations

from types import SimpleNamespace

from src.backtest.monte_carlo import (
    format_execution_monte_carlo_lines,
    run_execution_monte_carlo,
)


def _trades(pnls: list[float]) -> list[SimpleNamespace]:
    return [SimpleNamespace(pnl=value, volume=1) for value in pnls]


def test_execution_mc_is_deterministic_and_costs_money() -> None:
    trades = _trades([100, 100, -50, 80, 60, -40, 90, 70, 50, -20])
    first = run_execution_monte_carlo(
        trades,
        capital=100_000,
        tick_size=1.0,
        multiplier=10.0,
        n_runs=200,
        seed=7,
        slip_ticks=0.5,
        jitter_ticks=0.0,
        delay_ticks=0.0,
    )
    second = run_execution_monte_carlo(
        trades,
        capital=100_000,
        tick_size=1.0,
        multiplier=10.0,
        n_runs=200,
        seed=7,
        slip_ticks=0.5,
        jitter_ticks=0.0,
        delay_ticks=0.0,
    )
    assert first == second
    assert first["base_net"] == 440.0
    # 每边多付 0.5 跳，10 笔 × 2 边 × 0.5 × 1 × 10 = 100 元。
    assert first["p50_net"] == 340.0
    assert first["positive_rate"] == 1.0
    assert first["assumptions"]["slip_ticks"] == 0.5


def test_execution_mc_skips_small_sample() -> None:
    report = run_execution_monte_carlo(_trades([10.0] * 5), capital=100_000)
    assert report["skipped"] is True
    assert "5 笔" in report["skip_reason"]


def test_execution_mc_partial_fill_scales_pnl() -> None:
    trades = _trades([100.0] * 10)
    report = run_execution_monte_carlo(
        trades,
        capital=100_000,
        n_runs=500,
        seed=7,
        slip_ticks=0.0,
        jitter_ticks=0.0,
        delay_ticks=0.0,
        partial_fill_prob=1.0,
    )
    # 全部部分成交，成交比例落在 50%～100%，净盈亏一定低于满额 1000。
    assert 500.0 <= report["p50_net"] <= 1000.0
    assert report["positive_rate"] == 1.0


def test_format_execution_monte_carlo_lines_mentions_assumptions() -> None:
    report = run_execution_monte_carlo(
        _trades([50.0] * 10),
        capital=100_000,
        n_runs=100,
        seed=1,
        slip_ticks=0.5,
        jitter_ticks=1.0,
        delay_ticks=0.5,
    )
    text = "\n".join(format_execution_monte_carlo_lines(report))
    assert "执行噪声重跑" in text
    assert "0.5±1" in text
    assert "不参与写入闸" in text
