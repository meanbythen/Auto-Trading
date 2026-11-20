"""
信号账户的统计。

功能：按时间段、按品种，算出胜率、盈利因子、最大回撤、资金使用率和每日盈亏。
原理：平仓仍按先开先平配对。盈利因子是赚到的钱除以亏掉的钱的绝对值，和历史回测用的是同一个名字。
资金使用率是这段时间里保证金占用的最高值，除以这段开始时的余额。
日历上的每日盈亏：同花顺账只加已经平仓的交易盈亏；华泰账跟期货公司当日动态权益，不跟程序配对平仓。
日历按期货交易日分：夜盘 21:00 起算下一个交易日，归到次日那格。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from src.account.signal_book import matched_closes
from src.domain.contracts import DEFAULT_MULTIPLIER
from src.domain.pnl_stats import summarize_pnl
from src.utils.trading_day import futures_trading_day

# 界面上的时间段。天数含今天。全部表示不截断。
SPAN_DAYS = {
    "近1周": 7,
    "近1月": 30,
    "近3月": 90,
    "近6月": 180,
    "全部": None,
}


@dataclass
class SliceStat:
    """一段时间、或一个品种上的几个汇总数字。"""

    closed: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    max_drawdown: float = 0.0
    utilization: float | None = None
    net_pnl: float = 0.0


@dataclass
class SymbolStat:
    """一个品种在所选时间段里的成绩。"""

    symbol: str
    stat: SliceStat


@dataclass
class SignalReport:
    """给界面画图和填数字用的一份统计结果。"""

    account: SliceStat
    symbols: list[SymbolStat] = field(default_factory=list)
    equity: list[tuple[datetime, float]] = field(default_factory=list)
    daily_pnl: dict[date, float] = field(default_factory=dict)


def span_bounds(name: str, today: date) -> tuple[date | None, date]:
    """
    把「近1周」这类选项换成起止日期。

    结束日是传入的今天。全部没有开始日。近1周含今天在内共 7 天。
    调用方一般传期货交易日，这样夜盘也算在今天里。
    """
    days = SPAN_DAYS.get(name)
    if days is None:
        return None, today
    return today - timedelta(days=int(days) - 1), today


def lot_margin(
    symbol: str,
    price: float,
    volume: int,
    multiplier: float,
    *,
    margins: dict[str, float] | None = None,
    default_margin: float = 0.0,
    rate: float = 0.1,
    margin_rates: dict[str, float] | None = None,
) -> float:
    """
    这一手占用多少保证金。

    填了保证金比例：开仓价 × 每手数量 × 比例% × 手数。
    没填比例、写了每手金额：用那个固定金额 × 手数。
    再没有就用配置页兜底金额。都没有时按价格乘乘数乘手数再乘默认 10%。
    """
    product = "".join(ch for ch in str(symbol) if ch.isalpha()).lower()
    lots = max(0, int(volume))
    rate_pct = float((margin_rates or {}).get(product) or 0)
    if rate_pct > 0 and float(price) > 0 and float(multiplier) > 0:
        return float(price) * float(multiplier) * lots * (rate_pct / 100.0)
    per = float((margins or {}).get(product) or 0)
    if per > 0:
        return per * lots
    if default_margin > 0:
        return float(default_margin) * lots
    return float(price) * float(multiplier) * lots * float(rate)


def analyze_book(
    book: dict[str, Any],
    *,
    start: date | None,
    end: date | None,
    margins: dict[str, float] | None = None,
    default_margin: float = 0.0,
    margin_rate: float = 0.1,
    margin_rates: dict[str, float] | None = None,
) -> SignalReport:
    """
    统计一本信号账户。

    start 为空表示从账期第一天算起。end 为空表示不设结束日。
    """
    closes = matched_closes(book)
    chosen = [row for row in closes if _in_span(_as_date(row.get("exit_time")), start, end)]
    daily: dict[date, float] = {}
    for row in closes:
        day = _as_date(row.get("exit_time"))
        if day is None:
            continue
        daily[day] = round(daily.get(day, 0.0) + float(row.get("pnl") or 0), 2)
    broker_daily = _ctp_broker_daily(book)
    if broker_daily:
        # 华泰账：日历只跟期货公司当日盈亏，不把配对平仓混进来。
        daily = broker_daily

    equity = _equity_in_span(book, start, end)
    base = _equity_before(book, start)
    max_margin, by_symbol_margin = _margin_peaks(
        book,
        start,
        end,
        margins=margins,
        default_margin=default_margin,
        margin_rate=margin_rate,
        margin_rates=margin_rates,
    )
    account = _slice_from_closes(chosen, [point for _moment, point in equity])
    account.utilization = _ratio(max_margin, base)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in chosen:
        grouped.setdefault(str(row.get("symbol") or "未填品种"), []).append(row)
    symbols: list[SymbolStat] = []
    for symbol, rows in grouped.items():
        curve = _pnl_curve(rows)
        stat = _slice_from_closes(rows, curve)
        stat.utilization = _ratio(by_symbol_margin.get(symbol, 0.0), base)
        symbols.append(SymbolStat(symbol=symbol, stat=stat))
    symbols.sort(key=lambda item: abs(item.stat.net_pnl), reverse=True)
    if broker_daily:
        span_pnl = round(
            sum(value for day, value in broker_daily.items() if _in_span(day, start, end)),
            2,
        )
        account.net_pnl = span_pnl
        if len(symbols) == 1:
            symbols[0].stat.net_pnl = span_pnl
    return SignalReport(account=account, symbols=symbols, equity=equity, daily_pnl=daily)


def _ctp_broker_daily(book: dict[str, Any]) -> dict[date, float]:
    """华泰账每个交易日的期货公司盈亏。没有记录就空。"""
    if str(book.get("funding") or "") != "ctp":
        return {}
    # 收盘后柜台常把当日字段清零；先按历史把日历盈亏纠正回来。
    try:
        from src.account.signal_book import repair_ctp_daily

        repair_ctp_daily(book)
    except Exception:  # noqa: BLE001
        pass
    stored = book.get("ctp_daily")
    out: dict[date, float] = {}
    if isinstance(stored, dict):
        for key, raw in stored.items():
            try:
                day = date.fromisoformat(str(key)[:10])
                out[day] = round(float(raw), 2)
            except (TypeError, ValueError):
                continue
    snap = book.get("ctp_snapshot") if isinstance(book.get("ctp_snapshot"), dict) else {}
    if snap:
        from src.account.broker_pnl import broker_day_pnl
        from src.account.signal_book import broker_snapshot_day_pnl_usable

        # 结算后的空壳快照不要再盖日历；白天有效快照才覆盖。
        if broker_snapshot_day_pnl_usable(snap):
            pnl = broker_day_pnl(snap)
            if pnl is not None:
                when = _as_date(snap.get("time")) or futures_trading_day()
                out[when] = round(float(pnl), 2)
    return out


def format_win_rate(rate: float | None) -> str:
    """胜率写成百分数。还没有平仓就写横线。"""
    if rate is None:
        return "—"
    return f"{rate * 100:.2f}%"


def format_factor(factor: float | None) -> str:
    """盈利因子保留两位。只有赚钱、没有亏钱时不除零，直接写没有亏损。"""
    if factor is None:
        return "—"
    if math.isinf(factor):
        return "没有亏损"
    return f"{factor:.2f}"


def format_utilization(rate: float | None) -> str:
    """资金使用率写成百分数。这段开始时余额是 0，就算不出。"""
    if rate is None:
        return "—"
    return f"{rate * 100:.2f}%"


def _slice_from_closes(rows: list[dict[str, Any]], equity_points: list[float]) -> SliceStat:
    """用已经平掉的几段，算出胜率、盈利因子、净盈亏和回撤。"""
    stats = summarize_pnl(
        [float(row.get("pnl") or 0) for row in rows],
        zero_as_loss=False,
    )
    decided = stats.wins + stats.losses
    return SliceStat(
        closed=len(rows),
        win_rate=(stats.wins / decided) if decided else None,
        profit_factor=stats.profit_factor,
        max_drawdown=_drawdown(equity_points),
        net_pnl=round(sum(float(row.get("pnl") or 0) for row in rows), 2),
    )


def _pnl_curve(rows: list[dict[str, Any]]) -> list[float]:
    """一个品种自己的累计盈亏。回撤只看这条线，不把别的品种混进来。"""
    total = 0.0
    curve = [0.0]
    for row in rows:
        total = round(total + float(row.get("pnl") or 0), 2)
        curve.append(total)
    return curve


def _drawdown(points: list[float]) -> float:
    """从曾经到过的最高点往下量，取最大的那一段。"""
    if not points:
        return 0.0
    peak = points[0]
    worst = 0.0
    for point in points:
        peak = max(peak, point)
        worst = max(worst, peak - point)
    return round(worst, 2)


def _ratio(used: float, base: float) -> float | None:
    """占用除以本金。本金不是正数时不算。"""
    if base <= 0:
        return None
    return used / base


def _equity_before(book: dict[str, Any], start: date | None) -> float:
    """这段开始之前的余额。选全部时，用初始资金那一笔。"""
    total = 0.0
    seen_initial = False
    for row in book.get("changes") or []:
        day = _as_date(row.get("time"))
        amount = float(row.get("amount") or 0)
        if start is None:
            if not seen_initial and str(row.get("kind") or "") == "initial":
                return round(amount, 2)
            seen_initial = True
            continue
        if day is not None and day < start:
            total = round(total + amount, 2)
    if start is None:
        return round(total, 2)
    return total


def _equity_in_span(
    book: dict[str, Any],
    start: date | None,
    end: date | None,
) -> list[tuple[datetime, float]]:
    """
    这段里每一笔资金变动之后的余额。图上的权益线用它。

    选「全部」时从 0 开始加。初始资金已经是第一笔变动，不能再拿它当起点，
    否则 20340 会先放上去，循环里再加一次，图上就变成 40680。
    选近1周这类时段时，起点才是这段开始之前已经形成的余额，时段内的变动只加一遍。
    """
    balance = 0.0 if start is None else _equity_before(book, start)
    points: list[tuple[datetime, float]] = []
    if start is not None:
        points.append((datetime.combine(start, datetime.min.time()), balance))
    for row in book.get("changes") or []:
        moment = _as_datetime(row.get("time"))
        day = futures_trading_day(moment) if moment else None
        if not _in_span(day, start, end):
            continue
        balance = round(balance + float(row.get("amount") or 0), 2)
        if moment is not None:
            points.append((moment, balance))
    return points


def _margin_peaks(
    book: dict[str, Any],
    start: date | None,
    end: date | None,
    *,
    margins: dict[str, float] | None,
    default_margin: float,
    margin_rate: float,
    margin_rates: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """
    按开平仓重放，记下这段时间里保证金最高占到多少。

    先开的先减。平仓减掉的是当时开仓算出来的保证金，不是按平仓价重算。
    """
    held: list[dict[str, Any]] = []
    total = 0.0
    per_symbol: dict[str, float] = {}
    peak = 0.0
    peak_symbol: dict[str, float] = {}
    for row in book.get("trades") or []:
        moment = _as_datetime(row.get("time"))
        day = futures_trading_day(moment) if moment else None
        symbol = str(row.get("symbol") or "")
        if row.get("offset") == "开仓":
            margin = lot_margin(
                symbol,
                float(row.get("price") or 0),
                int(row.get("volume") or 0),
                float(row.get("multiplier") or DEFAULT_MULTIPLIER),
                margins=margins,
                default_margin=default_margin,
                rate=margin_rate,
                margin_rates=margin_rates,
            )
            held.append({**row, "left": int(row.get("volume") or 0), "margin_left": margin})
            total = round(total + margin, 2)
            per_symbol[symbol] = round(per_symbol.get(symbol, 0.0) + margin, 2)
        elif row.get("offset") == "平仓":
            need = int(row.get("volume") or 0)
            for item in held:
                if need <= 0:
                    break
                if item.get("symbol") != symbol or item.get("direction") != row.get("direction"):
                    continue
                left = int(item.get("left") or 0)
                if left <= 0:
                    continue
                take = min(left, need)
                part_margin = float(item.get("margin_left") or 0) * take / left if left else 0.0
                item["left"] = left - take
                item["margin_left"] = round(float(item.get("margin_left") or 0) - part_margin, 2)
                total = round(total - part_margin, 2)
                per_symbol[symbol] = round(per_symbol.get(symbol, 0.0) - part_margin, 2)
                need -= take
        if _in_span(day, start, end):
            peak = max(peak, total)
            for name, used in per_symbol.items():
                peak_symbol[name] = max(peak_symbol.get(name, 0.0), used)
    return round(peak, 2), {key: round(value, 2) for key, value in peak_symbol.items()}


def _in_span(day: date | None, start: date | None, end: date | None) -> bool:
    """这一天落在所选时间段里。没有日期的记录不参加这段统计。"""
    if day is None:
        return False
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _as_datetime(value: Any) -> datetime | None:
    """账本里的时间是「年-月-日 时:分:秒」。读不出来就当没有。"""
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _as_date(value: Any) -> date | None:
    """只取期货交易日。日历和按天汇总都用它，夜盘归到次日。"""
    moment = _as_datetime(value)
    if moment is None:
        return None
    return futures_trading_day(moment)
