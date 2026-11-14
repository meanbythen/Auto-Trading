"""
K 线快照：原始数据 + 已经算好的特征。

功能：策略计算用的输入只有这两块，避免一个巨大 ctx 塞满临时变量。
原理：bars 用副本，features 是算出来的字典。策略仍可走原来的 evaluate(bars)，
本结构给新代码和测试用，不强迫旧策略立刻改接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd


@dataclass(frozen=True)
class BarSnapshot:
    """一根扫描用的只读行情。"""

    symbol: str
    timeframe: str
    bars: pd.DataFrame
    features: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_bars(
        cls,
        symbol: str,
        timeframe: str,
        bars: pd.DataFrame | None,
        features: Mapping[str, Any] | None = None,
    ) -> "BarSnapshot | None":
        """从拉取到的 K 线做一份副本。没有 K 线就返回空，不编造。"""
        if bars is None or len(bars) == 0:
            return None
        copied = bars.copy(deep=True)
        return cls(
            symbol=str(symbol),
            timeframe=str(timeframe),
            bars=copied,
            features=dict(features or {}),
        )
