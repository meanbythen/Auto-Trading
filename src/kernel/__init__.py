"""
交易内核：发单前的风控链和只读快照。

功能：把「要不要做」从执行器里拆出来。常驻直接调用风控链。
原理：不超过六层。事件总线留给测试和以后真有人订阅时再用，下单热路径不空转。
开平仓规则仍在原来的策略文件里。
"""

from src.kernel.context import BarSnapshot
from src.kernel.events import EventBus, Event
from src.kernel.risk_chain import (
    GateResult,
    TradeIntent,
    run_early_gates,
    run_pretrade_gates,
    run_send_gates,
)

__all__ = [
    "BarSnapshot",
    "Event",
    "EventBus",
    "GateResult",
    "TradeIntent",
    "run_early_gates",
    "run_pretrade_gates",
    "run_send_gates",
]
