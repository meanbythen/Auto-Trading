"""斜率闸门 A/B：按时间折比较不同斜率阈值，判断是否够格改默认。

只读回测结果里的权益曲线和成交笔数，不改配置、不下单。
折的切法：取所有品种共同的起止时间，等分成 N 段；前 N-1 段算样本内，最后一段算样本外。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd

TZ = "Asia/Shanghai"
EquitySeries = Sequence[tuple[Sequence[str], Sequence[float]]]


@dataclass(frozen=True)
class SegmentStat:
    """一段时间里的净盈亏、最大回撤和 Calmar。"""

    label: str
    start: str
    end: str
    bars: int
    net: float
    max_drawdown: float
    calmar: float


@dataclass(frozen=True)
class ThresholdStat:
    """一个斜率阈值在全部品种上的整体表现。"""

    threshold: float
    trades: int
    folds: tuple[SegmentStat, ...]
    in_sample: SegmentStat
    out_sample: SegmentStat
    net: float
    max_drawdown: float

    @property
    def calmar(self) -> float:
        return _calmar(self.net, self.max_drawdown)

    @property
    def positive_folds(self) -> int:
        return sum(1 for fold in self.folds if fold.net > 0)


@dataclass(frozen=True)
class Verdict:
    """候选阈值是否达到切换条件。"""

    passes: bool
    reasons: tuple[str, ...]


def _calmar(net: float, drawdown: float) -> float:
    if drawdown > 1e-9:
        return float(net) / float(drawdown)
    return float("inf") if net > 0 else 0.0


def _naive_ts(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(TZ).tz_localize(None)
    return stamp


def _segment(
    series: EquitySeries,
    start: pd.Timestamp,
    end: pd.Timestamp,
    label: str,
    *,
    include_end: bool = False,
) -> SegmentStat:
    """把每个品种这段权益拼起来：净盈亏和回撤相加，Calmar 用合计值算。"""
    net = 0.0
    drawdown = 0.0
    bars = 0
    for times, equity in series:
        stamps = [_naive_ts(item) for item in times]
        values = [float(item) for item in equity]
        size = min(len(stamps), len(values))
        base = values[0] if values else 0.0
        picked: list[int] = []
        for i in range(size):
            stamp = stamps[i]
            if stamp is None:
                continue
            if stamp < start:
                base = values[i]
                continue
            if stamp > end or (stamp == end and not include_end):
                break
            picked.append(i)
        if not picked:
            continue
        curve = [base] + [values[i] for i in picked]
        bars += len(picked)
        net += curve[-1] - curve[0]
        peak = curve[0]
        symbol_drawdown = 0.0
        for value in curve:
            peak = max(peak, value)
            symbol_drawdown = max(symbol_drawdown, peak - value)
        drawdown += symbol_drawdown
    return SegmentStat(
        label,
        start.strftime("%Y-%m-%d %H:%M"),
        end.strftime("%Y-%m-%d %H:%M"),
        bars,
        net,
        drawdown,
        _calmar(net, drawdown),
    )


def fold_bounds(series: EquitySeries, folds: int = 3) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """所有品种共同的起止时间里等分。没有可用时间时返回空。"""
    count = max(2, int(folds))
    stamps = [_naive_ts(item) for times, _ in series for item in times]
    valid = [stamp for stamp in stamps if stamp is not None]
    if not valid:
        return []
    first, last = min(valid), max(valid)
    span = last - first
    return [(first + span * k / count, first + span * (k + 1) / count) for k in range(count)]


def threshold_stat(
    series: EquitySeries,
    threshold: float,
    trades: int,
    *,
    folds: int = 3,
) -> ThresholdStat:
    """算一个阈值的整体、样本内、样本外和各折表现。"""
    bounds = fold_bounds(series, folds)
    if not bounds:
        empty = SegmentStat("空", "", "", 0, 0.0, 0.0, 0.0)
        return ThresholdStat(threshold, trades, (), empty, empty, 0.0, 0.0)
    fold_stats = tuple(
        _segment(series, start, end, f"第{k + 1}折", include_end=k == len(bounds) - 1)
        for k, (start, end) in enumerate(bounds)
    )
    in_sample = _segment(series, bounds[0][0], bounds[-2][1], "样本内")
    out_sample = _segment(series, bounds[-2][1], bounds[-1][1], "样本外", include_end=True)
    whole = _segment(series, bounds[0][0], bounds[-1][1], "全段", include_end=True)
    return ThresholdStat(
        threshold,
        trades,
        fold_stats,
        in_sample,
        out_sample,
        whole.net,
        whole.max_drawdown,
    )


def judge(
    candidate: ThresholdStat,
    baseline: ThresholdStat,
    neighbors: dict[float, float],
    *,
    min_trades: int = 30,
    calmar_ratio: float = 0.7,
) -> Verdict:
    """按设计文档的验收线判断候选阈值，并额外要求样本外不输给基线。"""
    reasons: list[str] = []
    if candidate.trades < min_trades:
        reasons.append(f"成交 {candidate.trades} 笔，少于 {min_trades} 笔")
    if candidate.positive_folds < 2:
        reasons.append(f"正收益折数 {candidate.positive_folds}/{len(candidate.folds)}，少于 2")
    in_calmar = candidate.in_sample.calmar
    out_calmar = candidate.out_sample.calmar
    if in_calmar > 0:
        if out_calmar < calmar_ratio * in_calmar:
            reasons.append(
                f"样本外 Calmar {out_calmar:.2f} 低于 {calmar_ratio:g}×样本内 {in_calmar:.2f}"
            )
    elif out_calmar <= 0:
        reasons.append(f"样本内外 Calmar 都不为正（{in_calmar:.2f} / {out_calmar:.2f}）")
    if candidate.out_sample.net < baseline.out_sample.net:
        reasons.append(
            f"样本外净盈亏 {candidate.out_sample.net:.0f} 不如基线 {baseline.out_sample.net:.0f}"
        )
    # 换默认值不能把全段风险收益比做差；只看样本外容易押中某一段好行情。
    if candidate.calmar < baseline.calmar:
        reasons.append(f"全段 Calmar {candidate.calmar:.2f} 低于基线 {baseline.calmar:.2f}")
    for threshold in sorted(neighbors):
        if (candidate.net > 0) != (neighbors[threshold] > 0):
            reasons.append(f"邻域 {threshold:g}° 净盈亏 {neighbors[threshold]:.0f} 方向相反")
            break
    return Verdict(not reasons, tuple(reasons))


def neighbor_map(stats: dict[float, ThresholdStat]) -> dict[float, dict[float, float]]:
    """每个阈值只和紧挨着的两个候选比，避免跨多档比较。"""
    order = sorted(stats)
    out: dict[float, dict[float, float]] = {}
    for index, threshold in enumerate(order):
        near: dict[float, float] = {}
        if index > 0:
            left = order[index - 1]
            near[left] = stats[left].net
        if index + 1 < len(order):
            right = order[index + 1]
            near[right] = stats[right].net
        out[threshold] = near
    return out


def select_threshold(
    stats: dict[float, ThresholdStat],
    baseline_threshold: float,
    *,
    min_trades: int = 30,
    calmar_ratio: float = 0.7,
) -> tuple[float, dict[float, Verdict]]:
    """返回建议阈值和每个候选的判定。没有候选达标时保留基线。"""
    if not stats:
        return baseline_threshold, {}
    if baseline_threshold not in stats:
        baseline_threshold = min(stats, key=lambda value: abs(value - baseline_threshold))
    base = stats[baseline_threshold]
    near = neighbor_map(stats)
    verdicts = {
        threshold: judge(stat, base, near.get(threshold, {}), min_trades=min_trades, calmar_ratio=calmar_ratio)
        for threshold, stat in stats.items()
    }
    passing = [
        stat
        for threshold, stat in sorted(stats.items())
        if threshold != baseline_threshold and verdicts[threshold].passes
    ]
    if not passing:
        return baseline_threshold, verdicts
    passing.sort(key=lambda stat: (-stat.out_sample.calmar, stat.threshold))
    return passing[0].threshold, verdicts
