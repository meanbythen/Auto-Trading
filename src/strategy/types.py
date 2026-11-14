"""
策略公共类型
功能：统一动作枚举、决策结果、持仓状态，避免各策略各写一套。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.domain.position import PositionState as PositionState


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
    try:
        import numpy as np

        if isinstance(value, np.ndarray):
            if value.size == 0:
                return default
            return bool(value.reshape(-1)[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(value)
    except ValueError:
        return default


# 开仓触发方式（strategy.entry_mode），所有策略共用：
#   trigger = 当前未收线的高/低点碰到开仓位就进（实时看影线，激进）
#   close   = 等这根 K 线收盘，收盘价穿越开仓位才进（保守，默认）
ENTRY_MODE_TRIGGER = "trigger"
ENTRY_MODE_CLOSE = "close"
ENTRY_MODE_LABELS: dict[str, str] = {
    ENTRY_MODE_TRIGGER: "触发即开仓",
    ENTRY_MODE_CLOSE: "收盘价确认",
}


def normalize_entry_mode(value: Any) -> str:
    """把配置/界面写法收成 trigger 或 close。认不出的按默认 close。"""
    text = str(value or "").strip().lower()
    if text in {"trigger", "touch", "触及", "触发即开仓", "即开仓", "实时"}:
        return ENTRY_MODE_TRIGGER
    return ENTRY_MODE_CLOSE


def entry_mode_label(value: Any) -> str:
    """配置值换成配置页上的中文选项。"""
    return ENTRY_MODE_LABELS[normalize_entry_mode(value)]


def entry_mode_from_label(label: Any) -> str:
    """配置页选中的中文选项换回配置值。"""
    text = str(label or "").strip()
    for code, name in ENTRY_MODE_LABELS.items():
        if text == name or text == code:
            return code
    return normalize_entry_mode(text)


def entry_mode_from_cfg(cfg: dict[str, Any]) -> str:
    """读全策略共用的开仓触发方式。"""
    st = (cfg or {}).get("strategy") or {}
    return normalize_entry_mode(st.get("entry_mode"))


# 止损触发方式（strategy.stop_mode），所有策略共用：
#   touch = 价格最高/最低碰到止损位就平（实时看影线，默认）
#   close = 等这根 K 线收盘，收盘价跌破/站上止损位才平
STOP_MODE_TOUCH = "touch"
STOP_MODE_CLOSE = "close"
STOP_MODE_LABELS: dict[str, str] = {
    STOP_MODE_TOUCH: "触及即止损",
    STOP_MODE_CLOSE: "收盘价确认",
}


def normalize_stop_mode(value: Any) -> str:
    """把配置/界面写法收成 touch 或 close。认不出的按默认 touch。"""
    text = str(value or "").strip().lower()
    if text in {"close", "收盘", "收盘价", "收盘价确认", "k线收盘", "c"}:
        return STOP_MODE_CLOSE
    return STOP_MODE_TOUCH


def stop_mode_label(value: Any) -> str:
    """配置值换成配置页上的中文选项。"""
    return STOP_MODE_LABELS[normalize_stop_mode(value)]


def stop_mode_from_label(label: Any) -> str:
    """配置页选中的中文选项换回配置值。"""
    text = str(label or "").strip()
    for code, name in STOP_MODE_LABELS.items():
        if text == name or text == code:
            return code
    return normalize_stop_mode(text)


def stop_mode_from_cfg(cfg: dict[str, Any]) -> str:
    """
    读全策略共用的止损触发方式。

    旧配置曾写在 strategy.daily_body 里，读到也认，优先用 strategy.stop_mode。
    """
    st = (cfg or {}).get("strategy") or {}
    if "stop_mode" in st:
        return normalize_stop_mode(st.get("stop_mode"))
    body = st.get("daily_body") or {}
    return normalize_stop_mode(body.get("stop_mode"))


def stop_triggered(
    *,
    side: str,
    stop: float,
    close: float,
    high: float,
    low: float,
    mode: str,
) -> bool:
    """
    价格有没有碰到止损位。

    touch：看这一根的最高/最低，碰到就算。
    close：只看收盘价，影线刺出去又收回来不算。
    """
    if stop <= 0 or side not in {"long", "short"}:
        return False
    if normalize_stop_mode(mode) == STOP_MODE_CLOSE:
        return close <= stop if side == "long" else close >= stop
    return low <= stop if side == "long" else high >= stop


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



