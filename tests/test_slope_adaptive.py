"""自适应斜率阈值：逐根角度、分位阈值、斜率门与均线波段接入。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.ema_swing import _stop_ma_too_flat
from src.strategy.indicators import average_true_range
from src.strategy.slope import (
    SlopeGate,
    SlopeGateConfig,
    _trading_day_key,
    adaptive_angle_threshold,
    adaptive_threshold_from_bars,
    latest_atr,
    load_slope_gate,
    measure_slope,
    slope_angle_series,
)


def _frame(closes: list[float] | np.ndarray, spread: float = 1.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min")
    c = pd.Series(list(closes), index=idx, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 1.0},
        index=idx,
    )


def _ramp(steep: int, gentle: int, steep_step: float, gentle_step: float) -> list[float]:
    vals = [100.0]
    for _ in range(steep):
        vals.append(vals[-1] + steep_step)
    for _ in range(gentle):
        vals.append(vals[-1] + gentle_step)
    return vals


def test_angle_series_matches_single_measure() -> None:
    """逐根角度最后几点必须和 measure_slope 同口径，避免阈值与展示不一致。"""
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, 300))
    df = _frame(closes)
    ema = df["close"].ewm(span=20, adjust=False).mean()
    atr_s = average_true_range(df["high"], df["low"], df["close"], 14)
    series = slope_angle_series(ema, atr_s, lookback=20)
    for offset in (1, 5, 40):
        part = ema.iloc[: len(ema) - offset + 1]
        single = measure_slope(part, float(atr_s.iloc[len(ema) - offset]), lookback=20)
        assert single is not None
        assert series.iloc[len(ema) - offset] == pytest.approx(single.angle_deg, abs=1e-9)


def test_angle_series_diff_matches_single_measure() -> None:
    rng = np.random.default_rng(11)
    closes = 50 + np.cumsum(rng.normal(0, 0.2, 120))
    df = _frame(closes)
    ema = df["close"].ewm(span=10, adjust=False).mean()
    atr_s = average_true_range(df["high"], df["low"], df["close"], 14)
    series = slope_angle_series(ema, atr_s, lookback=10, method="diff")
    single = measure_slope(ema, float(atr_s.iloc[-1]), lookback=10, method="diff")
    assert single is not None
    assert series.iloc[-1] == pytest.approx(single.angle_deg, abs=1e-9)


def test_adaptive_threshold_clips_to_band() -> None:
    hot = pd.Series([10.0] * 100)
    calm = pd.Series([0.5] * 100)
    assert adaptive_angle_threshold(hot, 3.0) == pytest.approx(3.9)
    assert adaptive_angle_threshold(calm, 3.0) == pytest.approx(2.1)
    assert adaptive_angle_threshold(hot, 0.0) == 0.0


def test_adaptive_threshold_falls_back_without_enough_samples() -> None:
    assert adaptive_angle_threshold(pd.Series([10.0] * 10), 3.0) == 3.0


def test_adaptive_threshold_uses_recent_window() -> None:
    mixed = pd.Series([10.0] * 200 + [1.0] * 100)
    assert adaptive_angle_threshold(mixed, 3.0, window=100) == pytest.approx(2.1)


def test_gate_adaptive_raises_threshold() -> None:
    """历史很陡、当前只有 3.2°：固定 3° 放行，自适应阈值抬到 3.9° 就拦下。"""
    df = _frame(_ramp(80, 30, 0.35, 0.1118))
    ema = df["close"].ewm(span=2, adjust=False).mean()
    single = measure_slope(ema, latest_atr(df, df["close"]), lookback=2)
    assert single is not None
    assert 3.0 < single.angle_deg < 3.9

    fixed = SlopeGate(SlopeGateConfig(enabled=True, ema_period=2, lookback=2, min_deg=3.0))
    ok, _ = fixed.check(df, "long")
    assert ok

    adaptive = SlopeGate(
        SlopeGateConfig(
            enabled=True,
            ema_period=2,
            lookback=2,
            min_deg=3.0,
            adaptive=True,
            adaptive_quantile=0.8,
            adaptive_window=100,
        )
    )
    ok2, why = adaptive.check(df, "long")
    assert not ok2
    assert "自适应阈值" in why


def test_ema_swing_adaptive_raises_threshold() -> None:
    vals = _ramp(80, 30, 0.35, 0.1118)
    df = _frame(vals)
    ema = pd.Series(vals, index=df.index, dtype=float)
    close_s = df["close"]

    fixed, _ = _stop_ma_too_flat(
        "long", ema, df, close_s, lookback=2, min_deg=3.0, period=2
    )
    assert not fixed

    blocked, msg = _stop_ma_too_flat(
        "long",
        ema,
        df,
        close_s,
        lookback=2,
        min_deg=3.0,
        period=2,
        adaptive=True,
        adaptive_quantile=0.8,
        adaptive_window=100,
    )
    assert blocked
    assert "自适应阈值" in msg


def test_load_slope_gate_parses_adaptive() -> None:
    cfg = load_slope_gate(
        {
            "slope_gate": {
                "enabled": True,
                "adaptive": True,
                "adaptive_quantile": 0.9,
                "adaptive_window": 300,
            }
        }
    )
    assert cfg.adaptive is True
    assert cfg.adaptive_quantile == pytest.approx(0.9)
    assert cfg.adaptive_window == 300


def test_load_slope_gate_adaptive_defaults_off() -> None:
    cfg = load_slope_gate({"slope_gate": {"enabled": True}})
    assert cfg.adaptive is False
    assert cfg.adaptive_quantile == pytest.approx(0.80)
    assert cfg.adaptive_window == 500
    assert cfg.adaptive_daily is True


def test_trading_day_key_rolls_at_night() -> None:
    def key(hour: int, minute: int) -> str:
        idx = pd.DatetimeIndex([pd.Timestamp(2026, 11, 20, hour, minute)])
        frame = pd.DataFrame({"close": [100.0]}, index=idx)
        return _trading_day_key(frame)

    assert key(21, 15) != key(14, 45)


def test_daily_threshold_freezes_within_trading_day() -> None:
    """同一交易日只算一次；第二天才重算。"""
    steep = _frame(_ramp(80, 40, 0.35, 0.1118))
    calm = _frame(_ramp(80, 40, 0.05, 0.1118))
    cache: dict[str, tuple[str, float]] = {}
    first = adaptive_threshold_from_bars(
        steep,
        steep["close"].ewm(span=2, adjust=False).mean(),
        base_deg=3.0,
        lookback=2,
        window=100,
        daily=True,
        cache=cache,
        key="lh",
    )
    assert cache["lh"][1] == pytest.approx(first)
    assert first == pytest.approx(3.9)

    same_day = adaptive_threshold_from_bars(
        calm,
        calm["close"].ewm(span=2, adjust=False).mean(),
        base_deg=3.0,
        lookback=2,
        window=100,
        daily=True,
        cache=cache,
        key="lh",
    )
    assert same_day == pytest.approx(first)

    nxt = calm.copy()
    nxt.index = nxt.index + pd.offsets.Day(1)
    next_day = adaptive_threshold_from_bars(
        nxt,
        nxt["close"].ewm(span=2, adjust=False).mean(),
        base_deg=3.0,
        lookback=2,
        window=100,
        daily=True,
        cache=cache,
        key="lh",
    )
    assert next_day < first
    assert cache["lh"][1] == pytest.approx(next_day)


def test_gate_daily_cache_and_per_bar_switch() -> None:
    df = _frame(_ramp(80, 40, 0.35, 0.1118))
    base = dict(
        enabled=True,
        ema_period=2,
        lookback=2,
        min_deg=3.0,
        adaptive=True,
        adaptive_quantile=0.8,
        adaptive_window=100,
    )
    daily = SlopeGate(SlopeGateConfig(**base, adaptive_daily=True))
    daily.check(df, "long", symbol="lh")
    assert "lh" in daily._daily_cache

    per_bar = SlopeGate(SlopeGateConfig(**base, adaptive_daily=False))
    per_bar.check(df, "long", symbol="lh")
    assert per_bar._daily_cache == {}


def test_ema_swing_daily_freeze_keeps_threshold() -> None:
    """同一天换一段历史，冻结后的阈值不变，结论也不变。"""
    steep = _frame(_ramp(80, 40, 0.35, 0.1118))
    calm = _frame(_ramp(80, 40, 0.05, 0.1118))
    cache: dict[str, tuple[str, float]] = {}
    kw = dict(
        lookback=2,
        min_deg=3.0,
        period=2,
        adaptive=True,
        adaptive_quantile=0.8,
        adaptive_window=100,
        adaptive_daily=True,
        adaptive_cache=cache,
        symbol="lh",
    )
    blocked1, msg1 = _stop_ma_too_flat(
        "long", steep["close"].ewm(span=2, adjust=False).mean(), steep, steep["close"], **kw
    )
    blocked2, msg2 = _stop_ma_too_flat(
        "long", calm["close"].ewm(span=2, adjust=False).mean(), calm, calm["close"], **kw
    )
    assert blocked1 and blocked2
    assert "自适应阈值 3.9°" in msg1
    assert "自适应阈值 3.9°" in msg2
