"""
交易时段钟点。

功能：判断现在是不是在配置的交易时段里。白银、黄金夜盘会跨过零点，
     结束时间写成 02:30 时，21:00 之后和凌晨 02:30 之前都算在盘里。

原理：结束钟点比开始还早，就当成跨夜，而不是「从 21 点到凌晨 2 点中间是空的」。
     时段刚结束再留几十秒，免得最后一根刚收完就被当成休市。
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Any


# 和扫描里同一套：收线后 90 秒仍算在时段里。
DEFAULT_GRACE_SEC = 90.0


def parse_hhmm(value: Any) -> dtime:
    """把 '21:00' 收成钟点。坏数据当成 00:00，调用方不要靠它开仓。"""
    text = str(value or "").strip()
    try:
        hour_s, minute_s = text.split(":", 1)
        return dtime(int(hour_s), int(minute_s))
    except (TypeError, ValueError):
        return dtime(0, 0)


def last_open_at(now: datetime, session_open: dtime) -> datetime:
    """
    最近一次这段开盘的墙上时间。

    夜盘 21:00 开，凌晨 1 点看的时候，开盘是昨天 21:00，不是今天 21:00。
    """
    stamp = now.replace(second=0, microsecond=0)
    opened = datetime.combine(stamp.date(), session_open)
    if opened > stamp:
        opened -= timedelta(days=1)
    return opened


def cutoff_passed(now: datetime, session_open: dtime, cutoff: dtime) -> bool:
    """
    这段开仓截止钟点过了没有。

    夜盘截止若是当晚 22:30，凌晨也算已经过了。
    截止若写成 02:00，要到次日凌晨 2 点才过。
    """
    opened = last_open_at(now, session_open)
    edge = datetime.combine(opened.date(), cutoff)
    if edge <= opened:
        edge += timedelta(days=1)
    return now > edge


def in_clock_window(
    start: dtime,
    end: dtime,
    now: datetime,
    *,
    grace_sec: float = DEFAULT_GRACE_SEC,
) -> bool:
    """
    现在是否落在 start～end 里。

    普通时段：09:00～11:30，开始不晚于结束。
    跨夜时段：21:00～02:30，结束比开始早，21 点之后或凌晨结束之前都算在里面。
    """
    clock = now.time()
    grace = max(0.0, float(grace_sec))
    if start <= end:
        if start <= clock <= end:
            return True
        end_at = datetime.combine(now.date(), end)
        return bool(grace and end_at < now <= end_at + timedelta(seconds=grace))
    # 跨夜：晚上过了开始，或凌晨还没到结束。
    if clock >= start or clock <= end:
        return True
    end_at = datetime.combine(now.date(), end)
    return bool(grace and end_at < now <= end_at + timedelta(seconds=grace))


def in_session_windows(
    windows: list[tuple[dtime, dtime]],
    now: datetime,
    *,
    grace_sec: float = DEFAULT_GRACE_SEC,
) -> bool:
    """若干段时段，有一段套住现在就算在盘里。"""
    return any(in_clock_window(start, end, now, grace_sec=grace_sec) for start, end in windows)


# 贵金属夜盘收到次日凌晨。品种表没填时段时，白银/黄金用这一套。
LATE_NIGHT_PRODUCTS = frozenset({"ag", "au"})
_DAY_WINDOWS = (("09:00", "11:30"), ("13:30", "15:00"))


def product_code(symbol: Any) -> str:
    """ag2702 → ag。只留字母。"""
    return "".join(ch for ch in str(symbol or "").strip().lower() if "a" <= ch <= "z")


def parse_session_text(raw: Any) -> list[list[str]]:
    """
    把品种表里的「09:00-11:30,13:30-15:00,21:00-02:30」收成时段列表。
    空着表示这一行没写，调用方去用默认或总配置。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    text = text.replace("～", "-").replace("~", "-").replace("—", "-").replace("–", "-")
    text = text.replace("，", ",").replace("；", ",").replace(";", ",")
    out: list[list[str]] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" not in piece:
            continue
        start, end = piece.split("-", 1)
        start_s = start.strip()
        end_s = end.strip()
        if ":" not in start_s or ":" not in end_s:
            continue
        out.append([start_s[:5], end_s[:5]])
    return out


def parse_clock_list(raw: Any) -> list[str]:
    """把「14:55,02:25」收成钟点列表。"""
    text = str(raw or "").strip()
    if not text:
        return []
    text = text.replace("，", ",").replace("；", ",").replace(";", ",")
    out: list[str] = []
    for part in text.split(","):
        piece = part.strip()
        if ":" not in piece:
            continue
        out.append(piece[:5])
    return out


def format_session_text(sessions: list[list[str]] | None) -> str:
    """写回 Excel 用的一行字。"""
    bits = []
    for row in sessions or []:
        if not row or len(row) < 2:
            continue
        bits.append(f"{str(row[0]).strip()[:5]}-{str(row[1]).strip()[:5]}")
    return ",".join(bits)


def default_sessions_for_product(code: Any) -> list[list[str]]:
    """品种没填时段时的出厂值。白银黄金夜盘到 02:30，其它到 23:00。"""
    night = ["21:00", "02:30"] if product_code(code) in LATE_NIGHT_PRODUCTS else ["21:00", "23:00"]
    return [[a, b] for a, b in _DAY_WINDOWS] + [night]


def default_flatten_for_product(code: Any) -> list[str]:
    """品种没填清仓时刻时的出厂值。白银黄金用凌晨 02:25，其它用 22:55。"""
    night = "02:25" if product_code(code) in LATE_NIGHT_PRODUCTS else "22:55"
    return ["14:55", night]


def sessions_for_symbol(cfg: dict[str, Any] | None, symbol: Any) -> list[list[str]]:
    """
    当前合约实际用的交易时段。

    先看品种表这一行；空着时白银黄金用收到 02:30，其它用总配置，总配置也没有再用出厂值。
    """
    key = product_code(symbol)
    box = (((cfg or {}).get("risk") or {}).get("contracts") or {}).get(key) or {}
    parsed = parse_session_text(box.get("trading_sessions") if isinstance(box, dict) else "")
    if parsed:
        return parsed
    if key in LATE_NIGHT_PRODUCTS:
        return default_sessions_for_product(key)
    raw = ((cfg or {}).get("schedule") or {}).get("trading_sessions") or []
    out: list[list[str]] = []
    for row in raw:
        if row and len(row) >= 2:
            out.append([str(row[0]).strip(), str(row[1]).strip()])
    return out or default_sessions_for_product(key)


def flatten_times_for_symbol(cfg: dict[str, Any] | None, symbol: Any) -> list[str]:
    """当前合约实际用的清仓钟点。品种表优先，白银黄金默认 02:25。"""
    key = product_code(symbol)
    box = (((cfg or {}).get("risk") or {}).get("contracts") or {}).get(key) or {}
    parsed = parse_clock_list(box.get("flatten_times") if isinstance(box, dict) else "")
    if parsed:
        return parsed
    if key in LATE_NIGHT_PRODUCTS:
        return default_flatten_for_product(key)
    raw = ((cfg or {}).get("strategy") or {}).get("flatten_times") or []
    out = [str(x).strip()[:5] for x in raw if str(x).strip()]
    return out or default_flatten_for_product(key)


def extend_night_close(sessions: list[Any] | None) -> list[list[str]]:
    """兼容旧调用：原样整理时段，不再把所有 23:00 改成 02:30。"""
    out: list[list[str]] = []
    for row in sessions or []:
        if row and len(row) >= 2:
            out.append([str(row[0]).strip(), str(row[1]).strip()])
    return out


def in_trading_sessions(
    sessions: list[list[str]] | list[tuple[Any, Any]],
    now: datetime | None = None,
    *,
    grace_sec: float = DEFAULT_GRACE_SEC,
) -> bool:
    """配置里的 [['21:00','02:30'], ...] 是否包含现在。结束比开始早表示跨夜。"""
    stamp = now or datetime.now()
    windows: list[tuple[dtime, dtime]] = []
    for row in sessions or []:
        if not row or len(row) < 2:
            continue
        windows.append((parse_hhmm(row[0]), parse_hhmm(row[1])))
    if not windows:
        return True
    return in_session_windows(windows, stamp, grace_sec=grace_sec)


def split_day_night_sessions(
    sessions: list[list[str]] | list[tuple[Any, Any]] | None,
) -> tuple[list[list[str]], list[list[str]]]:
    """
    把品种表时段拆成日盘、夜盘。

    夜盘：开始在晚上 8 点及以后，或结束比开始早（跨零点，例如 21:00～02:30）。
    其余算日盘（含上午、午后）。
    """
    day: list[list[str]] = []
    night: list[list[str]] = []
    for row in sessions or []:
        if not row or len(row) < 2:
            continue
        start_s, end_s = str(row[0]).strip()[:5], str(row[1]).strip()[:5]
        start, end = parse_hhmm(start_s), parse_hhmm(end_s)
        if end < start or start >= dtime(20, 0):
            night.append([start_s, end_s])
        else:
            day.append([start_s, end_s])
    return day, night


def session_bounds(sessions: list[list[str]] | None) -> dict[str, dtime | None]:
    """
    从品种表时段读出日盘开/收、夜盘开/收。

    日盘开 = 日盘各段里最早的开始（商品一般 09:00，股指可能 09:30）。
    日盘收 = 日盘各段里最晚的结束（一般 15:00）。
    没有夜盘时 night_open / night_close 为空。
    """
    day, night = split_day_night_sessions(sessions)
    day_open = min((parse_hhmm(r[0]) for r in day), default=dtime(9, 0))
    day_close = max((parse_hhmm(r[1]) for r in day), default=dtime(15, 0))
    night_open: dtime | None = None
    night_close: dtime | None = None
    if night:
        night_open = min(parse_hhmm(r[0]) for r in night)
        overnight = [parse_hhmm(r[1]) for r in night if parse_hhmm(r[1]) < parse_hhmm(r[0])]
        night_close = overnight[0] if overnight else max(parse_hhmm(r[1]) for r in night)
    return {
        "day_open": day_open,
        "day_close": day_close,
        "night_open": night_open,
        "night_close": night_close,
    }


def bounds_for_symbol(cfg: dict[str, Any] | None, symbol: Any) -> dict[str, dtime | None]:
    """当前合约在品种表里的日夜盘开收。"""
    return session_bounds(sessions_for_symbol(cfg, symbol))


def session_tuples(
    cfg: dict[str, Any] | None, symbol: Any
) -> list[tuple[dtime, dtime]]:
    """策略 in_entry_session 用的 (开始, 结束) 列表。"""
    return [(parse_hhmm(a), parse_hhmm(b)) for a, b in sessions_for_symbol(cfg, symbol)]


def flatten_clocks_for_symbol(cfg: dict[str, Any] | None, symbol: Any) -> list[dtime]:
    """策略收盘清仓用的钟点列表。"""
    return [parse_hhmm(x) for x in flatten_times_for_symbol(cfg, symbol)]


def add_clock_minutes(base: dtime, minutes: int) -> dtime:
    """开盘加几分钟。夜盘 21:00 加 90 分钟是 22:30；跨天也只返回钟点。"""
    stamp = datetime(2000, 1, 1, base.hour, base.minute) + timedelta(minutes=int(minutes))
    return stamp.time()


def clock_reached(now: datetime, session_open: dtime, edge: dtime) -> bool:
    """这段开盘之后，某个钟点到了没有（含正好到点）。跨夜截止也能用。"""
    opened = last_open_at(now, session_open)
    at = datetime.combine(opened.date(), edge)
    if at <= opened:
        at += timedelta(days=1)
    return now >= at


def in_flatten_window(
    clocks: list[Any] | None,
    now: datetime | None = None,
    *,
    minutes: int = 10,
) -> bool:
    """
    现在是否落在某个清仓钟点后面的几分钟里。

    同时看昨天/今天/明天：清仓写 02:25 或 23:55 时，跨零点那几分钟也能平。
    """
    now = now or datetime.now()
    current = now.replace(second=0, microsecond=0)
    span = max(1, int(minutes))
    for item in clocks or []:
        if not isinstance(item, dtime):
            item = parse_hhmm(item)
        for days in (-1, 0, 1):
            start = datetime.combine(current.date() + timedelta(days=days), item)
            end = start + timedelta(minutes=span)
            if start <= current <= end:
                return True
    return False


def near_flatten_window(
    clocks: list[Any] | None,
    now: datetime | None = None,
    *,
    lead_minutes: int = 15,
) -> bool:
    """清仓钟点前几分钟（不含到点本身）。跨夜同样有效。"""
    if lead_minutes <= 0:
        return False
    now = now or datetime.now()
    current = now.replace(second=0, microsecond=0)
    lead = max(0, int(lead_minutes))
    for item in clocks or []:
        if not isinstance(item, dtime):
            item = parse_hhmm(item)
        for days in (-1, 0, 1):
            close_at = datetime.combine(current.date() + timedelta(days=days), item)
            begin = close_at - timedelta(minutes=lead)
            if begin <= current < close_at:
                return True
    return False


def slice_session_bars(
    frame: Any,
    day: datetime | Any,
    start: dtime,
    end: dtime,
) -> Any:
    """
    取出某一段开盘到结束之间的 K 线。结束比开始早时，结束算到次日。
    """
    import pandas as pd

    day_ts = pd.Timestamp(day).normalize()
    start_at = day_ts.replace(hour=start.hour, minute=start.minute)
    end_at = day_ts.replace(hour=end.hour, minute=end.minute)
    if end_at <= start_at:
        end_at = end_at + pd.Timedelta(days=1)
    return frame.loc[(frame.index > start_at) & (frame.index <= end_at)]
