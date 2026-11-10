"""配置页整页存档：名字、开关、密码不落盘。"""

from datetime import datetime
from pathlib import Path

from src.ui.config_presets import (
    add_preset,
    apply_preset_to_config,
    clean_config,
    delete_preset,
    get_preset,
    preset_name,
    switch_summary,
)


def test_preset_name_uses_symbol_strategy_and_time() -> None:
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "symbols": ["bu2612", "SA2701"],
        }
    }
    name = preset_name(cfg, datetime(2026, 10, 21, 12, 58, 3))
    assert name == "bu2612-日内实体-2026-10-21 12:58:03"


def test_clean_config_drops_passwords() -> None:
    cleaned = clean_config(
        {
            "_project_root": "D:/secret",
            "dry_run": False,
            "account": {"user_id": "20318052", "resolved_password": "should-not-save", "password": "nope"},
            "trade": {"executor": "gui", "ctp": {"broker_id": "9999", "resolved_password": "ctp-secret"}},
            "strategy": {"engine": "daily_body", "daily_body": {"eth_minutes": 20}},
        }
    )
    assert "_project_root" not in cleaned
    assert "resolved_password" not in cleaned["account"]
    assert "password" not in cleaned["account"]
    assert cleaned["account"]["user_id"] == "20318052"
    assert "resolved_password" not in cleaned["trade"]["ctp"]
    assert cleaned["trade"]["ctp"]["broker_id"] == "9999"
    assert cleaned["strategy"]["daily_body"]["eth_minutes"] == 20


def test_add_preset_can_use_custom_name(tmp_path: Path) -> None:
    """切实盘前可以留下认得出的存档名。"""
    saved = add_preset(
        tmp_path,
        {"strategy": {"engine": "daily_body", "symbols": ["fu2701"]}, "dry_run": True},
        datetime(2026, 11, 8, 23, 0, 0),
        name="切实盘前-fu2701-日内实体-2026-11-08 23:00:00",
    )
    assert saved["name"].startswith("切实盘前-")
    assert get_preset(tmp_path, saved["id"])["name"] == saved["name"]


def test_add_and_apply_keeps_switches_and_drops_password(tmp_path: Path) -> None:
    saved = add_preset(
        tmp_path,
        {
            "dry_run": False,
            "account": {"user_id": "20318052", "resolved_password": "hidden", "skip_auto_login": False},
            "strategy": {
                "engine": "daily_body",
                "symbols": ["bu2612"],
                "timeframe": "5",
                "flatten_at_close": True,
                "daily_body": {"eth_minutes": 30},
            },
            "trade": {"executor": "gui", "volume": 1},
            "risk": {"max_daily_loss": 1000},
        },
        datetime(2026, 10, 21, 12, 58, 3),
    )
    assert saved["name"] == "bu2612-日内实体-2026-10-21 12:58:03"
    assert "resolved_password" not in saved["config"]["account"]
    current = {
        "dry_run": True,
        "account": {"user_id": "old", "resolved_password": "keep-in-memory"},
        "strategy": {
            "engine": "trend_follow",
            "symbols": ["SA2701"],
            "daily_body": {"eth_minutes": 20},
        },
        "trade": {"executor": "sim"},
    }
    applied = apply_preset_to_config(current, saved["config"])
    assert applied["dry_run"] is False
    assert applied["strategy"]["engine"] == "daily_body"
    assert applied["strategy"]["symbols"] == ["bu2612"]
    assert applied["strategy"]["daily_body"]["eth_minutes"] == 30
    assert applied["trade"]["executor"] == "gui"
    assert applied["account"]["user_id"] == "20318052"
    # 内存里原来的密码留下来，写配置文件时仍会丢掉，不会进存档。
    assert applied["account"]["resolved_password"] == "keep-in-memory"
    text = switch_summary(saved["config"])
    assert "收盘清仓：开" in text
    assert "模拟模式：关" in text
    assert "日内实体" in text


def test_delete_preset_removes_only_the_selected_one(tmp_path: Path) -> None:
    """删掉一份存档时，另一份还在，当前配置文件不受这个函数影响。"""
    first = add_preset(
        tmp_path,
        {"strategy": {"engine": "daily_body", "symbols": ["bu2612"]}, "dry_run": False},
        datetime(2026, 10, 21, 13, 3, 52),
    )
    second = add_preset(
        tmp_path,
        {"strategy": {"engine": "daily_body", "symbols": ["bu2612"]}, "dry_run": True},
        datetime(2026, 10, 21, 13, 54, 33),
    )
    removed = delete_preset(tmp_path, second["id"])
    assert removed == second["name"]
    assert get_preset(tmp_path, second["id"]) is None
    assert get_preset(tmp_path, first["id"]) is not None
    try:
        delete_preset(tmp_path, second["id"])
        missing = False
    except ValueError:
        missing = True
    assert missing
