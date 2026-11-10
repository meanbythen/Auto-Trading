"""
策略健康分数（对照最近一次历史回测）。

功能：拿账本里最近若干笔已平仓，和 data/backtest_last.json 的胜率、盈利因子比一比。
     差太多只记告警、给运行台看一行字，默认不拦开仓。
原理：蓝图第一期「人能盯住」——衰减要看见，不要程序自己换策略或停手。
     样本不够时不评分，避免刚开始几笔就把人吓到。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOOKBACK = 20
MIN_TRADES = 10
WINRATE_DROP = 0.20
PF_RATIO = 0.50


@dataclass
class HealthReport:
    """一次健康检查的结果。status: skip=样本不够，ok=还行，watch=该看一眼。"""

    status: str
    message: str
    live_n: int = 0
    live_win_rate: float | None = None
    live_pf: float | None = None
    bt_n: int = 0
    bt_win_rate: float | None = None
    bt_pf: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "live_n": self.live_n,
            "live_win_rate": self.live_win_rate,
            "live_pf": self.live_pf,
            "bt_n": self.bt_n,
            "bt_win_rate": self.bt_win_rate,
            "bt_pf": self.bt_pf,
        }


def _profit_factor(pnls: list[float]) -> float:
    """赚到的钱除以亏掉的钱。没亏过时，有盈利记一个很大的数，方便比较。"""
    gain = sum(x for x in pnls if x > 0)
    loss = abs(sum(x for x in pnls if x < 0))
    if loss <= 1e-9:
        return 99.0 if gain > 0 else 0.0
    return gain / loss


def _win_rate(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    return sum(1 for x in pnls if x > 0) / len(pnls)


def _live_pnls(root: Path, lookback: int) -> list[float]:
    from src.account.signal_book import load_book, matched_closes

    book = load_book(root)
    if not book:
        return []
    rows = matched_closes(book)
    pnls = [float(row.get("pnl") or 0.0) for row in rows]
    if lookback > 0:
        pnls = pnls[-lookback:]
    return pnls


def _backtest_perf(root: Path) -> dict[str, Any]:
    path = root / "data" / "backtest_last.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    perf = data.get("performance") or {}
    return perf if isinstance(perf, dict) else {}


def evaluate_health(
    root: Path | str,
    *,
    lookback: int = LOOKBACK,
    min_trades: int = MIN_TRADES,
    winrate_drop: float = WINRATE_DROP,
    pf_ratio: float = PF_RATIO,
) -> HealthReport:
    """
    比较近 N 笔实盘账本和最近一次历史回测。

    不改开平仓。样本不够返回 skip。
    """
    base = Path(root)
    live = _live_pnls(base, lookback)
    perf = _backtest_perf(base)
    try:
        bt_n = int(perf.get("trades") or 0)
    except (TypeError, ValueError):
        bt_n = 0
    try:
        bt_wr = float(perf.get("win_rate"))
    except (TypeError, ValueError):
        bt_wr = None
    try:
        bt_pf = float(perf.get("profit_factor"))
    except (TypeError, ValueError):
        bt_pf = None

    if len(live) < min_trades:
        return HealthReport(
            status="skip",
            message=f"策略健康：账本已平仓 {len(live)} 笔，满 {min_trades} 笔才和回测对照",
            live_n=len(live),
            bt_n=bt_n,
            bt_win_rate=bt_wr,
            bt_pf=bt_pf,
        )
    if bt_n < min_trades or bt_wr is None:
        return HealthReport(
            status="skip",
            message="策略健康：还没有一次成交够数的历史回测，先跑一回再对照",
            live_n=len(live),
            live_win_rate=_win_rate(live),
            live_pf=_profit_factor(live),
            bt_n=bt_n,
            bt_win_rate=bt_wr,
            bt_pf=bt_pf,
        )

    live_wr = _win_rate(live)
    live_pf = _profit_factor(live)
    reasons: list[str] = []
    if live_wr + 1e-9 < bt_wr - winrate_drop:
        reasons.append(
            f"近{len(live)}笔胜率 {live_wr:.0%} ，最近回测 {bt_wr:.0%} ，低了超过 {winrate_drop:.0%}"
        )
    if bt_pf is not None and bt_pf > 0 and live_pf + 1e-9 < bt_pf * pf_ratio:
        reasons.append(
            f"近{len(live)}笔盈利因子 {live_pf:.2f} ，最近回测 {bt_pf:.2f} ，不到一半"
        )
    if reasons:
        msg = "策略健康偏差（只提醒，不停手）：" + "；".join(reasons)
        return HealthReport(
            status="watch",
            message=msg,
            live_n=len(live),
            live_win_rate=live_wr,
            live_pf=live_pf,
            bt_n=bt_n,
            bt_win_rate=bt_wr,
            bt_pf=bt_pf,
        )
    return HealthReport(
        status="ok",
        message=(
            f"策略健康正常：近{len(live)}笔胜率 {live_wr:.0%} / 盈利因子 {live_pf:.2f}，"
            f"回测 {bt_wr:.0%} / {bt_pf if bt_pf is not None else 0:.2f}"
        ),
        live_n=len(live),
        live_win_rate=live_wr,
        live_pf=live_pf,
        bt_n=bt_n,
        bt_win_rate=bt_wr,
        bt_pf=bt_pf,
    )


def maybe_emit_health(root: Path | str) -> HealthReport:
    """评分后，偏差时写入告警文件。十分钟冷却在 emit_alert 里。"""
    report = evaluate_health(root)
    if report.status == "watch":
        from src.ops.alerts import emit_alert

        emit_alert("strategy_health", report.message, level="warning", root=root)
    return report
