"""
信号与实盘对照
功能：把「策略重放会怎么做」和「程序当天真实留下的记录 / 账户记账」放在一起比。
原理：
  复盘发现缺陷的关键一步，是找出「该做没做」「不该做却做了」「做了但价差离谱」。
  重放假设空仓起步；实盘可能早上已有仓，所以差距要用人话说明，不能直接当成程序 bug。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.account.signal_book import load_book
from src.review.quality import SignalTrade
from src.review.session import SessionWindow
from src.review.technical import as_naive_datetime


@dataclass
class GapReport:
    """信号 vs 实盘的对照结论。"""

    lines: list[str] = field(default_factory=list)
    replay_opens: int = 0
    event_orders: int = 0
    book_fills: int = 0
    # matched / signal_only / actual_only / none
    alignment: str = "none"
    notes: list[str] = field(default_factory=list)


def build_gap_report(
    root: Path | str,
    *,
    symbol: str,
    windows: list[SessionWindow],
    trades: list[SignalTrade],
    actual_event_lines: list[str],
) -> GapReport:
    """
    对照三种来源：
      1) 策略重放开仓笔数
      2) events.jsonl 里的下单/拦截
      3) 信号账户里当天该品种的开平仓
    """
    report = GapReport()
    report.replay_opens = len(trades)
    report.event_orders = _count_event_orders(actual_event_lines)
    book_rows = _book_fills_in_windows(root, symbol, windows)
    report.book_fills = len(book_rows)

    report.lines.append(
        "对照三件事：策略重放会不会开仓、事件日志有没有真下单、信号账户有没有记上这一笔。"
        "三件事对得上，说明挂机执行和策略规则一致；对不上，优先查启动常驻、风控和同花顺窗口。"
    )
    report.lines.append(
        f"重放开仓 {report.replay_opens} 笔；事件日志相关 {report.event_orders} 条；"
        f"信号账户当日成交 {report.book_fills} 笔。"
    )

    if report.replay_opens == 0 and report.event_orders == 0 and report.book_fills == 0:
        report.alignment = "none"
        report.lines.append("三边都是空的：策略观望，且当天没有这个合约的挂机下单记录。这是「一致的空仓日」。")
        return report

    if report.replay_opens > 0 and report.event_orders == 0 and report.book_fills == 0:
        report.alignment = "signal_only"
        report.lines.append(
            "策略重放会开仓，但事件日志和账户都没有记录。"
            "常见原因：当天没点「启动常驻」、dry_run 只记日志、或复盘用的参数和挂机当时不一致。"
        )
        report.notes.append("信号有、实盘无")
    elif report.replay_opens == 0 and (report.event_orders > 0 or report.book_fills > 0):
        report.alignment = "actual_only"
        report.lines.append(
            "策略按今天参数重放不会开仓，但程序当天留下了下单或记账。"
            "常见原因：早上已有老仓在管、当时参数和现在不同、或手工/模拟器下过单。"
        )
        report.notes.append("实盘有、信号无")
    else:
        report.alignment = "matched"
        report.lines.append(
            "重放有信号，程序也有记录。笔数不一定相等（风控拦截、重复扫、或一笔分几次点），"
            "请对照第五节事件明细和账户分析里的时间。"
        )
        if abs(report.replay_opens - max(report.event_orders, report.book_fills)) >= 2:
            report.lines.append(
                "笔数差得比较多：建议打开运行页事件日志，逐笔看是被风控拦了，还是同花顺没点上。"
            )
            report.notes.append("笔数偏差大")

    if book_rows:
        report.lines.append("信号账户当日明细：")
        for row in book_rows[:12]:
            report.lines.append(f"  · {row}")
        if len(book_rows) > 12:
            report.lines.append(f"  · ……另有 {len(book_rows) - 12} 笔，请到「账户分析」查看。")
    return report


def _count_event_orders(lines: list[str]) -> int:
    """从 actual_lines 已经生成的人话里粗数下单相关条数。"""
    keys = ("下单", "风控拦住", "行情太旧")
    return sum(1 for line in lines if any(k in line for k in keys) and "·" in line)


def _book_fills_in_windows(
    root: Path | str,
    symbol: str,
    windows: list[SessionWindow],
) -> list[str]:
    book = load_book(root)
    if not book:
        return []
    want = str(symbol).strip().lower()
    rows: list[str] = []
    for item in book.get("trades") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol") or "").strip().lower() != want:
            continue
        when = _parse(item.get("time") or item.get("ts"))
        if when is None or not any(w.contains(when) for w in windows):
            continue
        offset = str(item.get("offset") or "")
        direction = str(item.get("direction") or "")
        price = item.get("price")
        volume = item.get("volume")
        reason = str(item.get("reason") or "")[:40]
        rows.append(
            f"{when:%m-%d %H:%M} {offset}{direction} "
            f"价格 {price} × {volume}手"
            + (f"，{reason}" if reason else "")
        )
    return rows


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return as_naive_datetime(value)
    except Exception:  # noqa: BLE001
        return None
