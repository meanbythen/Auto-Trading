"""
日内浮盈飘单。

拆成四个可替换模块：方向选择器、仓位（公共凯利/风控）、仓单生命周期、止损。
外面仍 `from src.strategy.float_hold import FloatHoldStrategy`。
"""

from .direction import (
    DIRECTION_DAY_CLUSTER,
    DIRECTION_ETH_TREND,
    DIRECTION_LABELS,
    DIRECTION_MACD_CYCLE,
    DIRECTION_OPENING_BOX,
    DIRECTION_REGISTRY,
    DirectionContext,
    DirectionPick,
    DirectionSelector,
    direction_label,
    make_direction,
    normalize_direction_key,
)
from .macd_cycle import (
    EMA_PAIR_LABELS,
    MACD_COMBO_LABELS,
    MACD_COMBO_PRESETS,
    MACD_TF_LABELS,
    ema_pair_label,
    format_macd_combo,
    macd_combo_label,
    macd_timeframe_label,
    normalize_macd_timeframe,
    parse_ema_pair_label,
    parse_macd_combo,
)
from .stop import atr_width_stop
from .strategy import FloatHoldStrategy

__all__ = [
    "FloatHoldStrategy",
    "atr_width_stop",
    "DIRECTION_DAY_CLUSTER",
    "DIRECTION_ETH_TREND",
    "DIRECTION_LABELS",
    "DIRECTION_MACD_CYCLE",
    "DIRECTION_OPENING_BOX",
    "DIRECTION_REGISTRY",
    "DirectionContext",
    "DirectionPick",
    "DirectionSelector",
    "direction_label",
    "make_direction",
    "normalize_direction_key",
    "EMA_PAIR_LABELS",
    "MACD_COMBO_LABELS",
    "MACD_COMBO_PRESETS",
    "MACD_TF_LABELS",
    "ema_pair_label",
    "format_macd_combo",
    "macd_combo_label",
    "macd_timeframe_label",
    "normalize_macd_timeframe",
    "parse_ema_pair_label",
    "parse_macd_combo",
]
