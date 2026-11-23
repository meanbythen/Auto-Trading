"""均线斜率：估计、ATR 归一化角度、t 值和三态判断。

所有需要判断「均线走平还是趋势」的策略都调用这一份，避免各写一套。
只读传入的均线序列，不拉数据、不下单。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .indicators import average_true_range, ema_series
from ..utils.trading_day import futures_trading_day


@dataclass(frozen=True)
class SlopeMeasure:
    """一次斜率测量的结果。角度按 ATR 归一化，t 值和 R² 衡量拟合质量。"""

    lookback: int
    method: str
    slope: float
    rise: float
    angle_deg: float
    t_stat: float
    r2: float


@dataclass(frozen=True)
class SlopeConfig:
    """斜率判定参数。exit_deg 为空时退化成单阈值，和旧行为一致。"""

    lookback: int = 20
    method: str = "reg"
    entry_deg: float = 3.0
    exit_deg: float | None = None
    min_t: float = 0.0
    min_r2: float = 0.0
    price_side: bool = False


@dataclass(frozen=True)
class SlopeState:
    """三态判定结果：1 多、-1 空、0 走平/不许开。"""

    direction: int
    angle_deg: float
    threshold_deg: float
    regime: str
    t_stat: float
    r2: float
    reason: str


def latest_atr(bars: pd.DataFrame, close: pd.Series, period: int = 14) -> float:
    """取最后一根已收盘 K 线的 ATR；没有 high/low 时退回收盘价。"""
    high = pd.to_numeric(bars["high"], errors="coerce") if "high" in bars.columns else close
    low = pd.to_numeric(bars["low"], errors="coerce") if "low" in bars.columns else close
    atr_s = average_true_range(high, low, close, period).dropna()
    return float(atr_s.iloc[-1]) if len(atr_s) else 0.0


def measure_slope(
    ema: pd.Series,
    atr: float,
    *,
    lookback: int = 20,
    method: str = "reg",
) -> SlopeMeasure | None:
    """量一段均线的斜率。

    method="reg"：最近 N+1 根做一元线性回归，用拟合斜率换算 N 根累计涨幅；
    method="diff"：只看首尾两点。两种都按 N × ATR 归一化再换算成角度。
    数据不够或含空值时返回 None，由调用方按走平处理。
    """
    n = max(2, int(lookback))
    if len(ema) <= n:
        return None
    now_v = ema.iloc[-1]
    if pd.isna(now_v):
        return None
    use_reg = str(method or "").strip().lower() != "diff"
    t_stat = float("nan")
    r2 = float("nan")
    if use_reg:
        vals = [float(v) for v in pd.to_numeric(ema.iloc[-(n + 1) :], errors="coerce")]
        if any(pd.isna(v) for v in vals):
            return None
        m = len(vals)
        mean_x = (m - 1) / 2.0
        mean_y = sum(vals) / m
        den = sum((i - mean_x) ** 2 for i in range(m))
        slope = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(vals)) / den
        rise = slope * n
        ss_res = sum((v - (mean_y + slope * (i - mean_x))) ** 2 for i, v in enumerate(vals))
        ss_tot = sum((v - mean_y) ** 2 for v in vals)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else (1.0 if ss_res <= 1e-18 else 0.0)
        if m > 2:
            se = math.sqrt((ss_res / (m - 2)) / den)
            if se > 0:
                t_stat = slope / se
            else:
                t_stat = float("inf") if abs(slope) > 0 else 0.0
    else:
        prev = ema.iloc[-1 - n]
        if pd.isna(prev):
            return None
        rise = float(now_v) - float(prev)
        slope = rise / n
    atr_v = float(atr)
    if not math.isfinite(atr_v) or atr_v <= 1e-12:
        atr_v = max(abs(float(now_v)) * 0.001, 1e-6)
    angle = math.degrees(math.atan(rise / (n * atr_v)))
    return SlopeMeasure(
        lookback=n,
        method="reg" if use_reg else "diff",
        slope=float(slope),
        rise=float(rise),
        angle_deg=float(angle),
        t_stat=float(t_stat),
        r2=float(r2),
    )


def slope_angle_series(
    ema: pd.Series,
    atr: pd.Series | float,
    *,
    lookback: int = 20,
    method: str = "reg",
) -> pd.Series:
    """逐根算均线倾角，口径与 measure_slope 完全一致。

    自适应阈值需要看过去 window 根的角度分布，不能只算最后一根。reg 用滚动
    一元回归的闭式解，diff 用首尾两点差；两者都按 N × ATR 归一化再换成角度。
    预热不足或含空值的根返回 NaN。
    """
    n = max(2, int(lookback))
    y = pd.to_numeric(ema, errors="coerce").astype(float)
    if len(y) == 0:
        return pd.Series(dtype=float)
    if isinstance(atr, pd.Series):
        atr_s = pd.to_numeric(atr, errors="coerce").astype(float)
        if not atr_s.index.equals(y.index):
            atr_s = atr_s.reindex(y.index)
    else:
        atr_s = pd.Series(float(atr), index=y.index, dtype=float)
    vals = y.to_numpy(dtype=float)
    use_reg = str(method or "").strip().lower() != "diff"
    if use_reg:
        m = n + 1
        # x = 0..n，最近一根的 x 是 n。Σ(i·y) = Σ(n-k)·y[t-k]。
        weights = np.arange(n, -1, -1, dtype=float)
        weighted = np.convolve(vals, weights, mode="full")[: len(vals)]
        sy = pd.Series(vals).rolling(m, min_periods=m).sum().to_numpy()
        s_i = m * (m - 1) / 2.0
        s_ii = (m - 1) * m * (2 * m - 1) / 6.0
        den = m * s_ii - s_i * s_i
        rise = np.empty(len(vals), dtype=float)
        rise[:] = np.nan
        rise[n:] = ((m * weighted - s_i * sy) / den * n)[n:]
    else:
        rise = np.empty(len(vals), dtype=float)
        rise[:] = np.nan
        rise[n:] = vals[n:] - vals[:-n]
    atr_vals = atr_s.to_numpy(dtype=float)
    floor = np.maximum(np.abs(vals) * 0.001, 1e-6)
    safe_atr = np.where(np.isfinite(atr_vals) & (atr_vals > 1e-12), atr_vals, floor)
    angle = np.degrees(np.arctan(rise / (n * safe_atr)))
    out = pd.Series(angle, index=y.index, dtype=float)
    out[y.isna()] = np.nan
    return out


ADAPTIVE_MIN_RATIO = 0.7
ADAPTIVE_MAX_RATIO = 1.3
ADAPTIVE_MIN_SAMPLES = 50


def adaptive_angle_threshold(
    angles: pd.Series,
    base_deg: float,
    *,
    quantile: float = 0.80,
    window: int = 500,
    min_ratio: float = ADAPTIVE_MIN_RATIO,
    max_ratio: float = ADAPTIVE_MAX_RATIO,
) -> float:
    """按过去 window 根 |角度| 的 quantile 给阈值，夹在基准的 0.7~1.3 倍内。

    只用已经算出来的角度（含当前这根，它已收盘），不引入未来数据。有效样本
    不够或分布异常时退回基准值。
    """
    base = abs(float(base_deg))
    if base <= 0:
        return 0.0
    hist = pd.to_numeric(angles, errors="coerce").abs().dropna()
    if window and int(window) > 0:
        hist = hist.iloc[-int(window) :]
    if len(hist) < ADAPTIVE_MIN_SAMPLES:
        return base
    q = min(max(float(quantile), 0.05), 0.95)
    try:
        raw = float(hist.quantile(q))
    except (TypeError, ValueError):
        return base
    if not math.isfinite(raw):
        return base
    lo = base * max(0.1, float(min_ratio))
    hi = base * max(float(min_ratio), float(max_ratio))
    return float(min(max(raw, lo), hi))


def _trading_day_key(bars: pd.DataFrame) -> str:
    """最后一根 K 线属于哪个期货交易日；夜盘 21:00 起算下一交易日。"""
    try:
        ts = pd.Timestamp(bars.index[-1])
        return futures_trading_day(ts.to_pydatetime()).isoformat()
    except (IndexError, TypeError, ValueError, AttributeError):
        return ""


def adaptive_threshold_from_bars(
    bars: pd.DataFrame,
    ema: pd.Series,
    *,
    base_deg: float,
    lookback: int = 20,
    method: str = "reg",
    quantile: float = 0.80,
    window: int = 500,
    daily: bool = True,
    cache: dict[str, tuple[str, float]] | None = None,
    key: str = "",
) -> float:
    """给当前 K 线算自适应阈值；daily=True 时同一交易日只算一次并缓存。

    缓存按 key（通常传合约）分开；同一交易日内直接复用，省掉逐根重算的角度序列。
    日盘、夜盘按期货交易日划分：夜盘 21:00 起算下一交易日，冻结到次日 20:59。
    """
    day = _trading_day_key(bars) if daily else ""
    if daily and cache is not None and day:
        hit = cache.get(key)
        if hit is not None and hit[0] == day:
            return hit[1]
    close = pd.to_numeric(bars["close"], errors="coerce")
    high = pd.to_numeric(bars["high"], errors="coerce") if "high" in bars.columns else close
    low = pd.to_numeric(bars["low"], errors="coerce") if "low" in bars.columns else close
    atr_s = average_true_range(high, low, close, 14)
    angles = slope_angle_series(ema, atr_s, lookback=lookback, method=method)
    value = adaptive_angle_threshold(
        angles,
        base_deg,
        quantile=quantile,
        window=window,
    )
    if daily and cache is not None and day:
        cache[key] = (day, float(value))
    return float(value)


def evaluate_slope(
    measure: SlopeMeasure | None,
    config: SlopeConfig,
    *,
    side: str | None = None,
    close: float | None = None,
    ema_value: float | None = None,
    prev_direction: int = 0,
) -> SlopeState:
    """把一次测量翻译成三态。

    side 给定时，只有与该方向一致的斜率才放行；exit_deg 与 entry_deg 之间的
    中间带保持 prev_direction，形成滞回，避免阈值附近反复开关。
    """
    hi = abs(float(config.entry_deg))
    lo = abs(float(config.exit_deg)) if config.exit_deg is not None else hi
    if measure is None:
        return SlopeState(0, 0.0, hi, "unknown", float("nan"), float("nan"), "斜率还没算稳")
    a = measure.angle_deg
    if config.min_t > 0 and (math.isnan(measure.t_stat) or abs(measure.t_stat) < config.min_t):
        return SlopeState(
            0,
            a,
            hi,
            "flat",
            measure.t_stat,
            measure.r2,
            f"斜率 t 值 {measure.t_stat:.2f}，低于 {config.min_t:g}，当震荡",
        )
    if config.min_r2 > 0 and measure.r2 < config.min_r2:
        return SlopeState(
            0,
            a,
            hi,
            "flat",
            measure.t_stat,
            measure.r2,
            f"拟合 R² {measure.r2:.2f}，低于 {config.min_r2:g}，当震荡",
        )
    base = 1 if a >= hi else -1 if a <= -hi else 0
    if base:
        direction = base
        regime = "up" if base > 0 else "down"
    elif abs(a) < lo:
        direction = 0
        regime = "flat"
    else:
        direction = int(prev_direction)
        regime = "neutral"
    reason = f"斜率 {a:.1f}°"
    if direction and config.price_side and close is not None and ema_value is not None:
        if (direction > 0 and close <= ema_value) or (direction < 0 and close >= ema_value):
            return SlopeState(
                0,
                a,
                hi,
                "flat",
                measure.t_stat,
                measure.r2,
                f"斜率 {a:.1f}°，但价格 {close:.2f} 不在均线 {ema_value:.2f} "
                f"{'上方' if direction > 0 else '下方'}，不算趋势",
            )
    if side is not None:
        want = 1 if side == "long" else -1
        if direction != want:
            return SlopeState(
                0,
                a,
                hi,
                "flat",
                measure.t_stat,
                measure.r2,
                f"斜率 {a:.1f}°，不够{'往上' if want > 0 else '往下'}（要 "
                f"{'≥' if want > 0 else '≤'} {want * hi:g}°），当震荡",
            )
    return SlopeState(direction, a, hi, regime, measure.t_stat, measure.r2, reason)


class SlopeFilter:
    """配置好的斜率过滤器，供各策略复用。"""

    def __init__(self, config: SlopeConfig | None = None) -> None:
        self.config = config or SlopeConfig()

    def measure(self, ema: pd.Series, atr: float) -> SlopeMeasure | None:
        return measure_slope(
            ema,
            atr,
            lookback=self.config.lookback,
            method=self.config.method,
        )

    def evaluate(
        self,
        ema: pd.Series,
        atr: float,
        *,
        side: str | None = None,
        close: float | None = None,
        prev_direction: int = 0,
    ) -> SlopeState:
        measure = self.measure(ema, atr)
        ema_value = float(ema.iloc[-1]) if len(ema) else None
        return evaluate_slope(
            measure,
            self.config,
            side=side,
            close=close,
            ema_value=ema_value,
            prev_direction=prev_direction,
        )


@dataclass(frozen=True)
class SlopeGateConfig:
    """可选的斜率环境门。enabled=False 时完全不影响原策略。"""

    enabled: bool = False
    ema_period: int = 120
    lookback: int = 20
    method: str = "reg"
    min_deg: float = 3.0
    min_t: float = 0.0
    price_side: bool = False
    adaptive: bool = False
    adaptive_quantile: float = 0.80
    adaptive_window: int = 500
    adaptive_daily: bool = True

    @property
    def need_bars(self) -> int:
        return max(2, self.ema_period) + max(2, self.lookback) + 2


def _flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开", "是"}
    return bool(value)


def load_slope_gate(box: Mapping[str, Any] | None, key: str = "slope_gate") -> SlopeGateConfig:
    """从策略小节里读 slope_gate；没写这一节就返回关闭状态。"""
    raw = box.get(key) if isinstance(box, Mapping) else None
    if not isinstance(raw, Mapping):
        return SlopeGateConfig()
    method = str(raw.get("method", "reg") or "reg").strip().lower()
    try:
        ema_period = int(float(raw.get("ema_period", 120) or 120))
    except (TypeError, ValueError):
        ema_period = 120
    try:
        lookback = int(float(raw.get("lookback", 20) or 20))
    except (TypeError, ValueError):
        lookback = 20
    try:
        min_deg = abs(float(raw.get("min_deg", 3.0) or 0))
    except (TypeError, ValueError):
        min_deg = 3.0
    try:
        min_t = abs(float(raw.get("min_t", 0.0) or 0))
    except (TypeError, ValueError):
        min_t = 0.0
    try:
        adaptive_quantile = float(raw.get("adaptive_quantile", 0.80) or 0.80)
    except (TypeError, ValueError):
        adaptive_quantile = 0.80
    try:
        adaptive_window = int(float(raw.get("adaptive_window", 500) or 500))
    except (TypeError, ValueError):
        adaptive_window = 500
    return SlopeGateConfig(
        enabled=_flag(raw.get("enabled"), False),
        ema_period=max(2, min(500, ema_period)),
        lookback=max(2, min(120, lookback)),
        method=method if method in {"reg", "diff"} else "reg",
        min_deg=min_deg,
        min_t=min_t,
        price_side=_flag(raw.get("price_side"), False),
        adaptive=_flag(raw.get("adaptive"), False),
        adaptive_quantile=min(max(adaptive_quantile, 0.05), 0.95),
        adaptive_window=max(50, min(5000, adaptive_window)),
        adaptive_daily=_flag(raw.get("adaptive_daily"), True),
    )


class SlopeGate:
    """环境门：方向和斜率不够时拦下开仓，默认关闭。"""

    def __init__(self, config: SlopeGateConfig | None = None) -> None:
        self.config = config or SlopeGateConfig()
        self._daily_cache: dict[str, tuple[str, float]] = {}
        self._filter = SlopeFilter(
            SlopeConfig(
                lookback=self.config.lookback,
                method=self.config.method,
                entry_deg=self.config.min_deg,
                exit_deg=None,
                min_t=self.config.min_t,
                min_r2=0.0,
                price_side=self.config.price_side,
            )
        )

    @property
    def enabled(self) -> bool:
        """min_deg 与 min_t 都是 0 时视同关闭，和 ema_swing 的「0=关」一致。"""
        return self.config.enabled and (self.config.min_deg > 0 or self.config.min_t > 0)

    @property
    def need_bars(self) -> int:
        return self.config.need_bars

    def check(self, bars: pd.DataFrame, side: str, *, symbol: str = "") -> tuple[bool, str]:
        """返回 (是否放行, 原因)。关闭时永远放行，且不碰 K 线。"""
        if not self.enabled:
            return True, ""
        if bars is None or "close" not in bars.columns or len(bars) < self.need_bars:
            return False, "斜率闸门：K线不足，先不开"
        close = pd.to_numeric(bars["close"], errors="coerce")
        ema = ema_series(close, max(2, self.config.ema_period))
        atr = latest_atr(bars, close, 14)
        measure = self._filter.measure(ema, atr)
        cfg = self._filter.config
        eff: float | None = None
        if self.config.adaptive and self.config.min_deg > 0:
            eff = adaptive_threshold_from_bars(
                bars,
                ema,
                base_deg=self.config.min_deg,
                lookback=self.config.lookback,
                method=self.config.method,
                quantile=self.config.adaptive_quantile,
                window=self.config.adaptive_window,
                daily=self.config.adaptive_daily,
                cache=self._daily_cache,
                key=symbol,
            )
            cfg = replace(cfg, entry_deg=eff)
        state = evaluate_slope(
            measure,
            cfg,
            side=side,
            close=float(close.iloc[-1]) if len(close) else None,
            ema_value=float(ema.iloc[-1]) if len(ema) else None,
        )
        reason = state.reason
        if eff is not None:
            reason = f"{reason}（自适应阈值 {eff:.1f}°，基准 {self.config.min_deg:g}°）"
        want = 1 if side == "long" else -1
        if state.direction != want:
            return False, f"斜率闸门：{reason}"
        return True, reason
