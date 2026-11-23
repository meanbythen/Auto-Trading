"""因子挖掘：两段显著才进池；勾选不下单；旧候选池只读迁移。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.research.factors import FACTORS, PAIRS, compute_factor_frame
from src.research.pool import load_pool, mark_selected, save_pool
from src.research.scan import Regularity, forward_return, scan_one_symbol, scan_regularities
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


def test_oi_factors_need_oi_and_never_fill_zero() -> None:
    """没有持仓量时 OI 组留空，不填 0；有持仓量时才出数。"""
    df = _bars_momentum(400)
    no_oi = compute_factor_frame(df)
    oi_specs = [spec for spec in FACTORS if spec.group == "oi"]
    assert oi_specs
    for spec in oi_specs:
        assert no_oi[spec.id].isna().all()

    with_oi = df.copy()
    with_oi["oi"] = np.linspace(1000.0, 1500.0, len(with_oi))
    frame = compute_factor_frame(with_oi)
    for spec in oi_specs:
        assert frame[spec.id].notna().any()


def test_canonicalize_accepts_oi_aliases() -> None:
    """各家的持仓量列名统一收成 oi；已有 oi 时不被别名覆盖。"""
    from src.data.periods import canonicalize_ohlc

    base = {
        "open": [1.0, 1.0, 1.0],
        "high": [1.0, 1.0, 1.0],
        "low": [1.0, 1.0, 1.0],
        "close": [1.0, 1.0, 1.0],
    }
    idx = pd.date_range("2024-01-02 09:00", periods=3, freq="5min")
    for alias in ("hold", "position", "open_interest", "open_oi", "close_oi"):
        frame = canonicalize_ohlc(pd.DataFrame({**base, alias: [10.0, 11.0, 12.0]}, index=idx))
        assert "oi" in frame.columns
        assert frame["oi"].notna().all()

    both = canonicalize_ohlc(
        pd.DataFrame({**base, "oi": [5.0, 5.0, 5.0], "hold": [9.0, 9.0, 9.0]}, index=idx)
    )
    assert both["oi"].tolist() == [5.0, 5.0, 5.0]


def test_continuous_contract_turns_oi_light_yellow(tmp_path: Path) -> None:
    """主力连续上不跑 OI 因子，持仓量灯要提示换成具体合约。"""
    from src.data.bar_store import BarStore
    from src.research.data_requirements import evaluate_readiness

    store = BarStore(tmp_path / "market.sqlite")
    report = evaluate_readiness(store, ["FG0"], ["D"], mode="quick", require_oi=True)
    assert report["lights"]["oi"]["status"] == "yellow"
    assert "具体合约" in report["lights"]["oi"]["detail"]


def test_stats_permutation_mi_and_fdr_behave() -> None:
    """置换能认出真关系；互信息能抓非线性；BH 按秩放大 p。"""
    from src.research.stats import (
        bh_fdr,
        layered_stats,
        mutual_info_test,
        permutation_ic_p,
        rank_ic_series,
    )

    rng = np.random.default_rng(7)
    n = 600
    factor = pd.Series(rng.normal(size=n))
    fwd = 0.6 * factor + pd.Series(rng.normal(size=n))
    strong = permutation_ic_p(factor, fwd, 1, n_perm=300)
    assert strong["ic"] > 0.3
    assert strong["p"] < 0.05

    noise = pd.Series(rng.normal(size=n))
    weak = permutation_ic_p(noise, fwd, 1, n_perm=300)
    assert weak["p"] > 0.05

    # y = x^2：线性相关接近 0，但互信息显著。
    nonlin = pd.Series(np.linspace(-1, 1, n))
    target = pd.Series(nonlin**2 + rng.normal(scale=0.05, size=n))
    mi = mutual_info_test(nonlin, target, 1, n_perm=100)
    assert mi["mi"] > 0.2
    assert mi["mi_p"] < 0.1

    layers = layered_stats(factor, fwd, bins=5)
    assert layers["monotonic"] > 0.8
    assert layers["top_bottom"] > 0

    series = rank_ic_series(factor, fwd, window=100)
    assert series and all(abs(v) <= 1.0 for v in series)

    adjusted = bh_fdr([0.001, 0.01, 0.04, 0.2])
    assert adjusted[0] <= 0.01
    assert adjusted[1] <= 0.05
    assert adjusted[3] == 0.2


def test_scan_reports_fdr_counts() -> None:
    """跑一轮后能说清门2过关多少、FDR 后留下多少。"""
    df = _bars_momentum(1200)
    diagnostics: dict = {}
    items = scan_regularities(
        {"FG2605": df}, require_cross_symbol=False, diagnostics=diagnostics
    )
    assert items
    assert diagnostics["gate2"] >= diagnostics["fdr_pass"] >= 1
    assert diagnostics["alpha"] == 0.05
    assert all("adj_p" in item.details for item in items)


def test_factor_wfa_and_lock_set() -> None:
    """滚动前进能看出方向延续；Lock 集只评估最后 20%。"""
    from src.research.stats import factor_wfa, lock_ic

    df = _bars_momentum(1200)
    factor = compute_factor_frame(df)["ret_1"]
    fwd = forward_return(df["close"], 3)
    wfa = factor_wfa(factor, fwd)
    assert wfa["wfa_consistency"] >= 0.6
    assert wfa["wfa_eff"] > 0.3
    lock = lock_ic(factor, fwd)
    assert lock["lock_ic"] > 0
    assert lock["lock_n"] > 0


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
    assert path.name == "因子挖掘候选池.json"
    assert "不是买卖点" in text
    assert "挖掘层不下单" in text
    mark_selected(tmp_path, {"ret_5|h6"})
    pool = load_pool(tmp_path)
    assert pool["items"][0]["selected"] is True


def test_legacy_pool_is_read_only_migrated(tmp_path: Path) -> None:
    """旧池勾选能读出来；第一次保存写新文件，旧文件不动。"""
    legacy = tmp_path / "data" / "规律候选池.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        '{"items": [{"id": "ret_5|h6", "title": "近5根涨跌", "selected": true}]}',
        encoding="utf-8",
    )

    pool = load_pool(tmp_path)
    assert pool["schema_version"] == 2
    assert pool["module"] == "factor_mining"
    assert pool["items"][0]["id"] == "ret_5|h6"
    assert pool["items"][0]["selected"] is True
    assert pool["items"][0]["verdict"] == "candidate"

    mark_selected(tmp_path, {"ret_5|h6"})
    new_file = tmp_path / "data" / "因子挖掘候选池.json"
    assert new_file.exists()
    assert legacy.exists()
    assert "规律候选池" not in new_file.read_text(encoding="utf-8")


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
