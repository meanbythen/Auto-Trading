# -*- coding: utf-8 -*-
"""研究稳健性分析：bootstrap 置信区间、DSR、PBO/CSCV、参数平原判定。"""

import math
import random

import numpy as np
import pytest

from src.backtest.robustness import (
    analyze_experiment,
    bootstrap_ci,
    deflated_sharpe,
    format_robustness_lines,
    pbo_cscv,
    plateau_analysis,
)


def _pnls(seed: int = 7, n: int = 60) -> list[float]:
    rng = random.Random(seed)
    return [round(rng.gauss(50, 200), 2) for _ in range(n)]


# --------------------------------------------------------------------------- #
# AC-5: bootstrap 确定且分位数正确
# --------------------------------------------------------------------------- #
def test_bootstrap_deterministic_same_seed():
    pnls = _pnls()
    a = bootstrap_ci(pnls, n=2000, confidence=0.95, seed=42)
    b = bootstrap_ci(pnls, n=2000, confidence=0.95, seed=42)
    assert a == b  # 同种子完全可复现
    assert a["skipped"] is False
    assert a["low"] <= a["mean"] <= a["high"]
    assert math.isclose(a["original_mean"], sum(pnls) / len(pnls), rel_tol=1e-12)


def test_bootstrap_quantile_matches_numpy_linear():
    """重抽均值分布的分位数与 numpy 默认线性插值一致。"""
    pnls = _pnls(seed=3, n=50)
    n = 237
    seed = 123
    ci = bootstrap_ci(pnls, n=n, confidence=0.9, seed=seed)

    # 独立复现同一种子的重抽均值序列。
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(pnls, k=len(pnls))) / len(pnls) for _ in range(n))
    expected_low = float(np.quantile(means, 0.05, method="linear"))
    expected_high = float(np.quantile(means, 0.95, method="linear"))
    assert math.isclose(ci["low"], expected_low, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(ci["high"], expected_high, rel_tol=1e-12, abs_tol=1e-12)


def test_bootstrap_different_seed_can_differ():
    pnls = _pnls()
    a = bootstrap_ci(pnls, n=3000, seed=1)
    b = bootstrap_ci(pnls, n=3000, seed=2)
    # 不同种子允许（也期望）结果不同，只要求各自合法。
    assert a["low"] <= a["high"]
    assert b["low"] <= b["high"]


def test_bootstrap_skipped_when_too_few():
    assert bootstrap_ci([1.0])["skipped"] is True
    assert bootstrap_ci([])["skipped"] is True
    box = bootstrap_ci([1.0, 2.0])
    assert box["skipped"] is False  # 2 笔可以算


# --------------------------------------------------------------------------- #
# AC-6: DSR / PBO 有界、可跳过
# --------------------------------------------------------------------------- #
def test_dsr_bounded_and_deterministic():
    pnls = _pnls()
    a = deflated_sharpe(pnls, 50, seed=42)
    b = deflated_sharpe(pnls, 50, seed=42)
    assert a == b
    assert 0.0 <= a["dsr"] <= 1.0
    assert math.isfinite(a["sharpe"])
    assert a["t_obs"] == len(pnls)


def test_dsr_strong_signal_is_significant():
    # 每笔夏普 ~5，T=100，N=10：校正后仍应高度显著（接近 1）。
    rng = random.Random(1)
    pnls = [round(rng.gauss(50, 10), 2) for _ in range(100)]
    d = deflated_sharpe(pnls, 10)
    assert d["skipped"] is False
    assert d["dsr"] > 0.99


def test_dsr_skipped_cases():
    pnls = _pnls()
    assert deflated_sharpe([1.0], 5)["skipped"] is True          # T < 2
    assert deflated_sharpe(pnls, 1)["skipped"] is True           # n_trials < 2
    assert deflated_sharpe([5.0] * 10, 5)["skipped"] is True     # 方差为 0
    # 两点分布 [0,0,0,M] 的 skew=2/√3、kurt=7/3，在 sharpe=√3 处分母根号项恰好为 0。
    two_point = [0.0, 0.0, 0.0, 100.0]
    box = deflated_sharpe(two_point, 5, sharpe=math.sqrt(3))
    assert box["skipped"] is True


def test_pbo_bounded_and_deterministic():
    rng = random.Random(5)
    matrix = [[rng.gauss(0, 0.02) for _ in range(5)] for _ in range(64)]
    a = pbo_cscv(matrix, n_partitions=16, seed=42)
    b = pbo_cscv(matrix, n_partitions=16, seed=42)
    assert a == b
    assert 0.0 <= a["pbo"] <= 1.0
    assert a["n_combinations"] > 0
    assert a["n_strategies"] == 5


def test_pbo_overfit_high_stable_low():
    """过拟合矩阵 PBO 高，稳定优秀矩阵 PBO 低。"""
    rng = random.Random(11)
    N, T = 6, 64
    overfit = []
    for t in range(T):
        row = []
        for c in range(N):
            if c == 0:
                row.append(rng.gauss(0.05 if t < T // 2 else -0.05, 0.02))
            else:
                row.append(rng.gauss(0, 0.02))
        overfit.append(row)
    stable = []
    for t in range(T):
        row = []
        for c in range(N):
            row.append(rng.gauss(0.06 if c == 0 else 0, 0.02))
        stable.append(row)
    assert pbo_cscv(overfit, n_partitions=16, seed=42)["pbo"] > 0.5
    assert pbo_cscv(stable, n_partitions=16, seed=42)["pbo"] < 0.2


def test_pbo_skipped_cases():
    rng = random.Random(5)
    matrix = [[rng.gauss(0, 0.02) for _ in range(5)] for _ in range(64)]
    assert pbo_cscv(matrix[:4], n_partitions=16)["skipped"] is True   # 行 < 分区数
    assert pbo_cscv([[1.0], [2.0], [3.0]], n_partitions=2)["skipped"] is True  # 列 < 2
    assert pbo_cscv([], n_partitions=16)["skipped"] is True


def test_pbo_large_partitions_uses_random_subset():
    """n_partitions 较大时组合数 > 20000，走随机子集分支，仍有界且不抛异常。"""
    rng = random.Random(9)
    matrix = [[rng.gauss(0, 0.02) for _ in range(4)] for _ in range(24)]
    box = pbo_cscv(matrix, n_partitions=24, seed=1)
    assert box["skipped"] is False
    assert 0.0 <= box["pbo"] <= 1.0
    assert 0 < box["n_combinations"] <= 20000


# --------------------------------------------------------------------------- #
# AC-7: 孤峰 vs 稳定平原
# --------------------------------------------------------------------------- #
def test_plateau_isolated_peak_collapses():
    trials = [
        {"params": {"a": 1, "b": 1}, "score": 100.0},
        {"params": {"a": 0, "b": 1}, "score": 10.0},
        {"params": {"a": 2, "b": 1}, "score": 10.0},
        {"params": {"a": 1, "b": 0}, "score": 10.0},
        {"params": {"a": 1, "b": 2}, "score": 10.0},
    ]
    box = plateau_analysis(trials)
    assert box["skipped"] is False
    assert box["collapsed"] is True
    assert box["n_neighbors"] == 4
    assert "孤峰" in box["verdict"] or "塌陷" in box["verdict"]


def test_plateau_stable_neighborhood():
    trials = [
        {"params": {"a": 1, "b": 1}, "score": 100.0},
        {"params": {"a": 0, "b": 1}, "score": 80.0},
        {"params": {"a": 2, "b": 1}, "score": 80.0},
        {"params": {"a": 1, "b": 0}, "score": 80.0},
        {"params": {"a": 1, "b": 2}, "score": 80.0},
    ]
    box = plateau_analysis(trials)
    assert box["skipped"] is False
    assert "稳定" in box["verdict"]
    assert box["collapsed"] is False


def test_plateau_in_between_is_general():
    # 邻域中位 60 < 70（不满足稳定平原），邻域最高 60 >= 50（不塌陷）-> 邻域一般
    trials = [
        {"params": {"a": 1, "b": 1}, "score": 100.0},
        {"params": {"a": 0, "b": 1}, "score": 60.0},
        {"params": {"a": 2, "b": 1}, "score": 60.0},
    ]
    box = plateau_analysis(trials)
    assert box["verdict"] == "邻域一般"


def test_plateau_no_neighbors():
    trials = [
        {"params": {"a": 1, "b": 1}, "score": 100.0},
        {"params": {"a": 9, "b": 9}, "score": 10.0},  # hamming 距离 2，不是邻域
    ]
    box = plateau_analysis(trials)
    assert box["verdict"] == "无邻域数据"
    assert box["n_neighbors"] == 0


def test_plateau_best_idx_override_and_skip():
    trials = [
        {"params": {"a": 1}, "score": 10.0},
        {"params": {"a": 2}, "score": 100.0},
    ]
    box = plateau_analysis(trials, best_idx=0)
    assert box["best_idx"] == 0
    assert box["best_score"] == 10.0
    assert plateau_analysis(trials[:1])["skipped"] is True  # < 2 个有效 trial


# --------------------------------------------------------------------------- #
# analyze_experiment + format 文本分区
# --------------------------------------------------------------------------- #
def test_analyze_experiment_runs_all_sections():
    pnls = _pnls()
    summary = analyze_experiment(
        {
            "trades": [{"pnl": x} for x in pnls],
            "trials": [{"params": {"i": i}, "score": float(i)} for i in range(50)],
        }
    )
    assert set(summary) == {"bootstrap", "dsr", "pbo", "plateau"}
    assert summary["pbo"]["skipped"] is True
    assert "缺少每期收益矩阵 trials_returns" in summary["pbo"]["skip_reason"]
    lines = format_robustness_lines(summary)
    assert any("稳健性" in ln for ln in lines)


def test_format_all_skipped_shows_single_note():
    summary = analyze_experiment({})  # 没有 trades/trials/trials_returns
    lines = format_robustness_lines(summary)
    assert lines[0] == "—— 稳健性 ——"
    assert any("样本不足或缺少数据，未计算" in ln for ln in lines)


def test_format_with_pbo_matrix_renders():
    rng = random.Random(5)
    matrix = [[rng.gauss(0.02 if c == 0 else 0, 0.02) for c in range(4)] for _ in range(48)]
    summary = analyze_experiment(
        {
            "trades": [{"pnl": x} for x in _pnls()],
            "trials": [{"params": {"i": i}, "score": float(i)} for i in range(20)],
            "trials_returns": matrix,
        }
    )
    assert summary["pbo"]["skipped"] is False
    lines = format_robustness_lines(summary)
    joined = "\n".join(lines)
    assert "PBO" in joined
    assert "DSR" in joined or "Deflated Sharpe" in joined


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
