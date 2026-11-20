"""
今日一致性：回测镜像闸门、1 分钟复核、实盘真实闸门，合成一页。

功能：回答「同一笔开仓，回测会不会放弃、实盘为什么没开、1 分钟会不会更早止损」。
原理：
  - 回测镜像：读最近一次回测的 mirror_gate_hits，先核对策略 / 品种 / 周期口径。
  - 1 分钟复核：只检查当天重放已经开出的仓，不在分钟线上另找开仓点。
  - 实盘闸门：读 data/events.jsonl 里落在复盘时段、且品种对得上的拦截事件。
只写报告，不改任何开平仓规则。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.backtest.live_gates import format_mirror_gate_lines
from src.backtest.minute_review import MinuteReviewReport, review_trades_on_minutes
from src.domain.trade import TradeRecord
from src.review.session import SessionWindow
from src.review.technical import as_naive_datetime

# 实盘事件名 → 复盘里的一句话。只列会被当成「闸门」的事件，普通扫描不进这一页。
_GATE_EVENTS = {
    "stale_block_open": "过期K",
    "slip_block_open": "追价",
    "risk_block": "风控拦截",
    "strategy_health": "健康停开",
    "order_no_price": "无价格",
    "symbol_frozen": "品种冻结",
    "order_fail": "下单失败",
}


def build_consistency_lines(
    root: Path | str,
    *,
    symbol: str,
    windows: list[SessionWindow],
    cfg: dict[str, Any] | None = None,
    trades: list[Any] | None = None,
    minutes: pd.DataFrame | None = None,
) -> list[str]:
    """拼出「今日一致性」一节。任何一块缺数据都写人话原因，不猜。"""
    base = Path(root)
    out = [f"—— 今日一致性（{symbol}） ——"]
    mirror_rows, enabled, note, compared = _mirror_snapshot(base, symbol, cfg)
    out.extend(note)
    same_window = [row for row in mirror_rows if _hit_in_windows(row, windows)] if enabled else []
    if enabled:
        mirror_lines = format_mirror_gate_lines(same_window, enabled=True)
        if mirror_lines:
            # 同一份摘要文案在回测页叫「实盘镜像」，这里是回测侧，改叫「回测镜像」免得读反。
            # 只拿落在本次复盘时段里的命中比，避免「全周期累计」对「今日一次」。
            mirror_lines[0] = mirror_lines[0].replace("实盘镜像", "回测镜像（同窗口）", 1)
        out.extend("  " + row for row in mirror_lines)
        if len(same_window) != len(mirror_rows):
            out.append(
                f"  回测镜像  全周期累计放弃 {len(mirror_rows)} 次；"
                f"今日只对比同窗口 {len(same_window)} 次。"
            )

    out.extend(_minute_lines(trades or [], minutes))
    gate_lines, gate_total = _live_gate_lines(base, symbol, windows)
    out.extend(gate_lines)
    out.append(_verdict(same_window, len(mirror_rows), enabled, gate_total, compared))
    return out


def _mirror_snapshot(
    root: Path,
    symbol: str,
    cfg: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], bool, list[str], bool]:
    """最近回测的镜像命中、是否同口径、人话说明。"""
    path = root / "data" / "backtest_last.json"
    if not path.exists():
        return [], False, ["  回测镜像  还没有最近一次回测，先跑一次再比。"], False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], False, ["  回测镜像  最近回测文件读不出来，先重跑一次。"], False
    if not isinstance(data, dict):
        return [], False, ["  回测镜像  最近回测文件格式不对，先重跑一次。"], False

    snap = data.get("params_snapshot") or {}
    snap = snap if isinstance(snap, dict) else {}
    strategy = (cfg or {}).get("strategy") if isinstance(cfg, dict) else {}
    strategy = strategy if isinstance(strategy, dict) else {}
    mismatch = _scope_mismatch(
        symbol=symbol,
        engine=str(strategy.get("engine") or ""),
        timeframe=str(strategy.get("timeframe") or ""),
        bt_symbol=str(data.get("symbol") or ""),
        bt_engine=str(snap.get("engine") or ""),
        bt_timeframe=str(data.get("timeframe") or ""),
    )
    if mismatch:
        return [], False, [f"  回测镜像  未对照：最近回测是 {mismatch}，不是今天这套配置。"], False
    rows = snap.get("mirror_gate_hits") or []
    rows = [row for row in rows if isinstance(row, dict)]
    return rows, bool(snap.get("mirror_live_gates")), [], True


def _scope_mismatch(
    *,
    symbol: str,
    engine: str,
    timeframe: str,
    bt_symbol: str,
    bt_engine: str,
    bt_timeframe: str,
) -> str:
    """记录到的口径和当前不一致时，返回一句「回测是什么」。没记录的部分不比较。"""
    if bt_symbol and symbol and bt_symbol.strip().lower() != symbol.strip().lower():
        return f"{bt_symbol} / {bt_engine or '—'} / {_tf(bt_timeframe)} 分钟"
    if bt_engine and engine and bt_engine.strip().lower() != engine.strip().lower():
        return f"{bt_symbol or '—'} / {bt_engine} / {_tf(bt_timeframe)} 分钟"
    if bt_timeframe and timeframe and _tf(bt_timeframe) != _tf(timeframe):
        return f"{bt_symbol or '—'} / {bt_engine or '—'} / {_tf(bt_timeframe)} 分钟"
    return ""


def _tf(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits or str(value or "")


def _minute_lines(trades: list[Any], minutes: pd.DataFrame | None) -> list[str]:
    """1 分钟只检查已有仓的出场，不重新找开仓点。"""
    rows = _trade_records(trades)
    if not rows:
        return ["  1分钟复核  当天重放没有可复核的已平仓成交。"]
    stops = [getattr(item, "stop_price", None) for item in trades if getattr(item, "exit_time", None)]
    if minutes is None or len(minutes) == 0:
        return ["  1分钟复核  没取到 1 分钟K线，沿用信号周期结果，不能当成盘中精确复盘。"]
    report = review_trades_on_minutes(
        rows,
        minutes,
        stop_prices=[float(x) if x else None for x in stops],
    )
    return _minute_summary(report)


def _trade_records(trades: list[Any]) -> list[TradeRecord]:
    """把复盘信号换成成交结构。未平仓的跳过，避免凭空编一个出场价。"""
    out: list[TradeRecord] = []
    for item in trades:
        exit_at = getattr(item, "exit_time", None)
        if exit_at is None:
            continue
        side = str(getattr(item, "side", "") or "")
        out.append(
            TradeRecord(
                symbol="",
                side="long" if side == "long" else "short",
                entry_time=_stamp(getattr(item, "entry_time", None)),
                exit_time=_stamp(exit_at),
                entry_price=float(getattr(item, "entry_price", 0) or 0),
                exit_price=float(getattr(item, "exit_price", 0) or getattr(item, "entry_price", 0) or 0),
                volume=1,
                pnl=float(getattr(item, "pnl_points", 0) or 0),
                reason_entry=str(getattr(item, "reason_entry", "") or ""),
                reason_exit=str(getattr(item, "reason_exit", "") or ""),
                commission=0.0,
            )
        )
    return out


def _minute_summary(report: MinuteReviewReport) -> list[str]:
    lines = [f"  1分钟复核  成交 {report.trades} 笔，其中更早碰到止损 {report.earlier_stops} 笔。"]
    shown = 0
    for item in report.items:
        if not item.earlier_stop:
            continue
        lines.append(
            f"    · {_short(item.entry_time)} {item.side} 原出场 {_short(item.original_exit_time)} → "
            f"1分钟 {_short(item.reviewed_exit_time)}（止损 {item.stop_price}）"
        )
        shown += 1
        if shown >= 3:
            break
    return lines


def _live_gate_lines(
    root: Path,
    symbol: str,
    windows: list[SessionWindow],
) -> tuple[list[str], int]:
    """统计实盘真实闸门命中。没有日志时明确说没挂机或没被拦。"""
    path = root / "data" / "events.jsonl"
    if not path.exists():
        return ["  实盘闸门  还没有事件日志，通常是这段时间没有点过「启动常驻」。"], 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"  实盘闸门  事件日志打不开：{exc}"], 0

    want = str(symbol or "").strip().lower()
    counts: dict[str, int] = {}
    samples: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(item.get("event") or "")
        label = _GATE_EVENTS.get(event)
        if label is None:
            continue
        when = _parse_ts(item.get("ts"))
        if when is None or not any(window.contains(when) for window in windows):
            continue
        ev_symbol = str(item.get("symbol") or "").strip().lower()
        if ev_symbol and ev_symbol != want:
            continue
        counts[label] = counts.get(label, 0) + 1
        if len(samples) < 4:
            detail = str(item.get("reason") or item.get("message") or "")
            extra = f"：{detail}" if detail else ""
            samples.append(f"    · {when:%m-%d %H:%M} {label}{extra}")

    if not counts:
        return ["  实盘闸门  这段时间没有被拦下的开仓事件。"], 0
    total = sum(counts.values())
    parts = " / ".join(f"{label} {n} 次" for label, n in counts.items())
    return [f"  实盘闸门  共 {total} 次（{parts}）", *samples], total


def _hit_in_windows(row: dict[str, Any], windows: list[SessionWindow]) -> bool:
    """这条回测镜像命中是否落在本次复盘时段内。没有时间的旧记录不算进今日对比。"""
    when = _parse_ts(row.get("time") or row.get("bar_id"))
    return bool(when and any(window.contains(when) for window in windows))


def _verdict(
    rows: list[dict[str, Any]],
    total: int,
    enabled: bool,
    live_total: int,
    compared: bool,
) -> str:
    """
    一句话比较同窗口的回测镜像和实盘闸门。

    total 是回测全周期累计，只作背景；绝不和今日的 live_total 直接相减或下结论。
    """
    if not compared:
        return "  结论  最近回测和今天不是同一口径，这一节先只看实盘闸门；跑一次同配置回测再比。"
    if not enabled:
        return "  结论  最近回测没开镜像闸门，今天先用实盘闸门明细看；下次回测勾上「实盘镜像」再比。"
    if not rows and not live_total:
        if total:
            return (
                f"  结论  同一时间窗内两边都没有放弃记录；回测全周期累计 {total} 次，"
                "不与今日互比。"
            )
        return "  结论  回测镜像和实盘闸门都没有放弃记录，两边口径暂时一致。"
    if not rows:
        return (
            f"  结论  回测今天这个窗口没有放弃记录（全周期累计 {total} 次，不与今日互比），"
            f"实盘拦下 {live_total} 次；先看实盘当时的分笔和挂机时间。"
        )
    if not live_total:
        return (
            f"  结论  同一时间窗内回测镜像放弃 {len(rows)} 次，实盘一次也没拦；"
            "先看实盘当时有没有挂机。"
        )
    return (
        f"  结论  同一时间窗内回测镜像放弃 {len(rows)} 次，实盘拦下 {live_total} 次；"
        "差得多的那类，先查当时的分笔和挂机时间，再决定调不调闸门。"
    )


def _short(value: Any) -> str:
    """报告里统一成「2026-11-20 10:10」，避免 T 和空格两种写法混排。"""
    return str(value or "").replace("T", " ")[:16]


def _stamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return as_naive_datetime(value)
    except Exception:  # noqa: BLE001
        return None
