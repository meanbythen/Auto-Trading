"""参数版本的默认名字和改名。"""

from datetime import datetime

import pandas as pd

from src.backtest.param_excel import export_append_excel
from src.backtest.param_history import (
    FACTORY_DEFAULT_ID,
    add_version,
    delete_version,
    get_version,
    list_version_labels,
    rename_version,
    version_display_name,
)


def _cfg(symbol: str = "bu2612") -> dict:
    return {
        "strategy": {
            "engine": "daily_body",
            "symbols": [symbol],
            "timeframe": "5",
            "daily_body": {"eth_minutes": 20},
        },
        "trade": {"symbol": symbol},
    }


def test_new_version_is_named_contract_and_time(tmp_path, monkeypatch) -> None:
    """新保存的版本，默认名字是「品种合约-策略名称-写入时间」。"""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return cls(2026, 10, 16, 22, 56, 3)

    monkeypatch.setattr("src.backtest.param_history.datetime", Frozen)
    entry = add_version(tmp_path, _cfg("bu2612"), note="写入优化参数", source="optimize")
    assert entry["name"] == "bu2612-日内实体-2026-10-16 22:56:03"
    labels = dict(list_version_labels(tmp_path))
    assert labels["bu2612-日内实体-2026-10-16 22:56:03"] == entry["id"]


def test_same_second_gets_a_different_name(tmp_path, monkeypatch) -> None:
    """同一秒连存两条时，第二条名字错开，下拉里不会变成同一项。"""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return cls(2026, 10, 16, 22, 56, 3)

    monkeypatch.setattr("src.backtest.param_history.datetime", Frozen)
    first = add_version(tmp_path, _cfg(), source="backup")
    second = add_version(tmp_path, _cfg(), source="optimize")
    assert first["name"] != second["name"]
    assert first["id"] != second["id"]
    assert second["name"].startswith("bu2612-日内实体-2026-10-16 22:56:03")


def test_history_list_filters_by_strategy(tmp_path) -> None:
    """下拉只列出选中的那一套。另一套的版本还在文件里，只是不混在这个列表里。"""
    body = add_version(tmp_path, _cfg("bu2612"), source="optimize", engine="daily_body")
    trend = add_version(
        tmp_path,
        {
            "strategy": {"engine": "trend_follow", "symbols": ["SA2701"], "timeframe": "D"},
            "trade": {"symbol": "SA2701"},
        },
        source="optimize",
        engine="trend_follow",
        symbol="SA2701",
    )
    labels = dict(list_version_labels(tmp_path, "trend_follow"))
    assert trend["id"] in labels.values()
    assert body["id"] not in labels.values()
    assert any("只重置趋势跟随" in text for text in labels)


def test_old_record_without_name_uses_contract_and_time(tmp_path) -> None:
    """以前存下、还没有名字的记录，打开时按合约、策略和时间显示。

    历史快照里仍可能写着已移除的引擎代号；程序把它当普通字符串显示，不再翻译成旧中文名。
    """
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "param_history.json").write_text(
        """
        {"versions": [{
          "id": "20261016_160957",
          "time": "2026-10-16T16:09:57",
          "source": "optimize",
          "engine": "liquidity_exhaustion",
          "symbol": "bu2612",
          "snapshot": {"engine": "liquidity_exhaustion", "symbols": ["bu2612"]}
        }]}
        """,
        encoding="utf-8",
    )
    entry = get_version(tmp_path, "20261016_160957")
    assert entry is not None
    assert version_display_name(entry) == "bu2612-liquidity_exhaustion-2026-10-16 16:09:57"


def test_rename_keeps_id_and_rejects_bad_names(tmp_path) -> None:
    """改名只改显示名字。空名字、重名、出厂默认都会被拒绝。"""
    entry = add_version(tmp_path, _cfg("c2611"), source="optimize")
    renamed = rename_version(tmp_path, entry["id"], "  玉米夜盘  ")
    assert renamed["id"] == entry["id"]
    assert renamed["name"] == "玉米夜盘"
    assert get_version(tmp_path, entry["id"])["name"] == "玉米夜盘"

    other = add_version(tmp_path, _cfg("bu2612"), source="optimize")
    try:
        rename_version(tmp_path, other["id"], "玉米夜盘")
        raised = False
    except ValueError as exc:
        raised = "已经有一个版本" in str(exc)
    assert raised

    try:
        rename_version(tmp_path, other["id"], "   ")
        empty_rejected = False
    except ValueError:
        empty_rejected = True
    assert empty_rejected

    try:
        rename_version(tmp_path, FACTORY_DEFAULT_ID, "别的名字")
        factory_rejected = False
    except ValueError as exc:
        factory_rejected = "出厂默认" in str(exc)
    assert factory_rejected


def test_delete_version_keeps_factory_and_other_rows(tmp_path) -> None:
    """删掉一条历史时，出厂默认和另一条还在。"""
    first = add_version(tmp_path, _cfg("bu2612"), source="optimize", engine="daily_body")
    second = add_version(tmp_path, _cfg("SA2701"), source="optimize", engine="trend_follow")
    removed = delete_version(tmp_path, first["id"])
    assert removed
    assert get_version(tmp_path, first["id"]) is None
    assert get_version(tmp_path, second["id"]) is not None
    labels = [vid for _label, vid in list_version_labels(tmp_path, "daily_body")]
    assert FACTORY_DEFAULT_ID in labels
    assert first["id"] not in labels
    try:
        delete_version(tmp_path, FACTORY_DEFAULT_ID)
        rejected = False
    except ValueError as exc:
        rejected = "出厂默认" in str(exc)
    assert rejected


def test_export_updates_renamed_row_without_adding_another(tmp_path) -> None:
    """改名后再导出，Excel 里同一条的名称会更新，不会多出一行。"""
    entry = add_version(tmp_path, _cfg("bu2612"), source="optimize")
    path = tmp_path / "Auto-Trading导出.xlsx"
    export_append_excel(tmp_path, path)
    rename_version(tmp_path, entry["id"], "bu2612-影线3倍")
    _path, added, total = export_append_excel(tmp_path, path)
    book = pd.read_excel(path, sheet_name="优化参数")
    row = book[book["版本ID"].astype(str) == entry["id"]].iloc[0]
    assert row["名称"] == "bu2612-影线3倍"
    assert added == 0
    assert total == len(book)
