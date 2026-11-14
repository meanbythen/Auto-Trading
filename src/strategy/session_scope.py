"""
交易时段范围（配置页的公共设置）。

功能：选出「全时段 / 只做日盘 / 只做夜盘 / 夜日连续」，决定这一段能不能开新仓。
原理：日盘最晚 15:15 收，夜盘最早 21:00 开，中间没有交易，
     所以 18:00 以后到次日 08:00 以前都算夜盘，其余算日盘。
     全时段 / 只做日盘 / 只做夜盘：已经拿着的仓不受这个限制，仍按各自的止损和收盘清仓走。
     夜日连续：留给隔夜跳空小的品种。日盘夜盘都能开新仓；日内实体还会把夜盘+日盘当成同一段，
     夜盘不开收盘清仓，拿到日盘 14:55。跳空大时最大回撤会明显变大。其它策略清仓钟点不变。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any

SCOPE_ALL = "all"
SCOPE_DAY = "day"
SCOPE_NIGHT = "night"
SCOPE_LINKED = "linked"

# 配置里存代码，界面上显示中文。
SCOPE_LABELS = {
    SCOPE_ALL: "全时段",
    SCOPE_DAY: "只做日盘",
    SCOPE_NIGHT: "只做夜盘",
    SCOPE_LINKED: "夜日连续（跳空小）",
}
_LABEL_TO_SCOPE = {label: code for code, label in SCOPE_LABELS.items()}
# 用户口头说法也认，避免配置里写成「夜盘+日盘连续」读成全时段。
_LABEL_TO_SCOPE["夜日连续"] = SCOPE_LINKED
_LABEL_TO_SCOPE["夜盘+日盘连续"] = SCOPE_LINKED
_LABEL_TO_SCOPE["夜盘+日盘连续时段"] = SCOPE_LINKED
_LABEL_TO_SCOPE["night_day"] = SCOPE_LINKED

_NIGHT_FROM = dtime(18, 0)
_NIGHT_UNTIL = dtime(8, 0)


def normalize_scope(value: Any) -> str:
    """把配置或界面上的值收成 all/day/night/linked。认不出来就当全时段。"""
    text = str(value or "").strip()
    if text in SCOPE_LABELS:
        return text
    if text in _LABEL_TO_SCOPE:
        return _LABEL_TO_SCOPE[text]
    low = text.lower()
    if low in _LABEL_TO_SCOPE:
        return _LABEL_TO_SCOPE[low]
    return low if low in SCOPE_LABELS else SCOPE_ALL


def scope_from_cfg(cfg: dict[str, Any] | None) -> str:
    """读公共设置 schedule.session_scope，缺省是全时段。"""
    schedule = (cfg or {}).get("schedule") or {}
    return normalize_scope(schedule.get("session_scope"))


def scope_code(label: Any) -> str:
    """界面上的中文标签换回配置代码。"""
    return normalize_scope(label)


def scope_label(scope: Any) -> str:
    return SCOPE_LABELS[normalize_scope(scope)]


def session_kind(moment: datetime | dtime) -> str:
    """这一刻算日盘还是夜盘。"""
    clock = moment if isinstance(moment, dtime) else moment.time()
    if clock >= _NIGHT_FROM or clock < _NIGHT_UNTIL:
        return SCOPE_NIGHT
    return SCOPE_DAY


def scope_allows(scope: Any, moment: datetime | dtime) -> bool:
    """这一段能不能开新仓。夜日连续和全时段一样，日盘夜盘都允许开。"""
    code = normalize_scope(scope)
    if code in {SCOPE_ALL, SCOPE_LINKED}:
        return True
    return session_kind(moment) == code


def scope_block_reason(scope: Any) -> str:
    """被时段范围拦下时给人看的原因。"""
    return f"时段范围设成「{scope_label(scope)}」，这一段不开新仓"
