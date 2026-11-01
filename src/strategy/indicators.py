"""
技术指标工具
功能：计算 Pinbar 形态、抛物线 SAR、简单趋势过滤。
原理：全部基于 OHLC 数值计算，不依赖同花顺截图，更稳定、可回测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd


PinbarSide = Literal["bull", "bear", "none"]


@dataclass
class PinbarResult:
    """单根 K 线的 Pinbar 判定结果。"""

    side: PinbarSide
    wick_extreme: float  # 多头 Pinbar=最低点；空头 Pinbar=最高点（用作初始止损）
    body: float
    upper_wick: float
    lower_wick: float
    reason: str = ""


def detect_pinbar(
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    wick_body_ratio: float = 2.0,
    wick_range_ratio: float = 0.6,
    max_opposite_wick_ratio: float = 0.3,
) -> PinbarResult:
    """
    识别经典 Pinbar（针形线）。

    规则（通俗版）：
    - 实体很小，某一侧影线很长
    - 长下影线 = 看涨 Pinbar（锤子）：影线反向做多，止损放在最低点
    - 长上影线 = 看跌 Pinbar（流星）：影线反向做空，止损放在最高点

    参数:
        wick_body_ratio: 主影线至少是实体的几倍
        wick_range_ratio: 主影线至少占整根 K 线振幅的比例
        max_opposite_wick_ratio: 另一侧影线不能太长（相对振幅）
    """
    rng = max(high - low, 1e-9)
    body = abs(close - open_)
    upper = high - max(open_, close)
    lower = min(open_, close) - low

    # 看涨：长下影
    if (
        lower >= body * wick_body_ratio
        and lower / rng >= wick_range_ratio
        and upper / rng <= max_opposite_wick_ratio
    ):
        return PinbarResult(
            side="bull",
            wick_extreme=low,
            body=body,
            upper_wick=upper,
            lower_wick=lower,
            reason="长下影 Pinbar，影线反向做多",
        )

    # 看跌：长上影
    if (
        upper >= body * wick_body_ratio
        and upper / rng >= wick_range_ratio
        and lower / rng <= max_opposite_wick_ratio
    ):
        return PinbarResult(
            side="bear",
            wick_extreme=high,
            body=body,
            upper_wick=upper,
            lower_wick=lower,
            reason="长上影 Pinbar，影线反向做空",
        )

    return PinbarResult("none", wick_extreme=close, body=body, upper_wick=upper, lower_wick=lower)


def macd_lines(
    close: pd.Series,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """
    计算经典 MACD。

    DIF 是快慢两条指数均线的差，DEA 是 DIF 自己的均线，柱子是两者的差。
    背离看的是 DIF：价格走新低时 DIF 没有走新低，或价格走新高时 DIF 没有走新高。
    """
    fast_ema = close.astype(float).ewm(span=int(fast), adjust=False).mean()
    slow_ema = close.astype(float).ewm(span=int(slow), adjust=False).mean()
    dif = fast_ema - slow_ema
    dea = dif.ewm(span=int(signal), adjust=False).mean()
    hist = dif - dea
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist}, index=close.index)


def macd(
    close: pd.Series,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD 零轴交叉还用这个名字。算出来的线和 macd_lines 是同一套。"""
    return macd_lines(close, fast=fast, slow=slow, signal=signal)


def ths_top_bottom_divergence(
    high: pd.Series,
    low: pd.Series,
    diff: pd.Series,
    dea: pd.Series,
) -> dict[str, Any] | None:
    """
    按同花顺副图公式，判断最后一根是不是顶背离或底背离。

    死叉：DIFF 从上往下穿过 DEA。金叉：DIFF 从下往上穿过 DEA。
    顶背离写在死叉这根上：从上次金叉到这次死叉，价格最高点比上一段更高，
    但这根公式里的 MACD 柱（DIFF-DEA 再乘 2）的最高点却比上一段更低。
    底背离写在金叉这根上：这一段价格最低点更低，MACD 柱的最低点却更高。
    第一次交叉还没有上一段可比，不算背离。
    """
    n = len(high)
    if n < 3 or len(low) != n or len(diff) != n or len(dea) != n:
        return None
    highs = high.astype(float).to_numpy()
    lows = low.astype(float).to_numpy()
    dif = diff.astype(float).to_numpy()
    signal = dea.astype(float).to_numpy()
    # 同花顺副图的柱子是 (DIFF-DEA)*2，比较高低时用这个数。
    stick = (dif - signal) * 2.0

    last_jc: int | None = None
    last_sc: int | None = None
    prev_hh: float | None = None
    prev_mhd: float | None = None
    prev_ll: float | None = None
    prev_mld: float | None = None
    found: dict[str, Any] | None = None

    for i in range(1, n):
        # CROSS：这一根 DIFF 在 DEA 上面，上一根还在下面或重合。
        golden = dif[i] > signal[i] and dif[i - 1] <= signal[i - 1]
        # CROSSDOWN：这一根 DIFF 在 DEA 下面，上一根还在上面或重合。
        death = dif[i] < signal[i] and dif[i - 1] >= signal[i - 1]
        if death and last_jc is not None:
            # BARSLAST(金叉)+1，窗口从上次金叉那根算到这次死叉，两头都算。
            start = last_jc
            price_high = float(np.max(highs[start : i + 1]))
            stick_high = float(np.max(stick[start : i + 1]))
            if prev_hh is not None and prev_mhd is not None and price_high > prev_hh and stick_high < prev_mhd:
                found = {"side": "bear", "stop": price_high, "index": i, "label": "顶背离"}
            else:
                found = None
            prev_hh = price_high
            prev_mhd = stick_high
            last_sc = i
        elif death:
            last_sc = i
            found = None
        elif golden and last_sc is not None:
            start = last_sc
            price_low = float(np.min(lows[start : i + 1]))
            stick_low = float(np.min(stick[start : i + 1]))
            if prev_ll is not None and prev_mld is not None and price_low < prev_ll and stick_low > prev_mld:
                found = {"side": "bull", "stop": price_low, "index": i, "label": "底背离"}
            else:
                found = None
            prev_ll = price_low
            prev_mld = stick_low
            last_jc = i
        elif golden:
            last_jc = i
            found = None
        else:
            found = None

    if found is not None and int(found["index"]) == n - 1:
        return found
    return None


def ths_macd_top_bottom(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    short: int = 5,
    long: int = 34,
    m: int = 5,
) -> dict[str, Any] | None:
    """
    用同花顺 MACD(SHORT, LONG, M) 算出 DIFF、DEA，再看最后一根有没有顶底背离。

    图上常见的是 MACD(5,34,5)。SHORT 是快线，LONG 是慢线，M 是 DIFF 的平滑根数。
    """
    lines = macd_lines(close, fast=int(short), slow=int(long), signal=int(m))
    return ths_top_bottom_divergence(high, low, lines["dif"], lines["dea"])


def find_regular_divergence(
    high: pd.Series,
    low: pd.Series,
    dif: pd.Series,
    *,
    left: int = 3,
    right: int = 3,
    min_gap: int = 5,
    max_gap: int = 40,
    require_zero: bool = True,
) -> dict[str, Any] | None:
    """
    找刚刚确认的常规背离。

    转折点要左右各有几根 K 线衬托，所以信号出现在转折点之后的第 right 根，不是转折点那根本身。
    只认常规背离：价格更低、DIF 更高是看涨；价格更高、DIF 更低是看跌。
    require_zero 为真时，看涨要发生在零轴下方，看跌要发生在零轴上方。这是比较常见的过滤。
    同一根上如果涨跌两边都算背离，就放弃，避免方向打架。
    """
    n = len(low)
    if n != len(high) or n != len(dif) or n < left + right + min_gap + 2:
        return None
    pivot_at = n - 1 - int(right)
    if pivot_at < left:
        return None

    def _is_pivot(i: int, kind: str) -> bool:
        if i < left or i + right >= n:
            return False
        if kind == "low":
            value = float(low.iloc[i])
            others = [float(low.iloc[j]) for j in range(i - left, i + right + 1) if j != i]
            return value < min(others)
        value = float(high.iloc[i])
        others = [float(high.iloc[j]) for j in range(i - left, i + right + 1) if j != i]
        return value > max(others)

    def _earlier(kind: str) -> int | None:
        # 从刚确认的这个转折点往前找上一个同类转折点，距离要落在设定的根数里。
        start = max(left, pivot_at - int(max_gap))
        end = pivot_at - int(min_gap)
        for i in range(end, start - 1, -1):
            if _is_pivot(i, kind):
                return i
        return None

    bull = None
    bear = None
    if _is_pivot(pivot_at, "low"):
        prev = _earlier("low")
        if prev is not None:
            price_now = float(low.iloc[pivot_at])
            price_prev = float(low.iloc[prev])
            dif_now = float(dif.iloc[pivot_at])
            dif_prev = float(dif.iloc[prev])
            zero_ok = (not require_zero) or dif_now < 0
            if price_now < price_prev and dif_now > dif_prev and zero_ok:
                bull = {
                    "side": "bull",
                    "stop": price_now,
                    "pivot_index": pivot_at,
                    "prev_index": prev,
                }
    if _is_pivot(pivot_at, "high"):
        prev = _earlier("high")
        if prev is not None:
            price_now = float(high.iloc[pivot_at])
            price_prev = float(high.iloc[prev])
            dif_now = float(dif.iloc[pivot_at])
            dif_prev = float(dif.iloc[prev])
            zero_ok = (not require_zero) or dif_now > 0
            if price_now > price_prev and dif_now < dif_prev and zero_ok:
                bear = {
                    "side": "bear",
                    "stop": price_now,
                    "pivot_index": pivot_at,
                    "prev_index": prev,
                }
    if bull is not None and bear is not None:
        return None
    return bull or bear


def trend_bias(closes: pd.Series, lookback: int = 8) -> Literal["up", "down", "flat"]:
    """
    用最近 lookback 根收盘价判断是否刚经历一段上涨/下跌。

    原理：简单比较「现在」和「lookback 根之前」的价格差，足够做 Pinbar 前置过滤。
    """
    if len(closes) < lookback + 1:
        return "flat"
    now = float(closes.iloc[-1])
    past = float(closes.iloc[-(lookback + 1)])
    change = (now - past) / max(abs(past), 1e-9)
    if change > 0.003:  # 约 0.3% 视为有上涨过程
        return "up"
    if change < -0.003:
        return "down"
    return "flat"


def average_true_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """
    平均真实波幅 ATR。

    每根取「最高-最低 / 最高离昨收 / 最低离昨收」三者最大，再对 period 根简单平均。
    """
    high_f = high.astype(float)
    low_f = low.astype(float)
    close_f = close.astype(float)
    prev = close_f.shift(1)
    tr = pd.concat(
        [(high_f - low_f), (high_f - prev).abs(), (low_f - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(max(2, int(period)), min_periods=max(2, int(period))).mean()


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[pd.Series, pd.Series]:
    """
    SuperTrend（ATR 通道趋势线）。

    返回 (趋势线, 方向)。方向 1=多头环境（线在价格下方），-1=空头（线在价格上方）。
    收盘穿越通道带时翻转，比单纯均线交叉更少在窄幅里来回抽打。
    """
    n = len(close)
    empty = pd.Series(dtype=float)
    if n == 0:
        return empty, empty
    atr = average_true_range(high, low, close, period=period)
    hl2 = (high.astype(float) + low.astype(float)) / 2.0
    basic_upper = hl2 + float(multiplier) * atr
    basic_lower = hl2 - float(multiplier) * atr

    final_upper = np.zeros(n, dtype=float)
    final_lower = np.zeros(n, dtype=float)
    st = np.zeros(n, dtype=float)
    direction = np.ones(n, dtype=float)

    for i in range(n):
        bu = float(basic_upper.iloc[i]) if not pd.isna(basic_upper.iloc[i]) else np.nan
        bl = float(basic_lower.iloc[i]) if not pd.isna(basic_lower.iloc[i]) else np.nan
        c = float(close.iloc[i])
        if i == 0 or np.isnan(bu) or np.isnan(bl):
            final_upper[i] = bu if not np.isnan(bu) else c
            final_lower[i] = bl if not np.isnan(bl) else c
            st[i] = final_lower[i]
            direction[i] = 1.0
            continue

        prev_fu = final_upper[i - 1]
        prev_fl = final_lower[i - 1]
        prev_c = float(close.iloc[i - 1])
        if bu < prev_fu or prev_c > prev_fu:
            final_upper[i] = bu
        else:
            final_upper[i] = prev_fu
        if bl > prev_fl or prev_c < prev_fl:
            final_lower[i] = bl
        else:
            final_lower[i] = prev_fl

        prev_dir = direction[i - 1]
        if prev_dir >= 0:
            if c < final_lower[i]:
                direction[i] = -1.0
                st[i] = final_upper[i]
            else:
                direction[i] = 1.0
                st[i] = final_lower[i]
        else:
            if c > final_upper[i]:
                direction[i] = 1.0
                st[i] = final_lower[i]
            else:
                direction[i] = -1.0
                st[i] = final_upper[i]

    return (
        pd.Series(st, index=close.index, name="supertrend"),
        pd.Series(direction, index=close.index, name="st_dir"),
    )


def parabolic_sar(
    high: pd.Series,
    low: pd.Series,
    *,
    af_start: float = 0.02,
    af_step: float = 0.02,
    af_max: float = 0.2,
) -> pd.Series:
    """
    计算抛物线 SAR（Parabolic Stop and Reverse）。

    原理：价格朝一个方向走时，止损点沿趋势加速上移/下移；
    碰到价格则翻转方向。适合你说的「方向确认后用 SAR 做移动止损」。
    """
    n = len(high)
    if n == 0:
        return pd.Series(dtype=float)

    sar = np.zeros(n, dtype=float)
    bull = True
    af = af_start
    ep = float(high.iloc[0])  # 极值点
    sar[0] = float(low.iloc[0])

    for i in range(1, n):
        prev_sar = sar[i - 1]
        if bull:
            sar[i] = prev_sar + af * (ep - prev_sar)
            # SAR 不能高于前两根最低价
            sar[i] = min(sar[i], float(low.iloc[i - 1]), float(low.iloc[max(0, i - 2)]))
            if float(low.iloc[i]) < sar[i]:
                # 翻空
                bull = False
                sar[i] = ep
                ep = float(low.iloc[i])
                af = af_start
            else:
                if float(high.iloc[i]) > ep:
                    ep = float(high.iloc[i])
                    af = min(af + af_step, af_max)
        else:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], float(high.iloc[i - 1]), float(high.iloc[max(0, i - 2)]))
            if float(high.iloc[i]) > sar[i]:
                bull = True
                sar[i] = ep
                ep = float(high.iloc[i])
                af = af_start
            else:
                if float(low.iloc[i]) < ep:
                    ep = float(low.iloc[i])
                    af = min(af + af_step, af_max)

    return pd.Series(sar, index=high.index, name="sar")
