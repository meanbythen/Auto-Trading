"""组合回测数据层：权重、对齐、汇总、容错。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.backtest.portfolio import (
    PortfolioLeg,
    allocate_weights,
    combine_results,
    format_portfolio_lines,
    simulate_shared_capital,
)
from src.domain.trade import TradeRecord


def _leg(symbol: str, values: list[float], days: list[str], trades: list | None = None) -> PortfolioLeg:
    result = SimpleNamespace(equity=list(values), equity_times=list(days), trades=list(trades or []))
    return PortfolioLeg(symbol=symbol, result=result)


def _trade(symbol: str, entry: str, exit_: str, *, volume: int = 1, price: float = 1000.0, mult: float = 10.0,
           pnl: float = 0.0, atr: float | None = None) -> TradeRecord:
    return TradeRecord(
        symbol=symbol,
        side="long",
        entry_time=entry,
        exit_time=exit_,
        entry_price=price,
        exit_price=price,
        volume=volume,
        pnl=pnl,
        multiplier=mult,
        atr=atr,
    )


def test_correlation_pairs_are_graded():
    days = [f"2024-01-{day:02d}" for day in range(2, 8)]
    rise = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    flat = [100.0] * 6
    out = combine_results(
        [_leg("a", rise, days), _leg("b", flat, days)],
        initial_capital=10_000.0,
        weights={"a": 0.5, "b": 0.5},
    )
    assert out["correlation_pairs"][0]["level"] == "低"
    assert "（低）" in "\n".join(format_portfolio_lines(out))


def test_high_correlation_pair_is_flagged_in_notes():
    days = [f"2024-01-{day:02d}" for day in range(2, 8)]
    rise = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    out = combine_results(
        [_leg("a", rise, days), _leg("b", rise, days)],
        initial_capital=10_000.0,
        weights={"a": 0.5, "b": 0.5},
    )
    pair = out["correlation_pairs"][0]
    assert pair["level"] == "高"
    assert any("相关性偏高" in item for item in out["notes"])
    assert "（高）" in "\n".join(format_portfolio_lines(out))


def test_negative_correlation_counted_as_diversified():
    days = [f"2024-01-{day:02d}" for day in range(2, 7)]
    # 日收益严格反向：+1% / -1% 交替，避免两条单边曲线因“幅度递减”出现伪正相关。
    up = [100.0, 101.0, 99.99, 100.9899, 99.980001]
    down = [100.0, 99.0, 99.99, 98.9901, 99.980001]
    out = combine_results(
        [_leg("a", up, days), _leg("b", down, days)],
        initial_capital=10_000.0,
        weights={"a": 0.5, "b": 0.5},
    )
    assert out["correlation_pairs"][0]["level"] == "反向"


def test_equal_weights_sum_to_one():
    legs = [_leg("a", [100.0, 101.0], ["2024-01-01 09:00", "2024-01-02 09:00"]),
            _leg("b", [100.0, 99.0], ["2024-01-01 09:00", "2024-01-02 09:00"])]
    out = allocate_weights(legs, "equal")
    assert out["mode"] == "equal"
    assert sum(out["weights"].values()) == pytest.approx(1.0)
    assert out["weights"]["a"] == pytest.approx(0.5)


def test_inverse_vol_favors_steady_leg():
    days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    steady = _leg("steady", [100.0, 100.1, 100.2, 100.3, 100.4], days + ["2024-01-06"])
    wild = _leg("wild", [100.0, 110.0, 90.0, 120.0, 80.0], days + ["2024-01-06"])
    out = allocate_weights([steady, wild], "inverse_vol")
    assert out["mode"] == "inverse_vol"
    assert sum(out["weights"].values()) == pytest.approx(1.0)
    assert out["weights"]["steady"] > out["weights"]["wild"]


def test_inverse_vol_falls_back_when_sample_missing():
    days = ["2024-01-01", "2024-01-02"]
    out = allocate_weights([_leg("a", [100.0, 101.0], days), _leg("b", [100.0, 99.0], days)], "inverse_vol")
    assert out["mode"] == "equal"
    assert "回退等权" in out["note"]
    assert sum(out["weights"].values()) == pytest.approx(1.0)


def test_risk_budget_uses_given_shares():
    days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    a = _leg("a", [100.0, 100.1, 100.2, 100.3, 100.4], days)
    b = _leg("b", [100.0, 100.2, 100.4, 100.6, 100.8], days)
    out = allocate_weights([a, b], "risk_budget", risk_budget={"a": 0.75, "b": 0.25})
    assert out["mode"] == "risk_budget"
    assert sum(out["weights"].values()) == pytest.approx(1.0)
    # a 波动更小、风险份额更大，权重必然更高。
    assert out["weights"]["a"] > out["weights"]["b"]


def test_fixed_caps_leave_cash_and_respect_limit():
    days = ["2024-01-01", "2024-01-02", "2024-01-03"]
    legs = [_leg("a", [100.0, 100.0, 100.0], days), _leg("b", [100.0, 100.0, 100.0], days)]
    out = allocate_weights(legs, "fixed", caps={"a": 0.2, "b": 0.2})
    assert out["weights"]["a"] == pytest.approx(0.2)
    assert out["weights"]["b"] == pytest.approx(0.2)
    assert sum(out["weights"].values()) < 1.0
    assert "现金" in out["note"]


def test_combine_equity_sums_weighted_legs_and_is_deterministic():
    days = ["2024-01-02 09:00", "2024-01-03 09:00", "2024-01-04 09:00"]
    a = _leg("a", [100_000.0, 101_000.0, 102_000.0], days)
    b = _leg("b", [100_000.0, 99_000.0, 98_000.0], days)
    first = combine_results([a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5})
    second = combine_results([a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5})
    assert first["equity"] == second["equity"]
    assert first["equity"][0] == pytest.approx(100_000.0)
    assert first["equity"][1] == pytest.approx(100_000.0)
    assert first["equity"][2] == pytest.approx(100_000.0)
    assert first["equity_times"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert first["performance"].trades == 0


def test_combine_forward_fills_missing_days():
    a = _leg("a", [100_000.0, 110_000.0], ["2024-01-02", "2024-01-04"])
    b = _leg("b", [100_000.0, 100_000.0, 100_000.0], ["2024-01-02", "2024-01-03", "2024-01-04"])
    out = combine_results([a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5})
    assert out["equity_times"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    # 01-03 这条腿还没有数据，按起始权益计入。
    assert out["equity"][1] == pytest.approx(100_000.0)
    assert out["equity"][2] == pytest.approx(105_000.0)


def test_margin_peak_sums_concurrent_legs_and_counts_positions():
    days = ["2024-01-02", "2024-01-03", "2024-01-04"]
    a = _leg("a", [100.0, 100.0, 100.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-04 15:00", pnl=10.0)])
    b = _leg("b", [100.0, 100.0, 100.0], days, [_trade("b", "2024-01-03 09:00", "2024-01-03 15:00", pnl=5.0)])
    out = combine_results(
        [a, b],
        initial_capital=100_000.0,
        weights={"a": 0.5, "b": 0.5},
        margin_rates={"a": 0.10, "b": 0.20},
    )
    assert out["margin_source"] == "配置保证金比例×开仓价×乘数"
    # 01-03 两腿同时持仓：500 + 1000。
    assert out["margin_peak"] == pytest.approx(1_500.0)
    assert out["max_concurrent"] == 2
    assert out["avg_concurrent"] == pytest.approx(4.0 / 3.0)
    assert out["margin_peak_pct"] == pytest.approx(0.015)


def test_margin_falls_back_to_estimated_rate():
    days = ["2024-01-02"]
    a = _leg("a", [100.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00")])
    out = combine_results([a], initial_capital=100_000.0, weights={"a": 1.0})
    assert "估算" in out["margin_source"]
    # 1000 × 10 × 1 × 10%
    assert out["margin_peak"] == pytest.approx(1_000.0)


def test_correlation_matrix_shape_and_values():
    days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    up = _leg("up", [100.0, 101.0, 100.5, 102.0, 101.5], days)
    down = _leg("down", [100.0, 99.0, 99.5, 98.0, 98.5], days)
    flat = _leg("flat", [100.0, 100.0, 100.0, 100.0, 100.0], days)
    out = combine_results([up, down, flat], initial_capital=100_000.0, weights={"up": 0.4, "down": 0.3, "flat": 0.3})
    corr = out["correlation"]
    assert set(corr) == {"up", "down", "flat"}
    assert corr["up"]["up"] == pytest.approx(1.0)
    assert corr["up"]["down"] == pytest.approx(-1.0, abs=0.01)
    assert corr["flat"]["up"] == pytest.approx(0.0)
    assert corr["flat"]["flat"] == pytest.approx(1.0)


def test_leg_without_times_is_skipped_with_reason():
    bad = PortfolioLeg(symbol="bad", result=SimpleNamespace(equity=[100.0], equity_times=[], trades=[]))
    out = combine_results([bad], initial_capital=100_000.0)
    assert out["symbols"] == []
    assert out["skipped"][0]["symbol"] == "bad"
    assert "缺时间戳" in format_portfolio_lines(out)[0]


def test_portfolio_performance_pnl_is_scaled_by_weight():
    days = ["2024-01-02", "2024-01-03"]
    a = _leg("a", [100.0, 110.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-03 15:00", pnl=10_000.0)])
    b = _leg("b", [100.0, 110.0], days, [_trade("b", "2024-01-02 09:00", "2024-01-03 15:00", pnl=10_000.0)])
    out = combine_results([a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5})
    # 组合权益只涨 10_000，绩效净盈亏也必须按权重缩放到同一量级。
    assert out["equity"][-1] - out["equity"][0] == pytest.approx(10_000.0)
    assert out["performance"].net_pnl == pytest.approx(10_000.0)
    assert out["contributions"]["a"]["net_pnl"] == pytest.approx(5_000.0)
    assert out["contributions"]["b"]["net_pnl"] == pytest.approx(5_000.0)


def test_zero_weight_leg_trades_excluded_from_performance():
    days = ["2024-01-02", "2024-01-03"]
    a = _leg("a", [100.0, 101.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-03 15:00", pnl=100.0)])
    b = _leg("b", [100.0, 90.0], days, [_trade("b", "2024-01-02 09:00", "2024-01-03 15:00", pnl=-500.0)])
    out = combine_results([a, b], initial_capital=100_000.0, weights={"a": 1.0, "b": 0.0})
    assert out["performance"].trades == 1
    assert out["performance"].net_pnl == pytest.approx(100.0)


def test_fixed_mode_without_caps_notes_equal_fallback():
    days = ["2024-01-01", "2024-01-02", "2024-01-03"]
    legs = [_leg("a", [100.0, 100.0, 100.0], days), _leg("b", [100.0, 100.0, 100.0], days)]
    out = allocate_weights(legs, "fixed")
    assert out["weights"] == {"a": 0.5, "b": 0.5}
    assert "按等权处理" in out["note"]


def test_margin_ok_compares_percent_with_percent():
    days = ["2024-01-02"]
    a = _leg("a", [100.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00")])
    tight = combine_results(
        [a], initial_capital=100_000.0, weights={"a": 1.0},
        margin_rates={"a": 3.0}, max_margin_usage=80.0,
    )
    loose_usage = combine_results(
        [a], initial_capital=100_000.0, weights={"a": 1.0},
        margin_rates={"a": 3.0}, max_margin_usage=10.0,
    )
    assert tight["margin_ok"] is True
    assert loose_usage["margin_ok"] is False


def test_margin_source_names_estimated_symbols():
    days = ["2024-01-02"]
    a = _leg("a", [100.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00")])
    b = _leg("b", [100.0], days, [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00")])
    out = combine_results(
        [a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5}, margin_rates={"a": 0.10}
    )
    assert "配置保证金比例" in out["margin_source"]
    assert "b" in out["margin_source"]
    assert any("前向填充" in note for note in out["notes"])


def test_margin_rate_used_for_peak():
    days = ["2024-01-02"]
    # 1000 元 × 10 倍 × 20% = 2000；若错用 10% 兜底会算成 1000。
    a = _leg("a", [100.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", price=1000.0, mult=10.0)])
    out = combine_results(
        [a], initial_capital=100_000.0, weights={"a": 1.0}, margin_rates={"a": 0.20}
    )
    assert out["margin_source"] == "配置保证金比例×开仓价×乘数"
    assert out["margin_peak"] == pytest.approx(2_000.0)


def test_format_lines_cover_key_sections():
    days = ["2024-01-02", "2024-01-03"]
    a = _leg("a", [100.0, 101.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-03 15:00", pnl=1000.0)])
    b = _leg("b", [100.0, 100.5], days, [_trade("b", "2024-01-02 09:00", "2024-01-03 15:00", pnl=500.0)])
    out = combine_results([a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5})
    text = "\n".join(format_portfolio_lines(out))
    for keyword in ("组合回测", "组合权益", "保证金", "品种贡献", "相关性", "权重"):
        assert keyword in text
    assert "通过" not in text and "不通过" not in text


def test_shared_capital_rejects_low_efficiency_trade_when_budget_tight():
    """预算只够一笔时，资金效率高的先占，另一笔记拒开。"""
    days = ["2024-01-02"]
    good = _leg("a", [100.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", pnl=100.0)])
    poor = _leg("b", [100.0], days, [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0)])
    # 每笔保证金 = 1000 × 10 × 1 × 10% = 1000；预算 1500 只够一笔。
    out = simulate_shared_capital([good, poor], initial_capital=1_500.0)
    assert out["trades_accepted"] == 1
    assert out["trades_rejected"] == 1
    assert out["accepted_pnl"] == pytest.approx(100.0)
    assert out["rejected"][0]["symbol"] == "b"
    assert out["rejected"][0]["reason"] == "保证金预算不足"
    assert out["final_equity"] == pytest.approx(1_600.0)


def test_shared_capital_accepts_all_when_budget_ample():
    """预算充足时全部接受，约束后的盈亏与无约束口径一致。"""
    days = ["2024-01-02"]
    a = _leg("a", [100.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", pnl=100.0)])
    b = _leg("b", [100.0], days, [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0)])
    out = simulate_shared_capital([a, b], initial_capital=100_000.0)
    assert out["trades_rejected"] == 0
    assert out["accepted_pnl"] == pytest.approx(110.0)
    assert out["accepted_pnl"] == pytest.approx(out["unconstrained_pnl"])
    assert out["pnl_gap"] == pytest.approx(0.0)
    assert out["final_equity"] == pytest.approx(out["unconstrained_equity"])


def test_shared_capital_enforces_portfolio_heat():
    """组合风险热度超限时拒开，并输出热度峰值。"""
    days = ["2024-01-02"]
    a = _leg(
        "a", [100.0], days,
        [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", pnl=100.0, atr=10.0)],
    )
    b = _leg(
        "b", [100.0], days,
        [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=10.0)],
    )
    # 每笔预计风险 = ATR 10 × 乘数 10 × 1 手 = 100；热度上限 1% × 10000 = 100，只够一笔。
    out = simulate_shared_capital([a, b], initial_capital=10_000.0, max_heat_pct=1.0)

    assert out["heat_budget"] == pytest.approx(100.0)
    assert out["heat_peak"] == pytest.approx(100.0)
    assert out["trades_accepted"] == 1
    assert out["trades_rejected"] == 1
    assert out["rejected"][0]["reason"] == "组合风险热度超限"


def test_shared_capital_heat_is_disabled_by_default():
    days = ["2024-01-02"]
    a = _leg(
        "a", [100.0], days,
        [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", pnl=100.0, atr=10.0)],
    )
    b = _leg(
        "b", [100.0], days,
        [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=10.0)],
    )
    out = simulate_shared_capital([a, b], initial_capital=10_000.0)

    assert out["heat_budget"] == 0.0
    assert out["trades_rejected"] == 0


def test_shared_capital_daily_loss_limit_blocks_new_opens():
    """当天已实现亏损达到上限后，当天后续开仓一律拒绝。"""
    days = ["2024-01-01", "2024-01-02"]
    loser = _leg("a", [100.0, 100.0], days, [_trade("a", "2024-01-01 09:00", "2024-01-02 15:00", pnl=-600.0)])
    fresh = _leg("b", [100.0, 100.0], days, [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=100.0)])
    out = simulate_shared_capital([loser, fresh], initial_capital=100_000.0, daily_loss_limit=500.0)
    assert out["blocked_days"] == ["2024-01-02"]
    assert out["trades_accepted"] == 1
    assert out["trades_rejected"] == 1
    assert out["rejected"][0]["reason"] == "日亏熔断"


def test_combine_results_attaches_shared_capital_without_touching_equity():
    """共享资金账只作为附加诊断，不改原组合权益和绩效。"""
    days = ["2024-01-02", "2024-01-03"]
    a = _leg("a", [100.0, 110.0], days, [_trade("a", "2024-01-02 09:00", "2024-01-03 15:00", pnl=10_000.0)])
    b = _leg("b", [100.0, 90.0], days, [_trade("b", "2024-01-02 09:00", "2024-01-03 15:00", pnl=-10_000.0)])
    base = combine_results([a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5})
    limited = combine_results(
        [a, b], initial_capital=100_000.0, weights={"a": 0.5, "b": 0.5}, daily_loss_limit=5_000.0
    )
    shared = limited["shared_capital"]
    assert shared["trades_total"] == 2
    assert shared["margin_budget"] == pytest.approx(100_000.0)
    assert limited["equity"] == base["equity"]
    assert limited["performance"].net_pnl == base["performance"].net_pnl


def test_shared_capital_priority_risk_usage_beats_efficiency():
    """预算只够一笔时，先比风险预算占用率，再比资金效率。"""
    days = ["2024-01-02"]
    # a 预计亏损更大（ATR 10），资金效率更低；按 4.3 顺序仍应排在 b 前面。
    high_risk = _leg(
        "a", [100.0], days,
        [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=10.0)],
    )
    efficient = _leg(
        "b", [100.0], days,
        [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=100.0, atr=1.0)],
    )
    out = simulate_shared_capital([high_risk, efficient], initial_capital=1_500.0)
    assert out["trades_accepted"] == 1
    assert out["rejected"][0]["symbol"] == "b"
    assert out["accepted_pnl"] == pytest.approx(10.0)
    assert out["rejected"][0]["risk_usage"] == pytest.approx(10.0 / 15.0, abs=1e-4)
    assert "风险预算占用率" in out["priority"]


def test_shared_capital_priority_penalizes_correlated_symbol():
    """同日抢资金时，与已持仓高度相关的品种排后，优先做分散的那条。"""
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-06"]
    rise = [100.0, 101.0, 102.0, 103.0, 104.0]
    # 用横盘代表不相关：单边下跌的日收益幅度也在收窄，会和单边上涨算出伪高相关。
    flat = [100.0] * 5
    a = _leg("a", rise, days, [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=1.0)])
    b = _leg("b", rise, days, [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=1.0)])
    c = _leg("c", flat, days, [_trade("c", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=1.0)])
    # 预算 2000 只够两笔：a、b 日收益完全相关，不能两条都占。
    out = simulate_shared_capital([a, b, c], initial_capital=2_000.0)
    assert out["trades_accepted"] == 2
    accepted = {symbol for symbol, bucket in out["by_symbol"].items() if bucket["accepted"]}
    assert accepted != {"a", "b"}
    assert "c" in accepted


def test_shared_capital_priority_slippage_and_stable_rank():
    """其余条件相同时，预计滑点占允许滑点比例大的先做；重复跑结果一致。"""
    days = ["2024-01-02"]
    wide = _leg("a", [100.0], days,
                [_trade("a", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=1.0)])
    tight = _leg("b", [100.0], days,
                 [_trade("b", "2024-01-02 09:00", "2024-01-02 15:00", pnl=10.0, atr=1.0)])
    kwargs = {
        "initial_capital": 1_500.0,
        "slippage_by_symbol": {"a": 2.0, "b": 0.0},
        "allowed_slip_ticks": 2.0,
    }
    first = simulate_shared_capital([wide, tight], **kwargs)
    second = simulate_shared_capital([wide, tight], **kwargs)
    assert first["trades_accepted"] == 1
    assert first["rejected"][0]["symbol"] == "b"
    assert first["rejected"][0]["slip_ratio"] == pytest.approx(0.0)
    assert first["priority"] == second["priority"]
    assert first["by_symbol"] == second["by_symbol"]
    assert "稳定哈希" in first["priority"]


