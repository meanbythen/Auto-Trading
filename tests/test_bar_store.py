"""本地K线库：新下载追加，旧时间不丢。"""

from datetime import datetime

import pandas as pd

from src.data.bar_store import (
    BarStore,
    aligned_bars,
    merge_bars,
    merge_local_and_download,
    rows_needing_write,
)
from src.data.kline import KlineProvider
from src.data.tq_source import session_open_minutes


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    index = pd.to_datetime([item[0] for item in rows])
    return pd.DataFrame(
        {
            "open": [item[1] for item in rows],
            "high": [item[1] + 1 for item in rows],
            "low": [item[1] - 1 for item in rows],
            "close": [item[1] for item in rows],
            "volume": [10.0 for _ in rows],
        },
        index=index,
    )


def test_merge_keeps_bars_the_new_download_no_longer_has() -> None:
    """新窗口里已经没有的旧K线要留下，同一时刻用新的收盘价。"""
    old = _frame([("2026-09-01 09:00:00", 100), ("2026-10-01 09:00:00", 110)])
    fresh = _frame([("2026-10-01 09:00:00", 111), ("2026-10-16 09:00:00", 120)])
    merged = merge_bars(old, fresh)
    assert list(merged.index.strftime("%Y-%m-%d %H:%M")) == [
        "2026-09-01 09:00",
        "2026-10-01 09:00",
        "2026-10-16 09:00",
    ]
    assert float(merged.iloc[1]["close"]) == 111


def test_store_appends_and_updates(tmp_path) -> None:
    """写入两次后，第一次独有的K线还在，重复时间被更新。"""
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert("bu2612", "5", _frame([("2026-09-01 09:00:00", 100), ("2026-10-01 09:00:00", 110)]))
    store.upsert("bu2612", "5", _frame([("2026-10-01 09:00:00", 111), ("2026-10-16 09:00:00", 120)]))
    loaded = store.load("bu2612", "5")
    assert loaded is not None
    assert store.count("bu2612", "5") == 3
    assert float(loaded.loc[datetime(2026, 10, 1, 9, 0), "close"]) == 111
    assert float(loaded.iloc[0]["close"]) == 100


def test_fetch_keeps_older_bars_when_the_next_download_is_shorter(tmp_path) -> None:
    """第二次下载只剩最近一段时，回测仍能拿到第一次留下的旧K线。"""
    cfg = {
        "_project_root": str(tmp_path),
        "data": {"source": "akshare", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 2},
        "strategy": {"timeframe": "5"},
    }
    provider = KlineProvider(cfg)
    first = _frame([("2026-09-01 09:05:00", 100), ("2026-09-01 09:10:00", 101), ("2026-10-16 22:00:00", 120)])
    second = _frame([("2026-10-16 22:00:00", 121), ("2026-10-16 22:05:00", 122)])
    provider._fetch_akshare = lambda *_args, **_kwargs: first  # type: ignore[method-assign]
    provider.fetch("bu2612", "5", keep_history=True)
    provider._fetch_akshare = lambda *_args, **_kwargs: second  # type: ignore[method-assign]
    history = provider.fetch("bu2612", "5", keep_history=True)
    assert len(history) == 4
    assert float(history.iloc[0]["close"]) == 100
    assert float(history.iloc[-1]["close"]) == 122

    # 实盘只看最近几根，但库里的旧K线不删。
    live = provider.fetch("bu2612", "5")
    assert len(live) == 2
    assert provider.store.count("bu2612", "5") == 4


def test_rows_needing_write_skips_rows_already_stored() -> None:
    """库里已经一样的K线不重写；新的一根和改动过的一根仍要写。"""
    stored = _frame([("2026-10-16 09:05:00", 100), ("2026-10-16 09:10:00", 101)])
    incoming = _frame(
        [
            ("2026-10-16 09:05:00", 100),  # 一样
            ("2026-10-16 09:10:00", 103),  # 改过
            ("2026-10-16 09:15:00", 104),  # 新的
        ]
    )
    pending = rows_needing_write(incoming, stored)
    assert pending is not None
    assert list(pending.index.strftime("%H:%M")) == ["09:10", "09:15"]


def test_live_fetch_does_not_grow_store_when_download_is_unchanged(tmp_path) -> None:
    """实盘每轮都会重复下载同一段K线，库存不应因此越写越多。"""
    cfg = {
        "_project_root": str(tmp_path),
        "data": {"source": "akshare", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 3},
        "strategy": {"timeframe": "5"},
    }
    provider = KlineProvider(cfg)
    downloaded = _frame(
        [
            ("2026-10-16 21:55:00", 100),
            ("2026-10-16 22:00:00", 101),
            ("2026-10-16 22:05:00", 102),
        ]
    )
    provider._fetch_akshare = lambda *_args, **_kwargs: downloaded  # type: ignore[method-assign]
    first = provider.fetch("bu2612", "5")
    stored_after_first = provider.store.count("bu2612", "5")
    second = provider.fetch("bu2612", "5")
    assert provider.store.count("bu2612", "5") == stored_after_first
    pd.testing.assert_frame_equal(first, second)


def test_aligned_bars_drop_demo_rows_between_real_15m_bars() -> None:
    """09:05 这种带小数的演示K线不能和 09:15 的真实行情放在一起算 SAR。"""
    mixed = _frame([("2026-10-16 09:15:00", 4620), ("2026-10-16 09:30:00", 4619)])
    mixed.loc[pd.Timestamp("2026-10-16 09:05:00")] = {
        "open": 3799.658,
        "high": 3799.741,
        "low": 3797.659,
        "close": 3798.023,
        "volume": 176,
    }
    kept = aligned_bars(mixed, "15")
    assert kept is not None
    assert list(kept.index.strftime("%H:%M")) == ["09:15", "09:30"]


def test_aligned_bars_keep_real_decimal_prices() -> None:
    """原油 0.1、国债 0.005 这类真实报价要按时间留下。一长串随机小数仍然丢掉。"""
    frame = pd.DataFrame(
        [
            {"open": 520.1, "high": 520.4, "low": 519.8, "close": 520.2, "volume": 10},
            {"open": 108.365, "high": 108.370, "low": 108.360, "close": 108.365, "volume": 8},
            {"open": 3799.658123, "high": 3799.741987, "low": 3797.659441, "close": 3798.023556, "volume": 1},
        ],
        index=pd.to_datetime(
            ["2026-10-16 09:15:00", "2026-10-16 09:20:00", "2026-10-16 09:25:00"]
        ),
    )
    kept = aligned_bars(frame, "5")
    assert kept is not None
    assert list(kept.index.strftime("%H:%M")) == ["09:15", "09:20"]
    assert float(kept.iloc[0]["close"]) == 520.2
    assert float(kept.iloc[1]["close"]) == 108.365


def test_aligned_bars_drop_old_session_open_stamps() -> None:
    """右标签下商品不可能在 09:00、13:30、21:00 收完，那是旧左标签留下的，要丢掉。"""
    frame = _frame(
        [
            ("2026-10-26 09:00:00", 5210),
            ("2026-10-26 09:05:00", 5205),
            ("2026-10-26 13:30:00", 5200),
            ("2026-10-26 21:00:00", 5330),
            ("2026-10-26 21:05:00", 5349),
        ]
    )
    kept = aligned_bars(frame, "5", session_opens=session_open_minutes("fu2701"))
    assert kept is not None
    assert list(kept.index.strftime("%H:%M")) == ["09:05", "21:05"]


def test_store_reuses_one_connection_and_turns_on_wal(tmp_path) -> None:
    """同一路径只开一条连接并启用 WAL，避免每轮 fetch 反复开关库、读写互锁。"""
    store = BarStore(tmp_path / "market.sqlite")
    first = store._connect()
    second = store._connect()
    assert first is second
    assert str(first.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    store.upsert("bu2612", "5", _frame([("2026-09-01 09:00:00", 100)]))
    assert store.count("bu2612", "5") == 1


def test_load_with_limit_returns_only_recent_bars_and_keeps_history(tmp_path) -> None:
    """实盘只读最近一段，旧K线仍留在库里，回测还能读全。"""
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(
        "bu2612",
        "5",
        _frame(
            [
                ("2026-09-01 09:00:00", 100),
                ("2026-09-02 09:00:00", 101),
                ("2026-09-03 09:00:00", 102),
            ]
        ),
    )
    tail = store.load("bu2612", "5", limit=2)
    assert tail is not None
    assert [float(v) for v in tail["close"]] == [101.0, 102.0]
    assert store.count("bu2612", "5") == 3
    assert len(store.load("bu2612", "5")) == 3


def test_aligned_bars_keep_cffex_afternoon_bar_at_1330() -> None:
    """中金所下午 13:00 开盘，13:30 是正常的右标签K线，不能按商品那套丢掉。"""
    frame = _frame([("2026-10-20 13:15:00", 6000), ("2026-10-20 13:30:00", 6001)])
    kept = aligned_bars(frame, "15", session_opens=session_open_minutes("ic0"))
    assert kept is not None
    assert list(kept.index.strftime("%H:%M")) == ["13:15", "13:30"]


def test_merge_keeps_complete_local_bars_and_appends_new_ones() -> None:
    """本地中间的K线不被这次下载改掉；最后一根刷新；更新的和缺口里的K线补上。"""
    local = _frame(
        [
            ("2026-09-01 09:00:00", 100),
            ("2026-10-01 09:00:00", 110),
            ("2026-10-16 21:00:00", 120),
        ]
    )
    fresh = _frame(
        [
            ("2026-10-01 09:00:00", 999),
            ("2026-10-02 09:00:00", 115),
            ("2026-10-16 21:00:00", 121),
            ("2026-10-16 21:05:00", 122),
        ]
    )
    merged = merge_local_and_download(local, fresh)
    assert list(merged.index.strftime("%Y-%m-%d %H:%M")) == [
        "2026-09-01 09:00",
        "2026-10-01 09:00",
        "2026-10-02 09:00",
        "2026-10-16 21:00",
        "2026-10-16 21:05",
    ]
    assert float(merged.loc[datetime(2026, 10, 1, 9, 0), "close"]) == 110
    assert float(merged.loc[datetime(2026, 10, 16, 21, 0), "close"]) == 121
    assert float(merged.iloc[-1]["close"]) == 122


def test_backtest_range_before_download_uses_local_then_new_bars(tmp_path) -> None:
    """开始日期早于这次下载窗口时，更早的本地K线还在，新K线接在后面。"""
    cfg = {
        "_project_root": str(tmp_path),
        "data": {"source": "akshare", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 2},
        "strategy": {"timeframe": "5"},
    }
    provider = KlineProvider(cfg)
    provider.store.upsert(
        "bu2612",
        "5",
        _frame([("2026-08-01 09:05:00", 80), ("2026-09-01 09:10:00", 100)]),
    )
    provider._fetch_akshare = lambda *_args, **_kwargs: _frame(  # type: ignore[method-assign]
        [("2026-10-16 22:00:00", 120), ("2026-10-16 22:05:00", 121)]
    )
    history = provider.fetch("bu2612", "5", keep_history=True, start="2026-08-01", end="2026-10-16")
    assert float(history.iloc[0]["close"]) == 80
    assert float(history.iloc[-1]["close"]) == 121
    assert len(history) == 4


def test_complete_past_range_skips_download(tmp_path) -> None:
    """结束日已经过去，而且本地从开始到结束都有K线时，不再下载。"""
    cfg = {
        "_project_root": str(tmp_path),
        "data": {"source": "akshare", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 2},
        "strategy": {"timeframe": "5"},
    }
    provider = KlineProvider(cfg)
    provider.store.upsert(
        "bu2612",
        "5",
        _frame([("2020-01-02 09:05:00", 80), ("2020-01-03 22:00:00", 90)]),
    )

    def _should_not_download(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("本地区间已经齐，不应再下载")

    # 断网：本地已齐时不该再下 1 分钟去重拼，否则会连新浪取到真实行情。
    provider._recent_minutes = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    provider._fetch_akshare = _should_not_download  # type: ignore[method-assign]
    history = provider.fetch("bu2612", "5", keep_history=True, start="2020-01-02", end="2020-01-03")
    assert len(history) == 2
    assert float(history.iloc[0]["close"]) == 80
