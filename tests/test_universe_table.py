"""品种与合约总表。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.risk.universe_table import (
    apply_universe_table_to_cfg,
    parse_universe_rows,
    read_universe_table,
    sync_ctp_contract_to_universe,
    write_universe_table,
)


def test_roundtrip_and_enabled(tmp_path: Path) -> None:
    path = tmp_path / "u.xlsx"
    write_universe_table(
        path,
        [
            {
                "symbol": "fu2701",
                "name": "燃油",
                "margin": 11232,
                "multiplier": 10,
                "commission": 2.63,
                "commission_close_today": 0.0,
                "enabled": True,
            },
            {"symbol": "c2701", "name": "玉米", "margin": 2220, "multiplier": 10, "enabled": True},
            {"symbol": "bu", "name": "沥青", "margin": 8276, "multiplier": 10, "enabled": False},
        ],
    )
    rows = read_universe_table(path)
    symbols, contracts = parse_universe_rows(rows)
    assert symbols == ["fu2701", "c2701"]
    assert "bu" not in [s.lower() for s in symbols]
    assert contracts["fu"]["margin"] == 11232
    assert float(contracts["fu"].get("margin_rate_pct") or 0) == 0
    assert contracts["fu"]["multiplier"] == 10
    assert contracts["fu"]["commission"] == 2.63
    assert contracts["fu"]["commission_close_today"] == 0.0
    assert contracts["c"]["margin"] == 2220
    assert contracts["c"].get("commission", 0) == 0
    assert "commission_close_today" not in contracts["c"]
    assert contracts["bu"]["margin"] == 8276


def test_same_product_fees_must_match(tmp_path: Path) -> None:
    path = tmp_path / "fee.xlsx"
    write_universe_table(
        path,
        [
            {
                "symbol": "fu2701",
                "name": "燃油",
                "margin": 1000,
                "multiplier": 10,
                "commission": 2.63,
                "enabled": True,
            },
            {
                "symbol": "fu2705",
                "name": "燃油",
                "margin": 1000,
                "multiplier": 10,
                "commission": 1.0,
                "enabled": True,
            },
        ],
    )
    with pytest.raises(ValueError, match="手续费"):
        read_universe_table(path)


def test_same_product_margin_must_match(tmp_path: Path) -> None:
    path = tmp_path / "bad.xlsx"
    write_universe_table(
        path,
        [
            {"symbol": "fu2701", "name": "燃油", "margin": 1000, "multiplier": 10, "enabled": True},
            {"symbol": "fu2705", "name": "燃油", "margin": 2000, "multiplier": 10, "enabled": True},
        ],
    )
    with pytest.raises(ValueError, match="fu"):
        read_universe_table(path)


def test_apply_to_cfg(tmp_path: Path) -> None:
    path = tmp_path / "u.xlsx"
    write_universe_table(
        path,
        [
            {"symbol": "jm2701", "name": "焦煤", "margin": 12690, "multiplier": 60, "enabled": True},
        ],
    )
    cfg: dict = {"_project_root": str(tmp_path), "risk": {}, "strategy": {"symbols": ["bu2612"]}}
    symbols, contracts = apply_universe_table_to_cfg(cfg, path=path, create_if_missing=False)
    assert symbols == ["jm2701"]
    assert cfg["strategy"]["symbols"] == ["jm2701"]
    assert contracts["jm"]["multiplier"] == 60
    # 几秒内再读一次应仍写进配置，但不再打开第二遍 Excel。
    cfg2: dict = {"_project_root": str(tmp_path), "risk": {}, "strategy": {"symbols": ["xx"]}}
    symbols2, _contracts2 = apply_universe_table_to_cfg(cfg2, path=path, create_if_missing=False)
    assert symbols2 == ["jm2701"]
    assert cfg2["strategy"]["symbols"] == ["jm2701"]


def test_symbol_sessions_roundtrip(tmp_path: Path) -> None:
    """品种表里的交易时段、清仓时刻会跟合约一起读出来。"""
    path = tmp_path / "sess.xlsx"
    write_universe_table(
        path,
        [
            {
                "symbol": "ag2702",
                "name": "白银",
                "margin": 1,
                "multiplier": 15,
                "enabled": True,
                "trading_sessions": "09:00-11:30,13:30-15:00,21:00-02:30",
                "flatten_times": "14:55,02:25",
            },
            {
                "symbol": "fu2701",
                "name": "燃油",
                "margin": 1,
                "multiplier": 10,
                "enabled": True,
                "trading_sessions": "09:00-11:30,13:30-15:00,21:00-23:00",
                "flatten_times": "14:55,22:55",
            },
        ],
    )
    cfg: dict = {"_project_root": str(tmp_path), "risk": {}, "strategy": {}}
    _symbols, contracts = apply_universe_table_to_cfg(cfg, path=path, create_if_missing=False)
    from src.utils.session_clock import in_trading_sessions, sessions_for_symbol

    now = __import__("datetime").datetime(2026, 11, 10, 0, 37)
    assert in_trading_sessions(sessions_for_symbol(cfg, "ag2702"), now) is True
    assert in_trading_sessions(sessions_for_symbol(cfg, "fu2701"), now) is False
    assert "02:30" in (contracts["ag"].get("trading_sessions") or "")
    assert "22:55" in (contracts["fu"].get("flatten_times") or "")


def test_margin_rate_roundtrip(tmp_path: Path) -> None:
    """新表用保证金比例%和每手数量；23 表示 23%，开仓时按价格现算。"""
    from src.risk.universe_table import parse_margin_rate_pct

    assert parse_margin_rate_pct(23) == 23
    assert parse_margin_rate_pct("23%") == 23
    assert parse_margin_rate_pct(0.23) == 23
    assert parse_margin_rate_pct(9240) == 0

    path = tmp_path / "rate.xlsx"
    write_universe_table(
        path,
        [
            {
                "symbol": "fu2701",
                "name": "燃油",
                "margin_rate_pct": 23,
                "multiplier": 10,
                "enabled": True,
            }
        ],
    )
    rows = read_universe_table(path)
    _symbols, contracts = parse_universe_rows(rows)
    assert contracts["fu"]["margin_rate_pct"] == 23
    assert contracts["fu"]["multiplier"] == 10
    assert float(contracts["fu"].get("margin") or 0) == 0


def test_sync_ctp_contract_updates_same_product(tmp_path: Path) -> None:
    """交易台写入只改数量、跳、保证金、手续费，不改启用和时段。"""
    path = tmp_path / "u.xlsx"
    write_universe_table(
        path,
        [
            {
                "symbol": "lh2701",
                "name": "生猪",
                "margin_rate_pct": 10,
                "multiplier": 10,
                "commission": 1,
                "enabled": True,
                "trading_sessions": "09:00-11:30,13:30-15:00,21:00-23:00",
                "flatten_times": "14:55,22:55",
            }
        ],
    )
    sync_ctp_contract_to_universe(
        path,
        {
            "symbol": "lh2701",
            "name": "生猪",
            "multiplier": 16,
            "price_tick": 5,
            "margin_rate_pct": 13,
            "commission": 29.92,
            "commission_close": 14.96,
            "commission_close_today": 0.0,
        },
    )
    rows = read_universe_table(path)
    _symbols, contracts = parse_universe_rows(rows)
    assert contracts["lh"]["multiplier"] == 16
    assert contracts["lh"]["price_tick"] == 5
    assert contracts["lh"]["margin_rate_pct"] == 13
    assert contracts["lh"]["commission"] == 29.92
    assert contracts["lh"]["commission_close"] == 14.96
    assert contracts["lh"]["commission_close_today"] == 0.0
    assert rows[0]["enabled"] is True
    assert "21:00" in (rows[0].get("trading_sessions") or "")


def test_read_old_header_file(tmp_path: Path) -> None:
    """旧表还是「每手保证金 / 乘数」时也能读，金额当兜底。"""
    import openpyxl

    path = tmp_path / "old.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["合约代码", "中文名", "每手保证金", "乘数", "启用"])
    sheet.append(["fu2701", "燃油", 9240, 10, "是"])
    book.save(path)
    rows = read_universe_table(path)
    _symbols, contracts = parse_universe_rows(rows)
    assert contracts["fu"]["margin"] == 9240
    assert contracts["fu"]["multiplier"] == 10
    assert float(contracts["fu"].get("margin_rate_pct") or 0) == 0
