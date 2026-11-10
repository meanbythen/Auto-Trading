"""上线四步：同一套柜台接口，不能跳步把策略接到真实资金。"""

from src.trader.ctp_trader import CtpTrader
from src.trader.factory import create_trader
from src.trader.go_live import (
    STAGE_AUTO_LIVE,
    STAGE_AUTO_SIM,
    STAGE_DESK_LIVE,
    STAGE_DESK_SIM,
    allows_strategy_orders,
    apply_stage_to_cfg,
    current_stage_from_cfg,
    empty_state,
    huatai_incomplete,
    load_state,
    mark_stage_passed,
    save_state,
    stage_hint,
    strategy_order_block_reason,
    why_cannot_enter,
)


def test_desk_stage_blocks_strategy_orders() -> None:
    assert allows_strategy_orders(STAGE_DESK_SIM) is False
    assert allows_strategy_orders(STAGE_DESK_LIVE) is False
    assert allows_strategy_orders(STAGE_AUTO_SIM) is True
    assert allows_strategy_orders(STAGE_AUTO_LIVE) is True


def test_apply_stage_keeps_same_ctp_and_only_auto_sets_executor() -> None:
    cfg = {"trade": {"executor": "gui", "ctp": {"account_kind": "sim"}}}
    apply_stage_to_cfg(cfg, STAGE_DESK_LIVE)
    assert cfg["trade"]["executor"] == "gui"
    assert cfg["trade"]["ctp"]["account_kind"] == "live"
    apply_stage_to_cfg(cfg, STAGE_AUTO_SIM)
    assert cfg["trade"]["executor"] == "ctp"
    assert cfg["trade"]["ctp"]["account_kind"] == "sim"
    trader = create_trader(cfg)
    assert isinstance(trader, CtpTrader)
    assert trader.settings.account_kind == "sim"


def test_cannot_skip_to_live_auto_without_sim_auto() -> None:
    state = empty_state()
    state["passed"][STAGE_DESK_SIM] = True
    assert "真实资金" in why_cannot_enter(state, STAGE_AUTO_LIVE)
    state["passed"][STAGE_DESK_LIVE] = True
    assert "仿真" in why_cannot_enter(state, STAGE_AUTO_LIVE)
    state["passed"][STAGE_AUTO_SIM] = True
    assert why_cannot_enter(state, STAGE_AUTO_LIVE) == ""


def test_huatai_must_be_ticked_before_desk_sim_passed() -> None:
    state = empty_state()
    ok, message = mark_stage_passed(state, STAGE_DESK_SIM)
    assert ok is False
    assert "华泰" in message
    assert huatai_incomplete(state)
    for key in list(state["huatai"]):
        state["huatai"][key] = True
    ok, message = mark_stage_passed(state, STAGE_DESK_SIM)
    assert ok is True
    assert why_cannot_enter(state, STAGE_DESK_LIVE) == ""


def test_state_roundtrip(tmp_path) -> None:
    state = empty_state()
    state["stage"] = STAGE_AUTO_SIM
    state["passed"][STAGE_DESK_SIM] = True
    save_state(tmp_path, state)
    loaded = load_state(tmp_path)
    assert loaded["stage"] == STAGE_AUTO_SIM
    assert loaded["passed"][STAGE_DESK_SIM] is True


def test_strategy_block_reason_reads_cfg() -> None:
    cfg = {"trade": {"go_live": {"stage": STAGE_DESK_SIM}, "executor": "ctp"}}
    text = strategy_order_block_reason(cfg)
    assert "交易台" in text
    cfg = apply_stage_to_cfg({"trade": {}}, STAGE_AUTO_SIM)
    assert strategy_order_block_reason(cfg) == ""
    assert current_stage_from_cfg(cfg) == STAGE_AUTO_SIM


def test_hints_say_desk_is_manual_and_auto_uses_resident() -> None:
    desk = stage_hint(STAGE_DESK_SIM)
    auto = stage_hint(STAGE_AUTO_SIM)
    assert "手工" in desk
    assert "启动常驻" in auto
    assert "交易台" in desk
