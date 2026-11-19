"""
回测里可选的「实盘镜像闸门」。

功能：默认关掉，回测成绩和以前一样。打开后，开仓前用与实盘相同的两道门
（K 线过期、追价滑点）看这一根会不会被挂机放弃，放弃就记下来、不开仓。
原理：过期 = 信号确认时刻到实际成交时刻隔得太久（隔夜开盘对昨收盘）。
      追价 = 做多现价更高、做空现价更低，超过 ATR 系数或点数上限。
      止损仍只用已收盘 K，不把影线止损塞进默认回测。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.data.periods import stale_after_minutes
from src.strategy.types import config_flag


def entry_slip_blocked(
    entry_price: float,
    live_price: float,
    atr: float,
    *,
    is_long: bool,
    limit_atr: float,
    limit_points: float = 0.0,
) -> tuple[bool, float, float]:
    """
    与挂机同一道追价门：只拦不利于持仓方向的偏移。

    做多现价更高、做空现价更低才算。数据不全时放行。
    """
    if entry_price <= 0 or live_price <= 0:
        return False, 0.0, 0.0
    limit = 0.0
    if limit_atr > 0 and atr > 0:
        limit = float(limit_atr) * float(atr)
    if limit_points > 0:
        limit = min(limit, float(limit_points)) if limit > 0 else float(limit_points)
    if limit <= 0:
        return False, 0.0, 0.0
    chase = (live_price - entry_price) if is_long else (entry_price - live_price)
    return chase > limit, chase, limit


def mirror_live_gates_enabled(cfg: dict[str, Any] | None) -> bool:
    """这次回测要不要套实盘开仓闸门。没写或写错都当关掉。"""
    bt = (cfg or {}).get("backtest") if isinstance((cfg or {}).get("backtest"), dict) else {}
    return config_flag((bt or {}).get("mirror_live_gates"), default=False)


def inspect_live_open_gate(
    *,
    enabled: bool,
    is_long: bool,
    signal_price: float,
    fill_price: float,
    signal_time: datetime | None,
    fill_time: datetime | None,
    atr: float,
    period: str,
    max_stale_minutes: float,
    limit_atr: float,
    limit_points: float,
) -> dict[str, Any] | None:
    """
    若这一笔按实盘规则不该开，返回一行对照；否则返回 None。

    先看过期，再看追价。数据不全时放行，和实盘闸门一样宁可漏拦。
    """
    if not enabled:
        return None
    try:
        signal_px = float(signal_price or 0)
        live_px = float(fill_price or 0)
    except (TypeError, ValueError):
        return None
    if signal_px <= 0 or live_px <= 0:
        return None

    stale_limit = stale_after_minutes(str(period or "15"), float(max_stale_minutes or 90))
    age_min = 0.0
    if signal_time is not None and fill_time is not None:
        age_min = (fill_time - signal_time).total_seconds() / 60.0
        if age_min < 0:
            age_min = 0.0
        if age_min > stale_limit:
            return {
                "kind": "stale_block",
                "reason": "stale_block",
                "label": "K线过期",
                "signal_price": round(signal_px, 4),
                "live_price": round(live_px, 4),
                "chase": round(age_min, 2),
                "limit": round(stale_limit, 2),
                "age_minutes": round(age_min, 2),
            }

    blocked, chase, limit = entry_slip_blocked(
        signal_px,
        live_px,
        float(atr or 0),
        is_long=bool(is_long),
        limit_atr=float(limit_atr or 0),
        limit_points=float(limit_points or 0),
    )
    if not blocked:
        return None
    return {
        "kind": "slip_block",
        "reason": "slip_block",
        "label": "开仓滑点过大",
        "signal_price": round(signal_px, 4),
        "live_price": round(live_px, 4),
        "chase": round(float(chase), 4),
        "limit": round(float(limit), 4),
        "age_minutes": round(age_min, 2),
    }


def live_gate_settings(cfg: dict[str, Any] | None) -> tuple[float, float, float]:
    """实盘那两道门的数字：ATR 系数、点数上限、过期分钟。"""
    trade = (cfg or {}).get("trade") if isinstance((cfg or {}).get("trade"), dict) else {}
    data = (cfg or {}).get("data") if isinstance((cfg or {}).get("data"), dict) else {}
    try:
        limit_atr = float((trade or {}).get("max_entry_slip_atr", 0.5) or 0)
    except (TypeError, ValueError):
        limit_atr = 0.5
    try:
        limit_points = float((trade or {}).get("max_entry_slip_points", 0) or 0)
    except (TypeError, ValueError):
        limit_points = 0.0
    try:
        stale = float((data or {}).get("max_stale_minutes", 90) or 90)
    except (TypeError, ValueError):
        stale = 90.0
    return max(0.0, limit_atr), max(0.0, limit_points), max(0.0, stale)


def format_mirror_gate_lines(rows: list[dict[str, Any]] | None, *, enabled: bool) -> list[str]:
    """回测摘要里给人看的几行。关掉时明确写没套闸门，避免和上一把混淆。"""
    if not enabled:
        return ["实盘镜像  关（开仓不套过期K和追价闸，成绩和以前同一口径）"]
    if not rows:
        return ["实盘镜像  开；这段没有因过期或追价放弃的开仓"]
    n_slip = sum(1 for row in rows if row.get("kind") == "slip_block")
    n_stale = sum(1 for row in rows if row.get("kind") == "stale_block")
    lines = [f"实盘镜像  开；放弃 {len(rows)} 次（追价{n_slip} 过期{n_stale}）"]
    for row in rows[:8]:
        when = str(row.get("bar_id") or row.get("time") or "")[:19]
        side = str(row.get("side") or "")
        lines.append(
            f"  {when} {side} {row.get('label')} "
            f"信号{row.get('signal_price')} 对照{row.get('live_price')} "
            f"偏移{row.get('chase')} > {row.get('limit')}"
        )
    if len(rows) > 8:
        lines.append(f"  …还有 {len(rows) - 8} 行，详见报告")
    return lines
