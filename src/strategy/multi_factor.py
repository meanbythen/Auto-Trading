"""
多因子复合 CTA（加权打分）

功能：四因子按固定权重合成总分，过阈值才开（engine=multi_factor）。
原理：信号周期跟配置页公共项走。SAR 开不开关在多因子自己的配置里，关掉后权重摊给剩下三个因子。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from loguru import logger

from .cta_factors import (
    FactorParams,
    FactorSnapshot,
    compute_factors,
)
from .cta_filter import CtaFilterParams, CtaGate, evaluate_cta_filter
from .market_state import MarketFilter, MarketFilterParams
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
        self.flatten_at_close = config_flag(
            self.box.get("flatten_at_close", True), default=True
        )
        self._open_snap: dict[str, dict[str, Any]] = {}
        self._last_closed_snap: dict[str, dict[str, Any]] = {}

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

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
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
        current = float(pos.trail_stop or pos.initial_stop or close)
        pad = self.atr_stop_mult * float(snap.atr)
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
        snap = compute_factors(
            bars, self.params, self._daily_frame(bars), daily_required=True
        )
        if snap is None:
            return decision
        if snap.block_entry:
            decision.reason = f"{self.label}空仓等待  {snap.block_reason}"
            return decision
        gate = evaluate_cta_filter(bars, self.cta_filter)
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
