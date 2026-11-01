"""
后台机器人线程
功能：在独立线程跑 TradingBot，避免卡住图形界面。
原理：daemon 线程 + stop() 分段 sleep，界面可随时请求停止。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from loguru import logger

from src.scheduler import TradingBot


class BotWorker:
    """包装 TradingBot 的可启停后台工人。"""

    def __init__(self) -> None:
        self.bot: TradingBot | None = None
        self._thread: threading.Thread | None = None
        self._on_stopped: Callable[[], None] | None = None
        # 一点「启动常驻」就记为在跑，不必等主循环真正转起来。
        # 否则开头那一小段里，界面还以为没启动，下单模拟器仍能被点到。
        self._alive = False

    @property
    def running(self) -> bool:
        """是否正在常驻扫描。"""
        return self._alive or bool(self.bot and self.bot.is_running)

    @property
    def is_running(self) -> bool:
        """running 的别名：避免旧代码写成 is_running 时弹出报错并抢到 Python 黑框焦点。"""
        return self.running

    def start(self, cfg: dict[str, Any], on_stopped: Callable[[], None] | None = None) -> None:
        """启动常驻扫描。"""
        if self.running:
            logger.warning("机器人已在运行")
            return
        self._on_stopped = on_stopped
        self.bot = TradingBot(cfg)
        self._alive = True

        def _run() -> None:
            try:
                assert self.bot is not None
                self.bot.run_forever()
            except Exception as e:  # noqa: BLE001
                logger.exception("机器人线程异常: {}", e)
            finally:
                self._alive = False
                if self._on_stopped:
                    self._on_stopped()

        self._thread = threading.Thread(target=_run, name="TradingBotWorker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求停止。"""
        if self.bot:
            self.bot.stop()

    def emergency_flatten(self) -> list[str]:
        """紧急清仓（若机器人已创建）。"""
        if not self.bot:
            return ["机器人未启动"]
        return self.bot.emergency_flatten()

    def status_snapshot(self) -> dict[str, Any]:
        if not self.bot:
            return {}
        return self.bot.status_snapshot()

    def run_once(self, cfg: dict[str, Any]) -> None:
        """单次扫描（后台线程）。"""
        def _run() -> None:
            try:
                bot = TradingBot(cfg)
                bot.do_login()
                bot.scan_once(force=True)
                logger.info("单次扫描完成")
            except Exception as e:  # noqa: BLE001
                logger.exception("单次扫描失败: {}", e)

        threading.Thread(target=_run, name="TradingBotOnce", daemon=True).start()
