"""本地K线库存总览：只读 sqlite，不拉网。"""

import pandas as pd
from openpyxl import load_workbook

from src.data.bar_store import BarStore, RawBarStore
from src.data.inventory import (
    collect_kline_inventory,
    inventory_message,
    preview_purge,
    preview_rebuilt_klines,
    purge_klines,
    purge_rebuilt_klines,
    split_stored_symbol,
    write_kline_inventory,
)
from tests.test_quality import _frame, _put_source


def test_split_stored_symbol_reads_suffix() -> None:
    assert split_stored_symbol("fu2701") == ("fu2701", "akshare")
    assert split_stored_symbol("fu2701@eastmoney") == ("fu2701", "eastmoney")
    assert split_stored_symbol("fu2701@tq") == ("fu2701", "tq")
    assert split_stored_symbol("fu2701@ths") == ("fu2701", "ths")


def test_inventory_lists_source_layer_period_and_count(tmp_path) -> None:
    """清洗和原样都会出现；同花顺图带自己的来源名。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    sina = _frame([("2026-10-16 09:10:00", 100.0), ("2026-10-16 09:15:00", 101.0)])
    ths = _frame([("2026-10-16 09:10:00", 100.0)])
    _put_source(raw, clean, "akshare", "fu2701", sina)
    _put_source(raw, clean, "ths", "fu2701", ths)

    book = collect_kline_inventory(tmp_path)
    assert book["clean_groups"] == 2
    assert book["raw_groups"] == 2
    assert book["clean_bars"] == 3
    assert "新浪" in book["sources"]
    assert "同花顺图" in book["sources"]
    assert "fu2701" in book["contracts"]

    detail = book["detail"]
    sina_clean = next(
        row
        for row in detail
        if row["数据源"] == "新浪" and str(row["哪一份"]).startswith("清洗")
    )
    assert sina_clean["合约"] == "fu2701"
    assert sina_clean["品种"] == "fu"
    assert sina_clean["周期"] == "5分钟"
    assert sina_clean["根数"] == 2
    assert "2026-10-16 09:10:00" in str(sina_clean["从"])
    assert "2026-10-16 09:15:00" in str(sina_clean["到"])

    path = write_kline_inventory(tmp_path, book)
    wb = load_workbook(path)
    assert "总览" in wb.sheetnames
    assert "明细" in wb.sheetnames
    assert "按数据源" in wb.sheetnames
    headers = [cell.value for cell in wb["明细"][1]]
    assert headers == [
        "哪一份",
        "数据源",
        "品种",
        "合约",
        "周期",
        "根数",
        "从",
        "到",
        "跨度天数",
        "距今分钟",
        "库存名",
    ]
    assert (tmp_path / "data" / "K线库存.xlsx").exists()
    assert "fu2701" not in inventory_message(book) or "合约 1 个" in inventory_message(book)
    assert "明细请看 Excel" in inventory_message(book)


def test_purge_one_source_keeps_the_other(tmp_path) -> None:
    """删同花顺图时，新浪还在。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    sina = _frame([("2026-10-16 09:10:00", 100.0), ("2026-10-16 09:15:00", 101.0)])
    ths = _frame([("2026-10-16 09:10:00", 100.0), ("2026-10-16 09:15:00", 102.0)])
    _put_source(raw, clean, "akshare", "fu2701", sina)
    _put_source(raw, clean, "ths", "fu2701", ths)
    seen = preview_purge(tmp_path, source="ths", symbol="fu2701")
    assert seen["clean_n"] == 2
    assert seen["raw_n"] == 2
    result = purge_klines(tmp_path, source="ths", symbol="fu2701")
    assert result["clean_deleted"] == 2
    assert clean.count("fu2701", "5") == 2
    assert clean.count("fu2701@ths", "5") == 0
    assert raw.count("fu2701", "5", "akshare") == 2
    assert raw.count("fu2701@ths", "5", "ths") == 0


def test_purge_time_range_keeps_other_bars(tmp_path) -> None:
    """只删一段时间，前后的K线还在。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    bars = _frame(
        [
            ("2026-10-14 22:00:00", 100.0),
            ("2026-10-15 22:00:00", 101.0),
            ("2026-10-16 22:00:00", 102.0),
        ]
    )
    _put_source(raw, clean, "akshare", "fu2701", bars)
    csv_path = kline_dir / "fu2701_5m.csv"
    bars.to_csv(csv_path, encoding="utf-8-sig")
    purge_klines(
        tmp_path,
        source="akshare",
        symbol="fu2701",
        period="5",
        ts_from="2026-10-15",
        ts_to="2026-10-15",
    )
    left = clean.load("fu2701", "5")
    assert left is not None
    assert len(left) == 2
    assert pd.Timestamp("2026-10-15 22:00:00") not in left.index
    saved = pd.read_csv(csv_path)
    assert len(saved) == 2


def test_purge_csv_locked_still_deletes_sqlite(tmp_path, monkeypatch) -> None:
    """Excel 占用 csv 时，库仍删掉，并记下哪份表改不了。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    bars = _frame([("2026-10-16 09:10:00", 100.0)])
    _put_source(raw, clean, "ths", "fu2701", bars)
    csv_path = kline_dir / "fu2701@ths_5m.csv"
    bars.to_csv(csv_path, encoding="utf-8-sig")

    def _busy_unlink(self, missing_ok=False):
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr("pathlib.Path.unlink", _busy_unlink)
    result = purge_klines(tmp_path, source="ths", symbol="fu2701")
    assert result["clean_deleted"] == 1
    assert result["raw_deleted"] == 1
    assert clean.count("fu2701@ths", "5") == 0
    assert csv_path.exists()
    assert any("fu2701@ths_5m.csv" in p for p in result["csv_locked"])
    from src.data.periods import csv_skip_marker

    assert csv_skip_marker(csv_path).exists()


def test_prepare_local_does_not_restore_csv_when_skip_marker_exists(tmp_path) -> None:
    """删库后 CSV 还在，但有 .skip 时，读图前不能把旧表补回清洗库。"""
    from src.data.kline import KlineProvider
    from src.data.periods import csv_skip_marker

    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    csv_path = kline_dir / "fu2701@ths_5m.csv"
    bars = _frame([("2026-10-16 09:10:00", 100.0), ("2026-10-16 09:15:00", 101.0)])
    bars.to_csv(csv_path, encoding="utf-8-sig")
    csv_skip_marker(csv_path).write_text("purged\n", encoding="utf-8")
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "ths", "cache_dir": "data/klines", "min_bars": 1},
            "strategy": {"timeframe": "5"},
        }
    )
    local = provider._prepare_local("fu2701", "5")
    assert local is None
    assert provider.store.count("fu2701@ths", "5") == 0


def test_save_cache_lock_does_not_mark_live_csv_skip(tmp_path, monkeypatch) -> None:
    """实盘写 CSV 被 Excel 占用时，不能给新浪文件打 .skip，否则扫描再也读不到备份。"""
    from src.data.kline import KlineProvider
    from src.data.periods import csv_skip_marker

    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "akshare", "cache_dir": "data/klines", "min_bars": 1},
            "strategy": {"timeframe": "5"},
        }
    )
    bars = _frame([("2026-10-16 09:10:00", 100.0)])

    def _busy(*_args, **_kwargs):
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(pd.DataFrame, "to_csv", _busy)
    provider._save_cache("fu2701", "5", bars)
    path = provider._cache_path("fu2701", "5")
    assert not csv_skip_marker(path).exists()


def test_purge_deletes_leftover_csv_when_sqlite_already_empty(tmp_path) -> None:
    """清洗库已经空了，文件夹里还留着 CSV 时，再删一次也能去掉这份表。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    csv_path = kline_dir / "fu2701@ths_5m.csv"
    bars = _frame([("2026-10-16 09:10:00", 100.0)])
    bars.to_csv(csv_path, encoding="utf-8-sig")
    result = purge_klines(tmp_path, source="ths", symbol="fu2701")
    assert result["clean_deleted"] == 0
    assert not csv_path.exists()


def test_purge_rebuilt_klines_keeps_one_minute_tick_and_raw(tmp_path) -> None:
    """一键清拼合 K：只删 2 分钟及以上，1 分钟、日线、tick 原始和原样库都留着。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    tick = BarStore(kline_dir / "tick_bars.sqlite")
    one = _frame([("2026-10-16 09:01:00", 100.0), ("2026-10-16 09:02:00", 101.0)])
    five = _frame(
        [
            ("2026-10-16 09:05:00", 100.0),
            ("2026-10-16 09:10:00", 101.0),
            ("2026-10-16 09:15:00", 102.0),
        ]
    )
    clean.upsert("fu2701", "1", one)
    clean.upsert("fu2701", "5", five)
    clean.upsert("fu2701", "30", five)
    clean.upsert("fu2701", "D", five)
    raw.upsert("fu2701", "30", "akshare", five)
    tick.upsert("fu2701@tick", "1", one)
    tick.upsert("fu2701@tick", "60", five)
    one_csv = kline_dir / "fu2701_1m.csv"
    five_csv = kline_dir / "fu2701_5m.csv"
    one.to_csv(one_csv, encoding="utf-8-sig")
    five.to_csv(five_csv, encoding="utf-8-sig")

    seen = preview_rebuilt_klines(tmp_path)
    assert seen["clean_n"] == 6
    assert seen["tick_n"] == 3
    assert seen["periods"] == ["5", "30", "60"]

    result = purge_rebuilt_klines(tmp_path)
    assert result["clean_deleted"] == 6
    assert result["tick_deleted"] == 3
    assert clean.count("fu2701", "1") == 2
    assert clean.count("fu2701", "5") == 0
    assert clean.count("fu2701", "30") == 0
    assert clean.count("fu2701", "D") == 3
    assert tick.count("fu2701@tick", "1") == 2
    assert tick.count("fu2701@tick", "60") == 0
    assert raw.count("fu2701", "30", "akshare") == 3
    assert one_csv.exists()
    assert not five_csv.exists()


def test_purge_refuses_to_wipe_everything(tmp_path) -> None:
    import pytest

    with pytest.raises(ValueError, match="至少选一个"):
        preview_purge(tmp_path)
