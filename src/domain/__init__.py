"""领域层：跨模块共用的数据类型与账本读写。"""

from src.domain.position import PositionState
from src.domain.trade import TradeRecord

__all__ = ["PositionState", "TradeRecord"]
