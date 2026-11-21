"""
影子盘：仿真账和真实账并排看。

功能：把 CTP 仿真账、CTP 真实账各自已平仓的成交收成两列，按「同品种、同方向、同开仓日」
      粗对齐，指出仿真有而实盘没有、实盘有而仿真没有的部分。
原理：两本账本来就分开写（signal_account_ctp_sim.json / signal_account_ctp_live.json），
      这里只读、不搬账、不改任何交易参数。账本不记录策略参数，所以「同参数」仍需人工确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.account.signal_book import load_named_book, matched_closes, open_position_rows
from src.account.workspace import CTP_LIVE, CTP_SIM, display_label
from src.domain.accounts import ctp_book_ids
from src.domain.pnl_stats import summarize_pnl


@dataclass
class BookSide:
    key: str
    label: str
    trades: int = 0
    pnl: float = 0.0
    wins: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None
    open_volume: int = 0
    first: str = ""
    last: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ShadowReport:
    sim: BookSide
    live: BookSide
    groups: list[dict[str, Any]] = field(default_factory=list)

    @property
    def both_empty(self) -> bool:
        return self.sim.trades == 0 and self.live.trades == 0


def _side(root: Path, account_id: str, *, symbol: str = "") -> BookSide:
    label = display_label(account_id, root)
    book = load_named_book(root, account_id)
    side = BookSide(key=account_id, label=label)
    if not book:
        return side
    rows = matched_closes(book)
    if symbol:
        rows = [row for row in rows if str(row.get("symbol") or "") == symbol]
    side.rows = rows
    side.trades = len(rows)
    side.pnl = round(sum(float(row.get("pnl") or 0) for row in rows), 2)
    stats = summarize_pnl([float(row.get("pnl") or 0) for row in rows], zero_as_loss=True)
    side.wins = stats.wins
    side.win_rate = stats.win_rate * 100.0
    factor = stats.profit_factor
    if factor is None or factor == float("inf"):
        side.profit_factor = None  # 没有亏损样本，盈利因子没有意义，显示「—」
    else:
        side.profit_factor = round(factor, 2)
    opens = open_position_rows(book)
    if symbol:
        opens = [row for row in opens if str(row.get("symbol") or "") == symbol]
    side.open_volume = sum(int(row.get("volume") or 0) for row in opens)
    if rows:
        stamps = sorted(str(row.get("exit_time") or "") for row in rows if row.get("exit_time"))
        side.first = stamps[0][:10] if stamps else ""
        side.last = stamps[-1][:10] if stamps else ""
    return side


def _group_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("symbol") or ""),
        str(row.get("direction") or ""),
        str(row.get("entry_time") or "")[:10],
    )


def _avg_entry(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(float(row.get("entry_price") or 0) for row in rows) / len(rows)


def shadow_report(root: Path | str, *, symbol: str = "") -> ShadowReport:
    """读两本 CTP 账，返回并排汇总。文件不存在时两边都是 0，不编造。"""
    base = Path(root)
    # 注册表存在时按账号账本比对；没有时回退旧四本账。
    ids = ctp_book_ids(base)
    sim = _side(base, ids.get("sim") or CTP_SIM, symbol=symbol)
    live = _side(base, ids.get("live") or CTP_LIVE, symbol=symbol)
    sim_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    live_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in sim.rows:
        sim_groups.setdefault(_group_key(row), []).append(row)
    for row in live.rows:
        live_groups.setdefault(_group_key(row), []).append(row)
    groups: list[dict[str, Any]] = []
    for key in sorted(set(sim_groups) | set(live_groups)):
        left = sim_groups.get(key, [])
        right = live_groups.get(key, [])
        groups.append(
            {
                "symbol": key[0],
                "direction": key[1],
                "day": key[2],
                "sim_n": len(left),
                "live_n": len(right),
                "entry_gap": round(_avg_entry(right) - _avg_entry(left), 4) if left and right else None,
                "pnl_gap": round(
                    sum(float(row.get("pnl") or 0) for row in right)
                    - sum(float(row.get("pnl") or 0) for row in left),
                    2,
                )
                if left and right
                else None,
            }
        )
    return ShadowReport(sim=sim, live=live, groups=groups)


def _side_line(side: BookSide) -> str:
    pf = "—" if side.profit_factor is None else f"{side.profit_factor:.2f}"
    return (
        f"  {side.label}  平仓 {side.trades} 笔，盈亏 {side.pnl:+.2f}，"
        f"胜率 {side.win_rate:.0f}%，盈利因子 {pf}，未平 {side.open_volume} 手"
    )


def format_shadow_lines(report: ShadowReport) -> list[str]:
    """复盘里「影子盘」一节的正文。"""
    lines = [_side_line(report.sim), _side_line(report.live)]
    if report.both_empty:
        lines.append("  两边都还没有已平仓成交：先在 CTP 仿真用同一份配置跑一段，再和实盘并排看。")
        lines.append("  账本不记录策略参数；切换账户前请确认配置没有改动，否则两边不是同一套规则。")
        return lines

    paired = [row for row in report.groups if row["sim_n"] and row["live_n"]]
    sim_only = sum(row["sim_n"] for row in report.groups if row["sim_n"] and not row["live_n"])
    live_only = sum(row["live_n"] for row in report.groups if row["live_n"] and not row["sim_n"])
    lines.append(
        f"  逐笔对齐  同日同向：对齐 {len(paired)} 组 / 仿真独有 {sim_only} 笔 / 实盘独有 {live_only} 笔"
    )
    for row in paired[:6]:
        lines.append(
            f"    {row['day']} {row['symbol']} {row['direction']}  "
            f"仿真 {row['sim_n']} 笔 / 实盘 {row['live_n']} 笔，"
            f"开仓均价差 {float(row['entry_gap'] or 0):+.2f}，盈亏差 {float(row['pnl_gap'] or 0):+.2f}"
        )
    if len(paired) > 6:
        lines.append(f"    …还有 {len(paired) - 6} 组")
    if report.sim.trades < 3 or report.live.trades < 3:
        lines.append("  样本偏少，差异先当线索看，不足以判断策略好坏。")
    if not paired and (report.sim.trades or report.live.trades):
        lines.append("  两本账没有同一天同方向的平仓，暂不能逐笔对齐；上面只看各自汇总。")
    lines.append("  账本不记录策略参数；切换账户前请确认配置没有改动，否则两边不是同一套规则。")
    return lines
