"""盘中止损：用还在走的那根K线判触及，不等收线。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from src.scheduler.runner import _intraday_stop_decision
from src.strategy.types import PositionState, StrategyAction


def _forming(last: str, high: float, low: float, close: float) -> pd.DataFrame:
    """一根右标签K线；high/low 就是盘中已经走出来的最高最低。"""
    return pd.DataFrame(
        {"open": [close], "high": [high], "low": [low], "close": [close], "volume": [10.0]},
        index=[pd.Timestamp(last)],
    )


def _pos(side: str, stop: float, *, opened_at: str = "2026-11-25 23:31:00") -> PositionState:
    return PositionState(
        symbol="ag2702",
        side=side,
        volume=1,
        entry_price=8937.0,
        initial_stop=stop,
        trail_stop=stop,
        opened_at=opened_at,
    )


def _leg(mode: str = "touch") -> SimpleNamespace:
    return SimpleNamespace(stop_mode=mode, label="多因子CTA", engine="multi_factor")


def test_short_touch_inside_forming_bar_triggers_close() -> None:
    """空单：这根还在走，最高价已经越过止损 9050.29 → 立刻平，不等 23:40 收线。"""
    decision = _intraday_stop_decision(
        _leg(),
        "ag2702",
        _pos("short", 9050.29),
        _forming("2026-11-25 23:40", 9075.0, 9013.0, 9024.0),
        period="5",
    )
    assert decision is not None
    assert decision.action == StrategyAction.CLOSE
    assert decision.stop_price == 9050.29
    assert decision.meta["intraday_stop"] is True
    assert decision.meta["bar_id"] == "2026-11-25 23:40:00"


def test_short_not_touched_keeps_holding() -> None:
    """最高价没到止损位，不动。"""
    assert (
        _intraday_stop_decision(
            _leg(),
            "ag2702",
            _pos("short", 9050.29),
            _forming("2026-11-25 23:40", 9040.0, 9013.0, 9024.0),
            period="5",
        )
        is None
    )


def test_close_mode_still_waits_for_bar_close() -> None:
    """收盘价确认模式不走盘中判定，仍由收线那根决定。"""
    assert (
        _intraday_stop_decision(
            _leg("close"),
            "ag2702",
            _pos("short", 9050.29),
            _forming("2026-11-25 23:40", 9075.0, 9013.0, 9024.0),
            period="5",
        )
        is None
    )


def test_entry_bar_itself_is_skipped() -> None:
    """开仓那根含开仓前的影线：23:36 进场，23:35–23:40 这根的影线不能拿来止损。"""
    assert (
        _intraday_stop_decision(
            _leg(),
            "ag2702",
            _pos("short", 9050.29, opened_at="2026-11-25 23:36:12"),
            _forming("2026-11-25 23:40", 9075.0, 9013.0, 9024.0),
            period="5",
        )
        is None
    )


def test_entry_just_after_bar_open_still_checks_this_bar() -> None:
    """收线后下一轮扫描才成交，开仓只比这根开盘晚几秒：这根照判，和回测成交根一致。"""
    decision = _intraday_stop_decision(
        _leg(),
        "ag2702",
        _pos("short", 9050.29, opened_at="2026-11-25 23:40:02"),
        _forming("2026-11-25 23:45", 9075.0, 9013.0, 9024.0),
        period="5",
    )
    assert decision is not None and decision.action == StrategyAction.CLOSE


def test_long_uses_low_and_ignores_high_side() -> None:
    """多单只看最低价；跌破止损才平。"""
    hit = _intraday_stop_decision(
        _leg(),
        "ag2702",
        _pos("long", 9000.0),
        _forming("2026-11-25 23:40", 9010.0, 8990.0, 8995.0),
        period="5",
    )
    assert hit is not None and hit.action == StrategyAction.CLOSE
    assert (
        _intraday_stop_decision(
            _leg(),
            "ag2702",
            _pos("long", 9000.0),
            _forming("2026-11-25 23:40", 9010.0, 9002.0, 9005.0),
            period="5",
        )
        is None
    )


def test_no_recorded_stop_does_nothing() -> None:
    """柜台认领/账本重建的仓没有止损价时，不凭空按现价平仓。"""
    pos = _pos("short", 0.0)
    pos.initial_stop = 0.0
    pos.trail_stop = 0.0
    assert (
        _intraday_stop_decision(
            _leg(),
            "ag2702",
            pos,
            _forming("2026-11-25 23:40", 9075.0, 9013.0, 9024.0),
            period="5",
        )
        is None
    )


def test_daily_period_has_no_intraday_judgement() -> None:
    """日线没有「盘中那根」的概念，返回 None。"""
    assert (
        _intraday_stop_decision(
            _leg(),
            "ag2702",
            _pos("short", 9050.29),
            _forming("2026-11-25", 9075.0, 9013.0, 9024.0),
            period="D",
        )
        is None
    )
