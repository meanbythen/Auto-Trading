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

from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


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
_SESSION_OPEN_MINUTES = (9 * 60, 13 * 60 + 30, 21 * 60)
# 国内商品常见收盘（用于判断「这一段要收盘，最后一根可能不足整周期」）
_SESSION_CLOSE_MINUTES = (
    10 * 60 + 15,
    11 * 60 + 30,
    15 * 60,
    23 * 60,
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


def bars_from_minutes(minutes: pd.DataFrame | None, period: str) -> pd.DataFrame | None:
    """
    用 1 分钟拼出更长的 K 线。

    时间戳用这一段的结束时刻（右标签），与同花顺主图一致：
    15 分钟的 21:15 = 约 21:00～21:15；5 分钟的 11:15 = 11:10～11:15。
    新浪直接下的同档时间戳常和这套不一致，开仓必须以 1 分钟拼出来的为准。
    最后一段分钟还没齐也先留着，扫描时当未收盘。
    开盘第一根允许略缺几分钟（夜盘常见），避免整根被丢掉。
    """
    if minutes is None or len(minutes) == 0:
        return minutes
    step = int(canonical_period(period))
    if step <= 1:
        return minutes
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


def fix_gap_spanning_bars(
    bars: pd.DataFrame | None, *, gap_ratio: float = 0.002
) -> pd.DataFrame | None:
    """
    修掉跨休市第一根K线里被拼进来的上一段收盘价。

    新浪的分钟接口在休市后的第一根（09:05、13:35、21:05）有时会把上一段最后一笔
    塞进 high/low：跳空低开时 high 正好压在上一根收盘，跳空高开时 low 正好压在
    上一根收盘。这个价位本段并没有成交（比如今天 fu2701 的 09:05 最高价是昨晚收盘
    4486，而今天开盘只有 4179），会让突破、ATR、止损全部失真。

    只在这根开盘相对上一根明显跳空（默认 0.2%）时处理，普通K线不动。
    取回本段自己的价格区间：低开时 high 收到 open/close 较高者，高开时 low 抬到较低者。
    """
    if bars is None or len(bars) < 2:
        return bars
    out = unique_ohlc_columns(bars)
    if out is None:
        return bars
    need = {"open", "high", "low", "close"}
    if not need.issubset(set(out.columns)):
        return out
    prev_close = ohlc_series(out, "close").shift(1)
    open_px = ohlc_series(out, "open")
    high_px = ohlc_series(out, "high")
    low_px = ohlc_series(out, "low")
    close_px = ohlc_series(out, "close")
    body_high = open_px.where(open_px > close_px, close_px)
    body_low = open_px.where(open_px < close_px, close_px)

    gap = (open_px - prev_close).abs()
    moved = gap >= (prev_close.abs() * gap_ratio)
    tol = (prev_close.abs() * 1e-4).clip(lower=1e-6)
    down = moved & (open_px < prev_close) & (high_px <= prev_close + tol) & (high_px > body_high)
    up = moved & (open_px > prev_close) & (low_px >= prev_close - tol) & (low_px < body_low)

    def _flag(mask: Any) -> pd.Series:
        arr = np.asarray(mask, dtype=bool)
        if arr.ndim > 1:
            arr = arr.reshape(len(out), -1)[:, -1]
        arr = arr.reshape(-1)[: len(out)]
        return pd.Series(arr, index=out.index)

    down_flag = _flag(down)
    up_flag = _flag(up)
    # 必须先收成一列 True/False。多列时 .any() 会变成一排数字，不能写进 if。
    if bool(down_flag.to_numpy().any()):
        out.loc[down_flag, "high"] = body_high[down_flag]
    if bool(up_flag.to_numpy().any()):
        out.loc[up_flag, "low"] = body_low[up_flag]
    return out


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
    min_ok = _min_minutes_for_bar(step)
    # 可落库：分钟够，或开盘/收盘边且达到放宽门槛
    counts = built["minute_count"].to_numpy(dtype=np.int64, copy=False)
    persist_mask = counts >= step
    if min_ok < step:
        persist_mask = persist_mask | (
            (counts >= min_ok) & _session_open_edge_mask(built.index, step)
        )
    # 收盘边的半桶也要落库，否则本地库存会比行情商少这一根
    persist_mask = persist_mask | (
        (counts >= 1) & _session_close_edge_mask(built.index, step)
    )
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
