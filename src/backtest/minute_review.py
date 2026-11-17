"""
P2.2 分钟级执行复核。

功能：用 1 分钟 K 线只检查「这笔已经在信号周期上开出的仓，止损/出场会不会更早」，
      绝不在 1 分钟上重新找开仓点。
原理：信号仍由 5/15/30 分钟等策略周期决定；1 分钟只是把那根大 K 线内部拆开，
      看影线或盘中是否先碰到止损。没有 1 分钟数据时，沿用原来的保守成交，
      并标明 evidence_level=conservative_no_1m，不能假装已经精确复盘。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.domain.trade import TradeRecord
from src.strategy.types import stop_triggered


EVIDENCE_MINUTE = "minute"
EVIDENCE_NONE = "conservative_no_1m"


@dataclass
class MinuteTradeReview:
    """一笔原回测成交，对照 1 分钟后的结论。"""

    symbol: str
    side: str
    entry_time: str
    original_exit_time: str
    original_exit_price: float
    original_reason: str
    stop_price: float | None
    reviewed_exit_time: str = ""
    reviewed_exit_price: float | None = None
    earlier_stop: bool = False
    evidence_level: str = EVIDENCE_NONE
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MinuteReviewReport:
    """一批成交的分钟复核摘要。"""

    evidence_level: str = EVIDENCE_NONE
    trades: int = 0
    earlier_stops: int = 0
    skipped_no_stop: int = 0
    items: list[MinuteTradeReview] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        return payload


def trades_from_backtest_file(path: Path | str) -> list[TradeRecord]:
    """读最近一次历史回测报告里的成交。没有文件或没有成交时返回空列表。"""
    file = Path(path)
    if not file.exists():
        return []
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("trades") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[TradeRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(
                TradeRecord(
                    symbol=str(row.get("symbol") or ""),
                    side=str(row.get("side") or ""),
                    entry_time=str(row.get("entry_time") or ""),
                    exit_time=str(row.get("exit_time") or ""),
                    entry_price=float(row.get("entry_price") or 0),
                    exit_price=float(row.get("exit_price") or 0),
                    volume=int(row.get("volume") or 0),
                    pnl=float(row.get("pnl") or 0),
                    reason_entry=str(row.get("reason_entry") or ""),
                    reason_exit=str(row.get("reason_exit") or ""),
                    commission=float(row.get("commission") or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def format_minute_review(report: MinuteReviewReport) -> str:
    """给回测页看的人话。"""
    level = "有1分钟" if report.evidence_level == EVIDENCE_MINUTE else "没有1分钟，沿用原结果"
    lines = [
        "—— 1 分钟复核 ——",
        level,
        f"成交 {report.trades} 笔，其中更早碰到止损 {report.earlier_stops} 笔。",
        "1 分钟只检查已经开出的仓，不会自己找开仓点。",
    ]
    lines.extend(report.notes)
    for item in report.items:
        if item.earlier_stop:
            lines.append(
                f"  {item.symbol} {item.side} 开仓 {item.entry_time}："
                f"原出场 {item.original_exit_time} → 1分钟 {item.reviewed_exit_time}"
                f"（止损 {item.stop_price}）"
            )
    if report.trades and report.earlier_stops == 0 and report.evidence_level == EVIDENCE_MINUTE:
        lines.append("这些成交在 1 分钟上没有更早碰到止损。")
    return "\n".join(lines)


def infer_stop_price(trade: TradeRecord, override: float | None = None) -> float | None:
    """优先用调用方给的止损价；否则只有出场原因写了止损，才把成交价当止损位。"""
    if override is not None and float(override) > 0:
        return float(override)
    reason = str(getattr(trade, "reason_exit", "") or "")
    if "止损" in reason:
        price = float(getattr(trade, "exit_price", 0) or 0)
        return price if price > 0 else None
    return None


def load_one_minute_bars(
    cfg: dict[str, Any],
    symbol: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame | None:
    """
    优先读 tick 合成的 1 分钟；没有再按回测页当前行情源拉 1 分钟。
    两处都没有时返回 None，复核会标明 conservative_no_1m。
    """
    from src.data.kline import KlineProvider

    attempts: list[dict[str, Any]] = []
    tick_cfg = dict(cfg)
    tick_data = dict(tick_cfg.get("data") or {})
    tick_data["source"] = "tick"
    tick_cfg["data"] = tick_data
    attempts.append(tick_cfg)
    attempts.append(cfg)
    for local in attempts:
        try:
            frame = KlineProvider(local).fetch(
                symbol,
                period="1",
                keep_history=True,
                start=start or None,
                end=end or None,
            )
        except Exception:  # noqa: BLE001
            continue
        if frame is not None and len(frame) > 0:
            return frame
    return None


def review_trades_on_minutes(
    trades: list[TradeRecord],
    minutes: pd.DataFrame | None,
    *,
    stop_mode: str = "touch",
    stop_prices: list[float | None] | None = None,
) -> MinuteReviewReport:
    """
    对照 1 分钟 K 线复核已有成交。

    不会扫描 1 分钟去产生 BUY_OPEN / SELL_OPEN。
    没有 1 分钟时返回 conservative_no_1m，原出场时间价格原样保留。
    """
    report = MinuteReviewReport(trades=len(trades))
    if minutes is None or len(minutes) == 0:
        report.evidence_level = EVIDENCE_NONE
        report.notes.append("没有 1 分钟 K 线，仍按信号周期成交，不能声称已经按盘中精确复盘。")
        for trade in trades:
            report.items.append(_passthrough(trade, EVIDENCE_NONE, "缺少 1 分钟数据"))
        return report

    report.evidence_level = EVIDENCE_MINUTE
    report.notes.append("只用 1 分钟检查已有仓的止损/出场，没有在 1 分钟上寻找新的开仓。")
    frame = minutes.sort_index()
    for index, trade in enumerate(trades):
        stop = infer_stop_price(
            trade,
            stop_prices[index] if stop_prices is not None and index < len(stop_prices) else None,
        )
        item = _review_one(trade, frame, stop_mode=stop_mode, stop_price=stop)
        report.items.append(item)
        if item.earlier_stop:
            report.earlier_stops += 1
        if item.stop_price is None:
            report.skipped_no_stop += 1
    return report


def _passthrough(trade: TradeRecord, evidence: str, note: str) -> MinuteTradeReview:
    return MinuteTradeReview(
        symbol=str(trade.symbol),
        side=str(trade.side),
        entry_time=str(trade.entry_time),
        original_exit_time=str(trade.exit_time),
        original_exit_price=float(trade.exit_price),
        original_reason=str(trade.reason_exit or ""),
        stop_price=infer_stop_price(trade),
        reviewed_exit_time=str(trade.exit_time),
        reviewed_exit_price=float(trade.exit_price),
        earlier_stop=False,
        evidence_level=evidence,
        note=note,
    )


def _review_one(
    trade: TradeRecord,
    minutes: pd.DataFrame,
    *,
    stop_mode: str,
    stop_price: float | None,
) -> MinuteTradeReview:
    item = _passthrough(trade, EVIDENCE_MINUTE, "1 分钟未更早碰到止损，沿用原出场")
    item.stop_price = stop_price
    if stop_price is None or float(stop_price) <= 0:
        item.note = "这笔没有可用止损价，1 分钟不改写出场（不在分钟上另找开仓）"
        return item
    entry = _as_ts(trade.entry_time)
    original_exit = _as_ts(trade.exit_time)
    if entry is None or original_exit is None:
        item.note = "开平时间无法解析，沿用原出场"
        return item
    # 只看开仓之后、原出场当时及之前的 1 分钟，避免偷看更后面的行情。
    window = minutes[(minutes.index > entry) & (minutes.index <= original_exit)]
    for stamp, row in window.iterrows():
        high = float(row.get("high", row.get("close", 0)) or 0)
        low = float(row.get("low", row.get("close", 0)) or 0)
        close = float(row.get("close", 0) or 0)
        if not stop_triggered(
            side=str(trade.side),
            stop=float(stop_price),
            close=close,
            high=high,
            low=low,
            mode=stop_mode,
        ):
            continue
        item.earlier_stop = pd.Timestamp(stamp) < pd.Timestamp(original_exit)
        item.reviewed_exit_time = str(pd.Timestamp(stamp))
        item.reviewed_exit_price = float(stop_price)
        item.note = (
            "1 分钟更早碰到止损，按止损价提前出场"
            if item.earlier_stop
            else "1 分钟也是在原出场时刻碰到止损"
        )
        return item
    return item


def _as_ts(value: object) -> pd.Timestamp | None:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp
