"""
按交割月提前换月。

功能：从合约代码读出交割年月（fu2701 → 2027 年 1 月），规定交割月当天起不再交易
      这一手；上一月的日盘收盘必须平仓。主力连续回测会提前切到下一手。
原理：国内商品期货的交割月写在代码后四位。不少品种最后交易日就在交割月前一个月
      的最后一个交易日；螺纹等品种虽然能做到交割月 15 号，这里按你的要求更严：
      任何品种都不把仓留进交割月。夜盘 21:00 起算下一个交易日，所以 12 月最后
      一个交易日的夜盘已经属于 1 月，必须在当天 15:00 日盘收完就走。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from src.risk.universe_table import product_key
from src.utils.trading_day import futures_trading_day

# 表里只有一个合约、看不出换月节奏时，用各品种常见的交割月。对不上就按每月都有。
KNOWN_DELIVERY_MONTHS: dict[str, tuple[int, ...]] = {
    "fu": (1, 3, 5, 6, 9, 11),
    "sc": tuple(range(1, 13)),
    "lu": tuple(range(1, 13)),
    "bu": tuple(range(1, 13)),
    "rb": (1, 5, 10),
    "hc": (1, 5, 10),
    "i": (1, 5, 9),
    "j": (1, 5, 9),
    "jm": (1, 5, 9),
    "ag": tuple(range(1, 13)),
    "au": tuple(range(1, 13)),
}

# 离最后可持仓日还有这么多天，配置页就用黄条提醒。
WARN_WITHIN_DAYS = 14
DAY_CLOSE = dtime(15, 0)
NIGHT_OPEN = dtime(21, 0)


def parse_contract_month(
    symbol: str,
    as_of: date | None = None,
) -> tuple[str, int, int] | None:
    """
    拆出字母、交割年和交割月。

    fu2701 / BU2612 → 四位年月。郑商所三位（TA605）按当前十年补年份。
    认不出就返回 None，调用方不要拦交易。
    """
    text = str(symbol or "").strip()
    letters = "".join(ch for ch in text if ch.isalpha())
    digits = "".join(ch for ch in text if ch.isdigit())
    if not letters or not digits:
        return None
    ref = as_of or date.today()
    if len(digits) == 4:
        year = 2000 + int(digits[:2])
        month = int(digits[2:])
    elif len(digits) == 3:
        year = (ref.year // 10) * 10 + int(digits[0])
        if year < ref.year - 5:
            year += 10
        month = int(digits[1:])
    else:
        return None
    if month < 1 or month > 12:
        return None
    return letters, year, month


def delivery_month_start(symbol: str, as_of: date | None = None) -> date | None:
    """交割月第一天。fu2701 → 2027-01-01。"""
    parsed = parse_contract_month(symbol, as_of)
    if parsed is None:
        return None
    _letters, year, month = parsed
    return date(year, month, 1)


def last_hold_date(symbol: str, as_of: date | None = None) -> date | None:
    """
    这一手最后还能算「没进交割月」的日历日。

    回测分段的结束日用这一天：当天日盘（到 20:59）还算旧合约，
    当天 21:00 夜盘已经是下一交易日，不会被收进这一段。
    """
    start = delivery_month_start(symbol, as_of)
    if start is None:
        return None
    return start - timedelta(days=1)


def format_contract(letters: str, year: int, month: int) -> str:
    """按原合约字母大小写拼回代码。"""
    return f"{letters}{year % 100:02d}{month:02d}"


def month_cycle_for(product: str, seen_months: list[int] | None = None) -> list[int]:
    """这个品种一年里通常有哪些交割月。表里见过的月优先并进已知节奏。"""
    key = product_key(product)
    known = list(KNOWN_DELIVERY_MONTHS.get(key) or ())
    extra = [int(m) for m in (seen_months or []) if 1 <= int(m) <= 12]
    months = []
    for month in known + extra:
        if month not in months:
            months.append(month)
    if not months:
        months = list(range(1, 13))
    return sorted(months)


def successor_symbol(symbol: str, cycle: list[int] | None = None) -> str | None:
    """按交割月节奏给出下一手。fu2701 且节奏含 3 月 → fu2703。"""
    parsed = parse_contract_month(symbol)
    if parsed is None:
        return None
    letters, year, month = parsed
    months = cycle or month_cycle_for(letters)
    later = [item for item in months if item > month]
    if later:
        nxt_month, nxt_year = later[0], year
    else:
        nxt_month, nxt_year = months[0], year + 1
    return format_contract(letters, nxt_year, nxt_month)


@dataclass(frozen=True)
class ContractGate:
    """这一根 K 线能不能还留在这手合约上。"""

    block_open: bool
    flatten: bool
    reason: str
    delivery_start: date | None
    last_hold: date | None


def contract_gate(symbol: str, now: datetime | date | None = None) -> ContractGate:
    """
    现在要不要为了交割月停手。

    交易日已经进交割月：平仓、不开新仓。
    当天日盘 15:00 及以后，若下一交易日（夜盘）已进交割月：当天收盘必须平，不再开。
    日线时间戳经常是 00:00，最后可持仓日的那根日线也按收盘处理。
    """
    stamp = now if now is not None else datetime.now()
    if isinstance(stamp, datetime):
        as_of = stamp.date()
        clock = stamp.time()
        moment: datetime | date = stamp
    else:
        as_of = stamp
        clock = dtime(0, 0)
        moment = stamp
    start = delivery_month_start(symbol, as_of)
    if start is None:
        return ContractGate(False, False, "", None, None)
    last = start - timedelta(days=1)
    td = futures_trading_day(moment)
    if td >= start:
        reason = (
            f"{symbol} 已到交割月（{start.year}年{start.month}月），"
            "交割月不再开仓，有仓必须平掉并换到下一手"
        )
        return ContractGate(True, True, reason, start, last)
    night_enters = False
    if isinstance(stamp, datetime) and clock >= DAY_CLOSE:
        night = stamp.replace(hour=NIGHT_OPEN.hour, minute=0, second=0, microsecond=0)
        night_enters = futures_trading_day(night) >= start
    daily_last = (not isinstance(stamp, datetime) or clock <= dtime(8, 0)) and td == last
    if night_enters or daily_last:
        reason = (
            f"{symbol} 日盘收盘后下一交易日进入交割月"
            f"（{start.year}年{start.month}月），收盘前必须平仓换月，不再开新仓"
        )
        return ContractGate(True, True, reason, start, last)
    return ContractGate(False, False, "", start, last)


def live_hint(symbol: str, now: datetime | date | None = None) -> tuple[str, bool]:
    """
    配置页关注合约下面的交割提示。

    返回 (文字, 要不要黄条)。认不出合约年月时文字为空。
    """
    stamp = now if now is not None else datetime.now()
    as_of = stamp.date() if isinstance(stamp, datetime) else stamp
    start = delivery_month_start(symbol, as_of)
    if start is None:
        return "", False
    last = start - timedelta(days=1)
    nxt = successor_symbol(symbol)
    nxt_txt = f"下一手常见是 {nxt}。" if nxt else ""
    gate = contract_gate(symbol, stamp)
    if gate.flatten:
        return (
            f"{gate.reason}。{nxt_txt}请把关注品种改成下一手，并自己平旧开新。",
            True,
        )
    remain = (last - futures_trading_day(stamp)).days
    warn = remain <= WARN_WITHIN_DAYS
    text = (
        f"{symbol} 交割月是 {start.year}年{start.month}月，"
        f"最晚 {last.isoformat()} 日盘收盘前换月，交割月不再交易。"
        f"{nxt_txt}"
    )
    return text.strip(), warn


def apply_delivery_cutoff(periods: list[Any]) -> list[Any]:
    """
    把主力时段裁到交割月之前，空出来的日子交给下一手。

    已经按持仓量提前换走的（下一手开始日更早）不往回拖。
    表把旧合约一直记到交割月里时，旧段在交割月前一天结束，后面改用下一手。
    """
    if not periods:
        return []
    product = getattr(periods[0], "product", "") or product_key(getattr(periods[0], "symbol", ""))
    seen = []
    for period in periods:
        start = delivery_month_start(period.symbol)
        if start is not None:
            seen.append(start.month)
    cycle = month_cycle_for(product, seen)
    ordered = sorted(periods, key=lambda p: (p.start, p.symbol))
    orig_end = max(p.end for p in ordered)

    cut: list[Any] = []
    for period in ordered:
        cap = last_hold_date(period.symbol)
        if cap is None:
            cut.append(period)
            continue
        if period.start > cap:
            continue
        cut.append(replace_period(period, end=min(period.end, cap)))

    filled: list[Any] = []
    for period in cut:
        start, end = period.start, period.end
        if filled:
            prev = filled[-1]
            if start <= prev.end:
                trimmed = start - timedelta(days=1)
                if trimmed >= prev.start:
                    filled[-1] = replace_period(prev, end=trimmed)
                else:
                    filled.pop()
            elif start > prev.end + timedelta(days=1):
                cap = last_hold_date(period.symbol)
                hole = prev.end + timedelta(days=1)
                if cap is None or hole <= cap:
                    start = hole
                    if cap is not None:
                        end = min(end, cap)
        if end >= start:
            filled.append(replace_period(period, start=start, end=end))

    if filled and filled[-1].end < orig_end:
        nxt = successor_symbol(filled[-1].symbol, cycle)
        if nxt:
            nstart = filled[-1].end + timedelta(days=1)
            ncap = last_hold_date(nxt) or orig_end
            nend = min(orig_end, ncap)
            if nend >= nstart:
                filled.append(
                    replace_period(
                        filled[-1],
                        symbol=nxt,
                        start=nstart,
                        end=nend,
                        manual=False,
                    )
                )
    return filled


def replace_period(period: Any, **changes: Any) -> Any:
    """MainPeriod 是 frozen dataclass，用 replace 改日期。"""
    from dataclasses import replace

    return replace(period, **changes)


def extend_last_before_delivery(
    periods: list[Any],
    req_end: date,
) -> tuple[list[Any], str]:
    """
    表只更新到昨天时，把最后一段顺延到用户选的结束日，但不得进交割月。

    顺延会碰到交割月时，旧合约停在交割月前一天，余下日子改用下一手。
    """
    if not periods:
        return periods, ""
    out = list(periods)
    last = out[-1]
    if last.end >= req_end:
        return out, ""
    cap = last_hold_date(last.symbol)
    seen_months = []
    for period in out:
        start = delivery_month_start(period.symbol)
        if start is not None:
            seen_months.append(start.month)
    cycle = month_cycle_for(last.product or product_key(last.symbol), seen_months)
    note = ""
    if cap is None or req_end <= cap:
        out[-1] = replace_period(last, end=req_end)
        note = (
            f"主力合约表最后一段只到 {last.end.isoformat()}，"
            f"{(last.end + timedelta(days=1)).isoformat()}~{req_end.isoformat()} "
            f"按 {last.symbol} 顺延；表过期时请点「更新主力合约表」。"
        )
        return out, note
    if last.end < cap:
        out[-1] = replace_period(last, end=cap)
    note = (
        f"{last.symbol} 交割月是 {delivery_month_start(last.symbol).isoformat()[:7]}，"
        f"最晚做到 {cap.isoformat()}，交割月不再交易。"
    )
    nxt = successor_symbol(last.symbol, cycle)
    nstart = cap + timedelta(days=1)
    if nxt and nstart <= req_end:
        ncap = last_hold_date(nxt) or req_end
        nend = min(req_end, ncap)
        if nend >= nstart:
            out.append(
                replace_period(last, symbol=nxt, start=nstart, end=nend, manual=False)
            )
            note += f"{nstart.isoformat()} 起改用 {nxt}。"
        else:
            note += "主力表还没有下一手，交割月这几天不测。"
    else:
        note += "主力表还没有下一手，交割月这几天不测。"
    return out, note
