"""期权到期生命周期。

功能：判断期权现在能不能开、要不要因为到期而强平。
原理：现项目原本没有任何到期日/最后交易日概念；期权到期不处理会变成行权或被作废。
     最后交易日来自品种与合约表的「到期日」列，不从合约代码猜。
     缺到期日一律拒绝开仓，避免「不知道哪天到期」的仓位裸奔。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Any

from src.domain.options import is_option

_DATE_PATTERN = re.compile(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})$")
_MONTH_PATTERN = re.compile(r"^(\d{2}|\d{4})[-/]?(\d{2})$")


def _cfg_box(cfg: dict[str, Any] | None, symbol: str) -> dict[str, Any]:
    from src.domain.option_contract import contract_box

    return contract_box(cfg, symbol)


def parse_expiry(value: Any) -> date | None:
    """
    解析到期日。

    支持：YYYY-MM-DD、YYYYMMDD、YYYY-MM（按当月最后一天）、YYMM（按当月最后一天）。
    认不出返回 None。
    """
    text = str(value or "").strip()
    if not text:
        return None
    match = _DATE_PATTERN.match(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = _MONTH_PATTERN.match(text)
    if match:
        year_text, month_text = match.group(1), match.group(2)
        year = int(year_text) if len(year_text) == 4 else 2000 + int(year_text)
        month = int(month_text)
        if not 1 <= month <= 12:
            return None
        day = calendar.monthrange(year, month)[1]
        return date(year, month, day)
    return None


def expiry_date(cfg: dict[str, Any] | None, symbol: str) -> date | None:
    """这个期权合约的最后交易日；没配或不是期权返回 None。"""
    if not is_option(symbol):
        return None
    box = _cfg_box(cfg, symbol)
    raw = (
        box.get("expiry")
        or box.get("expire_date")
        or box.get("last_trading_day")
        or ""
    )
    return parse_expiry(raw)


def days_to_expiry(
    cfg: dict[str, Any] | None, symbol: str, now: datetime | date | None = None
) -> int | None:
    """离最后交易日还有几天；没配到期日返回 None。"""
    last = expiry_date(cfg, symbol)
    if last is None:
        return None
    today = now.date() if isinstance(now, datetime) else now or date.today()
    return (last - today).days


def _risk_num(cfg: dict[str, Any] | None, key: str, default: float) -> float:
    try:
        return float(((cfg or {}).get("risk") or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def assert_tradable(
    cfg: dict[str, Any] | None,
    symbol: str,
    now: datetime | date | None = None,
) -> tuple[bool, str]:
    """
    开仓前检查：这个期权现在能不能开。

    返回 (能否开, 原因)。非期权恒为可开。
    """
    if not is_option(symbol):
        return True, ""
    last = expiry_date(cfg, symbol)
    if last is None:
        return False, (
            f"{symbol} 没有到期日，拒绝开仓：请先在「品种与合约」表填上「到期日」"
        )
    today = now.date() if isinstance(now, datetime) else now or date.today()
    if today > last:
        return False, f"{symbol} 已过最后交易日 {last.isoformat()}，不能开仓"
    if today == last and bool(
        ((cfg or {}).get("risk") or {}).get("option_no_open_on_expiry_day", True)
    ):
        return False, f"{symbol} 今天是最后交易日 {last.isoformat()}，只平不开"
    no_open_days = max(0, int(_risk_num(cfg, "option_no_open_days", 0)))
    left = (last - today).days
    if no_open_days > 0 and left <= no_open_days:
        return False, (
            f"{symbol} 距最后交易日只剩 {left} 天（阈值 {no_open_days} 天），只平不开"
        )
    return True, ""


def force_close_reason(
    cfg: dict[str, Any] | None,
    symbol: str,
    now: datetime | date | None = None,
) -> str | None:
    """
    持仓到期前强平原因；不需要强平返回 None。

    阈值：risk.option_force_close_days，默认 0 = 最后交易日当天强平。
    """
    if not is_option(symbol):
        return None
    last = expiry_date(cfg, symbol)
    if last is None:
        # 没配到期日时，持仓也不装死：按「缺数据」提示强平，避免进入行权。
        return f"{symbol} 缺到期日，无法判断行权风险，强制平仓"
    today = now.date() if isinstance(now, datetime) else now or date.today()
    keep_days = max(0, int(_risk_num(cfg, "option_force_close_days", 0)))
    if today >= last:
        return f"{symbol} 已到最后交易日 {last.isoformat()}，强制平仓，避免行权"
    left = (last - today).days
    if left <= keep_days:
        return (
            f"{symbol} 距最后交易日只剩 {left} 天"
            f"（阈值 {keep_days} 天），提前强制平仓"
        )
    return None
