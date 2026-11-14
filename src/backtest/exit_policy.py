"""回测平仓规则（ExitPolicy）。

从 engine.py 抽出来：止损取哪个价、什么时候算打穿、按什么价成交。
都是纯函数，吃到一根K线和持仓就能算，方便单独测。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.backtest.fill import FillModel
from src.data.periods import canonical_period


def carry_stop_price(bars: pd.DataFrame, price: float, side: str) -> float:
    """换月移仓的保护止损：2 倍 ATR(14)，数据不够时退回 0.5% 价格。"""
    atr = 0.0
    try:
        look = bars.tail(15)
        if len(look) >= 2:
            high = look["high"].astype(float)
            low = look["low"].astype(float)
            prev_close = look["close"].astype(float).shift(1)
            true_range = pd.concat(
                [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
                axis=1,
            ).max(axis=1)
            atr = float(true_range.tail(14).mean())
    except Exception:  # noqa: BLE001
        atr = 0.0
    if not atr > 0:
        atr = abs(float(price)) * 0.005
    pad = 2 * atr
    return round(float(price) - pad if side == "long" else float(price) + pad, 6)


def next_open_price(bars: pd.DataFrame, i: int, period: str) -> float | None:
    """下一根开盘价。

    跨休市也按下一根开盘成交：真实下单就是等下一次开盘，不能拿上一段收盘价顶替，
    否则跳空低开时会出现当天根本没成交过的开仓价。
    """
    if bars is None or i + 1 >= len(bars):
        return None
    code = canonical_period(period)
    if code in ("D", "W"):
        return float(bars["open"].iloc[i + 1])
    if not code.isdigit():
        return None
    return float(bars["open"].iloc[i + 1])


def position_stop(pos: Any, decision_stop: float | None) -> float:
    """
    平仓用的止损价：优先用策略更新后的尾随止损，没有再退回初始止损/决策止损。

    旧写法把 initial_stop 放在前面，`or` 一短路，尾随止损永远用不上，
    回测里移动止损就形同不存在。
    """
    trail = float(getattr(pos, "trail_stop", 0) or 0)
    initial = float(getattr(pos, "initial_stop", 0) or 0)
    return trail or initial or float(decision_stop or 0)


def stop_breached(side: str, stop: float, bar: pd.DataFrame) -> bool:
    """这根K线的最高或最低有没有打穿止损。"""
    if stop <= 0 or bar is None or len(bar) == 0:
        return False
    if side == "long":
        return float(bar["low"].iloc[-1]) <= stop
    if side == "short":
        return float(bar["high"].iloc[-1]) >= stop
    return False


def exit_fill_price(
    *,
    side: str,
    stop: float,
    bar: pd.DataFrame,
    next_open: float | None,
    close_px: float,
    fill: FillModel,
    touch_stop: bool = True,
) -> float:
    """
    平仓成交价。

    touch_stop=True（触及即止损）：这根K线已经打穿止损时按止损价成交（跳空按开盘价，更差）。
    touch_stop=False（收盘价确认）：这次平仓是收盘后才决定的，不能按止损价成交，
    退回下一根开盘价 / 当根收盘价，避免回测把收盘确认方式算得过于理想。
    两种都再按买卖方向让滑点。
    """
    ref = None
    if touch_stop and stop_breached(side, stop, bar):
        bar_open = float(bar["open"].iloc[-1])
        ref = min(stop, bar_open) if side == "long" else max(stop, bar_open)
    if ref is None:
        ref = next_open if next_open is not None else close_px
    return fill.sell(ref) if side == "long" else fill.buy(ref)
