"""
扫描解说（给人看的白话日志）
功能：每次拉完K线、策略评估后，用一两句话说明「最新K线长什么样」以及「能不能开仓」。
原理：只做展示，不改交易决策；形态用通用影线/实体描述，开仓结论直接复用 StrategyDecision。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from loguru import logger

from .indicators import detect_pinbar
from .types import StrategyAction, StrategyDecision

# 同一句话不要每 10 秒刷窗口；文件里每轮都记。键是合约。
_last_brief: dict[str, str] = {}


def _bar_shape(row: pd.Series) -> str:
    """把一根 OHLC 说成人话：阴阳、影线比例、是否像 Pin。"""
    o = float(row["open"])
    h = float(row["high"])
    lo = float(row["low"])
    c = float(row["close"])
    rng = max(h - lo, 1e-9)
    body = abs(c - o)
    up = h - max(o, c)
    dn = min(o, c) - lo
    candle = "阳线" if c > o else ("阴线" if c < o else "十字星")
    pin = detect_pinbar(o, h, lo, c, wick_body_ratio=2.0, wick_range_ratio=0.55, max_opposite_wick_ratio=0.3)
    if pin.side == "bull":
        pin_txt = "形态≈看涨Pin(长下影)"
    elif pin.side == "bear":
        pin_txt = "形态≈看跌Pin(长上影)"
    else:
        pin_txt = "形态=普通K(非标准Pin)"
    return (
        f"{candle} O={o:.0f} H={h:.0f} L={lo:.0f} C={c:.0f} "
        f"| 上影占{up/rng:.0%} 下影占{dn/rng:.0%} 实体占{body/rng:.0%} | {pin_txt}"
    )


def _is_holding(strategy: Any, symbol: str) -> bool:
    """按策略里的真实持仓判断，不拿 reason 里的字眼去猜。"""
    positions = getattr(strategy, "positions", None) or {}
    pos = positions.get(symbol)
    if pos is None:
        key = next((name for name in positions if str(name).lower() == str(symbol).lower()), None)
        pos = positions.get(key) if key is not None else None
    if pos is None:
        return False
    side = str(getattr(pos, "side", "flat") or "flat")
    volume = int(getattr(pos, "volume", 0) or 0)
    return side != "flat" and volume > 0


def _entry_verdict(decision: StrategyDecision, holding: bool) -> str:
    """是否符合开仓：直接翻译决策动作。"""
    if decision.action == StrategyAction.BUY_OPEN:
        return f"符合开仓条件 → 做多 | {decision.reason}"
    if decision.action == StrategyAction.SELL_OPEN:
        return f"符合开仓条件 → 做空 | {decision.reason}"
    if decision.action == StrategyAction.CLOSE:
        return f"持仓平仓信号（非新开仓）| {decision.reason}"
    # HOLD：持仓中继续拿着，还是空仓观望，看真实持仓，不看文字。
    reason = decision.reason or "无信号"
    if holding:
        return f"已有持仓，继续持有 | {reason}"
    return f"不符合开仓条件（观望）| {reason}"


def log_scan_brief(
    symbol: str,
    bars: pd.DataFrame | None,
    decision: StrategyDecision,
    strategy: Any,
    *,
    signal_tf: str = "15",
) -> None:
    """
    打出扫描解说日志。

    参数:
        symbol: 合约
        bars: 策略评估用的已收盘信号周期K线
        decision: 本轮策略结论
        strategy: 当前策略实例（保留参数，方便以后加解说）
        signal_tf: 信号周期（仅用于日志文字）
    """
    if bars is None or len(bars) == 0:
        _emit_scan_brief(symbol, f"{symbol} 扫描解说：暂无K线")
        return

    last = bars.iloc[-1]
    ts = str(bars.index[-1])
    shape = _bar_shape(last)
    verdict = _entry_verdict(decision, _is_holding(strategy, symbol))
    _emit_scan_brief(
        symbol,
        f"{symbol} 扫描解说 | 信号{signal_tf}m 最新已收盘 {ts} | {shape} | {verdict}",
    )


def _emit_scan_brief(symbol: str, text: str) -> None:
    """文件每轮都写；窗口只在这句话变了才出现（例如从观望变成开仓）。"""
    changed = _last_brief.get(symbol) != text
    _last_brief[symbol] = text
    logger.bind(ui=changed).info(text)
