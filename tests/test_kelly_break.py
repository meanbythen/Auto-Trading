"""凯利突破：收盘突破、ATR 止损、固定盈亏比止盈、公共收盘清仓开关。"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.strategy.factory import create_strategy, list_engines, sanitize_strategy_config
from src.strategy.kelly_break import KellyBreakStrategy
from src.strategy.types import StrategyAction

SYMBOL = "c2701"
NOW = datetime(2024, 1, 2, 10, 0)


def _bars(
    rows: list[tuple[float, float, float, float]],
    start: datetime = datetime(2024, 1, 2),
) -> pd.DataFrame:
    """每行是开高低收。索引从 start 起每根一分钟。"""
    idx = [start + timedelta(minutes=i) for i in range(len(rows))]
    data = {
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "volume": [1000] * len(rows),
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def _flat_rows(n: int = 20) -> list[tuple[float, float, float, float]]:
    """一段窄幅横盘：前 N 根高点 101、低点 99。"""
    return [(100, 101, 99, 100)] * n


def _cfg(**box: object) -> dict:
    section = {
        "lookback": 5,
        "atr_period": 14,
        "stop_atr": 1.2,
        "take_profit": True,
        "reward_risk": 2.5,
        "min_atr_ratio": 0.0,
    }
    section.update(box)
    return {
        "strategy": {
            "engine": "kelly_break",
            "timeframe": "D",
            "symbols": [SYMBOL],
            "flatten_at_close": False,
            "kelly_break": section,
        },
        "trade": {"volume": 1, "symbol": SYMBOL},
    }


def test_close_above_prior_high_opens_long_with_atr_stop() -> None:
    bars = _bars(_flat_rows() + [(105, 112, 104, 111)])
    s = KellyBreakStrategy(_cfg())
    ind = s.prepare(bars)
    assert ind is not None
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.BUY_OPEN
    assert d.stop_price == pytest.approx(111 - 1.2 * ind["atr"])
    assert "凯利突破做多" in d.reason


def test_close_below_prior_low_opens_short_with_atr_stop() -> None:
    bars = _bars(_flat_rows() + [(95, 96, 88, 88.5)])
    s = KellyBreakStrategy(_cfg())
    ind = s.prepare(bars)
    assert ind is not None
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.SELL_OPEN
    assert d.stop_price == pytest.approx(88.5 + 1.2 * ind["atr"])
    assert "凯利突破做空" in d.reason


def test_inside_prior_range_holds() -> None:
    bars = _bars(_flat_rows() + [(100, 101, 99, 100.5)])
    s = KellyBreakStrategy(_cfg())
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.HOLD


def test_close_reaching_target_takes_profit() -> None:
    s = KellyBreakStrategy(_cfg())
    s.positions[SYMBOL].side = "long"
    s.positions[SYMBOL].volume = 1
    s.positions[SYMBOL].entry_price = 111
    stop = 100.0
    s.positions[SYMBOL].initial_stop = stop
    s.positions[SYMBOL].trail_stop = stop
    target = 111 + (111 - stop) * 2.5
    bars = _bars(_flat_rows() + [(120, target + 1, 119, target)])
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.CLOSE
    assert "止盈平多" in d.reason


def test_target_is_ignored_when_take_profit_off() -> None:
    s = KellyBreakStrategy(_cfg(take_profit=False))
    s.positions[SYMBOL].side = "long"
    s.positions[SYMBOL].volume = 1
    s.positions[SYMBOL].entry_price = 111
    s.positions[SYMBOL].initial_stop = 100.0
    s.positions[SYMBOL].trail_stop = 100.0
    bars = _bars(_flat_rows() + [(120, 160, 119, 150)])
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.HOLD


def test_volatility_floor_blocks_new_entry() -> None:
    bars = _bars(_flat_rows() + [(105, 112, 104, 111)])
    s = KellyBreakStrategy(_cfg(min_atr_ratio=0.5))
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.HOLD


def test_flatten_follows_public_switch_only() -> None:
    on = _cfg()
    on["strategy"]["flatten_at_close"] = True
    off = _cfg()
    off["strategy"]["flatten_at_close"] = False
    assert KellyBreakStrategy(on).flatten_at_close is True
    assert KellyBreakStrategy(off).flatten_at_close is False
    # 策略小节里即使写着开关，也不影响隔夜判断。
    box_on = _cfg()
    box_on["strategy"]["flatten_at_close"] = False
    box_on["strategy"]["kelly_break"]["flatten_at_close"] = True
    assert KellyBreakStrategy(box_on).flatten_at_close is False


def test_registry_and_default_section() -> None:
    assert "kelly_break" in list_engines()
    strategy = create_strategy(_cfg())
    assert isinstance(strategy, KellyBreakStrategy)
    cfg = {"strategy": {"engine": "kelly_break", "kelly_break": {}}}
    sanitize_strategy_config(cfg)
    box = cfg["strategy"]["kelly_break"]
    assert box["lookback"] == 20
    assert box["atr_period"] == 14
    assert box["stop_atr"] == 1.2
    assert box["take_profit"] is True
    assert box["reward_risk"] == 2.5
    assert box["min_atr_ratio"] == 0.0
