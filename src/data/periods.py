"""
K 线周期。

配置和界面里可以写分钟数，也可以写日线、周线。
这里把这些写法收成同一种代码，后面拉行情、合成 K 线都用这个代码。

代码约定：
- "15" 表示 15 分钟
- "60" 表示 60 分钟
- "D" 表示日线
- "W" 表示周线
- "120" 这种不在下拉里的数字，表示自定义分钟
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# 程序里统一的 K 线列。各家下载之后都收成这一张表，避免换源时列名、时区对不上。
OHLC_COLUMNS = ("open", "high", "low", "close", "volume")


def canonicalize_ohlc(
    frame: pd.DataFrame | None,
    *,
    period: str | None = None,
) -> pd.DataFrame | None:
    """
    把任意一家刚下来的表收成同一口径。

    - 时间索引：不带时区的北京时间，列名 datetime，精确到秒。
    - 分钟、小时：右标签（这段结束时刻），例如 09:00–09:05 记成 09:05。
    - 日线：交易日 00:00（不再加一天）。
    - 列：open / high / low / close，有成交量就留 volume，有持仓量就留 oi。

    这里不改开高低收的数字，只整理格式。脏时间戳仍交给 aligned_bars 丢掉。
    """
    if frame is None or len(frame) == 0:
        return frame
    out = unique_ohlc_columns(frame)
    if out is None or len(out) == 0:
        return out
    if not isinstance(out.index, pd.DatetimeIndex):
        out = out.copy()
        out.index = pd.to_datetime(out.index)
    index = pd.DatetimeIndex(out.index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Shanghai").tz_localize(None)
    index = index.floor("s")
    code = canonical_period(period) if period else ""
    if code == "D":
        index = index.normalize()
    out = out.copy()
    out.index = index
    out.index.name = "datetime"
    rename = {}
    cols = {str(name).lower(): name for name in out.columns}
    for need in (*OHLC_COLUMNS, "oi"):
        if need in cols and cols[need] != need:
            rename[cols[need]] = need
    if rename:
        out = out.rename(columns=rename)
    keep = [name for name in (*OHLC_COLUMNS, "oi") if name in out.columns]
    if "close" not in keep:
        return out.sort_index()
    out = out[keep]
    for name in keep:
        out[name] = pd.to_numeric(out[name], errors="coerce")
    out = out.dropna(subset=["close"])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def unique_ohlc_columns(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """
    同一列名出现两次时只留最后一列。

    新浪有时同时带回单字母列（c）和英文列（close），整理后会变成两列都叫 close。
    后面如果写成 if 收盘价，Python 会把一整列数字当成「是不是」，报
    「truth value of an array is ambiguous」。
    """
    if frame is None or len(frame) == 0:
        return frame
    columns = getattr(frame, "columns", None)
    if columns is None:
        return frame
    if not columns.duplicated().any():
        return frame
    return frame.loc[:, ~columns.duplicated(keep="last")]


def ohlc_series(frame: pd.DataFrame | None, name: str) -> pd.Series:
    """取出开/高/低/收/量的一列数字。列名重复时用最后一列。"""
    if frame is None or name not in getattr(frame, "columns", []):
        return pd.Series(dtype=float)
    col = frame[name]
    if isinstance(col, pd.DataFrame):
        col = col.iloc[:, -1]
    # 已经是数字列时不用再转：pd.to_numeric 对数字列本来也是原样返回，
    # 但每根 K 线都调一次，省下的转换在回测里很可观。
    if isinstance(col, pd.Series) and col.dtype.kind in "fiu":
        return col
    return pd.to_numeric(col, errors="coerce")


def last_ohlc_price(frame: pd.DataFrame | None, name: str) -> float | None:
    """最新一根的开/高/低/收。拿不到或一格里挤了多个数就返回 None。"""
    series = ohlc_series(frame, name)
    if series.empty:
        return None
    # 数字列不可能一格挤多个数，直接看最后一个；是 NaN 才退回原来的 dropna 路径。
    if series.dtype.kind in "fiu":
        candidate = series.iloc[-1]
        if candidate == candidate:
            return float(candidate)
    series = series.dropna()
    if series.empty:
        return None
    raw = np.asarray(series.iloc[-1], dtype=float).reshape(-1)
    if raw.size != 1:
        return None
    value = float(raw[0])
    if value != value:
        return None
    return value

# 新浪分钟接口直接能给的档。别的分钟数要用其中一档再合成。
SINA_MINUTES = ("1", "5", "15", "30", "60")

# 下拉里给人看的名字。顺序就是界面上的顺序。
PERIOD_LABELS = {
    "1": "1分钟",
    "5": "5分钟",
    "15": "15分钟",
    "30": "30分钟",
    "60": "60分钟",
    "D": "日线",
    "W": "周线",
}
PERIOD_MENU = list(PERIOD_LABELS.values()) + ["自定义"]

# 国内商品常见开盘（用于判断「这一段刚开盘，分钟可能不满」）
# 10:30 是上午休息后的开盘，不是上一根的收盘。
_SESSION_OPEN_MINUTES = (9 * 60, 9 * 60 + 30, 10 * 60 + 30, 13 * 60, 13 * 60 + 30, 21 * 60)
# 国内商品常见收盘（用于判断「这一段要收盘，最后一根可能不足整周期」）
_SESSION_CLOSE_MINUTES = (
    10 * 60 + 15,
    11 * 60 + 30,
    15 * 60,
    23 * 60,
    1 * 60,
    2 * 60 + 30,
)


def canonical_period(text: str, custom: str = "") -> str:
    """
    把「15分钟」「日线」「120」「4小时」收成 15 / D / 120 / 240。

    选了「自定义」时，真正的数字在 custom 里。空的或看不懂的，退回 15，避免程序崩掉。
    """
    raw = str(text or "").strip().replace(" ", "")
    if raw in ("自定义", "custom"):
        raw = str(custom or "").strip().replace(" ", "")
    if not raw:
        return "15"
    key = raw.lower()
    aliases = {
        "日": "D",
        "日线": "D",
        "d": "D",
        "1d": "D",
        "daily": "D",
        "周": "W",
        "周线": "W",
        "w": "W",
        "1w": "W",
        "weekly": "W",
    }
    if key in aliases:
        return aliases[key]
    for suffix, mult in (("小时", 60), ("时", 60), ("h", 60)):
        if key.endswith(suffix) and key[: -len(suffix)].isdigit():
            number = int(key[: -len(suffix)])
            if number > 0:
                return str(number * mult)
    for suffix in ("分钟", "分", "min", "m"):
        if key.endswith(suffix) and key[: -len(suffix)].isdigit():
            number = int(key[: -len(suffix)])
            if number > 0:
                return str(number)
    if key.isdigit() and int(key) > 0:
        return str(int(key))
    return "15"


def ui_parts(code: str) -> tuple[str, str]:
    """配置里的周期拆成下拉框文字，和自定义框里的分钟数。"""
    period = canonical_period(code)
    if period in PERIOD_LABELS:
        return PERIOD_LABELS[period], ""
    return "自定义", period


def period_label(code: str) -> str:
    """给人看的名字。120 显示成「120分钟」，不会把日线说成多少分。"""
    period = canonical_period(code)
    if period in PERIOD_LABELS:
        return PERIOD_LABELS[period]
    return f"{period}分钟"


def period_rank(code: str) -> float:
    """用分钟数比谁更慢。日线按 1440，周线按 7 天。只用来比较，不当成真实交易分钟。"""
    period = canonical_period(code)
    if period == "D":
        return 24 * 60
    if period == "W":
        return 7 * 24 * 60
    return float(int(period))


def trend_period_for(signal: str, configured: str = "60") -> str:
    """
    公允价值缺口看方向时，方向周期必须比信号更慢。

    信号还是 15、30 这种时，仍然用配置里的 60 分钟。
    信号已经选了 60 分钟或更慢，60 分钟就不再是更大的周期，自动改成日线或周线。
    """
    signal_code = canonical_period(signal)
    trend_code = canonical_period(configured)
    if period_rank(trend_code) > period_rank(signal_code):
        return trend_code
    if period_rank(signal_code) < 24 * 60:
        return "D"
    if period_rank(signal_code) < 7 * 24 * 60:
        return "W"
    return "W"


def minute_base(period: str) -> str:
    """
    自定义分钟先拉新浪能直接给的那一档，再合成。

    120 分钟用 60 分钟合成。45 分钟用 15 分钟合成。
    """
    period = canonical_period(period)
    if period in ("D", "W"):
        raise ValueError("日线和周线不用分钟接口")
    number = int(period)
    natives = (60, 30, 15, 5, 1)
    if number in natives:
        return str(number)
    for base in natives:
        if number > base and number % base == 0:
            return str(base)
    for base in natives:
        if base < number:
            return str(base)
    return "1"


def pandas_rule(period: str) -> str:
    """pandas 重采样用的规则。周线收到周五，和国内期货一周的交易日对齐。"""
    period = canonical_period(period)
    if period == "D":
        return "1D"
    if period == "W":
        return "W-FRI"
    return f"{int(period)}min"


def cache_tag(period: str) -> str:
    """缓存文件名的后缀。60 分钟仍是 60m，和原来的文件对得上。"""
    period = canonical_period(period)
    if period == "D":
        return "1d"
    if period == "W":
        return "1w"
    return f"{period}m"


def csv_skip_marker(csv_path: Path) -> Path:
    """
    删库后这份 CSV 还被 Excel 开着改不掉时，在旁边放一个 .skip 标记。

    下次读图或拉行情看到这个标记，就不会把旧 CSV 再补进清洗库。
    CSV 真正删掉或成功写成新内容后，把标记去掉。
    """
    return Path(csv_path).with_name(Path(csv_path).name + ".skip")


def stale_after_minutes(period: str, base: float = 90.0) -> float:
    """
    多久没更新才算行情过期。

    15 分钟仍按原来的大约 90 分钟。
    日线如果还用 90 分钟，昨天的日线会被当成过期，开不了仓。
    """
    period = canonical_period(period)
    if period == "D":
        return max(float(base), 2 * 24 * 60)
    if period == "W":
        return max(float(base), 10 * 24 * 60)
    return max(float(base), int(period) * 2.0)


def resample_ohlc(df: pd.DataFrame | None, period: str) -> pd.DataFrame | None:
    """把更细的 K 线收成目标周期。开盘取第一根，最高最低取极值，收盘取最后一根。"""
    if df is None or len(df) == 0:
        return df
    df = unique_ohlc_columns(df)
    if df is None:
        return None
    rule = pandas_rule(period)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    out = (
        df.resample(rule, label="right", closed="right")
        .agg(agg)
        .dropna(subset=["close"])
    )
    return out


def trading_segments_for_symbol(symbol: Any = "") -> tuple[tuple[dtime, dtime], ...]:
    """
    1 分钟哪些时刻算在盘里。

    商品上午拆成 09:00～10:15、10:30～11:30，午休、夜盘结束到次日开盘都断开。
    夜盘组别跟品种走：无夜盘 / 23:00 / 01:00 / 02:30；中金所没有上午休息。
    """
    from src.utils.session_clock import (
        default_sessions_for_product,
        parse_hhmm,
        split_tea_break,
    )

    sessions = split_tea_break(default_sessions_for_product(symbol), symbol=symbol)
    return tuple((parse_hhmm(a), parse_hhmm(b)) for a, b in sessions if a and b)


def _clock_in_segments(stamp: datetime, segments: tuple[tuple[dtime, dtime], ...]) -> bool:
    """右标签 1 分钟：开盘那一分钟不算，收盘那一分钟算。"""
    hm = stamp.hour * 60 + stamp.minute
    for start, end in segments:
        start_m = start.hour * 60 + start.minute
        end_m = end.hour * 60 + end.minute
        if start <= end:
            if start_m < hm <= end_m:
                return True
        elif hm > start_m or hm <= end_m:
            return True
    return False


def _session_kind(stamp: datetime) -> str:
    """日盘 / 夜盘。夜盘可跨零点。"""
    hour = stamp.hour
    if hour >= 20 or hour < 8:
        return "night"
    return "day"


def _emit_bucket(rows: list[pd.Series], columns: list[str]) -> dict[str, Any]:
    """一桶 1 分钟收成一根：开=第一根开，收=最后一根收，高最高，低最低。"""
    frame = pd.DataFrame(rows)
    out: dict[str, Any] = {
        "open": float(frame["open"].iloc[0]),
        "high": float(frame["high"].max()),
        "low": float(frame["low"].min()),
        "close": float(frame["close"].iloc[-1]),
        "minute_count": int(len(frame)),
    }
    if "volume" in columns:
        out["volume"] = float(pd.to_numeric(frame["volume"], errors="coerce").fillna(0).sum())
    if "oi" in columns:
        oi = pd.to_numeric(frame["oi"], errors="coerce")
        out["oi"] = float(oi.iloc[-1]) if len(oi) and pd.notna(oi.iloc[-1]) else None
    return out


def _segment_for_stamp(
    stamp: datetime,
    segments: tuple[tuple[dtime, dtime], ...],
) -> tuple[dtime, dtime] | None:
    """这根 1 分钟落在哪一段。开盘那一分钟（09:00/10:30/13:30/21:00）并进这一段第一根。"""
    hm = stamp.hour * 60 + stamp.minute
    for start, end in segments:
        start_m = start.hour * 60 + start.minute
        if hm == start_m:
            return start, end
        if _clock_in_segments(stamp, ((start, end),)):
            return start, end
    return None


def _segment_end_at(stamp: datetime, start: dtime, end: dtime) -> datetime:
    """这段真正收盘的墙上时间。夜盘跨零点时，凌晨看的是当天结束钟点。"""
    if start <= end:
        return datetime.combine(stamp.date(), end)
    if stamp.time() >= start:
        return datetime.combine(stamp.date() + timedelta(days=1), end)
    return datetime.combine(stamp.date(), end)


def _next_clock_close(stamp: datetime, step: int) -> datetime:
    """下一根墙上整点格子：5/15/30 看分钟，60 看整点。"""
    t = stamp.replace(second=0, microsecond=0)
    hm = t.hour * 60 + t.minute
    rem = hm % step
    if rem == 0:
        return t
    return t + timedelta(minutes=step - rem)


def _bar_close_for(
    stamp: datetime,
    step: int,
    start: dtime,
    end: dtime,
) -> datetime:
    """
    这根 1 分钟该收到哪一根 K 上。

    先看墙上整点，再和这段收盘取较早的那个。
    这样 10:00～10:15 不会并进 11:00，11:00～11:30 也不会并进下午。
    """
    # 开盘时刻本身是 00 分时，不能收成开盘那根，要并进第一根完整格子。
    if stamp.hour == start.hour and stamp.minute == start.minute:
        stamp = stamp + timedelta(minutes=1)
    clock = _next_clock_close(stamp, step)
    return min(clock, _segment_end_at(stamp, start, end))


def minute_in_session(stamp: datetime, symbol: Any = "") -> bool:
    """这根 1 分钟（含开盘那一分钟）是不是在交易时段里。"""
    naive = stamp.replace(second=0, microsecond=0)
    if getattr(naive, "tzinfo", None) is not None:
        naive = naive.replace(tzinfo=None)
    return _segment_for_stamp(naive, trading_segments_for_symbol(symbol)) is not None


def session_close_times(
    day: datetime | date,
    step: int,
    *,
    symbol: Any = "",
    include_night: bool = True,
) -> list[datetime]:
    """
    这一天按交易时段应该出现的 K 线结束时刻。

    和拼 K 同一套：段内墙上时钟，段尾截断。休市不生成。
    夜盘跨零点时，02:30 落在次日凌晨。
    """
    if isinstance(day, datetime):
        day = day.date()
    step = max(int(step), 1)
    segments = list(trading_segments_for_symbol(symbol))
    if not include_night:
        segments = [(start, end) for start, end in segments if start <= end and start.hour < 16]
    closes: list[datetime] = []
    for start, end in segments:
        if start <= end:
            begin = datetime.combine(day, start)
            finish = datetime.combine(day, end)
        else:
            begin = datetime.combine(day, start)
            finish = datetime.combine(day + timedelta(days=1), end)
        cursor = begin
        while cursor < finish:
            nxt = _next_clock_close(cursor + timedelta(minutes=1), step)
            if nxt > finish:
                nxt = finish
            if nxt <= cursor:
                break
            closes.append(nxt)
            cursor = nxt
    return closes


def bars_from_session_clock(
    minutes: pd.DataFrame,
    step: int,
    *,
    symbol: str = "",
) -> pd.DataFrame:
    """
    按「每一段交易时段内部的自然时钟」收线，休市断开。

    商品日盘 60 分钟：10:00、10:15（短）、11:00（短）、11:30（短）、14:00（短）、15:00。
    没有 12:00、13:00，也不会把上午最后几分钟并进下午那根。
    时间戳是结束时刻（右标签）。09:00、10:30、13:30、21:00 是开盘，不是收线。

    整列用 numpy 分组，不用逐根 Python 循环。一年的 1 分钟也能在一秒内拼完，
    否则历史回测会在「请稍候」停很久。
    """
    step = max(int(step), 1)
    segments = trading_segments_for_symbol(symbol)
    data = minutes.sort_index()
    columns = [name for name in ("open", "high", "low", "close", "volume", "oi") if name in data.columns]
    if not columns or "close" not in columns:
        return pd.DataFrame(columns=list(dict.fromkeys(columns + ["minute_count"])))

    index = pd.DatetimeIndex(data.index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Shanghai").tz_localize(None)
    index = index.floor("min")
    hm = (index.hour * 60 + index.minute).to_numpy(dtype=np.int64)
    n = len(index)
    close_hm = np.full(n, -1, dtype=np.int64)
    extra_days = np.zeros(n, dtype=np.int64)

    for start, end in segments:
        start_m = start.hour * 60 + start.minute
        end_m = end.hour * 60 + end.minute
        overnight = start > end
        if overnight:
            mask = (hm == start_m) | (hm > start_m) | (hm <= end_m)
        else:
            mask = (hm == start_m) | ((hm > start_m) & (hm <= end_m))
        if not mask.any():
            continue
        work = hm[mask].copy()
        # 开盘那一分钟并进第一根，不要单独收成 09:00 / 21:00。
        work = np.where(work == start_m, work + 1, work)
        rem = work % step
        clock = np.where(rem == 0, work, work + (step - rem))
        cap = np.where(overnight & (hm[mask] >= start_m), 1440 + end_m, end_m)
        capped = np.minimum(clock, cap)
        days, of_day = np.divmod(capped, 1440)
        close_hm[mask] = of_day
        extra_days[mask] = days

    valid = close_hm >= 0
    if not valid.any():
        return pd.DataFrame(columns=list(dict.fromkeys(columns + ["minute_count"])))

    midnight = index[valid].normalize()
    keys = midnight + pd.to_timedelta(extra_days[valid], unit="D") + pd.to_timedelta(
        close_hm[valid], unit="m"
    )
    kept = data.loc[valid, columns].copy()
    kept.index = pd.DatetimeIndex(keys, name="datetime")
    grouped = kept.groupby(level=0, sort=True)
    agg: dict[str, str] = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in columns:
        agg["volume"] = "sum"
    if "oi" in columns:
        agg["oi"] = "last"
    out = grouped.agg(agg)
    out["minute_count"] = grouped["close"].count()
    out = out.dropna(subset=["close"])
    out.index.name = "datetime"
    return out.sort_index()


def bars_from_trading_minutes(
    minutes: pd.DataFrame,
    step: int,
    *,
    symbol: str = "",
) -> pd.DataFrame:
    """旧名字：以前按交易分钟凑满 60 根，会跨午休。现在改走时段内时钟。"""
    return bars_from_session_clock(minutes, step, symbol=symbol)


def _min_minutes_for_bar(step: int) -> int:
    """
    一根 K 至少要有多少根 1 分钟才算可用。

    夜盘刚开时常缺第一分钟，15 分钟可能只有 13～14 根；仍要留下，否则同花顺有针、本地没有。
    """
    step = max(1, int(step))
    if step <= 5:
        return max(1, step - 1)
    return max(1, step - 2)


def _session_open_edge_mask(index: pd.DatetimeIndex, step: int) -> np.ndarray:
    """
    这些右标签时间里，哪些是某段开盘后的第一根。

    例如 15 分钟：09:15、13:45、21:15；5 分钟：09:05、13:35、21:05。
    整批判断，不要逐根转 Timestamp，重建 1000 根时那一步很贵。
    """
    step = int(step)
    end_m = np.asarray(index.hour * 60 + index.minute, dtype=np.int64)
    mask = np.zeros(len(end_m), dtype=bool)
    for open_m in _SESSION_OPEN_MINUTES:
        mask |= end_m == open_m + step
        # 开盘时刻本身整除周期时（如 21:00 的 60 分钟），也算开盘边
        if open_m % step == 0:
            mask |= end_m == open_m
    return mask


def _session_close_edge_mask(index: pd.DatetimeIndex, step: int) -> np.ndarray:
    """
    这些右标签时间里，哪些是某段收盘前的最后一根。

    例如 15 分钟：10:15、11:30、15:00；30/60 分钟在这些时刻前面可能不足整周期，
    整根丢弃会让 K 线根数少于行情商。收盘边的半桶也要保留。
    """
    step = int(step)
    end_m = np.asarray(index.hour * 60 + index.minute, dtype=np.int64)
    mask = np.zeros(len(end_m), dtype=bool)
    for close_m in _SESSION_CLOSE_MINUTES:
        mask |= end_m == close_m
        # 收盘时刻本身整除周期时（如 15:00 的 60 分钟），也算收盘边
        if close_m % step == 0:
            mask |= end_m == close_m
    return mask


def is_session_open_stamp(ts: Any, opens: tuple[int, ...]) -> bool:
    """
    这个右标签时刻是不是正好落在某段开盘上。

    右标签记的是「结束时刻」：08:55～09:00、13:25～13:30、20:55～21:00 还没开盘，
    不可能有 K 线结束在这里。库里出现这种时间戳，都是早期左标签留下的旧戳。
    opens 是这一段交易时段的开盘分钟数：商品是 09:00/13:30/21:00，中金所是 09:30/13:00。
    """
    stamp = pd.Timestamp(ts)
    return (stamp.hour * 60 + stamp.minute) in opens


def bars_from_minutes(
    minutes: pd.DataFrame | None,
    period: str,
    *,
    symbol: str = "",
) -> pd.DataFrame | None:
    """
    用 1 分钟拼出更长的 K 线。

    时间戳用这一段的结束时刻（右标签）：
    15 分钟的 21:15 = 约 21:00～21:15；5 分钟的 11:15 = 11:10～11:15。
    5/15/30/60 都按交易时段内的自然时钟收，大休市断开，不把 10:15～10:30、
    11:30～13:30 并进同一根。60 分钟日盘是 10:00、10:15、11:00、11:30、14:00、15:00。
    最后一段分钟还没齐也先留着，扫描时当未收盘。
    开盘第一根允许略缺几分钟（夜盘常见），避免整根被丢掉。
    """
    if minutes is None or len(minutes) == 0:
        return minutes
    step = int(canonical_period(period))
    if step <= 1:
        return minutes
    if step >= 5:
        return bars_from_session_clock(minutes, step, symbol=symbol)
    data = minutes.sort_index()
    # 右闭右标签：与 resample_ohlc、同花顺结束时刻一致
    grouped = data.resample(f"{step}min", label="right", closed="right")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in data.columns:
        agg["volume"] = "sum"
    out = grouped.agg(agg)
    out["minute_count"] = grouped["close"].count()
    out = out.dropna(subset=["close"])
    if len(out) == 0:
        return out

    min_ok = _min_minutes_for_bar(step)
    counts = out["minute_count"].to_numpy(dtype=np.int64, copy=False)
    keep = counts >= step
    if min_ok < step:
        keep = keep | ((counts >= min_ok) & _session_open_edge_mask(out.index, step))
    # 时段收盘边的最后一根：即使分钟不足整周期也保留（如 10:15、11:30 前的 30/60 分钟半桶），
    # 否则 K 线根数会少于行情商。
    keep = keep | ((counts >= 1) & _session_close_edge_mask(out.index, step))
    kept = out.loc[keep].copy()
    # 正在走的最后一根：即使分钟未齐也留下给实盘扫描
    last_ts = out.index[-1]
    if last_ts not in kept.index:
        kept = pd.concat([kept, out.loc[[last_ts]]]).sort_index()
        kept = kept[~kept.index.duplicated(keep="last")]
    return kept


def _drop_clock_hour_native_60(native: pd.DataFrame, persist: pd.DataFrame) -> pd.DataFrame:
    """
    同一天已经用 1 分钟拼出时段内时钟 K 线时，删掉对不上的天勤整点。

    例如拼出了 10:15、11:30，就要拿掉午休里的 12:00、13:00，
    也拿掉旧的 11:15、14:15。这一天 1 分钟不够、拼不出来时，先留着原生。
    """
    if native is None or persist is None or len(native) == 0 or len(persist) == 0:
        return native
    persist_days = pd.DatetimeIndex(persist.index).normalize()
    native_days = pd.DatetimeIndex(native.index).normalize()
    drop = np.zeros(len(native), dtype=bool)
    native_hm = (native.index.hour * 60 + native.index.minute).to_numpy(dtype=np.int64)
    for day in persist_days.unique():
        built = persist.loc[persist_days == day]
        times = {int(h) * 60 + int(m) for h, m in zip(built.index.hour, built.index.minute)}
        if not times:
            continue
        same = np.asarray(native_days == day)
        drop |= same & ~np.isin(native_hm, np.fromiter(times, dtype=np.int64, count=len(times)))
    if not drop.any():
        return native
    return native.loc[~drop]


def overlay_minute_bars(
    native: pd.DataFrame | None,
    built: pd.DataFrame | None,
    step: int,
) -> tuple[pd.DataFrame | None, pd.DatetimeIndex]:
    """
    用 1 分钟拼好的 K 线，替换同一时段里新浪直接下的那几根。

    覆盖范围内整段以拼出来的为准（含夜盘补上的第一根），避免左标签旧时间戳残留。
    返回需要写回本地的那些时间。
    """
    if built is None or len(built) == 0 or "minute_count" not in getattr(built, "columns", []):
        return native, pd.DatetimeIndex([])
    step = int(step)
    counts = built["minute_count"].to_numpy(dtype=np.int64, copy=False)
    # 时段内时钟会收出 10:15、11:00、11:30、14:00 这种短 K，只要有分钟就落库。
    persist_mask = counts >= 1
    persist = built.loc[persist_mask].drop(columns=["minute_count"], errors="ignore")
    visible = built.drop(columns=["minute_count"], errors="ignore")
    if persist is None or len(persist) == 0:
        if native is None or len(native) == 0:
            return visible, pd.DatetimeIndex([])
        return native, pd.DatetimeIndex([])

    cols = [
        name
        for name in ("open", "high", "low", "close", "volume")
        if name in persist.columns and (native is None or name in getattr(native, "columns", []))
    ]
    if not cols:
        cols = [name for name in ("open", "high", "low", "close", "volume") if name in persist.columns]

    if native is None or len(native) == 0:
        return visible, persist.index

    out = native.copy()
    if step >= 60:
        # 有拼好的时段内 60 分钟时，当天对不上的天勤整点（含午休 12:00）要拿掉。
        out = _drop_clock_hour_native_60(out, persist)
    # 按时间戳逐根替换：只丢掉拼图里确实有的原生K线。
    # 原来按 [min,max] 整段删，区间内某天缺 1 分钟数据时该天原生 K 线会被误删且不回填。
    drop_mask = out.index.isin(persist.index)
    out = out.loc[~drop_mask]
    try:
        out.attrs = {}
        persist = persist.copy()
        persist.attrs = {}
    except Exception:  # noqa: BLE001
        pass
    out = pd.concat([out, persist[cols]]).sort_index()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    # 实盘最后一根未齐时，仍显示在返回结果里
    last_built = visible.index[-1]
    if last_built not in out.index and last_built in visible.index:
        out = pd.concat([out, visible.loc[[last_built], cols]]).sort_index()
        out = out[~out.index.duplicated(keep="last")].sort_index()
    return out, persist.index


def drop_forming_bar(
    bars: pd.DataFrame | None,
    *,
    now: datetime | None = None,
) -> pd.DataFrame | None:
    """
    去掉还没收盘的最后一根，留下已经走完的 K 线。

    右标签时间戳 = 这一段的结束时刻。例如 5 分钟的 13:35 表示 13:30～13:35。
    只有墙上时钟还没过这个结束时刻，最后一根才算「正在走」；
    已经过了结束时刻，就整段留下，不能再盲删最后一根。

    否则午休后、夜盘开盘后，第一根已收盘 K 线会单独挂在末尾，
    被误删后程序会一直以为最新已收盘还是上午最后一根，止损会晚好几分钟。
    """
    if bars is None or len(bars) == 0:
        return bars
    if len(bars) == 1:
        # 只有一根时：还没收盘就先空着；已经收盘就留下。
        last = pd.Timestamp(bars.index[-1]).to_pydatetime()
        wall = now or datetime.now()
        if getattr(last, "tzinfo", None) is not None:
            last = last.replace(tzinfo=None)
        if getattr(wall, "tzinfo", None) is not None:
            wall = wall.replace(tzinfo=None)
        return bars if wall > last else bars.iloc[0:0]
    last = pd.Timestamp(bars.index[-1]).to_pydatetime()
    wall = now or datetime.now()
    if getattr(last, "tzinfo", None) is not None:
        last = last.replace(tzinfo=None)
    if getattr(wall, "tzinfo", None) is not None:
        wall = wall.replace(tzinfo=None)
    # 结束时刻已过 → 最后一根已收盘，整表可用
    if wall > last:
        return bars
    return bars.iloc[:-1]
