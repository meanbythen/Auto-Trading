"""回测里的涨跌停/流动性近似。"""

from __future__ import annotations

import pandas as pd

from src.backtest.engine import run_backtest
from src.backtest.liquidity import LiquidityModel


def _bar(high: float, low: float, volume: float = 100.0) -> pd.Series:
    return pd.Series({"open": high, "high": high, "low": low, "close": low, "volume": volume})


def test_defaults_are_off() -> None:
    """没配这两项时，既不拦一字板也不按成交量限手数。"""
    model = LiquidityModel.from_cfg({})
    assert model.block_limit_locked is False
    assert model.max_volume_pct == 0.0
    assert model.locked(_bar(100.0, 100.0)) is False
    assert model.cap_open_volume(5, _bar(100.0, 100.0, volume=1)) == 5


def test_limit_locked_detects_one_price_bar() -> None:
    model = LiquidityModel.from_cfg(
        {"backtest": {"liquidity": {"block_limit_locked": True}}}
    )
    assert model.locked(_bar(100.0, 100.0)) is True
    assert model.locked(_bar(101.0, 99.0)) is False
    # 没有K线时不能拦，否则整段回测都开不了仓。
    assert model.locked(None) is False


def test_volume_cap_uses_bar_volume() -> None:
    model = LiquidityModel.from_cfg(
        {"backtest": {"liquidity": {"max_volume_pct": 0.1}}}
    )
    # 这根成交 40 手，10% 就是 4 手。
    assert model.cap_open_volume(10, _bar(101.0, 99.0, volume=40)) == 4
    # 想开的比上限少，就按想开的来。
    assert model.cap_open_volume(2, _bar(101.0, 99.0, volume=40)) == 2
    # 一根没成交，一手也开不了。
    assert model.cap_open_volume(3, _bar(101.0, 99.0, volume=0)) == 0


def test_backtest_snapshot_records_liquidity_settings() -> None:
    """口径要写进结果快照，历史结果和新规则才能对上。"""
    idx = pd.date_range("2026-10-23 09:00", periods=90, freq="5min")
    closes = [100.0 + 0.1 * i for i in range(len(idx))]
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=idx,
    )
    cfg = {
        "dry_run": True,
        "strategy": {
            "engine": "dual_ma",
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_at_close": False,
            "market_filter": {"enabled": False},
        },
        "trade": {"volume": 1, "symbol": "bu", "ctp": {"price_tick": 1}},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "risk": {"capital": 14000, "contracts": {"bu": {"multiplier": 10}}},
        "backtest": {
            "slippage_ticks": 0,
            "liquidity": {"block_limit_locked": True, "max_volume_pct": 0.2},
        },
    }
    result = run_backtest(cfg, symbol="bu", bars_signal=bars, warmup=20, volume=1)
    assert result.params_snapshot["liquidity"] == {
        "block_limit_locked": True,
        "max_volume_pct": 0.2,
    }
