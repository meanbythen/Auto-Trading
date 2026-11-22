"""凯利仓位：手填胜率/盈亏比后算手数。"""

from src.risk.kelly import (
    full_kelly_f,
    plan_from_config,
    plan_from_stats,
    read_manual_factors,
    summarize_closed_rows,
)
from src.risk.sizing import PositionSizer


def test_full_kelly_even_money_fair_game_is_zero() -> None:
    """胜率 50%、盈亏比 1：没有优势，仓位是 0。"""
    assert full_kelly_f(0.5, 1.0) == 0.0


def test_full_kelly_classic_example() -> None:
    """胜率 60%、盈亏比 1：全凯利是 20%。"""
    assert abs(full_kelly_f(0.6, 1.0) - 0.2) < 1e-9


def test_half_kelly_is_capped_by_max_f() -> None:
    """一半凯利 10%，再被每笔最多 2% 封顶。"""
    plan = plan_from_stats(win_rate=0.6, payoff=1.0, fraction=0.5, max_f_pct=2.0)
    assert plan.ok
    assert abs(plan.f_full - 0.2) < 1e-9
    assert abs(plan.risk_pct - 2.0) < 1e-9


def test_no_edge_refuses() -> None:
    plan = plan_from_stats(win_rate=0.4, payoff=1.0)
    assert not plan.ok
    assert "正期望" in plan.reason


def _live_cfg(kelly: dict) -> dict:
    return {
        "trade": {"volume": 3},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "15",
            "symbols": ["bu2701"],
            "daily_body": {"eth_minutes": 15},
        },
        "risk": {
            "sizing_mode": "fixed",
            "capital": 100_000,
            "risk_per_trade_pct": 0.5,
            "min_volume": 1,
            "margin_per_lot": 0,
            "kelly": kelly,
        },
    }


def test_switch_off_keeps_fixed_lots() -> None:
    cfg = _live_cfg({"enabled": False, "win_rate_pct": 60, "payoff": 1})
    sized = PositionSizer(cfg).calc_open("bu2701", 3000, 2990)
    assert sized.volume == 3


def test_switch_on_without_factors_opens_zero() -> None:
    cfg = _live_cfg({"enabled": True})
    sized = PositionSizer(cfg).calc_open("bu2701", 3000, 2990)
    assert sized.volume == 0
    assert "填写" in sized.reason


def test_win_rate_percent_or_fraction() -> None:
    rate, payoff = read_manual_factors({"win_rate_pct": 60, "payoff": 1.5})
    assert rate == 0.6
    assert payoff == 1.5
    rate2, _ = read_manual_factors({"win_rate": 0.55, "payoff": 1.2})
    assert rate2 == 0.55


def test_legacy_snapshot_still_reads() -> None:
    rate, payoff = read_manual_factors(
        {"snapshot": {"win_rate": 0.55, "avg_win": 150, "avg_loss": 100}}
    )
    assert rate == 0.55
    assert abs((payoff or 0) - 1.5) < 1e-9


def test_valid_kelly_replaces_fixed_lots() -> None:
    cfg = _live_cfg(
        {
            "enabled": True,
            "fraction": 0.25,
            "max_f_pct": 2.0,
            "win_rate_pct": 55,
            "payoff": 1.5,
        }
    )
    plan = plan_from_config(cfg)
    assert plan.ok
    sized = PositionSizer(cfg).calc_open("bu2701", 3000, 2990)
    assert sized.volume >= 1
    assert sized.volume != 3
    assert "凯利" in sized.reason


def test_zero_samples_use_fixed_risk_not_manual_factors() -> None:
    """自动模式的空列表是真实零样本，不能偷用手工胜率放大仓位。"""
    cfg = _live_cfg(
        {
            "enabled": True,
            "auto_from_ledger": True,
            "min_samples": 30,
            "win_rate_pct": 100,
            "payoff": 5,
        }
    )
    plan = plan_from_config(cfg, symbol="bu2701", closed_rows=[])
    assert plan.ok
    assert plan.source == "fixed_fallback"
    assert plan.sample_count == 0
    assert plan.risk_pct == 0.5
    assert "0/30" in plan.reason


def test_dynamic_kelly_is_always_capped_by_fixed_risk() -> None:
    cfg = _live_cfg(
        {
            "enabled": True,
            "auto_from_ledger": True,
            "min_samples": 3,
            "fraction": 1.0,
            "max_f_pct": 20,
        }
    )
    rows = [{"symbol": "bu2701", "net_pnl": 100}] * 3
    plan = plan_from_config(cfg, symbol="bu2701", closed_rows=rows)
    assert plan.ok
    assert plan.source == "leg"
    assert plan.risk_pct == 0.5
    assert "固定风险再封顶0.5%" in plan.reason


def test_sufficient_negative_expectation_refuses_new_open() -> None:
    cfg = _live_cfg({"enabled": True, "auto_from_ledger": True, "min_samples": 3})
    rows = [{"symbol": "bu2701", "net_pnl": -100}] * 3
    plan = plan_from_config(cfg, symbol="bu2701", closed_rows=rows)
    assert not plan.ok
    assert plan.source == "leg"
    assert plan.sample_count == 3
    sized = PositionSizer(cfg).calc_open("bu2701", 3000, 2990, kelly_rows=rows)
    assert sized.volume == 0
    assert "正期望" in sized.reason


def test_symbol_statistics_and_rolling_window_are_isolated() -> None:
    rows = [
        {"symbol": "bu2701", "net_pnl": -100},
        {"symbol": "c2701", "net_pnl": 100},
        {"symbol": "bu2701", "net_pnl": 100},
        {"symbol": "bu2701", "net_pnl": 100},
    ]
    stats = summarize_closed_rows(rows, "bu2701", window=2)
    assert stats == {"sample_count": 2, "win_rate": 1.0, "payoff": 5.0}
    corn = summarize_closed_rows(rows, "c2701", window=2)
    assert corn["sample_count"] == 1


def test_one_low_kelly_leg_does_not_pollute_the_next_leg() -> None:
    """固定风险是配置常量；前一腿较低的凯利结果不能写进下一腿。"""
    cfg = _live_cfg(
        {
            "enabled": True,
            "auto_from_ledger": True,
            "min_samples": 3,
            "fraction": 0.025,
            "max_f_pct": 10,
        }
    )
    cfg["risk"]["risk_per_trade_pct"] = 1.0
    cfg["backtest"] = {"slippage_ticks": 0}
    rows = [
        *([{"symbol": "bu2701", "net_pnl": value} for value in (100, -100, 100)]),
        *([{"symbol": "c2701", "net_pnl": 100}] * 3),
    ]
    sizer = PositionSizer(cfg)
    low = sizer.calc_open("bu2701", 3000, 2990, kelly_rows=rows)
    high = sizer.calc_open("c2701", 3000, 2990, kelly_rows=rows)
    assert abs(low.risk_cash - (100_000 * (1 / 120))) < 0.01
    assert high.risk_cash == 1000
    assert sizer.risk_pct == 1.0
