"""A/B 对比：同一品种两组参数、或两个账户的两份回测结果。

功能：并排给出收益、回撤、夏普、胜率、交易次数；算逐日收益相关性；逐项标出谁更好。
原理：只读两份回测结果，不重跑、不改参数。价格序列不同（不同品种/账户）时，
      相关性只作参考，不做显著性检验。
"""

from __future__ import annotations

import math
from typing import Any

from src.backtest.metrics import calc_performance
from src.backtest.risk_metrics import daily_returns

# (字段, 中文名, 越大越好?)；None 表示只展示不比较。
DIMENSIONS = (
    ("net_pnl", "净盈亏", True),
    ("return_pct", "收益率", True),
    ("max_drawdown_pct", "最大回撤", False),
    ("sharpe", "夏普", True),
    ("win_rate", "胜率", True),
    ("trades", "交易次数", None),
)


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _max_drawdown_pct(equity: list[float]) -> float:
    """按权益曲线算最大回撤比例。没有成交时 calc_performance 会提前返回，所以这里单独算。"""
    peak = 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 1e-9:
            worst = max(worst, (peak - value) / peak)
    return worst


def summarize(result: Any) -> dict[str, float]:
    """一份回测结果的关键指标。"""
    equity = list(getattr(result, "equity", None) or [])
    times = list(getattr(result, "equity_times", None) or [])
    trades = list(getattr(result, "trades", None) or [])
    perf = calc_performance(trades, equity, times=times or None)
    start = _number(equity[0]) if equity else 0.0
    end = _number(equity[-1]) if equity else 0.0
    return {
        "net_pnl": perf.net_pnl,
        "return_pct": (end / start - 1.0) if start > 1e-9 else 0.0,
        "max_drawdown_pct": max(perf.max_drawdown_pct, _max_drawdown_pct(equity)),
        "sharpe": perf.sharpe,
        "win_rate": perf.win_rate,
        "trades": float(perf.trades),
        "expectancy": perf.expectancy,
    }


def _return_corr(left: Any, right: Any) -> float | None:
    a = dict(daily_returns(getattr(left, "equity", None), getattr(left, "equity_times", None)))
    b = dict(daily_returns(getattr(right, "equity", None), getattr(right, "equity_times", None)))
    days = sorted(set(a) & set(b))
    if len(days) < 3:
        return None
    xs = [a[day] for day in days]
    ys = [b[day] for day in days]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 1e-18 or vy <= 1e-18:
        return None
    return cov / math.sqrt(vx * vy)


def compare_results(left: Any, right: Any, *, names: tuple[str, str] = ("A", "B")) -> dict[str, Any]:
    """两份结果并排比。返回逐项表格、相关性、各自胜项数量和一句结论。"""
    a = summarize(left)
    b = summarize(right)
    rows: list[dict[str, Any]] = []
    wins = {names[0]: 0, names[1]: 0}
    for key, label, higher_better in DIMENSIONS:
        av, bv = a.get(key, 0.0), b.get(key, 0.0)
        winner = ""
        if higher_better is True and abs(av - bv) > 1e-12:
            winner = names[0] if av > bv else names[1]
        elif higher_better is False and abs(av - bv) > 1e-12:
            winner = names[0] if av < bv else names[1]
        if winner:
            wins[winner] += 1
        rows.append({"key": key, "label": label, "a": av, "b": bv, "winner": winner})
    corr = _return_corr(left, right)
    compared = [row for row in rows if row["key"] != "trades"]
    if wins[names[0]] == wins[names[1]]:
        winner = "持平"
    else:
        winner = names[0] if wins[names[0]] > wins[names[1]] else names[1]
    conclusion = (
        f"{names[0]} 占优 {wins[names[0]]} 项，{names[1]} 占优 {wins[names[1]]} 项"
        f"（共 {len(compared)} 项），结论：{winner}"
    )
    if corr is not None:
        conclusion += f"；逐日收益相关性 {corr:+.2f}"
    return {"a": a, "b": b, "rows": rows, "correlation": corr, "winner": winner, "conclusion": conclusion}


def format_compare_lines(report: dict[str, Any], *, names: tuple[str, str] = ("A", "B")) -> list[str]:
    """把对比结果写成中文几行。"""
    lines = [f"A/B 对比  {names[0]} vs {names[1]}"]
    formats = {
        "net_pnl": lambda value: f"{value:+,.2f}",
        "return_pct": lambda value: f"{value:+.2%}",
        "max_drawdown_pct": lambda value: f"{value:.2%}",
        "sharpe": lambda value: f"{value:.2f}",
        "win_rate": lambda value: f"{value:.2%}",
        "trades": lambda value: f"{int(value)}",
    }
    for row in report.get("rows") or []:
        key = str(row.get("key"))
        fmt = formats.get(key, lambda value: f"{_number(value):.4f}")
        mark = ""
        if row.get("winner") == names[0]:
            mark = f"  [{names[0]} 较好]"
        elif row.get("winner") == names[1]:
            mark = f"  [{names[1]} 较好]"
        lines.append(f"{row.get('label')}  {names[0]} {fmt(_number(row.get('a')))} · {names[1]} {fmt(_number(row.get('b')))}" + mark)
    lines.append("结论      " + str(report.get("conclusion") or ""))
    return lines
