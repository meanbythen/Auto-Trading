"""
多因子自己的行情闸门（ADX + ATR 分位 + 弱趋势加严）

功能：加权打分自己带一套过滤，写在 `strategy.multi_factor.cta_filter` 里，
     不跟趋势策略的 filter_* 混用。
原理：资料里的顶层开关是「先看市场适不适合做趋势，再看因子」。
     ADX < 20 或波动缩在最低 20% → 直接不开；
     ADX 20～25 算偏弱：加权版要更高总分，并且仓位打成数。
     因子分歧仍用原来的上限，但挂在这组开关下面，关掉总开关就一起停。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .market_state import adx_series, atr_percent_rank, _as_bool, _as_float, _as_int
from .indicators import average_true_range


def default_cta_filter(kind: str) -> dict[str, Any]:
    """加权打分的出厂数字。"""
    base: dict[str, Any] = {
        "enabled": True,
        "use_adx": True,
        "use_atr": True,
        "use_disagree": True,
        "adx_period": 14,
        "adx_flat": 20.0,
        "adx_trend": 25.0,
        "atr_lookback": 60,
        "atr_low_pct": 0.2,
        "weak_score": 0.75,
        "weak_size": 0.5,
        "weak_votes": 3,
    }
    # 加权打分用总分和仓位成数，弱趋势不加票门槛。
    if kind == "multi_factor":
        base["weak_votes"] = 0
    return base


@dataclass(frozen=True)
class CtaFilterParams:
    """从 strategy.multi_factor 的 cta_filter 读出来。"""

    enabled: bool = True
    use_adx: bool = True
    use_atr: bool = True
    use_disagree: bool = True
    adx_period: int = 14
    adx_flat: float = 20.0
    adx_trend: float = 25.0
    atr_lookback: int = 60
    atr_low_pct: float = 0.2
    weak_score: float = 0.75
    weak_size: float = 0.5
    weak_votes: int = 3

    @classmethod
    def from_box(cls, box: dict[str, Any] | None, *, kind: str = "multi_factor") -> "CtaFilterParams":
        """策略小节里没有 cta_filter 时，用这一套的出厂值。"""
        raw = box if isinstance(box, dict) else {}
        filt = raw.get("cta_filter") if isinstance(raw.get("cta_filter"), dict) else {}
        seed = default_cta_filter(kind)
        merged = {**seed, **filt}
        atr_low = min(0.9, max(0.01, _as_float(merged.get("atr_low_pct"), 0.2)))
        weak_size = min(1.0, max(0.1, _as_float(merged.get("weak_size"), seed["weak_size"])))
        weak_score = min(0.95, max(0.0, _as_float(merged.get("weak_score"), seed["weak_score"])))
        return cls(
            enabled=_as_bool(merged.get("enabled"), True),
            use_adx=_as_bool(merged.get("use_adx"), True),
            use_atr=_as_bool(merged.get("use_atr"), True),
            use_disagree=_as_bool(merged.get("use_disagree"), True),
            adx_period=_as_int(merged.get("adx_period"), 14),
            adx_flat=_as_float(merged.get("adx_flat"), 20.0),
            adx_trend=_as_float(merged.get("adx_trend"), 25.0),
            atr_lookback=_as_int(merged.get("atr_lookback"), 60),
            atr_low_pct=atr_low,
            weak_score=weak_score,
            weak_size=weak_size,
            weak_votes=max(0, int(merged.get("weak_votes", seed["weak_votes"]) or 0)),
        )

    @property
    def need_bars(self) -> int:
        """ADX 和 ATR 分位都要够根数才算得稳。"""
        if not self.enabled:
            return 0
        return self.atr_lookback + self.adx_period + 2

    def to_dict(self) -> dict[str, Any]:
        """写回配置文件时用的一份。"""
        return {
            "enabled": self.enabled,
            "use_adx": self.use_adx,
            "use_atr": self.use_atr,
            "use_disagree": self.use_disagree,
            "adx_period": self.adx_period,
            "adx_flat": self.adx_flat,
            "adx_trend": self.adx_trend,
            "atr_lookback": self.atr_lookback,
            "atr_low_pct": self.atr_low_pct,
            "weak_score": self.weak_score,
            "weak_size": self.weak_size,
            "weak_votes": self.weak_votes,
        }


@dataclass(frozen=True)
class CtaGate:
    """一次闸门判断：能不能开、弱趋势要不要加严、仓位打几成。"""

    ok: bool
    reason: str
    adx: float = 0.0
    atr_rank: float = 1.0
    gray: bool = False
    size_scale: float = 1.0

    def describe(self) -> str:
        if not self.ok:
            return self.reason
        if self.gray:
            return (
                f"ADX={self.adx:.1f}偏弱 ATR分位={self.atr_rank:.0%} "
                f"仓位{self.size_scale:.0%}"
            )
        return f"ADX={self.adx:.1f} ATR分位={self.atr_rank:.0%}"


def gate_from_values(
    adx: float,
    atr_rank: float,
    params: CtaFilterParams,
) -> CtaGate:
    """
    用已经算好的 ADX 和 ATR 分位做判断，方便单测。

    顺序按资料：先否决，再给弱趋势降仓/加严，不拦已有持仓（调用方只在空仓时问一次）。
    """
    if not params.enabled:
        return CtaGate(True, "本策略行情过滤已关闭")

    reasons: list[str] = []
    if params.use_adx and adx < params.adx_flat:
        reasons.append(f"ADX={adx:.1f}<{params.adx_flat:.0f} 没有趋势")
    if params.use_atr and atr_rank <= params.atr_low_pct:
        reasons.append(
            f"ATR处于{atr_rank:.0%}分位，低于{params.atr_low_pct:.0%} 波动压缩"
        )
    if reasons:
        return CtaGate(
            False,
            "；".join(reasons) + "，不开新仓",
            adx=adx,
            atr_rank=atr_rank,
        )

    gray = bool(
        params.use_adx and params.adx_flat <= adx < params.adx_trend
    )
    size = params.weak_size if gray else 1.0
    if gray:
        reason = (
            f"ADX={adx:.1f}介于{params.adx_flat:.0f}~{params.adx_trend:.0f}，"
            f"弱趋势加严"
        )
    else:
        reason = "行情适合多因子"
    return CtaGate(True, reason, adx=adx, atr_rank=atr_rank, gray=gray, size_scale=size)


def evaluate_cta_filter(bars: pd.DataFrame, params: CtaFilterParams) -> CtaGate:
    """用当前信号周期的 K 线算 ADX 和 ATR 分位，再套闸门。"""
    if not params.enabled:
        return CtaGate(True, "本策略行情过滤已关闭")

    need = params.adx_period + 5
    if bars is None or len(bars) < need or not {"high", "low", "close"} <= set(bars.columns):
        # 和公共过滤器一样：数据不够时先不拦，避免刚启动全天空仓。
        return CtaGate(True, "K线不足，暂不限制开仓")

    adx_s, _plus, _minus = adx_series(
        bars["high"], bars["low"], bars["close"], period=params.adx_period
    )
    atr = average_true_range(
        bars["high"], bars["low"], bars["close"], period=params.adx_period
    )
    adx_now = float(adx_s.iloc[-1]) if len(adx_s) else 0.0
    rank = atr_percent_rank(atr, params.atr_lookback)
    if adx_now != adx_now:  # NaN
        adx_now = 0.0
    return gate_from_values(adx_now, rank, params)


def ensure_cta_filter_box(box: dict[str, Any], kind: str) -> dict[str, Any]:
    """sanitize 时补齐缺的键，已有数字不覆盖。"""
    seed = default_cta_filter(kind)
    filt = box.get("cta_filter")
    if not isinstance(filt, dict):
        box["cta_filter"] = dict(seed)
        return box["cta_filter"]
    for key, value in seed.items():
        if key not in filt:
            filt[key] = value
    return filt
