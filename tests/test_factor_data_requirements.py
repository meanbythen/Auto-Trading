"""因子挖掘数据门槛和红黄绿灯。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from src.data.bar_store import BarStore
from src.data.quote_check import storage_symbol
from src.research.data_requirements import (
    MODE_REQUIREMENTS,
    USABLE_DEDUCTION,
    evaluate_readiness,
    required_raw_bars,
)


def _frame(
    n: int,
    *,
    end: str | datetime | None = None,
    freq: str = "1D",
    oi: bool = True,
) -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(end or datetime.now().date()), periods=n, freq=freq)
    data = {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
    }
    if oi:
        data["oi"] = 1000.0
    return pd.DataFrame(data, index=index)


def _store(tmp_path: Path, symbol: str, period: str, frame: pd.DataFrame) -> BarStore:
    store = BarStore(tmp_path / "market.sqlite")
    store.upsert(storage_symbol(symbol, "tq"), period, frame)
    return store


def test_required_raw_bars_adds_deduction():
    assert required_raw_bars("standard") == MODE_REQUIREMENTS["standard"].min_bars + USABLE_DEDUCTION


def test_standard_green_with_two_contracts(tmp_path: Path):
    store = _store(tmp_path, "FG2605", "D", _frame(1084))
    store.upsert(storage_symbol("FG2609", "tq"), "D", _frame(1084))
    report = evaluate_readiness(
        store, ["FG2605", "FG2609"], ["D"], mode="standard", require_oi=True
    )
    assert report["overall"] == "green"
    assert report["lights"]["kline"]["status"] == "green"
    assert report["lights"]["oi"]["status"] == "green"
    assert report["lights"]["cross"]["status"] == "green"
    assert report["lights"]["fresh"]["status"] == "green"


def test_one_bar_short_is_red(tmp_path: Path):
    store = _store(tmp_path, "FG2605", "D", _frame(1083))
    store.upsert(storage_symbol("FG2609", "tq"), "D", _frame(1084))
    report = evaluate_readiness(store, ["FG2605", "FG2609"], ["D"], mode="standard")
    assert report["overall"] == "red"
    assert report["lights"]["kline"]["status"] == "red"
    assert any("999" in item for item in report["missing"])


def test_one_contract_is_red_on_cross(tmp_path: Path):
    store = _store(tmp_path, "FG2605", "D", _frame(1084))
    report = evaluate_readiness(store, ["FG2605"], ["D"], mode="standard")
    assert report["lights"]["kline"]["status"] == "green"
    assert report["lights"]["cross"]["status"] == "red"
    assert report["overall"] == "red"


def test_missing_oi_is_yellow_only(tmp_path: Path):
    store = _store(tmp_path, "FG2605", "D", _frame(1084, oi=False))
    store.upsert(storage_symbol("FG2609", "tq"), "D", _frame(1084, oi=False))
    report = evaluate_readiness(store, ["FG2605", "FG2609"], ["D"], mode="standard")
    assert report["lights"]["oi"]["status"] == "yellow"
    assert report["overall"] == "yellow"


def test_oi_can_be_turned_off(tmp_path: Path):
    store = _store(tmp_path, "FG2605", "D", _frame(1084, oi=False))
    store.upsert(storage_symbol("FG2609", "tq"), "D", _frame(1084, oi=False))
    report = evaluate_readiness(
        store, ["FG2605", "FG2609"], ["D"], mode="standard", require_oi=False
    )
    assert report["lights"]["oi"]["status"] == "off"
    assert report["overall"] == "green"


def test_short_span_is_red(tmp_path: Path):
    # 根数够，但 1084 根只铺了 22 天，铺不出 60 天跨度。
    store = _store(tmp_path, "FG2605", "5", _frame(1084, freq="30min"))
    store.upsert(storage_symbol("FG2609", "tq"), "5", _frame(1084, freq="30min"))
    report = evaluate_readiness(store, ["FG2605", "FG2609"], ["5"], mode="standard")
    assert report["lights"]["kline"]["status"] == "red"
    assert "跨度" in report["lights"]["kline"]["detail"]


def test_daily_freshness_allows_three_calendar_days(tmp_path: Path):
    old = pd.Timestamp(datetime.now().date()) - pd.Timedelta(days=4)
    store = _store(tmp_path, "FG2605", "D", _frame(1084, end=old))
    store.upsert(storage_symbol("FG2609", "tq"), "D", _frame(1084, end=old))
    report = evaluate_readiness(store, ["FG2605", "FG2609"], ["D"], mode="standard")
    assert report["lights"]["fresh"]["status"] == "red"


def test_no_symbols_is_red(tmp_path: Path):
    store = BarStore(tmp_path / "market.sqlite")
    report = evaluate_readiness(store, [], ["D"], mode="standard")
    assert report["overall"] == "red"
    assert report["lights"]["kline"]["status"] == "red"
