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
# 成交层蒙特卡洛的执行噪声默认值：只做参考，不参与写入闸。
DEFAULT_EXEC_SLIP_TICKS = 0.5
DEFAULT_EXEC_JITTER_TICKS = 1.0
DEFAULT_EXEC_DELAY_TICKS = 0.5
DEFAULT_EXEC_RUIN_PCT = 20.0


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


def _as_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if out == out else float(default)


def _percentile(values: list[float], q: float) -> float:
    """已排序列表的分位数，q 写 0.05 / 0.5 / 0.95。"""
    if not values:
        return 0.0
    pos = min(max(q, 0.0), 1.0) * (len(values) - 1)
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    frac = pos - low
    return float(values[low] * (1.0 - frac) + values[high] * frac)


def run_execution_monte_carlo(
    trades: list[Any] | None,
    *,
    capital: float,
    tick_size: Any = 1.0,
    multiplier: Any = 10.0,
    n_runs: Any = DEFAULT_MC_RUNS,
    seed: int = 42,
    slip_ticks: Any = DEFAULT_EXEC_SLIP_TICKS,
    jitter_ticks: Any = DEFAULT_EXEC_JITTER_TICKS,
    delay_ticks: Any = DEFAULT_EXEC_DELAY_TICKS,
    partial_fill_prob: Any = 0.0,
    ruin_pct: Any = DEFAULT_EXEC_RUIN_PCT,
) -> dict[str, Any]:
    """
    成交层蒙特卡洛：给每笔成交叠执行噪声，再看净盈亏和回撤分布。

    每笔多付的跳动 = |正规(滑点均值, 抖动)| + 均匀(0, 延迟上限)，买卖两边都算；
    可选按概率部分成交，成交比例均匀落在 50%～100%。
    只回答「执行差一点会怎样」，不参与写入闸。
    """
    base = [float(getattr(trade, "pnl", 0) or 0) for trade in trades or []]
    volumes = [max(1.0, _as_float(getattr(trade, "volume", 1), 1.0)) for trade in trades or []]
    money = _as_float(capital, 100_000.0)
    if money <= 0:
        money = 100_000.0
    runs = max(50, int(_as_float(n_runs, DEFAULT_MC_RUNS)))
    slip = max(0.0, _as_float(slip_ticks, DEFAULT_EXEC_SLIP_TICKS))
    jitter = max(0.0, _as_float(jitter_ticks, DEFAULT_EXEC_JITTER_TICKS))
    delay = max(0.0, _as_float(delay_ticks, DEFAULT_EXEC_DELAY_TICKS))
    partial = min(1.0, max(0.0, _as_float(partial_fill_prob, 0.0)))
    ruin = max(1.0, _as_float(ruin_pct, DEFAULT_EXEC_RUIN_PCT))
    tick = max(1e-12, _as_float(tick_size, 1.0))
    mult = max(1e-12, _as_float(multiplier, 10.0))

    assumptions = {
        "slip_ticks": round(slip, 4),
        "jitter_ticks": round(jitter, 4),
        "delay_ticks": round(delay, 4),
        "partial_fill_prob": round(partial, 4),
        "tick_size": round(tick, 6),
        "multiplier": round(mult, 4),
        "ruin_pct": round(ruin, 2),
    }
    if len(base) < MC_MIN_TRADES:
        return {
            "skipped": True,
            "skip_reason": (
                f"成交只有 {len(base)} 笔，少于 {MC_MIN_TRADES} 笔，"
                "执行噪声看不穩，这一项只供查看。"
            ),
            "trades": len(base),
            "runs": 0,
            "base_net": round(sum(base), 2),
            "assumptions": assumptions,
        }

    rng = random.Random(int(seed))
    nets: list[float] = []
    dds: list[float] = []
    for _ in range(runs):
        pnls: list[float] = []
        for pnl, volume in zip(base, volumes):
            # 开仓和平仓各付一次执行噪声。
            noise = abs(rng.gauss(slip, jitter)) + rng.uniform(0.0, delay)
            noise += abs(rng.gauss(slip, jitter)) + rng.uniform(0.0, delay)
            frac = rng.uniform(0.5, 1.0) if partial > 0 and rng.random() < partial else 1.0
            pnls.append(pnl * frac - noise * tick * mult * volume * frac)
        nets.append(sum(pnls))
        dds.append(max_drawdown_from_pnls(pnls, money))

    nets.sort()
    dds.sort()
    positive = sum(1 for value in nets if value > 0) / runs
    ruined = sum(1 for value in dds if value >= money * ruin / 100.0) / runs
    return {
        "skipped": False,
        "skip_reason": "",
        "trades": len(base),
        "runs": runs,
        "base_net": round(sum(base), 2),
        "mean_net": round(sum(nets) / runs, 2),
        "p05_net": round(_percentile(nets, 0.05), 2),
        "p50_net": round(_percentile(nets, 0.50), 2),
        "p95_net": round(_percentile(nets, 0.95), 2),
        "mean_dd": round(sum(dds) / runs, 2),
        "dd_p95": round(_percentile(dds, 0.95), 2),
        "positive_rate": round(positive, 4),
        "ruin_rate": round(ruined, 4),
        "assumptions": assumptions,
    }


def format_execution_monte_carlo_lines(report: dict[str, Any] | None) -> list[str]:
    """策略优化结果框里的执行噪声几行说明。"""
    box = dict(report or {})
    lines = ["—— 执行噪声重跑（成交层蒙特卡洛）——"]
    if not box:
        lines.append("（未计算）")
        return lines
    if box.get("skipped"):
        lines.append(str(box.get("skip_reason") or "未计算"))
        return lines
    assume = dict(box.get("assumptions") or {})
    lines.append(
        "每边多付 {slip:g}±{jitter:g} 跳 + 0～{delay:g} 跳延迟".format(
            slip=float(assume.get("slip_ticks") or 0),
            jitter=float(assume.get("jitter_ticks") or 0),
            delay=float(assume.get("delay_ticks") or 0),
        )
        + (
            f"，部分成交概率 {float(assume.get('partial_fill_prob') or 0):.0%}"
            if float(assume.get("partial_fill_prob") or 0) > 0
            else ""
        )
    )
    lines.append(
        f"净盈亏 中位 {float(box.get('p50_net') or 0):,.0f}，"
        f"5%～95% 区间 {float(box.get('p05_net') or 0):,.0f} ～ "
        f"{float(box.get('p95_net') or 0):,.0f}"
    )
    lines.append(
        f"最大回撤 中位 {float(box.get('mean_dd') or 0):,.0f}，"
        f"95% 分位 {float(box.get('dd_p95') or 0):,.0f}；"
        f"回撤超过 {float(assume.get('ruin_pct') or 0):g}% 权益的概率 "
        f"{float(box.get('ruin_rate') or 0):.1%}"
    )
    lines.append("只供对照，不参与写入闸；噪声要靠实盘滑点日志校准。")
    return lines


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
