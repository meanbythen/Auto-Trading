"""均线斜率共用模块：估计、t 值、三态与旧算法一致性。"""

import math

import numpy as np
import pandas as pd
import pytest

from src.strategy.indicators import ema_angle_deg, ema_series
from src.strategy.slope import (
    SlopeConfig,
    SlopeFilter,
    evaluate_slope,
    measure_slope,
)


def _line(angle_deg: float, n: int = 20, base: float = 100.0) -> pd.Series:
    slope = math.tan(math.radians(angle_deg))
    return pd.Series(base + slope * np.arange(n + 1, dtype=float))


def test_straight_line_slope_angle_and_quality() -> None:
    ema = _line(6.0)
    m = measure_slope(ema, 1.0, lookback=20)
    assert m is not None
    assert m.slope == pytest.approx(math.tan(math.radians(6.0)))
    assert m.angle_deg == pytest.approx(6.0, abs=1e-9)
    assert m.r2 == pytest.approx(1.0)
    assert m.t_stat > 1e3  # 完美直线：标准误接近 0，t 值极大


def test_flat_line_has_zero_angle() -> None:
    ema = pd.Series([100.0] * 21)
    m = measure_slope(ema, 1.0, lookback=20)
    assert m is not None
    assert m.angle_deg == pytest.approx(0.0)
    assert m.slope == pytest.approx(0.0)
    assert m.t_stat == 0.0


def test_regression_dampens_endpoint_spike() -> None:
    flat = [100.0] * 20 + [106.0]
    ema = pd.Series(flat)
    reg = measure_slope(ema, 1.0, lookback=20, method="reg")
    diff = measure_slope(ema, 1.0, lookback=20, method="diff")
    assert reg is not None and diff is not None
    assert reg.angle_deg < diff.angle_deg


def test_noisy_line_has_lower_t_than_clean_line() -> None:
    clean = _line(6.0)
    noisy = clean + pd.Series([0.0, 5.0] * 10 + [0.0])
    clean_m = measure_slope(clean, 1.0, lookback=20)
    noisy_m = measure_slope(noisy, 1.0, lookback=20)
    assert clean_m is not None and noisy_m is not None
    assert abs(noisy_m.t_stat) < abs(clean_m.t_stat)


def test_three_state_and_hysteresis() -> None:
    cfg = SlopeConfig(lookback=20, entry_deg=5.0, exit_deg=4.0)
    up = measure_slope(_line(6.0), 1.0, lookback=20)
    mid = measure_slope(_line(4.5), 1.0, lookback=20)
    flat = measure_slope(_line(3.0), 1.0, lookback=20)
    assert up is not None and mid is not None and flat is not None
    assert evaluate_slope(up, cfg).regime == "up"
    assert evaluate_slope(mid, cfg).regime == "neutral"
    assert evaluate_slope(mid, cfg, prev_direction=1).direction == 1
    assert evaluate_slope(mid, cfg, prev_direction=0).direction == 0
    assert evaluate_slope(flat, cfg).regime == "flat"


def test_side_and_price_side_block() -> None:
    cfg = SlopeConfig(lookback=20, entry_deg=5.0)
    up = measure_slope(_line(6.0), 1.0, lookback=20)
    assert up is not None
    assert evaluate_slope(up, cfg, side="long").direction == 1
    assert evaluate_slope(up, cfg, side="short").direction == 0
    blocked = evaluate_slope(
        up,
        SlopeConfig(lookback=20, entry_deg=5.0, price_side=True),
        side="long",
        close=99.0,
        ema_value=100.0,
    )
    assert blocked.direction == 0
    assert "不在均线" in blocked.reason


def test_min_t_gate_blocks_noisy_slope() -> None:
    base = _line(6.0)
    noisy = base + pd.Series([0.0, 9.0] * 10 + [0.0])
    m = measure_slope(noisy, 1.0, lookback=20)
    assert m is not None
    assert m.angle_deg >= 5.0
    state = evaluate_slope(m, SlopeConfig(lookback=20, entry_deg=5.0, min_t=2.0))
    assert state.direction == 0
    assert "t 值" in state.reason


def test_short_series_and_nan_return_unknown() -> None:
    assert measure_slope(pd.Series([100.0] * 5), 1.0, lookback=20) is None
    ema = _line(6.0)
    ema.iloc[-1] = float("nan")
    assert measure_slope(ema, 1.0, lookback=20) is None
    state = SlopeFilter(SlopeConfig(lookback=20)).evaluate(ema, 1.0)
    assert state.regime == "unknown"
    assert state.direction == 0


def test_measure_matches_legacy_ema_swing_formula() -> None:
    """与重构前 ema_swing._stop_ma_too_flat 的算法逐根一致。"""
    prices = pd.Series(
        np.linspace(100.0, 140.0, 220)
        + 4.0 * np.sin(np.arange(220, dtype=float) / 6.0)
    )
    ema = ema_series(prices, 30)
    atr = 1.7
    n = 20

    def legacy(end: int, method: str) -> float:
        part = ema.iloc[: end + 1]
        now_v = float(part.iloc[-1])
        if method == "reg":
            vals = [float(v) for v in pd.to_numeric(part.iloc[-(n + 1) :], errors="coerce")]
            m = len(vals)
            mean_x = (m - 1) / 2.0
            mean_y = sum(vals) / m
            den = sum((i - mean_x) ** 2 for i in range(m))
            slope = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(vals)) / den
            rise = slope * n
        else:
            rise = now_v - float(part.iloc[-1 - n])
        return ema_angle_deg(now_v, now_v - rise, n, atr)

    for end in range(n + 2, len(ema), 17):
        part = ema.iloc[: end + 1]
        for method in ("reg", "diff"):
            got = measure_slope(part, atr, lookback=n, method=method)
            assert got is not None
            assert got.angle_deg == pytest.approx(legacy(end, method), abs=1e-12)
