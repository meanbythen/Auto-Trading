"""斜率阈值标定：用未来趋势标签和样本外切片给阈值找依据。

只读本地 K 线，不改配置、不下单。输出每个「品种 × 周期 × 回看根数」的
角度分位数、覆盖率、精确率、召回率和样本外表现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from src.data.bar_store import BarStore
from src.data.inventory import split_stored_symbol
from src.strategy.indicators import average_true_range, ema_series

FIXED_THRESHOLDS: tuple[float, ...] = (
    2.0,
    2.5,
    3.0,
    3.5,
    4.0,
    4.5,
    5.0,
    5.5,
    6.0,
    6.5,
    7.0,
)


@dataclass(frozen=True)
class CalibrationRow:
    """一个品种、周期、回看根数、阈值下的一组指标。"""

    symbol: str
    period: str
    lookback: int
    threshold: float
    calib_coverage: float
    calib_precision: float
    calib_recall: float
    calib_auc: float
    oos_coverage: float
    oos_precision: float
    oos_recall: float
    oos_auc: float
    oos_false_rate: float


def slope_vectors(
    ema: np.ndarray, atr: np.ndarray, lookback: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """一次算出整段均线的角度、t 值和 R²，口径与 measure_slope 的 reg 一致。"""
    n = max(2, int(lookback))
    size = len(ema)
    angle = np.full(size, np.nan, dtype=float)
    t_stat = np.full(size, np.nan, dtype=float)
    r2 = np.full(size, np.nan, dtype=float)
    if size <= n:
        return angle, t_stat, r2
    windows = np.lib.stride_tricks.sliding_window_view(ema, n + 1)
    x = np.arange(n + 1, dtype=float)
    mean_x = n / 2.0
    den = float(((x - mean_x) ** 2).sum())
    mean_y = windows.mean(axis=1)
    centered = windows - mean_y[:, None]
    slope = (centered * (x - mean_x)).sum(axis=1) / den
    rise = slope * n
    a = atr[n:]
    ok = np.isfinite(a) & (a > 1e-12)
    ang = np.full(len(a), np.nan, dtype=float)
    ang[ok] = np.degrees(np.arctan(rise[ok] / (n * a[ok])))
    resid = windows - (mean_y[:, None] + slope[:, None] * (x - mean_x))
    ss_res = (resid**2).sum(axis=1)
    ss_tot = (centered**2).sum(axis=1)
    r2_v = np.where(
        ss_tot > 1e-18,
        1.0 - ss_res / ss_tot,
        np.where(ss_res <= 1e-18, 1.0, 0.0),
    )
    se = np.sqrt(np.divide(ss_res / max(len(x) - 2, 1), den, out=np.full_like(ss_res, np.inf), where=den > 0))
    t_v = np.divide(slope, se, out=np.zeros_like(slope), where=se > 0)
    t_v = np.where((se <= 0) & (np.abs(slope) > 0), np.inf, t_v)
    angle[n:] = ang
    t_stat[n:] = t_v
    r2[n:] = r2_v
    return angle, t_stat, r2


def future_trend_labels(
    close: np.ndarray,
    atr: np.ndarray,
    horizon: int = 20,
    *,
    move_mult: float = 2.0,
    adverse_mult: float = 1.0,
) -> np.ndarray:
    """给每根 K 线打未来标签：1 上涨趋势、-1 下跌趋势、0 震荡、NaN 数据不够。

    趋势定义：未来 horizon 根内同向净移动 ≥ move_mult × ATR，且逆向回撤 <
    adverse_mult × ATR。只看收盘价路径，不使用未来 K 线以外的信息。
    """
    h = max(2, int(horizon))
    size = len(close)
    labels = np.full(size, np.nan, dtype=float)
    for i in range(size - h):
        base = float(close[i])
        scale = float(atr[i])
        if not np.isfinite(base) or not np.isfinite(scale) or scale <= 1e-12:
            labels[i] = 0.0
            continue
        seg = close[i : i + h + 1]
        net = float(seg[-1]) - base
        up = float(seg.max()) - base
        down = base - float(seg.min())
        if net >= move_mult * scale and down <= adverse_mult * scale:
            labels[i] = 1.0
        elif net <= -move_mult * scale and up <= adverse_mult * scale:
            labels[i] = -1.0
        else:
            labels[i] = 0.0
    return labels


def roc_auc(scores: np.ndarray, positive: np.ndarray) -> float:
    """排序法算 AUC；样本只有一类时返回 NaN。"""
    score = np.asarray(scores, dtype=float)
    pos = np.asarray(positive, dtype=bool)
    ok = np.isfinite(score)
    score = score[ok]
    pos = pos[ok]
    n1 = int(pos.sum())
    n0 = int(len(pos) - n1)
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = pd.Series(score).rank(method="average").to_numpy()
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def _metrics(angle: np.ndarray, labels: np.ndarray, threshold: float, mask: np.ndarray) -> dict[str, float]:
    ok = mask & np.isfinite(angle) & np.isfinite(labels)
    if not ok.any():
        return {
            "coverage": float("nan"),
            "precision": float("nan"),
            "recall": float("nan"),
            "auc": float("nan"),
            "false_rate": float("nan"),
        }
    a = angle[ok]
    lab = labels[ok]
    passed = np.abs(a) >= threshold
    trend = lab != 0
    matched = passed & (np.sign(a) == lab)
    n_pass = int(passed.sum())
    n_trend = int(trend.sum())
    return {
        "coverage": n_pass / len(a),
        "precision": float(matched.sum()) / n_pass if n_pass else float("nan"),
        "recall": float(matched.sum()) / n_trend if n_trend else float("nan"),
        "auc": roc_auc(np.abs(a), trend),
        "false_rate": float((passed & ~trend).sum()) / n_pass if n_pass else float("nan"),
    }


def _series_summary(
    symbol: str,
    period: str,
    frame: pd.DataFrame,
    angle: np.ndarray,
    t_stat: np.ndarray,
    labels: np.ndarray,
    threshold: float,
    oos: dict[str, float],
) -> dict[str, Any]:
    ok = np.isfinite(angle) & np.isfinite(labels)
    vals = np.abs(angle[ok])
    tvals = np.abs(t_stat[ok & np.isfinite(t_stat)])
    quant = np.percentile(vals, [50, 70, 75, 80, 85, 90]) if len(vals) else np.full(6, np.nan)
    tq = np.percentile(tvals, [50, 80, 90]) if len(tvals) else np.full(3, np.nan)
    trend = labels[ok] != 0
    return {
        "合约": symbol,
        "周期": period,
        "根数": len(frame),
        "有效根数": int(ok.sum()),
        "从": str(frame.index[0])[:19],
        "到": str(frame.index[-1])[:19],
        "中位角": round(float(quant[0]), 2),
        "q70": round(float(quant[1]), 2),
        "q75": round(float(quant[2]), 2),
        "q80": round(float(quant[3]), 2),
        "q85": round(float(quant[4]), 2),
        "q90": round(float(quant[5]), 2),
        "t中位": round(float(tq[0]), 2),
        "t_q80": round(float(tq[1]), 2),
        "t_q90": round(float(tq[2]), 2),
        "t_AUC": round(roc_auc(np.abs(t_stat[ok]), trend), 3),
        "建议阈值": round(threshold, 1),
        "样本外覆盖": round(oos["coverage"], 3),
        "样本外精确": round(oos["precision"], 3),
        "样本外召回": round(oos["recall"], 3),
        "样本外AUC": round(oos["auc"], 3),
        "样本外假信号": round(oos["false_rate"], 3),
    }


def calibrate_frame(
    frame: pd.DataFrame,
    symbol: str,
    period: str,
    *,
    ema_period: int = 120,
    lookbacks: Sequence[int] = (15, 20, 25, 30),
    horizon: int = 20,
    split: float = 0.7,
) -> tuple[list[CalibrationRow], dict[str, Any]]:
    """一个合约一个周期：算分位数，按时间切分，返回每个阈值的指标和推荐值。"""
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    ema = ema_series(close, ema_period).to_numpy(dtype=float)
    atr = average_true_range(high, low, close, 14).to_numpy(dtype=float)
    labels = future_trend_labels(close.to_numpy(dtype=float), atr, horizon)
    cut = max(1, min(len(frame) - 1, int(len(frame) * split)))
    calib_mask = np.arange(len(frame)) < cut
    oos_mask = ~calib_mask
    rows: list[CalibrationRow] = []
    vectors: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    best: tuple[float, CalibrationRow, dict[str, float]] | None = None
    for n in lookbacks:
        angle, t_stat, _ = slope_vectors(ema, atr, n)
        vectors[int(n)] = (angle, t_stat)
        ok = np.isfinite(angle) & np.isfinite(labels) & calib_mask
        if not ok.any():
            continue
        q = np.percentile(np.abs(angle[ok]), [75, 80, 85])
        candidates = sorted({round(float(v), 1) for v in (*FIXED_THRESHOLDS, *q)})
        for t in candidates:
            cal = _metrics(angle, labels, t, calib_mask)
            oos = _metrics(angle, labels, t, oos_mask)
            row = CalibrationRow(
                symbol=symbol,
                period=period,
                lookback=int(n),
                threshold=float(t),
                calib_coverage=cal["coverage"],
                calib_precision=cal["precision"],
                calib_recall=cal["recall"],
                calib_auc=cal["auc"],
                oos_coverage=oos["coverage"],
                oos_precision=oos["precision"],
                oos_recall=oos["recall"],
                oos_auc=oos["auc"],
                oos_false_rate=oos["false_rate"],
            )
            rows.append(row)
            if 0.10 <= oos["coverage"] <= 0.40 and np.isfinite(oos["auc"]):
                score = oos["auc"] - abs(t - float(q[1])) * 0.01
                if best is None or score > best[0]:
                    best = (score, row, oos)
    # 只有样本外 AUC 明显高于 0.55 才采信数据驱动的阈值；否则回退到标定段 q80。
    # 15 分钟数据上常见 AUC≈0.5，这时「最优阈值」只是噪声，不能当成结论。
    if best is not None and best[2]["auc"] >= 0.55:
        chosen, chosen_oos = best[1], best[2]
        verdict = "可用（样本外AUC≥0.60）" if best[2]["auc"] >= 0.60 else "偏弱（样本外AUC 0.55~0.60）"
    else:
        n = int(lookbacks[1] if len(lookbacks) > 1 else lookbacks[0])
        if n not in vectors:
            angle, t_stat, _ = slope_vectors(ema, atr, n)
            vectors[n] = (angle, t_stat)
        angle, _ = vectors[n]
        ok = np.isfinite(angle) & np.isfinite(labels) & calib_mask
        t = float(np.percentile(np.abs(angle[ok]), 80)) if ok.any() else 3.0
        chosen = CalibrationRow(symbol, period, n, round(t, 1), *(float("nan"),) * 9)
        chosen_oos = _metrics(angle, labels, chosen.threshold, oos_mask)
        verdict = "区分度不足（样本外AUC<0.55），建议维持现阈值并继续观察"
    if chosen.lookback in vectors:
        angle, t_stat = vectors[chosen.lookback]
    else:
        angle = np.full(len(frame), np.nan)
        t_stat = np.full(len(frame), np.nan)
    summary = _series_summary(
        symbol,
        period,
        frame,
        angle,
        t_stat,
        labels,
        chosen.threshold,
        chosen_oos,
    )
    summary["推荐回看"] = chosen.lookback
    summary["标定结论"] = verdict
    return rows, summary


def calibrate_store(
    root: Path | str,
    *,
    symbols: Iterable[str] | None = None,
    periods: Sequence[str] = ("15",),
    ema_period: int = 120,
    lookbacks: Sequence[int] = (15, 20, 25, 30),
    horizon: int = 20,
    split: float = 0.7,
) -> tuple[list[CalibrationRow], list[dict[str, Any]], dict[str, Any]]:
    """从清洗库挑合约和周期，逐个标定。"""
    base = Path(root)
    store = BarStore(base / "data" / "klines" / "market.sqlite")
    wanted = {str(s).strip() for s in symbols} if symbols else None
    picked: dict[tuple[str, str], str] = {}
    for item in store.summarize():
        stored = str(item["symbol"])
        period = str(item["period"])
        if period not in periods:
            continue
        contract, _ = split_stored_symbol(stored)
        if wanted is not None and contract not in wanted and stored not in wanted:
            continue
        key = (contract, period)
        old = picked.get(key)
        if old is None or ("@tq" in stored and "@tq" not in old):
            picked[key] = stored
    rows: list[CalibrationRow] = []
    summaries: list[dict[str, Any]] = []
    skipped: list[str] = []
    for (contract, period), stored in sorted(picked.items()):
        frame = store.load(stored, period)
        need = ema_period + max(lookbacks) + horizon + 50
        if frame is None or len(frame) < need:
            skipped.append(f"{stored}/{period}（{0 if frame is None else len(frame)} 根，需 {need}）")
            continue
        part, summary = calibrate_frame(
            frame,
            contract,
            period,
            ema_period=ema_period,
            lookbacks=lookbacks,
            horizon=horizon,
            split=split,
        )
        rows.extend(part)
        summaries.append(summary)
    meta = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "数据": str((base / "data" / "klines" / "market.sqlite").resolve()),
        "周期": "、".join(periods),
        "EMA": ema_period,
        "回看": "、".join(str(n) for n in lookbacks),
        "未来窗口": horizon,
        "切分": f"前 {split:.0%} 标定 / 后 {1 - split:.0%} 样本外",
        "未纳入": "；".join(skipped) or "无",
    }
    return rows, summaries, meta


def write_report(
    path: Path,
    rows: Sequence[CalibrationRow],
    summaries: Sequence[dict[str, Any]],
    meta: dict[str, Any],
) -> Path:
    """写 Excel：说明、推荐、明细。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "说明"
    ws.append(["项目", "内容"])
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="1F4E79")
        cell.font = Font(color="FFFFFF", bold=True)
    for key, value in meta.items():
        ws.append([key, value])
    for row in ws.iter_rows(min_row=2, max_col=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 90
    ws.freeze_panes = "A2"

    def _sheet(title: str, columns: list[str], records: Sequence[dict[str, Any]]) -> None:
        sh = wb.create_sheet(title)
        sh.append(columns)
        for cell in sh[1]:
            cell.fill = PatternFill("solid", fgColor="1F4E79")
            cell.font = Font(color="FFFFFF", bold=True)
        for rec in records:
            sh.append([rec.get(col) for col in columns])
        sh.freeze_panes = "A2"
        sh.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(sh.max_row, 1)}"
        for idx, col in enumerate(columns, start=1):
            sh.column_dimensions[get_column_letter(idx)].width = max(10, min(len(col) * 2 + 4, 26))

    rec_columns = [field for field in CalibrationRow.__dataclass_fields__]
    rec_rows = [
        {
            "symbol": r.symbol,
            "period": r.period,
            "lookback": r.lookback,
            "threshold": r.threshold,
            "calib_coverage": round(r.calib_coverage, 3),
            "calib_precision": round(r.calib_precision, 3),
            "calib_recall": round(r.calib_recall, 3),
            "calib_auc": round(r.calib_auc, 3),
            "oos_coverage": round(r.oos_coverage, 3),
            "oos_precision": round(r.oos_precision, 3),
            "oos_recall": round(r.oos_recall, 3),
            "oos_auc": round(r.oos_auc, 3),
            "oos_false_rate": round(r.oos_false_rate, 3),
        }
        for r in rows
    ]
    _sheet("明细", rec_columns, rec_rows)
    summary_columns = [
        "合约",
        "周期",
        "根数",
        "有效根数",
        "从",
        "到",
        "中位角",
        "q70",
        "q75",
        "q80",
        "q85",
        "q90",
        "t中位",
        "t_q80",
        "t_q90",
        "t_AUC",
        "推荐回看",
        "建议阈值",
        "标定结论",
        "样本外覆盖",
        "样本外精确",
        "样本外召回",
        "样本外AUC",
        "样本外假信号",
    ]
    _sheet("推荐", summary_columns, list(summaries))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
