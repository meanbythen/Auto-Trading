"""
通用止损（独立于各策略自身的止损）。

功能：在策略自己的止损之外再加一道公共止损，谁先触发就按谁平仓。
     可选：SAR 移动止损、前 N 根高低点止损、ATR 吊灯（Chandelier）跟踪、
     盈利1ATR后保本；也能选关闭。
原理：止损线按「止损周期」（trail_timeframe）的 K 线算，不绑定具体策略：
     - SAR：按止损周期逐根算抛物线 SAR；刚开仓时 SAR 可能还在持仓反方向，
       先等它转同向再当止损用，避免一开仓就被反向的 SAR 误触。
     - 前高低点：多单守最近 N 根最低价，空单守最近 N 根最高价；跌破/站上就平。
     - ATR 吊灯：多单 = 最近 N 根最高 - k×ATR，空单 = 最近 N 根最低 + k×ATR。
     - 盈利1ATR后保本：开仓后浮盈达到 k×ATR，才把止损抬到开仓价；之前不生效。
触发口径沿用全策略共用的 strategy.stop_mode（touch 触及即止损 / close 收盘价确认），
并且只从「开仓之后」的 K 线开始判断，开仓那根自己的影线不算。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .indicators import average_true_range, parabolic_sar_next_state
from .types import STOP_MODE_CLOSE, normalize_stop_mode, stop_triggered

GENERIC_STOP_OFF = "off"
GENERIC_STOP_SAR = "sar"
GENERIC_STOP_PRIOR_EXTREME = "prior_extreme"
GENERIC_STOP_CHANDELIER = "chandelier"
GENERIC_STOP_BREAKEVEN = "breakeven"

GENERIC_STOP_LABELS: dict[str, str] = {
    GENERIC_STOP_OFF: "关闭",
    GENERIC_STOP_SAR: "SAR 移动止损",
    GENERIC_STOP_PRIOR_EXTREME: "前高低点止损",
    GENERIC_STOP_CHANDELIER: "Chandelier 吊灯跟踪",
    GENERIC_STOP_BREAKEVEN: "盈利1ATR后保本",
}


def normalize_generic_stop(value: Any) -> str:
    """把配置/界面写法收成五选一。认不出的按关闭，避免意外多平仓。"""
    text = str(value or "").strip().lower().replace(" ", "")
    if text in {"sar", "抛物线", "抛物线sar", "sar移动止损"}:
        return GENERIC_STOP_SAR
    if text in {
        "prior_extreme",
        "prior",
        "extreme",
        "前高低点",
        "前高前低",
        "前高低点止损",
        "高低点",
        "nbar",
        "donchian",
    }:
        return GENERIC_STOP_PRIOR_EXTREME
    if text in {
        "chandelier",
        "atr",
        "atr吊灯",
        "atr吊灯止损",
        "吊灯",
        "吊灯止损",
        "chandelier吊灯跟踪",
        "chandelier跟踪",
    }:
        return GENERIC_STOP_CHANDELIER
    if text in {
        "breakeven",
        "be",
        "保本",
        "保本止损",
        "盈利1atr后保本",
        "盈利后保本",
        "move_to_breakeven",
    }:
        return GENERIC_STOP_BREAKEVEN
    return GENERIC_STOP_OFF


def generic_stop_label(value: Any) -> str:
    return GENERIC_STOP_LABELS[normalize_generic_stop(value)]


def generic_stop_from_label(label: Any) -> str:
    text = str(label or "").strip()
    for code, name in GENERIC_STOP_LABELS.items():
        if text == name or text == code:
            return code
    return normalize_generic_stop(text)


@dataclass(frozen=True)
class GenericStopConfig:
    kind: str = GENERIC_STOP_OFF
    lookback: int = 20
    sar_af_start: float = 0.02
    sar_af_step: float = 0.02
    sar_af_max: float = 0.2
    atr_period: int = 14
    atr_mult: float = 3.0
    # 保本止损：浮盈达到几倍 ATR 后把止损抬到开仓价。
    be_atr_mult: float = 1.0

    @property
    def enabled(self) -> bool:
        return self.kind != GENERIC_STOP_OFF


def _float(value: Any, default: float) -> float:
    try:
        got = float(value)
    except (TypeError, ValueError):
        return default
    return got if got > 0 else default


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        got = int(value)
    except (TypeError, ValueError):
        got = default
    return max(low, min(high, got))


def generic_stop_from_cfg(cfg: dict[str, Any]) -> GenericStopConfig:
    """读 strategy.generic_stop；没写就是关闭。"""
    st = (cfg or {}).get("strategy") or {}
    raw = st.get("generic_stop") or {}
    if not isinstance(raw, dict):
        raw = {}
    return GenericStopConfig(
        kind=normalize_generic_stop(raw.get("kind", raw.get("type"))),
        lookback=_int(raw.get("lookback"), 20, 2, 200),
        sar_af_start=_float(raw.get("sar_af_start"), 0.02),
        sar_af_step=_float(raw.get("sar_af_step"), 0.02),
        sar_af_max=_float(raw.get("sar_af_max"), 0.2),
        atr_period=_int(raw.get("atr_period"), 14, 2, 200),
        atr_mult=_float(raw.get("atr_mult"), 3.0),
        be_atr_mult=_float(raw.get("be_atr_mult"), 1.0),
    )


@dataclass(frozen=True)
class GenericStopHit:
    kind: str
    stop: float
    reason: str
    # 碰到止损线的那根K线（回测按它判跳空成交）；实盘用不到，可以为空。
    bar: Any = field(default=None, repr=False, compare=False)


def _ts(value: Any) -> pd.Timestamp | None:
    try:
        got = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if got.tzinfo is not None:
        got = got.tz_localize(None)
    return got


def _entry_time(pos: Any) -> pd.Timestamp | None:
    return _ts(getattr(pos, "opened_at", None))


def _index_after(index: Any, entry: pd.Timestamp | None) -> list[bool] | None:
    if entry is None:
        return None
    try:
        idx = pd.to_datetime(index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        return [bool(x) for x in (idx > entry)]
    except (TypeError, ValueError):
        return None


def _sar_next_state(cfg: GenericStopConfig, bars: pd.DataFrame) -> tuple[float, bool] | None:
    """按这些已收盘K线往前推一根 SAR，返回下一根的价位和方向；数据不够返回 None。"""
    if bars is None or len(bars) < 4:
        return None
    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")
    if high.isna().all() or low.isna().all():
        return None
    state = parabolic_sar_next_state(
        high,
        low,
        af_start=cfg.sar_af_start,
        af_step=cfg.sar_af_step,
        af_max=cfg.sar_af_max,
    )
    if state is None or pd.isna(state[0]):
        return None
    return state


def _bars_after(bars: pd.DataFrame, entry: pd.Timestamp | None) -> pd.DataFrame:
    """只留开仓那根之后（不含）的K线；判断不了开仓时间就原样返回。"""
    usable = _index_after(bars.index, entry)
    if usable is None:
        return bars
    return bars.loc[usable]


def _stop_level(
    cfg: GenericStopConfig,
    side: str,
    bars: pd.DataFrame,
    *,
    entry_price: float = 0.0,
    entry_time: pd.Timestamp | None = None,
) -> tuple[float, str] | None:
    """
    用传入的这些已收盘K线，算出「下一根K线」要用的止损线；数据不够返回 None。

    统一口径：线只由这些K线算，不含正在判断的那一根，避免拿这根自己的
    高低点/收盘价算出的线去判它自己（SAR 翻向时会算到反方向）。
    """
    if bars is None or len(bars) == 0:
        return None
    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")
    close = pd.to_numeric(bars["close"], errors="coerce")
    if high.isna().all() or low.isna().all():
        return None

    if cfg.kind == GENERIC_STOP_SAR:
        # 用「下一根要碰到的 SAR 价」而不是最后一根的 SAR 值：
        # SAR 每根都会沿趋势再走一步，下一根的高/低越过这条线就翻向。
        state = _sar_next_state(cfg, bars)
        if state is None:
            return None
        return float(state[0]), "SAR"

    if cfg.kind == GENERIC_STOP_PRIOR_EXTREME:
        n = cfg.lookback
        if len(bars) < n:
            return None
        window = bars.tail(n)
        if side == "long":
            return float(pd.to_numeric(window["low"], errors="coerce").min()), f"前{n}根最低"
        return float(pd.to_numeric(window["high"], errors="coerce").max()), f"前{n}根最高"

    if cfg.kind == GENERIC_STOP_CHANDELIER:
        if len(bars) < max(cfg.lookback, cfg.atr_period) + 1:
            return None
        atr = average_true_range(high, low, close, cfg.atr_period)
        if atr is None or len(atr) == 0 or pd.isna(atr.iloc[-1]):
            return None
        window = bars.tail(cfg.lookback)
        if side == "long":
            top = float(pd.to_numeric(window["high"], errors="coerce").max())
            return top - cfg.atr_mult * float(atr.iloc[-1]), f"吊灯 {cfg.atr_mult:g}×ATR"
        bottom = float(pd.to_numeric(window["low"], errors="coerce").min())
        return bottom + cfg.atr_mult * float(atr.iloc[-1]), f"吊灯 {cfg.atr_mult:g}×ATR"

    if cfg.kind == GENERIC_STOP_BREAKEVEN:
        # 浮盈（开仓后最有利价 - 开仓价）达到 k×ATR，才把止损抬到开仓价；
        # 没赚到 k×ATR 之前这条线不生效，仍由策略自身止损兜着。
        if entry_price <= 0:
            return None
        since = _bars_after(bars, entry_time)
        if len(since) == 0:
            return None
        atr = average_true_range(high, low, close, cfg.atr_period)
        if atr is None or len(atr) == 0 or pd.isna(atr.iloc[-1]):
            return None
        if side == "long":
            best = float(pd.to_numeric(since["high"], errors="coerce").max())
            gain = best - entry_price
        else:
            best = float(pd.to_numeric(since["low"], errors="coerce").min())
            gain = entry_price - best
        if pd.isna(gain) or gain < cfg.be_atr_mult * float(atr.iloc[-1]):
            return None
        return entry_price, f"保本（浮盈≥{cfg.be_atr_mult:g}×ATR）"

    return None


class GenericStopOverlay:
    """按品种记「通用止损是否已生效」，并给出谁先触发的平仓信号。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = generic_stop_from_cfg(cfg)
        self._armed: dict[str, bool] = {}
        self._signature: dict[str, tuple[str, float, str]] = {}
        self._last_hit: dict[str, tuple[tuple[str, float, str], float]] = {}

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def reset(self, symbol: str) -> None:
        self._armed.pop(symbol, None)
        self._signature.pop(symbol, None)
        self._last_hit.pop(symbol, None)

    def _sync_position(self, symbol: str, pos: Any) -> None:
        side = str(getattr(pos, "side", "flat") or "flat")
        if side == "flat":
            self.reset(symbol)
            return
        try:
            entry = round(float(getattr(pos, "entry_price", 0) or 0), 6)
        except (TypeError, ValueError):
            entry = 0.0
        # 开仓时刻也放进去：平掉后又在同价开新仓时，SAR 要重新武装。
        sig = (side, entry, str(getattr(pos, "opened_at", "") or ""))
        if self._signature.get(symbol) != sig:
            self._signature[symbol] = sig
            # SAR 刚开仓时可能还在持仓反方向，先不武装。
            self._armed[symbol] = False

    def check(
        self,
        symbol: str,
        pos: Any,
        bars: pd.DataFrame | None,
        *,
        forming: pd.DataFrame | None = None,
        mode: str = "touch",
        cooldown_sec: float = 0.0,
    ) -> GenericStopHit | None:
        """
        返回触发的通用止损；没触发/数据不够/还没到开仓后的K线，返回 None。

        bars 用止损周期的已收盘K线算止损线；forming 是还在走的最后那一小段，
        touch 模式下允许用它的影线，但它必须在开仓之后才开始。
        """
        cfg = self.cfg
        if not cfg.enabled:
            return None
        side = str(getattr(pos, "side", "flat") or "flat")
        if side not in {"long", "short"}:
            return None
        self._sync_position(symbol, pos)
        if bars is None or len(bars) == 0 or not {"high", "low", "close"}.issubset(bars.columns):
            return None

        entry = _entry_time(pos)
        try:
            entry_price = float(getattr(pos, "entry_price", 0) or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
        usable = _index_after(bars.index, entry)
        if usable is None:
            usable = [True] * len(bars)
        closed_after = bars.loc[usable] if usable else bars.iloc[0:0]
        forming_ok = False
        if forming is not None and len(forming) > 0:
            f_idx = _index_after(forming.index, entry)
            forming_ok = bool(f_idx is None or any(f_idx))

        close_mode = normalize_stop_mode(mode) == STOP_MODE_CLOSE
        if len(closed_after) == 0 and not (forming_ok and not close_mode):
            return None

        armed_before = bool(self._armed.get(symbol, False))
        level_now: tuple[float, str] | None = None
        if cfg.kind == GENERIC_STOP_SAR and not armed_before:
            # 先看 SAR 是否已转到持仓同侧；反方向先不武装，也不拿它当止损。
            # 用 SAR 的方向判，不用价位和收盘价比——刚收在极值时两者会相等，容易判错。
            state = _sar_next_state(cfg, bars)
            if state is None:
                return None
            want_bull = side == "long"
            if bool(state[1]) != want_bull:
                return None
            level_now = (float(state[0]), "SAR")
            self._armed[symbol] = True

        # 刚收线的那根：用「这根之前」的线判断。
        # SAR 本次才武装时跳过这根——SAR 翻向的那根不是止损，拿翻向前的线判它反而会误平。
        if len(closed_after) > 0 and (cfg.kind != GENERIC_STOP_SAR or armed_before):
            row = closed_after.iloc[-1]
            level_prev = _stop_level(
                cfg,
                side,
                bars.iloc[:-1],
                entry_price=entry_price,
                entry_time=entry,
            )
            side_txt = "多" if side == "long" else "空"
            if cfg.kind == GENERIC_STOP_SAR and armed_before:
                row_close = float(row["close"])
                prev_state = _sar_next_state(cfg, bars.iloc[:-1])
                # 这根之前 SAR 就已经翻到持仓反方向：说明更早那根就该走，按当前收盘平。
                flipped_before = prev_state is not None and bool(prev_state[1]) != (side == "long")
                if flipped_before:
                    return GenericStopHit(
                        kind=cfg.kind,
                        stop=row_close,
                        reason=f"通用止损（{GENERIC_STOP_LABELS[cfg.kind]}）：SAR 已翻向，按收盘平{side_txt}",
                        bar=row,
                    )
            hit = self._hit_if_triggered(
                symbol,
                side,
                row,
                level_prev,
                mode,
                cooldown_sec,
            )
            if hit is not None:
                return hit
            if close_mode:
                return None

        # 还在走的那根：线用全部已收盘K线算，只判开仓之后的部分。
        if forming_ok and not close_mode:
            if level_now is None:
                level_now = _stop_level(
                    cfg,
                    side,
                    bars,
                    entry_price=entry_price,
                    entry_time=entry,
                )
            return self._hit_if_triggered(
                symbol, side, forming.iloc[-1], level_now, mode, cooldown_sec
            )
        return None

    def _hit_if_triggered(
        self,
        symbol: str,
        side: str,
        row: Any,
        level_pair: tuple[float, str] | None,
        mode: str,
        cooldown_sec: float,
    ) -> GenericStopHit | None:
        """拿一根K线去碰止损线；碰到就返回命中（含实盘冷却），没碰到返回 None。"""
        if level_pair is None:
            return None
        level, name = level_pair
        if level <= 0:
            return None
        try:
            row_close = float(row["close"])
            row_high = float(row["high"])
            row_low = float(row["low"])
        except (TypeError, ValueError):
            return None
        if pd.isna(row_close) or pd.isna(row_high) or pd.isna(row_low):
            return None
        if not stop_triggered(
            side=side,
            stop=level,
            close=row_close,
            high=row_high,
            low=row_low,
            mode=mode,
        ):
            return None

        # 实盘里平仓单可能还没成交，持仓会连续几轮都满足条件；
        # 冷却期内不重复报警，避免同一根K线里连发平仓单。回测不需要（传 0）。
        if cooldown_sec > 0:
            sig = self._signature.get(symbol) or ("", 0.0, "")
            last = self._last_hit.get(symbol)
            now_wall = time.monotonic()
            if last is not None and last[0] == sig and now_wall - last[1] < cooldown_sec:
                return None
            self._last_hit[symbol] = (sig, now_wall)
        side_txt = "多" if side == "long" else "空"
        return GenericStopHit(
            kind=self.cfg.kind,
            stop=level,
            reason=f"通用止损（{GENERIC_STOP_LABELS[self.cfg.kind]}）：{name} {level:.2f} 触及，平{side_txt}",
            bar=row,
        )


def generic_stop_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    """给回测结果/参数快照记下当前通用止损设置。"""
    got = generic_stop_from_cfg(cfg)
    return {
        "kind": got.kind,
        "lookback": got.lookback,
        "sar_af_start": got.sar_af_start,
        "sar_af_step": got.sar_af_step,
        "sar_af_max": got.sar_af_max,
        "atr_period": got.atr_period,
        "atr_mult": got.atr_mult,
        "be_atr_mult": got.be_atr_mult,
    }


__all__ = [
    "GENERIC_STOP_OFF",
    "GENERIC_STOP_SAR",
    "GENERIC_STOP_PRIOR_EXTREME",
    "GENERIC_STOP_CHANDELIER",
    "GENERIC_STOP_BREAKEVEN",
    "GENERIC_STOP_LABELS",
    "GenericStopConfig",
    "GenericStopHit",
    "GenericStopOverlay",
    "generic_stop_from_cfg",
    "generic_stop_from_label",
    "generic_stop_label",
    "generic_stop_snapshot",
    "normalize_generic_stop",
]
