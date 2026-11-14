"""凯利仓位：手填胜率/盈亏比后算手数。"""

from src.risk.kelly import (
    full_kelly_f,
    plan_from_config,
    plan_from_stats,
    read_manual_factors,
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
