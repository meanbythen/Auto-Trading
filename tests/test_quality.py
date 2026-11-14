"""接口原样库与清洗库分开存；质量检查只读本地、不下单。"""

from datetime import datetime

import pandas as pd

from src.data.bar_store import BarStore, RawBarStore, aligned_bars
from src.data.kline import KlineProvider
from openpyxl import load_workbook

from src.data.quality import (
    format_quality_book_text,
    inspect_quote,
    inspect_quote_book,
    write_quality_report,
)


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """测试用的简易K线表。"""
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


def test_raw_store_keeps_misaligned_and_long_decimal(tmp_path) -> None:
    """原样库留下 09:07 和对不齐的长小数；清洗规则仍会丢掉它们。"""
    store = RawBarStore(tmp_path / "raw.sqlite")
    mixed = _frame([("2026-10-16 09:07:00", 100.0), ("2026-10-16 09:10:00", 101.0)])
    mixed.loc[pd.Timestamp("2026-10-16 09:07:00"), "close"] = 3799.6581234
    store.upsert("fu2701", "5", "akshare", mixed)
    loaded = store.load("fu2701", "5", "akshare")
    assert loaded is not None
    assert len(loaded) == 2
    assert pd.Timestamp("2026-10-16 09:07:00") in loaded.index
    cleaned = aligned_bars(loaded, "5")
    assert cleaned is not None
    assert pd.Timestamp("2026-10-16 09:07:00") not in cleaned.index
    assert pd.Timestamp("2026-10-16 09:10:00") in cleaned.index


def test_inspect_quote_counts_dropped_bars(tmp_path) -> None:
    """有原样、有清洗时，报告能数出丢掉的脏时间戳。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    vendor = _frame(
        [
            ("2026-10-16 09:05:00", 100.0),
            ("2026-10-16 09:15:00", 101.0),
            ("2026-10-16 09:30:00", 102.0),
        ]
    )
    raw.upsert("fu2701", "15", "akshare", vendor)
    kept = aligned_bars(vendor, "15")
    assert kept is not None
    clean.upsert("fu2701", "15", kept)
    report = inspect_quote(tmp_path, "fu2701", "15", "akshare")
    assert report.raw_n == 3
    assert report.clean_n == 2
    assert report.dropped_n == 1
    assert report.misaligned_n == 1
    assert report.status in {"ok", "watch"}
    path = write_quality_report(tmp_path, report)
    assert path.suffix == ".xlsx"
    book = load_workbook(path)
    assert "总览" in book.sheetnames
    overview = " ".join(str(cell.value or "") for row in book["总览"].iter_rows(max_col=2) for cell in row)
    assert "原样" in overview
    assert (tmp_path / "data" / "行情质量.xlsx").exists()


def test_inspect_empty_when_no_store(tmp_path) -> None:
    """两个库都没有时，结论是还没数据，而不是报错。"""
    report = inspect_quote(tmp_path, "fu2701", "5", "akshare")
    assert report.status == "empty"
    assert report.raw_n == 0
    assert report.clean_n == 0


def test_save_vendor_raw_does_not_touch_clean_store(tmp_path) -> None:
    """另存原样时，清洗库根数不变。失败也不该抛到开仓路径。"""
    cfg = {
        "_project_root": str(tmp_path),
        "data": {"source": "akshare", "cache_dir": "data/klines", "min_bars": 1},
        "strategy": {"timeframe": "5"},
    }
    provider = KlineProvider(cfg)
    vendor = _frame([("2026-10-16 09:05:00", 100.0), ("2026-10-16 09:10:00", 101.0)])
    provider._save_vendor_raw("fu2701", "5", vendor)
    assert provider.raw_store.count("fu2701", "5", "akshare") == 2
    assert provider.store.count("fu2701", "5") == 0
    provider._save_vendor_raw("fu2701", "5", None)
    assert provider.raw_store.count("fu2701", "5", "akshare") == 2
    loaded = provider.raw_store.load("fu2701", "5", "akshare")
    assert loaded is not None
    assert datetime(2026, 10, 16, 9, 5) in list(loaded.index.to_pydatetime())


def _put_source(raw: RawBarStore, clean: BarStore, src: str, symbol: str, df: pd.DataFrame) -> None:
    """按各家库存名写入原样和清洗，模拟已经拉过一次。"""
    from src.data.quote_check import storage_symbol

    stored = storage_symbol(symbol, src)
    raw.upsert(stored, "5", src, df)
    clean.upsert(stored, "5", df)


def test_three_sources_side_by_side_close_spread(tmp_path) -> None:
    """三家同一时刻并排：东财收盘贵 2 点时，报告能写出差几跳、大约几元。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    times = [f"2026-10-16 09:{minute:02d}:00" for minute in range(10, 40, 5)]
    sina = _frame([(t, 100.0) for t in times])
    em = _frame([(t, 100.0) for t in times])
    tq = _frame([(t, 100.0) for t in times])
    em.loc[pd.Timestamp(times[1]), "close"] = 102.0
    _put_source(raw, clean, "akshare", "fu2701", sina)
    _put_source(raw, clean, "eastmoney", "fu2701", em)
    _put_source(raw, clean, "tq", "fu2701", tq)
    cfg = {
        "trade": {"ctp": {"price_tick": 1}},
        "risk": {"contracts": {"fu": {"multiplier": 10, "price_tick": 1}}},
    }
    book = inspect_quote_book(tmp_path, "fu2701", "5", cfg=cfg)
    assert book.clean_cmp["n_sources"] == 3
    assert book.clean_cmp["overlap3"] == len(times)
    assert book.clean_cmp["close_max_ticks"] >= 1.9
    assert book.clean_cmp["yuan_max_1lot"] >= 19
    text = format_quality_book_text(book)
    assert "并排" in text
    assert "对实际交易的影响" in text
    path = write_quality_report(tmp_path, book)
    wb = load_workbook(path)
    assert "问题K线" in wb.sheetnames
    assert "清洗同刻" in wb.sheetnames
    assert "两两对照" in wb.sheetnames
    headers = [cell.value for cell in wb["清洗同刻"][1]]
    assert "东方财富收" in headers
    assert "同花顺图收" in headers
    assert "收盘最大差跳" in headers
    # 东财那根贵 2 点，应出现在问题K线里，能按跳数筛。
    closes = []
    head = [cell.value for cell in wb["问题K线"][1]]
    close_idx = head.index("东方财富收")
    tick_idx = head.index("收盘最大差跳")
    for row in wb["问题K线"].iter_rows(min_row=2, values_only=True):
        closes.append(row[close_idx])
        if row[tick_idx] is not None:
            assert float(row[tick_idx]) >= 0.5
    assert 102.0 in closes


def test_four_sources_overlap_counts_ths(tmp_path) -> None:
    """同花顺图也进对照。三家已齐、第四家缺一根时，至少三家同一时刻仍能数出来。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    times = [f"2026-10-16 09:{minute:02d}:00" for minute in range(10, 40, 5)]
    base = _frame([(t, 100.0) for t in times])
    ths = _frame([(t, 100.0) for t in times[:-1]])
    _put_source(raw, clean, "akshare", "fu2701", base)
    _put_source(raw, clean, "eastmoney", "fu2701", base)
    _put_source(raw, clean, "tq", "fu2701", base)
    _put_source(raw, clean, "ths", "fu2701", ths)
    book = inspect_quote_book(tmp_path, "fu2701", "5")
    assert book.clean_cmp["n_sources"] == 4
    assert book.clean_cmp["overlap3"] >= len(times) - 1
    assert book.clean_cmp["overlap4"] == len(times) - 1


def test_book_asks_to_download_when_only_one_source(tmp_path) -> None:
    """只有一家时不能并排，会提醒先换来源再拉。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    df = _frame([("2026-10-16 09:10:00", 100.0), ("2026-10-16 09:15:00", 101.0)])
    _put_source(raw, clean, "akshare", "fu2701", df)
    book = inspect_quote_book(tmp_path, "fu2701", "5")
    assert book.status in {"watch", "empty"}
    assert book.clean_cmp["n_sources"] == 1
    assert "只有" in book.message or "只有" in (book.clean_cmp.get("message") or "")
