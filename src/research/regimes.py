"""市场状态识别（研究层）。

用 KMeans 把「趋势强度、波动、Hurst、自相关、量仓」聚成 2~4 个状态，
输出状态画像、转移矩阵、状态 × 因子、状态 × 可去交叉测试的引擎。

原则：
- 标准化只用样本内（前 60%~80%）拟合，样本外按最近中心归类，不重拟合；
- 状态标签只用于研究，不写进实盘策略，也不自动切换策略；
- 簇太小、分离度太弱、跨品种不一致时只描述，不给策略建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.research.factors import FACTOR_BY_ID, compute_factor_frame
from src.research.scan import forward_return
from src.research.styles import (
    ENGINES_BY_STYLE,
    STYLE_LABELS,
    STYLE_REVERSION,
    STYLE_TREND,
)
from src.strategy.indicators import average_true_range, ema_series
from src.strategy.market_state import adx_series

# 默认 8 个状态特征。ADX 和 EMA 斜率都刻画趋势，保留是为了互相校验。
FEATURE_LABELS = {
    "adx_14": "ADX14",
    "atr_pct": "ATR/收盘",
    "atr_pctile_60": "ATR分位",
    "realized_vol_20": "20根波动",
    "hurst_100": "Hurst100",
    "autocorr_20": "20根自相关",
    "volume_z_20": "成交量z分",
    "oi_chg_z_20": "持仓变化z分",
    "ema20_slope_atr": "EMA斜率/ATR",
}
DEFAULT_FEATURES = (
    "adx_14",
    "atr_pctile_60",
    "realized_vol_20",
    "hurst_100",
    "autocorr_20",
    "volume_z_20",
    "ema20_slope_atr",
)
OI_FEATURE = "oi_chg_z_20"
MIN_STATE_SAMPLES = 80
MIN_SHARE = 0.10


@dataclass
class RegimeResult:
    """一次状态识别的完整结果，够写报告和存 JSON。"""

    k: int
    seed: int
    train_ratio: float
    silhouette: float
    features: list[str]
    names: dict[int, str]
    labels: pd.Series
    profiles: list[dict[str, Any]] = field(default_factory=list)
    transition: list[list[float]] = field(default_factory=list)
    persistence: list[float] = field(default_factory=list)
    engines: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": int(self.k),
            "seed": int(self.seed),
            "train_ratio": float(self.train_ratio),
            "silhouette": float(self.silhouette),
            "features": list(self.features),
            "names": {str(key): value for key, value in self.names.items()},
            "profiles": self.profiles,
            "transition": self.transition,
            "persistence": self.persistence,
            "engines": self.engines,
            "warnings": self.warnings,
        }


def _hurst_sampled(returns: pd.Series, *, window: int = 100, stride: int = 10) -> pd.Series:
    """Hurst 逐根算太慢，每 stride 根算一次再前向填充。"""
    values = returns.astype(float).to_numpy()
    full = pd.Series(np.nan, index=returns.index, dtype=float)
    import math

    for end in range(window, len(values) + 1, stride):
        seg = values[end - window : end]
        seg = seg[np.isfinite(seg)]
        if len(seg) < 20:
            continue
        dev = seg - seg.mean()
        cumulative = np.cumsum(dev)
        spread = float(cumulative.max() - cumulative.min())
        std = float(seg.std(ddof=1))
        if std < 1e-12 or spread < 1e-12:
            continue
        full.iloc[end - 1] = math.log(spread / std) / math.log(len(seg))
    return full.ffill()


def build_regime_features(df: pd.DataFrame) -> pd.DataFrame:
    """只用 t 及以前的数据算状态特征。缺 OI 时 OI 特征留空。"""
    work = df.copy()
    work.columns = [str(name).lower() for name in work.columns]
    close = work["close"].astype(float)
    high = work["high"].astype(float)
    low = work["low"].astype(float)
    volume = work["volume"].astype(float) if "volume" in work.columns else pd.Series(0.0, index=work.index)
    out = pd.DataFrame(index=work.index)

    adx, _, _ = adx_series(high, low, close, period=14)
    out["adx_14"] = adx
    atr = average_true_range(high, low, close, period=14)
    out["atr_pct"] = atr / close.replace(0, pd.NA)
    out["atr_pctile_60"] = out["atr_pct"].rolling(60, min_periods=20).rank(pct=True)
    ret = close.pct_change()
    out["realized_vol_20"] = ret.rolling(20, min_periods=20).std()
    out["hurst_100"] = _hurst_sampled(ret, window=100)
    out["autocorr_20"] = close.rolling(20, min_periods=20).corr(close.shift(1))
    mean = volume.rolling(20, min_periods=20).mean()
    std = volume.rolling(20, min_periods=20).std()
    out["volume_z_20"] = (volume - mean) / std.replace(0, pd.NA)
    ema20 = ema_series(close, 20)
    slope = ema20 - ema20.shift(5)
    out["ema20_slope_atr"] = slope / (atr.replace(0, pd.NA) * 5.0)
    if "oi" in work.columns:
        oi = work["oi"].astype(float).replace(0, pd.NA)
        oi_change = oi / oi.shift(1) - 1.0
        oi_mean = oi_change.rolling(20, min_periods=20).mean()
        oi_std = oi_change.rolling(20, min_periods=20).std()
        out[OI_FEATURE] = (oi_change - oi_mean) / oi_std.replace(0, pd.NA)
    else:
        out[OI_FEATURE] = pd.NA
    return out


def _pick_features(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    """样本内相关太高的特征只留一个，避免聚类被同一件事重复带偏。"""
    kept: list[str] = []
    for name in columns:
        series = frame[name]
        if series.notna().sum() < 50 or float(series.std()) < 1e-12:
            continue
        redundant = False
        for other in kept:
            corr = series.corr(frame[other], method="spearman")
            if corr is not None and not pd.isna(corr) and abs(float(corr)) > 0.90:
                redundant = True
                break
        if not redundant:
            kept.append(name)
    return kept


def _name_state(profile: dict[str, float], adx_cut: float, vol_cut: float) -> str:
    """按 ADX、ATR 分位、Hurst、自相关给簇起一个人能看懂的名字。"""
    adx = float(profile.get("adx_14", 0.0) or 0.0)
    vol = float(profile.get("atr_pctile_60", 0.5) or 0.5)
    hurst = float(profile.get("hurst_100", 0.5) or 0.5)
    autocorr = float(profile.get("autocorr_20", 0.0) or 0.0)
    if hurst > 0.55 and autocorr > 0:
        return "延续态"
    if hurst < 0.45 and autocorr < 0:
        return "回归态"
    if adx >= adx_cut and vol < vol_cut:
        return "趋势-低波"
    if adx >= adx_cut:
        return "趋势-高波"
    if vol >= vol_cut:
        return "高波-无序"
    return "震荡-低波"


def _engines_for(name: str) -> list[dict[str, str]]:
    if name.startswith("趋势") or name == "延续态":
        style = STYLE_TREND
    elif name.startswith("震荡") or name == "回归态":
        style = STYLE_REVERSION
    else:
        return []
    engines = [
        {"id": engine_id, "name": engine_name, "style": STYLE_LABELS.get(style, "")}
        for engine_id, engine_name in ENGINES_BY_STYLE.get(style, ())
    ]
    return engines


def fit_regimes(
    df: pd.DataFrame,
    *,
    k: int | None = None,
    train_ratio: float = 0.70,
    seed: int = 42,
) -> RegimeResult:
    """样本内拟合 KMeans，样本外按最近中心归类；输出画像、转移矩阵和引擎清单。"""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    features = build_regime_features(df)
    ratio = min(max(float(train_ratio), 0.50), 0.85)
    usable = features.dropna()
    if len(usable) < 200:
        raise ValueError("状态识别至少要 200 根完整特征，先补 K 线。")
    split = max(int(len(usable) * ratio), 120)
    train = usable.iloc[:split]
    rest = usable.iloc[split:]

    columns = [name for name in (*DEFAULT_FEATURES, OI_FEATURE) if name in usable.columns]
    if "oi" not in df.columns:
        columns = [name for name in columns if name != OI_FEATURE]
    columns = _pick_features(train, columns)
    if len(columns) < 3:
        raise ValueError("可用状态特征不足 3 个，先补成交量/持仓量或更长的历史。")

    mean = train[columns].mean()
    std = train[columns].std(ddof=0).replace(0, np.nan)
    std = std.fillna(1.0)
    train_z = (train[columns] - mean) / std
    if k is None:
        best: tuple[float, int, Any, np.ndarray] | None = None
        for candidate in (2, 3, 4):
            model = KMeans(n_clusters=candidate, n_init=10, random_state=int(seed))
            labels = model.fit_predict(train_z)
            shares = np.bincount(labels, minlength=candidate) / len(labels)
            if shares.min() < MIN_SHARE:
                continue
            sample = train_z
            if len(sample) > 5000:
                sample = train_z.iloc[:: max(1, len(train_z) // 5000)]
                labels_sample = model.predict(sample)
            else:
                labels_sample = labels
            try:
                score = float(silhouette_score(sample, labels_sample))
            except Exception:  # noqa: BLE001
                score = -1.0
            if best is None or score > best[0] + 0.02:
                best = (score, candidate, model, labels)
        if best is None:
            raise ValueError("2~4 个状态都切不出足够大的簇，样本可能太单一。")
        silhouette, chosen_k, model, labels = best
    else:
        chosen_k = int(k)
        if chosen_k < 2 or chosen_k > 6:
            raise ValueError("聚类数只能选 2~6。")
        model = KMeans(n_clusters=chosen_k, n_init=10, random_state=int(seed))
        labels = model.fit_predict(train_z)
        try:
            silhouette = float(silhouette_score(train_z, labels))
        except Exception:  # noqa: BLE001
            silhouette = 0.0

    full_z = (usable[columns] - mean) / std
    all_labels = model.predict(full_z)
    label_series = pd.Series(all_labels, index=usable.index, name="state")

    adx_cut = float(train["adx_14"].median()) + 5.0
    vol_cut = float(train["atr_pctile_60"].median()) + 0.10
    fwd = forward_return(df["close"].astype(float), 6).reindex(usable.index)
    factor_frame = compute_factor_frame(df).reindex(usable.index)

    names: dict[int, str] = {}
    profiles: list[dict[str, Any]] = []
    for cluster in range(chosen_k):
        mask = label_series == cluster
        count = int(mask.sum())
        share = count / len(label_series) if len(label_series) else 0.0
        raw_means = {name: float(usable.loc[mask, name].mean()) for name in columns}
        name = _name_state(raw_means, adx_cut, vol_cut)
        if name in names.values():
            name = f"{name}#{cluster + 1}"
        names[cluster] = name
        top: list[dict[str, Any]] = []
        if count >= MIN_STATE_SAMPLES:
            target = fwd[mask]
            for spec in FACTOR_BY_ID.values():
                series = factor_frame[spec.id][mask]
                pair = pd.concat({"x": series, "y": target}, axis=1).dropna()
                if len(pair) < MIN_STATE_SAMPLES:
                    continue
                corr = pair["x"].corr(pair["y"], method="spearman")
                if corr is None or pd.isna(corr):
                    continue
                top.append({"factor": spec.id, "title": spec.title, "ic": float(corr)})
            top.sort(key=lambda row: abs(row["ic"]), reverse=True)
            top = top[:3]
        profiles.append(
            {
                "cluster": cluster,
                "name": name,
                "n": count,
                "share": share,
                "features": raw_means,
                "fwd_mean": float(fwd[mask].mean()) if count else 0.0,
                "fwd_vol": float(fwd[mask].std(ddof=1)) if count > 1 else 0.0,
                "top_factors": top,
            }
        )

    matrix = np.zeros((chosen_k, chosen_k), dtype=float)
    codes = label_series.to_numpy()
    for step in range(1, len(codes)):
        matrix[codes[step - 1], codes[step]] += 1
    row_sums = matrix.sum(axis=1, keepdims=True)
    transition = np.divide(matrix, row_sums, out=np.zeros_like(matrix), where=row_sums > 0)
    persistence = np.diag(transition).tolist()

    warnings: list[str] = []
    if silhouette < 0.20:
        warnings.append("轮廓系数低于 0.20，状态分离度弱，只作描述，不据此切策略。")
    if min(row["share"] for row in profiles) < MIN_SHARE:
        warnings.append("有状态样本占比低于 10%，画像容易不稳定。")
    if len(rest) >= 100:
        train_mean = train_z.mean()
        rest_z = (rest[columns] - mean) / std
        drift = float((rest_z.mean() - train_mean).abs().max())
        if drift > 1.0:
            warnings.append("样本外特征均值漂移超过 1 个标准差，标记「状态漂移」。")

    engines: list[dict[str, Any]] = []
    for profile in profiles:
        names_for_state = _engines_for(profile["name"])
        engines.append(
            {
                "state": profile["name"],
                "share": profile["share"],
                "engines": names_for_state,
                "note": (
                    "研究清单，不是切换指令"
                    if names_for_state
                    else "本程序没有专用回归引擎，只作过滤和风险提示"
                ),
            }
        )
    return RegimeResult(
        k=chosen_k,
        seed=int(seed),
        train_ratio=ratio,
        silhouette=float(silhouette),
        features=columns,
        names=names,
        labels=label_series,
        profiles=profiles,
        transition=[[float(v) for v in row] for row in transition],
        persistence=[float(v) for v in persistence],
        engines=engines,
        warnings=warnings,
    )


def format_regime_report(result: RegimeResult) -> str:
    """把状态结果写成报告段落，供界面右侧文本和 JSON 共用。"""
    lines = ["", "市场状态（研究画像，不是切换指令）", ""]
    lines.append(
        f"聚类 k={result.k}  轮廓系数 {result.silhouette:.2f}  样本内比例 {result.train_ratio:.0%}  种子 {result.seed}"
    )
    lines.append(f"特征  {'、'.join(FEATURE_LABELS.get(name, name) for name in result.features)}")
    profiles = sorted(result.profiles, key=lambda row: row["share"], reverse=True)
    for profile in profiles:
        index = int(profile["cluster"])
        keep = result.persistence[index] if index < len(result.persistence) else 0.0
        lines.append(
            f"- {profile['name']}：样本 {profile['n']}（{profile['share']:.0%}），"
            f"持续概率 {keep:.2f}，未来6根均值 {profile['fwd_mean']:+.4f}，波动 {profile['fwd_vol']:.4f}"
        )
        if profile["top_factors"]:
            top = "；".join(
                f"{row['title']} IC {row['ic']:+.2f}" for row in profile["top_factors"]
            )
            lines.append(f"  主要因子：{top}")
        else:
            lines.append("  主要因子：样本不足，暂不下结论")
    lines.append("")
    lines.append("状态转移矩阵（行=当前，列=下一根）：")
    header = "        " + "  ".join(f"{name[:6]:>6}" for name in [item["name"] for item in profiles])
    lines.append(header)
    order = [int(item["cluster"]) for item in profiles]
    for row_index in order:
        values = result.transition[row_index] if row_index < len(result.transition) else []
        cells = "  ".join(f"{values[col]:6.2f}" if col < len(values) else "   —  " for col in order)
        lines.append(f"{result.names.get(row_index, str(row_index))[:6]:>6}  {cells}")
    lines.append("")
    lines.append("状态 × 可去交叉测试的策略：")
    for item in result.engines:
        names = "、".join(engine["name"] for engine in item["engines"]) or "无"
        lines.append(f"- {item['state']}（{item['share']:.0%}）：{names}。{item['note']}")
    if result.warnings:
        lines.append("")
        lines.extend(f"提醒：{msg}" for msg in result.warnings)
    lines.append("")
    lines.append("状态标签只用于研究，不会自动改实盘策略；要用某条结论，必须另写完整开平仓规则。")
    return "\n".join(lines)


def save_regimes(root: Any, result: RegimeResult, *, symbol: str = "") -> Any:
    """把最近一次状态结果写到 data/factor_mining/regimes[_合约].json。"""
    import json
    import re
    from pathlib import Path

    suffix = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(symbol or "")).strip("_")
    name = f"regimes_{suffix}.json" if suffix else "regimes.json"
    path = Path(root) / "data" / "factor_mining" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["updated_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
