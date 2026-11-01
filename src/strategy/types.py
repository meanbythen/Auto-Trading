"""
策略公共类型
功能：统一动作枚举、决策结果、持仓状态，避免各策略各写一套。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


def config_flag(value: Any, default: bool = True) -> bool:
    """
    把配置里的开关读成真或假。

    界面保存的是布尔值。手改 yaml 时写成 false、off、关 也会当成关掉。
    没写这一项时用 default，避免旧配置突然改变行为。
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "关"}
    return bool(value)


class StrategyAction(str, Enum):
    """策略给出的动作类型。"""

    HOLD = "hold"
    BUY_OPEN = "buy_open"
    SELL_OPEN = "sell_open"
    CLOSE = "close"


@dataclass
class StrategyDecision:
    """一次策略评估的结果。"""

    action: StrategyAction
    symbol: str
    reason: str
    stop_price: float | None = None
    entry_price: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionState:
    """策略内部记住的持仓状态。"""

    side: Literal["flat", "long", "short"] = "flat"
    volume: int = 0
    entry_price: float = 0.0
    initial_stop: float = 0.0
    trail_stop: float = 0.0
    # 是否已用大周期 SAR「确认方向」并开始移动止损（确认前只守影线端初始止损）
    trail_armed: bool = False
    symbol: str = ""
    opened_at: str = ""

    def reduce_close(self, volume: int) -> bool:
        """
        按实际成交手数减仓。

        返回 True 表示这一笔之后已经没有仓了。
        平今、平昨拆成两笔时，第一笔只减掉自己的手数，止损和开仓价都留着。
        手数大于等于现有仓位时，调用方再把仓位清成空仓。
        """
        lots = max(0, int(volume or 0))
        held = int(self.volume or 0)
        if self.side == "flat" or held <= 0 or lots >= held:
            return True
        self.volume = held - lots
        return False
