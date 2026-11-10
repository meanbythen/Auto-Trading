"""交易台和常驻策略发出的 CTP 指令必须同一套代码。"""

from src.trader.ctp_gateway import CtpSettings
from src.trader.ctp_logic import PositionView
from src.trader.ctp_orders import (
    ctp_order,
    flatten_order_steps,
    prepare_live_ctp_cfg,
    reverse_order_steps,
)
from src.trader.ctp_trader import CtpTrader


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
