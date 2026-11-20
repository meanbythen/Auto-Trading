"""策略档案：每个正式引擎都有规则卡，别名能换算，抬头写清隔夜与清仓。"""

from pathlib import Path

from src.strategy.factory import ENGINE_ALIASES, ENGINE_LABELS
from src.strategy.profile import PROFILES, header_line, profile_of, profile_text


def test_every_formal_engine_has_a_rule_card() -> None:
    missing = [code for code in ENGINE_LABELS if code not in PROFILES]
    assert missing == []


def test_aliases_resolve_to_the_same_card() -> None:
    assert profile_of("boll").code == "bollinger"
    assert profile_of("macd_swing").code == "ema_swing"
    assert profile_of("ma_cross").code == "dual_ma"
    for alias, code in ENGINE_ALIASES.items():
        assert profile_of(alias) is PROFILES[code]


def test_header_line_states_overnight_and_close() -> None:
    text = header_line("daily_body")
    assert "日内实体" in text
    assert "默认不隔夜" in text
    assert "清仓" in text

    overnight = header_line("trend_follow", {"strategy": {"flatten_at_close": True}})
    assert "默认可隔夜" in overnight
    assert "跟随收盘清仓" in overnight

    held = header_line("daily_body", {"strategy": {"flatten_at_close": False}})
    assert "可留过收盘" in held


def test_unknown_engine_is_not_pretended() -> None:
    assert profile_of("no_such_engine") is None
    assert "没有登记" in profile_text("no_such_engine")
    assert header_line("no_such_engine") == ""


def test_doc_lists_every_engine() -> None:
    doc = Path("docs/系统评估升级/策略档案.md").read_text(encoding="utf-8")
    missing = [code for code in PROFILES if f"`{code}`" not in doc]
    assert missing == []
    assert "退役线" in doc
