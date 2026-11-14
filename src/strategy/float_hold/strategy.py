"""
日内浮盈飘单（编排）。

功能：把四个独立模块串起来跑。
     1. 方向选择器：现在默认「观察结束顺大方向」；也可以换成箱子或 MACD；
     2. 仓位控制：手数跟配置页左边的凯利 / 每笔风险 / 仓位上限，本策略不算另一套；
     3. 仓单生命周期：观察窗口、开仓截止、收盘清仓、日盘最多两枪；
     4. 止损控制：开盘前 ATR 宽度 + 浮盈保本。
原理：ETH 画箱子、截止钟点、跳空/箱宽/急破闸仍复用日内实体已经有的工具。
     和日内实体的差别只在这四个模块的实现，不改日内实体本身。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any

import pandas as pd

from src.strategy.daily_body import DailyBodyStrategy, _atr_mult
from src.strategy.market_state import MarketFilter, range_params_for_opening_break
from src.strategy.types import PositionState, StrategyAction, StrategyDecision

from .direction import DirectionContext, DirectionSelector, make_direction
from .lifecycle import SessionLifecycle
from .size import PublicRiskSize
from .stop import AtrWidthStop, atr_width_stop
from .trend_gate import TrendAlignGate, gate_minutes_from_host

# 给外部测试和旧 import 用，止损公式仍从本包能直接拿到。
__all__ = ["FloatHoldStrategy", "atr_width_stop"]


class FloatHoldStrategy(DailyBodyStrategy):
    """等开盘噪音走完，顺着当天方向拿，浮盈够了保本，拿到收盘。"""

    SIGNAL_NAME = "float_hold"
    SECTION = "float_hold"
    _DEFAULT_ETH = 60
    _DEFAULT_ETH2 = 0

    def __init__(self, cfg: dict[str, Any], *, direction: DirectionSelector | None = None) -> None:
        super().__init__(cfg)
        body = (cfg.get("strategy") or {}).get(self.SECTION) or {}
        # 跳空顺向闸。出厂关：错了用止损认赔，不再因为跳空少开。
        self.gap_align_atr_mult = _atr_mult(body.get("gap_align_atr_mult"), 0.0)
        # 止损离进场价几倍开盘前 ATR。出厂 1.5，换品种时跟着波动走。
        self.stop_atr_mult = _atr_mult(body.get("stop_atr_mult"), 1.5)
        if self.stop_atr_mult <= 0:
            self.stop_atr_mult = 1.5
        # 浮盈达到几倍开盘前 ATR 后保本。0=不做保本。出厂 2。
        self.be_atr_mult = _atr_mult(body.get("be_atr_mult"), 2.0)
        # 开仓时用的 ATR 先记在待成交里，成交后再冻到这笔单上，保本不再跟盘中 ATR 变。
        self._pending_atr: dict[str, float] = {}
        self._entry_atr: dict[str, float] = {}
        self._last_stop_atr: float | None = None
        self._last_stop_width: float = 0.0

        # 四个可替换模块。方向选择器可以在测试里直接注入，方便试下一种定方向办法。
        self.direction: DirectionSelector = direction or make_direction(
            body.get("direction"), body
        )
        self.lifecycle = SessionLifecycle()
        self.stop_ctl = AtrWidthStop()
        self.size = PublicRiskSize(cfg)
        self.trend_gate = TrendAlignGate(body)

    def _make_market_filter(self, cfg: dict[str, Any]):
        """默认关掉 ADX 过滤：这套靠晚开仓和跳空顺向，不靠「是不是震荡」。"""
        return MarketFilter(
            range_params_for_opening_break(cfg, "float_hold", default_enabled=False)
        )

    def _engine_title(self) -> str:
        return "日内浮盈飘单"

    def _direction_signal(
        self,
        bars: pd.DataFrame,
        now: datetime,
        anchor: dtime,
        eth: int,
        symbol: str = "",
        second: bool = False,
    ) -> tuple[str, str]:
        """把现场交给方向选择器。没有方向时返回 none，后面不会开仓。"""
        pick = self.direction.pick(
            DirectionContext(
                symbol=symbol or (self.symbols[0] if self.symbols else ""),
                bars=bars,
                now=now,
                anchor=anchor,
                eth_minutes=int(eth),
                timeframe=self.timeframe,
                second_try=bool(second),
                host=self,
            )
        )
        side = pick.side if pick.side in {"long", "short"} else "none"
        if side == "none":
            return side, pick.reason
        # 这一套方向本身就是顺着均线，不要再过滤一次。
        if getattr(self.direction, "uses_trend_gate", False):
            return side, pick.reason
        gated = self.trend_gate.check(
            side=side,
            bars=bars,
            now=now,
            timeframe=self.timeframe,
            macd_minutes=gate_minutes_from_host(self),
        )
        if not gated.ok:
            return "none", gated.reason
        extra = gated.reason
        if extra:
            return side, f"{pick.reason}。{extra}"
        return side, pick.reason

    def _session_limit(self, anchor: dtime, symbol: str | None = None) -> int:
        """
        日盘同一口箱子最多两枪：第一枪止损后、截止前再试一次。
        夜盘仍只开一次。夜日连续：夜盘一枪 + 日盘最多两枪。
        """
        if self._linked():
            return 3
        if self._is_night_anchor(anchor, symbol):
            return 1
        return 2

    def _uses_opening_box(self) -> bool:
        """MACD 循环不定箱子，开仓不等观察窗口走完。"""
        return bool(getattr(self.direction, "needs_opening_box", True))

    def _eth_plan(
        self, symbol: str, count: int, now: datetime, anchor: dtime
    ) -> tuple[int, str]:
        """箱子和「观察结束顺大方向」都要等窗口；MACD 循环把窗口当成 0。"""
        wait_eth = bool(
            getattr(self.direction, "needs_eth_wait", self._uses_opening_box())
        )
        if not wait_eth:
            del count, now
            if self._is_night_anchor(anchor, symbol):
                return 0, "night"
            return 0, "first"
        if self._linked():
            _minutes, kind = super()._eth_plan(symbol, count, now, anchor)
            return self.eth_minutes, kind
        del count, now
        if self._is_night_anchor(anchor, symbol):
            return self.eth_minutes, "night"
        return self.eth_minutes, "first"

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        """成交后把开仓时的 ATR 冻在这笔单上，后面保本按这个数，不再重算。"""
        super().on_filled(symbol, action, price, volume, initial_stop)
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            atr = self._pending_atr.pop(symbol, None)
            if atr is None:
                entry = float(price or 0)
                stop = float(initial_stop or 0)
                if entry > 0 and stop > 0 and self.stop_atr_mult > 0:
                    atr = abs(entry - stop) / self.stop_atr_mult
            if atr is not None and atr > 1e-12:
                self._entry_atr[symbol] = float(atr)
            return
        if action == StrategyAction.CLOSE:
            self._pending_atr.pop(symbol, None)
            self._entry_atr.pop(symbol, None)

    def _open_stop(
        self,
        side: str,
        box_low: float,
        box_high: float,
        tick_pad: float,
        entry: float | None = None,
        atr: float | None = None,
    ) -> float:
        """止损交给止损模块：进场价往回 stop_atr_mult 倍开盘前 ATR。"""
        del box_low, box_high, tick_pad
        return self.stop_ctl.initial_stop(self, side=side, entry=float(entry or 0), atr=atr)

    def _effective_stop(self, pos: PositionState) -> float:
        return self.stop_ctl.effective_stop(pos)

    def _frozen_atr(self, pos: PositionState) -> float | None:
        return self.stop_ctl.frozen_atr(self, pos)

    def _maybe_breakeven(self, pos: PositionState, bars: pd.DataFrame) -> None:
        self.stop_ctl.maybe_breakeven(self, pos, bars)

    def _hold_extra(self, pos: PositionState) -> str:
        return self.stop_ctl.hold_extra(pos)

    def _stop_hit_text(self, pos: PositionState, stop: float) -> str:
        return self.stop_ctl.stop_hit_text(self, pos, stop)

    def _open_stop_text(
        self, side: str, box_low: float, box_high: float, tick_pad: float
    ) -> str:
        del side, box_low, box_high, tick_pad
        return self.stop_ctl.open_stop_text(self)

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        """开仓时记下 ATR，并在决定上标明四个模块各自做了什么。"""
        decision = super().evaluate(
            symbol, bars, now=now, trail_bars=trail_bars, trend_bars=trend_bars
        )
        decision.meta.setdefault("direction", getattr(self.direction, "key", "opening_box"))
        decision.meta.setdefault("lifecycle", self.lifecycle.key)
        decision.meta.setdefault("stop_ctl", self.stop_ctl.key)
        if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            atr = self._last_stop_atr
            if atr is not None:
                self._pending_atr[symbol] = atr
            self.size.annotate(decision)
        return decision

    def _gap_align_block(
        self,
        symbol: str,
        side: str,
        bars: pd.DataFrame,
        now: datetime,
        anchor: dtime,
        text: str,
        second: bool,
    ) -> StrategyDecision | None:
        """
        跳空已经大到能看出方向时，不对着缺口填。

        高开只允许做多，低开只允许做空。跳空太小当没有方向，两边都还能做。
        第二枪发生在盘中，不再用开盘跳空拦。
        这一层仍算方向约束，以后若某种选择器自己处理跳空，可以关掉 gap_align_atr_mult。
        """
        if second or self.gap_align_atr_mult <= 0:
            return None
        gap = self._session_gap(bars, now, anchor)
        atr = self._pre_open_atr(bars, now, anchor)
        if gap is None or atr is None:
            return None
        size, _open_px, _prev = gap
        need = self.gap_align_atr_mult * atr
        if abs(size) < need:
            return None
        if size > 0 and side == "short":
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{text}。这一段开盘相对上一段跳空 {size:+.0f} 点"
                f"（{abs(size) / atr:.1f} 倍ATR，顺向门槛 {self.gap_align_atr_mult:.1f} 倍"
                f" = {need:.0f} 点），高开只做多，这段不做空去填缺口",
                meta={"blocked": True, "side": side, "block_key": "gap_align"},
            )
        if size < 0 and side == "long":
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{text}。这一段开盘相对上一段跳空 {size:+.0f} 点"
                f"（{abs(size) / atr:.1f} 倍ATR，顺向门槛 {self.gap_align_atr_mult:.1f} 倍"
                f" = {need:.0f} 点），低开只做空，这段不做多去填缺口",
                meta={"blocked": True, "side": side, "block_key": "gap_align"},
            )
        return None
