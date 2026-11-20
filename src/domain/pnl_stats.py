"""
平仓盈亏统计。

功能：胜率、盈利因子只在这里算一份，回测、健康对照、影子盘、账户页共用。
原理：口径写死为「盈亏 > 0 记胜」「盈亏 < 0 记负」；
      是否需要把「盈亏恰好为 0」也算进分母，由调用方显式选择，
      因为回测摘要一直把 0 当负笔，账户页则只按有方向的笔算胜率。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class PnlSummary:
    """一组平仓盈亏的汇总。"""

    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    # 没有亏损笔时：有盈利记 inf，完全没有盈亏记 None。展示层自己决定写 99、横杠还是「没有亏损」。
    profit_factor: float | None


def summarize_pnl(pnls: Iterable[float], *, zero_as_loss: bool = False) -> PnlSummary:
    """
    汇总一组平仓盈亏。

    zero_as_loss=True：盈亏为 0 的笔也算进亏损笔的分母（历史回测摘要的老口径）。
    zero_as_loss=False：盈亏为 0 的笔不进胜率分母（账户页、影子盘的老口径）。
    """
    values = [float(x) for x in pnls]
    wins = sum(1 for x in values if x > 0)
    losses = sum(1 for x in values if x < 0)
    if zero_as_loss:
        losses += sum(1 for x in values if x == 0)
    gross_profit = sum(x for x in values if x > 0)
    gross_loss = abs(sum(x for x in values if x < 0))
    decided = wins + losses
    if gross_loss > 1e-9:
        factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0:
        factor = math.inf
    else:
        factor = None
    return PnlSummary(
        trades=len(values),
        wins=wins,
        losses=losses,
        win_rate=(wins / decided) if decided else 0.0,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=factor,
    )
