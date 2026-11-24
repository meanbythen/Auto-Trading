"""登录、窗口检查与本程序成交标记的单元测试（白盒，Mixin + Fake）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.scheduler import login_flow
from src.scheduler.login_flow import LoginFlowMixin


class _Bot(LoginFlowMixin):
    def __init__(self, **kwargs):
        self.cfg = kwargs.get("cfg", {})
        self.scan_symbols = kwargs.get("scan_symbols", ["bu2612"])
        self.trader = kwargs.get("trader")
        self.strategy = kwargs.get("strategy", SimpleNamespace(positions={}))
        self.trader_capabilities = kwargs.get("caps", SimpleNamespace())
        self.login_mgr = kwargs.get("login_mgr", SimpleNamespace(window_exists=lambda: True))
        self.logged_in = False
        self.last_error = ""
        self.gui_assume_fill = kwargs.get("gui_assume_fill", True)
        self.events = []

    def _record_event(self, name, **kwargs):
        self.events.append((name, kwargs))


def _caps(**kwargs):
    base = {
        "window_driven": False,
        "can_query_positions": False,
        "orders_via_broker": False,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_note_own_fill_initializes_and_normalizes_symbol(monkeypatch):
    monkeypatch.setattr(login_flow.time, "monotonic", lambda: 100.0)
    bot = _Bot()
    bot._note_own_fill(" BU2612 ")
    assert list(bot._own_fills_at) == ["bu2612"]
    assert bot._own_fills_at["bu2612"] == 100.0
    assert bot._own_fill_kind("bu2612") == "open"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("close", "close"), ("flat", "close"), ("平仓", "close"), ("open", "open"), ("", "open"), (None, "open")],
)
def test_note_own_fill_maps_close_kinds(kind, expected):
    bot = _Bot()
    bot._note_own_fill("bu2612", kind=kind)
    assert bot._own_fill_kind("bu2612") == expected


def test_own_fill_kind_defaults_to_open_for_unknown_symbol():
    bot = _Bot()
    assert bot._own_fill_kind("unknown") == "open"
    assert bot._own_fill_kind(None) == "open"


def test_own_fill_too_fresh_uses_monotonic_window(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(login_flow.time, "monotonic", lambda: clock["now"])
    bot = _Bot()
    assert bot._own_fill_too_fresh("bu2612") is False

    bot._note_own_fill("bu2612")
    assert bot._own_fill_too_fresh("bu2612") is True

    clock["now"] = 1090.0
    assert bot._own_fill_too_fresh("bu2612") is False

    clock["now"] = 1000.0
    assert bot._own_fill_too_fresh("bu2612", within=0) is False


def test_own_fill_context_handles_missing_and_raising_methods():
    bot = _Bot()
    bot._own_fill_too_fresh = None
    bot._own_fill_kind = None
    assert bot._own_fill_context("bu2612") == (False, "open")

    def boom(symbol):
        raise RuntimeError("boom")

    bot._own_fill_too_fresh = boom
    assert bot._own_fill_context("bu2612") == (False, "open")


def test_own_fill_context_returns_fresh_and_kind():
    bot = _Bot()
    bot._own_fill_too_fresh = lambda symbol: True
    bot._own_fill_kind = lambda symbol: "close"
    assert bot._own_fill_context("bu2612") == (True, "close")


def test_internal_nets_converts_long_short_and_flat():
    positions = {
        "bu2612": SimpleNamespace(side="long", volume=3),
        "lh2601": SimpleNamespace(side="short", volume=2),
        "sa2701": SimpleNamespace(side="flat", volume=9),
        "cu2601": SimpleNamespace(side="long"),
    }
    bot = _Bot(strategy=SimpleNamespace(positions=positions))
    assert bot._internal_nets() == {
        "bu2612": 3,
        "lh2601": -2,
        "sa2701": 0,
        "cu2601": 0,
    }


def test_uses_ctp_broker_funds_requires_capability_and_snapshot():
    bot = _Bot(caps=_caps(can_query_positions=False), trader=SimpleNamespace())
    assert bot._uses_ctp_broker_funds() is False

    bot = _Bot(
        caps=_caps(can_query_positions=True),
        trader=SimpleNamespace(last_broker_snapshot={"equity": 100000}),
    )
    assert bot._uses_ctp_broker_funds() is True

    bot = _Bot(
        caps=_caps(can_query_positions=True),
        trader=SimpleNamespace(last_broker_snapshot=None, last_broker_equity=0),
    )
    assert bot._uses_ctp_broker_funds() is True

    bot = _Bot(
        caps=_caps(can_query_positions=True),
        trader=SimpleNamespace(last_broker_snapshot=None, last_broker_equity=None),
    )
    assert bot._uses_ctp_broker_funds() is False


def test_ensure_runtime_ready_returns_true_for_non_window_and_dry_run():
    assert _Bot(caps=_caps(window_driven=False))._ensure_runtime_ready() is True
    assert _Bot(caps=_caps(window_driven=True), cfg={"dry_run": True})._ensure_runtime_ready() is True


def test_ensure_runtime_ready_detects_lost_window(monkeypatch):
    alerts = []
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: alerts.append((a, k)))
    bot = _Bot(
        caps=_caps(window_driven=True),
        cfg={"dry_run": False},
        login_mgr=SimpleNamespace(window_exists=lambda: False),
    )
    assert bot._ensure_runtime_ready() is False
    assert bot.logged_in is False
    assert bot.last_error == "交易软件窗口丢失"
    assert bot.events == [("window_lost", {})]
    assert alerts and alerts[0][0][0] == "window_lost"


def test_arm_order_price_marks_ready_for_non_gui_and_dry_run():
    bot = _Bot(caps=_caps(window_driven=False))
    bot._arm_order_price()
    assert bot._gui_price_ready is True

    bot = _Bot(caps=_caps(window_driven=True), cfg={"dry_run": True})
    bot._arm_order_price()
    assert bot._gui_price_ready is True


def test_arm_order_price_handles_success_and_failure(monkeypatch):
    results = {"ok": True, "msg": "done"}
    monkeypatch.setattr(
        "src.trader.price_mode.ensure_opponent_price",
        lambda cfg: (results["ok"], results["msg"]),
    )
    bot = _Bot(caps=_caps(window_driven=True), cfg={"dry_run": False})
    bot._arm_order_price()
    assert bot._gui_price_ready is True

    results["ok"] = False
    bot._arm_order_price()
    assert bot._gui_price_ready is False


def test_arm_order_price_swallows_exceptions(monkeypatch):
    def boom(cfg):
        raise RuntimeError("no window")

    monkeypatch.setattr("src.trader.price_mode.ensure_opponent_price", boom)
    bot = _Bot(caps=_caps(window_driven=True), cfg={"dry_run": False})
    bot._arm_order_price()
    assert bot._gui_price_ready is False


def test_arm_ctp_short_circuits_for_sim_and_missing_connect():
    assert _Bot(caps=_caps(orders_via_broker=False))._arm_ctp() is True

    bot = _Bot(caps=_caps(orders_via_broker=True), cfg={"dry_run": True}, trader=SimpleNamespace())
    assert bot._arm_ctp() is True
    assert bot.logged_in is True

    bot = _Bot(caps=_caps(orders_via_broker=True), cfg={"dry_run": False}, trader=SimpleNamespace())
    assert bot._arm_ctp() is True
    assert bot.logged_in is True


def test_arm_ctp_respects_retry_window(monkeypatch):
    monkeypatch.setattr("src.trader.go_live.strategy_order_block_reason", lambda cfg: "")
    monkeypatch.setattr("src.account.ctp_sync.resident_book_guard", lambda root, cfg: "")
    bot = _Bot(
        caps=_caps(orders_via_broker=True),
        cfg={"dry_run": False},
        trader=SimpleNamespace(connect=lambda: (True, "ok")),
    )
    bot.root = "."
    bot._ctp_retry_at = datetime.now() + timedelta(seconds=30)
    assert bot._arm_ctp() is False


def test_arm_ctp_connects_and_syncs_equity(monkeypatch):
    monkeypatch.setattr("src.trader.go_live.strategy_order_block_reason", lambda cfg: "")
    monkeypatch.setattr("src.account.ctp_sync.resident_book_guard", lambda root, cfg: "")
    bot = _Bot(
        caps=_caps(orders_via_broker=True),
        cfg={"dry_run": False},
        trader=SimpleNamespace(connect=lambda: (True, "ok")),
    )
    bot.root = "."
    synced = []
    bot._startup_equity_sync = lambda: synced.append(True)
    assert bot._arm_ctp() is True
    assert bot.logged_in is True
    assert bot._ctp_retry_at is None
    assert synced == [True]


def test_arm_ctp_failure_sets_retry_and_alerts(monkeypatch):
    monkeypatch.setattr("src.trader.go_live.strategy_order_block_reason", lambda cfg: "")
    monkeypatch.setattr("src.account.ctp_sync.resident_book_guard", lambda root, cfg: "")
    alerts = []
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: alerts.append((a, k)))
    bot = _Bot(
        caps=_caps(orders_via_broker=True),
        cfg={"dry_run": False},
        trader=SimpleNamespace(connect=lambda: (False, "refused")),
    )
    bot.root = "."
    assert bot._arm_ctp() is False
    assert bot.logged_in is False
    assert bot._ctp_retry_at is not None
    assert alerts and alerts[0][0][0] == "ctp_down"
