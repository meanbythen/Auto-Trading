"""
链路追踪号：一次信号到成交共用一个本地关联号。

功能：生成/读取当前上下文的 trace_id，日志和审计都带它。
原理：contextvars 随线程/任务隔离；柜台编号仍由 CTP 的 order_ref 单独负责。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

TRACE_PREFIX = "OT-"
_CURRENT: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    """生成新的本地关联号，不当作柜台委托号。"""
    return f"{TRACE_PREFIX}{uuid.uuid4().hex[:16]}"


def get_trace_id() -> str:
    """当前上下文里的关联号；没设置时返回空串。"""
    return str(_CURRENT.get() or "")


def set_trace_id(trace_id: str) -> str:
    """设置当前关联号；传空串表示清除。返回实际生效值。"""
    value = str(trace_id or "").strip()
    _CURRENT.set(value)
    return value


def ensure_trace_id() -> str:
    """有就复用，没有就生成并设置；同一条链路不会出现两个号。"""
    current = get_trace_id()
    if current:
        return current
    return set_trace_id(new_trace_id())


@contextmanager
def bind_trace(trace_id: str | None = None) -> Iterator[str]:
    """临时绑定关联号，退出后恢复原值。"""
    value = str(trace_id or "").strip() or new_trace_id()
    token = _CURRENT.set(value)
    try:
        yield value
    finally:
        _CURRENT.reset(token)
