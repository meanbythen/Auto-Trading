"""
策略优化（网格搜索）
功能：在选定时间段/策略上跑多组回测，按能不能赚钱的得分选出一组，并可写回配置。
得分不看胜率。盈利因子超过 2 不再加分，还要看净赚能不能盖住回撤。
原理：
  - 一次切断：前面一段打分，后面一段只验收；检验比例可手调。
  - 滚动前进：把检验段拆成多段，走一步再验一步；最后一段挑出的参数才考虑写入。
  - 多品种验收：参数在优化用的合约上过关后，再拿到其它关注品种上跑一遍，不重新挑参数。
  - 前一段赚钱、检验段亏掉超过一半，或其它品种上大半都大亏，不准写入。
  - 前一段分数接近时，留更靠近出厂参数的一组。
"""

from __future__ import annotations

import copy
import itertools
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestResult, filter_bars_by_range, run_backtest
from src.data.kline import KlineProvider
from src.data.periods import canonical_period
from src.strategy.session_scope import normalize_scope


# 趋势跟随只搜进场看多长、止损放几倍波动。出场通道保持更短的默认，避免和进场通道拧在一起。
GRID_TREND_LIGHT: dict[str, list[Any]] = {
    "entry_bars": [24, 36, 48],
}

GRID_TREND: dict[str, list[Any]] = {
    "entry_bars": [24, 36, 48],
    "atr_mult": [1.5, 2.0, 2.5],
}

# SuperTrend 只搜 ATR 根数和通道倍数。
GRID_ST_LIGHT: dict[str, list[Any]] = {
    "multiplier": [2.5, 3.0, 3.5],
}

GRID_ST: dict[str, list[Any]] = {
    "atr_period": [7, 10, 14],
    "multiplier": [2.5, 3.0, 3.5],
}

# 单均线系统（Single_ma）：只在常用根数里比。收盘清仓听总开关。
GRID_SINGLE_MA_LIGHT: dict[str, list[Any]] = {
    "ma_period": [20, 60, 120],
}
GRID_SINGLE_MA: dict[str, list[Any]] = {
    "ma_period": [20, 60, 80, 120],
    "reverse_on_cross": [True, False],
}

# 双均线：常用短/长搭配。
GRID_DUAL_MA_LIGHT: dict[str, list[Any]] = {
    "short_period": [10, 20],
    "long_period": [60, 120],
}
GRID_DUAL_MA: dict[str, list[Any]] = {
    "short_period": [10, 20, 30],
    "long_period": [60, 80, 120],
    "reverse_on_cross": [True, False],
}

# 日内实体：只搜少数结构旋钮，避免又变成影线细则过拟合。
# 精简约 4 组；标准约 162 组（再被「最多试几组」抽样）。
# eth_minutes 写回 daily_body（和配置页「ETH分钟数」同一处）。
# timeframe 写回 strategy.timeframe；优化拉 K 线时用网格里最细的一档，策略内部再收成信号周期。
GRID_BODY_LIGHT: dict[str, list[Any]] = {
    "eth_minutes": [20, 30],
    "timeframe": ["5", "15"],
}

GRID_BODY: dict[str, list[Any]] = {
    "eth_minutes": [15, 20, 30],
    "timeframe": ["5", "15"],
    # 开仓截止钟点：过点就不追，日盘、夜盘各搜三档（都对齐 5/15 分钟的收盘）。
    "day_cutoff": ["10:15", "10:30", "10:45"],
    "night_cutoff": ["22:00", "22:15", "22:30"],
    # 公共交易时段：写回 schedule 小节（和配置页「交易时段」同一处）。
    "session_scope": ["all", "day", "night"],
}

# 早盘 A/B：先按出厂规则跑，不额外搜参数。
GRID_FIXED: dict[str, list[Any]] = {}

# 长线趋势：主要搜仓位门槛，不乱搜周期（周期固定日线+60分钟）。
GRID_SWING_LIGHT: dict[str, list[Any]] = {
    "daily_ma": [20, 40],
    "add_min_atr": [0.8, 1.2],
    "reduce_extend_atr": [2.0, 3.0],
}
GRID_SWING: dict[str, list[Any]] = {
    "daily_ma": [20, 40, 60],
    "add_min_atr": [0.8, 1.0, 1.5],
    "reduce_extend_atr": [2.0, 2.5, 3.5],
    "max_adds": [1, 2],
}

_FIXED_RULE_ENGINES = {
    "morning_box_a",
    "morning_a",
    "morning_structure_b",
    "morning_b",
}

# 参数写进策略自己读的那一节。
_ENGINE_PARAM_SECTION = {
    "supertrend": "supertrend",
    "st": "supertrend",
    "single_ma": "single_ma",
    "Single_ma": "single_ma",
    "dual_ma": "dual_ma",
    "ma_cross": "dual_ma",
    "morning_box_a": "morning_box_a",
    "morning_a": "morning_box_a",
    "morning_structure_b": "morning_structure_b",
    "morning_b": "morning_structure_b",
    "swing_trend": "swing_trend",
}

# 默认网格跟主策略日内实体一致
DEFAULT_GRID = GRID_BODY

# 默认：前 70% 挑参数，后 30% 只验收。界面上的「检验比例%」默认 30。
TRAIN_RATIO = 0.70
DEFAULT_HOLDOUT_PCT = 30.0
# 检验比例允许 10%～50%，避免训练段太短或检验段几乎没有。
MIN_HOLDOUT_PCT = 10.0
MAX_HOLDOUT_PCT = 50.0
# 前一段分数相差不超过这个数，就算差不多，改留更靠近出厂参数的一组。
SCORE_TIE = 0.02
# 后一段亏掉的钱，超过前一段盈利的这个比例，就算由赚变成大亏。
HOLDOUT_LOSS_RATIO = 0.50
# 检验方式：一次切断 = 旧逻辑；滚动前进 = 走一步再验一步。
MODE_ONCE = "一次切断"
MODE_WALK = "滚动前进"
DEFAULT_WALK_STEPS = 3

# 参数中文说明（界面展示用）。只保留现有策略还会搜到的项。
PARAM_LABELS_CN: dict[str, str] = {
    "eth_minutes": "ETH开盘观察分钟",
    "entry_bars": "趋势进场看前几根",
    "exit_bars": "趋势出场看前几根",
    "atr_period": "ATR用几根K线",
    "atr_mult": "趋势止损用几倍波动",
    "multiplier": "SuperTrend通道倍数",
    "ma_period": "单均线根数",
    "short_period": "双均线短根数",
    "long_period": "双均线长根数",
    "reverse_on_cross": "均线反向时是否反手",
    "reverse_on_flip": "SuperTrend翻向时是否反手",
    "timeframe": "信号周期（分钟）",
    "day_cutoff": "日盘开仓截止时间",
    "night_cutoff": "夜盘开仓截止时间",
    "session_scope": "交易时段（all 全时段 / day 只做日盘 / night 只做夜盘）",
    "daily_ma": "长线日线均线根数",
    "add_min_atr": "长线加仓最少浮盈（倍波动）",
    "reduce_extend_atr": "长线伸展减仓（倍波动）",
    "max_adds": "长线最多加仓次数",
}


def grid_for_engine(engine: str, depth: str = "标准") -> dict[str, list[Any]]:
    """
    按策略引擎 + 搜索深度返回网格。

    depth:
        精简 = 少参数、跑得快
        标准 = 含 Pinbar 定义等（推荐）
        完整 = 参数更全，组合更多，靠随机抽样控制次数
    """
    e = (engine or "daily_body").lower()
    d = (depth or "标准").strip()
    if e == "trend_follow":
        if d == "精简":
            return copy.deepcopy(GRID_TREND_LIGHT)
        return copy.deepcopy(GRID_TREND)
    if e in ("supertrend", "st"):
        if d == "精简":
            return copy.deepcopy(GRID_ST_LIGHT)
        return copy.deepcopy(GRID_ST)
    if e == "single_ma":
        if d == "精简":
            return copy.deepcopy(GRID_SINGLE_MA_LIGHT)
        return copy.deepcopy(GRID_SINGLE_MA)
    if e in ("dual_ma", "ma_cross"):
        if d == "精简":
            return copy.deepcopy(GRID_DUAL_MA_LIGHT)
        return copy.deepcopy(GRID_DUAL_MA)
    if e == "daily_body":
        if d == "精简":
            return copy.deepcopy(GRID_BODY_LIGHT)
        return copy.deepcopy(GRID_BODY)
    if e == "swing_trend":
        if d == "精简":
            return copy.deepcopy(GRID_SWING_LIGHT)
        return copy.deepcopy(GRID_SWING)
    if e in _FIXED_RULE_ENGINES:
        return copy.deepcopy(GRID_FIXED)
    # 未知引擎：不搜参数，避免误改别的策略小节
    return copy.deepcopy(GRID_FIXED)


def describe_grid(grid: dict[str, list[Any]]) -> str:
    """生成给人看的网格说明文字。"""
    lines = []
    total = 1
    for k, vals in grid.items():
        total *= max(len(vals), 1)
        label = PARAM_LABELS_CN.get(k, k)
        lines.append(f"  · {label} ({k}): {vals}")
    lines.insert(0, f"共 {len(grid)} 个旋钮，理论组合约 {total} 组（实际按「最多试几组」随机抽样）")
    return "\n".join(lines)


def _finest_grid_timeframe(grid: dict[str, list[Any]] | None, fallback: str) -> str:
    """
    网格里若搜信号周期，拉 K 线用最细的那一档。

    原理：5 分钟可以收成 15 分钟再判断；若只拉 15 分钟，无法公平试 5 分钟。
    """
    vals = list((grid or {}).get("timeframe") or [])
    minutes: list[int] = []
    for raw in vals:
        period = canonical_period(str(raw))
        if period.isdigit() and int(period) > 0:
            minutes.append(int(period))
    if minutes:
        return str(min(minutes))
    period = canonical_period(str(fallback or "5"))
    return period if period.isdigit() else str(fallback or "5")


def _sample_combos(
    keys: list[str],
    values: list[list[Any]],
    max_trials: int,
    *,
    seed: int = 42,
) -> list[tuple[Any, ...]]:
    """
    从全部组合里抽样，最多 max_trials 组。
    原理：组合太多时不能只取「前 N 组」（会偏向前几个参数的取值），改为固定种子随机抽，可复现。
    """
    combos = list(itertools.product(*values))
    if len(combos) <= max_trials:
        return combos
    rng = random.Random(seed)
    return rng.sample(combos, max_trials)


def clamp_holdout_pct(pct: float | int | str | None) -> float:
    """把界面上的检验比例收成 10～50。空着或乱填就用默认 30。"""
    try:
        value = float(pct) if pct is not None and str(pct).strip() != "" else DEFAULT_HOLDOUT_PCT
    except (TypeError, ValueError):
        value = DEFAULT_HOLDOUT_PCT
    return min(MAX_HOLDOUT_PCT, max(MIN_HOLDOUT_PCT, value))


def train_ratio_from_holdout_pct(pct: float | int | str | None) -> float:
    """检验比例 30 → 训练比例 0.70。"""
    return 1.0 - clamp_holdout_pct(pct) / 100.0


def split_train_holdout(n_bars: int, ratio: float = TRAIN_RATIO) -> int:
    """
    返回切断位置。前面 [0, cut) 用来挑参数，后面只验收一次。

    行情太短就直接说明，不再把整段拿去打分。前一段至少 50 根，后一段至少 30 根。
    ratio 是训练比例（0.5～0.9），不是检验比例。
    """
    if n_bars < 80:
        raise RuntimeError(
            f"这段行情只有 {n_bars} 根，分不出前一段和后一段。"
            "请把时间段放长，或选「全部可用」。"
        )
    ratio = min(0.90, max(0.50, float(ratio)))
    cut = int(n_bars * ratio)
    # 夹在「前一段够长」和「后一段还剩 30 根」之间，避免某一段只剩几根 K 线。
    return min(max(cut, 50), n_bars - 30)


def walk_forward_folds(n_bars: int, holdout_pct: float, steps: int) -> list[tuple[int, int]]:
    """
    滚动前进的每一段：(训练结束下标, 检验结束下标)，左闭右开。

    原理：把总检验长度拆成 steps 段，挨在行情末尾。
    第 1 段用更早的行情挑参数，验下一段；第 2 段把上一段检验行情并进训练，再验再下一段。
    这样是「走一步再验一步」，检验段从不参加这一步的打分。
    """
    steps = max(1, int(steps))
    holdout_pct = clamp_holdout_pct(holdout_pct)
    # 每一段检验至少 30 根；整段至少留下 50 根给第一段训练。
    min_total_oos = steps * 30
    want_oos = max(min_total_oos, int(n_bars * holdout_pct / 100.0))
    if n_bars < 50 + min_total_oos:
        raise RuntimeError(
            f"这段行情只有 {n_bars} 根，做不了 {steps} 段滚动前进。"
            "请把时间段放长，或减少滚动段数、把检验比例调小。"
        )
    total_oos = min(want_oos, n_bars - 50)
    # 总检验长度不够「每段 30 根」时，自动少做几段，避免硬拆出几根 K 线的空段。
    steps = min(steps, max(1, total_oos // 30))
    oos_len = total_oos // steps
    total_oos = oos_len * steps
    first_train_end = n_bars - total_oos
    folds: list[tuple[int, int]] = []
    for i in range(steps):
        train_end = first_train_end + i * oos_len
        oos_end = train_end + oos_len
        folds.append((train_end, oos_end))
    return folds


def judge_cross_symbols(train_net: float, rows: list[dict[str, Any]]) -> tuple[bool, str]:
    """
    其它关注品种上的样本外验收。参数不再重挑，只看会不会大半都大亏。

    训练段没赚钱时，前面的时间检验已经拦住了，这里直接放行说明。
    """
    usable = [row for row in rows if not row.get("error")]
    if not usable:
        return True, "没有其它关注品种可做多品种验收。"
    if train_net <= 0:
        return True, "训练段没有赚钱，多品种结果只供查看。"
    bad: list[str] = []
    for row in usable:
        net = float(row.get("net_pnl") or 0)
        if net < -HOLDOUT_LOSS_RATIO * train_net:
            bad.append(str(row.get("symbol") or "?"))
    if len(bad) > len(usable) / 2:
        return (
            False,
            "在超过一半的其它关注品种上，亏掉的超过训练盈利的一半："
            + "、".join(bad)
            + "。这次不写入。",
        )
    if bad:
        return True, "其它关注品种里有大亏的（" + "、".join(bad) + "），但不到一半，可以写入，请先看一眼。"
    return True, "其它关注品种上没有大面积大亏，可以写入。"


# 公开渠道（MultiCharts / 期货滚动优化常见说法）里：
# 检验效率 = 检验净盈亏 ÷ 训练净盈亏，大约 0.5 以上算还能保住一半；低于 0.3 很虚。
# 检验段赚钱比例：至少一半的滚动段检验是赚的，才比较像稳健，而不是偶发一段。
MIN_WALK_EFFICIENCY = 0.30
MIN_OOS_WINDOW_WIN_RATE = 0.50


def walk_forward_metrics(
    folds: list[dict[str, Any]],
    *,
    train_net: float,
    hold_net: float,
) -> dict[str, Any]:
    """
    滚动前进的公开常用指标：检验效率、检验段赚钱比例、参数稳不稳。

    原理：只看已经跑完、有检验成绩的段。效率用最后一段训练净盈亏去除总检验净盈亏，
    和「拼接样本外曲线」同一套口径，方便和外面软件对照，不是另一套神秘公式。
    """
    scored = [row for row in folds if row.get("holdout_performance") and not row.get("error")]
    profitable = 0
    for row in scored:
        if float((row.get("holdout_performance") or {}).get("net_pnl") or 0) > 0:
            profitable += 1
    oos_win_rate = (profitable / len(scored)) if scored else 0.0
    if train_net > 1e-9:
        efficiency = float(hold_net) / float(train_net)
    else:
        efficiency = 0.0
    stability = _param_stability_text(scored)
    return {
        "fold_count": len(scored),
        "oos_profitable_folds": profitable,
        "oos_win_rate": round(oos_win_rate, 4),
        "walk_efficiency": round(efficiency, 4),
        "param_stability": stability,
    }


def _param_stability_text(folds: list[dict[str, Any]]) -> str:
    """相邻两段挑出的参数改了多少。改动特别多时提醒可能在贴合噪声。"""
    params_list = [dict(row.get("params") or {}) for row in folds if row.get("params")]
    if len(params_list) < 2:
        return "段数不够，看不出参数稳不稳。"
    jumps = 0
    for left, right in zip(params_list, params_list[1:]):
        keys = set(left) | set(right)
        jumps += sum(1 for key in keys if left.get(key) != right.get(key))
    avg = jumps / (len(params_list) - 1)
    if avg <= 1.0:
        return "相邻两段参数改动很少，比较稳。"
    if avg <= 3.0:
        return "相邻两段参数有一些改动，请对照每一段看一眼。"
    return "相邻两段参数改动较多，可能在贴合噪声，请谨慎写入。"


def judge_walk_forward(metrics: dict[str, Any]) -> tuple[bool, str]:
    """
    滚动前进的额外门槛（一次切断不算这些）。

    检验段赚钱比例过低，或检验效率过低，都不写入。
    段数少于 2 时只提示，不额外拦（行情太短时仍靠原来的总检验判断）。
    """
    folds = int(metrics.get("fold_count") or 0)
    if folds < 2:
        return True, "滚动段少于 2，稳健性指标只供查看。"
    oos_win = float(metrics.get("oos_win_rate") or 0)
    efficiency = float(metrics.get("walk_efficiency") or 0)
    if oos_win + 1e-9 < MIN_OOS_WINDOW_WIN_RATE:
        return (
            False,
            f"滚动检验里赚钱的段只有 {oos_win:.0%}，不到一半，这次不写入。",
        )
    if efficiency + 1e-9 < MIN_WALK_EFFICIENCY:
        return (
            False,
            f"检验效率 {efficiency:.2f}（检验净盈亏÷训练净盈亏）低于 {MIN_WALK_EFFICIENCY:.2f}，"
            "训练里的赚法在检验段保不住，这次不写入。",
        )
    return (
        True,
        f"滚动检验赚钱段 {oos_win:.0%}，检验效率 {efficiency:.2f}。"
        + str(metrics.get("param_stability") or ""),
    )


def once_cut_efficiency(train_net: float, hold_net: float) -> float:
    """一次切断也可看检验效率，方便和公开说法对照。"""
    if train_net > 1e-9:
        return round(float(hold_net) / float(train_net), 4)
    return 0.0


def format_perf_block(
    title: str,
    perf: dict[str, Any] | None,
    *,
    bars: int = 0,
    span: str = "",
    data_from: str = "",
    data_to: str = "",
) -> list[str]:
    """
    把一段绩效写成和历史回测「整体结果」同一套字段。

    训练成绩、检验成绩并排对照时用这一份，避免一边写简略、一边写详细。
    盈利覆盖、每笔收益、日均和交叉测试同一套算法，方便对照。
    """
    from src.screen.picker import format_cover_lines

    p = perf or {}
    lines = [f"—— {title} ——"]
    if bars:
        lines.append(f"K线根数   {bars}" + (f"（{span}）" if span else ""))
    elif span:
        lines.append(f"区间      {span}")
    lines.extend(
        [
            f"成交      {int(p.get('trades') or 0)} 笔"
            f"（赢{int(p.get('wins') or 0)} / 亏{int(p.get('losses') or 0)}）",
            f"胜率      {float(p.get('win_rate') or 0):.0%}",
            f"净盈亏    {float(p.get('net_pnl') or 0):.0f} 元",
            f"盈利因子  {float(p.get('profit_factor') or 0):.2f}",
            f"最大回撤  {float(p.get('max_drawdown') or 0):.0f} 元"
            f"（{float(p.get('max_drawdown_pct') or 0):.2%}）",
            f"综合得分  {float(p.get('score') or 0):.3f}",
        ]
    )
    lines.extend(
        format_cover_lines(
            p.get("net_pnl"),
            p.get("max_drawdown"),
            p.get("trades"),
            data_from,
            data_to,
        )
    )
    return lines


def _factory_values(engine: str) -> dict[str, Any]:
    """出厂参数。分数接近时用来判断哪一组改得更少。"""
    from src.backtest.param_history import FACTORY_DEFAULT_SNAPSHOT, _ENGINE_SECTION

    snap = FACTORY_DEFAULT_SNAPSHOT
    section = _ENGINE_SECTION.get(engine, "")
    values = dict(snap.get(section) or {})
    if engine == "daily_body":
        # eth_minutes / timeframe / 两个截止钟点也会被优化搜到，分数接近时要能和出厂比距离。
        body = snap.get("daily_body") or {}
        for key in ("eth_minutes", "day_cutoff", "night_cutoff"):
            if key not in values and key in body:
                values[key] = body[key]
        if "timeframe" not in values and "timeframe" in snap:
            values["timeframe"] = snap["timeframe"]
        # 交易时段写在 schedule 小节，出厂是全时段。
        values.setdefault("session_scope", "all")
    return values


def param_distance(params: dict[str, Any], factory: dict[str, Any]) -> float:
    """
    和出厂参数差多远。数字按出厂值的大小缩放，开关不同记 1。

    只在前一段分数接近时使用，不拿后一段的盈亏来决定留哪一组。
    """
    dist = 0.0
    for key, value in params.items():
        base = factory.get(key)
        if base is None:
            dist += 1.0
            continue
        # bool 也是 int 的一种，必须先比开关，不能当成数字去减。
        if isinstance(value, bool) or isinstance(base, bool):
            dist += 0.0 if bool(value) == bool(base) else 1.0
            continue
        if isinstance(value, (int, float)) and isinstance(base, (int, float)):
            scale = abs(float(base)) if abs(float(base)) > 1e-9 else 1.0
            dist += abs(float(value) - float(base)) / scale
            continue
        if value != base:
            dist += 1.0
    return dist


def pick_train_winner(trials: list[dict[str, Any]], engine: str) -> dict[str, Any] | None:
    """只看前一段分数。成交太少的组（分数约 -999）不参加。分数接近时留更靠近出厂的。"""
    usable = [
        row for row in trials if float(row.get("score", -999)) > -100 and row.get("params")
    ]
    if not usable:
        return None
    best_score = max(float(row["score"]) for row in usable)
    near = [row for row in usable if best_score - float(row["score"]) <= SCORE_TIE]
    factory = _factory_values(engine)
    near.sort(key=lambda row: (param_distance(row["params"], factory), -float(row["score"])))
    return near[0]


def judge_holdout(train_net: float, hold_net: float, hold_trades: int) -> tuple[bool, str]:
    """
    后一段只看一次，不再改参数。

    前一段没赚钱：不写入。
    后一段没开仓，或还在赚钱，或只是小亏：可以写入。
    后一段把前一段的盈利亏掉一半以上：不写入。
    """
    if train_net <= 0:
        return False, "前一段没有赚到钱，这组参数不写入。"
    if hold_trades <= 0:
        return (
            True,
            "后一段没有开仓。没有变成大亏，但不能当成后一段还会赚。可以写入，请先看一眼。",
        )
    if hold_net >= 0:
        return True, "后一段也是赚的，可以写入。"
    if abs(hold_net) > HOLDOUT_LOSS_RATIO * train_net:
        return (
            False,
            f"前一段净赚 {train_net:.0f}，后一段净亏 {hold_net:.0f}，"
            "亏掉的超过前一段盈利的一半，这次不写入。",
        )
    return (
        True,
        f"后一段小亏 {hold_net:.0f}，没有超过前一段盈利的一半，可以写入。请先看一眼。",
    )


def _stamp(value: Any) -> str:
    """K 线时间写成界面上好读的日期。"""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _equity_from_trades(trades: list[Any], capital: float) -> list[float]:
    """按平仓盈亏累加权益，用来算后一段的回撤。后一段不另跑网格。"""
    equity = [float(capital)]
    cash = float(capital)
    for trade in trades:
        cash += float(getattr(trade, "pnl", 0) or 0)
        equity.append(cash)
    return equity


def _naive_ts(value: Any) -> pd.Timestamp:
    """去掉时区再比时间。K 线索引和成交时间有一边带时区时，直接比会报错。"""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        return stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    return stamp


def _trades_on_or_after(trades: list[Any], start: Any) -> list[Any]:
    """只留下开仓时间落在后一段的成交。前一段已经平掉的单不拿来验收。"""
    start_ts = _naive_ts(start)
    kept: list[Any] = []
    for trade in trades:
        raw = getattr(trade, "entry_time", "") or ""
        if not raw:
            continue
        try:
            when = _naive_ts(raw)
        except Exception:
            continue
        if when >= start_ts:
            kept.append(trade)
    return kept


@dataclass
class OptimizationResult:
    symbol: str
    best_params: dict[str, Any]
    best_score: float
    best_performance: dict[str, Any]
    trials: list[dict[str, Any]] = field(default_factory=list)
    bars: int = 0
    engine: str = ""
    start_date: str = ""
    end_date: str = ""
    depth: str = "标准"
    grid_keys: list[str] = field(default_factory=list)
    grid_describe: str = ""
    # 最优那一组回测实际用过的开仓限制，方便在结果里核对盈亏是不是按实盘手数算的。
    best_limits: dict[str, Any] = field(default_factory=dict)
    # 后一段验收过了才允许写入。没过时 best_params 仍是前一段挑出的那组，只是不能保存。
    accepted: bool = False
    accept_reason: str = ""
    train_bars: int = 0
    holdout_bars: int = 0
    train_end: str = ""
    holdout_start: str = ""
    train_performance: dict[str, Any] = field(default_factory=dict)
    holdout_performance: dict[str, Any] = field(default_factory=dict)
    used_factory_tie: bool = False
    # 检验方式、检验比例、滚动每一段的明细、其它品种上的样本外成绩。
    mode: str = MODE_ONCE
    holdout_pct: float = DEFAULT_HOLDOUT_PCT
    walk_steps: int = 0
    walk_folds: list[dict[str, Any]] = field(default_factory=list)
    cross_symbol_rows: list[dict[str, Any]] = field(default_factory=list)
    # 公开渠道常用的稳健性数字：检验效率、滚动段赚钱比例、参数稳不稳说明。
    walk_efficiency: float = 0.0
    oos_win_rate: float = 0.0
    param_stability: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "engine": self.engine,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "depth": self.depth,
            "grid_keys": self.grid_keys,
            "grid_describe": self.grid_describe,
            "best_params": self.best_params,
            "best_score": self.best_score,
            "best_performance": self.best_performance,
            "trials": self.trials,
            "bars": self.bars,
            "best_limits": self.best_limits,
            "accepted": self.accepted,
            "accept_reason": self.accept_reason,
            "train_bars": self.train_bars,
            "holdout_bars": self.holdout_bars,
            "train_end": self.train_end,
            "holdout_start": self.holdout_start,
            "train_performance": self.train_performance,
            "holdout_performance": self.holdout_performance,
            "used_factory_tie": self.used_factory_tie,
            "mode": self.mode,
            "holdout_pct": self.holdout_pct,
            "walk_steps": self.walk_steps,
            "walk_folds": self.walk_folds,
            "cross_symbol_rows": self.cross_symbol_rows,
            "walk_efficiency": self.walk_efficiency,
            "oos_win_rate": self.oos_win_rate,
            "param_stability": self.param_stability,
        }


# 开仓截止钟点写回 daily_body；读不出来就用出厂的 10:30 / 22:15。
_CUTOFF_DEFAULTS = {"day_cutoff": "10:30", "night_cutoff": "22:15"}


def _cutoff_text(value: Any, fallback: str) -> str:
    """把钟点写成两位的 HH:MM；填错就用出厂值。"""
    try:
        hour, minute = str(value).strip().split(":")
        hour_i, minute_i = int(hour), int(minute)
    except (TypeError, ValueError):
        return fallback
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return fallback
    return f"{hour_i:02d}:{minute_i:02d}"


def _apply_params(cfg: dict[str, Any], params: dict[str, Any], engine: str | None = None) -> dict[str, Any]:
    """
    把一组参数打进配置副本。

    engine 指定写进哪一套。只改那一节，不改配置里正在使用的策略名字。
    没指定时，仍按配置里的 strategy.engine 决定写到哪一节。
    """
    cfg = copy.deepcopy(cfg)
    st = cfg.setdefault("strategy", {})
    engine = str(engine or st.get("engine") or "daily_body").lower().strip()
    if engine in ("ma20_trend", "ma20"):
        engine = "single_ma"
    if engine == "trend_follow":
        trend = st.setdefault("trend_follow", {})
        for k, v in params.items():
            trend[k] = v
    elif engine in ("supertrend", "st"):
        box = st.setdefault("supertrend", {})
        for k, v in params.items():
            box[k] = v
    elif engine == "single_ma":
        box = st.setdefault("single_ma", {})
        for k, v in params.items():
            box[k] = v
        st.pop("ma20_trend", None)
    elif engine in ("dual_ma", "ma_cross"):
        box = st.setdefault("dual_ma", {})
        for k, v in params.items():
            box[k] = v
    elif engine == "daily_body":
        body = st.setdefault("daily_body", {})
        for k, v in params.items():
            if k == "eth_minutes":
                # 与配置页「ETH分钟数」同一项；写在 daily_body 小节。
                try:
                    body["eth_minutes"] = max(0, int(v))
                except (TypeError, ValueError):
                    body["eth_minutes"] = 20
            elif k == "timeframe":
                tf = str(v).strip() or "5"
                st["timeframe"] = tf
                # 日内实体止损不另用更粗周期，跟信号周期保持一致，避免优出一套用不上的止损周期。
                st["trail_timeframe"] = tf
            elif k == "session_scope":
                # 公共设置，写在 schedule 小节；和配置页「交易时段」同一项。
                cfg.setdefault("schedule", {})["session_scope"] = normalize_scope(v)
            elif k in _CUTOFF_DEFAULTS:
                # 开仓截止钟点，和配置页「日盘/夜盘开仓截止」同一项。
                body[k] = _cutoff_text(v, _CUTOFF_DEFAULTS[k])
            else:
                body[k] = v
    elif engine in _ENGINE_PARAM_SECTION:
        box = st.setdefault(_ENGINE_PARAM_SECTION[engine], {})
        for k, v in params.items():
            box[k] = v
    else:
        # 未单独列出的引擎：参数写在 strategy 根上
        for k, v in params.items():
            st[k] = v
    return cfg


def _slice_trail(bars_trail: pd.DataFrame | None, end_ts: Any) -> pd.DataFrame | None:
    """止损周期 K 线截到训练段结束时刻。没有止损线就返回 None。"""
    if bars_trail is None or not len(bars_trail):
        return None
    return bars_trail.loc[bars_trail.index <= end_ts]


def _score_grid_on_slice(
    cfg: dict[str, Any],
    *,
    symbol: str,
    engine_name: str,
    combos: list[tuple[Any, ...]],
    keys: list[str],
    train_signal: pd.DataFrame,
    train_trail: pd.DataFrame | None,
    min_trades: int,
    fold_tag: str = "",
) -> list[dict[str, Any]]:
    """只在给定训练段上给每一组参数打分。检验段绝不走进这里。"""
    from src.backtest.metrics import calc_performance

    trials: list[dict[str, Any]] = []
    prefix = f"{fold_tag} " if fold_tag else ""
    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        try:
            result = run_backtest(
                _apply_params(cfg, params, engine_name),
                symbol=symbol,
                bars_signal=train_signal,
                bars_trail=train_trail,
                engine=engine_name,
            )
            perf = calc_performance(result.trades, result.equity, min_trades=min_trades)
            trials.append({"params": params, "score": perf.score, "performance": perf.to_dict()})
            logger.info(
                "{}[{}/{}] 训练 score={:.3f} trades={} net={:.0f} | {}",
                prefix,
                i,
                len(combos),
                perf.score,
                perf.trades,
                perf.net_pnl,
                params,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("{}参数组失败 {}: {}", prefix, params, e)
            trials.append({"params": params, "score": -999, "error": str(e)})
    trials.sort(key=lambda x: float(x.get("score", -999)), reverse=True)
    return trials


def _holdout_perf_for_params(
    cfg: dict[str, Any],
    *,
    symbol: str,
    engine_name: str,
    params: dict[str, Any],
    bars_signal: pd.DataFrame,
    bars_trail: pd.DataFrame | None,
    holdout_start_ts: Any,
    holdout_end_ts: Any | None = None,
) -> tuple[dict[str, Any], list[Any]]:
    """
    用固定参数跑整段（或到 holdout_end），只统计检验段新开的仓。

    返回 (绩效字典, 检验段成交列表)。滚动前进把各段成交拼起来时，不再重跑一遍。
    不会用检验段的盈亏去换另一组参数。
    """
    from src.backtest.metrics import calc_performance

    trail = bars_trail
    signal = bars_signal
    if holdout_end_ts is not None:
        signal = bars_signal.loc[bars_signal.index <= holdout_end_ts]
        trail = _slice_trail(bars_trail, holdout_end_ts)
    full = run_backtest(
        _apply_params(cfg, params, engine_name),
        symbol=symbol,
        bars_signal=signal,
        bars_trail=trail,
        engine=engine_name,
    )
    hold_trades = _trades_on_or_after(full.trades, holdout_start_ts)
    if holdout_end_ts is not None:
        end_ts = _naive_ts(holdout_end_ts)
        kept = []
        for trade in hold_trades:
            raw = getattr(trade, "entry_time", "") or ""
            try:
                when = _naive_ts(raw)
            except Exception:
                continue
            if when <= end_ts:
                kept.append(trade)
        hold_trades = kept
    capital = float((cfg.get("risk") or {}).get("capital") or 1_000_000)
    hold = calc_performance(
        hold_trades,
        _equity_from_trades(hold_trades, capital),
        min_trades=1,
    )
    return hold.to_dict(), hold_trades


def validate_params_on_symbols(
    cfg: dict[str, Any],
    *,
    params: dict[str, Any],
    engine: str,
    symbols: list[str],
    skip_symbol: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """
    把已经挑好的参数，原样拿到其它合约上跑一遍。

    原理：这才是多品种样本外验收。交叉测试页的「每套策略×每个合约」用的是配置里的固定参数，
    那是对照筛选，不会在这里重新搜旋钮。
    """
    from src.backtest.metrics import calc_performance

    cfg = copy.deepcopy(cfg)
    st = cfg.setdefault("strategy", {})
    st["engine"] = str(engine)
    # 先把最优参数写进配置，再用里面的信号周期拉 K 线（日内实体可能优出 5 或 15）。
    cfg = _apply_params(cfg, params, engine)
    st = cfg.setdefault("strategy", {})
    provider = KlineProvider(cfg)
    tf = str(st.get("timeframe", "15"))
    trail_tf = str(st.get("trail_timeframe", tf))
    max_bars = int((cfg.get("backtest") or {}).get("max_bars", 1200))
    skip = str(skip_symbol or "").strip().lower()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in symbols:
        code = str(raw or "").strip()
        if not code or code == "（还没有）":
            continue
        key = code.lower()
        if key == skip or key in seen:
            continue
        seen.add(key)
        try:
            bars_signal = provider.fetch(
                code, period=tf, max_bars=max_bars, keep_history=True, start=start_date, end=end_date
            )
            bars_trail = None
            if trail_tf != tf:
                try:
                    bars_trail = provider.fetch(
                        code,
                        period=trail_tf,
                        max_bars=max_bars,
                        keep_history=True,
                        start=start_date,
                        end=end_date,
                    )
                except Exception:  # noqa: BLE001
                    bars_trail = None
            bars_signal = filter_bars_by_range(bars_signal, start_date, end_date)
            bars_trail = filter_bars_by_range(bars_trail, start_date, end_date)
            if bars_signal is None or len(bars_signal) < 30:
                rows.append({"symbol": code, "error": "K线不足", "net_pnl": 0, "trades": 0})
                continue
            result = run_backtest(
                cfg,
                symbol=code,
                bars_signal=bars_signal,
                bars_trail=bars_trail,
                engine=engine,
            )
            perf = calc_performance(result.trades, result.equity, min_trades=1)
            row = {"symbol": code, **perf.to_dict(), "bars": len(bars_signal)}
            rows.append(row)
            logger.info(
                "多品种验收 {} trades={} net={:.0f} pf={:.2f}",
                code,
                perf.trades,
                perf.net_pnl,
                perf.profit_factor,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("多品种验收失败 {}: {}", code, e)
            rows.append({"symbol": code, "error": str(e), "net_pnl": 0, "trades": 0})
    return rows


def optimize_strategy(
    cfg: dict[str, Any],
    *,
    symbol: str | None = None,
    grid: dict[str, list[Any]] | None = None,
    max_trials: int = 80,
    min_trades: int = 3,
    start_date: str | None = None,
    end_date: str | None = None,
    engine: str | None = None,
    depth: str = "标准",
    seed: int = 42,
    holdout_pct: float | int | str | None = None,
    mode: str = MODE_ONCE,
    walk_steps: int = DEFAULT_WALK_STEPS,
    cross_symbols: list[str] | None = None,
) -> OptimizationResult:
    """
    网格搜索最优参数。

    一次切断：按检验比例切开，前一段打分，后一段只验收。
    滚动前进：把检验段拆成多段，每段先在更早行情上挑参数，再验下一段；写入用最后一段挑出的参数。
    多品种：参数在优化合约上过关后，再拿到其它关注品种上原样验收。
    """
    from src.backtest.metrics import calc_performance

    cfg = copy.deepcopy(cfg)
    st = cfg.setdefault("strategy", {})
    if engine:
        st["engine"] = str(engine)
    engine_name = str(st.get("engine", "daily_body"))
    depth = (depth or "标准").strip() or "标准"
    mode = str(mode or MODE_ONCE).strip() or MODE_ONCE
    if mode not in (MODE_ONCE, MODE_WALK):
        mode = MODE_ONCE
    holdout_pct = clamp_holdout_pct(holdout_pct)
    train_ratio = train_ratio_from_holdout_pct(holdout_pct)
    walk_steps = max(1, int(walk_steps or DEFAULT_WALK_STEPS))
    grid = grid or grid_for_engine(engine_name, depth)
    symbol = symbol or (st.get("symbols") or [cfg.get("trade", {}).get("symbol", "bu2612")])[0]

    provider = KlineProvider(cfg)
    # 网格若含 timeframe，用最细一档拉信号 K；策略按每组参数自己的周期判断（细周期可收成粗周期）。
    tf = _finest_grid_timeframe(grid, str(st.get("timeframe", "15")))
    trail_tf = str(st.get("trail_timeframe", tf))
    if engine_name == "daily_body":
        trail_tf = tf
    bt_cfg = cfg.get("backtest") or {}
    max_bars = int(bt_cfg.get("max_bars", 1200))
    if "timeframe" in (grid or {}) and tf != str(st.get("timeframe", "")):
        logger.info(
            "优化按网格最细信号周期拉K线: {} 分钟（配置里原是 {}）",
            tf,
            st.get("timeframe"),
        )

    bars_signal = provider.fetch(
        symbol,
        period=tf,
        max_bars=max_bars,
        keep_history=True,
        start=start_date,
        end=end_date,
    )
    bars_trail = None
    if trail_tf != tf:
        try:
            bars_trail = provider.fetch(
                symbol,
                period=trail_tf,
                max_bars=max_bars,
                keep_history=True,
                start=start_date,
                end=end_date,
            )
        except Exception:  # noqa: BLE001
            bars_trail = None

    bars_signal = filter_bars_by_range(bars_signal, start_date, end_date)
    bars_trail = filter_bars_by_range(bars_trail, start_date, end_date)

    if bars_signal is None or len(bars_signal) < 50:
        raise RuntimeError(
            f"优化用K线不足 bars={0 if bars_signal is None else len(bars_signal)}，"
            "请改用「全部可用」时间段或检查网络。"
        )

    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    full_n = 1
    for vs in values:
        full_n *= max(len(vs), 1)
    combos = _sample_combos(keys, values, max_trials, seed=seed)
    if full_n > len(combos):
        logger.warning(
            "理论组合 {} 组，随机抽 {} 组试跑（depth={} seed={}），避免只扫到网格前几格",
            full_n,
            len(combos),
            depth,
            seed,
        )
    grid_text = describe_grid(grid)

    trials: list[dict[str, Any]] = []
    walk_folds: list[dict[str, Any]] = []
    best_params: dict[str, Any] = {}
    best_score = -999.0
    train_perf: dict[str, Any] = {}
    hold_perf: dict[str, Any] = {}
    best_result: BacktestResult | None = None
    accepted = False
    reason = "前一段成交太少，没有挑出能用的参数，这次不写入。"
    used_tie = False
    train_bars = 0
    holdout_bars = 0
    train_end = ""
    holdout_start = ""
    actual_steps = 0

    if mode == MODE_WALK:
        folds = walk_forward_folds(len(bars_signal), holdout_pct, walk_steps)
        actual_steps = len(folds)
        all_oos_trades: list[Any] = []
        logger.info(
            "滚动前进优化 symbol={} engine={} 检验比例={}% 计划{}段实际{}段 K线={} 实跑每段{}组",
            symbol,
            engine_name,
            holdout_pct,
            walk_steps,
            actual_steps,
            len(bars_signal),
            len(combos),
        )
        for fold_i, (train_end_i, oos_end_i) in enumerate(folds, 1):
            train_end_ts = bars_signal.index[train_end_i - 1]
            oos_start_ts = bars_signal.index[train_end_i]
            oos_end_ts = bars_signal.index[oos_end_i - 1]
            train_signal = bars_signal.iloc[:train_end_i]
            train_trail = _slice_trail(bars_trail, train_end_ts)
            fold_trials = _score_grid_on_slice(
                cfg,
                symbol=symbol,
                engine_name=engine_name,
                combos=combos,
                keys=keys,
                train_signal=train_signal,
                train_trail=train_trail,
                min_trades=min_trades,
                fold_tag=f"第{fold_i}段",
            )
            winner = pick_train_winner(fold_trials, engine_name)
            fold_row: dict[str, Any] = {
                "fold": fold_i,
                "train_bars": train_end_i,
                "holdout_bars": oos_end_i - train_end_i,
                "train_end": _stamp(train_end_ts),
                "holdout_start": _stamp(oos_start_ts),
                "holdout_end": _stamp(oos_end_ts),
            }
            if winner is None:
                fold_row["error"] = "训练段没有可用参数"
                walk_folds.append(fold_row)
                continue
            fold_params = dict(winner["params"])
            fold_train = dict(winner.get("performance") or {})
            fold_hold, segment_trades = _holdout_perf_for_params(
                cfg,
                symbol=symbol,
                engine_name=engine_name,
                params=fold_params,
                bars_signal=bars_signal,
                bars_trail=bars_trail,
                holdout_start_ts=oos_start_ts,
                holdout_end_ts=oos_end_ts,
            )
            # 各段检验成交拼在一起，最后算总检验成绩。不再为同一段重跑一遍。
            all_oos_trades.extend(segment_trades)
            fold_ok, fold_reason = judge_holdout(
                float(fold_train.get("net_pnl") or 0),
                float(fold_hold.get("net_pnl") or 0),
                int(fold_hold.get("trades") or 0),
            )
            fold_row.update(
                {
                    "params": fold_params,
                    "train_performance": fold_train,
                    "holdout_performance": fold_hold,
                    "accepted": fold_ok,
                    "accept_reason": fold_reason,
                    "score": float(winner.get("score") or -999),
                }
            )
            walk_folds.append(fold_row)
            # 最后一段的参数才拿去写入；前面几段只负责检验稳健性。
            trials = fold_trials
            best_params = fold_params
            best_score = float(winner.get("score") or -999)
            train_perf = fold_train
            train_bars = train_end_i
            holdout_bars = oos_end_i - train_end_i
            train_end = _stamp(train_end_ts)
            holdout_start = _stamp(oos_start_ts)
            top_score = max(float(row.get("score", -999)) for row in fold_trials)
            used_tie = best_score + 1e-9 < top_score
            best_result = run_backtest(
                _apply_params(cfg, best_params, engine_name),
                symbol=symbol,
                bars_signal=train_signal,
                bars_trail=train_trail,
                engine=engine_name,
            )

        capital = float((cfg.get("risk") or {}).get("capital") or 1_000_000)
        hold = calc_performance(
            all_oos_trades,
            _equity_from_trades(all_oos_trades, capital),
            min_trades=1,
        )
        hold_perf = hold.to_dict()
        holdout_bars = sum(int(f.get("holdout_bars") or 0) for f in walk_folds)
        walk_metrics = walk_forward_metrics(
            walk_folds,
            train_net=float(train_perf.get("net_pnl") or 0),
            hold_net=float(hold.net_pnl),
        )
        if best_params:
            accepted, reason = judge_holdout(
                float(train_perf.get("net_pnl") or 0),
                float(hold.net_pnl),
                int(hold.trades),
            )
            walk_ok, walk_reason = judge_walk_forward(walk_metrics)
            if accepted and not walk_ok:
                accepted = False
                reason = walk_reason
            elif walk_reason:
                reason = reason + walk_reason
            if used_tie:
                reason = "最后一段训练分数接近，留了更靠近出厂参数的一组。" + reason
            reason = f"滚动前进共 {actual_steps} 段。" + reason
        else:
            reason = "滚动前进每一段都没有挑出能用的参数，这次不写入。"
            walk_metrics = walk_forward_metrics(walk_folds, train_net=0.0, hold_net=0.0)
    else:
        walk_metrics = {
            "fold_count": 0,
            "oos_profitable_folds": 0,
            "oos_win_rate": 0.0,
            "walk_efficiency": 0.0,
            "param_stability": "",
        }
        cut = split_train_holdout(len(bars_signal), train_ratio)
        train_end_ts = bars_signal.index[cut - 1]
        holdout_start_ts = bars_signal.index[cut]
        train_signal = bars_signal.iloc[:cut]
        train_trail = _slice_trail(bars_trail, train_end_ts)
        train_bars = len(train_signal)
        holdout_bars = len(bars_signal) - cut
        train_end = _stamp(train_end_ts)
        holdout_start = _stamp(holdout_start_ts)

        logger.info(
            "一次切断优化 symbol={} engine={} 检验比例={}% 训练{}根 检验从{}起 实跑{}组",
            symbol,
            engine_name,
            holdout_pct,
            train_bars,
            holdout_start,
            len(combos),
        )
        trials = _score_grid_on_slice(
            cfg,
            symbol=symbol,
            engine_name=engine_name,
            combos=combos,
            keys=keys,
            train_signal=train_signal,
            train_trail=train_trail,
            min_trades=min_trades,
        )
        winner = pick_train_winner(trials, engine_name)
        if winner is not None:
            best_params = dict(winner["params"])
            best_score = float(winner["score"])
            train_perf = dict(winner.get("performance") or {})
            top_score = max(float(row.get("score", -999)) for row in trials)
            used_tie = best_score + 1e-9 < top_score
            best_result = run_backtest(
                _apply_params(cfg, best_params, engine_name),
                symbol=symbol,
                bars_signal=train_signal,
                bars_trail=train_trail,
                engine=engine_name,
            )
            hold_perf, _hold_trades = _holdout_perf_for_params(
                cfg,
                symbol=symbol,
                engine_name=engine_name,
                params=best_params,
                bars_signal=bars_signal,
                bars_trail=bars_trail,
                holdout_start_ts=holdout_start_ts,
            )
            accepted, reason = judge_holdout(
                float(train_perf.get("net_pnl") or 0),
                float(hold_perf.get("net_pnl") or 0),
                int(hold_perf.get("trades") or 0),
            )
            walk_metrics["walk_efficiency"] = once_cut_efficiency(
                float(train_perf.get("net_pnl") or 0),
                float(hold_perf.get("net_pnl") or 0),
            )
            if used_tie:
                reason = "前一段分数接近，留了更靠近出厂参数的一组。" + reason

    cross_rows: list[dict[str, Any]] = []
    if best_params and cross_symbols:
        cross_rows = validate_params_on_symbols(
            cfg,
            params=best_params,
            engine=engine_name,
            symbols=list(cross_symbols),
            skip_symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )
        cross_ok, cross_reason = judge_cross_symbols(
            float(train_perf.get("net_pnl") or 0),
            cross_rows,
        )
        if cross_reason:
            reason = reason + cross_reason
        if accepted and not cross_ok:
            accepted = False
            reason = cross_reason

    opt = OptimizationResult(
        symbol=symbol,
        best_params=best_params,
        best_score=float(best_score) if best_params else -999.0,
        best_performance=train_perf,
        trials=trials,
        bars=len(bars_signal),
        engine=engine_name,
        start_date=str(start_date or ""),
        end_date=str(end_date or ""),
        depth=depth,
        grid_keys=list(keys),
        grid_describe=grid_text,
        best_limits=dict((best_result.params_snapshot if best_result is not None else {}) or {}),
        accepted=accepted,
        accept_reason=reason,
        train_bars=train_bars,
        holdout_bars=holdout_bars,
        train_end=train_end,
        holdout_start=holdout_start,
        train_performance=train_perf,
        holdout_performance=hold_perf,
        used_factory_tie=used_tie,
        mode=mode,
        holdout_pct=float(holdout_pct),
        walk_steps=actual_steps if mode == MODE_WALK else 0,
        walk_folds=walk_folds,
        cross_symbol_rows=cross_rows,
        walk_efficiency=float(walk_metrics.get("walk_efficiency") or 0),
        oos_win_rate=float(walk_metrics.get("oos_win_rate") or 0),
        param_stability=str(walk_metrics.get("param_stability") or ""),
    )
    return opt


def apply_run_switches(
    cfg: dict[str, Any],
    engine: str | None,
    *,
    flatten_at_close: bool,
) -> dict[str, Any]:
    """
    把这次优化实际用来打分的开关写进配置。

    不改正在用的策略名字、品种和周期。
    听「收盘清仓」总开关的策略会写入 flatten_at_close。
    趋势跟随 / SuperTrend 不听这个开关，所以不写。
    """
    out = copy.deepcopy(cfg)
    st = out.setdefault("strategy", {})
    name = str(engine or "")
    if name in (
        "daily_body",
        "single_ma",
        "dual_ma",
        "ma_cross",
        "morning_box_a",
        "morning_a",
        "morning_structure_b",
        "morning_b",
    ):
        st["flatten_at_close"] = bool(flatten_at_close)
    return out


def describe_run_switches(
    engine: str | None,
    *,
    flatten_at_close: bool,
) -> str:
    """写成给人看的几行，说明这次打分到底听了哪些开关。"""
    name = str(engine or "")
    if name == "trend_follow":
        return "开关      趋势跟随不听收盘清仓，用它自己的设置。"
    if name in ("supertrend", "st"):
        return "开关      SuperTrend 默认不听收盘清仓总开关；翻向可反手，靠通道线离场。"
    if name == "single_ma":
        flat = "开" if flatten_at_close else "关"
        return (
            f"收盘清仓  {flat}\n"
            "单均线系统（Single_ma）听配置页「收盘清仓」总开关；收盘穿越均线开仓/反手，止损跟均线。根数可设。"
        )
    if name in ("dual_ma", "ma_cross"):
        flat = "开" if flatten_at_close else "关"
        return (
            f"收盘清仓  {flat}\n"
            "双均线趋势听配置页「收盘清仓」总开关；短线上穿长线做多、下穿做空，止损跟长均线。"
        )
    flat = "开" if flatten_at_close else "关"
    if name in ("morning_box_a", "morning_a"):
        return (
            f"收盘清仓  {flat}\n"
            "09:00–09:30 记箱体；之后放量收盘突破定方向；回踩边界入场；"
            "浮盈达箱体高度比例后移损到成本；否则拿到收盘。单日最多两笔。"
        )
    if name in ("morning_structure_b", "morning_b"):
        return (
            f"收盘清仓  {flat}\n"
            "用 09:00–09:30 高低点递进结构定方向；入场、保本、名额与箱体方案相同。"
        )
    if name == "daily_body":
        return (
            f"收盘清仓  {flat}\n"
            "开仓：ETH 画开盘箱体，信号周期收盘站上箱顶做多、跌破箱底做空；影线刺出不算；"
            "日盘约 10:30、夜盘约 22:15 后不追；日盘/夜盘各只开一次。\n"
            "止损：开盘箱体对沿再让 1 跳并冻结（多守箱底、空守箱顶）；"
            "打破对沿才平，中间不做回撤止损，否则拿到收盘。\n"
            "策略优化只搜：ETH 分钟、信号周期（组合很少，避免过拟合）。"
        )
    return f"收盘清仓  {flat}"


def apply_params_to_config(
    cfg: dict[str, Any],
    params: dict[str, Any],
    engine: str | None = None,
) -> dict[str, Any]:
    """把最优参数写进配置字典（供保存）。engine 有值时只写那一套，不切换正在用的策略。"""
    return _apply_params(cfg, params, engine=engine)


def save_optimization_report(result: OptimizationResult, out_path: Path | str) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 报告太大时只保留前 30 名明细
    payload = result.to_dict()
    payload["trials"] = payload["trials"][:30]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
