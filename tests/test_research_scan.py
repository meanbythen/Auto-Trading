"""规律扫描：两段显著才进池；勾选不下单。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.research.factors import FACTORS, PAIRS, compute_factor_frame
from src.research.pool import load_pool, mark_selected, save_pool
from src.research.scan import Regularity, scan_one_symbol, scan_regularities
from src.research.styles import (
    STYLE_REVERSION,
    STYLE_TREND,
    advise_styles,
)


def _bars_momentum(n: int = 1200, seed: int = 1) -> pd.DataFrame:
    """人造动量行情：这一根涨，后面几根更容易继续涨。"""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, 0.0012, n)
    ret = np.zeros(n)
    for i in range(1, n):
        ret[i] = 0.45 * ret[i - 1] + eps[i]
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.0008
    low = np.minimum(open_, close) * 0.9992
    volume = 1000.0 + 8000.0 * (ret > 0).astype(float)
    idx = pd.date_range("2024-01-02 09:00", periods=n, freq="5min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _bars_reversion(n: int = 1200, seed: int = 2) -> pd.DataFrame:
    """人造回归行情：这一根涨了，后面更容易往回走。"""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, 0.0012, n)
    ret = np.zeros(n)
    for i in range(1, n):
        ret[i] = -0.45 * ret[i - 1] + eps[i]
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.0008
    low = np.minimum(open_, close) * 0.9992
    volume = np.full(n, 1000.0)
    idx = pd.date_range("2024-01-02 09:00", periods=n, freq="5min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _bars_noise(n: int = 1200, seed: int = 9) -> pd.DataFrame:
    """几乎无结构的随机走，用来确认不会乱进池。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0, 0.002, n)
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.0005
    low = np.minimum(open_, close) * 0.9995
    idx = pd.date_range("2024-01-02 09:00", periods=n, freq="5min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": np.full(n, 1000.0)},
        index=idx,
    )


def test_factors_only_use_catalog() -> None:
    """扫描目录是封闭的，不能临时发明指标。"""
    assert len(FACTORS) >= 8
    assert len(PAIRS) >= 5
    ids = {item.id for item in FACTORS}
    for pair in PAIRS:
        assert pair.left in ids
        assert pair.right in ids
        assert pair.logic


def test_factor_frame_no_lookahead() -> None:
    """改最后一根，不应当改更早的因子值。"""
    df = _bars_momentum(400)
    a = compute_factor_frame(df)
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("close")] = float(df2["close"].iloc[-1]) * 1.2
    b = compute_factor_frame(df2)
    for col in a.columns:
        earlier = a[col].iloc[:-5]
        other = b[col].iloc[:-5]
        mask = earlier.notna() & other.notna()
        if mask.any():
            assert np.allclose(earlier[mask].astype(float), other[mask].astype(float), rtol=1e-9, atol=1e-9)


def test_momentum_series_finds_regularity() -> None:
    """动量人造数据上，短涨跌应能过关。"""
    df = _bars_momentum()
    hits = scan_one_symbol(df, "fu2701")
    assert hits
    assert any(key.startswith("ret_1") or key.startswith("ret_5") for key in hits)


def test_noise_rarely_passes_both_halves() -> None:
    """无结构随机走不应大批过关。"""
    hits = scan_one_symbol(_bars_noise(), "rb2701")
    assert len(hits) <= 3


def test_cross_symbol_requires_two() -> None:
    """开了跨品种、给了两个合约时，只在一个上显著的进不了池。"""
    strong = _bars_momentum()
    weak = _bars_noise()
    items = scan_regularities(
        {"fu2701": strong, "rb2701": weak},
        require_cross_symbol=True,
    )
    for item in items:
        assert len(item.symbols) >= 2


def test_single_symbol_still_enters_pool() -> None:
    """只扫一个品种时仍可进池，证据里要标明单品种。"""
    items = scan_regularities({"fu2701": _bars_momentum()}, require_cross_symbol=True)
    assert items
    assert any("仅单品种" in item.evidence for item in items)


def test_pool_keeps_selection_and_disclaimer(tmp_path: Path) -> None:
    """勾选只改标记，文件里写明不是买卖点。"""
    item = Regularity(
        id="ret_5|h6",
        title="近5根涨跌",
        logic="短趋势",
        kind="单因子",
        horizon=6,
        evidence="示例",
        symbols=["fu2701"],
        selected=False,
    )
    path = save_pool(tmp_path, [item])
    text = path.read_text(encoding="utf-8")
    assert "不是买卖点" in text
    assert "扫描层不下单" in text
    mark_selected(tmp_path, {"ret_5|h6"})
    pool = load_pool(tmp_path)
    assert pool["items"][0]["selected"] is True


def test_scan_records_window_consistency() -> None:
    """过关条目要记下分段同向，方便判断是不是大部分时间都稳。"""
    items = scan_regularities({"fu2701": _bars_momentum()}, require_cross_symbol=True)
    assert items
    for item in items:
        assert int(item.details.get("windows_total") or 0) >= 2


def test_momentum_style_tends_trend() -> None:
    """动量人造数据上，倾向应偏向趋势跟踪，并给出本程序已有的趋势策略名。"""
    df = _bars_momentum().copy()
    df["volume"] = 1000.0
    items = scan_regularities({"fu2701": df}, require_cross_symbol=True)
    advice = advise_styles(items)
    assert advice.primary == STYLE_TREND
    assert advice.used >= 1
    names = {eid for eid, _name in advice.engines}
    assert "dual_ma" in names
    assert "supertrend" in names


def test_reversion_style_tends_mean_revert() -> None:
    """回归人造数据上，倾向应偏向震荡回归，并如实说没有专用引擎。"""
    items = scan_regularities({"fu2701": _bars_reversion()}, require_cross_symbol=True)
    advice = advise_styles(items)
    assert items
    assert advice.primary == STYLE_REVERSION
    assert advice.engines == []
    assert "没有单独的震荡回归" in advice.summary


def test_unstable_window_not_used_for_style() -> None:
    """两半显著但分段只有 1/4 同向时，不能拿来投票。"""
    item = Regularity(
        id="ret_5|h6",
        title="近5根涨跌",
        logic="短趋势",
        kind="单因子",
        horizon=6,
        evidence="示例",
        symbols=["fu2701"],
        details={"t_first": 3.0, "t_second": 3.0, "windows_ok": 1, "windows_total": 4},
    )
    advice = advise_styles([item])
    assert advice.used == 0
    assert advice.skipped_unstable == 1
    assert advice.primary == ""


def test_noise_style_has_no_strong_advice() -> None:
    """乱走行情即使偶尔过关，也不该给出强硬类型。"""
    items = scan_regularities({"rb2701": _bars_noise()}, require_cross_symbol=True)
    advice = advise_styles(items)
    if advice.used == 0:
        assert advice.primary == ""
    else:
        assert advice.consistency >= 0.75


def test_research_source_has_no_trader_import() -> None:
    """研究层源码不得去碰下单。"""
    root = Path(__file__).resolve().parents[1] / "src" / "research"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "src.trader" not in text
        assert "from src.trader" not in text
        assert "BotWorker" not in text
