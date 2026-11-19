"""
新增的三套趋势策略：布林突破 / SAR 波段 / 多因子 CTA。
这里只验「该开的时候开、该走的时候走」，市场过滤统一关掉，过滤本身另有测试。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from src.strategy.bollinger_break import BollingerBreakStrategy
from src.strategy.multi_factor import MultiFactorStrategy
from src.strategy.sar_trend import SarTrendStrategy
from src.strategy.types import PositionState, StrategyAction


def _cfg(engine: str, **box) -> dict:
    return {
        "strategy": {
            "engine": engine,
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "market_filter": {"enabled": False},
            engine: box,
        },
        "trade": {"symbol": "bu", "volume": 1},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
    }


def _frame(closes: list[float], spread: float = 1.0) -> pd.DataFrame:
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


def test_bollinger_breakout_opens_long() -> None:
    closes = [100.0] * 30 + [112.0]
    bars = _frame(closes, 0.5)
    decision = BollingerBreakStrategy(_cfg("bollinger")).evaluate(
        "bu", bars, now=datetime(2026, 1, 1, 10, 0)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert "上轨" in decision.reason
    assert decision.stop_price is not None
    assert decision.stop_price < decision.entry_price


def test_bollinger_fall_back_to_mid_closes_long() -> None:
    closes = [100.0] * 29 + [112.0, 95.0]
    bars = _frame(closes, 0.5)
    strategy = BollingerBreakStrategy(_cfg("bollinger"))
    strategy.positions["bu"] = PositionState(
        side="long", volume=1, entry_price=112.0, initial_stop=100.0, trail_stop=100.0
    )
    decision = strategy.evaluate("bu", bars, now=datetime(2026, 1, 1, 10, 0))
    assert decision.action == StrategyAction.CLOSE
    assert "中轨" in decision.reason


def _daily_bias(direction: str) -> pd.DataFrame:
    """测试用日线：完整版多因子要用日线定方向，这里造一段明确的多/空。"""
    if direction == "up":
        closes = [90 + i * 0.5 for i in range(80)]
    else:
        closes = [130 - i * 0.5 for i in range(80)]
    index = pd.date_range("2025-08-01", periods=len(closes), freq="B")
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [1000.0] * len(closes),
        },
        index=index,
    )


def test_multi_factor_opens_long_when_all_factors_agree() -> None:
    # 缓慢上行 + 最后一根加速；日线也偏多，四个因子同向。
    closes = [100 + i * 0.12 for i in range(80)] + [118.0]
    bars = _frame(closes, 0.4)
    decision = MultiFactorStrategy(_cfg("multi_factor")).evaluate(
        "bu", bars, now=datetime(2026, 1, 1, 10, 0), trend_bars=_daily_bias("up")
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert "多因子做多" in decision.reason
    assert decision.meta.get("score", 0) >= 0.59


def test_multi_factor_exits_on_reverse_score() -> None:
    closes = [118 - i * 0.12 for i in range(80)] + [90.0]
    bars = _frame(closes, 0.4)
    strategy = MultiFactorStrategy(_cfg("multi_factor"))
    strategy.positions["bu"] = PositionState(
        side="long", volume=1, entry_price=105.0, initial_stop=100.0, trail_stop=100.0
    )
    decision = strategy.evaluate(
        "bu", bars, now=datetime(2026, 1, 1, 10, 0), trend_bars=_daily_bias("down")
    )
    assert decision.action == StrategyAction.CLOSE
    assert "反向" in decision.reason or "止损" in decision.reason


def test_sar_trend_builds_and_ratchets_the_stop() -> None:
    closes = [100 + i * 0.8 for i in range(60)]
    bars = _frame(closes, 2.0)
    strategy = SarTrendStrategy(_cfg("sar_trend"))
    strategy.positions["bu"] = PositionState(
        side="long", volume=1, entry_price=120.0, initial_stop=110.0, trail_stop=110.0
    )
    decision = strategy.evaluate("bu", bars, now=datetime(2026, 1, 1, 10, 0))
    assert decision.action == StrategyAction.HOLD
    assert decision.stop_price is not None
    # 上行趋势里 SAR 在价格下方，止损应该被抬高到初始止损之上。
    assert decision.stop_price > 110.0


def test_bollinger_and_sar_follow_public_flatten_switch() -> None:
    """公共开关关掉时，布林/SAR 自己那一节即使写着每天平仓，也不能到点强平。"""
    for engine, cls in (("bollinger", BollingerBreakStrategy), ("sar_trend", SarTrendStrategy)):
        cfg = _cfg(engine, flatten_at_close=True)
        cfg["strategy"]["flatten_at_close"] = False
        assert cls(cfg).flatten_at_close is False
        cfg["strategy"]["flatten_at_close"] = True
        assert cls(cfg).flatten_at_close is True


def test_sar_trend_short_position_exits_when_sar_flips_up() -> None:
    closes = [100 - i * 0.8 for i in range(55)] + [101.0, 108.0, 116.0]
    bars = _frame(closes, 2.0)
    strategy = SarTrendStrategy(_cfg("sar_trend"))
    strategy.positions["bu"] = PositionState(
        side="short", volume=1, entry_price=130.0, initial_stop=140.0, trail_stop=140.0
    )
    decision = strategy.evaluate("bu", bars, now=datetime(2026, 1, 1, 10, 0))
    # 反转后 SAR 翻多，空单应被平掉（要么主动平，要么被 SAR 止损打掉）。
    assert decision.action == StrategyAction.CLOSE
