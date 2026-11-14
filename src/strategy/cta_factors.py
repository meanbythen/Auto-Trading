"""
多因子 CTA 共用打分

功能：把均线 / 唐奇安 / 布林 / SAR 四个因子打成 -1、-0.5、0、+0.5、+1，
     再按日线方向过滤、固定权重合成总分，并记下多空票数方便日志。
原理：加权打分用这一套因子。票数只作记录，不再单独当开仓规则。
     信号周期由配置页公共项 strategy.timeframe 决定，这里不写死 5 分钟。
     均线因子、布林中轨、日线方向都用 EMA。拿不到日线时不算假日线，只拦住新开仓。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .indicators import average_true_range, ema_series, parabolic_sar
from .types import config_flag


@dataclass
class FactorParams:
    """四个因子和日线过滤用到的参数。"""

    ma_short: int = 20
    ma_long: int = 60
    channel: int = 20
    boll_window: int = 20
    boll_dev: float = 2.0
    af_start: float = 0.02
    af_step: float = 0.02
    af_max: float = 0.2
    atr_period: int = 14
    bias_ma: int = 60
    use_sar: bool = True
    w_ma: float = 0.30
    w_turtle: float = 0.25
    w_boll: float = 0.25
    w_sar: float = 0.20
    sar_flip_lookback: int = 20
    sar_flip_max: int = 3

    @classmethod
    def from_box(cls, box: dict[str, Any] | None) -> FactorParams:
        """从策略小节读参数；缺的用完整版默认值。"""
        raw = box if isinstance(box, dict) else {}
        ma_short = max(2, int(raw.get("ma_short", 20) or 20))
        ma_long = max(ma_short + 1, int(raw.get("ma_long", 60) or 60))
        weights = raw.get("weights") if isinstance(raw.get("weights"), dict) else {}
        params = cls(
            ma_short=ma_short,
            ma_long=ma_long,
            channel=max(5, int(raw.get("channel", 20) or 20)),
            boll_window=max(10, int(raw.get("boll_window", 20) or 20)),
            boll_dev=float(raw.get("boll_dev", 2.0) or 2.0),
            af_start=float(raw.get("af_start", 0.02) or 0.02),
            af_step=float(raw.get("af_step", 0.02) or 0.02),
            af_max=float(raw.get("af_max", 0.2) or 0.2),
            atr_period=max(2, int(raw.get("atr_period", 14) or 14)),
            bias_ma=max(2, int(raw.get("bias_ma", 60) or 60)),
            use_sar=config_flag(raw.get("use_sar", True), default=True),
            w_ma=float(weights.get("ma", 0.30) or 0.0),
            w_turtle=float(weights.get("turtle", 0.25) or 0.0),
            w_boll=float(weights.get("boll", 0.25) or 0.0),
            w_sar=float(weights.get("sar", 0.20) or 0.0),
        )
        params.normalize_weights()
        return params

    def normalize_weights(self) -> None:
        """
        权重按实际启用的因子重新摊成 1。

        关掉 SAR 时，它的 0.20 分给剩下三个，避免总分永远到不了原来的阈值。
        """
        parts = [
            ("w_ma", self.w_ma),
            ("w_turtle", self.w_turtle),
            ("w_boll", self.w_boll),
            ("w_sar", self.w_sar if self.use_sar else 0.0),
        ]
        total = sum(value for _name, value in parts)
        if total <= 0:
            if self.use_sar:
                self.w_ma, self.w_turtle, self.w_boll, self.w_sar = 0.30, 0.25, 0.25, 0.20
            else:
                self.w_ma, self.w_turtle, self.w_boll, self.w_sar = 0.40, 0.30, 0.30, 0.0
            return
        self.w_ma = parts[0][1] / total
        self.w_turtle = parts[1][1] / total
        self.w_boll = parts[2][1] / total
        self.w_sar = parts[3][1] / total

    def need_bars(self) -> int:
        """信号周期至少要这么多根，四个因子才算得稳。"""
        return (
            max(
                self.ma_long,
                self.channel + 1,
                self.boll_window,
                self.atr_period,
                self.sar_flip_lookback if self.use_sar else 0,
            )
            + 5
        )


@dataclass
class FactorSnapshot:
    """某一根已收盘 K 线上四个因子的完整快照，开仓、平仓、日志都读它。"""

    score_ma: float
    score_turtle: float
    score_boll: float
    score_sar: float
    vote_ma: int
    vote_turtle: int
    vote_boll: int
    vote_sar: int
    total: float
    disagreement: float
    long_votes: int
    short_votes: int
    atr: float
    daily_bias: int | None
    block_entry: bool
    block_reason: str
    use_sar: bool
    weights: dict[str, float] = field(default_factory=dict)

    def to_meta(self) -> dict[str, Any]:
        """写进下单决策，方便以后对账、导出。"""
        return {
            "score": self.total,
            "score_ma": self.score_ma,
            "score_turtle": self.score_turtle,
            "score_boll": self.score_boll,
            "score_sar": self.score_sar,
            "vote_ma": self.vote_ma,
            "vote_turtle": self.vote_turtle,
            "vote_boll": self.vote_boll,
            "vote_sar": self.vote_sar,
            "long_votes": self.long_votes,
            "short_votes": self.short_votes,
            "disagreement": self.disagreement,
            "daily_bias": self.daily_bias,
            "atr": self.atr,
        }


# 和衰减监控 Excel「参数权重」B 列、交易明细「主导因子」逐字一致。
FACTOR_KEYS: tuple[str, ...] = ("ma", "turtle", "boll", "sar")
FACTOR_LABELS: dict[str, str] = {
    "ma": "均线趋势",
    "turtle": "唐奇安突破",
    "boll": "布林趋势",
    "sar": "SAR转向",
}
FACTOR_CODES: dict[str, str] = {
    "ma": "F1",
    "turtle": "F2",
    "boll": "F3",
    "sar": "F4",
}


def scores_from_meta(meta: dict[str, Any] | None) -> dict[str, float]:
    """从开仓快照里取出四个因子分数。没有快照时全是 0。"""
    raw = meta if isinstance(meta, dict) else {}
    return {
        "ma": float(raw.get("score_ma") or 0.0),
        "turtle": float(raw.get("score_turtle") or 0.0),
        "boll": float(raw.get("score_boll") or 0.0),
        "sar": float(raw.get("score_sar") or 0.0),
    }


def allocate_factor_pnl(scores: dict[str, float], pnl: float) -> dict[str, float]:
    """
    平仓后按开仓时 |得分| 占比分摊盈亏。

    四个分数绝对值都是 0 时无法分摊，各记 0，不把整笔盈亏硬塞给某一个因子。
    """
    weights = {key: abs(float(scores.get(key) or 0.0)) for key in FACTOR_KEYS}
    total = sum(weights.values())
    if total <= 1e-12:
        return {key: 0.0 for key in FACTOR_KEYS}
    money = float(pnl or 0.0)
    return {key: money * (weights[key] / total) for key in FACTOR_KEYS}


def dominant_factor_label(scores: dict[str, float]) -> str:
    """
    这一笔的主导因子：开仓时 |得分| 最大的那个。

    衰减表「因子绩效」按这一列统计。名字必须和表里的「均线趋势」等完全一样。
    """
    best_key = ""
    best_abs = 0.0
    for key in FACTOR_KEYS:
        value = abs(float(scores.get(key) or 0.0))
        if value > best_abs:
            best_abs = value
            best_key = key
    if best_abs <= 0:
        return ""
    return FACTOR_LABELS[best_key]


def resonance_codes(scores: dict[str, float], *, min_abs: float = 0.5) -> str:
    """共振因子：除主导外、半票及以上的因子，写成 F2/F3。只给表里备注，不参与统计。"""
    dominant = dominant_factor_label(scores)
    parts: list[str] = []
    for key in FACTOR_KEYS:
        if FACTOR_LABELS[key] == dominant:
            continue
        if abs(float(scores.get(key) or 0.0)) >= min_abs:
            parts.append(FACTOR_CODES[key])
    return "/".join(parts)


def factor_fields_for_trade(meta: dict[str, Any] | None, pnl: float) -> dict[str, Any]:
    """回测成交和衰减表共用的一套字段。没有因子快照时全空，别的策略不受影响。"""
    raw = meta if isinstance(meta, dict) else {}
    if raw.get("score_ma") is None and raw.get("score") is None:
        return {}
    scores = scores_from_meta(raw)
    allocated = allocate_factor_pnl(scores, pnl)
    return {
        "score_ma": scores["ma"],
        "score_turtle": scores["turtle"],
        "score_boll": scores["boll"],
        "score_sar": scores["sar"],
        "score_total": float(raw.get("score") or 0.0),
        "atr": float(raw.get("atr") or 0.0) or None,
        "pnl_ma": allocated["ma"],
        "pnl_turtle": allocated["turtle"],
        "pnl_boll": allocated["boll"],
        "pnl_sar": allocated["sar"],
        "dominant_factor": dominant_factor_label(scores),
        "resonance": resonance_codes(scores),
    }


def score_to_vote(score: float) -> int:
    """打分转投票：正数投多、负数投空、零弃权。半票也算一票。"""
    if score > 0:
        return 1
    if score < 0:
        return -1
    return 0


def daily_bias(daily_bars: pd.DataFrame | None, bias_ma: int) -> int | None:
    """
    日线收盘相对 EMA：上方 +1、下方 -1、贴着 0。

    用指数均线（和同花顺 EXPMA 同一类），最近的大跌会更快反映出来。
    以前是简单均线，大跌后收盘仍可能长时间算多。
    只看已经走完的日线，当天还没收盘的那根不算。
    返回 None 表示日线根本没拿到，不能拿分钟线假装成日线。
    """
    n = int(bias_ma)
    if daily_bars is None or len(daily_bars) < n:
        return None
    close = pd.to_numeric(daily_bars["close"], errors="coerce")
    ma = ema_series(close, n)
    last_close = float(close.iloc[-1])
    last_ma = float(ma.iloc[-1])
    if pd.isna(last_close) or pd.isna(last_ma):
        return None
    if last_close > last_ma:
        return 1
    if last_close < last_ma:
        return -1
    return 0


def apply_daily_bias(score: float, bias: int | None) -> float:
    """日线方向一票否决：和日线对着干的分数清零。日线缺失时原样保留（开仓另拦）。"""
    if bias is None:
        return score
    if bias == 0:
        return 0.0
    if bias > 0 and score < 0:
        return 0.0
    if bias < 0 and score > 0:
        return 0.0
    return score


def position_ratio(
    abs_total: float,
    threshold: float,
    min_ratio: float = 0.5,
    max_ratio: float = 1.0,
) -> float:
    """混合版仓位系数：刚过门槛用最低成数，满分用满仓，中间按总分线性插。"""
    low = min(min_ratio, max_ratio)
    high = max(min_ratio, max_ratio)
    span = max(1e-9, 1.0 - float(threshold))
    strength = min(1.0, max(0.0, (float(abs_total) - float(threshold)) / span))
    return float(low + (high - low) * strength)


def _score_ma(
    close_now: float,
    ma_s: float,
    ma_l: float,
    prev_s: float,
    prev_l: float,
    atr: float,
) -> float:
    """均线因子：同向且金叉/死叉打满，同向但没交叉打半票，缠绕打 0。"""
    if abs(ma_s - ma_l) < 0.5 * atr:
        return 0.0
    if close_now > ma_l and ma_s > ma_l:
        golden = prev_s <= prev_l and ma_s > ma_l
        return 1.0 if golden else 0.5
    if close_now < ma_l and ma_s < ma_l:
        death = prev_s >= prev_l and ma_s < ma_l
        return -1.0 if death else -0.5
    return 0.0


def _score_turtle(close_now: float, prior_high: float, prior_low: float) -> float:
    """唐奇安：突破前 N 根高低点打满，站在通道上半/下半打半票。"""
    if close_now > prior_high:
        return 1.0
    if close_now < prior_low:
        return -1.0
    mid = (prior_high + prior_low) / 2.0
    if close_now > mid:
        return 0.5
    if close_now < mid:
        return -0.5
    return 0.0


def _score_boll(
    close_now: float,
    mid: float,
    upper: float,
    lower: float,
    slope: float,
) -> float:
    """布林：中轨向上且收盘在中轨上方才做多；破上轨打满，否则半票。"""
    if slope > 0 and close_now > mid:
        return 1.0 if close_now > upper else 0.5
    if slope < 0 and close_now < mid:
        return -1.0 if close_now < lower else -0.5
    return 0.0


def _sar_flip_count(close: pd.Series, sar: pd.Series, lookback: int) -> int:
    """最近若干根里 SAR 相对收盘翻了几次方向。翻太勤说明在震荡，SAR 因子作废。"""
    if len(close) < 3:
        return 0
    bull = pd.to_numeric(close, errors="coerce") > pd.to_numeric(sar, errors="coerce")
    changed = bull != bull.shift(1)
    window = changed.iloc[-int(lookback) :].fillna(False)
    return int(window.astype(bool).sum())


def compute_factors(
    bars: pd.DataFrame,
    params: FactorParams,
    daily_bars: pd.DataFrame | None = None,
    *,
    daily_required: bool = True,
) -> FactorSnapshot | None:
    """
    算四个因子。bars 必须是信号周期已收盘的 K 线。

    daily_required=True 时，日线缺失或贴着均线会拦住开仓，但平仓仍用未过滤的打分。
    """
    if bars is None or len(bars) < params.need_bars():
        return None
    close = pd.to_numeric(bars["close"], errors="coerce")
    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")
    ma_s = ema_series(close, params.ma_short)
    ma_l = ema_series(close, params.ma_long)
    # 唐奇安用「已经走完的前 N 根」，不能把当前这根算进去。
    prior_high = high.rolling(params.channel).max().shift(1)
    prior_low = low.rolling(params.channel).min().shift(1)
    mid = ema_series(close, params.boll_window)
    std = close.rolling(params.boll_window).std(ddof=0)
    atr_series = average_true_range(high, low, close, period=params.atr_period)
    atr = float(atr_series.iloc[-1])
    if pd.isna(atr) or atr <= 0:
        return None
    if (
        pd.isna(ma_s.iloc[-1])
        or pd.isna(ma_l.iloc[-1])
        or pd.isna(prior_high.iloc[-1])
        or pd.isna(mid.iloc[-1])
        or pd.isna(std.iloc[-1])
        or pd.isna(ma_s.iloc[-2])
        or pd.isna(ma_l.iloc[-2])
        or pd.isna(mid.iloc[-2])
    ):
        return None

    close_now = float(close.iloc[-1])
    score_ma = _score_ma(
        close_now,
        float(ma_s.iloc[-1]),
        float(ma_l.iloc[-1]),
        float(ma_s.iloc[-2]),
        float(ma_l.iloc[-2]),
        atr,
    )
    score_turtle = _score_turtle(
        close_now, float(prior_high.iloc[-1]), float(prior_low.iloc[-1])
    )
    mid_now = float(mid.iloc[-1])
    std_now = float(std.iloc[-1])
    slope = mid_now - float(mid.iloc[-2])
    upper = mid_now + params.boll_dev * std_now
    lower = mid_now - params.boll_dev * std_now
    score_boll = _score_boll(close_now, mid_now, upper, lower, slope)

    score_sar = 0.0
    if params.use_sar:
        sar = parabolic_sar(
            high,
            low,
            af_start=params.af_start,
            af_step=params.af_step,
            af_max=params.af_max,
        )
        if pd.isna(sar.iloc[-1]):
            return None
        sar_now = float(sar.iloc[-1])
        if close_now > sar_now:
            score_sar = 1.0
        elif close_now < sar_now:
            score_sar = -1.0
        if _sar_flip_count(close, sar, params.sar_flip_lookback) > params.sar_flip_max:
            score_sar = 0.0

    bias = daily_bias(daily_bars, params.bias_ma)
    block_entry = False
    block_reason = ""
    if daily_required and bias is None:
        block_entry = True
        block_reason = "日线没拿到，方向看不清，不开新仓"
    elif daily_required and bias == 0:
        block_entry = True
        block_reason = "日线贴着均线，方向不清，不开新仓"

    # 开仓用经过日线过滤的分数；日线缺失时分数原样保留，方便已有持仓按信号离场。
    filtered_ma = apply_daily_bias(score_ma, 0 if bias == 0 else bias)
    filtered_turtle = apply_daily_bias(score_turtle, 0 if bias == 0 else bias)
    filtered_boll = apply_daily_bias(score_boll, 0 if bias == 0 else bias)
    filtered_sar = apply_daily_bias(score_sar, 0 if bias == 0 else bias)
    if bias is None:
        filtered_ma, filtered_turtle, filtered_boll, filtered_sar = (
            score_ma,
            score_turtle,
            score_boll,
            score_sar,
        )

    total = (
        params.w_ma * filtered_ma
        + params.w_turtle * filtered_turtle
        + params.w_boll * filtered_boll
        + params.w_sar * filtered_sar
    )
    used = [filtered_ma, filtered_turtle, filtered_boll]
    if params.use_sar:
        used.append(filtered_sar)
    disagreement = float(np.std(used, ddof=0))
    vote_ma = score_to_vote(filtered_ma)
    vote_turtle = score_to_vote(filtered_turtle)
    vote_boll = score_to_vote(filtered_boll)
    vote_sar = score_to_vote(filtered_sar) if params.use_sar else 0
    votes = [vote_ma, vote_turtle, vote_boll]
    if params.use_sar:
        votes.append(vote_sar)
    long_votes = sum(1 for item in votes if item > 0)
    short_votes = sum(1 for item in votes if item < 0)
    return FactorSnapshot(
        score_ma=filtered_ma,
        score_turtle=filtered_turtle,
        score_boll=filtered_boll,
        score_sar=filtered_sar,
        vote_ma=vote_ma,
        vote_turtle=vote_turtle,
        vote_boll=vote_boll,
        vote_sar=vote_sar,
        total=float(total),
        disagreement=disagreement,
        long_votes=int(long_votes),
        short_votes=int(short_votes),
        atr=atr,
        daily_bias=bias,
        block_entry=block_entry,
        block_reason=block_reason,
        use_sar=params.use_sar,
        weights={
            "ma": params.w_ma,
            "turtle": params.w_turtle,
            "boll": params.w_boll,
            "sar": params.w_sar,
        },
    )
