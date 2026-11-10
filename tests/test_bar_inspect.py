"""抽查某一根本地K线：只读 sqlite/csv，不拉网。"""

from pathlib import Path

from src.data.bar_inspect import format_kline_inspect, inspect_kline, parse_bar_when
from src.data.bar_store import BarStore, RawBarStore
from tests.test_quality import _frame, _put_source


def test_parse_bar_when_accepts_short_clock() -> None:
    assert parse_bar_when("2026-9-22", "10:50", "1") == "2026-09-22 10:50:00"
    assert parse_bar_when("2026-09-22", "", "D") == "2026-09-22 00:00:00"


def test_inspect_kline_reads_clean_raw_and_csv(tmp_path: Path) -> None:
    """精确时刻能对上清洗、原样和 csv 同一行开高低收。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    bars = _frame(
        [
            ("2026-09-22 10:45:00", 3800.0),
            ("2026-09-22 10:50:00", 3808.0),
            ("2026-09-22 10:55:00", 3810.0),
        ]
    )
    bars.loc["2026-09-22 10:50:00", "open"] = 3805.0
    bars.loc["2026-09-22 10:50:00", "high"] = 3814.0
    bars.loc["2026-09-22 10:50:00", "low"] = 3803.0
    bars.loc["2026-09-22 10:50:00", "volume"] = 960.0
    _put_source(raw, clean, "ths", "fu2701", bars)
    csv_path = kline_dir / "fu2701@ths_5m.csv"
    bars.to_csv(csv_path, encoding="utf-8-sig")

    book = inspect_kline(
        tmp_path,
        source="ths",
        symbol="fu2701",
        period="5",
        date_text="2026-09-22",
        time_text="10:50",
    )
    assert book["exact"] is True
    assert book["stored"] == "fu2701@ths"
    assert book["clean"]["open"] == 3805.0
    assert book["clean"]["high"] == 3814.0
    assert book["clean"]["low"] == 3803.0
    assert book["clean"]["close"] == 3808.0
    assert book["clean"]["volume"] == 960.0
    assert book["raw"]["close"] == 3808.0
    assert book["csv"]["close"] == 3808.0
    assert book["clean_prev"]["ts"].startswith("2026-09-22 10:45")
    assert book["clean_next"]["ts"].startswith("2026-09-22 10:55")
    text = format_kline_inspect(book)
    assert "正好是这一根" in text
    assert "3805" in text
    assert "同花顺图" in text


def test_inspect_kline_falls_back_to_nearest_same_day(tmp_path: Path) -> None:
    """10:51 没有根时，落到当天最接近的 10:50。"""
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    raw = RawBarStore(kline_dir / "raw.sqlite")
    clean = BarStore(kline_dir / "market.sqlite")
    bars = _frame([("2026-09-22 10:50:00", 3808.0)])
    _put_source(raw, clean, "akshare", "fu2701", bars)
    book = inspect_kline(
        tmp_path,
        source="新浪",
        symbol="fu2701",
        period="5",
        date_text="2026-09-22",
        time_text="10:51",
    )
    assert book["exact"] is False
    assert book["clean"]["ts"].startswith("2026-09-22 10:50")
    assert "最接近" in format_kline_inspect(book)
