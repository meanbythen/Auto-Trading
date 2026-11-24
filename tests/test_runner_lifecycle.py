"""
runner.py 启动流程、退出收尾与收盘关机预约的白盒测试。

只替换外部依赖（交易通道、发件箱、防休眠、看门狗、关机工具），
主循环仍按真实分支执行；每轮扫描后立刻把 _running 置 False，避免死循环。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.kernel.commands import FLATTEN, SCAN, Command
from src.scheduler.runner import TradingBot


class _FakeKeepAwake:
    """记录 stop 是否被调用；需要时让 stop 抛错，验证退出路径继续。"""

    stop_raises = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        if _FakeKeepAwake.stop_raises:
            raise RuntimeError("keepalive stop boom")


class _FakeTrader:
    shutdown_raises = False

    def shutdown(self) -> None:
        if _FakeTrader.shutdown_raises:
            raise RuntimeError("trader shutdown boom")


class _FakeWatchdog:
    instances: list[_FakeWatchdog] = []
    stop_raises = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        if _FakeWatchdog.stop_raises:
            raise RuntimeError("watchdog stop boom")


class _FakeEvery:
    """只够 setup_schedule 用的 schedule.every() 替身。"""

    def __init__(self, record: list[tuple[str, Any]]) -> None:
        self.record = record

    @property
    def day(self) -> _FakeEvery:
        return self

    def at(self, text: str) -> _FakeEvery:
        self.record.append(("at", text))
        return self

    def do(self, func: Any) -> None:
        self.record.append(("do", func))


def _cfg(root: Path, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "dry_run": True,
        "trade": {"symbol": "bu2612", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(root),
    }
    cfg.update(overrides)
    return cfg


def _caps(**overrides: Any) -> SimpleNamespace:
    values = {
        "can_query_positions": True,
        "can_query_orders": True,
        "confirms_fill": True,
        "window_driven": False,
        "orders_via_broker": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _raise(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("boom")


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    caps: SimpleNamespace | None = None,
    runtime: dict[str, Any] | None = None,
    login_times: list[str] | None = None,
) -> tuple[TradingBot, dict[str, list[Any]]]:
    """构造一个只跑一圈的 bot：所有外部副作用都换成内存记录。"""
    schedule_cfg: dict[str, Any] = {
        "scan_interval_sec": 30,
        "trading_sessions": [["09:00", "15:00"]],
    }
    if login_times is not None:
        schedule_cfg["login_times"] = login_times
    bot = TradingBot(_cfg(tmp_path, schedule=schedule_cfg, runtime=runtime or {}))
    bot.trader = _FakeTrader()
    if caps is not None:
        monkeypatch.setattr(TradingBot, "trader_capabilities", property(lambda self: caps))

    rec: dict[str, list[Any]] = {
        "scan": [],
        "login": [],
        "ctp": [],
        "price": [],
        "audit": [],
    }
    monkeypatch.setattr(bot, "setup_schedule", lambda: None)
    monkeypatch.setattr(bot, "_check_guardian_actions", lambda: None)
    monkeypatch.setattr(bot, "_check_holiday_calendar", lambda: None)
    monkeypatch.setattr(bot, "_run_session_audit", lambda trigger="": rec["audit"].append(trigger))
    monkeypatch.setattr(bot, "_next_flatten_at", lambda now=None: None)
    monkeypatch.setattr(bot, "_any_flatten_due", lambda now=None: False)
    monkeypatch.setattr(bot, "_maybe_shutdown_after_close", lambda: None)
    monkeypatch.setattr(bot, "_start_watchdog", lambda: None)
    monkeypatch.setattr(bot, "_stop_watchdog", lambda: None)
    monkeypatch.setattr(bot, "_write_unprotected_flag", lambda reason: None)
    monkeypatch.setattr(bot, "_persist_state", lambda: None)
    monkeypatch.setattr(bot, "do_login", lambda: rec["login"].append(1))
    monkeypatch.setattr(bot, "_arm_ctp", lambda: rec["ctp"].append(1) or True)
    monkeypatch.setattr(bot, "_arm_order_price", lambda: rec["price"].append(1))

    monkeypatch.setattr("src.utils.keepalive.KeepAwake", _FakeKeepAwake)
    monkeypatch.setattr("src.login.prepare.startup_should_login", lambda cfg: False)
    monkeypatch.setattr("src.account.outbox.enqueue_order_stage", lambda root, row: None)
    monkeypatch.setattr("src.account.outbox.recover_pending", lambda root, apply: None)
    monkeypatch.setattr("src.account.outbox.start_dispatcher", lambda root, apply: None)
    monkeypatch.setattr("src.account.outbox.stop_dispatcher", lambda root: None)
    monkeypatch.setattr("src.account.signal_book.apply_outbox_event", lambda root, event: None)
    monkeypatch.setattr("src.trader.order_timeline.set_record_listener", lambda fn: None)
    monkeypatch.setattr("src.scheduler.runner.schedule", SimpleNamespace(run_pending=lambda: None))
    _FakeKeepAwake.stop_raises = False
    _FakeTrader.shutdown_raises = False

    def _scan() -> None:
        rec["scan"].append(1)
        bot._running = False

    monkeypatch.setattr(bot, "scan_once", _scan)
    return bot, rec


def _events(tmp_path: Path) -> str:
    return (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")


def test_startup_broker_channel_skips_gui_login(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps(orders_via_broker=True))

    bot.run_forever()

    assert rec["ctp"] == [1]
    assert rec["login"] == []
    assert rec["price"] == []
    assert bot._gui_price_ready is True
    assert bot.logged_in is False
    assert rec["audit"] == ["启动"]
    assert rec["scan"] == [1]
    assert bot._stopped.is_set()


def test_startup_logs_in_when_prepare_did_not(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr("src.login.prepare.startup_should_login", lambda cfg: True)

    bot.run_forever()

    assert rec["login"] == [1]
    assert rec["ctp"] == [1]
    assert rec["price"] == []


def test_startup_after_prepare_only_rearms_prices(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())

    bot.run_forever()

    assert rec["login"] == []
    assert rec["price"] == [1]
    assert rec["ctp"] == [1]
    assert bot.logged_in is True


def test_startup_flatten_clock_error_does_not_stop_scan(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr(
        bot, "_any_flatten_due", lambda now=None: (_ for _ in ()).throw(RuntimeError("clock"))
    )

    bot.run_forever()

    assert rec["scan"] == [1]
    assert bot._stopped.is_set()


def test_scan_command_triggers_immediate_scan(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    handled: list[str] = []
    monkeypatch.setattr(bot, "_handle_command", lambda command: handled.append(command.name))
    monkeypatch.setattr(bot.commands, "get", lambda timeout=0: Command(SCAN))

    bot.run_forever()

    assert handled == [SCAN]
    assert rec["scan"] == [1]


def test_flatten_command_skips_clock_flatten(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr(bot, "_next_flatten_at", lambda now=None: datetime.now())
    monkeypatch.setattr(bot, "_handle_command", lambda command: None)
    monkeypatch.setattr(bot.commands, "get", lambda timeout=0: Command(FLATTEN))
    flatten_calls: list[int] = []
    monkeypatch.setattr(bot, "flatten_if_due", lambda *a, **k: flatten_calls.append(1))

    bot.run_forever()

    assert rec["scan"] == [1]
    assert flatten_calls == []


def test_due_flatten_retries_until_pending_clears(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr(bot, "_next_flatten_at", lambda now=None: datetime.now())
    monkeypatch.setattr(bot, "flatten_if_due", lambda *a, **k: None)
    bot._flatten_pending = False

    bot.run_forever()

    assert rec["scan"] == [1]
    assert bot._flatten_pending is True


def test_due_flatten_resets_timer_when_done(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr(bot, "_next_flatten_at", lambda now=None: datetime.now())

    def _flatten(*args: Any, **kwargs: Any) -> None:
        bot._flatten_pending = False

    monkeypatch.setattr(bot, "flatten_if_due", _flatten)
    bot._flatten_pending = False

    bot.run_forever()

    assert rec["scan"] == [1]
    assert bot._flatten_pending is False


def test_loop_error_records_event_and_keeps_running(tmp_path, monkeypatch) -> None:
    bot, _ = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr(bot, "scan_once", _raise)
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        bot._running = False

    monkeypatch.setattr("src.scheduler.runner.time.sleep", _sleep)

    bot.run_forever()

    assert bot.last_error == "boom"
    assert sleeps == [5]
    assert '"loop_error"' in _events(tmp_path)


def test_keyboard_interrupt_exits_through_finally(tmp_path, monkeypatch) -> None:
    bot, _ = _prepare(monkeypatch, tmp_path, caps=_caps())

    def _interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(bot, "scan_once", _interrupt)

    bot.run_forever()  # 不应把 KeyboardInterrupt 抛给调用方

    assert bot._stopped.is_set()
    assert '"shutdown"' in _events(tmp_path)


def test_exit_cleanup_survives_every_failure(tmp_path, monkeypatch) -> None:
    bot, _ = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr(bot, "scan_once", lambda: setattr(bot, "_running", False))
    _FakeKeepAwake.stop_raises = True
    _FakeTrader.shutdown_raises = True
    monkeypatch.setattr(bot, "_stop_watchdog", _raise)
    monkeypatch.setattr(bot, "_write_unprotected_flag", _raise)
    monkeypatch.setattr("src.account.outbox.stop_dispatcher", _raise)
    monkeypatch.setattr(bot, "_persist_state", _raise)

    with pytest.raises(RuntimeError, match="boom"):
        bot.run_forever()

    assert bot._stopped.is_set()
    assert '"shutdown"' in _events(tmp_path)


def test_order_listener_enqueues_order_stage(tmp_path, monkeypatch) -> None:
    bot, _ = _prepare(monkeypatch, tmp_path, caps=_caps())
    staged: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "src.account.outbox.enqueue_order_stage", lambda root, row: staged.append(row)
    )
    monkeypatch.setattr(
        "src.trader.order_timeline.set_record_listener",
        lambda fn: captured.setdefault("fn", fn),
    )

    bot.run_forever()

    assert callable(captured["fn"])
    captured["fn"]({"order_id": "1"})
    assert staged == [{"order_id": "1"}]


def test_outbox_recover_failure_does_not_block_trading(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr("src.account.outbox.recover_pending", _raise)
    started: list[int] = []
    monkeypatch.setattr(
        "src.account.outbox.start_dispatcher", lambda root, apply: started.append(1)
    )

    bot.run_forever()

    assert rec["scan"] == [1]
    assert started == []  # 补账失败后不再启动消费线程，但扫描照跑


def test_setup_schedule_skips_registration_for_broker(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr(
        TradingBot, "trader_capabilities", property(lambda self: _caps(orders_via_broker=True))
    )
    monkeypatch.setattr(
        "src.scheduler.runner.schedule", SimpleNamespace(every=lambda: pytest.fail("不应注册"))
    )

    bot.setup_schedule()  # 不抛错即通过


def test_setup_schedule_registers_login_times(tmp_path, monkeypatch) -> None:
    bot = TradingBot(
        _cfg(
            tmp_path,
            schedule={"scan_interval_sec": 30, "login_times": ["08:50", "08:55"]},
        )
    )
    monkeypatch.setattr(
        TradingBot, "trader_capabilities", property(lambda self: _caps(orders_via_broker=False))
    )
    record: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "src.scheduler.runner.schedule", SimpleNamespace(every=lambda: _FakeEvery(record))
    )

    bot.setup_schedule()

    assert [item for action, item in record if action == "at"] == ["08:50", "08:55"]
    jobs = [item for action, item in record if action == "do"]
    assert len(jobs) == 2
    assert all(job.__self__ is bot for job in jobs)
    assert all(job.__func__.__name__ == "do_login" for job in jobs)


def test_auto_shutdown_disabled_returns_immediately(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr("src.utils.shutdown.ready_for_shutdown", pytest.fail)

    bot._maybe_shutdown_after_close()

    assert bot._shutdown_armed_date == ""


def test_auto_shutdown_arms_once_per_day(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path, runtime={"auto_shutdown_after_close": True}))
    bot._shutdown_armed_date = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr("src.utils.shutdown.ready_for_shutdown", pytest.fail)

    bot._maybe_shutdown_after_close()


def test_auto_shutdown_waits_while_positions_open(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path, runtime={"auto_shutdown_after_close": True}))
    monkeypatch.setattr(bot, "_multi_symbol_enabled", lambda: False)
    monkeypatch.setattr(bot, "_has_any_position", lambda: True)
    monkeypatch.setattr(
        "src.utils.shutdown.ready_for_shutdown",
        lambda **kwargs: (False, "还有未平仓位 1 手"),
    )
    monkeypatch.setattr("src.utils.shutdown.schedule_windows_shutdown", pytest.fail)

    bot._maybe_shutdown_after_close()

    assert bot._shutdown_armed_date == ""


def test_auto_shutdown_schedules_and_stops_loop(tmp_path, monkeypatch) -> None:
    bot = TradingBot(
        _cfg(
            tmp_path,
            runtime={"auto_shutdown_after_close": True, "shutdown_delay_sec": 60},
        )
    )
    monkeypatch.setattr(bot, "_multi_symbol_enabled", lambda: False)
    monkeypatch.setattr(bot, "_has_any_position", lambda: False)
    monkeypatch.setattr(bot.strategy, "flatten_times", [dtime(14, 55), "21:00"], raising=False)
    captured: dict[str, Any] = {}

    def _ready(**kwargs: Any) -> tuple[bool, str]:
        captured.update(kwargs)
        return True, "收盘后 5 分钟"

    monkeypatch.setattr("src.utils.shutdown.ready_for_shutdown", _ready)
    monkeypatch.setattr(
        "src.utils.shutdown.schedule_windows_shutdown", lambda delay: (True, "已预约")
    )
    bot._running = True

    bot._maybe_shutdown_after_close()

    assert captured["flatten_times"] == ["14:55", "21:00"]  # time 对象也转成 HH:MM
    assert captured["has_position"] is False
    assert bot._shutdown_armed_date == datetime.now().strftime("%Y-%m-%d")
    assert bot._running is False
    assert '"auto_shutdown_scheduled"' in _events(tmp_path)


def test_auto_shutdown_failure_allows_retry(tmp_path, monkeypatch) -> None:
    bot = TradingBot(
        _cfg(tmp_path, runtime={"auto_shutdown_after_close": True})
    )
    monkeypatch.setattr(bot, "_multi_symbol_enabled", lambda: False)
    monkeypatch.setattr(bot, "_has_any_position", lambda: False)
    monkeypatch.setattr(
        "src.utils.shutdown.ready_for_shutdown", lambda **kwargs: (True, "条件满足")
    )
    monkeypatch.setattr(
        "src.utils.shutdown.schedule_windows_shutdown", lambda delay: (False, "拒绝执行")
    )
    bot._running = True

    bot._maybe_shutdown_after_close()

    assert bot._shutdown_armed_date == ""
    assert bot.last_error == "拒绝执行"
    assert bot._running is True


def test_auto_shutdown_multi_symbol_merges_clocks_and_sessions(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path, runtime={"auto_shutdown_after_close": True}))
    bot.scan_symbols = ["fu2701", "lh2701"]
    monkeypatch.setattr(bot, "_multi_symbol_enabled", lambda: True)
    monkeypatch.setattr(bot, "_has_any_position", lambda: True)
    clocks = {"fu2701": ["14:55", "14:55"], "lh2701": ["21:00"]}
    sessions = {"fu2701": [["09:00", "15:00"]], "lh2701": [["21:00", "23:00"]]}
    monkeypatch.setattr(bot, "_symbol_flatten_clocks", lambda symbol: clocks[symbol])
    monkeypatch.setattr(bot, "_symbol_sessions_for", lambda symbol: sessions[symbol])
    captured: dict[str, Any] = {}

    def _ready(**kwargs: Any) -> tuple[bool, str]:
        captured.update(kwargs)
        return False, "还有未平仓位"

    monkeypatch.setattr("src.utils.shutdown.ready_for_shutdown", _ready)

    bot._maybe_shutdown_after_close()

    assert captured["flatten_times"] == ["14:55", "21:00"]  # 去重后按品种顺序
    assert captured["sessions"] == [["09:00", "15:00"], ["21:00", "23:00"]]
    assert captured["has_position"] is True


def test_due_flatten_retries_on_next_loop(tmp_path, monkeypatch) -> None:
    bot, rec = _prepare(monkeypatch, tmp_path, caps=_caps())
    clock = {"n": 0}

    def _next(now=None):
        clock["n"] += 1
        return datetime.now() if clock["n"] == 1 else None

    monkeypatch.setattr(bot, "_next_flatten_at", _next)
    bot._flatten_retry_sec = 0.0
    calls: list[bool] = []
    scans = {"n": 0}

    def _flatten(force: bool = False) -> None:
        calls.append(force)
        if len(calls) >= 2:
            bot._flatten_pending = False

    def _scan() -> None:
        rec["scan"].append(1)
        scans["n"] += 1
        if scans["n"] >= 2:
            bot._running = False

    monkeypatch.setattr(bot, "flatten_if_due", _flatten)
    monkeypatch.setattr(bot, "scan_once", _scan)
    bot._flatten_pending = False

    bot.run_forever()

    assert calls == [True, False]  # 第一轮开待办，第二轮走「没平掉继续重试」
    assert rec["scan"] == [1, 1]


def test_loop_error_survives_event_write_failure(tmp_path, monkeypatch) -> None:
    bot, _ = _prepare(monkeypatch, tmp_path, caps=_caps())
    monkeypatch.setattr(bot, "scan_once", _raise)
    original = bot._record_event

    def _event(name: str, **kwargs: Any) -> None:
        if name == "loop_error":
            raise RuntimeError("event disk boom")
        original(name, **kwargs)

    monkeypatch.setattr(bot, "_record_event", _event)
    monkeypatch.setattr(
        "src.scheduler.runner.time.sleep", lambda seconds: setattr(bot, "_running", False)
    )

    bot.run_forever()

    assert bot.last_error == "boom"
    assert bot._stopped.is_set()


def test_exit_without_trader_shutdown_method(tmp_path, monkeypatch) -> None:
    bot, _ = _prepare(monkeypatch, tmp_path, caps=_caps())
    bot.trader = object()

    bot.run_forever()

    assert bot._stopped.is_set()
    assert '"shutdown"' in _events(tmp_path)


def test_auto_shutdown_waits_for_other_reason(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path, runtime={"auto_shutdown_after_close": True}))
    monkeypatch.setattr(bot, "_multi_symbol_enabled", lambda: False)
    monkeypatch.setattr(bot, "_has_any_position", lambda: False)
    monkeypatch.setattr(
        "src.utils.shutdown.ready_for_shutdown", lambda **kwargs: (False, "还没到收盘时间")
    )
    monkeypatch.setattr("src.utils.shutdown.schedule_windows_shutdown", pytest.fail)

    bot._maybe_shutdown_after_close()

    assert bot._shutdown_armed_date == ""


def test_watchdog_start_skip_and_stop_failure(tmp_path, monkeypatch) -> None:
    _FakeWatchdog.instances.clear()
    _FakeWatchdog.stop_raises = False
    monkeypatch.setattr("src.ops.watchdog.TradingWatchdog", _FakeWatchdog)
    disabled = TradingBot(_cfg(tmp_path, runtime={"watchdog_enabled": False}))
    disabled._watchdog = None
    disabled._start_watchdog()
    assert _FakeWatchdog.instances == []

    bot = TradingBot(_cfg(tmp_path))
    bot._watchdog = SimpleNamespace(is_alive=lambda: True)
    bot._start_watchdog()
    assert _FakeWatchdog.instances == []  # 已有看门狗不重复建

    bot._watchdog = None
    bot._start_watchdog()
    created = _FakeWatchdog.instances[-1]
    assert created.started is True
    assert bot._watchdog is created

    bot._watchdog = None
    bot._stop_watchdog()  # 没有看门狗时直接返回
    _FakeWatchdog.stop_raises = True
    bot._watchdog = created
    bot._stop_watchdog()
    assert created.stopped is True
    assert bot._watchdog is None
