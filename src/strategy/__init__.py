# 策略子包：注册表工厂 + 已实现策略 + 公共类型
from .daily_body import DailyBodyStrategy
from .dual_ma import DualMaStrategy
from .factory import REGISTRY, create_strategy, list_engines, sanitize_strategy_config
from .morning_edge import (
    MorningBoxAStrategy,
    MorningStructureBStrategy,
)
from .single_ma import SingleMaStrategy
from .supertrend import SuperTrendStrategy
from .swing_trend import SwingTrendStrategy
from .trend_follow import TrendFollowStrategy
from .types import PositionState, StrategyAction, StrategyDecision

__all__ = [
    "REGISTRY",
    "create_strategy",
    "list_engines",
    "sanitize_strategy_config",
    "DailyBodyStrategy",
    "MorningBoxAStrategy",
    "MorningStructureBStrategy",
    "SuperTrendStrategy",
    "SingleMaStrategy",
    "DualMaStrategy",
    "TrendFollowStrategy",
    "SwingTrendStrategy",
    "StrategyAction",
    "StrategyDecision",
    "PositionState",
]
