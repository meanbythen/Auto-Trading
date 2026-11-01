"""
程序当天真实留下的记录
功能：和「策略重放」分开。重放是事后用K线推演；这里看挂机时有没有真的写下单日志。
原理：读 data/events.jsonl，只留落在复盘时段里、并且品种对得上的事件。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.review.session import SessionWindow
from src.review.technical import as_naive_datetime

# 事件名 → 给人看的一句。没列出来的事件不进复盘，避免心跳刷屏。
_EVENT_TEXT = {
    "order": "程序记录了一次下单",
    "order_sent_unconfirmed": "程序点过下单，但没有当成成交（需要你去同花顺核对）",
    "order_fail": "程序尝试下单但失败",
    "order_no_price": "想开仓但没有价格，被拒绝",
    "risk_block": "策略想做，被风控拦住",
    "stale_block_open": "行情太旧，禁止开仓",
    "emergency_flatten": "按过紧急清仓",
}


def actual_lines(
    root: Path | str,
    symbol: str,
    windows: list[SessionWindow],
) -> list[str]:
    """
    列出这段时间里和本合约有关的真实程序事件。

    没有日志时直接说明「没挂机或没写下单」，不要假装策略没触发。
    """
    path = Path(root) / "data" / "events.jsonl"
    if not path.exists():
        return ["还没有事件日志。通常是这段时间没有点过「启动常驻」，所以程序没有下过单。"]

    want = str(symbol).strip().lower()
    rows: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"事件日志打不开：{e}"]

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(item.get("event") or "")
        if event not in _EVENT_TEXT:
            continue
        when = _parse_ts(item.get("ts"))
        if when is None or not any(w.contains(when) for w in windows):
            continue
        ev_symbol = str(item.get("symbol") or "").strip().lower()
        if ev_symbol and ev_symbol != want:
            continue
        detail = item.get("reason") or item.get("message") or item.get("side") or ""
        extra = f"：{detail}" if detail else ""
        rows.append(f"  · {when:%m-%d %H:%M} {_EVENT_TEXT[event]}{extra}")

    if not rows:
        return [
            "这段时间的事件日志里，没有这个合约的下单、失败或风控拦截。"
            "可能没挂机，也可能策略当时就是观望、所以没有发单。"
        ]
    return [f"找到 {len(rows)} 条（只显示下单和被拦住的，不含普通扫描）：", *rows[:30]]


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return as_naive_datetime(value)
    except Exception:  # noqa: BLE001
        return None
