"""以损定开：高低点聚类止损区、击穿回流开仓、ATR 止损、ADX/量过滤、固定盈亏比止盈。"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.strategy.factory import create_strategy, list_engines, sanitize_strategy_config
from src.strategy.liq_stop import LiquidityStopStrategy
from src.strategy.types import StrategyAction

SYMBOL = "SA2701"
NOW = datetime(2024, 1, 2, 10, 0)
# 止损区窗口里有两处明显的低点拐点（96）或高点拐点（104）。
VALLEY_AT = (8, 11, 14)
PEAK_AT = (8, 11, 14)


def _bars(
    rows: list[tuple[float, float, float, float]],
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    idx = [datetime(2024, 1, 2) + timedelta(minutes=i) for i in range(len(rows))]
    data = {
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "volume": volumes or [1000.0] * len(rows),
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def _flat_long_prefix(n: int = 20) -> list[tuple[float, float, float, float]]:
    """缓慢上行的震荡：低点 96 在 3 处形成拐点，高点持续抬高。"""
    rows = []
    for i in range(n):
        close = 98.0 + i * 0.3
        low = 96.0 if i in VALLEY_AT else 96.1
        rows.append((close - 0.2, close + 1.0, low, close))
    return rows


def _flat_short_prefix(n: int = 20) -> list[tuple[float, float, float, float]]:
    """缓慢下行的震荡：高点 104 在 3 处形成拐点，低点持续走低。"""
    rows = []
    for i in range(n):
        close = 99.0 - i * 0.25
        high = 104.0 if i in PEAK_AT else 103.9
        rows.append((close + 0.2, high, close - 1.0, close))
    return rows


def _cfg(**box: object) -> dict:
    section = {
        "zone_lookback": 12,
        "zone_tol": 0.005,
        "pivot_span": 2,
        "atr_period": 5,
        "adx_period": 5,
        "adx_min": 0.0,
        "break_window": 2,
        "entry_offset": 0.5,
        "stop_atr": 1.0,
        "take_profit": True,
        "reward_risk": 2.0,
        "vol_mult": 0.0,
    }
    section.update(box)
    return {
        "strategy": {
            "engine": "liq_stop",
            "timeframe": "D",
            "symbols": [SYMBOL],
            "flatten_at_close": False,
            "liq_stop": section,
        },
        "trade": {"volume": 1, "symbol": SYMBOL},
    }


def test_break_below_zone_then_recover_opens_long() -> None:
    bars = _bars(_flat_long_prefix() + [(103.5, 105, 95, 104)])
    s = LiquidityStopStrategy(_cfg())
    ind = s.prepare(bars)
    assert ind is not None
    assert ind["zone_l"] < 96 < ind["zone_h"]
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.BUY_OPEN
    assert d.stop_price == pytest.approx(ind["zone_l"] - 1.0 * ind["atr"])
    assert "以损定开做多" in d.reason


def test_break_above_zone_then_fall_back_opens_short() -> None:
    bars = _bars(_flat_short_prefix() + [(94.5, 104.6, 92.5, 94.0)])
    s = LiquidityStopStrategy(_cfg())
    ind = s.prepare(bars)
    assert ind is not None
    assert ind["short_l"] < 104 < ind["short_h"]
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.SELL_OPEN
    assert d.stop_price == pytest.approx(ind["short_h"] + 1.0 * ind["atr"])
    assert "以损定开做空" in d.reason


def test_no_sweep_of_zone_holds() -> None:
    bars = _bars(_flat_long_prefix() + [(103.5, 105, 97, 104)])
    s = LiquidityStopStrategy(_cfg())
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.HOLD


def test_adx_floor_blocks_entry() -> None:
    bars = _bars(_flat_long_prefix() + [(103.5, 105, 95, 104)])
    s = LiquidityStopStrategy(_cfg(adx_min=100.0))
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.HOLD


def test_volume_filter_blocks_entry() -> None:
    bars = _bars(_flat_long_prefix() + [(103.5, 105, 95, 104)])
    s = LiquidityStopStrategy(_cfg(vol_mult=2.0))
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.HOLD


def test_close_reaching_target_takes_profit() -> None:
    s = LiquidityStopStrategy(_cfg())
    s.positions[SYMBOL].side = "long"
    s.positions[SYMBOL].volume = 1
    s.positions[SYMBOL].entry_price = 104.0
    s.positions[SYMBOL].initial_stop = 100.0
    s.positions[SYMBOL].trail_stop = 100.0
    bars = _bars(_flat_long_prefix() + [(110, 113, 103, 112.5)])
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.CLOSE
    assert "止盈平多" in d.reason


def test_target_ignored_when_take_profit_off() -> None:
    s = LiquidityStopStrategy(_cfg(take_profit=False))
    s.positions[SYMBOL].side = "long"
    s.positions[SYMBOL].volume = 1
    s.positions[SYMBOL].entry_price = 104.0
    s.positions[SYMBOL].initial_stop = 100.0
    s.positions[SYMBOL].trail_stop = 100.0
    bars = _bars(_flat_long_prefix() + [(110, 113, 103, 112.5)])
    d = s.evaluate(SYMBOL, bars, now=NOW)
    assert d.action == StrategyAction.HOLD


def test_flatten_follows_public_switch_only() -> None:
    on = _cfg()
    on["strategy"]["flatten_at_close"] = True
    off = _cfg()
    off["strategy"]["flatten_at_close"] = False
    assert LiquidityStopStrategy(on).flatten_at_close is True
    assert LiquidityStopStrategy(off).flatten_at_close is False
    box_on = _cfg()
    box_on["strategy"]["flatten_at_close"] = False
    box_on["strategy"]["liq_stop"]["flatten_at_close"] = True
    assert LiquidityStopStrategy(box_on).flatten_at_close is False


def test_registry_and_default_section() -> None:
    assert "liq_stop" in list_engines()
    strategy = create_strategy(_cfg())
    assert isinstance(strategy, LiquidityStopStrategy)
    cfg = {"strategy": {"engine": "liq_stop", "liq_stop": {}}}
    sanitize_strategy_config(cfg)
    box = cfg["strategy"]["liq_stop"]
    assert box["zone_lookback"] == 20
    assert box["atr_period"] == 20
    assert box["adx_min"] == 20.0
    assert box["entry_offset"] == 0.5
    assert box["stop_atr"] == 1.0
    assert box["take_profit"] is True
    assert box["reward_risk"] == 2.0
    assert box["vol_mult"] == 0.0
