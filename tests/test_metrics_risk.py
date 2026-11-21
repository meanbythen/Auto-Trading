"""批次1可信度底座：Performance 新增风险指标的手算与降级测试。"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from src.backtest.metrics import calc_performance


def _trade(pnl: float, commission: float, entry: str, exit: str) -> SimpleNamespace:
    return SimpleNamespace(pnl=pnl, commission=commission, entry_time=entry, exit_time=exit)


def test_new_risk_metrics_match_hand_calculation():
    # 4 笔成交，pnl = [100, -50, 200, -100]，commission 各 5；时间跨度 4 天。
    trades = [
        _trade(100.0, 5.0, "2020-01-01 00:00:00", "2020-01-02 00:00:00"),
        _trade(-50.0, 5.0, "2020-01-02 00:00:00", "2020-01-03 00:00:00"),
        _trade(200.0, 5.0, "2020-01-03 00:00:00", "2020-01-04 00:00:00"),
        _trade(-100.0, 5.0, "2020-01-04 00:00:00", "2020-01-05 00:00:00"),
    ]
    equity = [10000.0, 10100.0, 10050.0, 10250.0, 10150.0]
    times = ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04", "2020-01-05"]

    perf = calc_performance(trades, equity, min_trades=2, times=times)
    d = perf.to_dict()

    # 新增键齐全，且类型为有限数 / bool。
    for key in ("sharpe", "sortino", "ulcer", "exposure", "turnover", "cost_ratio"):
        assert key in d, key
        assert math.isfinite(float(d[key])), key

    pnls = [100.0, -50.0, 200.0, -100.0]
    mean = sum(pnls) / 4
    var = sum((x - mean) ** 2 for x in pnls) / 4
    std = math.sqrt(var)
    years = 4.0 / 365.0
    annual_factor = math.sqrt(4.0 / years)  # == sqrt(365)
    assert annual_factor == pytest.approx(math.sqrt(365))

    # Sharpe（年化）
    assert perf.sharpe == pytest.approx((mean / std) * annual_factor, rel=1e-6)
    assert perf.sharpe_annualized is True

    # Sortino：下行偏差 = sqrt(mean(min(pnl,0)^2))
    downside = math.sqrt(sum(min(x, 0.0) ** 2 for x in pnls) / 4)
    assert perf.sortino == pytest.approx((mean / downside) * annual_factor, rel=1e-6)

    # Ulcer：权益回撤百分比平方均值开方
    peak = equity[0]
    dd_sq = 0.0
    for e in equity:
        peak = max(peak, e)
        dd_pct = (peak - e) / peak
        dd_sq += dd_pct * dd_pct
    expected_ulcer = math.sqrt(dd_sq / len(equity))
    assert perf.ulcer == pytest.approx(expected_ulcer, rel=1e-6)

    # 成本占比 = 总手续费 ÷ 毛盈利
    assert perf.cost_ratio == pytest.approx(20.0 / 300.0, rel=1e-9)
    # 换手率 = 年成交笔数 = 4 / years = 365
    assert perf.turnover == pytest.approx(365.0, rel=1e-6)
    # 持仓暴露：4 天持仓 / 4 天跨度 = 1
    assert perf.exposure == pytest.approx(1.0, rel=1e-9)


def test_all_winning_trades_sortino_is_zero():
    trades = [
        _trade(100.0, 5.0, "2020-01-01 00:00:00", "2020-01-02 00:00:00"),
        _trade(50.0, 5.0, "2020-01-02 00:00:00", "2020-01-03 00:00:00"),
    ]
    perf = calc_performance(trades, [10000.0, 10100.0, 10150.0], min_trades=2,
                            times=["2020-01-01", "2020-01-02", "2020-01-03"])
    # 无下行波动 → sortino 记 0（展示处会写「算不出」），不抛异常。
    assert perf.sortino == 0.0
    assert perf.cost_ratio == pytest.approx(10.0 / 150.0, rel=1e-9)


def test_no_time_axis_degrades_gracefully():
    trades = [
        _trade(100.0, 5.0, "2020-01-01 00:00:00", "2020-01-02 00:00:00"),
        _trade(-40.0, 5.0, "2020-01-02 00:00:00", "2020-01-03 00:00:00"),
    ]
    # times=None：无法年化、无法算年换手；不得抛异常。
    perf = calc_performance(trades, [10000.0, 10100.0, 10060.0], min_trades=2)
    assert perf.sharpe_annualized is False
    assert perf.turnover == 0.0
    assert math.isfinite(perf.sharpe)
    assert math.isfinite(perf.sortino)


def test_single_trade_does_not_crash():
    trades = [_trade(10.0, 1.0, "2020-01-01 00:00:00", "2020-01-01 01:00:00")]
    perf = calc_performance(trades, [10000.0, 10010.0], min_trades=1,
                            times=["2020-01-01 00:00:00", "2020-01-01 01:00:00"])
    # 标准差为 0 → sharpe 0；不抛异常。
    assert perf.sharpe == 0.0
    assert perf.ulcer == 0.0
