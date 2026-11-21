"""
运维查询：输出当天关键指标，或按 trace_id 打印一条交易时间线。

功能：
    1. --day / 无参数：汇总订单、成交、拒单、平均延迟与告警数；
    2. --trace：把信号、风控决策、审计事件按时间拼成一条链路，便于对账。

用法：
    python tools/ops_report.py
    python tools/ops_report.py --day 2026-11-21
    python tools/ops_report.py --trace TRACE_ID
    python tools/ops_report.py --root .

只读 data/trading.db、data/order_timeline.jsonl 与 data/alerts.jsonl，不写库。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 确保项目根在路径上
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.ops.journal import metrics_snapshot, trace_events


def _print_metrics(root: Path, day: str) -> None:
    data = metrics_snapshot(day, root=root)
    title = f"{day} 运行指标" if day else "今日运行指标"
    print(title)
    print(f"  报单        {data['orders_sent']}")
    print(f"  成交        {data['fills']}")
    print(f"  拒单        {data['rejected']}（拒单率 {data['reject_rate']:.1%}）")
    print(f"  平均报单耗时 {data['avg_send_ms']} ms")
    print(f"  平均成交耗时 {data['avg_fill_ms']} ms")
    print(f"  平均滑点    {data['avg_slippage']} 跳")
    print(f"  冻结告警    {data['freeze_alerts']}")
    print(f"  对账告警    {data['reconcile_alerts']}")


def _print_trace(root: Path, trace_id: str) -> None:
    rows = trace_events(trace_id, root=root)
    if not rows:
        print(f"没有找到 trace_id={trace_id} 的记录。")
        return
    print(f"trace_id={trace_id} 共 {len(rows)} 条")
    for row in rows:
        parts = [str(row.get("ts") or ""), str(row.get("event") or "")]
        for key in ("symbol", "action", "gate", "allowed", "code", "message", "reason"):
            value = row.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        print("  " + " | ".join(parts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="输出运行指标或交易 trace 时间线")
    parser.add_argument("--root", default=str(_ROOT), help="项目根目录（默认仓库根）")
    parser.add_argument("--day", default="", help="统计日期，YYYY-MM-DD；默认当天")
    parser.add_argument("--trace", default="", help="按 trace_id 打印时间线")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if args.trace:
        _print_trace(root, args.trace)
    else:
        _print_metrics(root, args.day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
