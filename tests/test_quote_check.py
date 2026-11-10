"""行情来源：下拉名字、分开存放，以及东方财富的 K 线怎么拆。不访问网络。"""

from __future__ import annotations

import pandas as pd

from src.data.kline import KlineProvider
from src.data.quote_check import (
    BACKTEST_SOURCE_LABELS,
    SOURCE_LABELS,
    eastmoney_secid,
    frame_from_eastmoney_lines,
    is_ths_source,
    is_tq_source,
    normalize_source,
    source_label,
    storage_symbol,
)


def test_dropdown_names_keep_old_config() -> None:
    """旧配置写 akshare，界面上仍显示新浪，不用改文件。"""
    assert normalize_source("新浪") == "akshare"
    assert normalize_source("akshare") == "akshare"
    assert normalize_source("东方财富") == "eastmoney"
    assert source_label("akshare") == "新浪"
    assert source_label("eastmoney") == "东方财富"


def test_eastmoney_is_stored_apart_from_sina() -> None:
    """两家不写进同一个合约名，换来源不会盖住原来的新浪 K 线。"""
    assert storage_symbol("bu2612", "akshare") == "bu2612"
    assert storage_symbol("bu2612", "东方财富") == "bu2612@eastmoney"


def test_tq_is_stored_apart_from_sina() -> None:
    """天勤用 @tq 后缀，新浪的 fu2701 不会被覆盖。"""
    assert normalize_source("天勤") == "tq"
    assert normalize_source("tq") == "tq"
    assert source_label("tq") == "天勤"
    assert storage_symbol("fu2701", "天勤") == "fu2701@tq"
    assert storage_symbol("fu2701", "tq") == "fu2701@tq"
    assert storage_symbol("fu2701", "akshare") == "fu2701"


def test_ths_chart_is_backtest_only_and_stored_apart() -> None:
    """同花顺图不进实盘下拉，库存带 @ths，不会盖住另外三家。"""
    assert "同花顺图" not in SOURCE_LABELS
    assert "同花顺图" in BACKTEST_SOURCE_LABELS
    assert normalize_source("同花顺图") == "ths"
    assert normalize_source("ths") == "ths"
    assert source_label("ths") == "同花顺图"
    assert storage_symbol("fu2701", "ths") == "fu2701@ths"
    assert storage_symbol("fu2701", "同花顺图") == "fu2701@ths"
    assert is_ths_source("同花顺图")
    assert not is_ths_source("天勤")
    assert not is_tq_source("同花顺图")


def test_only_tianqin_source_logs_into_tq() -> None:
    """选新浪或东方财富时，即使填了天勤账号也不该去登录。"""
    assert is_tq_source("天勤")
    assert is_tq_source("tq")
    assert not is_tq_source("新浪")
    assert not is_tq_source("akshare")
    assert not is_tq_source("东方财富")
    assert not is_tq_source("eastmoney")
    assert not is_tq_source("")


def test_eastmoney_tries_the_next_address_after_a_dropped_connection() -> None:
    """主地址把连接掐断时，换下一个地址，仍然能拿到 K 线。"""
    from src.data.quote_check import fetch_eastmoney_frame

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"klines": ["2026-10-23 11:10,4720,4725,4729,4720,1197"]}}

    calls = {"n": 0}

    def getter(url, **_kwargs):
        calls["n"] += 1
        if "91." not in url:
            raise ConnectionError("connection aborted")
        return _Response()

    frame = fetch_eastmoney_frame("bu2612", "5", getter=getter)
    assert calls["n"] >= 2
    assert float(frame.iloc[-1]["open"]) == 4720
    assert float(frame.iloc[-1]["close"]) == 4725


def test_eastmoney_failure_says_it_in_plain_words() -> None:
    """几个地址都连不上时，报错用中文说明，不把英文堆给界面。"""
    from src.data.quote_check import fetch_eastmoney_frame

    def getter(_url, **_kwargs):
        raise ConnectionError("Remote end closed connection without response")

    try:
        fetch_eastmoney_frame("bu2612", "5", getter=getter)
    except RuntimeError as exc:
        assert "连接掐断" in str(exc)
    else:
        raise AssertionError("应该告诉用户这次没拿到K线")


def test_eastmoney_line_matches_tonghuashun_candle() -> None:
    """2026-10-23 11:10 这一行是开 4720、收 4725，和同花顺的阳线一致。"""
    frame = frame_from_eastmoney_lines(
        ["2026-10-23 11:10,4720,4725,4729,4720,1197,56558060"]
    )
    row = frame.loc[pd.Timestamp("2026-10-23 11:10")]
    assert row["open"] == 4720
    assert row["close"] == 4725
    assert row["high"] == 4729
    assert row["low"] == 4720
    assert eastmoney_secid("bu2612") == "113.bu2612"


def test_eastmoney_fetch_does_not_ask_sina(tmp_path) -> None:
    """选了东方财富就只走这一家，并且不读新建浪已经存下的那根。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "eastmoney", "cache_dir": "klines", "min_bars": 1, "live_max_bars": 10},
        }
    )
    sina = pd.DataFrame(
        [{"open": 4725, "high": 4729, "low": 4722, "close": 4725, "volume": 647}],
        index=pd.to_datetime(["2026-10-23 11:10"]),
    )
    provider.store.upsert("bu2612", "5", sina)

    def fake_eastmoney(_symbol: str, _period: str, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame(
            [{"open": 4720, "high": 4729, "low": 4720, "close": 4725, "volume": 1197}],
            index=pd.to_datetime(["2026-10-23 11:10"]),
        )

    provider._fetch_eastmoney = fake_eastmoney  # type: ignore[method-assign]
    # 断网：1 分钟重拼也不能连新浪，否则会用真实行情盖掉这次的桩数据。
    provider._recent_minutes = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    provider._fetch_akshare = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应该再问新浪"))  # type: ignore[method-assign]

    got = provider.fetch("bu2612", period="5", max_bars=10)
    assert float(got.iloc[-1]["close"]) == 4725
    assert float(got.iloc[-1]["open"]) == 4720
    assert provider.store.load("bu2612", "5") is not None
    saved = provider.store.load("bu2612@eastmoney", "5")
    assert saved is not None
    assert float(saved.iloc[-1]["open"]) == 4720
