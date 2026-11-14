"""
市场状态过滤（ADX + ATR 分位）：行情不匹配就停手。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.market_state import (
    MarketFilter,
    MarketFilterParams,
    evaluate_market_state,
    range_params_for_daily_body,
    trend_params_for_section,
)


def _bars(closes: list[float], spread: float = 2.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="5min")
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


def test_flat_market_is_blocked_by_adx() -> None:
    """横盘没有方向，ADX 低，应该拦住开仓。"""
    rng = np.random.default_rng(11)
    closes = (100 + rng.normal(0, 0.3, 120)).tolist()
    state = evaluate_market_state(_bars(closes, 0.5), MarketFilterParams())
    assert state.ok is False
    assert "ADX" in state.reason or "ATR" in state.reason


def test_evaluate_with_duplicate_ohlc_columns_does_not_crash() -> None:
    """收盘价列重复时，区间过滤仍要给出能不能开仓，不能抛 ambiguous。"""
    rng = np.random.default_rng(11)
    closes = (100 + rng.normal(0, 0.3, 120)).tolist()
    bars = _bars(closes, 0.5)
    bars = pd.concat([bars, bars[["high", "low", "close"]]], axis=1)
    state = evaluate_market_state(bars, MarketFilterParams(enabled=True, mode="range"))
    assert state.reason


def test_strong_trend_is_allowed() -> None:
    """单边趋势 + 正常波动，ADX 高、ATR 不低，应该放行。"""
    rng = np.random.default_rng(12)
    closes = (100 + np.arange(160) * 0.8 + rng.normal(0, 1.0, 160)).tolist()
    state = evaluate_market_state(_bars(closes, 3.0), MarketFilterParams())
    assert state.ok is True
    assert state.adx >= 20


def test_low_atr_percentile_blocks_even_in_a_trend() -> None:
    """趋势还在，但波动压缩到近 60 根最低 20% 分位，同样不开新仓。"""
    closes = [100 + i * 0.5 for i in range(120)]
    bars = _bars(closes, 0.0)
    # 波动从大到小：前面 60 根很宽，最近 30 根收窄，最后一根最窄。
    bars["high"] = bars["close"] + np.linspace(6.0, 0.2, len(bars))
    bars["low"] = bars["close"] - np.linspace(6.0, 0.2, len(bars))
    state = evaluate_market_state(
        bars, MarketFilterParams(atr_low_pct=0.2, lookback=60)
    )
    assert state.atr_rank <= 0.2
    assert state.ok is False
    assert "波动压缩" in state.reason


def test_range_filter_recalculates_each_check_not_latched() -> None:
    """同一份过滤器跟时间走：刚才拧来拧去还放行，换成强单边后要拦住，不能开盘锁死。"""
    filt = MarketFilter(
        MarketFilterParams(enabled=True, mode="range", adx_upper=30, atr_high_pct=1.0)
    )
    flat_rng = np.random.default_rng(11)
    allowed = filt.check(_bars((100 + flat_rng.normal(0, 0.3, 120)).tolist(), 0.5))
    assert allowed.ok is True
    rng = np.random.default_rng(12)
    trend = (100 + np.arange(160) * 0.8 + rng.normal(0, 1.0, 160)).tolist()
    later = filt.check(_bars(trend, 3.0))
    assert later.ok is False
    assert later.adx > allowed.adx


def test_disabled_filter_always_allows() -> None:
    rng = np.random.default_rng(13)
    bars = _bars((100 + rng.normal(0, 0.2, 80)).tolist(), 0.3)
    state = MarketFilter.from_cfg({"strategy": {"market_filter": {"enabled": False}}}).check(bars)
    assert state.ok is True
    assert "关闭" in state.reason


def test_trend_params_default_off_without_section_keys() -> None:
    """趋势策略没写 filter_enabled、也没有旧公共表时，默认不拦。"""
    p = trend_params_for_section({"strategy": {"trend_follow": {"entry_bars": 20}}}, "trend_follow")
    assert p.enabled is False
    assert p.mode == "trend"


def test_trend_params_inherit_legacy_market_filter() -> None:
    """旧配置只开了公共表时，趋势策略先沿用那个开关和数字。"""
    cfg = {
        "strategy": {
            "market_filter": {"enabled": True, "adx_flat": 18, "atr_low_pct": 0.15},
            "dual_ma": {"short_period": 20},
        }
    }
    p = trend_params_for_section(cfg, "dual_ma")
    assert p.enabled is True
    assert p.adx_flat == 18
    assert p.atr_low_pct == 0.15


def test_trend_params_section_overrides_legacy() -> None:
    """策略自己写了 filter_*，就不再听公共表。"""
    cfg = {
        "strategy": {
            "market_filter": {"enabled": True, "adx_flat": 18},
            "supertrend": {"filter_enabled": False, "filter_adx_flat": 22},
        }
    }
    p = trend_params_for_section(cfg, "supertrend")
    assert p.enabled is False


def test_defaults_are_read_from_config() -> None:
    params = MarketFilterParams.from_cfg(
        {"strategy": {"market_filter": {"adx_flat": 30, "lookback": 40, "atr_low_pct": 0.3}}}
    )
    assert params.adx_flat == 30
    assert params.lookback == 40
    assert params.atr_low_pct == 0.3


# —— range 模式（日内实体用）——


def test_strong_trend_is_blocked_in_range_mode() -> None:
    """主推日内实体：已经走出强单边（ADX 过高）不开。"""
    rng = np.random.default_rng(21)
    closes = (100 + np.arange(160) * 0.8 + rng.normal(0, 1.0, 160)).tolist()
    params = MarketFilterParams(mode="range", adx_upper=30.0, atr_high_pct=1.0)
    state = evaluate_market_state(_bars(closes, 3.0), params)
    assert state.adx > 30
    assert state.ok is False
    assert "强单边" in state.reason or "ADX" in state.reason


def test_compressed_atr_is_allowed_in_range_mode() -> None:
    """区间模式不拦波动压缩：开盘箱子最需要相对平静。"""
    rng = np.random.default_rng(7)
    closes = (100 + np.cumsum(rng.normal(0, 0.15, 120))).tolist()
    bars = _bars(closes, 0.0)
    bars["high"] = bars["close"] + np.linspace(6.0, 0.2, len(bars))
    bars["low"] = bars["close"] - np.linspace(6.0, 0.2, len(bars))
    state = evaluate_market_state(
        bars,
        MarketFilterParams(mode="range", atr_low_pct=0.2, lookback=60, adx_upper=0.0),
    )
    assert state.atr_rank <= 0.2
    assert state.ok is True
    assert "波动压缩" not in state.reason


def test_flat_market_is_allowed_in_range_mode() -> None:
    """主推日内实体：盘面拧来拧去（ADX 低）放行。"""
    rng = np.random.default_rng(22)
    closes = (100 + rng.normal(0, 0.5, 200)).tolist()
    bars = _bars(closes, 2.0)
    atr_series = 1.5 + rng.uniform(0.3, 3.0, 200)
    atr_series[-1] = 1.5
    half = len(atr_series) // 2
    atr_series[:half] += 1.5
    atr_series[half:-1] += 0.5
    bars["high"] = bars["close"] + atr_series
    bars["low"] = bars["close"] - atr_series
    params = MarketFilterParams(mode="range", adx_upper=30.0)
    state = evaluate_market_state(bars, params)
    assert state.adx < 30
    assert state.ok is True, f"reason={state.reason}, rank={state.atr_rank:.0%}, adx={state.adx:.1f}"
    assert state.mode == "range"


def test_extreme_atr_is_blocked_in_range_mode() -> None:
    """区间模式：ATR 处于 90% 高分位（波动太极端）拦掉，避免跳空止损滑价。"""
    closes = [100 + i * 0.2 for i in range(120)]
    bars = _bars(closes, 1.0)
    # 让最后几根波动突然放大，ATR 分位拉上去
    bars["high"] = bars["close"] + np.concatenate([np.ones(90) * 1.0, np.ones(30) * 8.0])
    bars["low"] = bars["close"] - np.concatenate([np.ones(90) * 1.0, np.ones(30) * 8.0])
    params = MarketFilterParams(mode="range", atr_high_pct=0.8)
    state = evaluate_market_state(bars, params)
    assert state.ok is False
    assert "极端波动" in state.reason or state.atr_rank > 0.8


def test_with_mode_derives_range_from_trend_base() -> None:
    """策略代码里用的派生方法：全局 trend 配置，单独给日内实体派生 range。"""
    base = MarketFilterParams(mode="trend", adx_flat=20.0, adx_upper=30.0, atr_low_pct=0.2)
    range_p = MarketFilterParams.with_mode(base, "range")
    assert range_p.mode == "range"
    assert range_p.adx_flat == base.adx_flat  # 其他参数不变


def test_daily_body_range_params_stay_on_when_public_filter_is_off() -> None:
    """公共行情过滤关掉时，日内实体仍用区间过滤，而且不拦波动压缩。"""
    params = range_params_for_daily_body(
        {
            "strategy": {
                "market_filter": {"enabled": False},
                "daily_body": {"filter_adx_upper": 0, "filter_atr_high_pct": 0.8},
            }
        }
    )
    assert params.enabled is True
    assert params.mode == "range"
    assert params.adx_upper == 0
    rng = np.random.default_rng(7)
    closes = (100 + np.cumsum(rng.normal(0, 0.15, 120))).tolist()
    bars = _bars(closes, 0.0)
    bars["high"] = bars["close"] + np.linspace(6.0, 0.2, len(bars))
    bars["low"] = bars["close"] - np.linspace(6.0, 0.2, len(bars))
    state = evaluate_market_state(bars, params)
    assert state.atr_rank <= 0.2
    assert state.ok is True
    assert "波动压缩" not in state.reason


def test_daily_body_can_turn_off_own_regime_filter() -> None:
    params = range_params_for_daily_body(
        {"strategy": {"daily_body": {"regime_filter": False}}}
    )
    assert params.enabled is False


def test_daily_body_filter_enabled_switch() -> None:
    """界面开关 filter_enabled 优先于旧键 regime_filter。"""
    off = range_params_for_daily_body(
        {"strategy": {"daily_body": {"filter_enabled": False, "regime_filter": True}}}
    )
    assert off.enabled is False
    on = range_params_for_daily_body(
        {"strategy": {"daily_body": {"filter_enabled": True}}}
    )
    assert on.enabled is True
    assert on.mode == "range"
    assert on.adx_upper == 30


def test_adx_upper_zero_turns_off_strong_trend_block() -> None:
    """ADX 上限填 0 表示关掉这一条。"""
    params = range_params_for_daily_body(
        {"strategy": {"daily_body": {"filter_adx_upper": 0}}}
    )
    assert params.adx_upper == 0


def test_adx_upper_keeps_30() -> None:
    """主推仍把 30 当强单边上限。"""
    params = range_params_for_daily_body(
        {"strategy": {"daily_body": {"filter_adx_upper": 30}}}
    )
    assert params.adx_upper == 30


def test_incremental_check_matches_full_recalculation() -> None:
    """回测逐根往前推时走增量缓存，每一步都要和整段重算给出一样的结果。"""
    rng = np.random.default_rng(31)
    bars = _bars((3000 + np.cumsum(rng.normal(0, 2.0, 400))).tolist(), 4.0)
    filt = MarketFilter(
        MarketFilterParams(enabled=True, mode="range", adx_upper=30, atr_high_pct=0.8)
    )
    need = filt.params.adx_period + 5
    for end in range(need, len(bars) + 1):
        window = bars.iloc[:end]
        got = filt.check(window)
        ref = evaluate_market_state(window, filt.params)
        assert got.ok == ref.ok
        assert got.reason == ref.reason
        assert got.adx == pytest.approx(ref.adx, abs=1e-9)
        assert got.atr == pytest.approx(ref.atr, abs=1e-9)
        assert got.atr_rank == pytest.approx(ref.atr_rank, abs=1e-12)


def test_incremental_check_rebuilds_when_bars_switch() -> None:
    """换一段 K 线（不同品种、窗口滑过）时缓存要失效，不能拿上一段接着算。"""
    rng = np.random.default_rng(32)
    first = _bars((3000 + np.cumsum(rng.normal(0, 2.0, 200))).tolist(), 4.0)
    second = _bars((4000 + np.cumsum(rng.normal(0, 2.0, 200))).tolist(), 4.0)
    filt = MarketFilter(MarketFilterParams(enabled=True, mode="trend"))
    filt.check(first)
    got = filt.check(second)
    ref = evaluate_market_state(second, filt.params)
    assert got.ok == ref.ok
    assert got.adx == pytest.approx(ref.adx, abs=1e-9)
    assert got.atr_rank == pytest.approx(ref.atr_rank, abs=1e-12)


def test_atr_high_zero_closes_upper_in_range_mode() -> None:
    """atr_high_pct 设成 0 = 关闭 ATR 上限，只拦 ADX。"""
    params = MarketFilterParams.from_cfg(
        {"strategy": {"market_filter": {"mode": "range", "atr_high_pct": 0}}}
    )
    assert params.atr_high_pct == 1.0  # from_cfg 把 0 转成 1.0 表示关闭
    assert params.mode == "range"
