"""交易台和常驻策略发出的 CTP 指令必须同一套代码。"""

from types import SimpleNamespace

from src.trader.ctp_gateway import CtpSettings
from src.ui.ctp_probe import CtpProbeWindow, watch_seed
from src.trader.ctp_logic import PositionView
from src.trader.ctp_orders import (
    ctp_order,
    flatten_order_steps,
    prepare_live_ctp_cfg,
    prepare_self_test_cfg,
    reverse_order_steps,
)
from src.trader.ctp_trader import CtpTrader


def test_desk_watchlist_current_symbol_first() -> None:
    """第一行是当前交易合约，后面按关注列表顺序，重复项只留一次。"""
    cfg = {
        "trade": {"symbol": "lh2701"},
        "strategy": {"symbols": ["fu2701", "LH2701", "jd2612"]},
    }
    assert watch_seed(cfg) == ["lh2701", "fu2701", "jd2612"]
    assert watch_seed({"trade": {}, "strategy": {"symbols": []}}) == ["fu2701"]


def test_desk_keeps_resident_wait_and_auto_cancel() -> None:
    """交易台只换操作账、关掉模拟，不得改短等待或关掉超时撤单。"""
    cfg = prepare_live_ctp_cfg(
        {
            "dry_run": True,
            "trade": {
                "ctp": {
                    "account_kind": "sim",
                    "order_wait_sec": 8,
                    "auto_cancel_on_timeout": True,
                    "cancel_wait_sec": 3,
                }
            },
        },
        "sim",
    )
    assert cfg["dry_run"] is False
    ctp = cfg["trade"]["ctp"]
    assert ctp["account_kind"] == "sim"
    assert ctp["order_wait_sec"] == 8
    assert ctp["auto_cancel_on_timeout"] is True
    assert ctp["desk_session"] is True
    settings = CtpSettings.from_cfg(cfg)
    assert settings.order_wait_sec == 8
    assert settings.auto_cancel_on_timeout is True
    assert settings.desk_session is True
    assert settings.submit_auto_cancel() is False
    assert settings.submit_wait_sec() <= 1.5


def test_open_close_requests_match_resident_shape() -> None:
    opened = ctp_order("fu2701", "buy_open", 1, 2500.0, remark="策略开仓")
    closed = ctp_order("fu2701", "close", 1, 2490.0, position_side="long", remark="止损")
    assert opened.side == "buy_open"
    assert opened.price == 2500.0
    assert opened.position_side is None
    assert closed.side == "close"
    assert closed.position_side == "long"


def test_flatten_and_reverse_steps_cover_resident_actions() -> None:
    long_only = PositionView(long_today=2)
    assert flatten_order_steps(long_only) == [
        {"side": "close", "position_side": "long", "volume": 2}
    ]
    error, steps = reverse_order_steps(long_only)
    assert error == ""
    assert steps[0]["side"] == "close"
    assert steps[1]["side"] == "sell_open"
    locked = PositionView(long_today=1, short_today=1)
    assert len(flatten_order_steps(locked)) == 2
    err, empty = reverse_order_steps(locked)
    assert empty == []
    assert "锁仓" in err


def test_cancel_ref_is_on_ctp_trader() -> None:
    """撤选中必须走 CtpTrader，不能交易台自己调网关。"""
    assert hasattr(CtpTrader, "place_order")
    assert hasattr(CtpTrader, "cancel_ref")
    assert hasattr(CtpTrader, "cancel_all_working")
    assert hasattr(CtpTrader, "cancel_latest")


def test_self_test_market_uses_counter_depth_without_real_md() -> None:
    window = object.__new__(CtpProbeWindow)
    window.md = object()
    messages: list[str] = []
    loaded: list[object] = []
    window._say = messages.append
    window._load_price_band = loaded.append
    trader = SimpleNamespace(settings=SimpleNamespace(is_self_test=True))

    window._open_market(trader)

    assert window.md is None
    assert loaded == [trader]
    assert "不连接 CTP 行情前置" in messages[0]


def test_self_test_cfg_keeps_builtin_identity_and_desk_flags() -> None:
    cfg = {
        "dry_run": True,
        "trade": {
            "ctp": {
                "account_kind": "sim",
                "account_id": "acc_program_selftest",
                "ctp_profile": "program_selftest",
                "order_wait_sec": 8,
            }
        },
    }

    out = prepare_self_test_cfg(cfg)
    ctp = out["trade"]["ctp"]

    assert out["dry_run"] is False
    assert ctp["account_kind"] == "sim"
    assert ctp["desk_session"] is True
    assert ctp["account_id"] == "acc_program_selftest"
    assert ctp["ctp_profile"] == "program_selftest"
    assert ctp["order_wait_sec"] == 8


def test_desk_operation_account_offers_self_test_and_maps_to_sim_kind() -> None:
    from src.domain import trading_account as ta

    window = object.__new__(CtpProbeWindow)
    window.kind = SimpleNamespace(get=lambda: ta.SELF_TEST_LABEL)
    assert window._selected_self_test() is True
    assert window._selected_mode() == "self_test"
    assert window._selected_kind() == "sim"

    window.kind = SimpleNamespace(get=lambda: "真实账号")
    assert window._selected_self_test() is False
    assert window._selected_mode() == "live"

    trader = SimpleNamespace(settings=SimpleNamespace(is_self_test=True, account_kind="sim"))
    assert window._settings_mode(trader) == "self_test"
    assert window._trader_label(trader) == ta.SELF_TEST_LABEL
