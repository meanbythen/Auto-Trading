"""市场状态识别：KMeans 能给出状态画像、转移矩阵和引擎清单，且同种子可复现。"""

from __future__ import annotations

import builtins
import sys

import numpy as np
import pandas as pd
import pytest

from src.research.regimes import (
    _rule_mood_series,
    _state_mood,
    fit_regimes,
    format_regime_report,
    save_regimes,
)


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


def test_fit_regimes_reports_missing_sklearn(monkeypatch) -> None:
    """sklearn 没装时，给出带安装命令的友好提示，而不是裸 ImportError。"""
    real_import = builtins.__import__

    def _block(name: str, *args: object, **kwargs: object) -> object:
        if name == "sklearn" or name.startswith("sklearn."):
            raise ImportError("No module named 'sklearn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block)
    for mod in list(sys.modules):
        if mod == "sklearn" or mod.startswith("sklearn."):
            monkeypatch.delitem(sys.modules, mod)

    with pytest.raises(RuntimeError, match="requirements-optional"):
        fit_regimes(_bars(), seed=1)


def test_rule_cross_structure_and_report(tmp_path) -> None:
    """规则状态 × 聚类状态：每个状态都有对照行，占比和为 1，报告与 JSON 都带上。"""
    df = _bars()
    result = fit_regimes(df, seed=42)
    assert len(result.rule_cross) == result.k
    for item in result.rule_cross:
        assert item["mood"] in {"trend", "range", "mixed"}
        breakdown = item["rule_breakdown"]
        assert set(breakdown) == {"趋势", "偏弱", "震荡"}
        assert abs(sum(breakdown.values()) - 1.0) < 1e-6
        assert item["rule_dominant"] in breakdown
    assert result.rule_latest["rule_mood"] in {"趋势", "偏弱", "震荡"}
    assert result.rule_latest["cluster_mood"] in {"trend", "range", "mixed"}

    text = format_regime_report(result)
    assert "规则状态 × 聚类状态" in text
    assert "最新一根" in text
    path = save_regimes(tmp_path, result)
    assert "rule_cross" in path.read_text(encoding="utf-8")


def test_rule_mood_series_thresholds() -> None:
    """规则情绪标签口径：ADX≥25 趋势、≥20 偏弱、否则震荡。"""
    frame = pd.DataFrame({"adx_14": [10.0, 20.0, 22.0, 25.0, 40.0]})
    mood = _rule_mood_series(frame)
    assert mood.tolist() == ["震荡", "偏弱", "偏弱", "趋势", "趋势"]


def test_state_mood_classification() -> None:
    assert _state_mood("延续态") == "trend"
    assert _state_mood("趋势-高波") == "trend"
    assert _state_mood("趋势-高波#2") == "trend"
    assert _state_mood("回归态") == "range"
    assert _state_mood("震荡-低波") == "range"
    assert _state_mood("高波-无序") == "mixed"
