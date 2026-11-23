"""斜率阈值 A/B：时间折、Calmar、验收线和阈值选择。"""

from src.backtest.slope_gate import (
    SegmentStat,
    ThresholdStat,
    judge,
    select_threshold,
    threshold_stat,
)


def _seg(label: str, net: float, drawdown: float) -> SegmentStat:
    calmar = net / drawdown if drawdown > 1e-9 else (float("inf") if net > 0 else 0.0)
    return SegmentStat(label, "2026-01-01 00:00", "2026-02-01 00:00", 10, net, drawdown, calmar)


def _stat(
    threshold: float,
    *,
    trades: int = 40,
    folds: tuple[float, ...] = (100.0, 50.0, 20.0),
    in_net: float = 150.0,
    in_dd: float = 50.0,
    out_net: float = 20.0,
    out_dd: float = 40.0,
    net: float = 170.0,
    dd: float = 90.0,
) -> ThresholdStat:
    return ThresholdStat(
        threshold=threshold,
        trades=trades,
        folds=tuple(_seg(f"第{i + 1}折", value, 0.0) for i, value in enumerate(folds)),
        in_sample=_seg("样本内", in_net, in_dd),
        out_sample=_seg("样本外", out_net, out_dd),
        net=net,
        max_drawdown=dd,
    )


def test_threshold_stat_splits_common_timeline() -> None:
    times = ["2026-01-01 15:00", "2026-01-11 15:00", "2026-01-21 15:00", "2026-01-31 15:00"]
    stat = threshold_stat([(times, [100.0, 110.0, 90.0, 120.0])], 3.0, 4, folds=3)
    assert len(stat.folds) == 3
    assert stat.net == 20.0
    assert stat.max_drawdown == 20.0
    assert stat.in_sample.net == 10.0
    assert stat.out_sample.net == 10.0
    assert stat.out_sample.max_drawdown == 20.0
    assert stat.out_sample.calmar == 0.5
    # 折的净盈亏从上一折末的权益接续算：第 2、3 折各 +10，第 1 折 0。
    assert stat.positive_folds == 2


def test_judge_passes_when_all_lines_met() -> None:
    base = _stat(3.0, out_net=10.0)
    candidate = _stat(4.0, in_dd=300.0)
    verdict = judge(candidate, base, {3.0: 120.0, 5.0: 80.0})
    assert verdict.passes, verdict.reasons


def test_judge_rejects_small_sample_and_weak_oos() -> None:
    base = _stat(3.0, out_net=10.0)
    small = judge(_stat(4.0, trades=8), base, {3.0: 100.0, 5.0: 80.0})
    assert not small.passes
    assert any("成交" in reason for reason in small.reasons)

    weak = judge(_stat(4.0, out_dd=400.0), base, {3.0: 100.0, 5.0: 80.0})
    assert not weak.passes
    assert any("Calmar" in reason for reason in weak.reasons)


def test_judge_rejects_neighbor_flip() -> None:
    base = _stat(3.0, out_net=10.0)
    verdict = judge(_stat(4.0), base, {3.0: 100.0, 5.0: -20.0})
    assert not verdict.passes
    assert any("方向相反" in reason for reason in verdict.reasons)


def test_judge_rejects_worse_total_calmar() -> None:
    base = _stat(3.0, out_net=10.0, net=200.0, dd=100.0)
    candidate = _stat(4.0, in_dd=400.0, net=150.0, dd=120.0)
    verdict = judge(candidate, base, {3.0: 100.0, 5.0: 80.0})
    assert not verdict.passes
    assert any("全段 Calmar" in reason for reason in verdict.reasons)


def test_select_keeps_baseline_when_nothing_passes() -> None:
    stats = {
        3.0: _stat(3.0, out_net=10.0),
        4.0: _stat(4.0, trades=10),
    }
    recommended, verdicts = select_threshold(stats, 3.0)
    assert recommended == 3.0
    assert not verdicts[4.0].passes


def test_select_picks_best_passing_candidate() -> None:
    stats = {
        3.0: _stat(3.0, out_net=10.0, in_dd=300.0),
        4.0: _stat(4.0, out_net=30.0, out_dd=40.0, in_dd=300.0),
        5.0: _stat(5.0, out_net=40.0, out_dd=20.0, in_dd=300.0),
    }
    recommended, _ = select_threshold(stats, 3.0)
    assert recommended == 5.0
