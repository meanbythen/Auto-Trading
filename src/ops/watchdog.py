"""
主循环心跳看门狗。

功能：进程还在、但扫描循环已经很久没心跳时告警。不依赖界面，关掉窗口也照跑。
原理：后台线程按固定间隔读一次运行快照；只有「交易时段 + 有持仓」才关心心跳，
     超过阈值通知一次，心跳恢复后才允许下一次通知。回调抛错也不让线程退出。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from loguru import logger


def _parse_stamp(value: Any) -> datetime | None:
    """把快照里的扫描时刻解析成 datetime；空值或坏格式返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


class TradingWatchdog:
    """主循环心跳看门狗：pid 还在但扫描停了时告警，不依赖界面。"""

    def __init__(
        self,
        *,
        get_snapshot: Callable[[], dict[str, Any]],
        stale_after_sec: float,
        on_stale: Callable[[dict[str, Any]], None],
        interval_sec: float = 5.0,
        name: str = "TradingWatchdog",
    ) -> None:
        self._get_snapshot = get_snapshot
        self._stale_after_sec = max(0.0, float(stale_after_sec))
        self._on_stale = on_stale
        self._interval_sec = max(0.001, float(interval_sec))
        self._name = str(name or "TradingWatchdog")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 一次停滞只告警一次，心跳恢复后清掉。
        self._alerted = False
        # 读不到最近扫描时间时只提醒一次，别每轮刷屏。
        self._parse_warned = False

    def start(self) -> None:
        """启动轮询线程。重复调用不会起第二个线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._alerted = False
            self._parse_warned = False
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            thread = self._thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """请求停止并等线程退出。线程里自己调时不 join，否则会一直等下去。"""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        if thread is threading.current_thread():
            return
        thread.join(max(0.0, float(timeout)))
        if thread.is_alive():
            logger.warning("{} 在 {} 秒内没有退出", self._name, timeout)

    @property
    def alive(self) -> bool:
        """轮询线程是否还在跑。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _run(self) -> None:
        """轮询主体：任何异常都只记日志，不能让监控线程退出。"""
        while not self._stop.wait(self._interval_sec):
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("看门狗这一轮检查失败，继续轮询")

    def _poll_once(self) -> None:
        """读一次快照，判断要不要告警。"""
        snap = self._get_snapshot() or {}
        if not snap.get("in_session") or not snap.get("has_position"):
            self._alerted = False
            return
        scanned = _parse_stamp(snap.get("last_scan_at"))
        if scanned is None:
            if not self._parse_warned:
                self._parse_warned = True
                logger.warning("看门狗读不到最近扫描时间（{!r}），本轮跳过", snap.get("last_scan_at"))
            return
        self._parse_warned = False
        age = (datetime.now() - scanned).total_seconds()
        if age < self._stale_after_sec:
            self._alerted = False
            return
        if self._alerted:
            return
        self._alerted = True
        try:
            self._on_stale({**snap, "age_sec": age})
        except Exception:  # noqa: BLE001
            logger.exception("看门狗告警回调失败")
