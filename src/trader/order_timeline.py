"""订单时间线：只记可观测事件，不把点击或模拟成交伪装成柜台回报。"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


FILE_NAME = "order_timeline.jsonl"
MAX_BYTES = 20 * 1024 * 1024
_WRITE_LOCK = threading.RLock()


def new_trace_id() -> str:
    """本地关联号；CTP 柜台号仍单独使用 order_ref/order_sys_id。"""
    return f"OT-{uuid.uuid4().hex[:16]}"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def elapsed_ms(started_at: object, ended_at: object) -> float | None:
    """两个本机接收时刻之间的耗时；柜台只有时分秒时不拿来猜跨日。"""
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(str(started_at))
        end = datetime.fromisoformat(str(ended_at))
        return round((end - start).total_seconds() * 1000.0, 3)
    except (TypeError, ValueError):
        return None


class OrderTimeline:
    """追加写时间线。root 为空时只留内存，避免测试替身污染真实工作区。"""

    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if str(root or "").strip() else None
        self.path = self.root / "data" / FILE_NAME if self.root is not None else None
        self.rows: list[dict[str, Any]] = []

    def record(self, stage: str, *, trace_id: str = "", **fields: Any) -> dict[str, Any]:
        row = {
            "ts": now_iso(),
            "trace_id": str(trace_id or "") or new_trace_id(),
            "stage": str(stage),
            **fields,
        }
        self.rows.append(row)
        if len(self.rows) > 2000:
            self.rows = self.rows[-1000:]
        if self.path is None:
            return row
        try:
            with _WRITE_LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= MAX_BYTES:
                    backup = self.path.with_name(self.path.name + ".1")
                    backup.unlink(missing_ok=True)
                    self.path.replace(backup)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            # 取证日志写坏不能拦住平仓保护；内存里仍保留这一条。
            logger.warning("订单时间线写盘失败: {}", exc)
        return row


def load_rows(root: Path | str, trace_id: str = "") -> list[dict[str, Any]]:
    """读取当前和上一段轮转日志；坏行不影响其它订单复盘。"""
    folder = Path(root) / "data"
    rows: list[dict[str, Any]] = []
    wanted = str(trace_id or "").strip()
    for path in (folder / (FILE_NAME + ".1"), folder / FILE_NAME):
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            if wanted and str(row.get("trace_id") or "") != wanted:
                continue
            rows.append(row)
    rows.sort(key=lambda item: str(item.get("ts") or ""))
    return rows


def format_recent_text(root: Path | str, *, limit: int = 10) -> str:
    """复盘页的简洁时间线视图；按本地关联号把一笔订单串起来。"""
    rows = load_rows(root)
    if not rows:
        return "—— 订单时间线 ——\n还没有订单时间线记录。"
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("trace_id") or ""), []).append(row)
    names = {
        "market_snapshot": "行情快照",
        "bar_closed": "K线完成",
        "strategy_decision": "策略决策",
        "risk_intent": "风控意图",
        "preflight": "通道前置",
        "order_sent": "委托发送",
        "channel_result": "通道返回",
        "broker_report": "柜台回报",
        "trade": "柜台成交",
        "assumed_fill": "GUI假定成交",
        "simulated_fill": "纯模拟成交",
        "accounted": "程序入账",
    }
    lines = ["—— 订单时间线（最近订单）——"]
    selected = list(grouped.items())[-max(1, int(limit)) :]
    for trace_id, items in selected:
        first = items[0]
        lines.append(
            f"{trace_id} | {first.get('symbol') or ''} {first.get('side') or ''} "
            f"{first.get('volume') or ''}手"
        )
        for row in items:
            price = row.get("fill_price", row.get("order_price", row.get("latest_price", "")))
            stage_ms = row.get("stage_ms")
            latency = f" +{float(stage_ms):.1f}ms" if isinstance(stage_ms, (int, float)) else ""
            extra = f" 价{price}" if price not in (None, "") else ""
            order_ref = str(row.get("order_ref") or "")
            order_sys_id = str(row.get("order_sys_id") or "").strip()
            trade_id = str(row.get("trade_id") or "")
            slip_ticks = row.get("slip_ticks")
            extra += f" 报单{order_ref}" if order_ref else ""
            extra += f" 柜台号{order_sys_id}" if order_sys_id else ""
            extra += f" 成交号{trade_id}" if trade_id else ""
            extra += f" 滑点{float(slip_ticks):+.2f}跳" if isinstance(slip_ticks, (int, float)) else ""
            lines.append(
                f"  {str(row.get('ts') or '')} {names.get(str(row.get('stage')), row.get('stage'))}"
                f"{latency}{extra}"
            )
    return "\n".join(lines)
