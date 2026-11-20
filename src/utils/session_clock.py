"""
交易时段钟点。

功能：判断现在是不是在配置的交易时段里。白银、黄金夜盘会跨过零点，
     结束时间写成 02:30 时，21:00 之后和凌晨 02:30 之前都算在盘里。

原理：结束钟点比开始还早，就当成跨夜，而不是「从 21 点到凌晨 2 点中间是空的」。
     时段刚结束再留几十秒，免得最后一根刚收完就被当成休市。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time as dtime, timedelta
from functools import lru_cache
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


# 贵金属、原油夜盘收到次日凌晨。品种表没填时段时用这一套。
LATE_NIGHT_PRODUCTS = frozenset({"ag", "au", "sc"})
# 沪铜等有色、国际铜夜盘到 01:00。
NIGHT_0100_PRODUCTS = frozenset("cu al zn pb ni sn ss bc".split())
# 中金所：日盘 09:30～11:30、13:00～15:00，没有上午休息、没有夜盘。
CFFEX_PRODUCTS = frozenset({"if", "ih", "ic", "im", "t", "tf", "ts", "tl"})
# 生猪、鸡蛋、广期所工业硅、集运指数等没有夜盘（股指国债走 CFFEX，不走这里的日盘骨架）。
NO_NIGHT_PRODUCTS = frozenset(
    {"lh", "jd", "fb", "bb", "rr", "si", "lc", "ps", "ap", "cj", "ec"}
)
_DAY_WINDOWS = (("09:00", "11:30"), ("13:30", "15:00"))
_CFFEX_DAY_WINDOWS = (("09:30", "11:30"), ("13:00", "15:00"))


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
    """品种没填时段时的出厂值。按夜盘组别分：无夜盘 / 23:00 / 01:00 / 02:30。"""
    key = product_code(code)
    if key in CFFEX_PRODUCTS:
        return [[a, b] for a, b in _CFFEX_DAY_WINDOWS]
    day = [[a, b] for a, b in _DAY_WINDOWS]
    if key in NO_NIGHT_PRODUCTS:
        return day
    if key in LATE_NIGHT_PRODUCTS:
        return day + [["21:00", "02:30"]]
    if key in NIGHT_0100_PRODUCTS:
        return day + [["21:00", "01:00"]]
    return day + [["21:00", "23:00"]]


def _minus_minutes(clock: str, minutes: int) -> str:
    """15:00 往前 5 分钟 → 14:55。"""
    stamp = datetime.combine(datetime(2000, 1, 1).date(), parse_hhmm(clock)) - timedelta(minutes=minutes)
    return stamp.strftime("%H:%M")


def flatten_times_from_sessions(sessions: list[list[str]] | None) -> list[str]:
    """
    按交易时段推清仓钟点：日盘收 15:00 就 14:55 清，夜盘收到几点就提前 5 分钟。

    这样做是为了让「清仓时刻」只跟品种时段走，代码里不再另存一套 14:55/22:55/02:25 的硬编码表。
    """
    day, night = split_day_night_sessions(sessions)
    out: list[str] = []
    if day:
        out.append(_minus_minutes(day[-1][1], 5))
    if night:
        out.append(_minus_minutes(night[-1][1], 5))
    return out


def default_flatten_for_product(code: Any) -> list[str]:
    """品种没填清仓时刻时的出厂值：由这个品种的出厂时段推出来。"""
    return flatten_times_from_sessions(default_sessions_for_product(code))


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
    """
    当前合约实际用的清仓钟点。

    品种表这一行有值就以它为准（回测、实盘、复盘对照都读这一处）；
    没填时才看策略页的旧字段，再没有就按这个品种的交易时段推「收盘前 5 分钟」。
    """
    key = product_code(symbol)
    box = (((cfg or {}).get("risk") or {}).get("contracts") or {}).get(key) or {}
    parsed = parse_clock_list(box.get("flatten_times") if isinstance(box, dict) else "")
    if parsed:
        return parsed
    raw = ((cfg or {}).get("strategy") or {}).get("flatten_times") or []
    out = [str(x).strip()[:5] for x in raw if str(x).strip()]
    if out:
        return out
    return flatten_times_from_sessions(sessions_for_symbol(cfg, symbol))


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
    sessions: Sequence[Sequence[Any]] | None,
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


def _session_rows_key(
    sessions: Sequence[Sequence[Any]] | None,
) -> tuple[tuple[str, str], ...]:
    """把时段表收成可缓存的键：只看文字，和调用方传的容器无关。"""
    rows: list[tuple[str, str]] = []
    for row in sessions or []:
        if not row or len(row) < 2:
            continue
        rows.append((str(row[0]).strip()[:5], str(row[1]).strip()[:5]))
    return tuple(rows)


@lru_cache(maxsize=128)
def _session_bounds_cached(
    key: tuple[tuple[str, str], ...],
) -> tuple[dtime, dtime, dtime | None, dtime | None]:
    """同一份时段文字只解析一次开收钟点，品种表没改就不重复拆字符串。"""
    day, night = split_day_night_sessions(key)
    day_open = min((parse_hhmm(r[0]) for r in day), default=dtime(9, 0))
    day_close = max((parse_hhmm(r[1]) for r in day), default=dtime(15, 0))
    night_open: dtime | None = None
    night_close: dtime | None = None
    if night:
        night_open = min(parse_hhmm(r[0]) for r in night)
        overnight = [parse_hhmm(r[1]) for r in night if parse_hhmm(r[1]) < parse_hhmm(r[0])]
        night_close = overnight[0] if overnight else max(parse_hhmm(r[1]) for r in night)
    return day_open, day_close, night_open, night_close


def session_bounds(sessions: Sequence[Sequence[Any]] | None) -> dict[str, dtime | None]:
    """
    从品种表时段读出日盘开/收、夜盘开/收。

    日盘开 = 日盘各段里最早的开始（商品一般 09:00，股指可能 09:30）。
    日盘收 = 日盘各段里最晚的结束（一般 15:00）。
    没有夜盘时 night_open / night_close 为空。
    """
    day_open, day_close, night_open, night_close = _session_bounds_cached(
        _session_rows_key(sessions)
    )
    return {
        "day_open": day_open,
        "day_close": day_close,
        "night_open": night_open,
        "night_close": night_close,
    }


def bounds_for_symbol(cfg: dict[str, Any] | None, symbol: Any) -> dict[str, dtime | None]:
    """当前合约在品种表里的日夜盘开收。"""
    return session_bounds(sessions_for_symbol(cfg, symbol))


def near_session_close(
    now: datetime | None,
    sessions: list[list[str]] | list[tuple[Any, Any]] | None,
    *,
    minutes: int = 10,
) -> bool:
    """
    是不是已经到了「这一段真正收盘」前的几分钟。

    日盘收盘用下午最晚那一下（一般 15:00），夜盘收盘用夜盘结束（23:00 或 02:30）。
    上午 11:30 是午休，不是收盘，这里不算。
    已经过了收盘钟点也算「太晚了」，空仓时不要再开。
    """
    if minutes <= 0:
        return False
    stamp = now or datetime.now()
    stamp = stamp.replace(microsecond=0)
    day, night = split_day_night_sessions(sessions)
    bounds = session_bounds(sessions)
    # 判断现在落在日盘还是夜盘，不用宽限秒，免得刚收盘还当成「还早」。
    in_day = in_trading_sessions(day, stamp, grace_sec=0)
    in_night = in_trading_sessions(night, stamp, grace_sec=0)
    close: dtime | None = None
    overnight = False
    night_open: dtime | None = None
    if in_day:
        close = bounds.get("day_close")
    elif in_night:
        close = bounds.get("night_close")
        night_open = bounds.get("night_open")
        if close is not None and night_open is not None and close < night_open:
            overnight = True
    if close is None:
        return False
    if overnight:
        if night_open is not None and stamp.time() >= night_open:
            close_at = datetime.combine(stamp.date() + timedelta(days=1), close)
        else:
            close_at = datetime.combine(stamp.date(), close)
    else:
        close_at = datetime.combine(stamp.date(), close)
    begin = close_at - timedelta(minutes=int(minutes))
    return stamp >= begin


def session_tuples(
    cfg: dict[str, Any] | None, symbol: Any
) -> list[tuple[dtime, dtime]]:
    """策略 in_entry_session 用的 (开始, 结束) 列表。"""
    return [(parse_hhmm(a), parse_hhmm(b)) for a, b in sessions_for_symbol(cfg, symbol)]


# 中金所没有 10:15～10:30 休息；商品期货上午要拆成两段。
_TEA_BREAK_START = dtime(10, 15)
_TEA_BREAK_RESUME = dtime(10, 30)


def has_commodity_tea_break(symbol: Any) -> bool:
    """商品期货上午 10:15～10:30 休息；股指国债没有这段。"""
    return product_code(symbol) not in CFFEX_PRODUCTS


def split_tea_break(
    sessions: Sequence[Sequence[Any]] | None,
    *,
    symbol: Any = "",
) -> list[list[str]]:
    """
    把「09:00～11:30」拆成 09:00～10:15 和 10:30～11:30。

    品种表通常写成一整段上午，但 10:15 之后到 10:30 是休市，
    这段时间的 K 线不能拿来开仓。中金所不拆。没给品种时按商品拆，
    因为回测默认品种都是商品。
    """
    rows: list[list[str]] = []
    for row in sessions or []:
        if not row or len(row) < 2:
            continue
        rows.append([str(row[0]).strip()[:5], str(row[1]).strip()[:5]])
    if symbol and not has_commodity_tea_break(symbol):
        return rows
    out: list[list[str]] = []
    for start_s, end_s in rows:
        start, end = parse_hhmm(start_s), parse_hhmm(end_s)
        covers_tea = start < _TEA_BREAK_START and end >= _TEA_BREAK_RESUME and start <= end
        if covers_tea:
            out.append([start_s, "10:15"])
            out.append(["10:30", end_s])
        else:
            out.append([start_s, end_s])
    return out


def tradable_windows_for_symbol(
    cfg: dict[str, Any] | None, symbol: Any
) -> list[tuple[dtime, dtime]]:
    """开仓/成交用的时段：品种表 + 商品上午休息拆段。"""
    rows = split_tea_break(sessions_for_symbol(cfg, symbol), symbol=symbol)
    return [(parse_hhmm(a), parse_hhmm(b)) for a, b in rows]


def _stamp_to_naive(ts: Any) -> datetime | None:
    """K 线时间收成不带时区的本地时间。"""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        stamp = ts
    else:
        try:
            stamp = pd_timestamp_to_datetime(ts)
        except Exception:  # noqa: BLE001
            return None
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return stamp.replace(second=0, microsecond=0)


def pd_timestamp_to_datetime(ts: Any) -> datetime:
    """pandas 时间戳转 datetime；没有 pandas 时走 fromisoformat。"""
    try:
        import pandas as pd

        return pd.Timestamp(ts).to_pydatetime()
    except Exception:  # noqa: BLE001
        text = str(ts)
        return datetime.fromisoformat(text.replace("Z", ""))


def bar_close_in_windows(
    stamp: datetime,
    windows: Sequence[tuple[dtime, dtime]],
) -> bool:
    """
    这根 K 线的结束时刻能不能当成真实行情。

    右标签记的是「这段走完的时刻」：开盘那一刻还没走完一根，不算；
    收盘那一刻是最后一根，算。所以用「开始 < 时刻 ≤ 结束」。
    跨夜时段同样：21:00 开盘那根丢掉，02:30 收盘那根留下。
    """
    clock = stamp.time().replace(second=0, microsecond=0)
    for start, end in windows:
        if start <= end:
            if start < clock <= end:
                return True
        elif clock > start or clock <= end:
            return True
    return False


def _resolve_windows(
    *,
    symbol: Any = "",
    cfg: dict[str, Any] | None = None,
    windows: Sequence[tuple[dtime, dtime]] | None = None,
) -> Sequence[tuple[dtime, dtime]] | None:
    """没给品种也没给时段时返回 None，表示不过滤。"""
    if windows is not None:
        return windows
    if not symbol and not cfg:
        return None
    return tradable_windows_for_symbol(cfg, symbol)


def bar_stamp_tradable(
    ts: Any,
    *,
    symbol: Any = "",
    cfg: dict[str, Any] | None = None,
    windows: Sequence[tuple[dtime, dtime]] | None = None,
) -> bool:
    """
    这个结束时刻能不能当成一根已经走完的 K 线。

    开盘那一刻还没走完一根（21:00），不算。午休 12:00 也不算。
    没给品种、也没给时段时不过滤，方便测试用假 K 线。
    """
    resolved = _resolve_windows(symbol=symbol, cfg=cfg, windows=windows)
    if resolved is None:
        return True
    stamp = _stamp_to_naive(ts)
    if stamp is None:
        return False
    return bar_close_in_windows(stamp, resolved)


def in_session_break(ts: Any, *, symbol: Any = "") -> bool:
    """
    是不是落在大家都知道的休市空档里。

    商品：10:15～10:30 上午休息，11:30～13:30 午休。
    中金所：只有 11:30～13:00 午休。
    夜盘开盘 21:00 不是空档。配置里如果把日盘写成 09:00～15:00 一整段，
    也不能把 21:00 当成休市丢掉。
    """
    stamp = _stamp_to_naive(ts)
    if stamp is None:
        return False
    clock = stamp.time().replace(second=0, microsecond=0)
    if has_commodity_tea_break(symbol):
        if dtime(10, 15) < clock < dtime(10, 30):
            return True
        if dtime(11, 30) < clock < dtime(13, 30):
            return True
        return False
    return dtime(11, 30) < clock < dtime(13, 0)


def bar_stamp_fillable(
    ts: Any,
    *,
    symbol: Any = "",
    cfg: dict[str, Any] | None = None,
    windows: Sequence[tuple[dtime, dtime]] | None = None,
) -> bool:
    """
    这个时刻能不能当下一根开盘的成交时间。

    午休、上午休息中间的整点不行。这段的开盘（10:30、13:30、21:00）可以。
    生猪没有夜盘，21:00 不能成交。没给品种时不过滤。
    """
    if not symbol and not cfg and windows is None:
        return True
    if in_session_break(ts, symbol=symbol):
        return False
    stamp = _stamp_to_naive(ts)
    if stamp is None:
        return False
    clock = stamp.time().replace(second=0, microsecond=0)
    # 品种出厂时段的开盘（10:30、13:30、21:00）可以成交。
    # 生猪没有夜盘，21:00 不会出现在这里。配置把日盘写成 09:00～15:00 时，
    # 仍允许夜盘开盘成交，免得回测把燃油当晚 21:00 那根跳过。
    for start, _end in tradable_windows_for_symbol(None, symbol):
        if clock == start:
            return True
    resolved = _resolve_windows(symbol=symbol, cfg=cfg, windows=windows)
    if resolved is None:
        return True
    for start, end in resolved:
        if clock == start:
            return True
        if in_clock_window(start, end, stamp, grace_sec=0):
            return True
    return False


def tradable_stamp_mask(
    hours: Any,
    minutes: Any,
    windows: Sequence[tuple[dtime, dtime]],
) -> Any:
    """整列判断哪些右标签落在可交易时段里。hours/minutes 是 numpy 数组。"""
    import numpy as np

    total = np.asarray(hours, dtype=np.int64) * 60 + np.asarray(minutes, dtype=np.int64)
    ok = np.zeros(len(total), dtype=bool)
    for start, end in windows:
        start_m = start.hour * 60 + start.minute
        end_m = end.hour * 60 + end.minute
        if start <= end:
            ok |= (total > start_m) & (total <= end_m)
        else:
            ok |= (total > start_m) | (total <= end_m)
    return ok


def session_close_minutes(
    windows: Sequence[tuple[dtime, dtime]],
) -> tuple[int, ...]:
    """各段结束时刻的分钟数，用来保留 11:30 这种不足整小时的收盘 K 线。"""
    out: list[int] = []
    for _start, end in windows:
        out.append(end.hour * 60 + end.minute)
    return tuple(out)


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
