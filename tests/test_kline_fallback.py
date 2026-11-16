"""天勤失败回退新浪：标记、库存隔离、开仓开关。全程不联网（mock 两家行情）。"""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.kline import KlineProvider


def _bars() -> pd.DataFrame:
    index = pd.to_datetime(["2026-11-13 14:55:00", "2026-11-13 15:00:00"])
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 20.0],
        },
        index=index,
    )


def _provider(tmp_path, *, allow_entry: bool = True) -> KlineProvider:
    return KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {
                "source": "tq",
                "fallback_source": "akshare",
                "fallback_allow_entry": allow_entry,
                "cache_dir": "data/klines",
                "min_bars": 1,
                "max_stale_minutes": 100000,
                "tq": {"user": "u", "password": "p"},
            },
            "strategy": {"timeframe": "5"},
        }
    )


def _break_tq(monkeypatch) -> None:
    def boom(self, symbol, period, *, limit=200):
        raise RuntimeError("天勤账号过期")

    monkeypatch.setattr(KlineProvider, "_fetch_tq", boom)


def _fake_sina(monkeypatch) -> None:
    monkeypatch.setattr(
        KlineProvider,
        "_fetch_akshare",
        lambda self, sina_code, period, *, symbol_hint=None: _bars().copy(),
    )
    monkeypatch.setattr(
        KlineProvider,
        "_recent_minutes",
        lambda self, sina_code: _bars().copy(),
    )


def test_tq_failure_falls_back_to_sina(tmp_path, monkeypatch) -> None:
    _break_tq(monkeypatch)
    _fake_sina(monkeypatch)
    provider = _provider(tmp_path)

    bars = provider.fetch("fu2701", "5", max_bars=10)

    assert bars["close"].tolist() == [101.0, 102.0]
    assert bars.attrs["fallback_from"] == "tq"
    assert bars.attrs["source"] == "akshare"
    assert "天勤" in bars.attrs["fallback_reason"]
    health = bars.attrs["market_health"]
    assert health["status"] == "fallback"
    assert health["source"] == "akshare"
    assert health["configured_source"] == "tq"

    # 回退数据按新浪原名入库，不污染 @tq
    store = BarStore(tmp_path / "data/klines/market.sqlite")
    assert store.load("fu2701", "5") is not None
    assert store.load("fu2701@tq", "5") is None


def test_fallback_can_forbid_entry(tmp_path, monkeypatch) -> None:
    _break_tq(monkeypatch)
    _fake_sina(monkeypatch)
    provider = _provider(tmp_path, allow_entry=False)

    bars = provider.fetch("fu2701", "5", max_bars=10)

    assert bars.attrs["allow_entry"] is False
    assert bars.attrs["market_health"]["allow_entry"] is False


def test_fallback_failure_raises_clear_error(tmp_path, monkeypatch) -> None:
    _break_tq(monkeypatch)

    def sina_down(self, sina_code, period, *, symbol_hint=None):
        raise RuntimeError("新浪也连不上")

    monkeypatch.setattr(KlineProvider, "_fetch_akshare", sina_down)
    provider = _provider(tmp_path)

    with pytest.raises(RuntimeError, match="备用源也没连上|天勤这次没有拿到"):
        provider.fetch("fu2701", "5", max_bars=10)
