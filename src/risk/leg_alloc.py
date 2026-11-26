"""
组合腿保证金分配

功能：多品种同时挂机时，先把「权益 × 仓位上限%」的预算在几条腿之间分好，
     让每条腿至少留 1 手，再把剩余额度尽量用满。

口径（用满额度 + 每腿至少 1 手）：
1. 先给每条腿各 min_each 手。预算连每人一手都放不下时，按每手占用从小到大尽量覆盖。
2. 余下预算用动态规划穷举加手，选「占用最大」的组合；同占用时手数多的优先。
3. 每手保证金由调用方按当前价格现算，价格变了下一轮重新分。

只产出每条腿的总手数上限，不直接下单：
止损距离、每笔风险%、单笔上限、柜台可用资金仍由 PositionSizer 继续取小。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# 动态规划按 10 元一格算，避免状态过多。剩余额度有几百元缓冲，10 元误差可忽略。
_STEP = 10.0


@dataclass(frozen=True)
class LegPlan:
    """一次分配的结果。lots 是每条腿的总手数上限（含已持有）。"""

    lots: dict[str, int]
    used: float
    budget: float
    uncovered: tuple[str, ...] = ()

    def summary(self) -> str:
        parts = [f"{symbol} {lots}手" for symbol, lots in self.lots.items() if lots > 0]
        text = " / ".join(parts) if parts else "无腿可分"
        remain = max(0.0, self.budget - self.used)
        note = (
            f"{text}（保证金占用约{self.used:.0f}元 / 预算{self.budget:.0f}元，"
            f"剩{remain:.0f}元）"
        )
        if self.uncovered:
            note += f"；预算不够保底，未覆盖：{'、'.join(self.uncovered)}"
        return note


def allocate(
    legs: Mapping[str, float],
    budget: float,
    *,
    min_each: int = 1,
) -> LegPlan:
    """
    把保证金预算分给各条腿。

    参数:
        legs: 品种 → 每手保证金（必须大于 0，价格不可用的腿不要传进来）。
        budget: 可占用金额，通常是「权益 × 仓位上限%」。
        min_each: 每条腿先保底几手，默认 1。
    """
    need = {str(key): float(value) for key, value in legs.items() if float(value) > 0}
    budget = max(0.0, float(budget))
    floor_lots = max(1, int(min_each))
    if not need or budget <= 0:
        return LegPlan({symbol: 0 for symbol in need}, 0.0, budget, tuple(need))

    lots = {symbol: 0 for symbol in need}
    uncovered: list[str] = []
    if sum(need.values()) * floor_lots <= budget:
        for symbol in need:
            lots[symbol] = floor_lots
    else:
        # 连保底都放不下：先照顾每手便宜的腿，尽量多覆盖一条。
        for symbol in sorted(need, key=lambda key: (need[key], key)):
            used_now = sum(need[sym] * lots[sym] for sym in need)
            if used_now + need[symbol] * floor_lots <= budget:
                lots[symbol] = floor_lots
            else:
                uncovered.append(symbol)

    used = sum(need[symbol] * lots[symbol] for symbol in need)
    remain_units = int((budget - used) // _STEP)
    if remain_units > 0:
        order = list(need)
        units = {symbol: max(1, int(round(need[symbol] / _STEP))) for symbol in order}
        # used_units -> 各腿额外手数；同占用保留总手数多的那个。
        best: dict[int, tuple[int, ...]] = {0: tuple(0 for _ in order)}
        for index, symbol in enumerate(order):
            size = units[symbol]
            max_lots = remain_units // size
            nxt: dict[int, tuple[int, ...]] = {}
            for used_units, extra in best.items():
                for count in range(0, max_lots + 1):
                    total = used_units + count * size
                    if total > remain_units:
                        break
                    candidate = extra[:index] + (count,) + extra[index + 1 :]
                    current = nxt.get(total)
                    if current is None or sum(candidate) > sum(current):
                        nxt[total] = candidate
            best = nxt
        _, extra = max(best.items(), key=lambda item: (item[0], sum(item[1])))
        for index, symbol in enumerate(order):
            lots[symbol] += extra[index]

    used = sum(need[symbol] * lots[symbol] for symbol in need)
    return LegPlan(lots, used, budget, tuple(uncovered))
