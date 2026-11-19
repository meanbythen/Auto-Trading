# 策略子包：注册表工厂 + 已实现策略 + 公共类型
from .base import StrategyBase
from .daily_body import DailyBodyStrategy
from .dual_ma import DualMaStrategy
from .macd_swing import MacdSwingStrategy
from .factory import (
    CROSS_TEST_ENGINE_ORDER,
    ENGINE_ALIASES,
    ENGINE_LABELS,
    REGISTRY,
    cross_engine_choices,
    create_strategy,
    engine_label,
    list_engines,
    normalize_engine,
    sanitize_strategy_config,
)
from .supertrend import SuperTrendStrategy
from .dual_donchian import DualDonchianStrategy
from .dual_fractal import DualFractalStrategy
from .swing_box import SwingBoxStrategy
from .swing_trend import SwingTrendStrategy
from .trend_follow import TrendFollowStrategy
from .types import PositionState, StrategyAction, StrategyDecision

__all__ = [
    "StrategyBase",
    "REGISTRY",
    "ENGINE_ALIASES",
    "ENGINE_LABELS",
    "CROSS_TEST_ENGINE_ORDER",
    "create_strategy",
    "engine_label",
    "normalize_engine",
    "cross_engine_choices",
    "list_engines",
    "sanitize_strategy_config",
    "DailyBodyStrategy",
    "SuperTrendStrategy",
    "DualMaStrategy",
    "MacdSwingStrategy",
    "TrendFollowStrategy",
    "SwingTrendStrategy",
    "SwingBoxStrategy",
    "DualDonchianStrategy",
    "DualFractalStrategy",
    "StrategyAction",
    "StrategyDecision",
    "PositionState",
]
