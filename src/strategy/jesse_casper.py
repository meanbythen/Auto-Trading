"""
Jesse Casper 简化策略（K 线 + 成交量 + 价值区）。

功能：按 Jesse Rogers 日内 SMC 骨架开平仓：大周期定方向，扫流动性后放量位移，
     回踩入场区才开；止损放在流动性外侧，止盈至少 3 倍风险；收盘清仓。
原理：原版要看足迹图、Delta、CVD，国内分钟 K 做不到。资料里的 vnpy 简化版
     用放量代替吸收，用价值区（VA）代替「平衡市不做」。本策略就是这一套的项目实现。
     不是原版订单流，假扫会比原版多，只适合先仿真、小仓位。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any

import pandas as pd

from src.utils.trading_day import futures_trading_day

from .jesse_va import (
    is_imbalance,
    previous_session_va,
    resample_minutes,
)
from .market_state import MarketFilter
from .trend_base import TrendStrategyBase
from .types import PositionState, StrategyAction, StrategyDecision, config_flag


def _hhmm(value: str, fallback: dtime) -> dtime:
    """把 09:00 这种文字收成钟点。"""
    try:
        hour, minute = str(value).strip().split(":")
        return dtime(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


class JesseCasperStrategy(TrendStrategyBase):
    """简化 Casper：15 分钟偏见 + 扫流动性 + 放量位移 + 价值区过滤。"""

    section = "jesse_casper"
    label = "Jesse Casper"
    uses_trend_bars = True

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        # 价值区自己当行情闸门，不再叠一层公共 ADX 过滤。
        self.market_filter = MarketFilter.from_cfg(
            {"strategy": {"market_filter": {"enabled": False}}}
        )
        self.trend_timeframe = str(self.box.get("trend_timeframe", "15") or "15")
        # 资料默认：信号 5 分钟，偏见 15 分钟。信号周期仍跟配置页公共项。
        self.lookback_liq = max(5, int(self.box.get("lookback_liq", 20) or 20))
        self.vol_ma_len = max(5, int(self.box.get("vol_ma_len", 20) or 20))
        self.vol_mult = float(self.box.get("vol_mult", 1.2) or 1.2)
        if self.vol_mult <= 0:
            self.vol_mult = 1.2
        self.body_ratio = float(self.box.get("displacement_body_ratio", 0.5) or 0.5)
        if not (0 < self.body_ratio < 1):
            self.body_ratio = 0.5
        self.min_rr = float(self.box.get("min_rr", 3.0) or 3.0)
        if self.min_rr < 1:
            self.min_rr = 3.0
        self.price_tick = float(self.box.get("price_tick", 1.0) or 1.0)
        if self.price_tick <= 0:
            self.price_tick = 1.0
        self.va_pct = float(self.box.get("va_pct", 0.70) or 0.70)
        self.use_va = config_flag(self.box.get("use_va", True), default=True)
        self.use_kill_zone = config_flag(self.box.get("use_kill_zone", True), default=True)
        self.use_daily_halt = config_flag(self.box.get("use_daily_halt", True), default=True)
        self.max_losses = max(1, int(self.box.get("max_losses_day", 2) or 2))
        self.max_wins = max(1, int(self.box.get("max_wins_day", 1) or 1))
        # 黄金时段：视频里夜盘 21:00–22:30、日盘 09:00–10:00。
        self.kill_start_day = _hhmm(str(self.box.get("kill_day_start", "09:00")), dtime(9, 0))
        self.kill_end_day = _hhmm(str(self.box.get("kill_day_end", "10:00")), dtime(10, 0))
        self.kill_start_night = _hhmm(str(self.box.get("kill_night_start", "21:00")), dtime(21, 0))
        self.kill_end_night = _hhmm(str(self.box.get("kill_night_end", "22:30")), dtime(22, 30))
        # 日内策略：默认到点清仓，不隔夜。
        self.flatten_at_close = config_flag(
            self.box.get("flatten_at_close", True), default=True
        )
        self.setup_wait = max(2, int(self.box.get("setup_wait_bars", 16) or 16))
        # 止盈看更远的高低点（下一个流动性），不要和扫单用同一扇 20 根窗口，否则几乎凑不够 3 倍。
        self.target_lookback = max(
            self.lookback_liq, int(self.box.get("target_lookback", 40) or 40)
        )
        # 扫流动性、位移、回踩是三步，不能挤在同一根 K 上（长影线和大实体互相打架）。
        self._sweep: dict[str, dict[str, Any]] = {}
        self._setup: dict[str, dict[str, Any]] = {}
        self._targets: dict[str, float] = {}
        self._va_cache: tuple[Any, Any] = (None, None)
        self._day = None
        self._wins = 0
        self._losses = 0

    def need_bars(self) -> int:
        # 15 分钟偏见要看约 20 根，折成 5 分钟大约 60 根；再留价值区一个完整时段。
        return max(80, self.lookback_liq + 5, self.vol_ma_len + 5, self.atr_period + 5)

    def _sync_day(self, now: datetime) -> None:
        day = futures_trading_day(now)
        if self._day != day:
            self._day = day
            self._wins = 0
            self._losses = 0
            self._setup.clear()
            self._sweep.clear()

    def _halted(self) -> bool:
        if not self.use_daily_halt:
            return False
        return self._losses >= self.max_losses or self._wins >= self.max_wins

    def _in_kill_zone(self, now: datetime) -> bool:
        if not self.use_kill_zone:
            return True
        t = now.time().replace(second=0, microsecond=0)
        day_ok = self.kill_start_day <= t <= self.kill_end_day
        night_ok = self.kill_start_night <= t <= self.kill_end_night
        return day_ok or night_ok

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        now = now or datetime.now()
        self._eval_symbol = symbol
        self._sync_day(now)
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        if pos.side == "flat":
            if self._halted():
                return StrategyDecision(
                    StrategyAction.HOLD,
                    symbol,
                    f"{self.label}今日已停手（赢{self._wins} 亏{self._losses}）",
                )
            # 新扫单只在黄金时段找。已经记下的回踩单，过了 10:00 仍允许成交（相当于限价还挂着）。
            armed = bool(self._setup.get(symbol) or self._sweep.get(symbol))
            if not armed and not self._in_kill_zone(now):
                return StrategyDecision(
                    StrategyAction.HOLD, symbol, f"{self.label}不在黄金时段"
                )
        return super().evaluate(
            symbol, bars, now=now, trail_bars=trail_bars, trend_bars=trend_bars
        )

    def prepare(self, bars: pd.DataFrame) -> dict[str, Any] | None:
        if bars is None or len(bars) < self.need_bars():
            return None
        atr = self._atr(bars)
        close = float(bars["close"].iloc[-1])
        vol = None
        vol_ma = None
        if "volume" in bars.columns:
            series = pd.to_numeric(bars["volume"], errors="coerce")
            vol = float(series.iloc[-1]) if pd.notna(series.iloc[-1]) else None
            window = series.iloc[-self.vol_ma_len :]
            if window.notna().any():
                vol_ma = float(window.mean())
        htf = self._htf_frame(bars)
        bias = self._bias(htf)
        stamp = bars.index[-1]
        area = None
        if self.use_va:
            cache_id = (len(bars), str(stamp), self.price_tick, self.va_pct)
            if self._va_cache[0] == cache_id:
                area = self._va_cache[1]
            else:
                from src.utils.session_clock import sessions_for_symbol

                live = getattr(self, "_eval_symbol", None) or (self.symbols[0] if self.symbols else "")
                area = previous_session_va(
                    bars,
                    now=stamp,
                    tick=self.price_tick,
                    value_pct=self.va_pct,
                    sessions=sessions_for_symbol(self.cfg, live),
                )
                self._va_cache = (cache_id, area)
        imbalance = (not self.use_va) or is_imbalance(close, area)
        return {
            "atr": atr,
            "vol": vol,
            "vol_ma": vol_ma,
            "bias": bias,
            "va": area,
            "imbalance": imbalance,
        }

    def _htf_frame(self, bars: pd.DataFrame) -> pd.DataFrame:
        """15 分钟图：优先用单独拉来的大周期，没有就把信号周期拼起来。"""
        extra = getattr(self, "_trend_bars", None)
        if extra is not None and len(extra) >= 20:
            return extra
        try:
            minutes = int(str(self.trend_timeframe).replace("m", "").replace("M", ""))
        except ValueError:
            minutes = 15
        signal = str(self.timeframe).strip()
        if signal in {str(minutes), f"{minutes}m", f"{minutes}M"}:
            return bars
        return resample_minutes(bars, minutes)

    @staticmethod
    def _bias(htf: pd.DataFrame) -> int:
        """近 20 根大周期：高点抬高且低点抬高=偏多；两边都降低=偏空。"""
        if htf is None or len(htf) < 20:
            return 0
        highs = htf["high"].iloc[-20:]
        lows = htf["low"].iloc[-20:]
        hh = float(highs.iloc[-10:].max())
        lh = float(highs.iloc[-20:-10].max())
        ll = float(lows.iloc[-10:].min())
        hl = float(lows.iloc[-20:-10].min())
        if hh > lh and ll > hl:
            return 1
        if ll < hl and hh < lh:
            return -1
        return 0

    def entry_signal(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        ind: dict[str, Any],
    ) -> tuple[str, float, str, dict[str, Any]] | None:
        symbol = str(getattr(self, "_eval_symbol", "") or self.symbols[0])
        setup_live = bool(self._setup.get(symbol) or self._sweep.get(symbol))
        # 已经记下的扫单/回踩，不因这一根又回到价值区或偏见变模糊而作废。
        if not ind.get("imbalance") and not setup_live:
            return None
        bias = int(ind.get("bias") or 0)
        if bias == 0 and not setup_live:
            return None
        vol = ind.get("vol")
        vol_ma = ind.get("vol_ma")
        vol_ok = True
        if vol is not None and vol_ma is not None and vol_ma > 0:
            vol_ok = vol > vol_ma * self.vol_mult
        i = len(bars) - 1
        if i < self.lookback_liq + 1:
            return None
        window = bars.iloc[i - self.lookback_liq : i]
        swing_high = float(window["high"].max())
        swing_low = float(window["low"].min())
        high = float(bar["high"])
        low = float(bar["low"])
        open_px = float(bar["open"])
        close = float(bar["close"])
        rng = high - low
        if rng <= 0:
            return None

        setup = self._setup.get(symbol)
        if setup and int(setup.get("expire_i", 0)) < i:
            setup = None
            self._setup.pop(symbol, None)
        sweep = self._sweep.get(symbol)
        if sweep and int(sweep.get("expire_i", 0)) < i:
            sweep = None
            self._sweep.pop(symbol, None)

        # 第三步：价格重新碰到入场区（影线扫到即可，不必收盘卡在窄区间里）。
        if setup:
            side = setup["side"]
            top = float(setup["zone_top"])
            btm = float(setup["zone_btm"])
            touched = (low <= top and close >= btm) if side == "long" else (high >= btm and close <= top)
            if touched:
                stop = float(setup["stop"])
                target = float(setup["target"])
                risk = (close - stop) if side == "long" else (stop - close)
                reward = (target - close) if side == "long" else (close - target)
                if risk > 0 and reward / risk >= self.min_rr:
                    self._setup.pop(symbol, None)
                    self._sweep.pop(symbol, None)
                    self._open_target = target
                    reason = (
                        f"{self.label}回踩入场 {side} "
                        f"止损{stop:.2f} 目标{target:.2f} RR≥{self.min_rr:g}"
                    )
                    return side, stop, reason, {"target": target, "bias": bias}

        body = close - open_px
        # 第二步：扫完之后，另找一根放量大实体当位移（可以是后面几根，不必跟扫单同一根）。
        if sweep and vol_ok:
            side = sweep["side"]
            liq = float(sweep["liq"])
            # 止损钉在被扫掉的流动性外侧一点点，用扫那根的波幅，不用后面大实体的波幅。
            buffer = float(sweep.get("buffer") or max(self.price_tick * 2, rng * 0.3))
            ext = bars.iloc[max(0, i - self.target_lookback) : i]
            disp = False
            if side == "long" and body > 0 and body / rng > self.body_ratio and close > liq:
                disp = True
                zone_top = open_px
                zone_btm = low
                stop = liq - buffer
                target = max(float(ext["high"].max()) if len(ext) else swing_high, high)
                rr = (target - zone_top) / max(zone_top - stop, self.price_tick)
            elif side == "short" and body < 0 and abs(body) / rng > self.body_ratio and close < liq:
                disp = True
                zone_top = high
                zone_btm = open_px
                stop = liq + buffer
                target = min(float(ext["low"].min()) if len(ext) else swing_low, low)
                rr = (zone_btm - target) / max(stop - zone_btm, self.price_tick)
            else:
                rr = 0.0
            if disp and rr >= self.min_rr:
                self._setup[symbol] = {
                    "side": side,
                    "zone_top": zone_top,
                    "zone_btm": zone_btm,
                    "stop": stop,
                    "target": target,
                    "expire_i": i + self.setup_wait,
                }
                self._sweep.pop(symbol, None)

        # 第一步：黄金时段里记下扫流动性。长影刺破即可，不要求这根本身是大实体。
        stamp = None
        try:
            stamp = pd.Timestamp(bar.name).to_pydatetime()
        except (TypeError, ValueError):
            stamp = None
        allow_new_sweep = stamp is None or self._in_kill_zone(stamp)
        if allow_new_sweep and not setup:
            buffer = max(self.price_tick * 2, rng * 0.3)
            if bias == 1 and low < swing_low and close > swing_low:
                self._sweep[symbol] = {
                    "side": "long",
                    "liq": swing_low,
                    "buffer": buffer,
                    "expire_i": i + self.setup_wait,
                }
            elif bias == -1 and high > swing_high and close < swing_high:
                self._sweep[symbol] = {
                    "side": "short",
                    "liq": swing_high,
                    "buffer": buffer,
                    "expire_i": i + self.setup_wait,
                }
        return None

    def exit_reason(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> str | None:
        target = self._targets.get(pos.symbol)
        if target is None:
            return None
        high = float(bar["high"])
        low = float(bar["low"])
        if pos.side == "long" and high >= target:
            return f"{self.label}止盈 {target:.2f}"
        if pos.side == "short" and low <= target:
            return f"{self.label}止盈 {target:.2f}"
        return None

    def trail_stop(
        self,
        bars: pd.DataFrame,
        bar: pd.Series,
        pos: PositionState,
        ind: dict[str, Any],
    ) -> float:
        # 资料：止损钉在流动性外侧，不往有利方向收，避免还没走到 3 倍就被洗出去。
        return float(pos.initial_stop or pos.trail_stop or 0.0)

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        pos = self.positions.get(symbol)
        side = pos.side if pos is not None else "flat"
        entry = float(pos.entry_price or 0.0) if pos is not None else 0.0
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            target = getattr(self, "_open_target", None)
            super().on_filled(symbol, action, price, volume, initial_stop)
            if target is not None:
                self._targets[symbol] = float(target)
            return
        super().on_filled(symbol, action, price, volume, initial_stop)
        if action == StrategyAction.CLOSE and side in {"long", "short"} and entry:
            pnl = (price - entry) if side == "long" else (entry - price)
            if pnl > 0:
                self._wins += 1
            elif pnl < 0:
                self._losses += 1
            self._targets.pop(symbol, None)
            self._setup.pop(symbol, None)
