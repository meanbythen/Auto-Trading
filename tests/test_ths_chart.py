"""同花顺图信息框解析与回测取数。不打开真实窗口。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data.kline import KlineProvider
from src.data.ths_chart import (
    _chart_score,
    _target_is_latest,
    advance_to_latest,
    bars_from_tooltips,
    parse_ths_tooltip,
    walk_tooltips,
)


_BOX_2105 = """
11-06 21:05
数值 4333
开盘 4226
收盘 4306
今结 --
涨跌 -51
涨幅 -1.17%
振幅 +2.04%
最高 4315
最低 4226
持仓 162836
成交量 4.25万
成交额 18.18亿
"""

_BOX_2240 = """
11-06 22:40
数值 4346
开盘 4324
收盘 4305
今结 --
涨跌 -19
涨幅 -0.44%
振幅 +1.16%
最高 4333
最低 4283
持仓 166617
成交量 2.05万
成交额 8.83亿
"""


def test_parse_tooltip_uses_right_label_and_ohlc() -> None:
    """框顶时间当右标签；收盘用「收盘」而不是最新数值。"""
    now = datetime(2026, 11, 8, 21, 0, 0)
    row = parse_ths_tooltip(_BOX_2105, now=now)
    assert row is not None
    assert row["datetime"] == datetime(2026, 11, 6, 21, 5, 0)
    assert row["open"] == 4226
    assert row["close"] == 4306
    assert row["high"] == 4315
    assert row["low"] == 4226
    assert row["volume"] == 42500


def test_parse_tooltip_one_line_blob() -> None:
    """无障碍接口常把整框粘成一行，仍要能拆出开高低收。"""
    blob = "11-06 22:55 数值 4318 开盘 4325 收盘 4322 今结 -- 最高 4332 最低 4321 成交量 1036"
    row = parse_ths_tooltip(blob, now=datetime(2026, 11, 8, 23, 0, 0))
    assert row is not None
    assert row["datetime"] == datetime(2026, 11, 6, 22, 55, 0)
    assert row["open"] == 4325
    assert row["close"] == 4322
    assert row["high"] == 4332
    assert row["low"] == 4321


def test_parse_tooltip_volume_when_label_and_number_split() -> None:
    """无障碍接口常把成交量和数字拆成两行，不能再写成 0。"""
    blob = """
11-06 10:50
数值
4319
开盘
4325
收盘
4308
最高
4326
最低
4306
持仓
160548
成交量
5544.00
成交额
2.39亿
"""
    row = parse_ths_tooltip(blob, now=datetime(2026, 11, 9, 12, 0, 0))
    assert row is not None
    assert row["open"] == 4325
    assert row["close"] == 4308
    assert row["high"] == 4326
    assert row["low"] == 4306
    assert row["volume"] == 5544.0
    assert row["oi"] == 160548


def test_parse_tooltip_volume_fullwidth_colon_and_comma() -> None:
    blob = "11-06 10:50 开盘：4325 收盘：4308 最高：4326 最低：4306 成交量：5,544.00"
    row = parse_ths_tooltip(blob, now=datetime(2026, 11, 9, 12, 0, 0))
    assert row is not None
    assert row["volume"] == 5544.0
    assert row["close"] == 4308


def test_parse_tooltip_volume_wan_and_close_prefers_shoupan() -> None:
    now = datetime(2026, 11, 8, 22, 50, 0)
    row = parse_ths_tooltip(_BOX_2240, now=now)
    assert row is not None
    assert row["datetime"] == datetime(2026, 11, 6, 22, 40, 0)
    assert row["close"] == 4305
    assert row["volume"] == 20500


def test_walk_left_collects_older_bars_and_stops_on_repeat() -> None:
    boxes = [_BOX_2240, _BOX_2105, _BOX_2105]
    keys = []

    def read_bar():
        if not boxes:
            return None
        return parse_ths_tooltip(boxes.pop(0), now=datetime(2026, 11, 8))

    def press(key: str) -> None:
        keys.append(key)

    rows = walk_tooltips(read_bar, press, limit=10, pause_sec=0)
    assert len(rows) == 2
    assert rows[0]["datetime"] == datetime(2026, 11, 6, 22, 40, 0)
    assert rows[1]["datetime"] == datetime(2026, 11, 6, 21, 5, 0)
    frame = bars_from_tooltips(rows)
    assert list(frame.index) == [pd.Timestamp("2026-11-06 21:05:00"), pd.Timestamp("2026-11-06 22:40:00")]
    assert float(frame.iloc[0]["oi"]) == 162836
    assert "left" in keys


def test_walk_reports_progress_for_every_bar() -> None:
    """每读到一根就回调一次，界面才能实时刷进度条和日志。"""
    boxes = [_BOX_2240, _BOX_2105, _BOX_2105]
    seen: list[tuple[int, str]] = []

    def read_bar():
        if not boxes:
            return None
        return parse_ths_tooltip(boxes.pop(0), now=datetime(2026, 11, 8))

    def on_read(count: int, row: dict) -> None:
        seen.append((count, row["datetime"].strftime("%H:%M")))

    walk_tooltips(read_bar, lambda _key: None, limit=10, pause_sec=0, on_read=on_read)
    assert seen == [(1, "22:40"), (2, "21:05")]


def test_chart_score_prefers_kline_over_quote_list() -> None:
    assert _chart_score("Product.FuturePro.KLinePage\n开盘 4600 收盘 4610") > 0
    assert _chart_score("Product.FuturePro.QuotationPage\n自选股\nlu2701 5093") < 0
    assert _chart_score("") == 0


def test_advance_to_latest_presses_right_until_stamp_stops() -> None:
    """点击落在中间时，一路按右键推到最新一根；时间连续两批不变才停。"""
    stamps = [
        "2026-11-13 21:50:00",
        "2026-11-13 22:00:00",
        "2026-11-13 22:30:00",
        "2026-11-13 22:30:00",
        "2026-11-13 22:30:00",
    ]
    presses: list[str] = []

    def read_stamp() -> str:
        return stamps.pop(0) if stamps else "2026-11-13 22:30:00"

    pressed, last = advance_to_latest(
        read_stamp, lambda: presses.append("right"), step=3, pause_sec=0
    )
    assert last == "2026-11-13 22:30:00"
    assert pressed == 12
    assert presses == ["right"] * 12


def test_advance_to_latest_stops_on_cancel_and_miss() -> None:
    """停止按钮按下后不再按键；信息框一直读不到时也不空按满上限。"""
    pressed, _ = advance_to_latest(
        lambda: "2026-11-13 21:50:00", lambda: None, pause_sec=0, should_stop=lambda: True
    )
    assert pressed == 0

    misses = {"n": 0}

    def empty() -> str:
        misses["n"] += 1
        return ""

    pressed, last = advance_to_latest(empty, lambda: None, step=2, pause_sec=0)
    assert last == ""
    assert pressed == 6


def test_target_is_latest_only_when_end_is_now() -> None:
    """结束时间是现在才自己推到最新；填了历史时点就走手动定位。"""
    assert _target_is_latest(None, "5")
    assert _target_is_latest(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "5")
    assert not _target_is_latest("2026-11-11 23:00:00", "5")
    assert not _target_is_latest("2020-01-01 09:00:00", "15")


def test_walk_stops_when_asked_and_keeps_read_bars() -> None:
    """ESC / 停止按钮会打断左键循环，已经读到的根还在。"""
    boxes = [_BOX_2240, _BOX_2105, _BOX_2105]
    presses: list[str] = []

    def read_bar():
        if not boxes:
            return None
        return parse_ths_tooltip(boxes.pop(0), now=datetime(2026, 11, 8))

    def press(key: str) -> None:
        presses.append(key)

    def should_stop() -> bool:
        return len(presses) >= 1

    rows = walk_tooltips(read_bar, press, limit=10, pause_sec=0, should_stop=should_stop)
    assert len(rows) >= 1
    assert len(presses) == 1


def test_live_fetch_refuses_ths(tmp_path) -> None:
    """实盘扫描 keep_history=False 时直接拒绝，避免抢鼠标。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "ths", "cache_dir": "klines", "min_bars": 1, "live_max_bars": 10},
        }
    )
    with pytest.raises(RuntimeError, match="只能用于历史回测"):
        provider.fetch("fu2701", period="5", max_bars=10)


def test_backtest_ths_without_local_asks_to_scrape(tmp_path) -> None:
    """没点过读图时，回测不能去抢期货通鼠标。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "ths", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 10},
        }
    )
    provider._fetch_ths = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不该读图"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="从同花顺图读取K线"):
        provider.fetch("fu2701", period="5", keep_history=True)


def test_backtest_uses_saved_ths_even_if_end_is_today(tmp_path) -> None:
    """已经读进本地的同花顺图，回测结束日是今天也不再去期货通读。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "ths", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 10},
        }
    )
    fake = pd.DataFrame(
        [{"open": 4324, "high": 4333, "low": 4283, "close": 4305, "volume": 20500}],
        index=pd.to_datetime(["2026-11-06 22:40"]),
    )
    calls = {"n": 0}

    def fake_ths(_symbol: str, _period: str, **_kwargs) -> pd.DataFrame:
        calls["n"] += 1
        return fake

    provider._fetch_ths = fake_ths  # type: ignore[method-assign]
    provider._fetch_akshare = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不该问新浪"))  # type: ignore[method-assign]
    provider.import_ths_chart("fu2701", "5")
    assert calls["n"] == 1
    today = datetime.now().strftime("%Y-%m-%d")
    got = provider.fetch("fu2701", period="5", keep_history=True, start="2026-01-01", end=today)
    assert calls["n"] == 1
    assert float(got.iloc[-1]["close"]) == 4305
    assert provider.store.load("fu2701@ths", "5") is not None
    assert provider.store.load("fu2701", "5") is None


def test_import_ths_chart_writes_at_ths(tmp_path) -> None:
    """按钮读图成功后只写入 @ths。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "ths", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 10},
        }
    )
    fake = pd.DataFrame(
        [{"open": 4325, "high": 4332, "low": 4321, "close": 4322, "volume": 1036, "oi": 162836}],
        index=pd.to_datetime(["2026-11-06 22:55"]),
    )
    provider._fetch_ths = lambda *_a, **_k: fake  # type: ignore[method-assign]
    got = provider.import_ths_chart("fu2701", "5")
    assert float(got.iloc[-1]["close"]) == 4322
    assert float(got.iloc[-1]["oi"]) == 162836
    stored = provider.store.load("fu2701@ths", "5")
    assert stored is not None
    assert float(stored.iloc[-1]["oi"]) == 162836
    raw = provider.raw_store.load("fu2701@ths", "5", "ths")
    assert raw is not None
    assert float(raw.iloc[-1]["oi"]) == 162836
    assert provider.store.load("fu2701", "5") is None


def test_import_ths_chart_empty_raises(tmp_path) -> None:
    """读不到时直接报错，不用别家库存顶替。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "ths", "cache_dir": "data/klines", "min_bars": 1, "live_max_bars": 10},
        }
    )
    provider._fetch_ths = lambda *_a, **_k: pd.DataFrame(  # type: ignore[method-assign]
        columns=["open", "high", "low", "close", "volume"]
    )
    with pytest.raises(RuntimeError, match="没有读到"):
        provider.import_ths_chart("fu2701", "5")
