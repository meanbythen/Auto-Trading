"""
事件总线与标准报文。

功能：各层只发布/订阅事件，不互相拿对方的内部对象改来改去。
原理：同步队列，实盘和回测都按发生顺序处理，保证时序。发出去的 payload 先浅拷贝，
避免一个订阅者改字典、另一个订阅者跟着变。
"""

from __future__ import annotations

from collections import defaultdict
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

# 现在用到的事件名。还没订阅的类型不先占着，避免空转。
SIGNAL = "signal"
ORDER_REQUEST = "order_request"
RISK_BLOCK = "risk_block"


@dataclass(frozen=True)
class Event:
    """标准事件。data 只放这一件事需要的字段，不塞扫描过程的全部临时变量。"""

    name: str
    data: Mapping[str, Any]
    at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


Handler = Callable[[Event], None]


class EventBus:
    """同步事件总线。回测单线程顺序触发；常驻也用同步，避免乱序。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, name: str, handler: Handler) -> None:
        """订阅一种事件。同一处理函数只登记一次。"""
        bucket = self._handlers[str(name)]
        if handler not in bucket:
            bucket.append(handler)

    def unsubscribe(self, name: str, handler: Handler) -> None:
        bucket = self._handlers.get(str(name)) or []
        if handler in bucket:
            bucket.remove(handler)

    def publish(self, name: str, data: Mapping[str, Any] | None = None) -> Event:
        """发出事件。订阅者拿到的是副本，改它不会改到别人手里的那份。"""
        payload = dict(data or {})
        event = Event(name=str(name), data=payload)
        for handler in list(self._handlers.get(event.name) or []):
            handler(Event(name=event.name, data=copy(payload), at=event.at))
        return event
