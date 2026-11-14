"""滑点压力与参数扰动：用假回测，不依赖行情。"""

from __future__ import annotations

from typing import Any

from src.backtest.evaluation import EvaluationReport, EvaluationThresholds
from src.backtest.stress import (
    _engine_box,
    evaluate_with_stress,
    perturb_param_set,
    run_perturbation,
    run_slippage_stress,
)


class _Perf:
    def __init__(self, profit_factor: float, net_pnl: float = 1000.0, trades: int = 120) -> None:
        self.profit_factor = profit_factor
        self.net_pnl = net_pnl
        self.trades = trades


class _Result:
    def __init__(self, profit_factor: float, net_pnl: float = 1000.0, trades: int = 120) -> None:
        self.performance = _Perf(profit_factor, net_pnl, trades)
        self.evaluation = None


def test_slippage_stress_adds_ticks_and_reads_pf() -> None:
    seen: dict[str, Any] = {}

    def runner(cfg: dict[str, Any], **kwargs: Any) -> _Result:
        seen["ticks"] = cfg["backtest"]["slippage_ticks"]
        seen["symbol"] = kwargs["symbol"]
        return _Result(1.35)

    box = run_slippage_stress(
        {"backtest": {"slippage_ticks": 0.0}},
        symbol="bu2612",
        extra_ticks=1.0,
        runner=runner,
    )
    assert box["ok"] is True
    assert box["base_ticks"] == 0.0
    assert box["stress_ticks"] == 1.0
    assert seen["ticks"] == 1.0
    assert box["profit_factor"] == 1.35


def test_perturb_param_set_only_touches_numbers() -> None:
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "daily_body": {"period": 20, "mult": 1.5, "use_filter": True, "mode": "x"},
        }
    }
    points = perturb_param_set(cfg, ["period", "mult", "use_filter"])
    assert points
    assert len(points) <= 4
    assert all(key in ("period", "mult") for key, _value, _patch in points)
    assert any(key == "period" and value == 18 for key, value, _ in points)
    assert any(key == "period" and value == 22 for key, value, _ in points)


def test_perturbation_flags_a_cliff() -> None:
    cfg = {"strategy": {"engine": "daily_body", "daily_body": {"period": 20, "mult": 1.5}}}
    box = run_perturbation(
        cfg,
        symbol="bu2612",
        param_keys=["period"],
        base_pf=1.60,
        thresholds=EvaluationThresholds(),
        runner=lambda _cfg, **_kwargs: _Result(0.90, net_pnl=-200),
    )
    assert box["error"] == ""
    assert box["cliff"] is True
    assert box["worst_pf"] == 0.9


def test_perturbation_survives_small_changes() -> None:
    cfg = {"strategy": {"engine": "daily_body", "daily_body": {"period": 20}}}
    box = run_perturbation(
        cfg,
        symbol="bu2612",
        param_keys=["period"],
        base_pf=1.60,
        thresholds=EvaluationThresholds(),
        runner=lambda _cfg, **_kwargs: _Result(1.50),
    )
    assert box["cliff"] is False
    assert box["ok"] is True
    assert box["points_count"] >= 1


def test_evaluate_with_stress_runs_both_checks() -> None:
    """历史回测和策略优化共用同一个入口：滑点和扰动都要留下，且只开跑一次。"""
    cfg = {
        "backtest": {"slippage_ticks": 1.0},
        "strategy": {"engine": "daily_body", "daily_body": {"period": 20}},
    }
    result = _Result(1.50, net_pnl=2000.0)
    report = EvaluationReport()
    started: list[int] = []

    report, slip, pert, note = evaluate_with_stress(
        cfg,
        result,
        report=report,
        symbol="bu2612",
        thresholds=EvaluationThresholds(),
        runner=lambda _cfg, **_kwargs: _Result(1.45),
        on_start=lambda: started.append(1),
    )

    keys = {check.key for check in report.checks}
    assert {"slippage", "perturbation"} <= keys
    assert slip["stress_ticks"] == 2.0
    assert pert["ok"] is True
    assert note == ""
    assert started == [1]
    assert result.evaluation is report


def test_evaluate_with_stress_skips_small_sample() -> None:
    result = _Result(1.50, trades=10)
    called: list[int] = []

    _report, slip, pert, note = evaluate_with_stress(
        {"strategy": {"engine": "daily_body"}},
        result,
        report=EvaluationReport(),
        symbol="bu2612",
        thresholds=EvaluationThresholds(),
        runner=lambda _cfg, **_kwargs: called.append(1),
    )

    assert slip == {} and pert == {}
    assert "跳过" in note
    assert called == []


def test_evaluate_with_stress_respects_switch() -> None:
    result = _Result(1.50)
    _report, slip, pert, note = evaluate_with_stress(
        {"strategy": {"engine": "daily_body"}},
        result,
        report=EvaluationReport(),
        symbol="bu2612",
        thresholds=EvaluationThresholds(stress_on_backtest=False),
        runner=lambda _cfg, **_kwargs: _Result(1.45),
    )

    assert slip == {} and pert == {}
    assert note == "压力测试 关（配置页已关闭）"


def test_engine_box_finds_params_under_the_engine_alias() -> None:
    """macd_swing 的参数在 ema_swing 节里，不能因为名字不同就跳过压力测试。"""
    cfg = {"strategy": {"engine": "macd_swing", "ema_swing": {"fast_ma": 12, "slow_ma": 26}}}
    assert _engine_box(cfg, "macd_swing") == {"fast_ma": 12, "slow_ma": 26}


def test_engine_box_falls_back_to_the_engine_name() -> None:
    cfg = {"strategy": {"engine": "daily_body", "daily_body": {"eth_minutes": 20}}}
    assert _engine_box(cfg, "daily_body") == {"eth_minutes": 20}
