"""tick 合成 K 线的只读来源：只读本地库，不联网。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.kline import KlineProvider


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [price for _, price in rows],
            "high": [price + 1 for _, price in rows],
            "low": [price - 1 for _, price in rows],
            "close": [price for _, price in rows],
            "volume": [10.0 for _ in rows],
        },
        index=pd.to_datetime([stamp for stamp, _ in rows]),
    )


def _provider(tmp_path: Path) -> KlineProvider:
    return KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {
                "source": "tick",
                "cache_dir": "data/klines",
                "min_bars": 1,
                "tick": {"bar_store": "data/klines/tick_bars.sqlite"},
            },
            "strategy": {"timeframe": "5"},
        }
    )


def test_tick_source_reads_local_store(tmp_path) -> None:
    store = BarStore(tmp_path / "data/klines/tick_bars.sqlite")
    store.upsert(
        "fu2701@tick",
        "5",
        _frame([("2020-01-02 09:05:00", 100.0), ("2020-01-02 09:10:00", 101.0)]),
    )
    store.upsert(
        "fu2701@tick",
        "1",
        _frame([("2020-01-02 09:01:00", 100.0), ("2020-01-02 09:02:00", 102.0)]),
    )

    provider = _provider(tmp_path)
    bars = provider.fetch("fu2701", "5", max_bars=10)
    assert bars["close"].tolist() == [100.0, 101.0]
    assert bars.attrs["source"] == "tick"
    assert bars.attrs["from_cache"] is True
    assert provider.latest_price("fu2701") == 102.0


def test_tick_source_never_downloads_when_empty(tmp_path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(RuntimeError, match="tick合成K线"):
        provider.fetch("fu2701", "5", max_bars=10)
