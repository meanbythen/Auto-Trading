"""
进程内命令总线。

功能：界面/脚本把「扫描、清仓、停止」这类命令投进队列，常驻主循环按 FIFO 取走执行。
原理：单消费者、先进先出、同步执行。命令只是通知，不改变风控与报单的串行顺序；
      队列满时返回 False，界面不会被阻塞。超时等待替代每秒轮询，stop 也能及时响应。
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from typing import Any, Mapping

from loguru import logger

SCAN = "scan"
FLATTEN = "flatten"
STOP = "stop"


@dataclass(frozen=True)
class Command:
    """一条命令。data 只放执行这条命令需要的最少字段。"""

    name: str
    data: Mapping[str, Any] = field(default_factory=dict)


class CommandBus:
    """线程安全命令队列；常驻主循环是唯一消费者。"""

    def __init__(self, maxsize: int = 256) -> None:
        self._queue: queue.Queue[Command] = queue.Queue(maxsize=maxsize)

    def post(self, name: str, data: Mapping[str, Any] | None = None) -> bool:
        """投递命令。队列满返回 False，不阻塞调用方。"""
        command = Command(name=str(name), data=dict(data or {}))
        try:
            self._queue.put_nowait(command)
        except queue.Full:
            logger.warning("命令队列已满，丢弃命令 {}", command.name)
            return False
        return True

    def get(self, timeout: float | None = None) -> Command | None:
        """取一条命令；超时返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[Command]:
        """把当前排队的命令一次取完，保持原顺序。"""
        items: list[Command] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                return items

    def clear(self) -> None:
        """丢弃尚未处理的命令（退出或测试用）。"""
        self.drain()
