"""
滑点灵敏度：同一份K线、同一套策略参数，只换滑点跳数重跑回测。

功能：回答「这个策略的利润边缘有多厚」——滑点加到几跳就由盈转亏。
原理：K线只拉一次；每档滑点深拷贝配置改 backtest.slippage_ticks 后走同一套回测引擎。
     分水岭=净盈亏从正转负的位置（线性插值）；利润边缘=滑点0时每笔平均净盈亏折算成单边跳数。
"""

from __future__ import annotations

import copy
from typing import Any

# 默认扫的滑点档位（跳）
SLIP_STEPS: tuple[float, ...] = (0, 0.5, 1, 1.5, 2, 3)


def run_slippage_scan(
    cfg: dict[str, Any],
    *,
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    engine: str | None = None,
    slips: tuple[float, ...] = SLIP_STEPS,
) -> dict[str, Any]:
    """
    拉一次K线，按每档滑点各跑一遍回测。

    返回 {"rows": [{slip, trades, win_rate, net_pnl, profit_factor, max_drawdown}...],
          "bars": K线根数, "data_from": ..., "data_to": ...}。
    """
    from src.backtest.engine import run_backtest
    from src.data.kline import KlineProvider
    from src.strategy import create_strategy

    provider = KlineProvider(cfg)
    probe = copy.deepcopy(cfg)
    strategy = create_strategy(probe)
    st = probe.get("strategy") or {}
    tf = str(getattr(strategy, "timeframe", None) or st.get("timeframe", "15"))
    trail_tf = str(getattr(strategy, "trail_timeframe", None) or st.get("trail_timeframe", tf))
    max_bars = int((probe.get("backtest") or {}).get("max_bars", 1200))

    bars = provider.fetch(
        symbol, period=tf, max_bars=max_bars, keep_history=True, start=start_date, end=end_date
    )
    trail = None
    if trail_tf != tf:
        try:
            trail = provider.fetch(
                symbol, period=trail_tf, max_bars=max_bars, keep_history=True,
                start=start_date, end=end_date,
            )
        except Exception:  # noqa: BLE001
            trail = None  # 引擎内部会退回信号周期，和单次回测一致

    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"bars": 0, "data_from": "", "data_to": ""}
    for slip in slips:
        one = copy.deepcopy(cfg)
        one.setdefault("backtest", {})["slippage_ticks"] = float(slip)
        res = run_backtest(
            one,
            symbol=symbol,
            bars_signal=bars,
            bars_trail=trail,
            start_date=start_date,
            end_date=end_date,
            engine=engine,
        )
        p = res.performance
        rows.append(
            {
                "slip": float(slip),
                "trades": p.trades,
                "win_rate": p.win_rate,
                "net_pnl": p.net_pnl,
                "profit_factor": p.profit_factor,
                "max_drawdown": p.max_drawdown,
            }
        )
        if not meta["bars"]:
            snap = res.params_snapshot or {}
            meta = {
                "bars": res.bars,
                "data_from": str(snap.get("data_from") or "")[:16],
                "data_to": str(snap.get("data_to") or "")[:16],
            }
    return {"rows": rows, **meta}


def breakeven_slip(rows: list[dict[str, Any]]) -> float | None:
    """净盈亏由盈转亏的滑点位置（线性插值）。一直亏或一直盈返回 None。"""
    prev: dict[str, Any] | None = None
    for row in rows:
        if prev is not None and prev["net_pnl"] > 0 >= row["net_pnl"]:
            span = row["slip"] - prev["slip"]
            if span <= 0:
                return float(row["slip"])
            ratio = prev["net_pnl"] / (prev["net_pnl"] - row["net_pnl"])
            return round(prev["slip"] + span * ratio, 2)
        prev = row
    return None


def edge_ticks_per_trade(rows: list[dict[str, Any]], *, tick: float, mult: float) -> float | None:
    """
    滑点 0 时每笔平均净盈亏折算成「单边几跳」。

    来回成本 = 2 × 单边滑点跳数 × 每跳价格 × 乘数 × 手数（这里按1手口径，手数不改变比值）。
    """
    zero = next((r for r in rows if r["slip"] == 0), None)
    if not zero or zero["trades"] <= 0 or zero["net_pnl"] <= 0:
        return None
    per_trade = zero["net_pnl"] / zero["trades"]
    one_tick_value = max(float(tick), 1e-9) * max(float(mult), 1e-9)
    return round(per_trade / one_tick_value / 2, 2)


def format_slip_scan(
    scan: dict[str, Any],
    *,
    symbol: str,
    current_slip: float,
    tick: float,
    mult: float,
) -> str:
    """把扫描结果收成历史回测页能直接显示的文字。"""
    rows = scan.get("rows") or []
    lines = [
        f"滑点灵敏度  {symbol}",
        f"区间 {scan.get('data_from') or '?'} ~ {scan.get('data_to') or '?'} | K线 {scan.get('bars', 0)} 根",
        "除滑点外，其余参数与「开始历史回测」完全一致（含开仓限制、过滤器、下一根开盘成交）",
        "",
        f"{'滑点(跳)':>7} {'笔数':>5} {'胜率':>5} {'净盈亏':>9} {'盈利因子':>6} {'最大回撤':>8}",
    ]
    for row in rows:
        mark = "  ← 当前" if abs(row["slip"] - float(current_slip)) < 1e-9 else ""
        lines.append(
            f"{row['slip']:>7g} {row['trades']:>5} {row['win_rate']:>5.0%} "
            f"{row['net_pnl']:>+9.0f} {row['profit_factor']:>6.2f} {row['max_drawdown']:>8.0f}{mark}"
        )
    lines.append("")
    edge = edge_ticks_per_trade(rows, tick=tick, mult=mult)
    if edge is not None:
        lines.append(f"利润边缘：滑点0时每笔平均净盈亏 ≈ 单边 {edge:g} 跳。滑点超过这个数，策略就不赚钱。")
    turn = breakeven_slip(rows)
    if turn is not None:
        lines.append(f"盈亏分水岭 ≈ {turn:g} 跳：滑点加到这一档，净盈亏由正转负。")
    elif rows and rows[-1]["net_pnl"] > 0:
        lines.append(f"到 {rows[-1]['slip']:g} 跳仍然盈利，策略对滑点不敏感。")
    else:
        lines.append("滑点为 0 时已经亏损：问题不在成交口径，先看策略本身。")
    lines.append("实盘真实滑点看「复盘」页尾部的滑点校准段；回测建议以 2 跳仍不亏作为及格线。")
    return "\n".join(lines)
