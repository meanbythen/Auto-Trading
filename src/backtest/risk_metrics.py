"""组合风险指标：历史 VaR/CVaR、极值窗口、相关性聚类。

功能：从组合日收益算单日 VaR/CVaR、最差单日与最差连续窗口；按相关系数把品种归簇。
原理：只读组合结果里的权益曲线和相关性矩阵，不重跑回测、不改权重。
      样本不足时返回 None，由展示层写「算不出」，避免把缺失当成 0 风险。
"""

from __future__ import annotations

from typing import Any

# VaR/CVaR 至少要有这么多天日收益；样本太少时分位数没有意义。
MIN_RISK_DAYS = 20
# 相关系数达到这个值就算同一簇：同簇品种按一个风险来源控制仓位。
CLUSTER_THRESHOLD = 0.7


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _day_key(value: Any) -> str:
    text = str(value or "").strip().replace("T", " ")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def daily_returns(equity: Any, times: Any) -> list[tuple[str, float]]:
    """把权益曲线压成「交易日 → 当日收益」，同日多条取最后一条。"""
    values = list(equity or [])
    stamps = list(times or [])
    if not values or not stamps:
        return []
    if len(stamps) < len(values):
        stamps = stamps + [stamps[-1]] * (len(values) - len(stamps))
    curve: dict[str, float] = {}
    order: list[str] = []
    for value, stamp in zip(values, stamps):
        day = _day_key(stamp)
        if not day:
            continue
        if day not in curve:
            order.append(day)
        curve[day] = _number(value)
    out: list[tuple[str, float]] = []
    prev: float | None = None
    for day in order:
        value = curve[day]
        if prev is not None and abs(prev) > 1e-12:
            out.append((day, value / prev - 1.0))
        prev = value
    return out


def _quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数，q 取 0~1。输入必须已升序。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = max(0.0, min(1.0, q)) * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    weight = pos - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def historical_var(returns: list[float], level: float = 0.95) -> float | None:
    """历史法单日 VaR，返回正数表示亏损比例；样本不足返回 None。"""
    if len(returns) < MIN_RISK_DAYS:
        return None
    loss = -_quantile(sorted(returns), 1.0 - level)
    return max(0.0, loss)


def historical_cvar(returns: list[float], level: float = 0.95) -> float | None:
    """历史法尾部均值（CVaR），比 VaR 更看极端亏损；样本不足返回 None。"""
    if len(returns) < MIN_RISK_DAYS:
        return None
    cutoff = _quantile(sorted(returns), 1.0 - level)
    tail = [item for item in returns if item <= cutoff]
    if not tail:
        return max(0.0, -cutoff)
    return max(0.0, -sum(tail) / len(tail))


def worst_day(rows: list[tuple[str, float]]) -> dict[str, Any] | None:
    if not rows:
        return None
    day, value = min(rows, key=lambda item: item[1])
    return {"day": day, "ret": value}


def worst_window(rows: list[tuple[str, float]], window: int) -> dict[str, Any] | None:
    """最差连续 window 天的累计收益（简单相加，不按复利）。"""
    if window <= 0 or len(rows) < window:
        return None
    best: dict[str, Any] | None = None
    for index in range(len(rows) - window + 1):
        chunk = rows[index : index + window]
        total = sum(item[1] for item in chunk)
        if best is None or total < best["ret"]:
            best = {"window": window, "ret": total, "start": chunk[0][0], "end": chunk[-1][0]}
    return best


def cluster_symbols(correlation: dict[str, dict[str, float]], threshold: float = CLUSTER_THRESHOLD) -> list[list[str]]:
    """把相关系数 ≥ 阈值的品种并成一簇（只看正相关，负相关是对冲不算同源）。"""
    symbols = sorted(correlation)
    parent = {symbol: symbol for symbol in symbols}

    def _find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for index, left in enumerate(symbols):
        for right in symbols[index + 1 :]:
            value = _number((correlation.get(left) or {}).get(right))
            if value >= threshold:
                a, b = _find(left), _find(right)
                if a != b:
                    parent[b] = a
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(_find(symbol), []).append(symbol)
    return [sorted(group) for _, group in sorted(groups.items())]


def risk_summary(
    equity: Any,
    times: Any,
    correlation: dict[str, dict[str, float]] | None = None,
    *,
    cluster_threshold: float = CLUSTER_THRESHOLD,
) -> dict[str, Any]:
    """组合层面的风险摘要。样本不足时各指标为 None，并写明原因。"""
    rows = daily_returns(equity, times)
    values = [item[1] for item in rows]
    notes: list[str] = []
    if len(values) < MIN_RISK_DAYS:
        notes.append(f"日收益只有 {len(values)} 天，不足 {MIN_RISK_DAYS} 天，VaR/CVaR 算不出")
    summary: dict[str, Any] = {
        "days": len(values),
        "var": {label: historical_var(values, level) for label, level in (("95", 0.95), ("99", 0.99))},
        "cvar": {"95": historical_cvar(values, 0.95)},
        "worst_day": worst_day(rows),
        "worst_windows": [item for item in (worst_window(rows, size) for size in (5, 20)) if item],
        "clusters": cluster_symbols(correlation or {}, cluster_threshold),
        "notes": notes,
    }
    cluster_note = _cluster_note(summary["clusters"])
    if cluster_note:
        summary["notes"].append(cluster_note)
    return summary


def _cluster_note(clusters: list[list[str]]) -> str:
    merged = [group for group in clusters if len(group) > 1]
    if not merged:
        return ""
    text = "、".join("[" + "×".join(group) + "]" for group in merged)
    return f"{text} 日收益同向高相关，按一个风险来源控制仓位"


def format_risk_lines(summary: dict[str, Any] | None) -> list[str]:
    """把风险摘要写成中文几行，组合结果区直接用。"""
    if not summary:
        return []
    lines: list[str] = []
    var = summary.get("var") or {}
    cvar = summary.get("cvar") or {}
    if var.get("95") is not None:
        lines.append(
            f"风险      VaR95 {var['95']:.2%} · VaR99 {_number(var.get('99')):.2%} · "
            f"CVaR95 {_number(cvar.get('95')):.2%}（历史法，单日）"
        )
    worst = summary.get("worst_day") or {}
    parts = []
    if worst:
        parts.append(f"最差单日 {_number(worst.get('ret')):.2%}（{worst.get('day')}）")
    for item in summary.get("worst_windows") or []:
        parts.append(f"最差 {item.get('window')} 日 {_number(item.get('ret')):.2%}")
    if parts:
        lines.append("极值      " + " · ".join(parts))
    clusters = summary.get("clusters") or []
    if clusters:
        lines.append("聚类      " + " ｜ ".join("[" + "×".join(group) + "]" for group in clusters))
    for note in summary.get("notes") or []:
        lines.append("提示      " + note)
    return lines
