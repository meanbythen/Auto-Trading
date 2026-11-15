"""主循环心跳看门狗：停摆只告警一次，空仓和休市不告警；阈值随扫描间隔走。"""

import threading
import time
from datetime import datetime, timedelta

from src.ops.watchdog import TradingWatchdog
from src.scheduler.runner import TradingBot


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


def _stale_snapshot(*, has_position: bool = True, in_session: bool = True) -> dict:
    """最近一次扫描停在 10 分钟前。"""
    stamp = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
    return {"last_scan_at": stamp, "has_position": has_position, "in_session": in_session}


def _run_watchdog(snapshot: dict, hits: list[dict], fired: threading.Event) -> TradingWatchdog:
    watchdog = TradingWatchdog(
        get_snapshot=lambda: snapshot,
        stale_after_sec=1,
        on_stale=lambda snap: (hits.append(snap), fired.set()),
        interval_sec=0.01,
    )
    watchdog.start()
    return watchdog


def test_stale_heartbeat_alerts_once() -> None:
    """有仓、在盘里、心跳停了：触发一次，之后不再重复。"""
    hits: list[dict] = []
    fired = threading.Event()
    watchdog = _run_watchdog(_stale_snapshot(), hits, fired)
    try:
        assert fired.wait(2.0) is True
        time.sleep(0.2)
        assert len(hits) == 1
        assert hits[0]["age_sec"] >= 60
    finally:
        watchdog.stop()
    assert watchdog.alive is False


def test_flat_book_never_alerts() -> None:
    """空仓不关心心跳，停摆也不告警。"""
    hits: list[dict] = []
    fired = threading.Event()
    watchdog = _run_watchdog(_stale_snapshot(has_position=False), hits, fired)
    try:
        time.sleep(0.2)
        assert len(hits) == 0
    finally:
        watchdog.stop()


def test_outside_session_never_alerts() -> None:
    """非交易时段本就不扫描，停摆不告警。"""
    hits: list[dict] = []
    fired = threading.Event()
    watchdog = _run_watchdog(_stale_snapshot(in_session=False), hits, fired)
    try:
        time.sleep(0.2)
        assert len(hits) == 0
    finally:
        watchdog.stop()


def test_watchdog_threshold_follows_scan_interval(tmp_path) -> None:
    """阈值取三个扫描周期和 180 秒里更大的那个。"""
    bot = TradingBot(_cfg(tmp_path))
    assert bot._watchdog_threshold() == max(3 * bot.scan_interval, 180.0)

    cfg = _cfg(tmp_path)
    cfg["schedule"]["scan_interval_sec"] = 120
    slow_bot = TradingBot(cfg)
    assert slow_bot._watchdog_threshold() == 360.0
