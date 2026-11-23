"""市场状态识别：KMeans 能给出状态画像、转移矩阵和引擎清单，且同种子可复现。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.regimes import fit_regimes, format_regime_report, save_regimes


def _bars(n: int = 700, seed: int = 11) -> pd.DataFrame:
    """三段不同波动/趋势的人造行情，方便聚类切出状态。"""
    rng = np.random.default_rng(seed)
    drift = np.zeros(n)
    vol = np.full(n, 0.0015)
    step = n // 3
    drift[:step] = 0.0012
    drift[step : 2 * step] = -0.0008
    vol[step : 2 * step] = 0.0035
    drift[2 * step :] = 0.0
    vol[2 * step :] = 0.0010
    ret = drift + rng.normal(0, 1, n) * vol
    close = 1000.0 * np.cumprod(1.0 + ret)
    open_ = close * (1.0 + rng.normal(0, 0.0004, n))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.0012, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.0012, n)))
    volume = rng.integers(500, 5000, n).astype(float)
    oi = 10000.0 + np.cumsum(rng.integers(-80, 100, n))
    index = pd.date_range("2023-01-02 09:00", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "oi": oi,
        },
        index=index,
    )


def test_fit_regimes_profiles_and_transition() -> None:
    df = _bars()
    result = fit_regimes(df, seed=42)
    assert 2 <= result.k <= 4
    assert len(result.profiles) == result.k
    assert abs(sum(row["share"] for row in result.profiles) - 1.0) < 1e-6
    assert len(result.transition) == result.k
    for row in result.transition:
        assert abs(sum(row) - 1.0) < 1e-6
    assert len(result.persistence) == result.k
    assert all(result.names.values())
    assert len(result.engines) == result.k


def test_fit_regimes_is_reproducible() -> None:
    df = _bars()
    first = fit_regimes(df, seed=7)
    second = fit_regimes(df, seed=7)
    assert first.names == second.names
    assert first.labels.tolist() == second.labels.tolist()
    assert first.silhouette == second.silhouette


def test_regime_report_and_save(tmp_path) -> None:
    df = _bars()
    result = fit_regimes(df, k=2, seed=3)
    text = format_regime_report(result)
    assert "市场状态" in text
    assert "状态 × 可去交叉测试的策略" in text
    assert "不是切换指令" in text
    assert "不会自动改实盘策略" in text
    path = save_regimes(tmp_path, result)
    assert path.exists()
    assert "transition" in path.read_text(encoding="utf-8")
