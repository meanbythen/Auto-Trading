"""行情来源：下拉名字、分开存放。东方财富已下线。不访问网络。"""

from __future__ import annotations

import pandas as pd

from src.data.kline import KlineProvider
from src.data.periods import canonicalize_ohlc
from src.data.quote_check import (
    BACKTEST_SOURCE_LABELS,
    SOURCE_LABELS,
    is_live_source,
    is_ths_source,
    is_tq_source,
    normalize_fallback,
    normalize_source,
    source_label,
    storage_symbol,
    stored_source_label,
)
from src.data.ths_chart import bars_from_tooltips, parse_ths_tooltip
from src.data.tq_source import frame_from_klines


def test_dropdown_names_keep_old_config() -> None:
    """旧配置写 akshare，界面上仍显示新浪，不用改文件。"""
    assert normalize_source("新浪") == "akshare"
    assert normalize_source("akshare") == "akshare"
    assert source_label("akshare") == "新浪"
    assert SOURCE_LABELS == ("天勤", "新浪")
    assert "东方财富" not in SOURCE_LABELS
    assert "东方财富" not in BACKTEST_SOURCE_LABELS


def test_retired_eastmoney_becomes_tianqin() -> None:
    """以前选东方财富的配置，改成主源天勤，不再去连东财。"""
    assert normalize_source("东方财富") == "tq"
    assert normalize_source("eastmoney") == "tq"
    assert source_label("eastmoney") == "天勤"
    assert stored_source_label("eastmoney") == "东方财富(已停用)"
    assert normalize_fallback("eastmoney") == "akshare"
    assert normalize_fallback("tq") == "akshare"
    assert storage_symbol("bu2612", "东方财富") == "bu2612@tq"


def test_tq_is_stored_apart_from_sina() -> None:
    """天勤用 @tq 后缀，新浪的 fu2701 不会被覆盖。"""
    assert normalize_source("天勤") == "tq"
    assert normalize_source("tq") == "tq"
    assert source_label("tq") == "天勤"
    assert storage_symbol("fu2701", "天勤") == "fu2701@tq"
    assert storage_symbol("fu2701", "tq") == "fu2701@tq"
    assert storage_symbol("fu2701", "akshare") == "fu2701"


def test_ths_chart_is_backtest_only_and_stored_apart() -> None:
    """同花顺图不进实盘下拉，库存带 @ths。"""
    assert "同花顺图" not in SOURCE_LABELS
    assert "同花顺图" in BACKTEST_SOURCE_LABELS
    assert normalize_source("同花顺图") == "ths"
    assert source_label("ths") == "同花顺图"
    assert storage_symbol("fu2701", "ths") == "fu2701@ths"
    assert is_ths_source("同花顺图")
    assert not is_ths_source("天勤")
    assert not is_live_source("同花顺图")
    assert is_live_source("天勤")
    assert is_live_source("新浪")


def test_only_tianqin_source_logs_into_tq() -> None:
    """选新浪时，即使填了天勤账号也不该去登录。"""
    assert is_tq_source("天勤")
    assert is_tq_source("tq")
    assert not is_tq_source("新浪")
    assert not is_tq_source("akshare")
    assert is_tq_source("东方财富")  # 旧配置已迁到天勤
    assert is_tq_source("")  # 缺省主源是天勤


def test_fetch_health_records_download_latency_and_bar_age(tmp_path, monkeypatch) -> None:
    """模拟 250ms 下载：接收时间、来源、耗时和最新 K 线年龄必须可核对。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "akshare", "cache_dir": "klines", "min_bars": 1},
        }
    )
    frame = pd.DataFrame(
        [{"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10}],
        index=pd.DatetimeIndex([pd.Timestamp.now().floor("min")]),
    )
    provider._fetch_impl = lambda *_args, **_kwargs: frame  # type: ignore[method-assign]
    ticks = iter([100.0, 100.25])
    monkeypatch.setattr("src.data.kline.time.perf_counter", lambda: next(ticks))

    got = provider.fetch("bu2612", period="5")

    health = got.attrs["market_health"]
    assert health["source"] == "akshare"
    assert health["download_ms"] == 250.0
    assert health["fetch_started_at"] and health["received_at"]
    assert health["latest_bar_time"] == str(frame.index[-1])
    assert health["latest_bar_age_minutes"] >= 0
    assert health["from_cache"] is False
    assert provider.get_market_health("bu2612", "5") == health


def test_tq_outage_only_falls_back_to_tq_cache(tmp_path) -> None:
    """天勤断流只读 @tq 本地库；备源另走回退，不会把新浪写进 @tq。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "tq", "cache_dir": "klines", "min_bars": 1, "fallback_source": "akshare"},
        }
    )
    cached = pd.DataFrame(
        [{"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10}],
        index=pd.DatetimeIndex([pd.Timestamp.now().floor("min")]),
    )
    provider.store.upsert("bu2612@tq", "5", cached)

    def offline(*_args, **_kwargs):
        raise ConnectionError("模拟天勤断流")

    provider._fetch_tq = offline  # type: ignore[method-assign]
    provider._try_fallback = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    provider._rebuild_with_minutes = lambda _symbol, _code, _period, full: full  # type: ignore[method-assign]

    got = provider.fetch("bu2612", period="5")

    health = got.attrs["market_health"]
    assert health["source"] == "tq"
    assert health["from_cache"] is True
    assert provider.source == "tq"


def test_three_sources_share_right_label_and_ohlc_columns() -> None:
    """天勤起点、同花顺图右标签、整理后的表，时间都落在 09:05，列名一致。"""
    # tqsdk：UTC 01:00 起点 +8 小时 = 北京 09:00，再加 5 分钟右标签 = 09:05。
    stamps = pd.to_datetime(["2026-11-06 01:00"]).astype("datetime64[ns]").asi8
    tq_raw = pd.DataFrame(
        {
            "datetime": stamps,
            "open": [4300.0],
            "high": [4345.0],
            "low": [4297.0],
            "close": [4333.0],
            "volume": [100.0],
        }
    )
    tq = frame_from_klines(tq_raw, 300)
    tooltip = parse_ths_tooltip(
        "11-06 09:05\n数值 4334\n开盘 4300\n收盘 4334\n最高 4343\n最低 4297\n成交量 100",
        now=pd.Timestamp("2026-11-06 15:00").to_pydatetime(),
    )
    assert tooltip is not None
    ths = bars_from_tooltips([tooltip])
    sina = canonicalize_ohlc(
        pd.DataFrame(
            [{"open": 4240.0, "high": 4343.0, "low": 4240.0, "close": 4334.0, "volume": 10.0}],
            index=pd.DatetimeIndex(["2026-11-06 09:05:00+08:00"]),
        )
    )
    assert list(tq.index) == [pd.Timestamp("2026-11-06 09:05:00")]
    assert list(ths.index) == [pd.Timestamp("2026-11-06 09:05:00")]
    assert sina is not None
    assert list(sina.index) == [pd.Timestamp("2026-11-06 09:05:00")]
    assert sina.index.tz is None
    for frame in (tq, ths, sina):
        assert frame.index.name == "datetime"
        for col in ("open", "high", "low", "close", "volume"):
            assert col in frame.columns
