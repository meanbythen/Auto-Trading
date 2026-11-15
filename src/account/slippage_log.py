"""
实盘滑点记录与统计。

功能：每笔成交回报记账时，把「理论价 vs 实际成交价」的差追加到 data/slippage_log.csv。
     复盘页尾部显示汇总：各方向的滑点中位数、70 分位，并给出建议回测滑点。
原理：滑点统一折算成跳数，且永远是「对你不利为正」：
     买入（开多/平空）= (成交价 − 理论价) / 每跳；卖出（开空/平多）= (理论价 − 成交价) / 每跳。
     理论价：开仓=信号给的入场价（信号K线收盘价）；止损平仓=止损价；其他平仓没有理论价，留空。
     GUI 假定成交的滑点恒为 0，只留记录；真实滑点要看 CTP 成交回报。
"""

from __future__ import annotations

import csv
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

FILE_NAME = "slippage_log.csv"

_FIELDS = [
    "ts",
    "symbol",
    "action",
    "volume",
    "theory_price",
    "fill_price",
    "slip_ticks",
    "tick",
    "executor",
    "dry_run",
]


def _csv_path(root: Path | str) -> Path:
    return Path(root) / "data" / FILE_NAME


def record_fill(
    root: Path | str,
    *,
    symbol: str,
    action: str,
    volume: int,
    theory_price: float | None,
    fill_price: float,
    tick: float,
    side_before: str = "",
    executor: str = "",
    dry_run: bool = True,
    assumed_fill: bool = False,
) -> float | None:
    """
    追加一条成交的滑点记录。返回滑点跳数（算不出时返回 None）。

    side_before：平仓时用——平多是卖出，平空是买入。开仓不用传。
    """
    fill = float(fill_price or 0)
    if fill <= 0:
        return None
    tick = float(tick or 0)
    if tick <= 0:
        tick = 1.0
    theory = float(theory_price or 0)
    action_key = str(action or "").strip().lower()
    slip: float | None = None
    if theory > 0:
        # GUI 只有点击成功的假定成交，参考价差不能冒充真实滑点。
        if assumed_fill:
            slip = 0.0
        # 买入方向成交价越高越亏；卖出方向越低越亏。不利方向记正数。
        elif action_key == "buy_open" or (action_key == "close" and side_before == "short"):
            slip = (fill - theory) / tick
        elif action_key == "sell_open" or (action_key == "close" and side_before == "long"):
            slip = (theory - fill) / tick
    path = _csv_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(_FIELDS)
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                symbol,
                action,
                int(volume or 0),
                f"{theory:g}" if theory > 0 else "",
                f"{fill:g}",
                f"{slip:.2f}" if slip is not None else "",
                f"{tick:g}",
                executor,
                "1" if dry_run else "0",
            ]
        )
    return slip


def load_rows(root: Path | str) -> list[dict[str, str]]:
    """读出全部滑点记录。文件不存在时返回空。"""
    path = _csv_path(root)
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _percentile(values: list[float], pct: float) -> float:
    """线性插值分位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _stat_line(values: list[float]) -> tuple[int, float, float, float]:
    """(条数, 均值, 中位, 70分位)。"""
    if not values:
        return 0, 0.0, 0.0, 0.0
    return len(values), statistics.fmean(values), statistics.median(values), _percentile(values, 0.7)


def summarize(root: Path | str, *, real_only: bool = True) -> dict[str, Any]:
    """
    汇总滑点。默认只看真实成交（dry_run=0 且 executor=ctp 的行）；没有真实成交时退回全部记录。

    返回 dict：rows 用到的记录数、overall=(n,均值,中位,p70)、suggested 建议滑点跳数（p70 向上取到 0.5）。
    """
    rows = load_rows(root)
    slips: list[float] = []
    real_slips: list[float] = []
    for row in rows:
        raw = str(row.get("slip_ticks") or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        slips.append(value)
        if str(row.get("dry_run") or "1") == "0" and str(row.get("executor") or "") == "ctp":
            real_slips.append(value)
    used = real_slips if (real_only and real_slips) else slips
    n, mean, median, p70 = _stat_line(used)
    # 建议回测滑点：70 分位向上取到 0.5 跳，留一点余量。
    suggested = math.ceil(max(p70, 0.0) * 2) / 2 if n else 0.0
    return {
        "rows": len(rows),
        "real_rows": len(real_slips),
        "n": n,
        "mean": mean,
        "median": median,
        "p70": p70,
        "suggested": suggested,
        "used_real": bool(real_slips),
    }


def summarize_text(root: Path | str) -> str:
    """复盘页尾部展示的滑点校准段。"""
    info = summarize(root)
    lines = ["—— 实盘滑点校准 ——"]
    if not info["rows"]:
        lines.append("还没有成交滑点记录。真实成交（CTP）后会自动记到 data/slippage_log.csv。")
        return "\n".join(lines)
    scope = "真实成交" if info["used_real"] else "全部记录（含GUI假定成交，滑点恒为0，仅供参考）"
    lines.append(
        f"样本 {info['n']} 笔（{scope}，共 {info['rows']} 行记录）："
        f"均值 {info['mean']:+.2f} 跳，中位 {info['median']:+.2f} 跳，70分位 {info['p70']:+.2f} 跳"
    )
    if info["n"]:
        lines.append(
            f"建议回测滑点：{info['suggested']:g} 跳（按 70 分位向上取 0.5）。"
            "正数=成交价比理论价差，0 附近=基本按理论价成交。"
        )
    if not info["used_real"]:
        lines.append("目前没有 CTP 真实成交样本；GUI 假定成交得不出真实滑点。")
    return "\n".join(lines)
