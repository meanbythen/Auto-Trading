"""
tick 合成 K 线。

天勤 tick 的 volume 是当日累计成交量，不能逐笔相加：
单根 K 线成交量 = 该根最后一笔累计量 − 第一笔累计量，负数钳为 0（跨交易日重置）。
持仓量取最后一笔。1 分钟用右标签（结束时刻），5/15/30/60 复用 bars_from_minutes，
日线按期货交易日归属（夜盘算下一交易日）。

结果写独立的 tick_bars 库，键为 <合约>@tick，不和天勤官方 K 线、新浪 K 线混在一起。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.data.bar_store import BarStore
from src.data.periods import bars_from_minutes, canonical_period, drop_forming_bar, resample_ohlc
from src.data.tick.clean import clean_tick_frame
from src.data.tick.vnpy_store import load_tick_frame, split_instrument
from src.data.tq_source import config_symbol_from_tq, tq_symbol
from src.utils.trading_day import futures_trading_day

TICK_BAR_SUFFIX = "@tick"
DEFAULT_PERIODS = ("1", "5", "15", "30", "60", "D")


def ticks_to_minutes(ticks: pd.DataFrame | None) -> pd.DataFrame:
    """
    tick 合成 1 分钟 K 线，时间戳是这一分钟的结束时刻。

    开=第一笔最新价，高/低=极值，收=最后一笔，量=末笔累计量−首笔累计量，持仓=最后一笔。
    """
    if ticks is None or len(ticks) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
    data = ticks.copy()
    data.index = pd.to_datetime(data.index)
    if getattr(data.index, "tz", None) is not None:
        data.index = data.index.tz_convert("Asia/Shanghai").tz_localize(None)
    data = data.sort_index()
    if "last_price" not in data.columns:
        raise ValueError("tick 表里没有 last_price 列")

    grouped = data.resample("1min", label="right", closed="right")
    out = pd.DataFrame(
        {
            "open": grouped["last_price"].first(),
            "high": grouped["last_price"].max(),
            "low": grouped["last_price"].min(),
            "close": grouped["last_price"].last(),
        }
    )
    if "volume" in data.columns:
        first = grouped["volume"].first()
        last = grouped["volume"].last()
        out["volume"] = (last - first).clip(lower=0)
    else:
        out["volume"] = 0.0
    out["oi"] = grouped["open_interest"].last() if "open_interest" in data.columns else None

    out = out.dropna(subset=["close"])
    out.index.name = "datetime"
    return out[["open", "high", "low", "close", "volume", "oi"]]


def minutes_to_daily(minutes: pd.DataFrame | None) -> pd.DataFrame:
    """把 1 分钟按期货交易日聚成日线；夜盘归下一交易日，时间戳用交易日 00:00。"""
    if minutes is None or len(minutes) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
    days = pd.Index([futures_trading_day(stamp) for stamp in minutes.index], name="trading_day")
    grouped = minutes.groupby(days)
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    )
    if "oi" in minutes.columns:
        out["oi"] = grouped["oi"].last()
    out.index = pd.DatetimeIndex([pd.Timestamp(day) for day in out.index])
    out.index.name = "datetime"
    return out.sort_index()


def build_tick_bars(
    ticks: pd.DataFrame | None,
    periods: tuple[str, ...] | list[str] = DEFAULT_PERIODS,
) -> dict[str, pd.DataFrame]:
    """把一批 tick 合成各周期 K 线。只返回已经收盘的周期，最后一根形成中的不写库。"""
    minutes = ticks_to_minutes(ticks)
    done = drop_forming_bar(minutes)
    if done is None or len(done) == 0:
        return {}
    daily: pd.DataFrame | None = None
    bars: dict[str, pd.DataFrame] = {}
    for period in periods:
        code = canonical_period(period)
        if code in bars:
            continue
        if code == "1":
            frame = done
        elif code == "D":
            daily = minutes_to_daily(done)
            frame = daily
        elif code == "W":
            daily = daily if daily is not None else minutes_to_daily(done)
            frame = resample_ohlc(daily, "W")
        else:
            frame = bars_from_minutes(done, code)
            frame = drop_forming_bar(frame) if frame is not None else None
        if frame is None or len(frame) == 0:
            continue
        bars[code] = frame
    return bars


def synthesize_symbol(
    symbol: str,
    *,
    database: str | Path,
    bar_store: str | Path,
    start: Any = None,
    end: Any = None,
    periods: tuple[str, ...] | list[str] = DEFAULT_PERIODS,
) -> dict[str, Any]:
    """
    读一段 tick，清洗后合成各周期 K 线，写入 tick_bars 库。

    symbol 用配置写法（fu2701 / SR2701）。start/end 是 tick 的起止时间，end 不含。
    返回本次清洗和写库的审计信息；库里没有 tick 时返回空统计，不报错。
    """
    instrument = tq_symbol(symbol)
    vn_symbol, exchange = split_instrument(instrument)
    key = config_symbol_from_tq(instrument) or str(symbol)
    ticks = load_tick_frame(vn_symbol, exchange, start, end, database=database)
    cleaned, stats = clean_tick_frame(ticks)
    bars = build_tick_bars(cleaned, periods)
    store = BarStore(Path(bar_store))
    written: dict[str, int] = {}
    for period, frame in bars.items():
        written[period] = store.upsert(f"{key}{TICK_BAR_SUFFIX}", period, frame)
    report = {
        "symbol": key,
        "instrument": instrument,
        "ticks": int(stats.raw),
        "kept": int(stats.kept),
        "dropped": int(stats.dropped),
        "reasons": dict(stats.reasons),
        "bars": written,
        "from": str(cleaned.index[0]) if len(cleaned) else "",
        "to": str(cleaned.index[-1]) if len(cleaned) else "",
    }
    logger.info(
        "tick 合成 {}：tick {} 条（清洗后 {}），写库 {}",
        key,
        report["ticks"],
        report["kept"],
        written,
    )
    return report
