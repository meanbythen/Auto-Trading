"""
市场状态过滤（ADX + ATR 历史分位）

功能：判断当前行情还适不适合开仓。支持两种模式：
    - trend（默认）：趋势类策略用。ADX 太低（没趋势）或 ATR 太低（波动压缩）时停手。
    - range  ：区间/突破类策略用（如日内实体）。ADX 太高（强单边接逆势）或 ATR 太宽时停手。
               开盘箱子最需要的就是相对平静，不再用「波动压缩」去拦。

原理：对应《策略基础逻辑》里的第一条底层规则 —— 「策略有明确的适用行情，行情不匹配直接停手」。

配置已经拆进各策略：
    日内实体 → daily_body.filter_*（区间模式）
    海龟/SuperTrend/双均线/布林/SAR/长线 → 各自小节 filter_*（趋势模式）
    多因子 → cta_filter
    早盘箱体、Jesse → 不走这一层
旧配置里的 strategy.market_filter 只给趋势策略补缺省，不再当总开关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.data.periods import unique_ohlc_columns

from .indicators import average_true_range, price_1d


MarketFilterMode = Literal["trend", "range"]


def _last_float(series: pd.Series | None) -> float:
    """一列数字的最后一个。格子里挤了多个数就当 0，避免写进 if。"""
    if series is None or len(series) == 0:
        return 0.0
    raw = np.asarray(series.iloc[-1], dtype=float).reshape(-1)
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
        """复用现有参数只换 mode；给日内实体在 trend 全局配置上派生 range 模式用。"""
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


# 会自己带「趋势模式」ADX/ATR 过滤的策略小节。日内实体、多因子、早盘、Jesse 不在这里。
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
    box = st.get(section) if isinstance(st.get(section), dict) else {}
    mf = st.get("market_filter") if isinstance(st.get("market_filter"), dict) else {}
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


def _daily_body_filter_enabled(body: dict[str, Any]) -> bool:
    """日内实体过滤开关：界面写 filter_enabled；旧配置/测试里的 regime_filter 仍认。"""
    if "filter_enabled" in body:
        return _as_bool(body.get("filter_enabled"), True)
    if "regime_filter" in body:
        return _as_bool(body.get("regime_filter"), True)
    return True


def range_params_for_daily_body(cfg: dict[str, Any] | None) -> MarketFilterParams:
    """
    日内实体自带的区间过滤。

    ADX 太高、ATR 太极端才停手，不拦波动压缩。数字读 daily_body.filter_*。
    开关是 daily_body.filter_enabled（默认开）。旧键 regime_filter=关 仍整段关掉。
    """
    st = ((cfg or {}).get("strategy") or {}) if isinstance(cfg, dict) else {}
    body = st.get("daily_body") if isinstance(st.get("daily_body"), dict) else {}
    mf = st.get("market_filter") if isinstance(st.get("market_filter"), dict) else {}
    if not _daily_body_filter_enabled(body):
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
        "adx_upper": _pick("filter_adx_upper", "adx_upper", 30),
        "atr_high_pct": _pick("filter_atr_high_pct", "atr_high_pct", 0.8),
        "atr_low_pct": 0.2,
        "adx_flat": 20,
        "adx_trend": 25,
    }
    return MarketFilterParams.with_mode(
        MarketFilterParams.from_cfg({"strategy": {"market_filter": box}}),
        "range",
    )


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


def evaluate_market_state(
    bars: pd.DataFrame,
    params: MarketFilterParams,
) -> MarketState:
    """
    用已经收盘的 K 线判断现在能不能开新仓。

    数据不够时按「允许」处理（早期不拦），并在 reason 里写明，方便日志排查。
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
    adx_now = _last_float(adx)
    atr_now = _last_float(atr)
    rank = atr_percent_rank(atr, params.lookback)
    di_plus = _last_float(plus_di)
    di_minus = _last_float(minus_di)

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
        # 区间模式（日内实体）：怕强单边里接刀、怕极端波动滑价。
        # 不拦「波动压缩」：开盘箱子正是相对平静时才画得清楚。
        if params.adx_upper > 0 and adx_now > params.adx_upper:
            reasons.append(
                f"ADX={adx_now:.1f}>{params.adx_upper:.0f} 强单边，箱体突破易接逆势"
            )
        if rank > params.atr_high_pct:
            reasons.append(
                f"ATR处于{rank:.0%}分位，高于{params.atr_high_pct:.0%} 极端波动，跳空多止损易滑价"
            )
        ok = not reasons
        ok_reason = "行情适合区间/突破策略"

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


class MarketFilter:
    """策略持有的过滤器：读一次配置，每根 K 线判一次。"""

    def __init__(self, params: MarketFilterParams) -> None:
        self.params = params

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
        return evaluate_market_state(bars, self.params)


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
        range 模式：
            - ADX 越低（越震荡）分数越高，封顶 50
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
        # range 模式：ADX 越低越好。15 以下给 50 分，55 以上给 0 分，中间线性
        adx_score = max(0.0, min(50.0, 50.0 - (adx_now - 15.0) * (50.0 / 40.0)))
        # ATR rank: 在 [low, high] 区间里给 50 分，区间外线性衰减
        lo = float(params.atr_low_pct)
        hi = float(params.atr_high_pct) if params.atr_high_pct < 1.0 else 1.0
        if atr_rank <= lo:
            atr_score = 50.0 * (atr_rank / max(lo, 0.01))
        elif atr_rank >= hi:
            # hi=1.0 说明关闭上限，rank 在 lo~1.0 都给 50 分
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
