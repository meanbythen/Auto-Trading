"""
成交盈亏的蒙特卡洛。

功能：不改开仓规则，只把已经发生的每笔盈亏打乱或重抽，看净盈亏和回撤是不是靠运气撑的。
原理：
  - 打乱顺序：总和不变，最大回撤会变。现在回撤特别好看，常常只是亏的笔没连在一起。
  - 有放回重抽：总和会变。多数抽完仍为正，才比较像这组盈亏本身站得住。
笔数太少时不算，只说明，不当证据。
"""

from __future__ import annotations

import random
from typing import Any

DEFAULT_MC_RUNS = 500
MC_MIN_TRADES = 8
# 重抽后净盈亏仍为正的比例低于这个，更像运气。
MC_BOOTSTRAP_POSITIVE_MIN = 0.70


def trade_pnls(trades: list[Any] | None) -> list[float]:
    """从成交记录里取出每笔平仓盈亏。"""
    out: list[float] = []
    for trade in trades or []:
        try:
            out.append(float(getattr(trade, "pnl", 0) or 0))
        except (TypeError, ValueError):
            continue
    return out


def max_drawdown_from_pnls(pnls: list[float], capital: float) -> float:
    """按成交顺序累加权益，算出最大回撤（元）。"""
    peak = cash = float(capital)
    max_dd = 0.0
    for pnl in pnls:
        cash += float(pnl)
        if cash > peak:
            peak = cash
        dd = peak - cash
        if dd > max_dd:
            max_dd = dd
    return float(max_dd)


def run_trade_monte_carlo(
    pnls: list[float],
    *,
    capital: float,
    n_runs: int = DEFAULT_MC_RUNS,
    seed: int = 42,
) -> dict[str, Any]:
    """
    对同一组盈亏做打乱和重抽。

    返回给界面和写入闸用的数字。笔数不够时 skipped=true，不拦写入。
    """
    values = [float(x) for x in pnls]
    runs = max(50, int(n_runs or DEFAULT_MC_RUNS))
    money = float(capital) if capital and float(capital) > 0 else 100_000.0
    if len(values) < MC_MIN_TRADES:
        return {
            "skipped": True,
            "skip_reason": (
                f"成交只有 {len(values)} 笔，少于 {MC_MIN_TRADES} 笔，"
                "打乱看不出是规则还是运气，这一项只供查看。"
            ),
            "trades": len(values),
            "runs": 0,
            "net_pnl": round(sum(values), 2),
            "original_dd": round(max_drawdown_from_pnls(values, money), 2) if values else 0.0,
            "bootstrap_positive_rate": 0.0,
            "shuffle_median_dd": 0.0,
            "shuffle_dd_luck": 0.0,
        }

    original_net = float(sum(values))
    original_dd = max_drawdown_from_pnls(values, money)
    rng = random.Random(int(seed))

    shuffle_dds: list[float] = []
    lucky = 0
    for _ in range(runs):
        shuffled = list(values)
        rng.shuffle(shuffled)
        dd = max_drawdown_from_pnls(shuffled, money)
        shuffle_dds.append(dd)
        if dd + 1e-9 >= original_dd:
            lucky += 1

    bootstrap_pos = 0
    for _ in range(runs):
        sample = [values[rng.randrange(len(values))] for _ in values]
        if sum(sample) > 0:
            bootstrap_pos += 1

    shuffle_dds.sort()
    mid = shuffle_dds[len(shuffle_dds) // 2]
    return {
        "skipped": False,
        "skip_reason": "",
        "trades": len(values),
        "runs": runs,
        "net_pnl": round(original_net, 2),
        "original_dd": round(original_dd, 2),
        "bootstrap_positive_rate": round(bootstrap_pos / runs, 4),
        "shuffle_median_dd": round(float(mid), 2),
        "shuffle_dd_luck": round(lucky / runs, 4),
    }


def judge_monte_carlo(report: dict[str, Any] | None) -> tuple[bool, str]:
    """
    写入闸：重抽后多数仍赚钱，且打乱后回撤不是差出一截。

    笔数不够时放行并说明，避免用太少的成交假装稳健。
    """
    box = dict(report or {})
    if box.get("skipped"):
        return True, str(box.get("skip_reason") or "成交打乱未计算。")
    rate = float(box.get("bootstrap_positive_rate") or 0)
    if rate + 1e-9 < MC_BOOTSTRAP_POSITIVE_MIN:
        return (
            False,
            f"把成交盈亏打乱重抽 {int(box.get('runs') or 0)} 次后，"
            f"净盈亏仍为正的只有 {rate:.0%}，不到 {MC_BOOTSTRAP_POSITIVE_MIN:.0%}，"
            "更像运气，这次不写入。",
        )
    original_dd = float(box.get("original_dd") or 0)
    median_dd = float(box.get("shuffle_median_dd") or 0)
    # 有亏有赚却几乎没有回撤：只可能是亏的笔刚好没连在一起。
    if original_dd <= 1e-9 and median_dd > 1e-9:
        return (
            False,
            f"原来最大回撤几乎是 0，把成交顺序打乱后一半情况回撤变成 {median_dd:.0f}。"
            "现在曲线太平，很大程度上是成交顺序运气，这次不写入。",
        )
    return True, "把成交盈亏打乱重抽后，净盈亏多数仍为正。"


def format_monte_carlo_lines(report: dict[str, Any] | None) -> list[str]:
    """策略优化结果框里的几行说明。"""
    box = dict(report or {})
    lines = ["—— 成交打乱（看是规则还是运气）——"]
    if not box:
        lines.append("（未计算）")
        return lines
    if box.get("skipped"):
        lines.append(str(box.get("skip_reason") or "未计算"))
        return lines
    lines.append(
        f"用了 {int(box.get('trades') or 0)} 笔成交，随机 {int(box.get('runs') or 0)} 次"
    )
    lines.append(
        f"重抽后净盈亏仍为正  {float(box.get('bootstrap_positive_rate') or 0):.0%}"
        f"（低于 {MC_BOOTSTRAP_POSITIVE_MIN:.0%} 不能写入）"
    )
    lines.append(
        f"原来最大回撤  {float(box.get('original_dd') or 0):.0f}，"
        f"打乱顺序后一半情况  {float(box.get('shuffle_median_dd') or 0):.0f}"
        "（只供对照，不单独拦写入）"
    )
    return lines
