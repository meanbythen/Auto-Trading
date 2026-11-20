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
import json
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


def product_of(symbol: str) -> str:
    """fu2701 → fu，SA2701 → sa。分品种看滑点时不必区分具体月份。"""
    text = str(symbol or "").strip()
    letters = "".join(ch for ch in text if ch.isalpha())
    return letters.lower() or text


def _slip_value(row: dict[str, Any]) -> float | None:
    try:
        return float(str(row.get("slip_ticks") or "").strip())
    except ValueError:
        return None


def _is_real(row: dict[str, Any]) -> bool:
    return str(row.get("dry_run") or "1") == "0" and str(row.get("executor") or "") == "ctp"


def _group_stats(rows: list[dict[str, Any]], key_of) -> list[dict[str, Any]]:
    """按某个维度分组，优先真实成交；样本 < 3 的不进结论。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = _slip_value(row)
        if value is None:
            continue
        key = str(key_of(row) or "").strip()
        if not key:
            continue
        groups.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        real = [row for row in items if _is_real(row)]
        used = real or items
        values = [value for row in used if (value := _slip_value(row)) is not None]
        if len(values) < 3:
            continue
        n, _mean, median, p70 = _stat_line(values)
        out.append(
            {
                "key": key,
                "n": n,
                "median": median,
                "p70": p70,
                "suggested": math.ceil(max(p70, 0.0) * 2) / 2,
                "used_real": bool(real),
            }
        )
    out.sort(key=lambda item: (-int(item["n"]), str(item["key"])))
    return out


def summarize_by_side(root: Path | str) -> list[dict[str, Any]]:
    """按买卖方向给建议。买入和卖出的滑点来源不完全一样，不能直接平均。"""

    def key_of(row: dict[str, Any]) -> str:
        action = str(row.get("action") or "").upper()
        if action.startswith("BUY"):
            return "买"
        if action.startswith("SELL"):
            return "卖"
        return ""

    return _group_stats(load_rows(root), key_of)


def summarize_by_session(root: Path | str) -> list[dict[str, Any]]:
    """按日盘 / 夜盘给建议。夜盘流动性差时，滑点常比日盘大。"""

    def key_of(row: dict[str, Any]) -> str:
        stamp = str(row.get("ts") or "")
        try:
            hour = int(stamp[11:13])
        except (TypeError, ValueError):
            return ""
        if 9 <= hour < 15:
            return "日盘"
        if hour >= 21 or hour < 3:
            return "夜盘"
        return "其他"

    return _group_stats(load_rows(root), key_of)


# 复盘里算「拒单 / 被拦」的事件。只统计没发出去或柜台退回的，不把正常撤单算进来。
_REJECT_EVENTS = {
    "order_fail": "柜台拒单",
    "order_no_price": "没有价格",
    "risk_block": "风控拦截",
    "symbol_frozen": "品种冻结",
    "strategy_health": "健康停开",
}


def reject_summary(root: Path | str, *, days: int = 30) -> dict[str, Any]:
    """
    近 N 天没发出去或被退回的单，按品种和原因分开数。

    数据来自 data/events.jsonl；没有文件时返回空统计，调用方要如实说「没有记录」。
    """
    path = Path(root) / "data" / "events.jsonl"
    out: dict[str, Any] = {"total": 0, "by_symbol": [], "by_reason": []}
    if not path.exists():
        return out
    cutoff = datetime.now().timestamp() - max(1, int(days)) * 86400
    by_symbol: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        label = _REJECT_EVENTS.get(str(item.get("event") or ""))
        if label is None:
            continue
        when = _event_ts(item.get("ts"))
        if when is not None and when < cutoff:
            continue
        symbol = product_of(str(item.get("symbol") or "")) or "—"
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
        by_reason[label] = by_reason.get(label, 0) + 1
        out["total"] += 1
    out["by_symbol"] = sorted(
        ({"key": key, "n": n} for key, n in by_symbol.items()),
        key=lambda item: (-item["n"], item["key"]),
    )
    out["by_reason"] = sorted(
        ({"key": key, "n": n} for key, n in by_reason.items()),
        key=lambda item: (-item["n"], item["key"]),
    )
    return out


def _event_ts(value: Any) -> float | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def quality_text(root: Path | str) -> str:
    """复盘尾部「成交质量」：方向、时段、拒单三块。样本不足时直说。"""
    lines = ["—— 成交质量（滑点与拒单） ——"]
    side = summarize_by_side(root)
    if side:
        lines.append("按方向：" + "；".join(
            f"{item['key']} {item['n']}笔 → 建议 {item['suggested']:g} 跳" for item in side
        ))
    else:
        lines.append("按方向：真实样本不足 3 笔，先不改方向参数。")

    session = summarize_by_session(root)
    if session:
        lines.append("按时段：" + "；".join(
            f"{item['key']} {item['n']}笔 → 建议 {item['suggested']:g} 跳" for item in session
        ))
    else:
        lines.append("按时段：真实样本不足 3 笔，先不改夜盘参数。")

    rejects = reject_summary(root)
    if not rejects["total"]:
        lines.append("拒单：近 30 天没有记录（或还没点过「启动常驻」）。")
    else:
        reasons = "、".join(f"{item['key']} {item['n']} 次" for item in rejects["by_reason"])
        symbols = "、".join(f"{item['key']} {item['n']} 次" for item in rejects["by_symbol"][:5])
        lines.append(f"拒单：近 30 天 {rejects['total']} 次（{reasons}）")
        lines.append(f"  分品种  {symbols}")
    return "\n".join(lines)


def summarize_by_symbol(
    root: Path | str,
    *,
    real_only: bool = True,
    min_n: int = 3,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """
    按品种汇总滑点，给出各自建议跳数。

    每个品种优先只用真实成交；该品种没有真实成交时才退回全部记录。
    样本不足 min_n 的品种不参与建议，避免一笔成交就改参数。
    """
    rows = load_rows(root)
    groups: dict[str, list[float]] = {}
    real: dict[str, list[float]] = {}
    for row in rows:
        raw = str(row.get("slip_ticks") or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        key = product_of(str(row.get("symbol") or ""))
        if not key:
            continue
        groups.setdefault(key, []).append(value)
        if str(row.get("dry_run") or "1") == "0" and str(row.get("executor") or "") == "ctp":
            real.setdefault(key, []).append(value)
    out: list[dict[str, Any]] = []
    for key, values in groups.items():
        real_values = real.get(key) or []
        used = real_values if (real_only and real_values) else values
        n, _mean, median, p70 = _stat_line(used)
        if n < min_n:
            continue
        out.append(
            {
                "symbol": key,
                "n": n,
                "median": median,
                "p70": p70,
                "suggested": math.ceil(max(p70, 0.0) * 2) / 2,
                "used_real": bool(real_values),
            }
        )
    out.sort(key=lambda item: (-int(item["n"]), str(item["symbol"])))
    return out[:limit]


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
    per_symbol = summarize_by_symbol(root)
    if len(per_symbol) >= 2:
        lines.append("分品种建议（各品种 70 分位向上取 0.5 跳，满 3 笔才列）：")
        for item in per_symbol:
            suffix = "" if item["used_real"] else "（含假定成交）"
            lines.append(
                f"  {item['symbol']}  {item['n']}笔  70分位 {item['p70']:+.2f}  → "
                f"{item['suggested']:g} 跳{suffix}"
            )
    return "\n".join(lines)
