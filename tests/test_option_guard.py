"""期权实盘隔离开关：默认关闭，真实账号只平不开。"""

from src.account.option_guard import (
    is_live_kind,
    is_open_side,
    option_live_block_reason,
    option_live_enabled,
)


def _cfg(enabled: bool = False) -> dict:
    return {"trade": {"option_live_enabled": enabled}}


def test_default_is_off() -> None:
    assert option_live_enabled({}) is False
    assert option_live_enabled(None) is False
    assert option_live_enabled(_cfg()) is False
    assert option_live_enabled(_cfg(True)) is True


def test_live_account_blocks_option_open() -> None:
    reason = option_live_block_reason(_cfg(), "rb2601C3500", account_kind="live")
    assert reason and "option_live_enabled=false" in reason
    assert option_live_block_reason(_cfg(), "rb2601C3500", is_live=True)


def test_sim_and_futures_and_dry_run_are_allowed() -> None:
    # 仿真账号不拦
    assert option_live_block_reason(_cfg(), "rb2601C3500", account_kind="sim") is None
    # 期货不拦
    assert option_live_block_reason(_cfg(), "rb2601", account_kind="live") is None
    # dry_run 不下真单，不拦
    assert (
        option_live_block_reason(
            _cfg(), "rb2601C3500", account_kind="live", dry_run=True
        )
        is None
    )


def test_switch_on_allows_live_option() -> None:
    assert option_live_block_reason(_cfg(True), "rb2601C3500", account_kind="live") is None


def test_side_helpers() -> None:
    assert is_live_kind("live")
    assert is_live_kind("真实账号")
    assert not is_live_kind("sim")
    assert is_open_side("buy_open")
    assert is_open_side("sell_open")
    assert not is_open_side("close")


def test_executor_blocks_option_open_on_live(tmp_path) -> None:
    """策略开期权仓时，真实账号 + 开关关闭必须在算仓前拦住。"""
    from types import SimpleNamespace

    from src.risk.sizing import PositionSizer
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = {
        "trade": {"option_live_enabled": False},
        "risk": {
            "capital": 100000,
            "contracts": {
                "rb2601C3500": {
                    "multiplier": 10,
                    "margin_rate_pct": 12,
                    "expiry": "2027-01-15",
                    "strike": 3500,
                }
            },
        },
    }
    host = SimpleNamespace(
        root=tmp_path,
        cfg=cfg,
        sizer=PositionSizer(cfg),
        risk=SimpleNamespace(total_abs_position=lambda: 0, positions={}),
        _option_account_is_live=lambda: True,
    )
    decision = SimpleNamespace(
        symbol="rb2601C3500", entry_price=100.0, stop_price=90.0, meta={}
    )
    volume, reason = OrderExecutorMixin._size_open(host, decision)
    assert volume == 0
    assert "option_live_enabled=false" in reason


def test_executor_allows_option_open_after_switch_on(tmp_path) -> None:
    """打开开关后，真实账号的期权开仓不再被隔离层拦住。"""
    from types import SimpleNamespace

    from src.risk.sizing import PositionSizer
    from src.scheduler.order_executor import OrderExecutorMixin

    cfg = {
        "trade": {"option_live_enabled": True},
        "risk": {
            "capital": 100000,
            "contracts": {
                "rb2601C3500": {
                    "multiplier": 10,
                    "margin_rate_pct": 12,
                    "expiry": "2027-01-15",
                    "strike": 3500,
                }
            },
        },
    }
    host = SimpleNamespace(
        root=tmp_path,
        cfg=cfg,
        sizer=PositionSizer(cfg),
        risk=SimpleNamespace(total_abs_position=lambda: 0, positions={}),
        _option_account_is_live=lambda: True,
    )
    decision = SimpleNamespace(
        symbol="rb2601C3500", entry_price=100.0, stop_price=90.0, meta={}
    )
    _volume, reason = OrderExecutorMixin._size_open(host, decision)
    assert "option_live_enabled=false" not in reason
