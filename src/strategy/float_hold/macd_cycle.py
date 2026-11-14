"""
MACD 一轮循环后，再用一对均线定当天方向。

功能：早盘从开盘那根 MACD 周期（1 分钟或 5 分钟）算起，先看柱子是红还是绿。
     等开盘这一色走完，再等反向那一色走完，才算走完一轮多空循环。
     循环走完那一根，短均线在长均线上面就做多，下面就做空。当天这个方向冻住。
原理：红柱 = MACD 柱在零轴上方，绿柱 = 在零轴下方。柱子变号就算这一波走完。
     零轴上的平柱不算换色。MACD 用开盘前的历史一起算，避免一开盘柱子乱跳。
     不看箱子、也不做反向震荡单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from src.data.periods import last_ohlc_price, ohlc_series, resample_ohlc
from src.strategy.daily_body import _naive, _signal_minutes, _with_clock
from src.strategy.indicators import ema_series, macd_lines
from src.utils.session_clock import last_open_at

from .direction import DIRECTION_LABELS, DIRECTION_MACD_CYCLE, DirectionContext, DirectionPick

# 配置页：MACD 用 1 分钟还是 5 分钟。
MACD_TF_CHOICES = ("1", "5")
MACD_TF_LABELS: dict[str, str] = {"1": "1分钟", "5": "5分钟"}

# 配置页：定向均线只许从这五个根数里挑一对，短的在前、长的在后。
EMA_PERIODS = (5, 10, 20, 60, 120)
EMA_PAIR_CHOICES: tuple[tuple[int, int], ...] = tuple(
    (a, b) for a in EMA_PERIODS for b in EMA_PERIODS if a < b
)
EMA_PAIR_LABELS: tuple[str, ...] = tuple(f"EMA{a} / EMA{b}" for a, b in EMA_PAIR_CHOICES)


def normalize_macd_timeframe(value: Any) -> str:
    """收成 1 或 5。写 1分钟、5m 也认。"""
    text = str(value or "").strip().lower().replace("分钟", "").replace("m", "")
    if text in {"1", "01"}:
        return "1"
    return "5"


def macd_timeframe_label(value: Any) -> str:
    """配置值换成配置页上的中文。"""
    return MACD_TF_LABELS[normalize_macd_timeframe(value)]


# 优化和配置页用的常见 MACD 组合：快线 / 慢线 / 平滑。
MACD_COMBO_PRESETS: tuple[str, ...] = ("12/26/9", "5/34/5", "8/17/9", "6/13/5")
MACD_COMBO_LABELS: tuple[str, ...] = (
    "12 / 26 / 9（常用）",
    "5 / 34 / 5",
    "8 / 17 / 9",
    "6 / 13 / 5",
)


def parse_macd_combo(value: Any) -> tuple[int, int, int]:
    """
    读「12/26/9」或「12,26,9」。认不出用 12、26、9。

    快线必须短于慢线，否则对调。
    """
    text = str(value or "").strip().replace(" ", "").replace("，", "/")
    text = text.replace(",", "/").replace("-", "/")
    parts = [p for p in text.split("/") if p]
    nums: list[int] = []
    for part in parts[:3]:
        try:
            nums.append(int(float(part)))
        except (TypeError, ValueError):
            nums = []
            break
    if len(nums) != 3:
        return 12, 26, 9
    fast, slow, signal = nums
    fast = max(2, min(80, fast))
    slow = max(3, min(120, slow))
    signal = max(2, min(60, signal))
    if fast >= slow:
        fast, slow = min(fast, slow), max(fast + 1, slow)
    return fast, slow, signal


def format_macd_combo(fast: Any, slow: Any, signal: Any) -> str:
    """三根参数收成 12/26/9，方便优化网格和配置对照。"""
    a, b, c = parse_macd_combo(f"{fast}/{slow}/{signal}")
    return f"{a}/{b}/{c}"


def macd_combo_label(value: Any) -> str:
    """配置值换成配置页下拉里的中文。对不上常见组合时仍显示数字。"""
    combo = format_macd_combo(*(parse_macd_combo(value)))
    for raw, label in zip(MACD_COMBO_PRESETS, MACD_COMBO_LABELS):
        if raw == combo:
            return label
    a, b, c = parse_macd_combo(combo)
    return f"{a} / {b} / {c}"


def ema_pair_label(fast: Any, slow: Any) -> str:
    """两根均线换成「EMA5 / EMA20」这种给人看的名字。"""
    a, b = _ema_pair_ints(fast, slow)
    return f"EMA{a} / EMA{b}"


def parse_ema_pair_label(value: Any, *, default_fast: int = 5, default_slow: int = 20) -> tuple[int, int]:
    """
    读配置页或 yaml 里的均线对。

    认「EMA5 / EMA20」「5/20」。认不出就用出厂 5 和 20。
    """
    text = str(value or "").strip().upper().replace(" ", "")
    text = text.replace("EMA", "")
    for sep in ("/", "-", ",", "对"):
        if sep in text:
            left, right = text.split(sep, 1)
            try:
                return _ema_pair_ints(int(left), int(right))
            except (TypeError, ValueError):
                break
    return _ema_pair_ints(default_fast, default_slow)


def _ema_pair_ints(fast: Any, slow: Any) -> tuple[int, int]:
    """短均线必须小于长均线。不在名单里的数字（测试用）只要一短一长也保留。"""
    try:
        a = int(float(fast))
    except (TypeError, ValueError):
        a = 5
    try:
        b = int(float(slow))
    except (TypeError, ValueError):
        b = 20
    a = max(2, min(240, a))
    b = max(3, min(240, b))
    if a == b:
        b = a + 1
    if a > b:
        a, b = b, a
    return a, b


def _macd_int(value: Any, default: int, lo: int, hi: int) -> int:
    """MACD / 均线根数。写错就用出厂值。"""
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    return max(int(lo), min(int(hi), out))


def _hist_color(value: float) -> int:
    """柱子颜色：+1 红柱（零轴上），-1 绿柱（零轴下），0 贴着零轴。"""
    if value > 1e-12:
        return 1
    if value < -1e-12:
        return -1
    return 0


def _color_name(sign: int) -> str:
    if sign > 0:
        return "红柱"
    if sign < 0:
        return "绿柱"
    return "平"


@dataclass
class CycleScan:
    """开盘后扫 MACD 柱子的结果。ready=已经走完一轮。"""

    ready: bool
    first: int
    flips: int
    done_at: pd.Timestamp | None
    reason: str


def scan_macd_cycle(hist: pd.Series, opened: pd.Timestamp, now: pd.Timestamp) -> CycleScan:
    """
    从这段开盘扫到现在：第一色走完、第二色再走完，才 ready。

    hist 是整段（含开盘前）的 MACD 柱。只在开盘之后的柱上数循环。
    """
    if hist is None or len(hist) == 0:
        return CycleScan(False, 0, 0, None, "MACD还没有柱子，先等")
    session = hist.loc[(hist.index > opened) & (hist.index <= now)]
    if session.empty:
        return CycleScan(False, 0, 0, None, "这段开盘后的MACD还没出来，先等")

    first = 0
    last = 0
    flips = 0
    done_at: pd.Timestamp | None = None
    for stamp, raw in session.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value != value:
            continue
        color = _hist_color(value)
        if color == 0:
            continue
        if first == 0:
            first = color
            last = color
            continue
        if color != last:
            flips += 1
            last = color
            if flips == 2:
                done_at = pd.Timestamp(stamp)
                break

    if first == 0:
        return CycleScan(False, 0, 0, None, "开盘后MACD柱还贴着零轴，先等出现红柱或绿柱")
    first_name = _color_name(first)
    other_name = _color_name(-first)
    if flips == 0:
        return CycleScan(
            False,
            first,
            0,
            None,
            f"开盘是{first_name}，还在等这波{first_name}走完",
        )
    if flips == 1:
        return CycleScan(
            False,
            first,
            1,
            None,
            f"开盘{first_name}已经走完，正在等这波{other_name}走完，才算一轮循环",
        )
    return CycleScan(
        True,
        first,
        2,
        done_at,
        f"开盘{first_name}走完后又走完一波{other_name}，一轮多空循环结束",
    )


def macd_period_view(
    bars: pd.DataFrame,
    now: datetime,
    timeframe: str,
    macd_minutes: int,
) -> tuple[pd.DataFrame | None, str]:
    """
    拿出算 MACD 用的 K 线。

    要 5 分钟：手里是 1 分钟就收成 5 分钟，已经是 5 分钟就直接用。
    要 1 分钟：必须本身就是大约 1 分钟的 K，更粗的收不回去。
    更粗的信号周期拼不出更细的 MACD，请把左边信号周期改成不粗于 MACD 周期。
    """
    want = 1 if int(macd_minutes) <= 1 else 5
    tag = "1分钟" if want == 1 else "5分钟"
    minutes = _signal_minutes(timeframe)
    if minutes <= 0:
        return None, f"{tag}MACD只看分钟线，日线、周线不定这个方向"
    frame = _with_clock(bars)
    moment = pd.Timestamp(_naive(now))
    frame = frame.loc[frame.index <= moment]
    if frame is None or frame.empty:
        return None, "没有可用的K线，先等"
    delta = 0.0
    if len(frame) >= 2:
        delta = (frame.index[-1] - frame.index[-2]).total_seconds() / 60.0
    if want == 1:
        if minutes == 1 or (0 < delta <= 1.6):
            return frame, ""
        return None, "1分钟MACD需要1分钟K线。请把左边信号周期改成1分钟"
    if minutes == 5 or (4.0 <= delta <= 6.5):
        return frame, ""
    if minutes < 5 or (0 < delta < 4.0):
        view = resample_ohlc(frame, "5")
        if view is None or view.empty:
            return None, "1分钟还不够收成5分钟，先等"
        return view.loc[view.index <= moment], ""
    return None, f"{tag}MACD需要{tag}K线。请把左边信号周期改成不粗于{tag}"


class MacdCycleDirection:
    """
    早盘走完一轮 MACD 红绿柱，再用选定的均线对定当天方向。

    不等开盘箱子。循环走完时均线怎么排，当天就按这个方向拿。
    """

    key = DIRECTION_MACD_CYCLE
    label = DIRECTION_LABELS[DIRECTION_MACD_CYCLE]
    needs_opening_box = False

    def __init__(self) -> None:
        self.fast = 12
        self.slow = 26
        self.signal = 9
        self.ema_fast = 5
        self.ema_slow = 20
        self.bar_minutes = 5

    def configure(self, params: dict[str, Any] | None) -> None:
        """从策略配置读 MACD 周期、参数和均线对。没写就用 5 分钟、12/26/9、EMA5/20。"""
        box = params or {}
        self.bar_minutes = 1 if normalize_macd_timeframe(box.get("macd_timeframe")) == "1" else 5
        if box.get("macd_combo"):
            self.fast, self.slow, self.signal = parse_macd_combo(box.get("macd_combo"))
        else:
            self.fast = _macd_int(box.get("macd_fast"), 12, 2, 60)
            self.slow = _macd_int(box.get("macd_slow"), 26, 3, 120)
            self.signal = _macd_int(box.get("macd_signal"), 9, 2, 60)
        pair_text = box.get("dir_ema_pair")
        if pair_text:
            self.ema_fast, self.ema_slow = parse_ema_pair_label(
                pair_text,
                default_fast=_macd_int(box.get("dir_ema_fast"), 5, 2, 120),
                default_slow=_macd_int(box.get("dir_ema_slow"), 20, 3, 240),
            )
        else:
            self.ema_fast, self.ema_slow = _ema_pair_ints(
                box.get("dir_ema_fast", 5), box.get("dir_ema_slow", 20)
            )
        if self.slow <= self.fast:
            self.slow = self.fast + 1

    def pick(self, ctx: DirectionContext) -> DirectionPick:
        tag = "1分钟" if self.bar_minutes == 1 else "5分钟"
        view, wait = macd_period_view(
            ctx.bars, ctx.now, ctx.timeframe, self.bar_minutes
        )
        if view is None or wait:
            return DirectionPick("none", wait or f"没有{tag}K线，先等", {"direction": self.key})
        closes = ohlc_series(view, "close").dropna()
        need = max(self.slow + self.signal, self.ema_slow) + 5
        if len(closes) < need:
            return DirectionPick(
                "none",
                f"{tag}K线还不够算MACD（至少大约 {need} 根，现在 {len(closes)} 根），先等",
                {"direction": self.key},
            )
        opened = pd.Timestamp(last_open_at(_naive(ctx.now), ctx.anchor))
        now_ts = pd.Timestamp(_naive(ctx.now))
        macd = macd_lines(
            closes, fast=self.fast, slow=self.slow, signal=self.signal
        )
        scan = scan_macd_cycle(macd["hist"], opened, now_ts)
        if not scan.ready or scan.done_at is None:
            return DirectionPick("none", scan.reason, {"direction": self.key})

        fast_ma = ema_series(closes, self.ema_fast)
        slow_ma = ema_series(closes, self.ema_slow)
        stamp = scan.done_at
        if stamp not in fast_ma.index:
            stamp = fast_ma.index[fast_ma.index <= stamp]
            if len(stamp) == 0:
                return DirectionPick("none", "循环走完了，但均线还没算出来，先等", {"direction": self.key})
            stamp = stamp[-1]
        e_fast = float(fast_ma.loc[stamp])
        e_slow = float(slow_ma.loc[stamp])
        clock = pd.Timestamp(stamp).strftime("%H:%M")
        if e_fast > e_slow + 1e-12:
            side = "long"
            ma_text = (
                f"循环走完时（{clock}）EMA{self.ema_fast} {e_fast:.2f} "
                f"在 EMA{self.ema_slow} {e_slow:.2f} 上方，当天做多"
            )
        elif e_fast < e_slow - 1e-12:
            side = "short"
            ma_text = (
                f"循环走完时（{clock}）EMA{self.ema_fast} {e_fast:.2f} "
                f"在 EMA{self.ema_slow} {e_slow:.2f} 下方，当天做空"
            )
        else:
            return DirectionPick(
                "none",
                f"{scan.reason}，但循环走完时两条均线贴在一起，先等",
                {"direction": self.key},
            )
        last_close = last_ohlc_price(view, "close")
        extra = ""
        if last_close is not None:
            extra = f"。现在收盘 {last_close:.2f}"
        return DirectionPick(
            side,
            f"{scan.reason}。{ma_text}{extra}",
            {
                "direction": self.key,
                "cycle_done_at": str(scan.done_at),
                "first_hist": scan.first,
            },
        )
