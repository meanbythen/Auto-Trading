"""因子挖掘的统计检验：Rank IC 序列、互信息、分块置换、分层单调性、BH-FDR、Hurst。

只用 numpy / pandas，保证同种子可复现。
这些函数只回答「历史统计上站不站得住」，不生成买卖点。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.backtest.robustness import bootstrap_ci

# Rank IC 滚动窗。默认不重叠（步长=窗口），避免重叠窗口把 t 值撑大。
IC_WINDOW = 60
# 互信息分箱数。固定分箱规则，报告里写明。
MI_BINS = 10
# 置换次数。分块置换而不是逐点打乱，见 change log。
PERMUTATIONS = 1000
MI_PERMUTATIONS = 200


def _aligned(x: pd.Series, y: pd.Series) -> tuple[pd.Series, pd.Series] | None:
    pair = pd.concat({"x": x, "y": y}, axis=1).dropna()
    if len(pair) < 3:
        return None
    return pair["x"], pair["y"]


def rank_ic_series(
    factor: pd.Series,
    fwd: pd.Series,
    *,
    window: int = IC_WINDOW,
    step: int | None = None,
) -> list[float]:
    """把样本切成不重叠的窗，每窗算一次 Rank IC。"""
    pair = _aligned(factor, fwd)
    if pair is None:
        return []
    x, y = pair
    size = int(window)
    stride = int(step or window)
    if size < 10 or len(x) < size:
        return []
    out: list[float] = []
    for start in range(0, len(x) - size + 1, stride):
        seg_x = x.iloc[start : start + size]
        seg_y = y.iloc[start : start + size]
        try:
            value = seg_x.corr(seg_y, method="spearman")
        except Exception:  # noqa: BLE001
            value = None
        out.append(0.0 if value is None or pd.isna(value) else float(value))
    return out


def ic_summary(series: Iterable[float], *, seed: int = 42) -> dict[str, Any]:
    """IC 均值、标准差、ICIR、IC>0 占比、t 值和 bootstrap 置信区间。"""
    values = [float(v) for v in series if v is not None and math.isfinite(float(v))]
    if not values:
        return {}
    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    windows = int(len(arr))
    icir = mean / std if std > 1e-12 else 0.0
    t_val = mean / (std / math.sqrt(windows)) if std > 1e-12 and windows > 1 else 0.0
    ci = bootstrap_ci(values, seed=seed)
    return {
        "ic_mean": mean,
        "ic_std": std,
        "icir": float(icir),
        "ic_pos": float(np.mean(arr > 0)),
        "ic_t": float(t_val),
        "ic_windows": windows,
        "ic_ci_low": float(ci.get("low", 0.0)),
        "ic_ci_high": float(ci.get("high", 0.0)),
    }


def block_length(horizon: int, n: int) -> int:
    """置换块长：至少覆盖一个标签周期，再取 n^(1/3)。"""
    return max(int(horizon), int(math.ceil(max(n, 1) ** (1.0 / 3.0))), 2)


def permutation_index(
    n: int,
    *,
    n_perm: int = PERMUTATIONS,
    block_len: int,
    seed: int = 0,
) -> np.ndarray:
    """环形分块随机起点，返回 (n_perm, n) 的索引矩阵。"""
    if n <= 1:
        return np.zeros((n_perm, max(n, 1)), dtype=np.int64)
    blocks = int(math.ceil(n / block_len))
    rng = np.random.default_rng(int(seed))
    starts = rng.integers(0, n, size=(int(n_perm), blocks))
    offsets = np.arange(int(block_len))
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    return idx.reshape(int(n_perm), -1)[:, :n]


def _perm_matrix(
    n: int,
    horizon: int,
    *,
    n_perm: int,
    cache: dict[tuple[int, int, int], np.ndarray] | None,
) -> np.ndarray:
    length = block_length(horizon, n)
    key = (n, length, n_perm)
    if cache is not None and key in cache:
        return cache[key]
    matrix = permutation_index(n, n_perm=n_perm, block_len=length)
    if cache is not None:
        cache[key] = matrix
    return matrix


def permutation_ic_p(
    factor: pd.Series,
    fwd: pd.Series,
    horizon: int,
    *,
    n_perm: int = PERMUTATIONS,
    cache: dict[tuple[int, int, int], np.ndarray] | None = None,
    seed: int = 0,
) -> dict[str, float]:
    """全样本 |Rank IC| 的分块置换 p 值。原假设：因子和未来收益没关系。"""
    pair = _aligned(factor, fwd)
    if pair is None:
        return {"ic": 0.0, "p": 1.0, "n": 0}
    x, y = pair
    if int(x.nunique()) < 2 or int(y.nunique()) < 2:
        return {"ic": 0.0, "p": 1.0, "n": int(len(x))}
    xr = x.rank(method="average").to_numpy(dtype=float)
    yr = y.rank(method="average").to_numpy(dtype=float)
    xc = xr - xr.mean()
    yc = yr - yr.mean()
    x_norm = float(np.sqrt(np.dot(xc, xc)))
    y_norm = float(np.sqrt(np.dot(yc, yc)))
    if x_norm < 1e-12 or y_norm < 1e-12:
        return {"ic": 0.0, "p": 1.0, "n": int(len(x))}
    observed = float(np.dot(xc, yc) / (x_norm * y_norm))
    idx = _perm_matrix(len(x), horizon, n_perm=n_perm, cache=cache)
    # 只打乱未来收益，保留因子顺序；块内局部时序结构不被破坏。
    permuted = yr[idx]
    permuted = permuted - permuted.mean(axis=1, keepdims=True)
    norms = np.sqrt((permuted**2).sum(axis=1, keepdims=True))
    null = (permuted @ xc) / (norms[:, 0] * x_norm)
    hits = int(np.sum(np.abs(null) >= abs(observed) - 1e-12))
    return {"ic": observed, "p": (1.0 + hits) / (1.0 + int(n_perm)), "n": int(len(x))}


def _bins(values: np.ndarray, bins: int) -> np.ndarray:
    """按分位切箱；重复值不会把箱子撑爆。"""
    try:
        edges = np.unique(np.quantile(values, np.linspace(0.0, 1.0, bins + 1)))
    except Exception:  # noqa: BLE001
        edges = np.array([values.min(), values.max() + 1e-12])
    if len(edges) < 2:
        return np.zeros(len(values), dtype=np.int64)
    clipped = np.clip(values, edges[0], edges[-1])
    code = np.searchsorted(edges[1:-1], clipped, side="right")
    return code.astype(np.int64)


def mutual_information(x: np.ndarray, y: np.ndarray, *, bins: int = MI_BINS) -> float:
    """直方图互信息（nats）。固定分位分箱，不引入 sklearn。"""
    if len(x) < 10:
        return 0.0
    xb = _bins(x, bins)
    yb = _bins(y, bins)
    table = np.zeros((int(xb.max()) + 1, int(yb.max()) + 1), dtype=float)
    np.add.at(table, (xb, yb), 1.0)
    total = table.sum()
    if total <= 0:
        return 0.0
    pxy = table / total
    px = pxy.sum(axis=1, keepdims=True)
    py = pxy.sum(axis=0, keepdims=True)
    nz = pxy > 0
    return float(np.sum(pxy[nz] * np.log(pxy[nz] / (px @ py)[nz])))


def mutual_info_test(
    factor: pd.Series,
    fwd: pd.Series,
    horizon: int,
    *,
    bins: int = MI_BINS,
    n_perm: int = MI_PERMUTATIONS,
    cache: dict[tuple[int, int, int], np.ndarray] | None = None,
) -> dict[str, float]:
    """互信息及其置换 p 值。MI 高但 IC 低时标「状态型」。"""
    pair = _aligned(factor, fwd)
    if pair is None:
        return {}
    x, y = pair
    xv = x.to_numpy(dtype=float)
    yv = y.to_numpy(dtype=float)
    observed = mutual_information(xv, yv, bins=bins)
    idx = _perm_matrix(len(xv), horizon, n_perm=n_perm, cache=cache)
    null = np.empty(int(n_perm), dtype=float)
    for i in range(int(n_perm)):
        null[i] = mutual_information(xv, yv[idx[i]], bins=bins)
    mean = float(null.mean())
    std = float(null.std(ddof=1)) if len(null) > 1 else 0.0
    z = (observed - mean) / std if std > 1e-12 else 0.0
    hits = int(np.sum(null >= observed - 1e-12))
    return {
        "mi": observed,
        "mi_p": (1.0 + hits) / (1.0 + int(n_perm)),
        "mi_z": float(z),
        "mi_bins": int(bins),
    }


def layered_stats(factor: pd.Series, fwd: pd.Series, *, bins: int = 5) -> dict[str, Any]:
    """按因子分成若干层，看每层未来收益；单调性用层序号与层均值的 Spearman。"""
    pair = _aligned(factor, fwd)
    if pair is None or len(pair[0]) < bins * 10:
        return {}
    x, y = pair
    codes = pd.qcut(x.rank(method="first"), int(bins), labels=False, duplicates="drop")
    frame = pd.DataFrame({"g": codes, "y": y}).dropna()
    if frame["g"].nunique() < 2:
        return {}
    rows: list[dict[str, Any]] = []
    for group, part in frame.groupby("g", sort=True):
        rows.append(
            {
                "layer": int(group) + 1,
                "n": int(len(part)),
                "mean": float(part["y"].mean()),
                "win": float((part["y"] > 0).mean()),
            }
        )
    means = [row["mean"] for row in rows]
    order = list(range(len(rows)))
    try:
        mono = pd.Series(order).corr(pd.Series(means), method="spearman")
    except Exception:  # noqa: BLE001
        mono = None
    return {
        "layers": rows,
        "monotonic": 0.0 if mono is None or pd.isna(mono) else float(mono),
        "top_bottom": float(means[-1] - means[0]) if len(means) >= 2 else 0.0,
    }


def bh_fdr(pvalues: Iterable[float], *, alpha: float = 0.05) -> list[float]:
    """Benjamini-Hochberg 校正。返回和输入等长的 adjusted p。"""
    values = np.asarray([float(v) for v in pvalues], dtype=float)
    if values.size == 0:
        return []
    adjusted = np.full(values.shape, np.nan)
    ok = np.isfinite(values)
    if not ok.any():
        return adjusted.tolist()
    picked = values[ok]
    order = np.argsort(picked)
    ranked = picked[order]
    m = len(ranked)
    scaled = ranked * m / np.arange(1, m + 1)
    scaled = np.minimum.accumulate(scaled[::-1])[::-1]
    scaled = np.clip(scaled, 0.0, 1.0)
    back = np.empty(m, dtype=float)
    back[order] = scaled
    adjusted[ok] = back
    return adjusted.tolist()


def fdr_pass(pvalue: float, alpha: float = 0.05) -> bool:
    return bool(math.isfinite(float(pvalue)) and float(pvalue) <= alpha)


def factor_wfa(
    factor: pd.Series,
    fwd: pd.Series,
    *,
    train: int = 400,
    test: int = 100,
    expanding: bool = True,
    max_folds: int = 8,
) -> dict[str, Any]:
    """
    因子级滚动前进检验：每折只用训练段定方向，再看紧跟其后的检验段是否同向。

    与策略 WFA 分开：这里不重估参数，只看 IC 的方向和强度能否延续。
    重叠标签会让折与折之间不完全独立，因此结果只作参考，不当成显著性证明。
    """
    pair = _aligned(factor, fwd)
    if pair is None:
        return {}
    x, y = pair
    size = int(train)
    step = int(test)
    if size < 100 or step < 20 or len(x) < size + step:
        return {}
    folds: list[dict[str, float]] = []
    start = size
    while start + step <= len(x) and len(folds) < int(max_folds):
        if expanding:
            tr_x, tr_y = x.iloc[:start], y.iloc[:start]
        else:
            tr_x, tr_y = x.iloc[start - size : start], y.iloc[start - size : start]
        te_x, te_y = x.iloc[start : start + step], y.iloc[start : start + step]
        try:
            ic_train = tr_x.corr(tr_y, method="spearman")
            ic_test = te_x.corr(te_y, method="spearman")
        except Exception:  # noqa: BLE001
            break
        if ic_train is None or ic_test is None or pd.isna(ic_train) or pd.isna(ic_test):
            break
        folds.append(
            {
                "start": int(start),
                "train_ic": float(ic_train),
                "test_ic": float(ic_test),
            }
        )
        start += step
    if not folds:
        return {}
    train_ics = [row["train_ic"] for row in folds]
    test_ics = [row["test_ic"] for row in folds]
    strengths = [abs(v) for v in train_ics if abs(v) > 1e-9]
    aligned = [
        (1.0 if row["train_ic"] >= 0 else -1.0) * row["test_ic"] for row in folds
    ]
    efficiency = float(np.mean(aligned) / np.mean(strengths)) if strengths else 0.0
    consistency = float(
        np.mean([row["train_ic"] * row["test_ic"] > 0 for row in folds])
    )
    return {
        "folds": folds,
        "wfa_eff": efficiency,
        "wfa_consistency": consistency,
        "wfa_test_ic": float(np.mean(test_ics)),
    }


def lock_ic(
    factor: pd.Series,
    fwd: pd.Series,
    *,
    ratio: float = 0.20,
    min_rows: int = 40,
) -> dict[str, float]:
    """最后一段 Lock 集：只评估、不参与挑因子。返回 IC、样本数和方向是否一致。"""
    pair = _aligned(factor, fwd)
    if pair is None:
        return {}
    x, y = pair
    keep = max(int(len(x) * (1.0 - float(ratio))), 1)
    lock_x, lock_y = x.iloc[keep:], y.iloc[keep:]
    if len(lock_x) < int(min_rows):
        return {}
    try:
        value = lock_x.corr(lock_y, method="spearman")
    except Exception:  # noqa: BLE001
        return {}
    if value is None or pd.isna(value):
        return {}
    return {"lock_ic": float(value), "lock_n": int(len(lock_x))}


def hurst_rs(returns: pd.Series, *, window: int = 100) -> pd.Series:
    """滚动 R/S Hurst：>0.5 偏趋势，<0.5 偏均值回归。"""
    values = returns.astype(float)

    def _one(seg: np.ndarray) -> float:
        seg = seg[np.isfinite(seg)]
        if len(seg) < 20:
            return float("nan")
        dev = seg - seg.mean()
        cumulative = np.cumsum(dev)
        spread = float(cumulative.max() - cumulative.min())
        std = float(seg.std(ddof=1))
        if std < 1e-12 or spread < 1e-12:
            return float("nan")
        return math.log(spread / std) / math.log(len(seg))

    return values.rolling(window, min_periods=20).apply(_one, raw=True)
