"""研究数据集：具体合约快照、覆盖缺口、原始校验和换月审计。"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from src.backtest.experiment import research_dataset_reference
from src.data.bar_store import BarStore, RawBarStore
from src.data.coverage import expected_bar_times
from src.data.research_dataset import build_research_dataset


def _write_table(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        rows,
        columns=["品种", "中文名", "交易所", "主力合约", "交割月份", "开始日期", "结束日期", "交易日数", "平均持仓量（手）"],
    )
    frame.to_excel(path, sheet_name="主力时段", index=False, startrow=2)


def _bars(stamps) -> pd.DataFrame:
    index = pd.DatetimeIndex(stamps)
    return pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [102.0] * len(index),
            "low": [99.0] * len(index),
            "close": [101.0] * len(index),
            "volume": [10.0] * len(index),
            "oi": [500.0] * len(index),
        },
        index=index,
    )


def _put(root, symbol: str, period: str, source: str, bars: pd.DataFrame) -> None:
    stored = symbol + {"akshare": "", "ths": "@ths"}[source]
    folder = root / "data" / "klines"
    BarStore(folder / "market.sqlite").upsert(stored, period, bars)
    RawBarStore(folder / "raw.sqlite").upsert(stored, period, source, bars)


def test_complete_half_year_dataset_has_immutable_raw_and_clean_checksums(tmp_path) -> None:
    table = tmp_path / "config" / "主力合约表.xlsx"
    _write_table(table, [["fu", "燃油", "SHFE", "fu2701", "2027-01", "2026-05-16", "2026-11-16", 130, 1000]])
    stamps = expected_bar_times(datetime(2026, 5, 16), datetime(2026, 11, 16, 23, 59), "D")
    bars = _bars(stamps)
    _put(tmp_path, "fu2701", "D", "akshare", bars)
    clean = BarStore(tmp_path / "data" / "klines" / "market.sqlite")
    clean.record_ingest(
        source="akshare",
        symbol="fu2701",
        period="D",
        read_from=str(stamps[0]),
        read_to=str(stamps[-1]),
        read_n=len(bars),
    )

    manifest = build_research_dataset(
        tmp_path,
        product="fu",
        period="D",
        start_date="2026-05-16",
        end_date="2026-11-16",
    )

    assert manifest["coverage"]["missing_n"] == 0
    assert manifest["readiness"]["data_complete"] is True
    assert manifest["readiness"]["screening_ready"] is True
    assert manifest["readiness"]["validation_ready"] is False
    assert "main_table_point_in_time_unproven" in manifest["readiness"]["risk_flags"]
    segment = manifest["segments"][0]
    assert segment["clean"]["rows"] == len(bars)
    assert segment["raw"]["rows"] == len(bars)
    assert len(segment["clean"]["file_sha256"]) == 64
    assert len(segment["raw"]["file_sha256"]) == 64
    assert segment["downloaded"]["status"] == "recorded"
    assert segment["clean_version"] == "legacy_unversioned_current_store"
    folder = tmp_path / "data" / "research_datasets" / manifest["dataset_id"]
    saved = pd.read_csv(folder / segment["clean"]["path"])
    assert len(saved) == len(bars)
    assert json.loads((folder / "coverage.json").read_text(encoding="utf-8"))["missing_n"] == 0
    assert (tmp_path / "data" / "research_datasets" / "index.json").exists()

    ref = research_dataset_reference(
        {
            "_project_root": str(tmp_path),
            "backtest": {"research_dataset": manifest["dataset_id"]},
        }
    )
    assert ref["dataset_id"] == manifest["dataset_id"]
    assert ref["dataset_sha256"] == manifest["dataset_sha256"]
    assert ref["readiness"]["screening_ready"] is True


def test_missing_bar_is_reported_and_not_filled(tmp_path) -> None:
    table = tmp_path / "config" / "主力合约表.xlsx"
    _write_table(table, [["fu", "燃油", "SHFE", "fu2701", "2027-01", "2026-11-12", "2026-11-12", 1, 1000]])
    stamps = expected_bar_times(datetime(2026, 11, 12), datetime(2026, 11, 12, 23, 59), "5")
    bars = _bars(stamps.delete(1))
    _put(tmp_path, "fu2701", "5", "akshare", bars)

    manifest = build_research_dataset(
        tmp_path,
        product="fu",
        period="5",
        start_date="2026-11-12",
        end_date="2026-11-12",
    )

    segment = manifest["segments"][0]
    assert segment["coverage"]["missing_n"] == 1
    assert segment["coverage"]["missing_ranges"][0][:2] == [
        stamps[1].strftime("%Y-%m-%d %H:%M"),
        stamps[1].strftime("%Y-%m-%d %H:%M"),
    ]
    assert segment["clean"]["rows"] == len(stamps) - 1
    assert manifest["readiness"]["data_complete"] is False
    assert "clean_data_incomplete" in manifest["readiness"]["risk_flags"]


def test_rollover_audit_explains_two_contract_boundary_prices(tmp_path) -> None:
    table = tmp_path / "config" / "主力合约表.xlsx"
    _write_table(
        table,
        [
            ["fu", "燃油", "SHFE", "fu2612", "2026-12", "2026-11-02", "2026-11-04", 3, 1000],
            ["fu", "燃油", "SHFE", "fu2701", "2027-01", "2026-11-05", "2026-11-06", 2, 1100],
        ],
    )
    old = _bars(pd.DatetimeIndex(["2026-11-05 15:00"]))
    old.iloc[0, old.columns.get_loc("close")] = 100.0
    new = _bars(pd.DatetimeIndex(["2026-11-06 15:00"]))
    new.iloc[0, new.columns.get_loc("open")] = 110.0
    _put(tmp_path, "fu2612", "D", "akshare", old)
    _put(tmp_path, "fu2701", "D", "akshare", new)

    manifest = build_research_dataset(
        tmp_path,
        product="fu",
        period="D",
        start_date="2026-11-02",
        end_date="2026-11-06",
    )

    change = manifest["rollover_audit"]["transitions"][0]
    assert change["old_symbol"] == "fu2612"
    assert change["new_symbol"] == "fu2701"
    assert change["old_effective_end"] == "2026-11-05"
    assert change["new_effective_start"] == "2026-11-06"
    assert change["old_last_close"] == 100.0
    assert change["new_first_open"] == 110.0
    assert change["price_difference"] == 10.0
    assert "不合成主连" in change["interpretation"]
    assert manifest["rollover_audit"]["point_in_time_proven"] is False


def test_ths_dataset_uses_independent_source_snapshot(tmp_path) -> None:
    table = tmp_path / "config" / "主力合约表.xlsx"
    _write_table(table, [["fu", "燃油", "SHFE", "fu2701", "2027-01", "2026-11-12", "2026-11-12", 1, 1000]])
    bars = _bars(pd.DatetimeIndex(["2026-11-12 15:00"]))
    _put(tmp_path, "fu2701", "D", "akshare", bars)
    _put(tmp_path, "fu2701", "D", "ths", bars * 2)

    manifest = build_research_dataset(
        tmp_path,
        product="fu",
        period="D",
        source="ths",
        start_date="2026-11-12",
        end_date="2026-11-12",
    )

    segment = manifest["segments"][0]
    assert segment["stored_symbol"] == "fu2701@ths"
    assert manifest["source"] == "ths"
    assert manifest["usage"]["source_role"] == "cross_check_only"
    saved = pd.read_csv(tmp_path / "data" / "research_datasets" / manifest["dataset_id"] / segment["clean"]["path"])
    assert saved.iloc[0]["close"] == 202.0
