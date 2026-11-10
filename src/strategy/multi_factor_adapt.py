"""
多因子 CTA · 自适应权重对照版。

功能：开平仓规则与「多因子CTA加权打分」相同；只在平仓之后，按最近 20 笔分摊盈亏慢慢改四个因子的权重。
原理：原文件 `multi_factor.py` 一行不改。这里继承它的开仓/平仓/止损，另存配置小节 `multi_factor_adapt`。
     主推仍是固定权重。这一套要你在下拉框里主动选，不会自动替换实盘。
     权重不拿去网格穷搜，避免曲线好看、实盘变差。
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any

import numpy as np
from loguru import logger

from src.strategy.adaptive_weights import update_weights
from src.strategy.cta_factors import (
    FACTOR_KEYS,
    FactorParams,
    FactorSnapshot,
    allocate_factor_pnl,
    scores_from_meta,
)
from src.strategy.cta_filter import CtaFilterParams
from src.strategy.multi_factor import MultiFactorStrategy
from src.strategy.types import StrategyAction, config_flag


class MultiFactorAdaptStrategy(MultiFactorStrategy):
    """固定规则 + 平仓后慢速改权重。对照用，不是新的开仓公式。"""

    section = "multi_factor_adapt"
    label = "多因子CTA自适应"
    mode = "score"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        st = cfg.get("strategy") or {}
        own = st.get(self.section) if isinstance(st.get(self.section), dict) else {}
        # 自己的小节覆盖抄来的主推数字，两套配置互不改写。
        self.box = {**self.box, **own}
        self.params = FactorParams.from_box(self.box)
        self.cta_filter = CtaFilterParams.from_box(self.box, kind="multi_factor")
        self.threshold = min(0.95, max(0.1, float(self.box.get("threshold", 0.6) or 0.6)))
        self.atr_stop_mult = float(self.box.get("atr_stop_mult", 2.0) or 2.0)
        if self.atr_stop_mult <= 0:
            self.atr_stop_mult = 2.0
        self.disagree_max = float(self.box.get("disagree_max", 0.7) or 0.7)
        self.flatten_at_close = config_flag(
            self.box.get("flatten_at_close", True), default=True
        )
        self.adapt_enabled = config_flag(self.box.get("adapt_enabled", True), default=True)
        self.lookback_trades = max(5, int(self.box.get("lookback_trades", 20) or 20))
        self.min_weight = float(self.box.get("min_weight", 0.05) or 0.05)
        self.max_weight = float(self.box.get("max_weight", 0.40) or 0.40)
        self.recent_mix = float(self.box.get("recent_mix", 0.30) or 0.30)
        self.max_step = float(self.box.get("max_step", 0.03) or 0.03)
        self._base_weights = {
            "ma": self.params.w_ma,
            "turtle": self.params.w_turtle,
            "boll": self.params.w_boll,
            "sar": self.params.w_sar,
        }
        self._pnl_rows: deque[dict[str, float]] = deque(maxlen=max(40, self.lookback_trades))
        self._dead_factors: set[str] = set()

    def prepare(self, bars):
        packed = super().prepare(bars)
        if not packed:
            return packed
        snap = packed.get("snap")
        if isinstance(snap, FactorSnapshot):
            packed["snap"] = self._apply_live_weights(snap)
        return packed

    def _apply_live_weights(self, snap: FactorSnapshot) -> FactorSnapshot:
        """用当前权重重算总分；窗口为负的因子当期得分打成 0。"""
        scores = {
            "ma": float(snap.score_ma),
            "turtle": float(snap.score_turtle),
            "boll": float(snap.score_boll),
            "sar": float(snap.score_sar),
        }
        if self.adapt_enabled:
            for key in self._dead_factors:
                if key in scores:
                    scores[key] = 0.0
        weights = {
            "ma": self.params.w_ma,
            "turtle": self.params.w_turtle,
            "boll": self.params.w_boll,
            "sar": self.params.w_sar,
        }
        total = (
            weights["ma"] * scores["ma"]
            + weights["turtle"] * scores["turtle"]
            + weights["boll"] * scores["boll"]
            + weights["sar"] * scores["sar"]
        )
        used = [scores["ma"], scores["turtle"], scores["boll"]]
        if snap.use_sar:
            used.append(scores["sar"])
        from src.strategy.cta_factors import score_to_vote

        vote_ma = score_to_vote(scores["ma"])
        vote_turtle = score_to_vote(scores["turtle"])
        vote_boll = score_to_vote(scores["boll"])
        vote_sar = score_to_vote(scores["sar"]) if snap.use_sar else 0
        votes = [vote_ma, vote_turtle, vote_boll]
        if snap.use_sar:
            votes.append(vote_sar)
        return replace(
            snap,
            score_ma=scores["ma"],
            score_turtle=scores["turtle"],
            score_boll=scores["boll"],
            score_sar=scores["sar"],
            vote_ma=vote_ma,
            vote_turtle=vote_turtle,
            vote_boll=vote_boll,
            vote_sar=vote_sar,
            total=float(total),
            disagreement=float(np.std(used, ddof=0)),
            long_votes=sum(1 for item in votes if item > 0),
            short_votes=sum(1 for item in votes if item < 0),
            weights=dict(weights),
        )

    def on_trade_closed(
        self,
        symbol: str,
        pnl: float,
        **_kwargs: Any,
    ) -> None:
        """平仓后才改权重。回测和实盘都走这里，不改开仓公式本身。"""
        if not self.adapt_enabled:
            return
        snap = self._last_closed_snap.get(symbol)
        if not isinstance(snap, dict):
            return
        allocated = allocate_factor_pnl(scores_from_meta(snap), float(pnl))
        self._pnl_rows.append({key: float(allocated.get(key) or 0.0) for key in FACTOR_KEYS})
        current = {
            "ma": self.params.w_ma,
            "turtle": self.params.w_turtle,
            "boll": self.params.w_boll,
            "sar": self.params.w_sar,
        }
        new_w, window_pnl, dead = update_weights(
            self._base_weights,
            current,
            list(self._pnl_rows),
            lookback=self.lookback_trades,
            min_weight=self.min_weight,
            max_weight=self.max_weight,
            recent_mix=self.recent_mix,
            max_step=self.max_step,
            use_sar=self.params.use_sar,
        )
        self._dead_factors = dead if len(self._pnl_rows) >= self.lookback_trades else set()
        self.params.w_ma = new_w["ma"]
        self.params.w_turtle = new_w["turtle"]
        self.params.w_boll = new_w["boll"]
        self.params.w_sar = new_w["sar"]
        self.params.normalize_weights()
        logger.info(
            "{} {}平仓后权重 均线{:.3f} 唐奇安{:.3f} 布林{:.3f} SAR{:.3f} "
            "窗口盈亏 均线{:+.1f} 唐奇安{:+.1f} 布林{:+.1f} SAR{:+.1f} 已记{}笔{}",
            symbol,
            self.label,
            self.params.w_ma,
            self.params.w_turtle,
            self.params.w_boll,
            self.params.w_sar,
            window_pnl["ma"],
            window_pnl["turtle"],
            window_pnl["boll"],
            window_pnl["sar"],
            len(self._pnl_rows),
            "（还不够回看，权重未改）" if len(self._pnl_rows) < self.lookback_trades else "",
        )

    def evaluate(self, symbol, bars, now=None, trail_bars=None, trend_bars=None):
        decision = super().evaluate(
            symbol, bars, now=now, trail_bars=trail_bars, trend_bars=trend_bars
        )
        if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            meta = dict(decision.meta or {})
            meta["weights"] = {
                "ma": self.params.w_ma,
                "turtle": self.params.w_turtle,
                "boll": self.params.w_boll,
                "sar": self.params.w_sar,
            }
            meta["adapt"] = True
            decision.meta = meta
        return decision
