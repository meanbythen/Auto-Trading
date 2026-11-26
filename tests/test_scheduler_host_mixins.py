"""用轻量宿主补测 scheduler 各 Mixin 的分支行为。

重点覆盖下单编排、成交入账、登录/CTP、收盘清仓和委托意图这几块的
连续未覆盖区；断言都是返回值、调用次数或事件名，不做空断言。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ops import journal
from src.scheduler.order_state import OrderIntentWriteError
from src.strategy import StrategyAction, StrategyDecision
from src.trader.ctp_logic import symbol_key
from tests.fixtures.scheduler_host import (
    FakeCfg,
    FakeGateway,
    FakeKline,
    FakeResult,
    FakeStrategy,
    FakeTrader,
    LightHost,
    bars_frame,
    make_position,
)


@pytest.fixture(autouse=True)
def _side_channels(monkeypatch):
    """默认不写真实审计库、滑点文件或发真实告警；需要断言的用例再局部覆盖。"""
    monkeypatch.setattr(journal, "record_audit", lambda *a, **k: True)
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: None)
    monkeypatch.setattr("src.account.slippage_log.record_fill", lambda *a, **k: 0.5)


def _decision(
    action: StrategyAction,
    symbol: str = "bu2612",
    *,
    entry: float = 100.0,
    stop: float = 95.0,
    meta: dict | None = None,
    reason: str = "unit",
) -> StrategyDecision:
    return StrategyDecision(
        action,
        symbol,
        reason,
        entry_price=entry,
        stop_price=stop,
        meta=meta or {},
    )


# --- order_executor: 手工开平仓 -------------------------------------------------


def test_manual_open_writes_position_and_risk() -> None:
    host = LightHost(FakeCfg(dry_run=True))
    host.strategy.positions["bu2612"] = make_position()

    row, balance = host.manual_open("bu2612", "long", 2, 100.0, initial_stop=95.0)

    assert row == {
        "symbol": "bu2612",
        "side": "long",
        "volume": 2,
        "entry_price": 100.0,
    }
    assert balance is None
    pos = host.strategy.positions["bu2612"]
    assert (pos.side, pos.volume, pos.initial_stop) == ("long", 2, 95.0)
    assert pos.trail_armed is True
    assert ("on_order_filled", ("buy_open", 2), {"symbol": "bu2612"}) in host.risk.calls
    assert host.event_names() == ["manual_open"]
    assert host.persisted == 1


def test_manual_open_rejects_bad_params_and_unknown_symbol() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position()

    assert host.manual_open("bu2612", "flat", 2, 100.0) == (None, None)
    assert host.manual_open("bu2612", "long", 0, 100.0) == (None, None)
    assert host.manual_open("bu2612", "long", 1, 0.0) == (None, None)
    assert host.manual_open("fu2601", "long", 1, 100.0) == (None, None)
    assert host.events == []


def test_manual_close_books_balance_and_daily_pnl(monkeypatch, tmp_path: Path) -> None:
    host = LightHost({"dry_run": False}, root=tmp_path)
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=1, entry_price=90.0
    )
    monkeypatch.setattr(
        "src.account.signal_book.record_program_trade", lambda *a, **k: 101000.0
    )
    monkeypatch.setattr("src.account.signal_book.write_capital_to_config", lambda *a, **k: None)
    monkeypatch.setattr("src.account.signal_book.load_book", lambda root: {"trades": []})
    monkeypatch.setattr("src.account.signal_book.today_realized_pnl", lambda book: 12.5)

    row, balance = host.manual_close("bu2612", 110.0)

    assert row is not None and row["symbol"] == "bu2612"
    assert balance == 101000.0
    assert host.sizer.capital == 101000.0
    assert host.cfg["risk"]["capital"] == 101000.0
    assert host.risk.daily_pnl == 12.5
    assert ("set_daily_pnl", (12.5,), {}) in host.risk.calls
    assert host.strategy.positions["bu2612"].side == "flat"


def test_adopt_broker_position_ledger_reject_keeps_frozen(tmp_path: Path) -> None:
    host = LightHost({"dry_run": False}, root=tmp_path)
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=100.0
    )
    host._record_signal_account = lambda *a, **k: None  # type: ignore[method-assign]

    ok, message = host.adopt_broker_position("bu2612")

    assert ok is False
    assert "账本没记上" in message
    assert host._ledger_failed is True
    assert "ledger_block_position_update" in host.event_names()
    assert host.persisted == 0


def test_adopt_broker_position_success_releases_freeze() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=100.0
    )
    host._frozen_symbols.add("bu2612")
    host._reconcile_state = {"unclaimed": ["BU2612"]}
    host._record_signal_account = lambda *a, **k: 1234.0  # type: ignore[method-assign]

    ok, message = host.adopt_broker_position("bu2612")

    assert ok is True
    assert "已认领 bu2612 long 2 手" in message
    assert "bu2612" not in host._frozen_symbols
    assert host._reconcile_state["unclaimed"] == []
    assert "position_adopted" in host.event_names()
    assert host.persisted == 1


def test_close_broker_position_books_ghost_position() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=3, entry_price=100.0
    )
    host.kline = FakeKline(latest=105.0)

    ok, message = host.close_broker_position("bu2612")

    assert ok is True
    assert "补记平仓 3 手" in message
    assert host.strategy.positions["bu2612"].side == "flat"
    assert host.event_names() == ["manual_close", "position_broker_closed"]
    assert host.persisted == 2


def test_close_broker_position_rejects_missing_position_or_price() -> None:
    host = LightHost()
    assert host.close_broker_position("bu2612") == (False, "程序账上没有这个持仓")

    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=0.0
    )
    host.kline = FakeKline(latest=0.0)
    assert host.close_broker_position("bu2612") == (False, "拿不到价格，无法平账")


# --- order_executor: 手数与风控闸门 ---------------------------------------------


def test_size_open_applies_size_scale_and_dynamic_note(monkeypatch) -> None:
    host = LightHost()
    host.sizer._volume = 10
    monkeypatch.setattr(
        "src.risk.dynamic.live_equity_and_peak_or_error",
        lambda root: (1000.0, 1200.0, ""),
    )
    monkeypatch.setattr(
        "src.risk.dynamic.apply_dynamic_open",
        lambda cfg, vol, bars, *, equity=None, peak=None: (4, "动态减仓"),
    )

    vol, note = host._size_open(
        _decision(StrategyAction.BUY_OPEN, meta={"volume": 20, "size_scale": 0.4})
    )

    assert vol == 4
    assert "策略指定20手" in note
    assert "×0.40" in note
    assert "动态减仓" in note
    assert host._dynamic_note == "动态减仓"

    vol, note = host._size_open(
        _decision(StrategyAction.BUY_OPEN, meta={"volume": 20, "size_scale": -1})
    )
    assert vol == 0
    assert "→0手" in note


def test_size_open_invalid_scale_and_strategy_volume_fallbacks(monkeypatch) -> None:
    host = LightHost()
    host.sizer._volume = 3
    monkeypatch.setattr(
        "src.risk.dynamic.live_equity_and_peak_or_error",
        lambda root: (1000.0, 1000.0, ""),
    )
    monkeypatch.setattr(
        "src.risk.dynamic.apply_dynamic_open",
        lambda cfg, vol, bars, *, equity=None, peak=None: (vol, ""),
    )

    assert host._size_open(
        _decision(StrategyAction.BUY_OPEN, meta={"volume": "bad"})
    ) == (3, "sized（策略手数无效，按风控）")
    assert host._size_open(_decision(StrategyAction.BUY_OPEN, meta={"volume": 0})) == (
        0,
        "策略指定0手",
    )
    vol, _note = host._size_open(
        _decision(StrategyAction.BUY_OPEN, meta={"volume": 9, "size_scale": "bad"})
    )
    assert vol == 3


def test_size_open_stops_when_equity_unreadable(monkeypatch) -> None:
    host = LightHost()
    monkeypatch.setattr(
        "src.risk.dynamic.live_equity_and_peak_or_error",
        lambda root: (None, None, "读取账本权益失败: disk"),
    )

    vol, note = host._size_open(_decision(StrategyAction.BUY_OPEN))

    assert vol == 0
    assert note == "读取账本权益失败: disk"
    assert host._dynamic_note == note


@pytest.mark.parametrize(
    ("code", "action", "reason", "expected_event"),
    [
        ("order_paused", StrategyAction.BUY_OPEN, "暂停", "order_paused"),
        ("order_dedup", StrategyAction.BUY_OPEN, "重复", "order_dedup"),
        ("order_wait_next_bar", StrategyAction.BUY_OPEN, "等下一根", "order_wait_next_bar"),
        ("close_skip_no_position", StrategyAction.CLOSE, "无仓", "close_skip_no_position"),
        ("size_skip", StrategyAction.BUY_OPEN, "0手", "size_skip"),
        ("order_blocked_stage", StrategyAction.BUY_OPEN, "阶段", "order_blocked_stage"),
        ("order_working", StrategyAction.BUY_OPEN, "挂单", "order_working"),
        ("strategy_health", StrategyAction.BUY_OPEN, "健康", "strategy_health"),
        ("risk_block", StrategyAction.BUY_OPEN, "柜台对账未通过", "risk_block"),
        ("ledger_error_block", StrategyAction.BUY_OPEN, "账本", "ledger_error_block"),
        ("mystery", StrategyAction.BUY_OPEN, "未知", "mystery"),
    ],
)
def test_reject_gate_records_code_specific_event(
    code: str, action: StrategyAction, reason: str, expected_event: str
) -> None:
    host = LightHost()
    decision = _decision(action, meta={"bar_id": "b1", "volume": 3})

    host._reject_gate(decision, code, reason, "b1")

    assert host.event_names() == [expected_event]
    if code in {
        "order_paused",
        "order_working",
        "order_blocked_stage",
        "ledger_error_block",
        "risk_block",
    }:
        assert host.strategy.cleared_bars == [("bu2612", "b1")]
    else:
        assert host.strategy.cleared_bars == []
    if code == "ledger_error_block":
        assert host.last_error == reason


def test_execute_locked_reverse_closes_old_position_first() -> None:
    host = LightHost()
    pos = make_position(side="short", volume=2, entry_price=100.0)
    host.strategy.positions["bu2612"] = pos
    closed: list[StrategyDecision] = []

    def fake_execute(decision: StrategyDecision) -> bool:
        closed.append(decision)
        pos.side = "flat"
        pos.volume = 0
        return True

    host._execute = fake_execute  # type: ignore[method-assign]
    host._size_open = lambda decision: (0, "")  # type: ignore[method-assign]

    ok = host._execute_locked(
        _decision(
            StrategyAction.BUY_OPEN,
            entry=100.0,
            meta={"bar_id": "b1", "reverse": True},
        )
    )

    assert ok is False
    assert [item.action for item in closed] == [StrategyAction.CLOSE]
    assert "size_skip" in host.event_names()


def test_dispatch_ctp_without_price_releases_signal_mark() -> None:
    host = LightHost(trader=FakeTrader(caps="ctp"))
    host.strategy._pending = {"bu2612": {"bar_id": "b1"}}
    host._ctp_limit_price = lambda decision: None  # type: ignore[method-assign]

    ok = host._dispatch(_decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"}), 1, "buy_open", "", "b1")

    assert ok is False
    assert "order_no_price" in host.event_names()
    assert host.strategy.cleared_bars == [("bu2612", "b1")]
    assert host.strategy._pending == {}


def test_dispatch_ctp_slip_blocked_returns_false() -> None:
    host = LightHost(trader=FakeTrader(caps="ctp"))
    host._ctp_limit_price = lambda decision: 101.0  # type: ignore[method-assign]
    host._ctp_open_slip_blocked = lambda decision, price: True  # type: ignore[method-assign]

    ok = host._dispatch(
        _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"}), 1, "buy_open", "", "b1"
    )

    assert ok is False
    assert "order_no_price" not in host.event_names()
    assert host.strategy.cleared_bars == [("bu2612", "b1")]


def test_dispatch_passes_position_side_for_ctp_close() -> None:
    host = LightHost(trader=FakeTrader(caps="ctp"))
    host.strategy.positions["bu2612"] = make_position(side="short", volume=1, entry_price=100.0)
    host._ctp_limit_price = lambda decision: 99.0  # type: ignore[method-assign]
    calls: list[tuple] = []
    host._send_and_book = lambda *args: calls.append(args) or True  # type: ignore[method-assign]

    ok = host._dispatch(
        _decision(StrategyAction.CLOSE, meta={"bar_id": "b1"}), 1, "close", "", "b1"
    )

    assert ok is True
    assert calls[0][3] == "short"


def test_dispatch_sim_channel_prices_and_meta() -> None:
    host = LightHost(trader=FakeTrader(caps="sim"))
    calls: list[tuple] = []
    host._send_and_book = lambda *args: calls.append(args) or True  # type: ignore[method-assign]
    decision = _decision(StrategyAction.BUY_OPEN, entry=99.0, meta={"bar_id": "b1"})

    assert host._dispatch(decision, 1, "buy_open", "", "b1") is True
    assert calls[0][2] == 99.0
    assert decision.meta["price_mode"] == "limit"
    assert decision.meta["order_price"] == 99.0

    close = _decision(StrategyAction.CLOSE, entry=0.0, stop=95.0, meta={"bar_id": "b2"})
    assert host._dispatch(close, 1, "close", "", "b2") is True
    assert calls[1][2] is None
    assert close.meta["price_mode"] == "opponent"


# --- order_executor: 真正点单 ---------------------------------------------------


def test_send_and_book_ctp_marks_sent_bar(monkeypatch) -> None:
    result = FakeResult(
        ok=True,
        raw={"filled_volume": 1, "accounted_volume": 1, "filled": True, "fill_price": 100.0},
    )
    host = LightHost(FakeCfg(trade={"executor": "ctp"}), trader=FakeTrader(caps="ctp", result=result))
    monkeypatch.setattr(journal, "record_audit", host._record_audit)
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    ok = host._send_and_book(decision, 1, 100.0, None, "buy_open", "", "b1")

    assert ok is True
    assert host._sent_bar == {"bu2612": "b1"}
    assert len(host.trader.place_calls) == 1
    assert [item[0] for item in host.audits] == ["order_submitted", "order_accepted"]


def test_send_and_book_legacy_trader_uses_convenience_methods(monkeypatch) -> None:
    class _LegacyTrader:
        capabilities = FakeTrader(caps="sim").capabilities

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        def buy_open(self, symbol: str, volume: int, price: object = None) -> FakeResult:
            self.calls.append(("buy", symbol, volume))
            return FakeResult(ok=True)

        def sell_open(self, symbol: str, volume: int, price: object = None) -> FakeResult:
            self.calls.append(("sell", symbol, volume))
            return FakeResult(ok=True)

        def close_position(self, symbol: str, volume: int, price: object = None) -> FakeResult:
            self.calls.append(("close", symbol, volume))
            return FakeResult(ok=True)

    monkeypatch.setattr(journal, "record_audit", lambda *a, **k: True)
    for action, expected in (
        (StrategyAction.BUY_OPEN, "buy"),
        (StrategyAction.SELL_OPEN, "sell"),
        (StrategyAction.CLOSE, "close"),
    ):
        trader = _LegacyTrader()
        host = LightHost(trader=trader)  # type: ignore[arg-type]
        host.strategy.positions["bu2612"] = make_position(
            side="long", volume=1, entry_price=100.0
        )
        decision = _decision(action, entry=100.0, meta={"bar_id": "b1"})

        assert host._send_and_book(decision, 1, 100.0, "long", action.value, "", "b1") is True
        assert trader.calls == [(expected, "bu2612", 1)]


def test_send_and_book_skips_position_update_when_assume_fill_off(monkeypatch) -> None:
    result = FakeResult(ok=True, assumed_fill=True)
    host = LightHost(
        FakeCfg(trade={"executor": "gui"}),
        trader=FakeTrader(caps="gui", result=result),
        gui_assume_fill=False,
    )
    monkeypatch.setattr(journal, "record_audit", lambda *a, **k: True)
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    ok = host._send_and_book(decision, 1, None, None, "buy_open", "", "b1")

    assert ok is True
    assert host.event_names() == ["order_sent_unconfirmed"]
    assert host.strategy.positions.get("bu2612") is None


def test_consume_signal_bar_after_flat_uses_legacy_setters() -> None:
    host = LightHost()
    host.strategy.note_flat_wait_next_bar = None  # type: ignore[assignment]
    host.kline = FakeKline(frame=bars_frame())

    host._consume_signal_bar_after_flat("bu2612")

    assert len(host.strategy.signaled_bars) == 1
    symbol, bar_id = host.strategy.signaled_bars[0]
    assert symbol == "bu2612" and bar_id
    assert host.strategy.last_entry_bars["bu2612"] == bar_id


def test_release_signal_mark_without_bar_key_is_noop() -> None:
    host = LightHost()
    host._release_signal_mark(_decision(StrategyAction.BUY_OPEN, meta={}))
    assert host.strategy.cleared_bars == []


def test_book_external_close_handles_missing_position_and_price() -> None:
    host = LightHost()
    assert host._book_external_close("bu2612", "柜台已空") is False

    host.strategy.positions["bu2612"] = make_position(side="long", volume=1, entry_price=0.0)
    host.kline = FakeKline(latest=0.0)
    assert host._book_external_close("bu2612", "柜台已空") is False

    host.kline = FakeKline(latest=103.0)
    assert host._book_external_close("bu2612", "柜台已空") is True
    assert host.strategy.positions["bu2612"].side == "flat"


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("no_gateway", None),
        ("not_ready", None),
        ("missing_symbol", 0),
        ("net", 2),
        ("bad_net", None),
    ],
)
def test_broker_net_for_variants(setup: str, expected: int | None) -> None:
    host = LightHost()
    if setup == "no_gateway":
        pass
    elif setup == "not_ready":
        host.trader = FakeTrader(
            caps="ctp", gateway=FakeGateway(positions={}, positions_ready=False)
        )
    elif setup == "missing_symbol":
        host.trader = FakeTrader(caps="ctp", gateway=FakeGateway(positions={}))
    elif setup == "net":
        view = SimpleNamespace(net=2)
        host.trader = FakeTrader(
            caps="ctp", gateway=FakeGateway(positions={symbol_key("bu2612"): view})
        )
    else:
        view = SimpleNamespace(net="bad")
        host.trader = FakeTrader(
            caps="ctp", gateway=FakeGateway(positions={symbol_key("bu2612"): view})
        )

    assert host._broker_net_for("bu2612") == expected


def test_reject_and_ignore_reconcile_symbol() -> None:
    host = LightHost()
    host._reconcile_state = {
        "unclaimed": ["bu2612"],
        "phantom": ["BU2612", "fu2601"],
        "extra": [],
    }
    host._phantom_rounds = {"bu2612": 2}
    host._frozen_symbols = {"bu2612"}

    ok, message = host.reject_broker_lots("BU2612")

    assert ok is True
    assert "已拒绝把柜台 BU2612 收进程序" in message
    assert host._frozen_symbols == set()
    assert host._reconcile_state["phantom"] == ["fu2601"]
    assert host._phantom_rounds == {}
    assert host.event_names() == ["position_rejected"]

    ok, message = host.reject_broker_lots("fu2601")
    assert ok is True
    assert "仍暂停自动开仓" in message
    assert host.event_names()[-1] == "reconcile_rejected"

    ok, message = host.ignore_reconcile_symbol("fu2601")
    assert ok is True and message == "已忽略提示，品种仍保持冻结"
    assert host.event_names()[-1] == "reconcile_ignored"


# --- fill_reconcile: 成交入账 ---------------------------------------------------


def test_apply_fill_accounting_close_updates_strategy_risk_and_event() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=100.0
    )
    decision = _decision(StrategyAction.CLOSE, entry=100.0, meta={"bar_id": "b1"})

    ok = host._apply_fill_accounting("bu2612", decision, 2, 100.0, 110.0)

    assert ok is True
    names = [call[0] for call in host.strategy.calls]
    assert names == ["on_filled", "on_trade_closed", "note_factor_pnl", "set_last_entry_bar"]
    trade_closed = next(call for call in host.strategy.calls if call[0] == "on_trade_closed")
    assert trade_closed[1][1] == 200.0
    assert host.strategy.note_flat_calls == [("bu2612", "b1")]
    assert host.strategy.last_entry_bars["bu2612"] == "b1"
    assert ("update_pnl", (200.0,), {}) in host.risk.calls
    assert host.event_names() == ["pnl"]
    assert host.events[0][1]["pnl"] == 200.0
    assert host.persisted == 0


def test_apply_fill_accounting_swallows_strategy_hook_errors() -> None:
    host = LightHost(
        strategy=FakeStrategy(raise_trade_closed=True, raise_factor_pnl=True)
    )
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=1, entry_price=100.0
    )
    decision = _decision(StrategyAction.CLOSE, entry=100.0, meta={"bar_id": "b1"})

    assert host._apply_fill_accounting("bu2612", decision, 1, 100.0, 104.0) is True
    assert host.event_names() == ["pnl"]
    assert host.risk.daily_pnl == 40.0


def test_apply_fill_accounting_open_charges_commission() -> None:
    host = LightHost({"dry_run": True, "risk": {"contracts": {"bu": {"commission": 3.0}}}})
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    assert host._apply_fill_accounting("bu2612", decision, 2, 100.0, 100.0) is True
    assert host.risk.daily_pnl == -6.0
    assert host.event_names() == ["commission"]
    assert host.events[0][1]["fee"] == 6.0


def test_finish_ctp_order_reject_when_broker_already_flat() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=100.0
    )
    host.kline = FakeKline(latest=105.0)
    decision = _decision(StrategyAction.CLOSE, meta={"bar_id": "b1"})

    host._finish_ctp_order(
        decision, 2, FakeResult(ok=False, message="没有可平仓位"), "close", ""
    )

    assert host.strategy.positions["bu2612"].side == "flat"
    assert host.event_names() == ["manual_close"]


def test_finish_ctp_order_reject_flat_ledger_skips_alert() -> None:
    host = LightHost()
    decision = _decision(StrategyAction.CLOSE, meta={"bar_id": "b1"})

    host._finish_ctp_order(
        decision, 1, FakeResult(ok=False, message="平仓量超过持仓量"), "close", ""
    )

    assert host.events == []


def test_finish_ctp_order_unfilled_close_adds_chase_ticks(monkeypatch) -> None:
    host = LightHost()
    host._close_chase_ticks = None  # type: ignore[assignment]
    alerts: list[tuple] = []
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: alerts.append((a, k)))
    decision = _decision(StrategyAction.CLOSE, meta={"bar_id": "b1"})

    host._finish_ctp_order(
        decision, 1, FakeResult(ok=False, message="平仓未成交"), "close", ""
    )
    host._finish_ctp_order(
        decision, 1, FakeResult(ok=False, message="平仓未成交"), "close", ""
    )

    assert host._close_chase_ticks == {"bu2612": 4}
    assert all(name == "order_rejected" for name in host.event_names())
    assert [item[0][0] for item in alerts] == ["ctp_reject", "ctp_reject"]


def test_finish_ctp_order_fully_booked_returns_without_extra_events() -> None:
    host = LightHost()
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})
    result = FakeResult(
        ok=True,
        raw={"filled": True, "filled_volume": 1, "accounted_volume": 1, "fill_price": 100.0},
    )

    host._finish_ctp_order(decision, 1, result, "buy_open", "sized")

    assert host.events == []
    assert host._own_fill_kinds == {"bu2612": "open"}


def test_finish_ctp_order_books_leftover_from_order_rows() -> None:
    host = LightHost()
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})
    raw = {
        "filled_volume": 2,
        "accounted_volume": 0,
        "fill_price": 101.0,
        "orders": [
            {"order_ref": "r1", "filled": 2, "accounted_volume": 0, "avg_price": 101.0},
            {"order_ref": "", "filled": 2, "accounted_volume": 0, "avg_price": 101.0},
        ],
    }

    host._finish_ctp_order(decision, 2, FakeResult(ok=True, raw=raw), "buy_open", "sized")

    assert host.event_names() == ["order", "order_no_price", "order_partial"]
    assert host._own_fill_kinds == {"bu2612": "open"}


def test_finish_ctp_order_reports_unbooked_split_and_no_price() -> None:
    host = LightHost()
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    host._finish_ctp_order(
        decision,
        2,
        FakeResult(
            ok=True,
            order_id="a,b",
            raw={"filled_volume": 2, "accounted_volume": 0, "fill_price": 100.0},
        ),
        "buy_open",
        "sized",
    )
    assert host.event_names() == ["ctp_fill_unbooked", "order_partial"]

    host.events.clear()
    host._finish_ctp_order(
        decision,
        2,
        FakeResult(
            ok=True,
            order_id="r9",
            raw={"filled_volume": 2, "accounted_volume": 0, "fill_price": 0.0},
        ),
        "buy_open",
        "sized",
    )
    assert host.event_names() == ["order_no_price", "order_partial"]


def test_finish_ctp_order_working_and_unfilled_events() -> None:
    host = LightHost()
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    host._finish_ctp_order(
        decision, 3, FakeResult(ok=True, raw={"working": True}, order_id="r1"), "buy_open", ""
    )
    assert host.event_names() == ["order_working"]
    assert host.events[0][1]["volume"] == 3

    host.events.clear()
    host._finish_ctp_order(
        decision, 3, FakeResult(ok=True, raw={}, order_id="r2"), "buy_open", ""
    )
    assert host.event_names() == ["order_unfilled"]
    assert host.events[0][1]["order_id"] == "r2"


def test_finish_ctp_order_notes_close_fill_kind() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=1, entry_price=100.0
    )
    decision = _decision(StrategyAction.CLOSE, meta={"bar_id": "b1"})
    result = FakeResult(
        ok=True,
        raw={"filled": True, "filled_volume": 1, "accounted_volume": 1, "fill_price": 101.0},
    )

    host._finish_ctp_order(decision, 1, result, "close", "")

    assert host._own_fill_kinds == {"bu2612": "close"}


# --- login_flow: 柜台资金与启动对账 ---------------------------------------------


def test_apply_broker_equity_calls_runtime_and_swallows_failure(
    monkeypatch, tmp_path: Path
) -> None:
    host = LightHost(
        {"dry_run": False},
        trader=FakeTrader(caps="ctp", gateway=FakeGateway()),
        root=tmp_path,
    )
    calls: list[dict] = []

    def fake_apply(root, **kwargs):
        calls.append({"root": root, **kwargs})
        if len(calls) == 2:
            raise RuntimeError("ctp sync boom")

    monkeypatch.setattr("src.account.ctp_sync.apply_broker_runtime", fake_apply)

    host._apply_broker_equity()
    host._apply_broker_equity()

    assert len(calls) == 2
    assert calls[0]["root"] == tmp_path
    assert calls[0]["sizer"] is host.sizer
    assert calls[0]["risk"] is host.risk
    assert calls[0]["trader"] is host.trader


def test_apply_broker_equity_without_gateway_is_noop(monkeypatch) -> None:
    host = LightHost()
    called: list[bool] = []
    monkeypatch.setattr(
        "src.account.ctp_sync.apply_broker_runtime",
        lambda *a, **k: called.append(True),
    )

    host._apply_broker_equity()

    assert called == []


def test_arm_ctp_book_guard_blocks_and_alerts(monkeypatch, tmp_path: Path) -> None:
    host = LightHost({"dry_run": False}, trader=FakeTrader(caps="ctp"), root=tmp_path)
    alerts: list[tuple] = []
    monkeypatch.setattr("src.trader.go_live.strategy_order_block_reason", lambda cfg: "")
    monkeypatch.setattr(
        "src.account.ctp_sync.resident_book_guard", lambda root, cfg: "常驻账本不一致"
    )
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: alerts.append((a, k)))

    ok = host._arm_ctp()

    assert ok is False
    assert host.logged_in is False
    assert host._ctp_retry_at is not None
    assert alerts == [
        (("ctp_book_mismatch", "常驻账本不一致"), {"level": "error", "root": tmp_path})
    ]


def test_startup_equity_sync_applies_report(monkeypatch, tmp_path: Path) -> None:
    report = SimpleNamespace(ok=True, diffs=["资金已按柜台同步"])
    host = LightHost(
        {"dry_run": False},
        trader=FakeTrader(caps="ctp", gateway=FakeGateway(), reconcile_report=report),
        root=tmp_path,
    )
    monkeypatch.setattr("src.account.ctp_sync.apply_broker_runtime", lambda *a, **k: None)
    host._startup_reconcile_pending = True

    host._startup_equity_sync()

    assert host._applied_reconcile == [(report, "startup")]
    assert host._startup_reconcile_pending is False
    assert host.trader.reconcile_calls == 1


def test_startup_equity_sync_marks_unknown_on_error() -> None:
    host = LightHost({"dry_run": False}, trader=FakeTrader(caps="ctp"))
    host._startup_reconcile_pending = True

    def boom() -> None:
        raise RuntimeError("rpc down")

    host._call_reconcile = boom  # type: ignore[method-assign]

    host._startup_equity_sync()

    assert host.trader.reconcile_unknown == ["启动对账异常：rpc down"]
    assert host._startup_reconcile_pending is True


# --- flatten_manager: 按时钟清仓 -------------------------------------------------


def test_flatten_force_clears_sent_and_stale_tries() -> None:
    host = LightHost(strategy=FakeStrategy(symbols=["bu2612"]), multi_enabled=True)
    fresh = time.monotonic()
    host._flatten_sent = {"bu2612"}
    host._flatten_attempts = {"bu2612": 3}
    host._flatten_try = {"fu2601": fresh, "ag2612": 0.0}
    host._flatten_pending = True

    host.flatten_if_due(force=True, now=datetime(2026, 11, 24, 14, 0))

    assert host._flatten_sent == set()
    assert host._flatten_attempts == {}
    assert host._flatten_try == {"fu2601": fresh}
    assert host._flatten_pending is False


def test_flatten_swallows_strategy_clock_error() -> None:
    host = LightHost(strategy=FakeStrategy(raise_clock=True))
    host._flatten_try = {"bu2612": time.monotonic()}

    host.flatten_if_due(now=datetime(2026, 11, 24, 14, 0))

    assert host._flatten_try == {}
    assert host._flatten_pending is False


def test_flatten_multi_skips_symbols_outside_session() -> None:
    host = LightHost(multi_enabled=True)
    stamp = time.monotonic()
    host._flatten_pending = True
    host._flatten_try = {"bu2612": stamp}
    host._any_symbol_in_session = lambda symbols, now=None: False  # type: ignore[method-assign]

    host.flatten_if_due(now=datetime(2026, 11, 24, 14, 0))

    assert host._flatten_try == {"bu2612": stamp}
    assert host._flatten_pending is True
    assert host.events == []


def test_flatten_marks_pending_when_execute_raises() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=100.0
    )

    def boom(decision: StrategyDecision) -> bool:
        raise RuntimeError("gui 点不动")

    host._execute = boom  # type: ignore[method-assign]

    host.flatten_if_due(now=datetime(2026, 11, 24, 14, 0))

    assert host._flatten_attempts == {"bu2612": 1}
    assert host._flatten_pending is True
    assert host._flatten_try["bu2612"] > 0


def test_flatten_records_unconfirmed_send() -> None:
    host = LightHost(gui_assume_fill=False)
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=100.0
    )
    host._execute = lambda decision: True  # type: ignore[method-assign]

    host.flatten_if_due(now=datetime(2026, 11, 24, 14, 0))

    assert host._flatten_sent == {"bu2612"}
    assert host.event_names() == ["flatten_sent_unconfirmed"]
    assert host.events[0][1]["attempts"] == 1


def test_flatten_gives_up_after_retry_limit() -> None:
    host = LightHost()
    host.strategy.positions["bu2612"] = make_position(
        side="long", volume=2, entry_price=100.0
    )
    host.strategy.is_flatten_time = lambda when=None: False  # type: ignore[method-assign]
    host._flatten_pending = True
    host._flatten_attempts = {"bu2612": 29}
    host._execute = lambda decision: True  # type: ignore[method-assign]

    host.flatten_if_due(now=datetime(2026, 11, 24, 14, 0))

    assert host._flatten_attempts == {"bu2612": 30}
    assert host._flatten_sent == {"bu2612"}
    assert host.event_names() == ["flatten_give_up"]
    assert host.events[0][1]["attempts"] == 30
    assert host.last_error == "收盘清仓多次未确认，已停止自动重试"


def test_next_flatten_at_skips_invalid_clock() -> None:
    host = LightHost(strategy=FakeStrategy(flatten_at_close=True, flatten_times=["bad"]))

    assert host._next_flatten_at(datetime(2026, 11, 24, 14, 0)) is None


# --- order_state: 时间线与委托意图 ---------------------------------------------


class _Timeline:
    def __init__(self, *, boom: bool = False) -> None:
        self.rows: list[tuple[str, dict]] = []
        self.boom = boom

    def record(self, stage: str, **fields) -> None:
        if self.boom:
            raise RuntimeError("timeline disk full")
        self.rows.append((stage, fields))


def test_timeline_trace_uses_stop_price_for_close() -> None:
    host = LightHost()
    decision = _decision(StrategyAction.CLOSE, stop=95.0, meta={})

    trace_id = host._timeline_trace(decision)

    assert trace_id
    assert decision.meta["trace_id"] == trace_id
    assert decision.meta["theory_price"] == 95.0
    assert "decided_at" in decision.meta


def test_record_order_timeline_swallows_recorder_error() -> None:
    host = LightHost(order_timeline=_Timeline(boom=True))
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    host._record_order_timeline("order_sent", decision, volume=1)

    assert decision.meta["theory_price"] == 100.0


def test_start_decision_timeline_records_three_stages() -> None:
    timeline = _Timeline()
    host = LightHost(order_timeline=timeline)
    decision = _decision(
        StrategyAction.BUY_OPEN,
        meta={
            "volume": "bad",
            "bar_id": "b1",
            "market_health": {"latest_bar_time": "09:40", "source": "ctp", "period": "5"},
        },
    )

    host._start_decision_timeline(decision)

    assert [row[0] for row in timeline.rows] == [
        "market_snapshot",
        "bar_closed",
        "strategy_decision",
    ]
    assert timeline.rows[0][1]["volume"] == 0
    assert timeline.rows[0][1]["source"] == "ctp"
    assert timeline.rows[2][1]["reason"] == "unit"


def test_write_order_intent_refuses_to_overwrite_pending(tmp_path: Path) -> None:
    folder = tmp_path / "intents"
    folder.mkdir()
    (folder / "old.json").write_text("{}", encoding="utf-8")
    host = LightHost(intent_dir=folder)
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    with pytest.raises(OrderIntentWriteError) as excinfo:
        host._write_order_intent(decision, 1)

    assert "old.json" in str(excinfo.value)


def test_write_order_intent_raises_when_target_unwritable(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    host = LightHost(intent_dir=blocker / "sub")
    decision = _decision(StrategyAction.BUY_OPEN, meta={"bar_id": "b1"})

    with pytest.raises(OrderIntentWriteError) as excinfo:
        host._write_order_intent(decision, 1)

    assert "写委托意图失败" in str(excinfo.value)


def test_clear_order_intent_swallows_unlink_errors() -> None:
    class _BadPath:
        name = "bad.json"

        def unlink(self, missing_ok: bool = False) -> None:
            raise OSError("locked by another process")

    host = LightHost()
    host._pending_intent = {"symbol": "bu2612"}
    host._pending_intents = [{"symbol": "bu2612"}]
    host._pending_intent_files = [_BadPath()]
    host.intent_path = _BadPath()

    host._clear_order_intent()

    assert host._pending_intent is None
    assert host._pending_intents == []
    assert host._pending_intent_files == []


def test_check_pending_intent_read_error_halts_gui(monkeypatch, tmp_path: Path) -> None:
    folder = tmp_path / "intents"
    folder.mkdir()
    (folder / "bad.json").write_text("{not json", encoding="utf-8")
    host = LightHost(
        {"dry_run": False, "trade": {"executor": "gui"}}, intent_dir=folder
    )

    host._check_pending_intent()

    assert host._pending_intents == [{}]
    assert host.risk.halted is True
    assert host.risk.halt_kind == "pending_intent"
    assert host.last_error.startswith(host._INTENT_HALT_PREFIX)
    assert host.event_names() == ["pending_intent_found"]


def test_halt_on_pending_intent_counts_all_and_survives_alert_error(monkeypatch) -> None:
    host = LightHost()
    host._pending_intents = [
        {"symbol": "bu2612", "side": "buy_open", "volume": 2, "ts": "2026-11-24T10:00:00"},
        {"symbol": "fu2601", "side": "sell_open", "volume": 1, "ts": "2026-11-24T11:00:00"},
    ]

    def boom(*args, **kwargs):
        raise RuntimeError("alert channel down")

    monkeypatch.setattr("src.ops.alerts.emit_alert", boom)

    host._halt_on_pending_intent()

    assert "共 2 笔未核对委托" in host.last_error
    assert host.risk.halt_kind == "pending_intent"
    assert host.risk.halt_reason == host.last_error
    assert host.events[0][1]["symbol"] == "fu2601"


def test_pending_intent_matches_open_and_close() -> None:
    host = LightHost()
    host._pending_intents = [
        {"symbol": "BU2612", "side": "buy_open", "volume": 2},
        {"symbol": "bu2612", "side": "close", "volume": 9},
        {"symbol": "bu2612", "side": "buy_open", "volume": "x"},
        {},
        None,
    ]

    assert host.pending_intent_matches("bu2612", "buy_open", 2) == {
        "symbol": "BU2612",
        "side": "buy_open",
        "volume": 2,
    }
    assert host.pending_intent_matches("bu2612", "buy_open", 3) is None
    assert host.pending_intent_matches("bu2612", "close", 99) == {
        "symbol": "bu2612",
        "side": "close",
        "volume": 9,
    }
    assert host.pending_intent_matches("bu2612", "sell_open", 1) is None

    host._pending_intents = None
    host._pending_intent = {"symbol": "bu2612", "side": "buy_open", "volume": 1}
    assert host.pending_intent_matches("bu2612", "buy_open", 1) is not None


def test_confirm_positions_reviewed_clears_manual_halt() -> None:
    host = LightHost()
    host.risk.halted = True
    host.risk.halt_kind = "risk_sync_failed"
    host.set_orders_paused = lambda paused: (_ for _ in ()).throw(RuntimeError("gui off"))  # type: ignore[method-assign]

    ok, message = host.confirm_positions_reviewed()

    assert ok is True
    assert message == "已解除熔断，可以继续扫描。"
    assert host.risk.halted is False
    assert host.persisted == 1
    assert host.events == [("position_review_confirmed", {"halt_kind": "risk_sync_failed"})]

    ok, message = host.confirm_positions_reviewed()
    assert (ok, message) == (False, "没有待核对的委托记录。")


def test_confirm_positions_reviewed_clears_pending_intent() -> None:
    host = LightHost()
    host._pending_intent = {"symbol": "bu2612", "side": "buy_open", "volume": 2}
    host._pending_intent_files = []
    host.risk.halted = True
    host.risk.halt_kind = "order_dispatch_unknown"
    host.set_orders_paused = lambda paused: (_ for _ in ()).throw(RuntimeError("gui off"))  # type: ignore[method-assign]

    ok, message = host.confirm_positions_reviewed()

    assert ok is True
    assert host._pending_intent is None
    assert host.risk.halted is False
    assert host.events == [
        (
            "position_review_confirmed",
            {"symbol": "bu2612", "side": "buy_open", "volume": 2},
        )
    ]


def test_confirm_positions_reviewed_matches_halt_reason_prefix() -> None:
    host = LightHost()
    host._pending_intent = {"symbol": "fu2601", "side": "sell_open", "volume": 1}
    host._pending_intent_files = []
    host.risk.halted = True
    host.risk.halt_kind = "other"
    host.risk.halt_reason = host._INTENT_HALT_PREFIX + "：fu2601"
    host.set_orders_paused = lambda paused: None  # type: ignore[method-assign]

    ok, _message = host.confirm_positions_reviewed()

    assert ok is True
    assert host.risk.halted is False
    assert host.events[0][1]["symbol"] == "fu2601"
