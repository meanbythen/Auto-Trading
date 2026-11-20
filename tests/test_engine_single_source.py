"""口径与名单的单一来源测试。"""

from __future__ import annotations

from src.domain.contracts import DEFAULT_CONTRACTS, DEFAULT_MULTIPLIER
from src.screen.picker import MAIN_UNIVERSE
from src.strategy.factory import (
    REMOVED_ENGINES,
    _OBSOLETE_STRATEGY_SECTIONS,
    param_section,
)


def _universe_multiplier(code: str) -> float:
    for item in MAIN_UNIVERSE:
        if item[0] == code:
            return float(item[2])
    raise AssertionError(f"{code} 不在全品种名单里")


def test_default_multiplier_is_ten() -> None:
    assert DEFAULT_MULTIPLIER == 10.0


def test_default_contracts_match_main_universe() -> None:
    for key, info in DEFAULT_CONTRACTS.items():
        assert float(info["multiplier"]) == _universe_multiplier(f"{key}0")


def test_param_section_follows_engine_aliases() -> None:
    assert param_section("ma_cross") == "dual_ma"
    assert param_section("macd_swing") == "ema_swing"
    assert param_section("ST") == "supertrend"
    assert param_section("dual_fractal") == "dual_fractal"
    assert param_section("") == ""
    assert param_section(None) == ""


def test_obsolete_sections_cover_removed_engines() -> None:
    assert REMOVED_ENGINES <= _OBSOLETE_STRATEGY_SECTIONS
    assert "rotation_scan" in _OBSOLETE_STRATEGY_SECTIONS
