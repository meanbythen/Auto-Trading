"""期权实盘隔离开关。

功能：账户还没开通期权交易权限前，默认禁止在真实账号开期权仓；
     仿真账号、sim/gui 试跑不受影响；账户开通后把 trade.option_live_enabled 打开即可。
原理：只拦「开仓」，不拦平仓——有仓时平仓是降风险，不能因为开关关着把仓锁死。
     风控仍然是最高优先级：这里只做通道隔离，不参与手数计算。
"""

from __future__ import annotations

from typing import Any

from src.domain.options import is_option

DEFAULT_OPTION_LIVE_ENABLED = False

_LIVE_KINDS = {"live", "real", "prod", "实盘", "真实", "真实账号"}

# 平仓方向不拦；只有这两个开仓方向需要检查。
_OPEN_SIDES = {"buy_open", "sell_open"}


def option_live_enabled(cfg: dict[str, Any] | None) -> bool:
    """期权实盘开关；没写时默认关闭。"""
    trade = (cfg or {}).get("trade") or {}
    if not isinstance(trade, dict):
        return DEFAULT_OPTION_LIVE_ENABLED
    return bool(trade.get("option_live_enabled", DEFAULT_OPTION_LIVE_ENABLED))


def is_live_kind(value: Any) -> bool:
    """账号种类是不是真实账号。"""
    return str(value or "").strip().lower() in _LIVE_KINDS


def is_open_side(side: Any) -> bool:
    """是不是开仓方向。"""
    return str(side or "").strip().lower() in _OPEN_SIDES


def option_live_block_reason(
    cfg: dict[str, Any] | None,
    symbol: str,
    *,
    account_kind: Any = None,
    is_live: bool | None = None,
    dry_run: bool = False,
) -> str | None:
    """
    期权开仓隔离检查。

    返回 None 表示可以下单；返回文字表示必须拦住，并直接作为拒单原因。
    """
    if not is_option(symbol):
        return None
    if option_live_enabled(cfg):
        return None
    live = is_live if is_live is not None else is_live_kind(account_kind)
    if not live or dry_run:
        return None
    return (
        f"{symbol} 是期权，当前开关 trade.option_live_enabled=false，"
        "禁止在真实账号开仓。请先用仿真账号测试；"
        "账户开通期权交易权限后，到配置页打开「允许期权实盘」再交易。"
    )
