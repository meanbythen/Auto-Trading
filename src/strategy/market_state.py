"""
市场状态过滤（ADX + ATR 历史分位）

功能：判断当前行情还适不适合开仓。支持两种模式：
    - trend（默认）：趋势类策略用。ADX 太低（没趋势）或 ATR 太低（波动压缩）时停手。
    - range  ：日内实体用。ADX 太高（已经单边）或 ATR 太极端时停手；
               盘面拧来拧去反而放行。不拦波动压缩。

原理：对应《策略基础逻辑》里的第一条底层规则 —— 「策略有明确的适用行情，行情不匹配直接停手」。

配置已经拆进各策略：
    日内实体 → daily_body.filter_*（区间模式，ADX 过高不开）
    海龟/SuperTrend/双均线/布林/SAR/长线 → 各自小节 filter_*（趋势模式）
    多因子 → cta_filter
    长线箱体突破 → 箱子和 ADX 写在策略里
旧配置里的 strategy.market_filter 只给趋势策略补缺省，不再当总开关。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.data.periods import unique_ohlc_columns

from .indicators import average_true_range, price_1d


MarketFilterMode = Literal["trend", "range"]


def _last_float(series: pd.Series | None) -> float:
    """一列数字的最后一个。格子里挤了多个数就当 0，避免写进 if。"""
    return _float_ago(series, 0)


def _float_ago(series: pd.Series | None, bars_ago: int) -> float:
    """倒数第 bars_ago 根的数。0 就是最新一根。不够长或空格子当 0。"""
    if series is None or len(series) <= int(bars_ago):
        return 0.0
    raw = np.asarray(series.iloc[-(int(bars_ago) + 1)], dtype=float).reshape(-1)
    if raw.size != 1:
        return 0.0
    value = float(raw[0])
    return value if np.isfinite(value) else 0.0


def _as_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def _as_int(value: Any, default: int, *, low: int = 2) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, out)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "关"}
    if isinstance(value, np.ndarray):
        if value.size == 0:
            return default
        return bool(value.reshape(-1)[-1])
    return bool(value)


def _as_mode(value: Any, default: MarketFilterMode = "trend") -> MarketFilterMode:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("trend", "趋势", "trending"):
            return "trend"
        if v in ("range", "区间", "震荡", "breakout"):
            return "range"
    return default


@dataclass(frozen=True)
class MarketFilterParams:
    """市场状态过滤参数。默认值取文档里的统一约定（ADX14 / ATR14 / 回看60 / 20%）。"""

    enabled: bool = True
    mode: MarketFilterMode = "trend"
    adx_period: int = 14
    atr_period: int = 14
    lookback: int = 60
    adx_flat: float = 20.0
    adx_trend: float = 25.0
    adx_upper: float = 30.0
    atr_low_pct: float = 0.2
    atr_high_pct: float = 0.8

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any] | None) -> "MarketFilterParams":
        """从 strategy.market_filter 读；没写就用文档默认值（开启，trend 模式）。"""
        box = ((cfg or {}).get("strategy") or {}).get("market_filter") or {}
        if not isinstance(box, dict):
            box = {}
        atr_low = min(0.9, max(0.01, _as_float(box.get("atr_low_pct"), 0.2)))
        atr_high_raw = _as_float(box.get("atr_high_pct"), 0.8)
        # 0 或负数视为关闭上限。
        if atr_high_raw <= 0:
            atr_high_raw = 1.0
        atr_high = min(1.0, max(atr_low + 0.05, atr_high_raw))
        return cls(
            enabled=_as_bool(box.get("enabled"), True),
            mode=_as_mode(box.get("mode"), "trend"),
            adx_period=_as_int(box.get("adx_period"), 14),
            atr_period=_as_int(box.get("atr_period"), 14),
            lookback=_as_int(box.get("lookback"), 60),
            adx_flat=_as_float(box.get("adx_flat"), 20.0),
            adx_trend=_as_float(box.get("adx_trend"), 25.0),
            adx_upper=_as_float(box.get("adx_upper"), 30.0),
            atr_low_pct=atr_low,
            atr_high_pct=atr_high,
        )

    @classmethod
    def with_mode(cls, base: "MarketFilterParams", mode: MarketFilterMode) -> "MarketFilterParams":
        """复用现有参数只换 mode；给日内实体派生区间过滤用。"""
        if mode == base.mode:
            return base
        return cls(
            enabled=base.enabled,
            mode=mode,
            adx_period=base.adx_period,
            atr_period=base.atr_period,
            lookback=base.lookback,
            adx_flat=base.adx_flat,
            adx_trend=base.adx_trend,
            adx_upper=base.adx_upper,
            atr_low_pct=base.atr_low_pct,
            atr_high_pct=base.atr_high_pct,
        )

    @property
    def need_bars(self) -> int:
        """算出稳定 ADX 和分位至少要多少根 K 线。"""
        return self.lookback + self.adx_period + 2


# 会自己带「趋势模式」ADX/ATR 过滤的策略小节。日内实体、多因子、长线箱体不在这里。
TREND_FILTER_SECTIONS = (
    "trend_follow",
    "supertrend",
    "dual_ma",
    "bollinger",
    "sar_trend",
    "swing_trend",
)

# 引擎名（含旧别名）→ 上面那六个小节。
ENGINE_TREND_SECTION: dict[str, str] = {
    "trend_follow": "trend_follow",
    "supertrend": "supertrend",
    "st": "supertrend",
    "dual_ma": "dual_ma",
    "ma_cross": "dual_ma",
    "bollinger": "bollinger",
    "boll": "bollinger",
    "bollinger_break": "bollinger",
    "sar_trend": "sar_trend",
    "sar": "sar_trend",
    "parabolic_sar": "sar_trend",
    "swing_trend": "swing_trend",
}


def trend_params_for_section(cfg: dict[str, Any] | None, section: str) -> MarketFilterParams:
    """
    趋势策略自己的行情过滤（ADX 太低或波动压缩则不开新仓）。

    数字写在该策略小节的 filter_* 里。没有写时，可暂时沿用旧的公共 market_filter 数字，
    方便旧配置过渡。不看公共开关去拦别的策略。
    """
    st = ((cfg or {}).get("strategy") or {}) if isinstance(cfg, dict) else {}
    raw_box = st.get(section)
    box: dict[str, Any] = raw_box if isinstance(raw_box, dict) else {}
    raw_mf = st.get("market_filter")
    mf: dict[str, Any] = raw_mf if isinstance(raw_mf, dict) else {}
    enabled = box.get("filter_enabled")
    if enabled is None:
        enabled = mf.get("enabled", False)
    if not _as_bool(enabled, False):
        return MarketFilterParams(enabled=False, mode="trend")

    def _pick(body_key: str, mf_key: str, default: Any) -> Any:
        if box.get(body_key) not in (None, ""):
            return box.get(body_key)
        if mf.get(mf_key) not in (None, ""):
            return mf.get(mf_key)
        return default

    packed = {
        "enabled": True,
        "mode": "trend",
        "adx_period": _pick("filter_adx_period", "adx_period", 14),
        "atr_period": _pick("filter_atr_period", "atr_period", 14),
        "lookback": _pick("filter_lookback", "lookback", 60),
        "adx_flat": _pick("filter_adx_flat", "adx_flat", 20),
        "adx_trend": _pick("filter_adx_trend", "adx_trend", 25),
        "atr_low_pct": _pick("filter_atr_low_pct", "atr_low_pct", 0.2),
        "adx_upper": 80,
        "atr_high_pct": 1.0,
    }
    return MarketFilterParams.from_cfg({"strategy": {"market_filter": packed}})


def ensure_trend_filter_defaults(box: dict[str, Any], mf: dict[str, Any] | None = None) -> dict[str, Any]:
    """给趋势策略小节补上自己的过滤键。只补缺，不改已经写过的。"""
    seed = mf if isinstance(mf, dict) else {}
    if "filter_enabled" not in box:
        box["filter_enabled"] = bool(seed.get("enabled", False))
    box.setdefault("filter_adx_period", int(seed.get("adx_period") or 14))
    box.setdefault("filter_atr_period", int(seed.get("atr_period") or 14))
    box.setdefault("filter_lookback", int(seed.get("lookback") or 60))
    box.setdefault("filter_adx_flat", float(seed.get("adx_flat") or 20))
    atr_low = seed.get("atr_low_pct")
    try:
        atr_low_f = float(atr_low) if atr_low not in (None, "") else 0.2
    except (TypeError, ValueError):
        atr_low_f = 0.2
    if atr_low_f > 1:
        atr_low_f = atr_low_f / 100.0
    box.setdefault("filter_atr_low_pct", atr_low_f)
    return box


def _daily_body_filter_enabled(body: dict[str, Any], default: bool = True) -> bool:
    """日内实体过滤开关：界面写 filter_enabled；旧配置/测试里的 regime_filter 仍认。"""
    if "filter_enabled" in body:
        return _as_bool(body.get("filter_enabled"), default)
    if "regime_filter" in body:
        return _as_bool(body.get("regime_filter"), default)
    return bool(default)


def _daily_body_adx_upper(body: dict[str, Any], mf: dict[str, Any]) -> float:
    """日内实体区间过滤：ADX 上限。0=不看这一条。默认 30。"""
    if body.get("filter_adx_upper") not in (None, ""):
        return max(0.0, _as_float(body.get("filter_adx_upper"), 30.0))
    if mf.get("adx_upper") not in (None, ""):
        return max(0.0, _as_float(mf.get("adx_upper"), 30.0))
    return 30.0


def range_params_for_opening_break(
    cfg: dict[str, Any] | None,
    section: str = "daily_body",
    *,
    default_enabled: bool = True,
) -> MarketFilterParams:
    """
    开盘箱体类策略共用的区间过滤。

    ADX 高于上限当成已经单边，禁止开仓；盘面拧来拧去反而放行。
    ATR 太极端仍可停手，不拦波动压缩。
    数字读对应小节的 filter_*。开关是 filter_enabled。
    """
    st = ((cfg or {}).get("strategy") or {}) if isinstance(cfg, dict) else {}
    raw = st.get(section)
    body: dict[str, Any] = raw if isinstance(raw, dict) else {}
    raw_mf = st.get("market_filter")
    mf: dict[str, Any] = raw_mf if isinstance(raw_mf, dict) else {}
    if not _daily_body_filter_enabled(body, default_enabled):
        return MarketFilterParams(enabled=False, mode="range")

    def _pick(body_key: str, mf_key: str, default: Any) -> Any:
        if body.get(body_key) not in (None, ""):
            return body.get(body_key)
        if mf.get(mf_key) not in (None, ""):
            return mf.get(mf_key)
        return default

    box = {
        "enabled": True,
        "mode": "range",
        "adx_period": _pick("filter_adx_period", "adx_period", 14),
        "atr_period": _pick("filter_atr_period", "atr_period", 14),
        "lookback": _pick("filter_lookback", "lookback", 60),
        "adx_upper": _daily_body_adx_upper(body, mf),
        "atr_high_pct": _pick("filter_atr_high_pct", "atr_high_pct", 0.8),
        "atr_low_pct": 0.2,
        "adx_flat": 20,
        "adx_trend": 25,
    }
    return MarketFilterParams.with_mode(
        MarketFilterParams.from_cfg({"strategy": {"market_filter": box}}),
        "range",
    )


def range_params_for_daily_body(cfg: dict[str, Any] | None) -> MarketFilterParams:
    """日内实体：过滤默认开。"""
    return range_params_for_opening_break(cfg, "daily_body", default_enabled=True)


@dataclass(frozen=True)
class MarketState:
    """一次市场状态判断的结果。"""

    ok: bool
    adx: float
    atr: float
    atr_rank: float
    plus_di: float
    minus_di: float
    reason: str
    mode: str = "trend"

    @property
    def is_trend(self) -> bool:
        return self.adx >= 25.0

    def describe(self) -> str:
        """给日志/界面用的一行说明。"""
        mood = "趋势" if self.adx >= 25.0 else ("偏弱" if self.adx >= 20.0 else "震荡")
        tag = "R" if self.mode == "range" else "T"
        return (
            f"[{tag}] ADX={self.adx:.1f}({mood}) "
            f"ATR分位={self.atr_rank:.0%} "
            f"DI+={self.plus_di:.0f}/DI-={self.minus_di:.0f}"
        )


def adx_series(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Wilder ADX，返回 (ADX, +DI, -DI)。

    和 TA-Lib / 通达信口径一致：先对 TR、+DM、-DM 做 Wilder 平滑，
    再算 DX，最后对 DX 再做一次 Wilder 平滑得到 ADX。
    """
    high_f = price_1d(high)
    low_f = price_1d(low)
    close_f = price_1d(close)
    prev_close = close_f.shift(1)
    up_move = np.asarray(high_f.diff(), dtype=float).reshape(-1)
    down_move = np.asarray(-low_f.diff(), dtype=float).reshape(-1)
    if up_move.size != down_move.size:
        n = min(up_move.size, down_move.size)
        up_move = up_move[-n:]
        down_move = down_move[-n:]
        high_f = high_f.iloc[-n:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat(
        [(high_f - low_f), (high_f - prev_close).abs(), (low_f - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    n = max(2, int(period))
    alpha = 1.0 / n
    tr_smooth = tr.ewm(alpha=alpha, adjust=False, min_periods=n).mean()
    plus_smooth = pd.Series(plus_dm, index=high_f.index).ewm(
        alpha=alpha, adjust=False, min_periods=n
    ).mean()
    minus_smooth = pd.Series(minus_dm, index=high_f.index).ewm(
        alpha=alpha, adjust=False, min_periods=n
    ).mean()

    plus_di = (100.0 * plus_smooth / tr_smooth).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    minus_di = (100.0 * minus_smooth / tr_smooth).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    total = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / total).where(total > 0, 0.0)
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=n).mean()
    return adx, plus_di, minus_di


def atr_percent_rank(atr: pd.Series, lookback: int = 60) -> float:
    """
    当前 ATR 在最近 lookback 根里的百分位（0~100%）。

    用真实排序而不是简单 (x-min)/(max-min)：后者遇到一根极端大 ATR 会把其余都压到很低。
    """
    window = atr.tail(max(2, int(lookback))).dropna()
    if window.empty:
        return 1.0
    raw = np.asarray(window.iloc[-1], dtype=float).reshape(-1)
    if raw.size != 1 or not np.isfinite(float(raw[0])):
        return 1.0
    current = float(raw[0])
    return float((window <= current).mean())


def _market_state_from_values(
    params: MarketFilterParams,
    adx_now: float,
    atr_now: float,
    rank: float,
    di_plus: float,
    di_minus: float,
) -> MarketState:
    """指标值已经有了，只按 trend/range 两套规则决定放行还是停手。"""
    reasons: list[str] = []
    if params.mode == "trend":
        # 趋势模式：ADX 太低（没趋势）或 ATR 太低（波动压缩）→ 停手
        if adx_now < params.adx_flat:
            reasons.append(f"ADX={adx_now:.1f}<{params.adx_flat:.0f} 没有趋势")
        if rank <= params.atr_low_pct:
            reasons.append(f"ATR处于{rank:.0%}分位，低于{params.atr_low_pct:.0%} 波动压缩")
        ok = not reasons
        ok_reason = "行情适合趋势策略"
    else:
        # 主推日内实体：ADX 已经很高说明单边走完一截，再追箱体突破容易被带走。
        # 盘面拧来拧去反而放行。不拦波动压缩。极端波动仍可拦滑价。
        upper = max(0.0, float(params.adx_upper or 0))
        if upper > 0 and adx_now > upper:
            reasons.append(
                f"ADX={adx_now:.1f}>{upper:.0f} 强单边，箱体突破容易被趋势带走"
            )
        if rank > params.atr_high_pct:
            reasons.append(
                f"ATR处于{rank:.0%}分位，高于{params.atr_high_pct:.0%} 极端波动，跳空多止损易滑价"
            )
        ok = not reasons
        ok_reason = "行情适合箱体突破"

    if not ok:
        return MarketState(
            False,
            adx_now,
            atr_now,
            rank,
            di_plus,
            di_minus,
            "；".join(reasons) + "，停手不开新仓",
            mode=params.mode,
        )
    return MarketState(True, adx_now, atr_now, rank, di_plus, di_minus, ok_reason, mode=params.mode)


_OHLC_NAMES = ("high", "low", "close")


def _ohlc_float_arrays(
    bars: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """把高开低收取成三列 float，已有的 float64 列不复制。取不出来时返回 None。"""
    # K 线表通常整张都是数字。一次 to_numpy 拿到整块数据再按列切，比逐列
    # bars[name] 快得多；切片只是视图，不会额外复制。有字符串列等杂项时退回逐列取。
    columns = bars.columns
    try:
        positions = [columns.get_loc(name) for name in _OHLC_NAMES]
    except KeyError:
        return None
    matrix = bars.to_numpy()
    if (
        matrix.ndim == 2
        and matrix.shape[0] == len(bars)
        and matrix.dtype.kind in "fiu"
    ):
        try:
            matrix = np.asarray(matrix, dtype=float)
        except (TypeError, ValueError):
            return None
        picks: list[np.ndarray] = []
        for position in positions:
            # 重名列时 get_loc 返回切片或位置数组，取最后一列，和原来的 iloc[:, -1] 一致。
            if isinstance(position, slice):
                index = position.stop - 1
            elif isinstance(position, np.ndarray):
                # 重名列时 pandas 给的是布尔掩码（老版本给位置数组）。
                if position.dtype == bool:
                    index = int(np.flatnonzero(position)[-1])
                else:
                    index = int(position[-1])
            else:
                index = position
            picks.append(matrix[:, index])
        return picks[0], picks[1], picks[2]

    arrays: list[np.ndarray] = []
    for name in _OHLC_NAMES:
        column = bars[name]
        if isinstance(column, pd.DataFrame):
            column = column.iloc[:, -1]
        try:
            # 已经是 float64 时这里不复制；整数列才转一次；不是数字就退回整段重算。
            values = np.asarray(column.to_numpy(), dtype=float).reshape(-1)
        except (TypeError, ValueError):
            return None
        if values.size != len(bars):
            return None
        arrays.append(values)
    return arrays[0], arrays[1], arrays[2]


def _all_finite(*arrays: np.ndarray) -> bool:
    for array in arrays:
        # 回测每根通常只多一格，整列 isfinite 的固定开销比逐个数还大。
        if array.size <= 16:
            if not all(-np.inf < value < np.inf for value in array.tolist()):
                return False
        elif not np.isfinite(array).all():
            return False
    return True


class _MarketBuffer:
    """
    接着上一根继续算 ADX / ATR，给 MarketFilter 复用。

    口径和 adx_series / average_true_range 一致（Wilder 平滑 + 简单 ATR），
    但把回测里「每推进一根就从头重算整段」换成「只算新加的那几根」。
    只接受「同一段前缀继续往后加」的调用；数据换了就 reset 后整段重来。
    """

    __slots__ = (
        "period", "atr_period", "lookback", "alpha", "n",
        "prev_high", "prev_low", "prev_close",
        "tr_smooth", "plus_smooth", "minus_smooth", "dx_smooth",
        "tr_window", "atr_window", "high", "low", "close",
        "adx", "plus_di", "minus_di", "atr",
    )

    def __init__(self, params: MarketFilterParams) -> None:
        self.period = max(2, int(params.adx_period))
        self.atr_period = max(2, int(params.atr_period))
        self.lookback = max(2, int(params.lookback))
        self.alpha = 1.0 / self.period
        self.tr_window: deque[float] = deque(maxlen=self.atr_period)
        self.atr_window: deque[float] = deque(maxlen=self.lookback)
        self.reset()

    def reset(self) -> None:
        self.n = 0
        self.prev_high = 0.0
        self.prev_low = 0.0
        self.prev_close = 0.0
        self.tr_smooth = 0.0
        self.plus_smooth = 0.0
        self.minus_smooth = 0.0
        self.dx_smooth = 0.0
        self.tr_window.clear()
        self.atr_window.clear()
        self.high = np.empty(0, dtype=float)
        self.low = np.empty(0, dtype=float)
        self.close = np.empty(0, dtype=float)
        self.adx = 0.0
        self.plus_di = 0.0
        self.minus_di = 0.0
        self.atr = 0.0

    def matches(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> bool:
        """新送进来的这段是不是「上次那段 + 尾巴」。"""
        n = self.n
        return (
            n > 0
            and high.size >= n
            and np.array_equal(self.high[:n], high[:n])
            and np.array_equal(self.low[:n], low[:n])
            and np.array_equal(self.close[:n], close[:n])
        )

    def _grow(self, size: int) -> None:
        """存 K 线的数组不够长时一次扩到位，够用就不动。"""
        if size <= self.high.size:
            return
        capacity = max(size, 64, self.high.size * 2)
        for name in ("high", "low", "close"):
            old = getattr(self, name)
            grown = np.empty(capacity, dtype=float)
            grown[: old.size] = old
            setattr(self, name, grown)

    def feed(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> None:
        start = self.n
        stop = high.size
        if stop <= start:
            return
        # 先扩容再算，避免每加一根就把整段 np.concatenate 一遍。
        self._grow(stop)
        for i in range(start, stop):
            self._step(float(high[i]), float(low[i]), float(close[i]))
        self.high[start:stop] = high[start:stop]
        self.low[start:stop] = low[start:stop]
        self.close[start:stop] = close[start:stop]

    def rank(self) -> float:
        """最新 ATR 在最近 lookback 根里的百分位，算法同 atr_percent_rank。"""
        window = np.fromiter(self.atr_window, dtype=float, count=len(self.atr_window))
        window = window[window == window]
        if window.size == 0:
            return 1.0
        return float(np.mean(window <= window[-1]))

    def _step(self, high: float, low: float, close: float) -> None:
        alpha = self.alpha
        if self.n == 0:
            plus_dm = minus_dm = 0.0
            tr = high - low
            self.tr_smooth = tr
            self.plus_smooth = plus_dm
            self.minus_smooth = minus_dm
        else:
            up = high - self.prev_high
            down = self.prev_low - low
            plus_dm = up if (up > down and up > 0.0) else 0.0
            minus_dm = down if (down > up and down > 0.0) else 0.0
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
            self.tr_smooth = tr * alpha + self.tr_smooth * (1.0 - alpha)
            self.plus_smooth = plus_dm * alpha + self.plus_smooth * (1.0 - alpha)
            self.minus_smooth = minus_dm * alpha + self.minus_smooth * (1.0 - alpha)

        self.tr_window.append(tr)
        if len(self.tr_window) >= self.atr_period:
            atr = float(np.mean(np.fromiter(self.tr_window, dtype=float, count=self.atr_period)))
        else:
            atr = float("nan")
        self.atr_window.append(atr)

        # 没凑满 period 根时 pandas 的 DI 是 NaN，后面 fillna(0) 当 0 用。
        if self.n >= self.period - 1 and self.tr_smooth > 0.0:
            plus_di = 100.0 * self.plus_smooth / self.tr_smooth
            minus_di = 100.0 * self.minus_smooth / self.tr_smooth
        else:
            plus_di = minus_di = 0.0
        total = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / total if total > 0.0 else 0.0
        self.dx_smooth = dx if self.n == 0 else dx * alpha + self.dx_smooth * (1.0 - alpha)

        self.plus_di = plus_di
        self.minus_di = minus_di
        self.adx = self.dx_smooth if self.n >= self.period - 1 else float("nan")
        self.atr = atr
        self.prev_high = high
        self.prev_low = low
        self.prev_close = close
        self.n += 1


def evaluate_market_state(
    bars: pd.DataFrame,
    params: MarketFilterParams,
) -> MarketState:
    """
    用已经收盘的 K 线判断现在能不能开新仓。

    数据不够时按「允许」处理（早期不拦），并在 reason 里写明，方便日志排查。
    每次调用都整段重算；回测逐根推进走 MarketFilter.check 的增量缓存。
    """
    if not params.enabled:
        return MarketState(True, 0.0, 0.0, 1.0, 0.0, 0.0, "市场过滤已关闭", mode=params.mode)

    bars = unique_ohlc_columns(bars)
    need = params.adx_period + 5
    if bars is None or len(bars) < need or not {"high", "low", "close"} <= set(bars.columns):
        return MarketState(True, 0.0, 0.0, 1.0, 0.0, 0.0, "K线不足，暂不限制开仓", mode=params.mode)

    adx, plus_di, minus_di = adx_series(
        bars["high"], bars["low"], bars["close"], period=params.adx_period
    )
    atr = average_true_range(
        bars["high"], bars["low"], bars["close"], period=params.atr_period
    )
    return _market_state_from_values(
        params,
        _last_float(adx),
        _last_float(atr),
        atr_percent_rank(atr, params.lookback),
        _last_float(plus_di),
        _last_float(minus_di),
    )


class MarketFilter:
    """策略持有的过滤器：读一次配置，每根 K 线判一次。"""

    def __init__(self, params: MarketFilterParams) -> None:
        self.params = params
        self._buffer = _MarketBuffer(params)

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any] | None) -> "MarketFilter":
        return cls(MarketFilterParams.from_cfg(cfg))

    @classmethod
    def from_cfg_with_mode(
        cls, cfg: dict[str, Any] | None, mode: MarketFilterMode
    ) -> "MarketFilter":
        """从配置读默认参数，只换 mode；给不同策略派生各自模式用。"""
        base = MarketFilterParams.from_cfg(cfg)
        return cls(MarketFilterParams.with_mode(base, mode))

    @property
    def enabled(self) -> bool:
        return self.params.enabled

    @property
    def mode(self) -> str:
        return self.params.mode

    def check(self, bars: pd.DataFrame) -> MarketState:
        """
        判这一段 K 线的行情。回测每根往前推一格时，只算新加的那一根。

        数据不是「上次那段 + 尾巴」（换了品种、K 线被改写、有空格子）时，
        退回 evaluate_market_state 整段重算，结果和原来逐根重算完全一致。
        """
        params = self.params
        if not params.enabled:
            return MarketState(True, 0.0, 0.0, 1.0, 0.0, 0.0, "市场过滤已关闭", mode=params.mode)

        bars = unique_ohlc_columns(bars)
        need = params.adx_period + 5
        if bars is None or len(bars) < need or not {"high", "low", "close"} <= set(bars.columns):
            return MarketState(True, 0.0, 0.0, 1.0, 0.0, 0.0, "K线不足，暂不限制开仓", mode=params.mode)

        arrays = _ohlc_float_arrays(bars)
        if arrays is None:
            return evaluate_market_state(bars, params)
        high, low, close = arrays
        buffer = self._buffer
        start = buffer.n if buffer.matches(high, low, close) else 0
        # 只检查新加的那几根；老前缀上次已经验过，而且刚确认逐位相同。
        if start == 0:
            buffer.reset()
        if not _all_finite(high[start:], low[start:], close[start:]):
            buffer.reset()
            return evaluate_market_state(bars, params)
        buffer.feed(high, low, close)
        return _market_state_from_values(
            params,
            buffer.adx if np.isfinite(buffer.adx) else 0.0,
            buffer.atr if np.isfinite(buffer.atr) else 0.0,
            buffer.rank(),
            buffer.plus_di,
            buffer.minus_di,
        )


def score_market_state(
    bars: pd.DataFrame,
    params: MarketFilterParams,
) -> float:
    """
    把 evaluate_market_state 的 ok/不ok 扩展成 0~100 的连续分数。

    用于交叉测试的「当下行情」列：同一个关注列表里，分数高的合约/策略组合更适配当下行情，
    分数低的排后面甚至跳过。

    规则（和 evaluate_market_state 的 blocking 逻辑一一对应）：
        trend 模式：
            - ADX 越高（趋势越强）分数越高，封顶 50
            - ATR 分位越高（波动够）分数越高，封顶 50
        range 模式（日内实体）：
            - ADX 越低（还没单边）分数越高，封顶 50
            - ATR 分位在 [atr_low_pct, atr_high_pct] 区间里给满分，两端衰减，封顶 50

    数据不足或过滤器关闭时返回 50（中性分，不偏向任何一边）。
    """
    if not params.enabled:
        return 50.0

    need = params.adx_period + 5
    if bars is None or len(bars) < need or not {"high", "low", "close"} <= set(bars.columns):
        return 50.0

    adx, _plus_di, _minus_di = adx_series(
        bars["high"], bars["low"], bars["close"], period=params.adx_period
    )
    atr = average_true_range(
        bars["high"], bars["low"], bars["close"], period=params.atr_period
    )
    adx_now = _last_float(adx)
    atr_rank = atr_percent_rank(atr, params.lookback)
    if not np.isfinite(atr_rank):
        atr_rank = 0.5

    if params.mode == "trend":
        # ADX: 0~50 线性映射到 0~50 分
        adx_score = min(50.0, max(0.0, adx_now))
        # ATR rank: 0~1 线性映射到 0~50 分
        atr_score = max(0.0, min(1.0, atr_rank)) * 50.0
        return adx_score + atr_score
    else:
        # 区间突破：ADX 越低越好（还没走出单边）。
        adx_score = max(0.0, 50.0 - min(50.0, adx_now))

    # range 的 ATR 打分。
    lo = float(params.atr_low_pct)
    hi = float(params.atr_high_pct) if params.atr_high_pct < 1.0 else 1.0
    if atr_rank <= lo:
        atr_score = 50.0 * (atr_rank / max(lo, 0.01))
    elif atr_rank >= hi:
        if hi >= 1.0:
            atr_score = 50.0
        else:
            excess = atr_rank - hi
            span = max(0.001, 1.0 - hi)
            atr_score = max(0.0, 50.0 * (1.0 - excess / span))
    else:
        atr_score = 50.0
    return adx_score + atr_score


def market_block_reason(state: MarketState) -> str | None:
    """状态不允许开仓时返回一句原因，允许时返回 None。"""
    return None if state.ok else state.reason
