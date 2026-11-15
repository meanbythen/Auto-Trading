"""关窗停后台：要等主循环真正退出并把状态落盘，不能直接强杀 daemon 线程。"""

import threading
import time

from src.scheduler.runner import TradingBot
from src.ui.bot_worker import BotWorker
from src.utils.state_store import default_state_path, load_state


class _FakeBot:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeTrader:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeKeepAwake:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def test_stop_waits_for_thread_and_returns_true() -> None:
    worker = BotWorker()
    worker.bot = _FakeBot()
    done: list[bool] = []
    worker._thread = threading.Thread(
        target=lambda: (time.sleep(0.05), done.append(True)),
        name="FakeWorkerDone",
        daemon=True,
    )
    worker._thread.start()

    assert worker.stop(wait=True, timeout=2.0) is True
    assert done == [True]
    assert worker.bot.stop_calls == 1


def test_stop_returns_false_when_thread_outlives_timeout() -> None:
    worker = BotWorker()
    worker.bot = _FakeBot()
    worker._thread = threading.Thread(
        target=lambda: time.sleep(5),
        name="FakeWorkerSlow",
        daemon=True,
    )
    worker._thread.start()
    try:
        assert worker.stop(wait=True, timeout=0.2) is False
    finally:
        worker._thread.join(6.0)


def test_stop_from_inside_worker_thread_returns_true() -> None:
    worker = BotWorker()
    worker.bot = _FakeBot()
    results: list[bool] = []
    worker._thread = threading.Thread(
        target=lambda: results.append(worker.stop(wait=True, timeout=2.0)),
        name="FakeWorkerSelfStop",
        daemon=True,
    )
    worker._thread.start()
    worker._thread.join(2.0)

    assert not worker._thread.is_alive()
    assert results == [True]


def test_stop_without_bot_returns_true() -> None:
    worker = BotWorker()
    assert worker.stop(wait=True, timeout=0.1) is True


def _cfg(root) -> dict:
    """一套最小的配置，构造 bot 时会自动读状态并跟账本对一次。"""
    return {
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


def test_run_forever_sets_stopped_after_shutdown_and_persist(tmp_path, monkeypatch) -> None:
    bot = TradingBot(_cfg(tmp_path))
    trader = _FakeTrader()
    bot.trader = trader

    monkeypatch.setattr(bot, "scan_once", lambda *a, **k: None)
    monkeypatch.setattr(bot, "setup_schedule", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_run_session_audit", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_next_flatten_at", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_arm_ctp", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_arm_order_price", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_maybe_shutdown_after_close", lambda *a, **k: None)
    # 没到清仓钟点：停机路径只走 finally，不触发真实平仓。
    monkeypatch.setattr(bot.strategy, "is_flatten_time", lambda *a, **k: False)
    monkeypatch.setattr("src.login.prepare.startup_should_login", lambda *a, **k: False)
    monkeypatch.setattr("src.utils.keepalive.KeepAwake", _FakeKeepAwake)

    thread = threading.Thread(target=bot.run_forever, name="FakeTradingBot", daemon=True)
    thread.start()
    time.sleep(0.2)
    bot.stop()

    try:
        assert bot.wait_stopped(5.0) is True
    finally:
        thread.join(5.0)
    assert trader.shutdown_calls == 1
    assert load_state(default_state_path(tmp_path))["saved_at"]
