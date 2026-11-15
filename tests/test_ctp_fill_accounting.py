"""CTP 成交回报入账：回调加锁、去重、失败重试，以及不跟主线程死锁。"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

import pytest

from src.account import signal_book
from src.account.signal_book import load_book
from src.scheduler.runner import TradingBot
from src.trader.ctp_gateway import CtpGateway, CtpSettings
from src.trader.ctp_logic import TrackedOrder


def _cfg(root) -> dict:
    """最小配置。dry_run=False 才会真的写信号账户。"""
    return {
        "dry_run": False,
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


def _report(order_ref: str = "1001") -> dict:
    return {
        "order_ref": order_ref,
        "symbol": "bu2612",
        "direction": "0",
        "offset": "0",
        "filled": 1,
        "accounted_volume": 0,
        "avg_price": 3500,
    }


def _events(root) -> str:
    path = root / "data" / "events.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _volume(bot: TradingBot, symbol: str = "bu2612") -> int:
    return int(getattr(bot._position_for(symbol), "volume", 0) or 0)


@pytest.fixture(autouse=True)
def _no_capital_write(monkeypatch):
    """记账成功会写真实配置文件，测试里一律挡住。"""
    monkeypatch.setattr("src.account.signal_book.write_capital_to_config", lambda balance: None)


def test_concurrent_same_fill_is_booked_once(tmp_path):
    """两个线程同时处理同一份回报：都要成功返回，但账本和仓位只加一次。"""
    bot = TradingBot(_cfg(tmp_path))
    bot._recent_fill_keys = OrderedDict()
    report = _report()
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        results.append(bot._on_ctp_fill(report))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert results == [True, True]
    book = load_book(tmp_path)
    rows = [row for row in book["trades"] if str(row.get("fill_key") or "") == "ctp:1001:1"]
    assert len(rows) == 1
    assert _volume(bot) == 1


def test_ledger_failure_returns_false_then_retries(tmp_path, monkeypatch):
    """账本写失败时返回 False、仓位不动；恢复后同一份回报还能正常入账。"""
    bot = TradingBot(_cfg(tmp_path))
    bot._recent_fill_keys = OrderedDict()
    report = _report("2001")
    state = {"fail": True}
    real = signal_book.record_program_trade

    def flaky(*args, **kwargs):
        if state["fail"]:
            raise RuntimeError("账本写盘失败")
        return real(*args, **kwargs)

    monkeypatch.setattr(signal_book, "record_program_trade", flaky)

    assert bot._on_ctp_fill(report) is False
    assert _volume(bot) == 0

    state["fail"] = False
    assert bot._on_ctp_fill(report) is True
    assert _volume(bot) == 1


def test_fill_lock_timeout_returns_false(tmp_path):
    """等交易锁超时：不记账，返回 False，并留下 fill_lock_timeout 事件。"""
    bot = TradingBot(_cfg(tmp_path))

    class _NeverLock:
        def acquire(self, timeout=None):
            return False

        def release(self):
            return None

    real_lock = bot._trade_lock
    bot._trade_lock = _NeverLock()
    try:
        assert bot._on_ctp_fill(_report("3001")) is False
    finally:
        bot._trade_lock = real_lock
    assert "fill_lock_timeout" in _events(tmp_path)


def test_gateway_sets_waiter_before_accounting() -> None:
    """记账回调里必须已经能看见等待者被唤醒，否则主线程会持锁空等。"""
    settings = CtpSettings(
        td_front="tcp://127.0.0.1:1234",
        broker_id="0000",
        user_id="10001",
        password="secret",
        app_id="client_AutoTrading_1.0",
        auth_code="auth",
        flow_dir="data/ctp_flow",
        project_root="",
    )
    gateway = CtpGateway(settings)
    gateway.table.add(TrackedOrder("1", "bu2612", "0", "0", 1, 3500))
    waiter = threading.Event()
    gateway._waiters["1"] = waiter
    seen: dict[str, bool] = {}

    def handler(report: dict) -> None:
        seen["waiter_set"] = waiter.is_set()

    gateway.fill_handler = handler
    gateway.on_trade("1", 3500, 1, {"symbol": "bu2612", "direction": "0", "offset": "0"})

    assert seen["waiter_set"] is True


def test_no_deadlock_when_main_thread_holds_trade_lock(tmp_path):
    """主线程持锁时，回调线程等到锁释放后仍能入账，不会把双方卡死。"""
    bot = TradingBot(_cfg(tmp_path))
    bot._recent_fill_keys = OrderedDict()
    report = _report("5001")
    result: dict[str, bool] = {}

    def worker() -> None:
        result["ok"] = bot._on_ctp_fill(report)

    thread = threading.Thread(target=worker)
    bot._trade_lock.acquire()
    try:
        thread.start()
        time.sleep(0.3)
    finally:
        bot._trade_lock.release()

    thread.join(5)
    assert not thread.is_alive()
    assert result.get("ok") is True
    assert _volume(bot) == 1
