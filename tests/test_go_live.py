"""上线四步：同一套柜台接口，不能跳步把策略接到真实资金。"""

from src.trader.ctp_trader import CtpTrader
from src.trader.factory import create_trader
from src.trader.go_live import (
    STAGE_AUTO_LIVE,
    STAGE_AUTO_SIM,
    STAGE_DESK_LIVE,
    STAGE_DESK_SIM,
    allows_strategy_orders,
    apply_account_kind_to_cfg,
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


def test_apply_stage_does_not_overwrite_gui_executor() -> None:
    """上线走到③④也不能把同花顺通道改成 CTP。"""
    cfg = {"trade": {"executor": "gui", "ctp": {"account_kind": "sim"}}}
    apply_stage_to_cfg(cfg, STAGE_DESK_LIVE)
    assert cfg["trade"]["executor"] == "gui"
    assert cfg["trade"]["ctp"]["account_kind"] == "live"
    apply_stage_to_cfg(cfg, STAGE_AUTO_SIM)
    assert cfg["trade"]["executor"] == "gui"
    assert cfg["trade"]["ctp"]["account_kind"] == "sim"
    cfg_ctp = {"trade": {"executor": "ctp", "ctp": {"account_kind": "sim"}}}
    apply_stage_to_cfg(cfg_ctp, STAGE_AUTO_LIVE)
    assert cfg_ctp["trade"]["executor"] == "ctp"
    trader = create_trader(cfg_ctp)
    assert isinstance(trader, CtpTrader)
    assert trader.settings.account_kind == "live"


def test_strategy_block_skips_gui_channel() -> None:
    """同花顺点单不受 CTP 上线四步拦截。"""
    cfg = {"trade": {"go_live": {"stage": STAGE_DESK_SIM}, "executor": "gui"}}
    assert strategy_order_block_reason(cfg) == ""


def test_config_kind_switch_keeps_auto_or_desk_and_picks_sim_profile() -> None:
    """配置页切仿真/真实只换接口：策略自动仍自动，手工仍手工；仿真用 28077。"""
    from src.trader.go_live import pair_stage_with_kind

    assert pair_stage_with_kind(STAGE_AUTO_LIVE, "sim") == STAGE_AUTO_SIM
    assert pair_stage_with_kind(STAGE_AUTO_SIM, "live") == STAGE_AUTO_LIVE
    assert pair_stage_with_kind(STAGE_DESK_LIVE, "sim") == STAGE_DESK_SIM
    cfg = {
        "trade": {
            "executor": "ctp",
            "go_live": {"stage": STAGE_AUTO_LIVE},
            "ctp": {
                "account_kind": "live",
                "ctp_profile": "huatai_live",
                "ctp_profiles": {
                    "huatai_sim": {
                        "kind": "sim",
                        "td_front": "tcp://59.36.3.115:27226",
                        "md_front": "tcp://59.36.3.115:27236",
                        "broker_id": "8080",
                        "user_id": "28077",
                    },
                    "huatai_live": {
                        "kind": "live",
                        "td_front": "tcp://220.196.208.129:41206",
                        "md_front": "tcp://220.196.208.129:41214",
                        "broker_id": "8080",
                        "user_id": "20318052",
                    },
                },
            },
        }
    }
    apply_account_kind_to_cfg(cfg, "sim")
    assert cfg["trade"]["go_live"]["stage"] == STAGE_AUTO_SIM
    assert cfg["trade"]["ctp"]["account_kind"] == "sim"
    assert cfg["trade"]["ctp"]["ctp_profile"] == "huatai_sim"
    trader = create_trader(cfg)
    assert isinstance(trader, CtpTrader)
    assert trader.settings.account_kind == "sim"
    assert trader.settings.user_id == "28077"
    assert "59.36.3.115" in trader.settings.td_front


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
    cfg = apply_stage_to_cfg({"trade": {"executor": "ctp"}}, STAGE_AUTO_SIM)
    assert strategy_order_block_reason(cfg) == ""
    assert current_stage_from_cfg(cfg) == STAGE_AUTO_SIM


def test_hints_say_desk_is_manual_and_auto_uses_resident() -> None:
    desk = stage_hint(STAGE_DESK_SIM)
    auto = stage_hint(STAGE_AUTO_SIM)
    assert "手工" in desk
    assert "启动常驻" in auto
    assert "交易台" in desk
