"""保证金只按 价格×每手数量×比例 计算；缺比例直接拒绝开仓。"""

import pytest

from src.risk.sizing import PositionSizer, disable_margin_cap


def _cfg(
    rate_pct: float,
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
            "margin_usage_pct": margin_usage_pct,
            "contracts": {
                "bu": {"multiplier": 10, "margin_rate_pct": rate_pct},
                "fu": {"multiplier": 10, "margin_rate_pct": rate_pct},
            },
        },
    }


def test_margin_cuts_two_lots_down_to_one() -> None:
    """止损很近时风险允许 2 手，但 14000 元只够 7794 元保证金的 1 手。"""
    sized = PositionSizer(_cfg(17)).calc_open("bu2612", 4585, 4575)
    assert sized.volume == 1
    assert "仓位上限" in sized.reason or "保证金" in sized.reason


def test_missing_margin_rejects_open() -> None:
    """品种没有保证金比例时直接拒开，不能用任何固定金额越过风控。"""
    sized = PositionSizer(_cfg(0)).calc_open("bu2612", 4585, 4575)
    assert sized.volume == 0
    assert sized.margin_lots == 0
    assert "保证金比例" in sized.reason
    assert "拒绝开仓" in sized.reason


def test_symbol_absent_from_table_rejects_open() -> None:
    """ag2702 这类表里完全没有的品种：即使其它品种有比例，也必须拒开。"""
    sized = PositionSizer(_cfg(17)).calc_open("ag2702", 8900, 9000)
    assert sized.volume == 0
    assert "ag2702" in sized.reason
    assert "请先在「品种与合约」表里补上保证金比例%" in sized.reason


def test_margin_is_price_times_qty_times_rate() -> None:
    """每手占用 = 开仓价 × 每手数量 × 比例%；公式口径单独钉住。"""
    sizer = PositionSizer(_cfg(17))
    assert round(sizer.margin_for("bu2612", 4585), 2) == 7794.5
    assert sizer.margin_for("bu2612", 5000) > sizer.margin_for("bu2612", 4000)
    assert sizer.margin_for("bu2612", None) == 0.0


def test_ignore_margin_cap_skips_the_layer_for_research() -> None:
    """交叉测试显式关闭保证金上限后，不看比例也不拦手数。"""
    cfg = _cfg(17)
    disable_margin_cap(cfg)
    sized = PositionSizer(cfg).calc_open("bu2612", 4585, 4575)
    assert sized.volume == sized.intent_lots
    assert sized.margin_lots is None
    assert cfg["risk"]["contracts"]["bu"]["margin_rate_pct"] == 17
    assert "margin" not in cfg["risk"]["contracts"]["bu"]


def test_complete_risk_includes_fees_and_two_sided_symbol_slippage() -> None:
    """每手风险 = 止损金额 + 开平费 + 开平各一次滑点。"""
    cfg = _cfg(100, capital=100_000)
    cfg["risk"]["risk_per_trade_pct"] = 1
    cfg["risk"]["contracts"]["bu"].update(
        {"commission": 1.5, "commission_close": 0.5, "commission_close_today": 2.5, "price_tick": 0.5}
    )
    cfg["backtest"] = {"slippage_ticks": 9, "slippage_by_symbol": {"BU2612": 2}}
    sized = PositionSizer(cfg).calc_open("bu2612", 1000, 990)
    # 10点×10 + 开1.5 + 平今2.5 + 双边2跳×0.5×10 = 124元/手。
    assert sized.risk_per_lot == 124
    assert sized.risk_cash == 1000
    assert sized.volume == 8
    assert "开平费4.00" in sized.reason
    assert "双边滑点20.00" in sized.reason


def test_margin_usage_pct_keeps_buffer() -> None:
    """
    权益 21373、每手 10500：按全部权益能开 2 手；
    仓位上限 80% 时预算约 17098，只够 1 手。
    """
    full = PositionSizer(
        _cfg(21, capital=21373, margin_usage_pct=100)
    ).calc_open("fu2701", 5000, 4990)
    assert full.volume == 2

    capped = PositionSizer(
        _cfg(21, capital=21373, margin_usage_pct=80)
    ).calc_open("fu2701", 5000, 4990)
    assert capped.volume == 1
    assert capped.margin_lots == 1


def test_margin_usage_allows_two_lots_with_enough_equity() -> None:
    """2 手 × 10500，仓位上限 70% 时权益至少约 30000 才开得出。"""
    cfg = _cfg(21, capital=30000, margin_usage_pct=70)
    sized = PositionSizer(cfg).calc_open("fu2701", 5000, 4990)
    assert sized.volume == 2


def test_each_product_uses_its_own_margin() -> None:
    """沥青和玉米比例不同。已经拿着 1 手沥青时，剩下的钱按玉米的占用算。"""
    cfg = _cfg(17, margin_usage_pct=100)
    cfg["risk"]["contracts"]["c"] = {"multiplier": 10, "name": "玉米", "margin_rate_pct": 6.5217}
    sizer = PositionSizer(cfg)
    corn = sizer.calc_open("c2601", 2300, 2290, current_abs_position=1, positions={"bu2612": 1})
    assert corn.volume >= 1
    assert "1500" in corn.reason
    asphalt = sizer.calc_open("bu2612", 4585, 4575, positions={"bu2612": 1})
    assert asphalt.volume == 0


def test_per_order_cap_cuts_lots_before_the_broker_rejects() -> None:
    """资金管理允许更多手，但本机单笔上限只有 1 手时，算仓就直接压到 1 手。"""
    cfg = _cfg(17)
    disable_margin_cap(cfg)
    sized = PositionSizer(cfg).calc_open("bu2612", 4585, 4575, max_per_order=1)
    assert sized.intent_lots >= 2
    assert sized.volume == 1
    assert "单笔上限1" in sized.reason


def test_per_order_cap_does_not_change_lots_below_the_cap() -> None:
    """上限比算出来的手数大时，手数不受影响。"""
    cfg = _cfg(17)
    disable_margin_cap(cfg)
    free = PositionSizer(cfg).calc_open("bu2612", 4585, 4575)
    capped = PositionSizer(cfg).calc_open("bu2612", 4585, 4575, max_per_order=99)
    assert capped.volume == free.volume
    assert "单笔上限99" in capped.reason


def test_ctp_available_caps_margin_without_double_count() -> None:
    """柜台可用资金更紧时按可用封顶；没有可用时手数和以前一样。"""
    free = PositionSizer(_cfg(21, capital=21373, margin_usage_pct=100))
    full = free.calc_open("fu2701", 5000, 4990)
    assert full.volume == 2

    capped = PositionSizer(_cfg(21, capital=21373, margin_usage_pct=100))
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
            "margin_usage_pct": 80,
            "apply_limits": True,
            "contracts": {"fu": {"multiplier": 10, "margin_rate_pct": 18.768}},
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
            "margin_usage_pct": 80,
            "apply_limits": True,
            "contracts": {"fu": {"multiplier": 10, "margin_rate_pct": 18.768}},
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
    cfg = _cfg(23, capital=30000, margin_usage_pct=100)
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
    sized = PositionSizer(_cfg(32.7154, mode="fixed")).calc_open("bu2612", 4585, 4575)
    assert sized.volume == 0
    assert "仓位上限不够" in sized.reason or "保证金不够" in sized.reason


def test_resolve_margin_rate_prefers_product_table() -> None:
    from src.backtest.metrics import resolve_margin_rate

    cfg = {"risk": {"contracts": {"rb": {"margin_rate_pct": 12}}}}
    assert resolve_margin_rate(cfg, "rb2510", {}) == pytest.approx(0.12)


def test_resolve_margin_rate_falls_back_to_backtest_config() -> None:
    from src.backtest.metrics import resolve_margin_rate

    cfg = {"risk": {"contracts": {}}}
    assert resolve_margin_rate(cfg, "rb2510", {"margin_rate": 0.15}) == pytest.approx(0.15)


def test_resolve_margin_rate_defaults_when_missing() -> None:
    from src.backtest.metrics import resolve_margin_rate
    from src.domain.contracts import MARGIN_RATE

    assert resolve_margin_rate({"risk": {"contracts": {}}}, "rb2510", {}) == MARGIN_RATE
    assert resolve_margin_rate(None, "rb2510", None) == MARGIN_RATE


def test_resolve_margin_rate_rejects_illegal_backtest_config() -> None:
    from src.backtest.metrics import resolve_margin_rate
    from src.domain.contracts import MARGIN_RATE

    cfg = {"risk": {"contracts": {}}}
    assert resolve_margin_rate(cfg, "rb2510", {"margin_rate": 5}) == MARGIN_RATE
    assert resolve_margin_rate(cfg, "rb2510", {"margin_rate": "bad"}) == MARGIN_RATE


def test_leg_alloc_keeps_one_lot_each_then_fills_budget() -> None:
    """4.2万权益、80%上限：燃油每手8864、生猪每手15152 → 2手燃油+1手生猪，占用78%。"""
    from src.risk.leg_alloc import allocate

    plan = allocate({"fu2701": 8864, "lh2703": 15152}, 42171.81 * 0.8)
    assert plan.lots == {"fu2701": 2, "lh2703": 1}
    assert plan.uncovered == ()
    assert plan.used <= plan.budget
    assert "fu2701 2手" in plan.summary()


def test_leg_alloc_reports_uncovered_leg_when_budget_too_small() -> None:
    """预算连一人一手都不够时，先保便宜的腿，另一条腿明确报未覆盖。"""
    from src.risk.leg_alloc import allocate

    plan = allocate({"fu2701": 8864, "lh2703": 15152}, 20000)
    assert plan.lots["fu2701"] == 2
    assert plan.lots["lh2703"] == 0
    assert plan.uncovered == ("lh2703",)
    assert plan.used <= 20000


def test_leg_quota_caps_each_leg_even_if_money_allows_more() -> None:
    """组合分配 2 手燃油 + 1 手生猪：燃油不能再开第 3 手，生猪留得住 1 手。"""
    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    sizer = PositionSizer(cfg)
    sizer.set_leg_quota({"fu": 2, "lh": 1}, {"fu": 8864.05, "lh": 15152})
    fu = sizer.calc_open("fu2701", 3898, 3858)
    assert fu.volume == 2
    assert "组合分配上限2手" in fu.reason
    lh = sizer.calc_open("lh2703", 9470, 9440, positions={"fu2701": 2})
    assert lh.volume == 1
    again = sizer.calc_open("fu2701", 3898, 3858, positions={"fu2701": 2, "lh2703": 1})
    assert again.volume == 0
    assert "组合分配2手已用完" in again.reason


def test_no_leg_quota_keeps_first_come_first_served() -> None:
    """没配组合分配时保持原行为：同一笔预算里燃油可以开到 3 手。"""
    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    sized = PositionSizer(cfg).calc_open("fu2701", 3898, 3858)
    assert sized.volume == 3
    assert "组合分配" not in sized.reason


def test_sync_leg_quota_only_in_multi_symbol_mode() -> None:
    """多品种挂机时按当前价格自动分配 2 手燃油 + 1 手生猪；单品种模式清空分配。"""
    from types import SimpleNamespace

    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    prices = {"fu2701": 3898.0, "lh2703": 9470.0}
    host = SimpleNamespace(
        _multi_enabled=True,
        symbols=["fu2701", "lh2703"],
        _multi_symbols=[],
        sizer=PositionSizer(cfg),
        kline=SimpleNamespace(latest_price=lambda symbol: prices.get(symbol, 0.0)),
    )
    OrderExecutorMixin._sync_leg_quota(host, {"fu2701": 3898.0})
    assert host.sizer.leg_quota == {"fu": 2, "lh": 1}
    assert host.sizer.leg_lot_margin["lh"] == pytest.approx(15152, rel=1e-6)

    host._multi_enabled = False
    OrderExecutorMixin._sync_leg_quota(host, {})
    assert host.sizer.leg_quota is None


def test_leg_quota_never_exceeds_the_risk_budget() -> None:
    """分配只是上限：给到 99 手也不能越过权益×仓位上限%算出来的手数。"""
    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {"fu": {"multiplier": 10, "margin_rate_pct": 22.74}}
    sizer = PositionSizer(cfg)
    sizer.set_leg_quota({"fu": 99}, {"fu": 8864.05})
    sized = sizer.calc_open("fu2701", 3898, 3858)
    assert sized.volume == 3
    assert sized.volume < 99
    assert "仓位上限" in sized.reason


def test_leg_alloc_never_exceeds_budget_for_any_budget() -> None:
    """任何预算下分配占用都不得越界，且先保每腿至少 1 手。"""
    from src.risk.leg_alloc import allocate

    legs = {"fu2701": 8864.05, "lh2703": 15152.0}
    for budget in range(0, 40001, 500):
        plan = allocate(legs, float(budget))
        assert plan.used <= plan.budget + 1e-6
        if budget >= 24016.05:
            assert plan.lots["fu2701"] >= 1
            assert plan.lots["lh2703"] >= 1


def test_sync_leg_quota_skips_and_warns_when_margin_missing() -> None:
    """某条腿缺保证金比例时不做「占用最大化」，退回单信号风控；该腿开仓被拒绝。"""
    from types import SimpleNamespace

    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16},  # 故意不填保证金比例
    }
    prices = {"fu2701": 3898.0, "lh2703": 9470.0}
    host = SimpleNamespace(
        _multi_enabled=True,
        symbols=["fu2701", "lh2703"],
        _multi_symbols=[],
        sizer=PositionSizer(cfg),
        kline=SimpleNamespace(latest_price=lambda symbol: prices.get(symbol, 0.0)),
    )
    OrderExecutorMixin._sync_leg_quota(host, {"fu2701": 3898.0})
    assert host.sizer.leg_quota is None
    rejected = host.sizer.calc_open("lh2703", 9470, 9440)
    assert rejected.volume == 0
    assert "保证金比例" in rejected.reason
    assert "拒绝开仓" in rejected.reason


def test_sync_leg_quota_skips_when_margin_cap_disabled() -> None:
    """研究/交叉测试关掉保证金层时，组合分配也必须停用。"""
    from types import SimpleNamespace

    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    disable_margin_cap(cfg)
    prices = {"fu2701": 3898.0, "lh2703": 9470.0}
    host = SimpleNamespace(
        _multi_enabled=True,
        symbols=["fu2701", "lh2703"],
        _multi_symbols=[],
        sizer=PositionSizer(cfg),
        kline=SimpleNamespace(latest_price=lambda symbol: prices.get(symbol, 0.0)),
    )
    OrderExecutorMixin._sync_leg_quota(host, {"fu2701": 3898.0})
    assert host.sizer.leg_quota is None


def test_margin_missing_writes_run_page_alert(tmp_path) -> None:
    """缺保证金比例时除日志外，还要在运行页「最近告警」留一条。"""
    from types import SimpleNamespace

    from src.ops.alerts import last_alert
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16},
    }
    prices = {"fu2701": 3898.0, "lh2703": 9470.0}
    host = SimpleNamespace(
        root=tmp_path,
        _multi_enabled=True,
        symbols=["fu2701", "lh2703"],
        _multi_symbols=[],
        sizer=PositionSizer(cfg),
        kline=SimpleNamespace(latest_price=lambda symbol: prices.get(symbol, 0.0)),
    )
    OrderExecutorMixin._sync_leg_quota(host, {"fu2701": 3898.0})
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "leg_margin_missing"
    assert "lh2703" in row["message"]
    assert "补上比例" in row["message"] or "补上保证金比例" in row["message"]


def test_single_symbol_open_rejected_writes_alert(tmp_path) -> None:
    """单品种模式下缺保证金比例，算仓拒开的同时也写运行页告警。"""
    from types import SimpleNamespace

    from src.ops.alerts import last_alert
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {"lh": {"multiplier": 16}}  # 故意不填比例
    host = SimpleNamespace(
        root=tmp_path,
        cfg=cfg,
        sizer=PositionSizer(cfg),
        risk=SimpleNamespace(total_abs_position=lambda: 0, positions={}),
    )
    decision = SimpleNamespace(
        symbol="lh2703", entry_price=9470.0, stop_price=9440.0, meta={}
    )
    volume, _reason = OrderExecutorMixin._size_open(host, decision)
    assert volume == 0
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "leg_margin_missing"
    assert "lh2703" in row["message"]


def _write_alert(root, code: str, message: str) -> None:
    import json

    from src.ops.alerts import alerts_path

    path = alerts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": "2026-11-26 09:00:00",
        "level": "warning",
        "code": code,
        "message": message,
        "run_id": "",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _multi_host(tmp_path, cfg, prices):
    from types import SimpleNamespace

    return SimpleNamespace(
        root=tmp_path,
        _multi_enabled=True,
        symbols=["fu2701", "lh2703"],
        _multi_symbols=[],
        sizer=PositionSizer(cfg),
        kline=SimpleNamespace(latest_price=lambda symbol: prices.get(symbol, 0.0)),
    )


def test_leg_quota_success_clears_stale_margin_alert(tmp_path) -> None:
    """数据补齐、分配恢复正常后，运行页那条缺比例告警转为「已恢复」，不再当活跃故障。"""
    from src.ops.alerts import last_alert
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    _write_alert(tmp_path, "leg_margin_missing", "缺保证金比例，lh2703 开仓被拒绝：请先补比例")
    host = _multi_host(tmp_path, cfg, {"fu2701": 3898.0, "lh2703": 9470.0})
    OrderExecutorMixin._sync_leg_quota(host, {"fu2701": 3898.0})
    assert host.sizer.leg_quota == {"fu": 2, "lh": 1}
    row = last_alert(tmp_path)
    assert row is not None
    assert row["code"] == "leg_margin_missing"
    assert row["recovered"] is True


def test_leg_quota_success_writes_no_recovery_without_alert(tmp_path) -> None:
    """本来就没有告警时，不写多余的恢复行。"""
    from src.ops.alerts import alerts_path
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    host = _multi_host(tmp_path, cfg, {"fu2701": 3898.0, "lh2703": 9470.0})
    OrderExecutorMixin._sync_leg_quota(host, {"fu2701": 3898.0})
    assert not alerts_path(tmp_path).exists()


def test_single_symbol_success_keeps_other_leg_alert(tmp_path) -> None:
    """只有正文提到本合约的告警才撤；另一条腿的缺比例告警不能被顺手清掉。"""
    from types import SimpleNamespace

    from src.ops.alerts import last_alert
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    _write_alert(tmp_path, "leg_margin_missing", "缺保证金比例，fu2701 开仓被拒绝：请先补比例")
    host = SimpleNamespace(
        root=tmp_path,
        cfg=cfg,
        sizer=PositionSizer(cfg),
        risk=SimpleNamespace(total_abs_position=lambda: 0, positions={}),
    )
    decision = SimpleNamespace(
        symbol="lh2703", entry_price=9470.0, stop_price=9440.0, meta={}
    )
    OrderExecutorMixin._size_open(host, decision)
    row = last_alert(tmp_path)
    assert row is not None
    assert "fu2701" in row["message"]
    assert not row.get("recovered")


def test_leg_quota_recovery_written_once(tmp_path) -> None:
    """连续多次分配成功只补一条恢复行，不能每笔信号都往告警文件里写。"""
    from src.ops.alerts import alerts_path
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = _cfg(22.74, capital=42171.81, margin_usage_pct=80)
    cfg["risk"]["contracts"] = {
        "fu": {"multiplier": 10, "margin_rate_pct": 22.74},
        "lh": {"multiplier": 16, "margin_rate_pct": 10},
    }
    _write_alert(tmp_path, "leg_margin_missing", "缺保证金比例，lh2703 开仓被拒绝：请先补比例")
    host = _multi_host(tmp_path, cfg, {"fu2701": 3898.0, "lh2703": 9470.0})
    for _ in range(3):
        OrderExecutorMixin._sync_leg_quota(host, {"fu2701": 3898.0})
    text = alerts_path(tmp_path).read_text(encoding="utf-8")
    assert text.count('"code": "leg_alloc_ok"') == 1
