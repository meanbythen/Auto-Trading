"""
成交记录（领域对象）。

功能：回测、复盘、导出共用同一种已平仓成交结构，序列化只走这一处。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class TradeRecord:
    """一笔已平仓交易。"""

    symbol: str
    side: str  # long / short
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    volume: int
    pnl: float  # 元（已扣本笔分摊的开平手续费）
    reason_entry: str = ""
    reason_exit: str = ""
    commission: float = 0.0  # 本笔开+平手续费合计（元）
    # 多因子开仓快照。别的策略这些字段保持空，导出时不填衰减表。
    score_ma: float | None = None
    score_turtle: float | None = None
    score_boll: float | None = None
    score_sar: float | None = None
    score_total: float | None = None
    atr: float | None = None
    pnl_ma: float | None = None
    pnl_turtle: float | None = None
    pnl_boll: float | None = None
    pnl_sar: float | None = None
    dominant_factor: str = ""
    resonance: str = ""
    multiplier: float | None = None
    # 开仓那根K线的 ADX（评估时用来分趋势组/震荡组）。老结果没有这个字段时为 None。
    adx_at_entry: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """导出可 JSON 的成交字典。"""
        return asdict(self)
