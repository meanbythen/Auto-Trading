"""
多因子复合 CTA（加权打分）

功能：四因子按固定权重合成总分，过阈值才开（engine=multi_factor）。
原理：信号周期跟配置页公共项走。SAR 开不开关在多因子自己的配置里，关掉后权重摊给剩下三个因子。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from .cta_factors import (
    FactorParams,
    FactorSnapshot,
    _score_boll,
    _score_ma,
    _score_turtle,
    apply_daily_bias,
    compute_factors,
    score_to_vote,
)
from .cta_filter import CtaFilterParams, CtaGate, evaluate_cta_filter, gate_from_values
from .indicators import average_true_range, ema_series, parabolic_sar
from .market_state import MarketFilter, MarketFilterParams, adx_series
from .trend_base import TrendStrategyBase
from .types import PositionState, StrategyAction, config_flag


def _load_cta_box(cfg: dict[str, Any], section: str) -> dict[str, Any]:
    """读这一套多因子自己的配置小节。"""
    st = cfg.get("strategy") or {}
    own = st.get(section) if isinstance(st.get(section), dict) else {}
    return dict(own)


class CtaFactorStrategy(TrendStrategyBase):
    """多因子加权打分骨架：算因子、日线过滤、2 倍 ATR 只往有利方向收止损。"""

    uses_trend_bars = True
    mode = "score"
    shared_section = "multi_factor"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self.box = _load_cta_box(cfg, self.section)
        self.params = FactorParams.from_box(self.box)
        self.atr_period = self.params.atr_period
        self.threshold = min(0.95, max(0.1, float(self.box.get("threshold", 0.6) or 0.6)))
        self.atr_stop_mult = float(self.box.get("atr_stop_mult", 2.0) or 2.0)
        if self.atr_stop_mult <= 0:
            self.atr_stop_mult = 2.0
        self.disagree_max = float(self.box.get("disagree_max", 0.7) or 0.7)
        # 本策略自己的行情闸门；不再套页面底下那张公共趋势过滤器，避免拦两次。
        self.cta_filter = CtaFilterParams.from_box(self.box, kind=self.section)
        self.market_filter = MarketFilter(MarketFilterParams(enabled=False, mode="trend"))
        self.min_position_ratio = float(self.box.get("min_position_ratio", 0.5) or 0.5)
        self.max_position_ratio = float(self.box.get("max_position_ratio", 1.0) or 1.0)
        # 日线周期写在策略自己这一节；信号周期仍是配置页公共项 timeframe。
        self.trend_timeframe = str(self.box.get("trend_timeframe", "D") or "D")
        # 收盘清仓听交易配置总开关。以前燃油为主时写死每天平仓，公共开关关了也不管用。
        # 总开关写过就用总开关；没写过才看自己那一节，再没有则仍按到点平仓。
        st = cfg.get("strategy") or {}
        if "flatten_at_close" in st:
            flatten_raw = st.get("flatten_at_close")
        else:
            flatten_raw = self.box.get("flatten_at_close", True)
        self.flatten_at_close = config_flag(flatten_raw, default=True)
        self._open_snap: dict[str, dict[str, Any]] = {}
        self._last_closed_snap: dict[str, dict[str, Any]] = {}
        self._hist_index: pd.DatetimeIndex | None = None
        self._hist_prep: list[dict[str, Any] | None] | None = None

    def note_factor_pnl(
        self,
        symbol: str,
        pnl: float,
        *,
        volume: int = 0,
        multiplier: float = 0.0,
        side: str = "",
        entry_time: str = "",
        exit_time: str = "",
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        reason_exit: str = "",
        commission: float = 0.0,
    ) -> None:
        """成交后把分摊盈亏记下来，供衰减表和日志使用。"""
        from .cta_factors import allocate_factor_pnl, factor_fields_for_trade, scores_from_meta

        snap = self._last_closed_snap.get(symbol)
        if not isinstance(snap, dict):
            return
        fields = factor_fields_for_trade(snap, pnl)
        allocated = allocate_factor_pnl(scores_from_meta(snap), pnl)
        logger.info(
            "{} {}盈亏分摊 {:+.2f}元 → 均线{:+.2f} 唐奇安{:+.2f} 布林{:+.2f} SAR{:+.2f} 主导{}",
            symbol,
            self.label,
            float(pnl),
            allocated["ma"],
            allocated["turtle"],
            allocated["boll"],
            allocated["sar"],
            fields.get("dominant_factor") or "无",
        )
        try:
            from src.export.cta_decay import append_live_factor_trade

            append_live_factor_trade(
                {
                    **snap,
                    **fields,
                    "symbol": symbol,
                    "side": side,
                    "pnl": float(pnl),
                    "volume": int(volume or 0),
                    "multiplier": float(multiplier or 0.0),
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "entry_price": float(entry_price or 0.0),
                    "exit_price": float(exit_price or 0.0),
                    "reason_exit": reason_exit,
                    "commission": float(commission or 0.0),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("{} 因子分摊没有写成文件: {}", symbol, exc)

    def need_bars(self) -> int:
        return max(self.params.need_bars(), int(self.cta_filter.need_bars))

    @property
    def required_bars(self) -> int:
        # 公共过滤器已关掉，回看窗口改看本策略闸门。
        return self.need_bars()

    def _daily_frame(self, bars: pd.DataFrame) -> pd.DataFrame | None:
        """信号本身就是日线/周线时，不必再另拉一套。"""
        if str(self.timeframe).upper() in {"D", "W"}:
            return bars
        trend = getattr(self, "_trend_bars", None)
        if trend is None or len(trend) == 0:
            return None
        return trend

    def bind_history(
        self,
        bars: pd.DataFrame | None,
        trend_bars: pd.DataFrame | None = None,
    ) -> None:
        """回测开始前把因子整表算一遍。逐根再算 SAR 会按根数平方变慢。"""
        self._hist_index = None
        self._hist_prep = None
        if bars is None or len(bars) == 0:
            return
        self._trend_bars = trend_bars
        n = len(bars)
        prep: list[dict[str, Any] | None] = [None] * n
        need = max(self.need_bars(), 3)
        close = pd.to_numeric(bars["close"], errors="coerce")
        high = pd.to_numeric(bars["high"], errors="coerce")
        low = pd.to_numeric(bars["low"], errors="coerce")
        params = self.params
        ma_s = ema_series(close, params.ma_short)
        ma_l = ema_series(close, params.ma_long)
        prior_high = high.rolling(params.channel).max().shift(1)
        prior_low = low.rolling(params.channel).min().shift(1)
        mid = ema_series(close, params.boll_window)
        std = close.rolling(params.boll_window).std(ddof=0)
        atr_s = average_true_range(high, low, close, period=params.atr_period)
        sar = (
            parabolic_sar(
                high,
                low,
                af_start=params.af_start,
                af_step=params.af_step,
                af_max=params.af_max,
            )
            if params.use_sar
            else None
        )
        bull = None
        flips = None
        if sar is not None:
            bull = close > sar
            flips = (bull != bull.shift(1)).fillna(False).rolling(params.sar_flip_lookback).sum()
        adx_s, _p, _m = adx_series(high, low, close, period=self.cta_filter.adx_period)
        atr_gate = average_true_range(high, low, close, period=self.cta_filter.adx_period)
        atr_v = atr_gate.to_numpy(dtype=float) if atr_gate is not None else None
        look = max(2, int(self.cta_filter.atr_lookback))
        daily_close = None
        daily_ma = None
        daily_index = None
        daily = self._daily_frame(bars)
        if daily is not None and len(daily) >= params.bias_ma:
            daily_close = pd.to_numeric(daily["close"], errors="coerce")
            daily_ma = ema_series(daily_close, params.bias_ma)
            daily_index = pd.DatetimeIndex(daily.index)

        from src.utils.trading_day import futures_trading_day

        for i in range(need - 1, n):
            if (
                pd.isna(ma_s.iloc[i])
                or pd.isna(ma_l.iloc[i])
                or pd.isna(prior_high.iloc[i])
                or pd.isna(mid.iloc[i])
                or pd.isna(std.iloc[i])
                or pd.isna(atr_s.iloc[i])
                or pd.isna(ma_s.iloc[i - 1])
                or pd.isna(ma_l.iloc[i - 1])
                or pd.isna(mid.iloc[i - 1])
            ):
                continue
            atr = float(atr_s.iloc[i])
            if atr <= 0:
                continue
            close_now = float(close.iloc[i])
            score_ma = _score_ma(
                close_now,
                float(ma_s.iloc[i]),
                float(ma_l.iloc[i]),
                float(ma_s.iloc[i - 1]),
                float(ma_l.iloc[i - 1]),
                atr,
            )
            score_turtle = _score_turtle(
                close_now, float(prior_high.iloc[i]), float(prior_low.iloc[i])
            )
            mid_now = float(mid.iloc[i])
            std_now = float(std.iloc[i])
            slope = mid_now - float(mid.iloc[i - 1])
            score_boll = _score_boll(
                close_now,
                mid_now,
                mid_now + params.boll_dev * std_now,
                mid_now - params.boll_dev * std_now,
                slope,
            )
            score_sar = 0.0
            if params.use_sar:
                if sar is None or pd.isna(sar.iloc[i]):
                    continue
                sar_now = float(sar.iloc[i])
                if close_now > sar_now:
                    score_sar = 1.0
                elif close_now < sar_now:
                    score_sar = -1.0
                flip_now = 0.0
                if flips is not None and not pd.isna(flips.iloc[i]):
                    flip_now = float(flips.iloc[i])
                if flip_now > params.sar_flip_max:
                    score_sar = 0.0
            bias: int | None = None
            if daily_index is not None and daily_close is not None and daily_ma is not None:
                stamp = pd.Timestamp(bars.index[i]).to_pydatetime()
                if getattr(stamp, "tzinfo", None) is not None:
                    stamp = stamp.replace(tzinfo=None)
                cutoff = pd.Timestamp(futures_trading_day(stamp))
                pos = int(daily_index.searchsorted(cutoff, side="left")) - 1
                # 和逐根 daily_bias 一样：截到这一天之前，至少要有 bias_ma 根日线。
                if (
                    pos + 1 >= params.bias_ma
                    and not pd.isna(daily_ma.iloc[pos])
                    and not pd.isna(daily_close.iloc[pos])
                ):
                    last_c = float(daily_close.iloc[pos])
                    last_m = float(daily_ma.iloc[pos])
                    if last_c > last_m:
                        bias = 1
                    elif last_c < last_m:
                        bias = -1
                    else:
                        bias = 0
            block_entry = False
            block_reason = ""
            if bias is None:
                block_entry = True
                block_reason = "日线没拿到，方向看不清，不开新仓"
            elif bias == 0:
                block_entry = True
                block_reason = "日线贴着均线，方向不清，不开新仓"
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
            snap = FactorSnapshot(
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
                long_votes=sum(1 for item in votes if item > 0),
                short_votes=sum(1 for item in votes if item < 0),
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
            if not self.cta_filter.enabled:
                gate = CtaGate(True, "本策略行情过滤已关闭")
            elif (i + 1) < self.cta_filter.adx_period + 5:
                gate = CtaGate(True, "K线不足，暂不限制开仓")
            else:
                adx_now = (
                    float(adx_s.iloc[i])
                    if adx_s is not None and not pd.isna(adx_s.iloc[i])
                    else 0.0
                )
                rank = 1.0
                if atr_v is not None:
                    lo = max(0, i - look + 1)
                    window = atr_v[lo : i + 1]
                    window = window[np.isfinite(window)]
                    if len(window):
                        rank = float((window <= window[-1]).mean())
                gate = gate_from_values(adx_now, rank, self.cta_filter)
            prep[i] = {"snap": snap, "gate": gate}
        self._hist_index = pd.DatetimeIndex(bars.index)
        self._hist_prep = prep

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        n = len(bars)
        if (
            self._hist_prep is not None
            and self._hist_index is not None
            and n <= len(self._hist_prep)
            and n > 0
            and self._hist_index[n - 1] == bars.index[-1]
        ):
            return self._hist_prep[n - 1]
        snap = compute_factors(
            bars,
            self.params,
            self._daily_frame(bars),
            daily_required=True,
        )
        if snap is None:
            return None
        gate = evaluate_cta_filter(bars, self.cta_filter)
        return {"snap": snap, "gate": gate}

    def _score_text(self, snap: FactorSnapshot) -> str:
        sar = f"/SAR{snap.score_sar:+.1f}" if snap.use_sar else "/SAR关"
        return (
            f"总分{snap.total:+.2f} "
            f"[均线{snap.score_ma:+.1f}/唐奇安{snap.score_turtle:+.1f}"
            f"/布林{snap.score_boll:+.1f}{sar}]"
        )

    def _vote_text(self, snap: FactorSnapshot) -> str:
        return f"多{snap.long_votes}票/空{snap.short_votes}票"

    def _disagreement_blocks(self, snap: FactorSnapshot) -> bool:
        """总开关或「过滤因子分歧」关掉时，不再因为打架拦开仓。"""
        if not self.cta_filter.enabled or not self.cta_filter.use_disagree:
            return False
        return snap.disagreement > self.disagree_max

    def _gate_blocks(self, gate: CtaGate | None) -> bool:
        return gate is not None and not gate.ok

    def _gray_need_score(self, gate: CtaGate | None) -> float:
        """弱趋势时要把总分抬到资料里的 0.75。"""
        if gate is None or not gate.gray:
            return 0.0
        return float(self.cta_filter.weak_score or 0.0)

    def _merge_size(self, gate: CtaGate | None, extra: dict[str, Any] | None) -> dict[str, Any]:
        """弱趋势把仓位打成数写进开仓备注，1 手账户仍会开成 1 手。"""
        out = dict(extra or {})
        if gate is not None and gate.gray and gate.size_scale < 1.0:
            prev = float(out.get("size_scale") or 1.0)
            out["size_scale"] = round(min(prev, gate.size_scale), 4)
        return out

    def _open_pack(
        self,
        side: str,
        close: float,
        snap: FactorSnapshot,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> tuple[str, float, str, dict[str, Any]]:
        pad = self.atr_stop_mult * float(snap.atr)
        stop = close - pad if side == "long" else close + pad
        meta = snap.to_meta()
        if extra:
            meta.update(extra)
        return side, stop, reason, meta

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        snap: FactorSnapshot = ind["snap"]
        gate: CtaGate | None = ind.get("gate")
        if snap.block_entry:
            return None
        if self._gate_blocks(gate):
            return None
        if self._disagreement_blocks(snap):
            return None
        close = float(bar["close"])
        return self._entry_from_snap(close, snap, gate)

    def _entry_from_snap(
        self, close: float, snap: FactorSnapshot, gate: CtaGate | None = None
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        raise NotImplementedError

    def exit_reason(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> str | None:
        snap: FactorSnapshot = ind["snap"]
        return self._exit_from_snap(pos, snap)

    def _exit_from_snap(self, pos: PositionState, snap: FactorSnapshot) -> str | None:
        raise NotImplementedError

    def trail_stop(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> float:
        """2 倍 ATR 跟踪止损，只往有利方向收，亏的方向不放宽。"""
        snap: FactorSnapshot = ind["snap"]
        close = float(bar["close"])
        pad = self.atr_stop_mult * float(snap.atr)
        current = float(pos.trail_stop or pos.initial_stop or 0.0)
        if current <= 0:
            # 柜台认领 / 账本重建的仓没有止损价：按开仓价和现价里更差的一侧往外放 2×ATR。
            # 不能拿现价兜底当止损，否则认领后第一根 K 线就会把仓位平掉。
            entry = float(pos.entry_price or close)
            if pos.side == "long":
                current = min(entry, close) - pad
            else:
                current = max(entry, close) + pad
        if pos.side == "long":
            stop = max(current, close - pad)
        else:
            stop = min(current, close + pad)
        pos.trail_stop = stop
        return stop

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now=None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
    ):
        """空仓被日线/分歧拦住时，把原因写进 HOLD，避免看起来像策略死了。"""
        decision = super().evaluate(
            symbol, bars, now=now, trail_bars=trail_bars, trend_bars=trend_bars
        )
        if decision.action != StrategyAction.HOLD:
            return decision
        pos = self.positions.get(symbol)
        if pos is not None and pos.side != "flat":
            return decision
        # super().evaluate 没有把 prepare 结果留下来，这里再算一次太贵；
        # 改成：若 reason 仍是默认等待，用最新 bars 补一句更准的原因。
        if "空仓等待" not in str(decision.reason):
            return decision
        # 刚在 prepare 里算过，不要每根空仓再算一遍 SAR/均线，回测会按根数平方变慢。
        ind = getattr(self, "_last_prepare", None)
        if not isinstance(ind, dict) or "snap" not in ind:
            return decision
        snap = ind["snap"]
        if snap is None:
            return decision
        if snap.block_entry:
            decision.reason = f"{self.label}空仓等待  {snap.block_reason}"
            return decision
        gate = ind.get("gate")
        if self._gate_blocks(gate):
            decision.reason = f"{self.label}空仓等待  {gate.reason}"
            return decision
        if self._disagreement_blocks(snap):
            decision.reason = (
                f"{self.label}空仓等待  因子分歧{snap.disagreement:.2f}"
                f">{self.disagree_max:.2f}，不开新仓"
            )
            return decision
        if gate.gray:
            decision.reason = f"{self.label}空仓等待  {gate.describe()}，信号还不够"
            return decision
        return decision

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        pending = self._pending.get(symbol) or {}
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            snap = pending.get("snap_meta")
            if isinstance(snap, dict):
                self._open_snap[symbol] = dict(snap)
        elif action == StrategyAction.CLOSE:
            snap = self._open_snap.pop(symbol, None)
            if snap:
                self._last_closed_snap[symbol] = dict(snap)
                logger.info(
                    "{} {}平仓归因 开仓时总分{:+.2f} 均线{:+.1f} 唐奇安{:+.1f} "
                    "布林{:+.1f} SAR{:+.1f} 多{}票空{}票 成交价{:.2f} 手数{}",
                    symbol,
                    self.label,
                    float(snap.get("score") or 0),
                    float(snap.get("score_ma") or 0),
                    float(snap.get("score_turtle") or 0),
                    float(snap.get("score_boll") or 0),
                    float(snap.get("score_sar") or 0),
                    int(snap.get("long_votes") or 0),
                    int(snap.get("short_votes") or 0),
                    float(price),
                    int(volume),
                )
        super().on_filled(symbol, action, price, volume, initial_stop=initial_stop)


def _stash_open_meta(strategy: CtaFactorStrategy, symbol: str, meta: dict[str, Any]) -> None:
    """开仓那一拍把因子快照塞进 pending，成交后再拿来做归因。"""
    pending = strategy._pending.get(symbol)
    if isinstance(pending, dict):
        pending["snap_meta"] = dict(meta)


class MultiFactorStrategy(CtaFactorStrategy):
    """固定权重加权打分。这是多因子家族的主推对照。"""

    section = "multi_factor"
    label = "多因子CTA"
    mode = "score"

    def _entry_from_snap(self, close: float, snap: FactorSnapshot, gate: CtaGate | None = None):
        need = max(self.threshold, self._gray_need_score(gate))
        extra = self._merge_size(gate, None)
        tag = f" {gate.describe()}" if gate and gate.ok else ""
        if snap.total >= need:
            return self._open_pack(
                "long",
                close,
                snap,
                f"多因子做多 {self._score_text(snap)}≥{need:.2f}{tag} "
                f"止损{close - self.atr_stop_mult * snap.atr:.2f}",
                extra=extra or None,
            )
        if snap.total <= -need:
            return self._open_pack(
                "short",
                close,
                snap,
                f"多因子做空 {self._score_text(snap)}≤-{need:.2f}{tag} "
                f"止损{close + self.atr_stop_mult * snap.atr:.2f}",
                extra=extra or None,
            )
        return None

    def _exit_from_snap(self, pos: PositionState, snap: FactorSnapshot) -> str | None:
        if pos.side == "long" and snap.total <= -self.threshold:
            return f"多因子平多 {self._score_text(snap)} 反向穿阈值"
        if pos.side == "short" and snap.total >= self.threshold:
            return f"多因子平空 {self._score_text(snap)} 反向穿阈值"
        return None

    def evaluate(self, symbol: str, bars: pd.DataFrame, now=None, trail_bars=None, trend_bars=None):
        decision = super().evaluate(
            symbol, bars, now=now, trail_bars=trail_bars, trend_bars=trend_bars
        )
        if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            _stash_open_meta(self, symbol, decision.meta or {})
        return decision
