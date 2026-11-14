"""交割月提前换月：解析合约、裁时段、日盘收盘平仓。不依赖外网。"""

from __future__ import annotations

from datetime import date, datetime

from src.backtest.rollover import MainPeriod
from src.risk.delivery import (
    apply_delivery_cutoff,
    contract_gate,
    delivery_month_start,
    extend_last_before_delivery,
    last_hold_date,
    live_hint,
    successor_symbol,
)
from src.strategy.decision_guard import apply_delivery_gate
from src.strategy.types import StrategyAction, StrategyDecision


def test_parse_fuel_oil_delivery_month() -> None:
    assert delivery_month_start("fu2701") == date(2027, 1, 1)
    assert last_hold_date("fu2701") == date(2026, 12, 31)
    assert successor_symbol("fu2701") == "fu2703"


def test_parse_rebar_and_czce() -> None:
    assert delivery_month_start("rb2605") == date(2026, 5, 1)
    assert last_hold_date("rb2605") == date(2026, 4, 30)
    assert successor_symbol("rb2605") == "rb2610"
    assert delivery_month_start("TA605", as_of=date(2026, 3, 1)) == date(2026, 5, 1)


def test_cutoff_does_not_delay_early_oi_roll() -> None:
    """持仓量已经提前换走时，不要为了「用满到交割月前」把旧合约拖长。"""
    periods = [
        MainPeriod("rb", "rb2605", date(2026, 1, 5), date(2026, 4, 2)),
        MainPeriod("rb", "rb2610", date(2026, 4, 3), date(2026, 8, 27)),
    ]

    got = apply_delivery_cutoff(periods)

    assert [(p.symbol, p.start, p.end) for p in got] == [
        ("rb2605", date(2026, 1, 5), date(2026, 4, 2)),
        ("rb2610", date(2026, 4, 3), date(2026, 8, 27)),
    ]


def test_cutoff_clips_into_delivery_and_hands_off() -> None:
    """表把旧合约记进交割月时，旧段停在前一个月最后一天，余下日子给下一手。"""
    periods = [
        MainPeriod("fu", "fu2701", date(2026, 10, 13), date(2027, 1, 15)),
    ]

    got = apply_delivery_cutoff(periods)

    assert [(p.symbol, p.start, p.end) for p in got] == [
        ("fu2701", date(2026, 10, 13), date(2026, 12, 31)),
        ("fu2703", date(2027, 1, 1), date(2027, 1, 15)),
    ]


def test_extend_last_stops_before_delivery() -> None:
    periods = [
        MainPeriod("fu", "fu2701", date(2026, 10, 13), date(2026, 12, 20)),
    ]

    got, note = extend_last_before_delivery(periods, date(2027, 1, 20))

    assert [(p.symbol, p.start, p.end) for p in got] == [
        ("fu2701", date(2026, 10, 13), date(2026, 12, 31)),
        ("fu2703", date(2027, 1, 1), date(2027, 1, 20)),
    ]
    assert "交割月" in note
    assert "fu2703" in note


def test_gate_flat_after_day_close_on_last_hold() -> None:
    """12月31日 15:00 日盘收完，夜盘已算 1 月交割月，必须平仓。"""
    noon = contract_gate("fu2701", datetime(2026, 12, 31, 14, 30))
    close = contract_gate("fu2701", datetime(2026, 12, 31, 15, 0))
    night = contract_gate("fu2701", datetime(2026, 12, 31, 21, 30))

    assert noon.flatten is False and noon.block_open is False
    assert close.flatten is True and close.block_open is True
    assert night.flatten is True
    assert "交割月" in close.reason


def test_live_hint_warns_near_delivery() -> None:
    text, warn = live_hint("fu2701", datetime(2026, 12, 20, 10, 0))
    assert "2027年1月" in text
    assert "2026-12-31" in text
    assert warn is True
    far, far_warn = live_hint("fu2701", datetime(2026, 11, 1, 10, 0))
    assert "交割月" in far
    assert far_warn is False


def test_gate_decision_flattens_holding_and_blocks_new_open() -> None:
    """实盘和回测共用的闸门：有仓改平仓，空仓的开仓改观望并交回被拒决策。"""
    open_long = StrategyDecision(StrategyAction.BUY_OPEN, "fu2701", "突破")
    in_month = datetime(2027, 1, 5, 9, 5)

    held = apply_delivery_gate(
        "fu2701", in_month, open_long, flat=False, flatten_meta={"delivery_flatten": True}
    )
    assert held.decision.action == StrategyAction.CLOSE
    assert held.flattened is True
    assert held.rejected is None
    assert held.decision.meta == {"delivery_flatten": True}

    flat = apply_delivery_gate("fu2701", in_month, open_long, flat=True)
    assert flat.decision.action == StrategyAction.HOLD
    assert flat.rejected is open_long
    assert flat.flattened is False


def test_gate_decision_leaves_other_contracts_alone() -> None:
    """没进交割月的合约，决策原样返回，不记拒单。"""
    decision = StrategyDecision(StrategyAction.BUY_OPEN, "bu2612", "突破")
    outcome = apply_delivery_gate("bu2612", datetime(2026, 11, 14, 10, 0), decision, flat=True)
    assert outcome.decision is decision
    assert outcome.rejected is None
    assert outcome.flattened is False
