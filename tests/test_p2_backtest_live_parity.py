"""P2 集成：回测与实盘共用同一策略工厂和评估入口。

两件事必须锁死：
1. 回测里的策略实例与实盘 `create_strategy(cfg)` 是同一实现，不能各写一套；
2. 回测调用 `evaluate` 时只喂到当前这根 K 线，不允许提前看到未来数据。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.backtest.engine import run_backtest
from src.data.kline import KlineProvider
from src.strategy import factory as strategy_factory


def _cfg() -> dict[str, Any]:
    return {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "daily_body": {"eth_minutes": 0, "regime_filter": False},
        },
        "trade": {"symbol": "bu2612", "volume": 1, "executor": "sim"},
        "risk": {"capital": 1_000_000, "contracts": {"bu": {"multiplier": 10}}},
        "data": {"source": "demo", "cache_dir": "data/klines", "min_bars": 60},
        "schedule": {"trading_sessions": [["09:00", "23:00"]]},
    }


def _bars(cfg: dict[str, Any]) -> pd.DataFrame:
    return KlineProvider(cfg).fetch("bu2612", "15")


def test_backtest_uses_the_same_strategy_factory_as_live(monkeypatch) -> None:
    cfg = _cfg()
    live = strategy_factory.create_strategy(cfg)
    real_create = strategy_factory.create_strategy
    created: list[Any] = []

    def _spy(c: dict[str, Any]):
        instance = real_create(c)
        created.append(instance)
        return instance

    monkeypatch.setattr("src.backtest.engine.create_strategy", _spy)
    bars = _bars(cfg)
    run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)

    assert len(created) == 1
    assert type(created[0]) is type(live)
    assert type(created[0]).evaluate is type(live).evaluate


def test_backtest_never_feeds_future_bars(monkeypatch) -> None:
    cfg = _cfg()
    bars = _bars(cfg)
    strategy_cls = type(strategy_factory.create_strategy(cfg))
    original = strategy_cls.evaluate
    calls: list[tuple[pd.Timestamp, Any]] = []

    def _spy(self, symbol, frame=None, now=None, **kwargs):
        if frame is not None and len(frame):
            calls.append((pd.Timestamp(frame.index[-1]), now))
        return original(self, symbol, frame, now=now, **kwargs)

    monkeypatch.setattr(strategy_cls, "evaluate", _spy)
    run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)

    assert calls
    for last_bar, now in calls:
        if now is None:
            continue
        assert last_bar <= pd.Timestamp(now)


def test_backtest_is_reproducible_for_the_same_bars() -> None:
    cfg = _cfg()
    bars = _bars(cfg)

    first = run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)
    second = run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)

    def _key(result):
        return [
            (t.side, t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.volume)
            for t in result.trades
        ]

    assert _key(first) == _key(second)
