"""
启动常驻第一轮回溯补开仓。

功能：启动后只看一次最近几根已收盘信号 K 线；若当时有开仓信号，
且仍满足「时间不太久、现价没跑太远、止损没被扫到」，就允许补开一笔。
原理：用临时策略实例去试历史前缀，不弄脏常驻策略的内部记忆；
三道门（时间 / ATR 偏离 / 止损路径）都过了，才把同一段前缀交给正式策略下单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
from loguru import logger

from src.strategy import StrategyAction, StrategyDecision, create_strategy
from src.strategy.indicators import average_true_range
from src.strategy.types import config_flag


@dataclass(frozen=True)
class CatchUpSettings:
    """回溯补开仓的开关和门槛。"""

    enabled: bool = True
    lookback_bars: int = 3
    max_age_minutes: float = 20.0
    max_entry_atr: float = 0.5
    atr_period: int = 14

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "CatchUpSettings":
        raw = ((cfg.get("strategy") or {}).get("catch_up_on_start") or {})
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            enabled=config_flag(raw.get("enabled"), True),
            lookback_bars=max(1, int(raw.get("lookback_bars") or 3)),
            max_age_minutes=float(raw.get("max_age_minutes") or 20),
            max_entry_atr=float(raw.get("max_entry_atr") or 0.5),
            atr_period=max(2, int(raw.get("atr_period") or 14)),
        )


@dataclass
class CatchUpHit:
    """找到一笔可以补的开仓：用哪一段 K 线前缀，以及探测到的决策。"""

    prefix: pd.DataFrame
    trail_prefix: pd.DataFrame | None
    trend_prefix: pd.DataFrame | None
    decision: StrategyDecision
    bar_time: datetime
    atr: float
    price_gap: float


def _bar_time(bars: pd.DataFrame, index: int = -1) -> datetime | None:
    """取出某一根 K 线的时间。优先用索引，没有再用 datetime 列。"""
    if bars is None or len(bars) == 0:
        return None
    label = bars.index[index]
    ts = pd.Timestamp(label)
    if pd.isna(ts) and "datetime" in bars.columns:
        ts = pd.to_datetime(bars["datetime"].iloc[index], errors="coerce")
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()


def _truncate_by_end(
    frame: pd.DataFrame | None,
    end_time: datetime,
) -> pd.DataFrame | None:
    """把辅助周期截到信号周期这一根结束时刻（含）。"""
    if frame is None or len(frame) == 0:
        return frame
    end = pd.Timestamp(end_time)
    try:
        keep = frame.index <= end
        out = frame.loc[keep]
    except Exception:  # noqa: BLE001
        return frame
    return out if len(out) > 0 else frame.iloc[0:0]


def _prefix_bars(closed: pd.DataFrame, older: int) -> pd.DataFrame | None:
    """
    older=0 用到最新已收盘；older=1 再往前少一根，以此类推。
    根数不够就返回 None。
    """
    if closed is None or len(closed) <= older:
        return None
    end = len(closed) - older
    if end < 1:
        return None
    return closed.iloc[:end]


def _atr_at_end(bars: pd.DataFrame, period: int) -> float | None:
    """信号当时那段 K 线末端的 ATR。算不出或太小就返回 None。"""
    if bars is None or len(bars) < period + 1:
        return None
    if not {"high", "low", "close"}.issubset(bars.columns):
        return None
    series = average_true_range(
        bars["high"],
        bars["low"],
        bars["close"],
        period=period,
    )
    if series is None or len(series) == 0:
        return None
    value = float(series.iloc[-1])
    if pd.isna(value) or value <= 0:
        return None
    return value


def stop_was_touched(
    *,
    side: str,
    stop_price: float,
    after_signal: pd.DataFrame,
) -> bool:
    """
    信号之后的高低点有没有碰到止损。

    多仓：任意一根最低价 <= 止损；空仓：任意一根最高价 >= 止损。
    """
    if after_signal is None or len(after_signal) == 0:
        return False
    if not {"high", "low"}.issubset(after_signal.columns):
        return False
    stop = float(stop_price)
    highs = pd.to_numeric(after_signal["high"], errors="coerce")
    lows = pd.to_numeric(after_signal["low"], errors="coerce")
    if side == "long":
        return bool((lows <= stop).fillna(False).any())
    if side == "short":
        return bool((highs >= stop).fillna(False).any())
    return False


def _probe_open(
    cfg: dict[str, Any],
    symbol: str,
    prefix: pd.DataFrame,
    trail_prefix: pd.DataFrame | None,
    trend_prefix: pd.DataFrame | None,
    bar_time: datetime,
) -> StrategyDecision | None:
    """临时策略实例评估前缀；只有开仓动作才返回。"""
    probe = create_strategy(cfg)
    kwargs: dict[str, Any] = {"trail_bars": trail_prefix, "now": bar_time}
    if getattr(probe, "uses_trend_bars", False):
        kwargs["trend_bars"] = trend_prefix
    try:
        decision = probe.evaluate(symbol, prefix, **kwargs)
    except TypeError:
        # 个别策略不收 trend_bars / now，退回最小参数。
        decision = probe.evaluate(symbol, prefix, trail_bars=trail_prefix)
    if decision.action not in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
        return None
    return decision


def find_catch_up_open(
    *,
    cfg: dict[str, Any],
    symbol: str,
    closed: pd.DataFrame,
    trail_closed: pd.DataFrame | None = None,
    trend_closed: pd.DataFrame | None = None,
    forming: pd.DataFrame | None = None,
    settings: CatchUpSettings | None = None,
    now: datetime | None = None,
) -> CatchUpHit | None:
    """
    从新到旧最多看 lookback_bars 根，找第一笔仍合格的开仓。

    forming：当前还没收盘的那一根（可空）。用来判断「到现在」止损有没有被扫。
    """
    settings = settings or CatchUpSettings.from_cfg(cfg)
    if not settings.enabled:
        return None
    if closed is None or len(closed) == 0:
        return None

    now = now or datetime.now()
    current_close = float(pd.to_numeric(closed["close"], errors="coerce").iloc[-1])
    skip_notes: list[str] = []

    for older in range(settings.lookback_bars):
        prefix = _prefix_bars(closed, older)
        if prefix is None or len(prefix) == 0:
            continue
        bar_time = _bar_time(prefix, -1)
        if bar_time is None:
            skip_notes.append(f"往前第{older + 1}根没有时间戳")
            continue

        age_min = (now - bar_time).total_seconds() / 60.0
        if age_min < 0:
            age_min = 0.0
        if age_min > settings.max_age_minutes:
            skip_notes.append(
                f"往前第{older + 1}根已过 {age_min:.0f} 分钟，超过 {settings.max_age_minutes:.0f} 分钟"
            )
            continue

        trail_prefix = _truncate_by_end(trail_closed, bar_time)
        trend_prefix = _truncate_by_end(trend_closed, bar_time)
        decision = _probe_open(cfg, symbol, prefix, trail_prefix, trend_prefix, bar_time)
        if decision is None:
            continue

        entry = float(decision.entry_price or 0.0)
        stop = float(decision.stop_price or 0.0)
        if entry <= 0 or stop <= 0:
            skip_notes.append(f"往前第{older + 1}根开仓信号缺少开仓价或止损价")
            continue

        atr = _atr_at_end(prefix, settings.atr_period)
        if atr is None:
            skip_notes.append(f"往前第{older + 1}根算不出 ATR，不能判断价差")
            continue
        price_gap = abs(current_close - entry)
        if price_gap > settings.max_entry_atr * atr + 1e-12:
            skip_notes.append(
                f"往前第{older + 1}根现价偏离 {price_gap:.4f}，"
                f"超过 {settings.max_entry_atr}×ATR({atr:.4f})"
            )
            continue

        # 信号这根本身不算「之后」；之后 = 更新的已收盘 + 正在走的那根。
        after = closed.iloc[len(prefix) :]
        if forming is not None and len(forming) > 0:
            after = pd.concat([after, forming]) if len(after) else forming
        side = "long" if decision.action == StrategyAction.BUY_OPEN else "short"
        if stop_was_touched(side=side, stop_price=stop, after_signal=after):
            skip_notes.append(f"往前第{older + 1}根开仓后价格已碰到止损 {stop}")
            continue

        logger.info(
            "启动回溯补开仓命中 {} | 信号时间 {} | 已过 {:.0f} 分钟 | "
            "开仓价 {} 现价 {} 偏离 {:.4f} ATR {:.4f} | {}",
            symbol,
            bar_time.strftime("%Y-%m-%d %H:%M"),
            age_min,
            entry,
            current_close,
            price_gap,
            atr,
            decision.reason,
        )
        return CatchUpHit(
            prefix=prefix,
            trail_prefix=trail_prefix,
            trend_prefix=trend_prefix,
            decision=decision,
            bar_time=bar_time,
            atr=atr,
            price_gap=price_gap,
        )

    if skip_notes:
        logger.info("启动回溯补开仓未命中 {}：{}", symbol, "；".join(skip_notes[-3:]))
    else:
        logger.info("启动回溯补开仓未命中 {}：最近 {} 根没有开仓信号", symbol, settings.lookback_bars)
    return None
