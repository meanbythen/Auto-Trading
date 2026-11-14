# 策略子包：注册表工厂 + 已实现策略 + 公共类型
from .base import StrategyBase
from .daily_body import DailyBodyStrategy
from .float_hold import FloatHoldStrategy
from .dual_ma import DualMaStrategy
from .macd_swing import MacdSwingStrategy
from .factory import REGISTRY, create_strategy, list_engines, sanitize_strategy_config
from .supertrend import SuperTrendStrategy
from .swing_box import SwingBoxStrategy
from .swing_trend import SwingTrendStrategy
from .trend_follow import TrendFollowStrategy
from .types import PositionState, StrategyAction, StrategyDecision

__all__ = [
    "StrategyBase",
    "REGISTRY",
    "create_strategy",
    "list_engines",
    "sanitize_strategy_config",
    "DailyBodyStrategy",
    "FloatHoldStrategy",
    "SuperTrendStrategy",
    "DualMaStrategy",
    "MacdSwingStrategy",
    "TrendFollowStrategy",
    "SwingTrendStrategy",
    "SwingBoxStrategy",
    "StrategyAction",
    "StrategyDecision",
    "PositionState",
]
