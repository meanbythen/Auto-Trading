"""P2：命令总线、信号事件与命令处理。"""

from __future__ import annotations

import time

from src.kernel.commands import Command, CommandBus, FLATTEN, SCAN, STOP
from src.kernel.events import SIGNAL
from src.scheduler.runner import TradingBot
from src.strategy import StrategyAction, StrategyDecision


def test_command_bus_keeps_fifo_order() -> None:
    bus = CommandBus()
    assert bus.post(SCAN)
    assert bus.post(FLATTEN, {"symbol": "ag2702"})

    first = bus.get(timeout=0)
    second = bus.get(timeout=0)

    assert first is not None and first.name == SCAN
    assert second is not None and second.name == FLATTEN
    assert second.data["symbol"] == "ag2702"
    assert bus.get(timeout=0) is None


def test_command_bus_timeout_and_full_queue() -> None:
    bus = CommandBus(maxsize=1)
    assert bus.post(STOP)
    assert bus.post(STOP) is False

    got = bus.get(timeout=0)
    assert got is not None and got.name == STOP

    start = time.monotonic()
    assert bus.get(timeout=0.05) is None
    assert time.monotonic() - start >= 0.04


def test_command_bus_drain_keeps_order() -> None:
    bus = CommandBus()
    for name in (SCAN, STOP, FLATTEN):
        bus.post(name)

    assert [item.name for item in bus.drain()] == [SCAN, STOP, FLATTEN]
    assert bus.drain() == []


def test_post_command_enqueues() -> None:
    bot = object.__new__(TradingBot)
    bot.commands = CommandBus()

    assert bot.post_command(SCAN, reason="manual")
    command = bot.commands.get(timeout=0)
    assert command is not None
    assert command.name == SCAN
    assert command.data["reason"] == "manual"


def test_handle_command_stop_and_flatten() -> None:
    bot = object.__new__(TradingBot)
    bot._stop_requested_at = None
    bot._running = True
    bot._flatten_pending = False
    seen: list[bool] = []
    bot.flatten_if_due = lambda force=False: seen.append(force)  # type: ignore[method-assign]

    TradingBot._handle_command(bot, Command(STOP))
    assert bot._running is False

    TradingBot._handle_command(bot, Command(FLATTEN))
    assert bot._flatten_pending is True
    assert seen == [True]


def test_journal_signal_publishes_signal_event(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "src.scheduler.runner.journal.record_signal",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    monkeypatch.setattr(
        "src.scheduler.runner.journal.record_audit",
        lambda *args, **kwargs: True,
    )
    bot = object.__new__(TradingBot)
    bot.cfg = {"_project_root": str(tmp_path)}
    bot.account_id = "acc_test_01"
    bot.strategy = type("_Strategy", (), {"name": "demo"})()
    from src.kernel.events import EventBus

    bot.events = EventBus()
    received = []
    bot.events.subscribe(SIGNAL, received.append)

    decision = StrategyDecision(
        action=StrategyAction.BUY_OPEN,
        symbol="ag2702",
        reason="测试信号",
        entry_price=10000.0,
        stop_price=9900.0,
        meta={},
    )
    trace_id = bot._journal_signal(decision, "2026-11-21 10:00:00")

    assert calls and calls[0]["symbol"] == "ag2702"
    assert len(received) == 1
    event = received[0]
    assert event.name == SIGNAL
    assert event.data["trace_id"] == trace_id
    assert event.data["account_id"] == "acc_test_01"
    assert event.data["strategy_id"] == "demo"
    assert event.data["action"] == "buy_open"
    assert event.data["price"] == 10000.0
    assert event.data["stop_price"] == 9900.0

    # 旁路订阅者报错不能影响信号本身。
    def _boom(_event) -> None:
        raise RuntimeError("boom")

    bot.events.subscribe(SIGNAL, _boom)
    assert bot._journal_signal(decision, "2026-11-21 10:05:00")
    assert len(received) == 2
