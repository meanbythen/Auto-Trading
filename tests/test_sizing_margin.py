"""保证金与仓位上限会卡住能开的手数。"""

from src.risk.sizing import PositionSizer, bind_run_margin


def _cfg(
    margin: float,
    mode: str = "risk_pct",
    *,
    capital: float = 14000,
    margin_usage_pct: float = 100,
) -> dict:
    return {
        "trade": {"volume": 2},
        "risk": {
            "sizing_mode": mode,
            "capital": capital,
            "risk_per_trade_pct": 30,
            "min_volume": 1,
            "margin_per_lot": margin,
            "margin_usage_pct": margin_usage_pct,
            "contracts": {"bu": {"multiplier": 10}},
        },
    }


def test_margin_cuts_two_lots_down_to_one() -> None:
    """止损很近时风险允许 2 手，但 14000 元只够 7794 元保证金的 1 手。"""
    sized = PositionSizer(_cfg(7794)).calc_open("bu2612", 4585, 4575)
    assert sized.volume == 1
    assert "仓位上限" in sized.reason or "保证金" in sized.reason


def test_margin_zero_does_not_block() -> None:
    """没填保证金时，仓位上限层不拦；手数只看每笔风险算出来的意图。"""
    sized = PositionSizer(_cfg(0)).calc_open("bu2612", 4585, 4575)
    assert sized.volume == sized.intent_lots
    assert sized.volume > 0
    assert sized.margin_lots is None


def test_margin_usage_pct_keeps_buffer() -> None:
    """
    权益 21373、每手 10500：按全部权益能开 2 手；
    仓位上限 80% 时预算约 17098，只够 1 手。
    """
    full = PositionSizer(
        _cfg(10500, capital=21373, margin_usage_pct=100)
    ).calc_open("fu2701", 5000, 4990)
    assert full.volume == 2

    capped = PositionSizer(
        _cfg(10500, capital=21373, margin_usage_pct=80)
    ).calc_open("fu2701", 5000, 4990)
    assert capped.volume == 1
    assert capped.margin_lots == 1


def test_margin_usage_allows_two_lots_with_enough_equity() -> None:
    """2 手 × 10500，仓位上限 70% 时权益至少约 30000 才开得出。"""
    cfg = _cfg(10500, capital=30000, margin_usage_pct=70)
    cfg["risk"]["contracts"] = {"fu": {"multiplier": 10, "margin": 10500}}
    sized = PositionSizer(cfg).calc_open("fu2701", 5000, 4990)
    assert sized.volume == 2


def test_each_product_uses_its_own_margin() -> None:
    """沥青和玉米保证金不同。已经拿着 1 手沥青时，剩下的钱按玉米的保证金算。"""
    cfg = _cfg(0, margin_usage_pct=100)
    cfg["risk"]["contracts"]["bu"]["margin"] = 7794
    cfg["risk"]["contracts"]["c"] = {"multiplier": 10, "name": "玉米", "margin": 1500}
    sizer = PositionSizer(cfg)
    corn = sizer.calc_open("c2601", 2300, 2290, current_abs_position=1, positions={"bu2612": 1})
    assert corn.volume >= 1
    assert "1500" in corn.reason
    asphalt = sizer.calc_open("bu2612", 4585, 4575, positions={"bu2612": 1})
    assert asphalt.volume == 0


def test_backtest_margin_box_overrides_product_margin() -> None:
    """回测这一次只认输入框。品种自己的保证金先清掉，填 0 就不再被 7794 卡住。"""
    cfg = _cfg(0)
    cfg["risk"]["contracts"]["bu"]["margin"] = 7794
    bind_run_margin(cfg, 0)
    sized = PositionSizer(cfg).calc_open("bu2612", 4585, 4575)
    assert sized.volume == sized.intent_lots
    assert sized.margin_lots is None
    assert cfg["risk"]["contracts"]["bu"]["margin"] == 0


def test_ctp_available_caps_margin_without_double_count() -> None:
    """柜台可用资金更紧时按可用封顶；没有可用时手数和以前一样。"""
    free = PositionSizer(_cfg(10500, capital=21373, margin_usage_pct=100))
    full = free.calc_open("fu2701", 5000, 4990)
    assert full.volume == 2

    capped = PositionSizer(_cfg(10500, capital=21373, margin_usage_pct=100))
    capped.cash_available = 10500
    sized = capped.calc_open("fu2701", 5000, 4990)
    assert sized.volume == 1
    assert "柜台可用" in sized.reason


def test_backtest_lots_stay_on_start_capital() -> None:
    """真实账户口径关：手数按起始权益，纸面盈亏变了也不加仓减仓。"""
    from src.backtest.engine import BacktestLimits

    cfg = {
        "trade": {"volume": 1},
        "risk": {
            "sizing_mode": "risk_pct",
            "capital": 1_030_018,
            "risk_per_trade_pct": 10,
            "min_volume": 1,
            "margin_per_lot": 9384,
            "margin_usage_pct": 80,
            "apply_limits": True,
            "contracts": {"fu": {"multiplier": 10, "margin": 9384}},
        },
    }
    limits = BacktestLimits(cfg)
    limits.note_equity(1_030_018)
    high, _ = limits.size_open("fu2701", 5000, 4990)
    assert high == 87
    limits.note_equity(500_000)
    still, reason = limits.size_open("fu2701", 5000, 4990)
    assert still == high
    assert "500000" not in reason.replace(",", "")


def test_backtest_lots_follow_equity_when_chain_capital() -> None:
    """真实账户口径开：手数跟着剩余权益走，权益腰斩后面手数也少。"""
    from src.backtest.engine import BacktestLimits

    cfg = {
        "trade": {"volume": 1},
        "backtest": {"chain_capital": True},
        "risk": {
            "sizing_mode": "risk_pct",
            "capital": 1_030_018,
            "risk_per_trade_pct": 10,
            "min_volume": 1,
            "margin_per_lot": 9384,
            "margin_usage_pct": 80,
            "apply_limits": True,
            "contracts": {"fu": {"multiplier": 10, "margin": 9384}},
        },
    }
    limits = BacktestLimits(cfg)
    limits.note_equity(1_030_018)
    high, _ = limits.size_open("fu2701", 5000, 4990)
    limits.note_equity(500_000)
    low, reason = limits.size_open("fu2701", 5000, 4990)
    assert high > low
    assert low == 42
    assert "500000" in reason.replace(",", "") or "500,000" in reason


def test_fu_night_trade_stays_two_lots_on_live_capital() -> None:
    """口径关：燃油 4032 空、止损 4119、起始 2.9 万。纸面涨到 4.2 万仍是 2 手，和当时实盘一致。"""
    from src.backtest.engine import BacktestLimits

    cfg = {
        "trade": {"volume": 1},
        "risk": {
            "sizing_mode": "risk_pct",
            "capital": 29493.74,
            "risk_per_trade_pct": 10,
            "min_volume": 1,
            "margin_usage_pct": 80,
            "apply_limits": True,
            "contracts": {
                "fu": {"multiplier": 10, "name": "燃油", "margin_rate_pct": 22.74}
            },
        },
    }
    limits = BacktestLimits(cfg)
    # 口径关：纸面权益涨到 4.2 万，手数仍按 29493 算，不会变成 3 手。
    limits.note_equity(42000)
    vol, reason = limits.size_open("fu2701", 4032.0, 4119.0)
    assert vol == 2
    assert "意图3" in reason.replace(" ", "") or "意图 3" in reason


def test_fu_night_trade_adds_lot_when_chain_capital() -> None:
    """口径开：同一口夜盘箱子，纸面权益涨到 4.2 万后按剩余权益开，会变成 3 手。"""
    from src.backtest.engine import BacktestLimits

    cfg = {
        "trade": {"volume": 1},
        "backtest": {"chain_capital": True},
        "risk": {
            "sizing_mode": "risk_pct",
            "capital": 29493.74,
            "risk_per_trade_pct": 10,
            "min_volume": 1,
            "margin_usage_pct": 80,
            "apply_limits": True,
            "contracts": {
                "fu": {"multiplier": 10, "name": "燃油", "margin_rate_pct": 22.74}
            },
        },
    }
    limits = BacktestLimits(cfg)
    limits.note_equity(42000)
    vol, _reason = limits.size_open("fu2701", 4032.0, 4119.0)
    assert vol == 3


def test_margin_rate_grows_with_price() -> None:
    """比例固定时，价格越高每手占用越多，同样的钱能开的手数变少。"""
    cfg = _cfg(0, capital=30000, margin_usage_pct=100)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "name": "燃油", "margin_rate_pct": 23}
    }
    cheap = PositionSizer(cfg).calc_open("fu2701", 4000, 3990)
    dear = PositionSizer(cfg).calc_open("fu2701", 5000, 4990)
    # 4000×10×23%=9200；5000×10×23%=11500
    assert cheap.volume == 3
    assert dear.volume == 2
    assert "9200" in cheap.reason or "每手9200" in cheap.reason.replace(",", "")
    assert cheap.margin_lots == 3
    assert dear.margin_lots == 2


def test_margin_blocks_when_equity_cannot_buy_one_lot() -> None:
    sized = PositionSizer(_cfg(15000, mode="fixed")).calc_open("bu2612", 4585, 4575)
    assert sized.volume == 0
    assert "仓位上限不够" in sized.reason or "保证金不够" in sized.reason
