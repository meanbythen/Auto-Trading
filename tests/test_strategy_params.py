"""策略参数默认值（单一事实源）与配置键名校验。"""

from pathlib import Path
from typing import Any

from src.strategy.daily_body import DailyBodyStrategy
from src.strategy.factory import sanitize_strategy_config
from src.strategy.params import ENGINE_SECTION_DEFAULTS, StrategyParams, section_default
from src.utils.config_loader import diff_config_keys, find_duplicate_keys

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"


def _cfg(**strategy: Any) -> dict[str, Any]:
    return {
        "_project_root": ".",
        "strategy": strategy,
        "trade": {"executor": "sim", "volume": 1, "symbol": "bu"},
        "schedule": {"trading_sessions": [["09:00", "11:30"]]},
        "dry_run": True,
    }


def test_apply_defaults_fills_empty_section() -> None:
    st: dict[str, Any] = {}
    StrategyParams().apply_defaults(st)
    assert st == {
        "engine": "daily_body",
        "stop_mode": "touch",
        "entry_mode": "close",
        "generic_stop": {"kind": "off", "lookback": 20},
    }


def test_apply_defaults_keeps_user_values() -> None:
    st: dict[str, Any] = {
        "stop_mode": "close",
        "generic_stop": {"kind": "sar", "lookback": 10},
    }
    StrategyParams().apply_defaults(st)
    assert st["stop_mode"] == "close"
    assert st["entry_mode"] == "close"
    assert st["generic_stop"] == {"kind": "sar", "lookback": 10}


def test_sanitize_keeps_user_numbers_and_fills_rest() -> None:
    cfg = _cfg(engine="daily_body", daily_body={"eth_minutes": 45, "skip_gap_atr_mult": 0})
    sanitize_strategy_config(cfg)
    body = cfg["strategy"]["daily_body"]
    assert body["eth_minutes"] == 45
    assert body["skip_gap_atr_mult"] == 0
    assert body["max_box_atr_mult"] == ENGINE_SECTION_DEFAULTS["daily_body"]["max_box_atr_mult"]


def test_daily_body_reads_shared_section_defaults() -> None:
    strategy = DailyBodyStrategy(_cfg(engine="daily_body"))
    assert strategy.skip_gap_atr_mult == section_default("daily_body", "skip_gap_atr_mult", 0.0)
    assert strategy.max_box_atr_mult == section_default("daily_body", "max_box_atr_mult", 0.0)
    assert strategy.max_chase_atr_mult == section_default("daily_body", "max_chase_atr_mult", 0.0)


def test_section_default_falls_back_for_unknown_section() -> None:
    assert section_default("no_such_engine", "key", 7) == 7


def test_find_duplicate_keys_flags_same_level() -> None:
    text = """
strategy:
  engine: daily_body
  engine: float_hold
trade:
  ctp:
    broker_id: ""
"""
    assert find_duplicate_keys(text) == ["strategy.engine"]


def test_find_duplicate_keys_ignores_broken_yaml() -> None:
    assert find_duplicate_keys("a: [") == []


def test_example_config_has_no_duplicate_keys() -> None:
    assert find_duplicate_keys(EXAMPLE.read_text(encoding="utf-8")) == []


def test_diff_config_keys_skips_runtime_and_dynamic_sections() -> None:
    example = {
        "strategy": {"engine": "daily_body", "unknown": 1},
        "risk": {"contracts": {"bu": {"multiplier": 10}}},
    }
    cfg = {
        "_project_root": ".",
        "strategy": {"engine": "daily_body", "typo": 2},
        "risk": {"contracts": {"c": {"multiplier": 5}}},
    }
    unknown, missing = diff_config_keys(cfg, example)
    assert unknown == ["strategy.typo"]
    assert missing == ["strategy.unknown"]
