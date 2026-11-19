"""
品种手续费。

功能：按「品种与合约」表里的每手手续费，算开仓/平仓要扣多少钱。
原理：      开仓按「开仓手续费」×手数；平昨按「平仓手续费」（空=跟开仓相同）；
      平今按「平今手续费」（空=跟平仓/开仓相同）；填 0 表示免费。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.risk.universe_table import product_key
from src.utils.trading_day import futures_trading_day


def _box(contracts: dict[str, Any] | None, symbol: str) -> dict[str, Any]:
    table = contracts or {}
    key = product_key(symbol)
    item = table.get(key) if key else None
    return item if isinstance(item, dict) else {}


def open_commission_per_lot(contracts: dict[str, Any] | None, symbol: str) -> float:
    """开仓：每手多少元。"""
    box = _box(contracts, symbol)
    return max(0.0, float(box.get("commission") or 0))


def close_commission_per_lot(
    contracts: dict[str, Any] | None,
    symbol: str,
    *,
    close_today: bool,
) -> float:
    """
    平仓：每手多少元。

    平昨优先用 commission_close，没有就跟开仓相同。
    平今用 commission_close_today；没单独写时跟平昨同一价。
    """
    box = _box(contracts, symbol)
    open_fee = max(0.0, float(box.get("commission") or 0))
    close_fee = open_fee
    if "commission_close" in box and box.get("commission_close") is not None:
        close_fee = max(0.0, float(box.get("commission_close") or 0))
    if not close_today:
        return close_fee
    if "commission_close_today" not in box or box.get("commission_close_today") is None:
        return close_fee
    return max(0.0, float(box.get("commission_close_today") or 0))


def fee_open(contracts: dict[str, Any] | None, symbol: str, volume: int) -> float:
    """开仓手续费合计。"""
    lots = max(0, int(volume or 0))
    return round(open_commission_per_lot(contracts, symbol) * lots, 2)


def fee_close(
    contracts: dict[str, Any] | None,
    symbol: str,
    volume: int,
    *,
    close_today: bool,
) -> float:
    """平仓手续费合计。"""
    lots = max(0, int(volume or 0))
    return round(close_commission_per_lot(contracts, symbol, close_today=close_today) * lots, 2)


def is_close_today(opened_at: str | datetime | None, closed_at: str | datetime | None) -> bool:
    """
    是不是平今：开仓和平仓属于同一个期货交易日（夜盘 21:00 起算，以日盘日期为准）。

    例：周一 21:36 开、周二 10:00 平，算平今；周二日盘开、周二夜盘 21:30 平，
    夜盘已属周三，算平昨。
    """
    if opened_at is None or closed_at is None:
        return True
    try:
        import pandas as pd

        a = pd.Timestamp(opened_at)
        b = pd.Timestamp(closed_at)
        if a.tzinfo is not None:
            a = a.tz_localize(None)
        if b.tzinfo is not None:
            b = b.tz_localize(None)
        return futures_trading_day(a.to_pydatetime()) == futures_trading_day(b.to_pydatetime())
    except Exception:  # noqa: BLE001
        return True
