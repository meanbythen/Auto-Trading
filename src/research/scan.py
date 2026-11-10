"""
多维度扫统计规律。

对每个因子、每个预先指定的双因子组合，看它和「后面 N 根的涨跌」是否同向。
样本从中间切开，两半都要过关，才算一条规律。多个品种时，最好至少两个品种同时过关。

产出是说明书和数字证据，不是买或卖。本模块不调用下单通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.research.factors import FACTOR_BY_ID, FACTORS, PAIRS, compute_factor_frame

# 往后看几根。3/6/12 覆盖很短、短、稍长三档，避免只盯一根的噪声。
HORIZONS: tuple[int, ...] = (3, 6, 12)
# 每一半至少要有这么多根，太少时 t 值不可信。
MIN_HALF = 80
# 大约 10% 显著性的粗门槛。不宣称精确 p 值。
MIN_ABS_T = 1.65


@dataclass
class HalfStats:
    """样本一半上的相关和高低组差距。"""

    n: int
    corr: float
    spread: float
    t: float


# 把整段再切成几段，看是不是大部分时间都同向。默认 4 段，3 段同向才算大部分时间。
# 每一段至少要有半样本门槛那么多根，否则算不出可信的 t。
N_WINDOWS = 4
MIN_WINDOW = MIN_HALF


@dataclass
class SymbolHit:
    """某一个品种上，一条假设过关时记下的数字。"""

    symbol: str
    first: HalfStats
    second: HalfStats
    windows_ok: int = 0
    windows_total: int = 0


@dataclass
class Regularity:
    """一条过关的规律说明书。selected 只表示人勾过，不表示已经拿去交易。"""

    id: str
    title: str
    logic: str
    kind: str
    horizon: int
    evidence: str
    symbols: list[str]
    selected: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """写成候选池 JSON 里的一项。"""
        return {
            "id": self.id,
            "title": self.title,
            "logic": self.logic,
            "kind": self.kind,
            "horizon": self.horizon,
            "evidence": self.evidence,
            "symbols": list(self.symbols),
            "selected": bool(self.selected),
            "details": dict(self.details),
        }


def forward_return(close: pd.Series, horizon: int) -> pd.Series:
    """
    从这一根收到往后第 horizon 根收盘的涨跌幅。

    这是要解释的结果，不是因子。最后 horizon 根没有完整未来，会是空值。
    """
    c = close.astype(float)
    future = c.shift(-int(horizon))
    return future / c.replace(0, pd.NA) - 1.0


def _half_stats(factor: pd.Series, fwd: pd.Series) -> HalfStats | None:
    """
    算斯皮尔曼相关，以及最高三分位 vs 最低三分位的平均收益差。

    三分位差能抓住「只在两端才有用」的非线性，不只看直线相关。
    """
    aligned = pd.concat({"x": factor, "y": fwd}, axis=1).dropna()
    if len(aligned) < MIN_HALF:
        return None
    x = aligned["x"]
    y = aligned["y"]
    if int(x.nunique()) < 2 or int(y.nunique()) < 2:
        corr = 0.0
    else:
        raw_corr = x.corr(y, method="spearman")
        corr = 0.0 if raw_corr is None or pd.isna(raw_corr) else float(raw_corr)
    q1, q2 = x.quantile([1.0 / 3.0, 2.0 / 3.0])
    top = y[x >= q2]
    bot = y[x <= q1]
    if len(top) < 20 or len(bot) < 20:
        return None
    spread = float(top.mean() - bot.mean())
    var_top = float(top.var(ddof=1) or 0.0)
    var_bot = float(bot.var(ddof=1) or 0.0)
    se = float(np.sqrt(var_top / len(top) + var_bot / len(bot)))
    t_val = spread / se if se > 1e-12 else 0.0
    return HalfStats(n=int(len(aligned)), corr=corr, spread=spread, t=float(t_val))


def _split_halves(factor: pd.Series, fwd: pd.Series) -> tuple[HalfStats, HalfStats] | None:
    """按时间对半切，两半都要能算出统计量。"""
    aligned = pd.concat({"x": factor, "y": fwd}, axis=1).dropna()
    if len(aligned) < MIN_HALF * 2:
        return None
    mid = len(aligned) // 2
    first = _half_stats(aligned["x"].iloc[:mid], aligned["y"].iloc[:mid])
    second = _half_stats(aligned["x"].iloc[mid:], aligned["y"].iloc[mid:])
    if first is None or second is None:
        return None
    return first, second


def _both_significant(first: HalfStats, second: HalfStats) -> bool:
    """两半 t 值同号，并且绝对值都过门槛。"""
    if first.t == 0 or second.t == 0:
        return False
    if first.t * second.t <= 0:
        return False
    return abs(first.t) >= MIN_ABS_T and abs(second.t) >= MIN_ABS_T


def _zscore(series: pd.Series) -> pd.Series:
    """用名次换成大约 -1 到 1 的分数，减少极端值带偏。"""
    ranks = series.rank(pct=True)
    return (ranks - 0.5) * 2.0


def _interaction(left: pd.Series, right: pd.Series) -> pd.Series:
    """两个因子同时偏高或同时偏低时，乘积同号放大。"""
    return _zscore(left) * _zscore(right)


def _window_agreement(factor: pd.Series, fwd: pd.Series, sign_ref: float) -> tuple[int, int]:
    """
    按时间切成几段，数有几段和总方向同向且显著。

    样本不够切 4 段时，能切几段算几段。切不开就用 0。
    """
    aligned = pd.concat({"x": factor, "y": fwd}, axis=1).dropna()
    if len(aligned) < MIN_WINDOW * 2:
        return 0, 0
    n_win = min(N_WINDOWS, max(2, len(aligned) // MIN_WINDOW))
    ok = 0
    total = 0
    size = len(aligned) // n_win
    for i in range(n_win):
        start = i * size
        stop = len(aligned) if i == n_win - 1 else (i + 1) * size
        stats = _half_stats(aligned["x"].iloc[start:stop], aligned["y"].iloc[start:stop])
        if stats is None:
            continue
        total += 1
        if stats.t * sign_ref > 0 and abs(stats.t) >= MIN_ABS_T:
            ok += 1
    return ok, total


def _eval_series(factor: pd.Series, close: pd.Series, horizon: int) -> tuple[HalfStats, HalfStats] | None:
    """一条序列对某一档未来收益是否两半都显著。"""
    fwd = forward_return(close, horizon)
    split = _split_halves(factor, fwd)
    if split is None:
        return None
    first, second = split
    if not _both_significant(first, second):
        return None
    return first, second


def _direction_text(t_val: float) -> str:
    """把 t 的正负说成人话：因子高时后面涨还是跌。"""
    return "因子高时后面偏涨" if t_val > 0 else "因子高时后面偏跌"


def _evidence(first: HalfStats, second: HalfStats, horizon: int, symbols: list[str]) -> str:
    """给界面看的一段证据，不写函数名。"""
    return (
        f"往后看 {horizon} 根。"
        f"前半 {first.n} 根：相关 {first.corr:.3f}，高低组差 {first.spread:.4%}，t={first.t:.2f}；"
        f"后半 {second.n} 根：相关 {second.corr:.3f}，高低组差 {second.spread:.4%}，t={second.t:.2f}。"
        f"{_direction_text(first.t)}。"
        f"品种：{'、'.join(symbols)}。"
        "这只说明这段历史上统计上站得住，不是下一笔该买或该卖。"
    )


def scan_one_symbol(df: pd.DataFrame, symbol: str) -> dict[str, SymbolHit]:
    """
    扫一个品种上目录里的单因子和双因子。

    返回 {假设编号: 命中}，假设编号含周期，例如 ret_5|h6。
    """
    if df is None or len(df) < MIN_HALF * 2 + max(HORIZONS) + 25:
        return {}
    work = df.copy()
    if "volume" not in work.columns:
        work["volume"] = 0.0
    frame = compute_factor_frame(work)
    close = work["close"].astype(float)
    hits: dict[str, SymbolHit] = {}

    for spec in FACTORS:
        series = frame[spec.id]
        for horizon in HORIZONS:
            got = _eval_series(series, close, horizon)
            if got is None:
                continue
            first, second = got
            key = f"{spec.id}|h{horizon}"
            sign_ref = (first.t + second.t) / 2.0
            fwd = forward_return(close, horizon)
            win_ok, win_total = _window_agreement(series, fwd, sign_ref)
            hits[key] = SymbolHit(
                symbol=symbol,
                first=first,
                second=second,
                windows_ok=win_ok,
                windows_total=win_total,
            )

    for pair in PAIRS:
        if pair.left not in frame.columns or pair.right not in frame.columns:
            continue
        combo = _interaction(frame[pair.left], frame[pair.right])
        for horizon in HORIZONS:
            got = _eval_series(combo, close, horizon)
            if got is None:
                continue
            first, second = got
            key = f"{pair.id}|h{horizon}"
            sign_ref = (first.t + second.t) / 2.0
            fwd = forward_return(close, horizon)
            win_ok, win_total = _window_agreement(combo, fwd, sign_ref)
            hits[key] = SymbolHit(
                symbol=symbol,
                first=first,
                second=second,
                windows_ok=win_ok,
                windows_total=win_total,
            )
    return hits


def _mean_half(hits: list[SymbolHit], which: str) -> HalfStats:
    """多个品种的半样本数字做简单平均，只为写进说明书。"""
    parts = [getattr(item, which) for item in hits]
    return HalfStats(
        n=int(sum(p.n for p in parts) / len(parts)),
        corr=float(sum(p.corr for p in parts) / len(parts)),
        spread=float(sum(p.spread for p in parts) / len(parts)),
        t=float(sum(p.t for p in parts) / len(parts)),
    )


def scan_regularities(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    require_cross_symbol: bool = True,
    previous: list[dict[str, Any]] | None = None,
) -> list[Regularity]:
    """
    扫多个品种，把过关的规律收成列表。

    require_cross_symbol 为真、并且给了两个及以上品种时：一条规律至少要在两个品种上过关。
    只扫一个品种时仍然可以进池，但会在证据里标明「仅单品种」。
    previous 用来保留你上次勾选过的编号。
    """
    selected_ids = {str(item.get("id")) for item in (previous or []) if item.get("selected")}
    per_key: dict[str, list[SymbolHit]] = {}
    for symbol, df in bars_by_symbol.items():
        if df is None or len(df) == 0:
            continue
        for key, hit in scan_one_symbol(df, symbol).items():
            per_key.setdefault(key, []).append(hit)

    scanned = [name for name, df in bars_by_symbol.items() if df is not None and len(df) > 0]
    need_cross = bool(require_cross_symbol) and len(scanned) >= 2
    out: list[Regularity] = []

    for key, hits in sorted(per_key.items()):
        if need_cross and len(hits) < 2:
            continue
        base, _, horizon_tag = key.partition("|h")
        try:
            horizon = int(horizon_tag)
        except ValueError:
            continue
        symbols = [item.symbol for item in hits]
        first = _mean_half(hits, "first")
        second = _mean_half(hits, "second")
        extra = "" if len(symbols) >= 2 else "仅单品种，跨品种还没验证。"
        win_ok = int(sum(hit.windows_ok for hit in hits) / len(hits))
        win_total = int(sum(hit.windows_total for hit in hits) / len(hits))
        if win_total > 0:
            extra += f" 分段 {win_ok}/{win_total} 同向显著。"
            if win_ok / win_total < 0.75:
                extra += "大部分时间不够稳，类型倾向里不会用这条。"
        if base in FACTOR_BY_ID:
            spec = FACTOR_BY_ID[base]
            title = spec.title
            logic = spec.logic
            kind = "单因子"
        else:
            pair = next((item for item in PAIRS if item.id == base), None)
            if pair is None:
                continue
            title = pair.title
            logic = pair.logic
            kind = "双因子"
        item = Regularity(
            id=key,
            title=title,
            logic=logic,
            kind=kind,
            horizon=horizon,
            evidence=_evidence(first, second, horizon, symbols) + extra,
            symbols=symbols,
            selected=key in selected_ids,
            details={
                "corr_first": first.corr,
                "corr_second": second.corr,
                "t_first": first.t,
                "t_second": second.t,
                "spread_first": first.spread,
                "spread_second": second.spread,
                "cross_symbol": len(symbols) >= 2,
                "windows_ok": win_ok,
                "windows_total": win_total,
            },
        )
        out.append(item)

    out.sort(key=lambda row: abs(float(row.details.get("t_first", 0))) + abs(float(row.details.get("t_second", 0))), reverse=True)
    return out


def format_scan_report(
    items: list[Regularity],
    *,
    symbols: list[str],
    bars: dict[str, int],
    skipped: list[str],
    style_text: str = "",
) -> str:
    """界面右侧长文：先声明不是买卖点，再写类型倾向，再列过关规律。"""
    lines = [
        "规律扫描完成（这不是买卖点，扫描层不会下单）",
        f"品种  {'、'.join(symbols) or '无'}",
        "根数  " + "；".join(f"{name} {count} 根" for name, count in bars.items()),
        f"过关  {len(items)} 条（两段样本都显著；多品种时尽量要求至少两个合约）",
        "",
    ]
    if style_text:
        lines.append(style_text.rstrip())
        lines.append("")
    lines.extend(
        [
            "怎么读：因子高/低对应后面几根的平均涨跌。你要先问「这句话经济上说不说得通」，再说要不要勾进候选池。",
            "勾选只表示你认可这条逻辑，不会改现在的开平仓。类型倾向只建议去交叉测试哪一类，不会自动换策略。",
            "",
        ]
    )
    if skipped:
        lines.append("没扫成的品种：")
        lines.extend(f"- {msg}" for msg in skipped)
        lines.append("")
    if not items:
        lines.append("这一段里没有同时过关的规律。可能是样本太短、波动太乱，或这些因子在这段历史上站不住。")
        return "\n".join(lines)
    for i, item in enumerate(items, start=1):
        mark = "已勾选" if item.selected else "未勾选"
        lines.append(f"{i}. [{item.kind}] {item.title}（往后 {item.horizon} 根）  {mark}")
        lines.append(f"   逻辑：{item.logic}")
        lines.append(f"   证据：{item.evidence}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
