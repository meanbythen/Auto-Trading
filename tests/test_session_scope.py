"""
公共交易时段范围（日盘 / 夜盘 / 全时段）的测试。

它只决定能不能开新仓：18:00 以后到次日 08:00 以前算夜盘，其余算日盘。
"""

from datetime import datetime, time as dtime

from src.strategy.session_scope import (
    SCOPE_ALL,
    SCOPE_DAY,
    SCOPE_LINKED,
    SCOPE_NIGHT,
    normalize_scope,
    scope_allows,
    scope_block_reason,
    scope_code,
    scope_from_cfg,
    scope_label,
    session_kind,
)


def test_session_kind_splits_day_and_night() -> None:
    """日盘收盘最晚 15:15，夜盘最早 21:00 开，18:00 到次日 08:00 中间的都算夜盘。"""
    assert session_kind(dtime(9, 0)) == SCOPE_DAY
    assert session_kind(dtime(15, 15)) == SCOPE_DAY
    assert session_kind(dtime(17, 59)) == SCOPE_DAY
    assert session_kind(dtime(18, 0)) == SCOPE_NIGHT
    assert session_kind(dtime(22, 0)) == SCOPE_NIGHT
    assert session_kind(dtime(2, 30)) == SCOPE_NIGHT
    assert session_kind(dtime(7, 59)) == SCOPE_NIGHT
    assert session_kind(dtime(8, 0)) == SCOPE_DAY


def test_scope_allows_by_selected_session() -> None:
    morning = datetime(2026, 10, 23, 9, 45)
    night = datetime(2026, 10, 23, 22, 0)
    assert scope_allows(SCOPE_ALL, morning)
    assert scope_allows(SCOPE_ALL, night)
    assert scope_allows(SCOPE_DAY, morning)
    assert not scope_allows(SCOPE_DAY, night)
    assert scope_allows(SCOPE_NIGHT, night)
    assert not scope_allows(SCOPE_NIGHT, morning)
    assert scope_allows(SCOPE_LINKED, morning)
    assert scope_allows(SCOPE_LINKED, night)


def test_normalize_scope_accepts_labels_and_unknown_falls_back() -> None:
    """配置写代码、界面写中文都能读；认不出来就按全时段。"""
    assert normalize_scope("day") == SCOPE_DAY
    assert normalize_scope("只做日盘") == SCOPE_DAY
    assert normalize_scope("NIGHT") == SCOPE_NIGHT
    assert normalize_scope("夜日连续") == SCOPE_LINKED
    assert normalize_scope("夜日连续（跳空小）") == SCOPE_LINKED
    assert scope_label("linked") == "夜日连续（跳空小）"
    assert normalize_scope("夜盘+日盘连续时段") == SCOPE_LINKED
    assert normalize_scope("linked") == SCOPE_LINKED
    assert normalize_scope("") == SCOPE_ALL
    assert normalize_scope("随便") == SCOPE_ALL
    assert scope_code("只做夜盘") == SCOPE_NIGHT
    assert scope_label("") == "全时段"


def test_scope_from_cfg_reads_the_public_setting() -> None:
    """公共设置放在 schedule.session_scope，缺省是全时段。"""
    assert scope_from_cfg({"schedule": {"session_scope": "night"}}) == SCOPE_NIGHT
    assert scope_from_cfg({"schedule": {"session_scope": "只做日盘"}}) == SCOPE_DAY
    assert scope_from_cfg({}) == SCOPE_ALL
    assert scope_from_cfg(None) == SCOPE_ALL


def test_block_reason_mentions_the_selection() -> None:
    assert "只做日盘" in scope_block_reason(SCOPE_DAY)
    assert "只做夜盘" in scope_block_reason(SCOPE_NIGHT)
