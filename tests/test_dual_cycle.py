"""双周期通道突破、回调分型：大周期定方向，小周期才开仓。"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from src.strategy.dual_cycle import (
    donchian_prior,
    htf_bias,
    last_ema,
    strong_bottom_fractal,
    strong_top_fractal,
)
from src.strategy.dual_donchian import DualDonchianStrategy
from src.strategy.dual_fractal import DualFractalStrategy
from src.strategy.factory import create_strategy
from src.strategy.types import StrategyAction


def _bars(n: int, *, start: float = 100.0, drift: float = 0.4, freq: str = "1D") -> pd.DataFrame:
    """造一段单向走的 K 线，方便测通道和均线方向。"""
    idx = pd.date_range("2025-01-06", periods=n, freq=freq)
    rows = []
    price = start
    for _ in range(n):
        o = price
        c = price + drift
        rows.append(
            {
                "open": o,
                "high": max(o, c) + 0.2,
                "low": min(o, c) - 0.2,
                "close": c,
                "volume": 1000,
            }
        )
        price = c
    return pd.DataFrame(rows, index=idx)


def _cfg(engine: str, **box: object) -> dict:
    section = {
        "trend_timeframe": "D",
        "adx_min": 0,
        "adx_period": 5,
        "flatten_at_close": False,
    }
    section.update(box)
    return {
        "strategy": {
            "engine": engine,
            "timeframe": "D",
            "trail_timeframe": "D",
            "symbols": ["bu2612"],
            engine: section,
        },
        "trade": {"volume": 1, "symbol": "bu2612"},
        "risk": {},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
    }


def test_donchian_prior_excludes_current_bar() -> None:
    bars = _bars(12, start=10.0, drift=1.0)
    band = donchian_prior(bars, 5)
    assert band is not None
    up, down = band
    # 当前这根的最高不能算进通道，否则永远破不了自己。
    assert up == float(bars["high"].iloc[-6:-1].max())
    assert down == float(bars["low"].iloc[-6:-1].min())


def test_sma_replaced_by_ema() -> None:
    """资料里的简单平均必须走指数平均。"""
    bars = _bars(20, start=100.0, drift=1.0)
    ema = last_ema(bars, 5)
    sma = float(bars["close"].iloc[-5:].mean())
    assert ema is not None
    assert abs(ema - sma) > 1e-6


def test_strong_fractals() -> None:
    idx = pd.date_range("2025-01-06", periods=3, freq="1D")
    bottom = pd.DataFrame(
        {
            "open": [10, 9, 11],
            "high": [11, 10, 13],
            "low": [9, 7, 10],
            "close": [9.5, 8, 12],
            "volume": [1, 1, 1],
        },
        index=idx,
    )
    assert strong_bottom_fractal(bottom)
    top = pd.DataFrame(
        {
            "open": [12, 13, 10],
            "high": [13, 15, 12],
            "low": [11, 12, 8],
            "close": [12.5, 14, 9],
            "volume": [1, 1, 1],
        },
        index=idx,
    )
    assert strong_top_fractal(top)


def test_factory_creates_dual_engines() -> None:
    d = create_strategy(_cfg("dual_donchian", entry_bars=10, exit_bars=4))
    f = create_strategy(_cfg("dual_fractal", trend_mode="ema", trend_ema=5))
    assert isinstance(d, DualDonchianStrategy)
    assert isinstance(f, DualFractalStrategy)
    assert d.uses_trend_bars is True
    assert f.uses_trend_bars is True
    assert d.trend_timeframe == "D"
    assert f.trend_mode == "ema"


def test_dual_donchian_follows_htf_direction() -> None:
    s = create_strategy(
        _cfg("dual_donchian", entry_bars=8, exit_bars=3, htf_donchian=8, atr_period=5)
    )
    bars = _bars(40, start=100.0, drift=0.8)
    now = bars.index[-1].to_pydatetime()
    decision = s.evaluate("bu2612", bars, now=now, trend_bars=bars)
    assert decision.action == StrategyAction.BUY_OPEN
    assert "做多" in decision.reason


def test_dual_donchian_blocks_when_htf_inside() -> None:
    s = create_strategy(
        _cfg("dual_donchian", entry_bars=8, exit_bars=3, htf_donchian=8, atr_period=5)
    )
    signal = _bars(40, start=100.0, drift=0.8)
    # 大周期横盘：收盘夹在通道里，即使小周期已经突破也不能开。
    htf = _bars(40, start=100.0, drift=0.0)
    now = signal.index[-1].to_pydatetime()
    decision = s.evaluate("bu2612", signal, now=now, trend_bars=htf)
    assert decision.action == StrategyAction.HOLD
    assert "通道" in decision.reason or "趋势" in decision.reason or "方向" in decision.reason


def test_dual_fractal_needs_divergence_and_fractal() -> None:
    s = create_strategy(
        _cfg("dual_fractal", trend_mode="ema", trend_ema=5, atr_period=5, adx_min=0)
    )
    down = _bars(50, start=140.0, drift=-0.6)
    # 最后三根做成强底分型，并且最后一根低点再创新低。
    last_close = float(down["close"].iloc[-1])
    extra = pd.DataFrame(
        [
            {
                "open": last_close,
                "high": last_close + 1,
                "low": last_close - 1,
                "close": last_close - 0.5,
                "volume": 1000,
            },
            {
                "open": last_close - 0.5,
                "high": last_close,
                "low": last_close - 3,
                "close": last_close - 2,
                "volume": 1000,
            },
            {
                "open": last_close - 2,
                "high": last_close + 4,
                "low": last_close - 2,
                "close": last_close + 3,
                "volume": 1000,
            },
        ],
        index=pd.date_range(down.index[-1] + timedelta(days=1), periods=3, freq="1D"),
    )
    bars = pd.concat([down, extra])
    now = bars.index[-1].to_pydatetime()
    # 先记下前低，再让最后一根价格更低、DIF 更高。
    s._swing["bu2612"] = {"last_low_price": last_close + 5.0, "last_low_dif": -10.0}
    # 大周期用一段上涨，EMA 在下方，ADX 门槛已关掉。
    htf = _bars(40, start=80.0, drift=1.0)
    htf.index = bars.index[-len(htf) :]
    decision = s.evaluate("bu2612", bars, now=now, trend_bars=htf)
    assert strong_bottom_fractal(bars)
    assert decision.action == StrategyAction.BUY_OPEN


def test_htf_ema_bias() -> None:
    up = _bars(30, start=50.0, drift=1.0)
    bias, _why = htf_bias(up, mode="ema", donchian=10, ema_len=5, adx_period=5, adx_min=0)
    assert bias == "long"
    down = _bars(30, start=150.0, drift=-1.0)
    bias, _why = htf_bias(down, mode="ema", donchian=10, ema_len=5, adx_period=5, adx_min=0)
    assert bias == "short"
