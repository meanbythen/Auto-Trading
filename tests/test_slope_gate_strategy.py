"""可选斜率环境门：默认关闭、方向一致才放行、接到趋势策略后不改变原行为。"""

from datetime import datetime

import pandas as pd

from src.strategy.slope import SlopeGate, SlopeGateConfig, load_slope_gate
from src.strategy.supertrend import SuperTrendStrategy
from src.strategy.trend_follow import TrendFollowStrategy
from src.strategy.types import StrategyAction


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-10-20 21:00", periods=len(closes), freq="15min")
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close}
    )


def _trend_cfg(gate: dict | None = None) -> dict:
    box = {
        "entry_bars": 5,
        "exit_bars": 3,
        "atr_period": 3,
        "atr_mult": 2.0,
    }
    if gate is not None:
        box["slope_gate"] = gate
    return {
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "trend_follow": box,
        },
        "trade": {"volume": 1, "symbol": "bu2612"},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def _st_cfg(gate: dict | None = None) -> dict:
    box: dict = {"atr_period": 3, "multiplier": 2.0}
    if gate is not None:
        box["slope_gate"] = gate
    return {
        "strategy": {
            "engine": "supertrend",
            "timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "supertrend": box,
        },
        "trade": {"volume": 1, "symbol": "bu2612"},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def test_gate_config_parsing_and_defaults() -> None:
    assert load_slope_gate({}).enabled is False
    cfg = load_slope_gate(
        {"slope_gate": {"enabled": "true", "ema_period": "60", "min_deg": "4.5", "method": "DIFF"}}
    )
    assert cfg.enabled is True
    assert cfg.ema_period == 60
    assert cfg.min_deg == 4.5
    assert cfg.method == "diff"


def test_disabled_gate_always_allows() -> None:
    gate = SlopeGate(SlopeGateConfig(enabled=False))
    allowed, _ = gate.check(_frame([100.0] * 50), "long")
    assert allowed


def test_zero_threshold_turns_gate_off() -> None:
    """min_deg 和 min_t 都是 0 时视同关闭：不拦开仓，也不要求额外 K 线。"""
    gate = SlopeGate(SlopeGateConfig(enabled=True, ema_period=10, lookback=3, min_deg=0.0, min_t=0.0))
    assert gate.enabled is False
    allowed, why = gate.check(_frame([100.0] * 60), "long")
    assert allowed
    assert why == ""


def test_zero_angle_with_t_threshold_stays_enabled() -> None:
    """只开 t 值门（min_deg=0、min_t>0）仍算启用，不能当成关闭。"""
    gate = SlopeGate(SlopeGateConfig(enabled=True, ema_period=10, lookback=3, min_deg=0.0, min_t=2.0))
    assert gate.enabled is True


def test_gate_follows_slope_direction() -> None:
    gate = SlopeGate(SlopeGateConfig(enabled=True, ema_period=10, lookback=3, min_deg=10.0))
    rising = _frame([100 + i * 0.5 for i in range(60)])
    falling = _frame([130 - i * 0.5 for i in range(60)])
    assert gate.check(rising, "long")[0]
    assert not gate.check(rising, "short")[0]
    assert gate.check(falling, "short")[0]
    assert not gate.check(falling, "long")[0]


def test_gate_blocks_when_threshold_impossible() -> None:
    gate = SlopeGate(SlopeGateConfig(enabled=True, ema_period=10, lookback=3, min_deg=89.9))
    allowed, why = gate.check(_frame([100 + i * 0.5 for i in range(60)]), "long")
    assert not allowed
    assert "斜率闸门" in why


def test_trend_follow_gate_off_matches_original() -> None:
    """关闭时不碰 K 线，突破照常开仓。"""
    closes = [100.0] * 20
    closes.append(108.0)
    strategy = TrendFollowStrategy(_trend_cfg({"enabled": False, "min_deg": 89.9}))
    decision = strategy.evaluate("bu2612", _frame(closes), now=datetime(2026, 10, 21, 10, 0))
    assert decision.action == StrategyAction.BUY_OPEN


def test_trend_follow_gate_blocks_breakout() -> None:
    closes = [100.0] * 20
    closes.append(108.0)
    strategy = TrendFollowStrategy(
        _trend_cfg({"enabled": True, "ema_period": 10, "lookback": 3, "min_deg": 89.9})
    )
    decision = strategy.evaluate("bu2612", _frame(closes), now=datetime(2026, 10, 21, 10, 0))
    assert decision.action == StrategyAction.HOLD
    assert "斜率闸门" in decision.reason


def test_supertrend_gate_blocks_flip() -> None:
    """先跌后涨制造翻转；关闭时能开，开启不可能达到的角度则被拦。"""
    closes = [100 - i * 0.5 for i in range(40)] + [80 + i * 1.0 for i in range(40)]
    bars = _frame(closes)
    off = SuperTrendStrategy(_st_cfg({"enabled": False}))
    opened = False
    for end in range(10, len(closes) + 1):
        decision = off.evaluate("bu2612", bars.iloc[:end], now=datetime(2026, 10, 21, 10, 0))
        if decision.action == StrategyAction.BUY_OPEN:
            opened = True
            break
    assert opened, "构造的行情没有触发 SuperTrend 做多翻转"
    on = SuperTrendStrategy(
        _st_cfg({"enabled": True, "ema_period": 5, "lookback": 3, "min_deg": 89.9})
    )
    blocked = on.evaluate("bu2612", bars.iloc[:end], now=datetime(2026, 10, 21, 10, 0))
    assert blocked.action == StrategyAction.HOLD
    assert "斜率闸门" in blocked.reason
