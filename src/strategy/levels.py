"""
关键点位识别（支撑 / 压力）
功能：从K线中找出近期摆动高低点，作为「关键支撑/压力」。
原理：流动性耗尽往往发生在「假突破关键位后收回」——先刺破再收回，才算有效。
说明：这里用经典 swing（左右各 lookback 根更高/更低）近似你盘面里的关键位。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class KeyLevel:
    """一个关键价位。"""

    price: float
    kind: str  # support / resistance
    index: int
    strength: int = 1


def find_swing_levels(
    df: pd.DataFrame,
    lookback: int = 3,
    max_levels: int = 8,
) -> list[KeyLevel]:
    """
    找出近期摆动支撑与压力。

    参数:
        df: 含 high/low 的K线
        lookback: 左右各看几根来确认摆动点
        max_levels: 最多保留几个点位（越近越优先）
    """
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    levels: list[KeyLevel] = []

    for i in range(lookback, len(df) - lookback):
        window_high = highs.iloc[i - lookback : i + lookback + 1]
        window_low = lows.iloc[i - lookback : i + lookback + 1]
        h = float(highs.iloc[i])
        low = float(lows.iloc[i])
        if h >= float(window_high.max()) - 1e-12:
            levels.append(KeyLevel(price=h, kind="resistance", index=i))
        if low <= float(window_low.min()) + 1e-12:
            levels.append(KeyLevel(price=low, kind="support", index=i))

    # 越靠近当前K线越重要
    levels.sort(key=lambda x: x.index, reverse=True)
    return levels[:max_levels]


def nearest_level(
    levels: list[KeyLevel],
    kind: str,
    price: float,
    max_distance_pct: float = 0.008,
) -> KeyLevel | None:
    """
    在价格附近找最近的同类型关键位。

    参数:
        max_distance_pct: 允许偏离百分比（默认约 0.8%）
    """
    cands = [x for x in levels if x.kind == kind]
    if not cands:
        return None
    best = min(cands, key=lambda x: abs(x.price - price))
    if abs(best.price - price) / max(abs(price), 1e-9) <= max_distance_pct:
        return best
    return None


def pierced_then_reclaimed_support(
    low: float,
    body_low: float,
    support: float,
    pierce_tol: float = 0.0,
) -> bool:
    """
    下影线击穿支撑后，实体收回支撑上方。

    白话：先假跌破，再收回来，说明下方流动性被收割完。
    """
    pierced = low < support - pierce_tol
    reclaimed = body_low >= support - pierce_tol
    return pierced and reclaimed


def pierced_then_reclaimed_resistance(
    high: float,
    body_high: float,
    resistance: float,
    pierce_tol: float = 0.0,
) -> bool:
    """上影线击穿压力后，实体收回压力下方。"""
    pierced = high > resistance + pierce_tol
    reclaimed = body_high <= resistance + pierce_tol
    return pierced and reclaimed
