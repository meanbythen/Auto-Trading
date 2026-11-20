"""柜台资金口径：用 CTP 资金快照算当天盈亏。

账本对账、账户统计和日亏熔断都读这一份，避免各处用不同字段组合。
"""

from __future__ import annotations

from typing import Any


def broker_day_pnl(data: dict[str, Any] | None) -> float | None:
    """
    用柜台资金算出当天盈亏，给日亏损熔断用。

    有昨结就用：动态权益 − 昨结 − 入金 + 出金。没有昨结时，用平仓盈亏加盯市浮盈。
    算不出就返回 None，调用方继续用原来的账本口径，不会因此停开仓。
    """
    if not isinstance(data, dict):
        return None
    try:
        pre = float(data.get("pre_balance") or 0)
        bal = float(data.get("balance") or 0)
        dep = float(data.get("deposit") or 0)
        if data.get("withdraw_amt") is not None:
            wdr = float(data.get("withdraw_amt") or 0)
        elif data.get("withdraw_quota") is not None:
            wdr = float(data.get("withdraw") or 0)
        else:
            # 已翻译快照里 withdraw 是可取资金，不能当出金。
            wdr = 0.0
    except (TypeError, ValueError):
        return None
    # 可取资金几乎等于权益时，那是额度不是出金。
    if wdr > 1 and pre > 1 and abs(wdr - pre) < 0.05:
        wdr = 0.0
    if wdr > 1 and bal > 1 and abs(wdr - bal) < 0.05:
        wdr = 0.0
    equity_pnl = round(bal - pre - dep + wdr, 2) if pre > 1e-9 and bal > 0 else None
    try:
        close_p = float(data.get("close_profit") or 0)
        pos_p = float(data.get("position_profit") or 0)
        fee = float(data.get("commission") or 0)
        trade_pnl = round(close_p + pos_p - fee, 2)
        has_trade = any(
            data.get(key) is not None
            for key in ("close_profit", "position_profit", "commission")
        )
    except (TypeError, ValueError):
        trade_pnl = None
        has_trade = False
    if equity_pnl is not None:
        if has_trade and abs(equity_pnl) < 0.005 and abs(trade_pnl or 0) >= 0.01:
            return trade_pnl
        return equity_pnl
    if has_trade:
        return trade_pnl
    return None
