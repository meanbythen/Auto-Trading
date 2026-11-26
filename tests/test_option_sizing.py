"""期权算仓：买方按权利金，卖方按保证金；缺数据一律拒开。"""

from src.risk.sizing import PositionSizer


def _cfg(box: dict, *, capital: float = 100_000, budget_pct: float = 20.0) -> dict:
    return {
        "trade": {"volume": 100},
        "risk": {
            "sizing_mode": "fixed",
            "capital": capital,
            "risk_per_trade_pct": 50,
            "min_volume": 1,
            "margin_usage_pct": 100,
            "option_premium_budget_pct": budget_pct,
            "contracts": {"rb2601C3500": box},
        },
    }


def _box(**extra) -> dict:
    base = {
        "multiplier": 10,
        "margin_rate_pct": 12,
        "price_tick": 0.5,
        "strike": 3500,
        "expiry": "2027-01-15",
    }
    base.update(extra)
    return base


def test_buyer_margin_is_premium() -> None:
    """买方一手占用 = 权利金 × 乘数。"""
    sizer = PositionSizer(_cfg(_box()))
    assert sizer.margin_for("rb2601C3500", 100, "long") == 1000.0


def test_seller_margin_uses_ratio_and_strike() -> None:
    """卖方缺标的价时用行权价近似：3500×10×12% = 4200。"""
    sizer = PositionSizer(_cfg(_box()))
    assert sizer.margin_for("rb2601C3500", 100, "short") == 4200.0


def test_seller_missing_margin_rejects() -> None:
    """卖方没有保证金数据时必须拒开，不能按权利金放行。"""
    sizer = PositionSizer(_cfg({"multiplier": 10, "price_tick": 0.5, "strike": 3500}))
    sized = sizer.calc_open("rb2601C3500", 100, 90, side="short", kelly_rows=[])
    assert sized.volume == 0
    assert "卖方保证金缺数据" in sized.reason


def test_buyer_calc_open_and_premium_budget() -> None:
    """买方按权利金算仓，并受「期权权利金预算%」二次约束。"""
    sized = PositionSizer(_cfg(_box(), budget_pct=20)).calc_open(
        "rb2601C3500", 100, 90, side="long", kelly_rows=[]
    )
    assert sized.volume == 20
    assert "期权权利金预算上限" in sized.reason


def test_seller_calc_open_uses_margin() -> None:
    """卖方按保证金算仓：10 万 / 4200 ≈ 23 手。"""
    sized = PositionSizer(_cfg(_box())).calc_open(
        "rb2601C3500", 100, 90, side="short", kelly_rows=[]
    )
    assert sized.volume == 23


def test_futures_margin_unchanged() -> None:
    """期货口径不受期权改造影响。"""
    cfg = {
        "trade": {"volume": 1},
        "risk": {
            "sizing_mode": "fixed",
            "capital": 100_000,
            "margin_usage_pct": 100,
            "contracts": {"rb": {"multiplier": 10, "margin_rate_pct": 12}},
        },
    }
    sizer = PositionSizer(cfg)
    assert sizer.margin_for("rb2601", 3500) == 3500 * 10 * 0.12
