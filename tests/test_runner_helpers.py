"""scheduler.runner 模块级纯函数的单元测试。

这些函数只吃参数、不依赖 TradingBot 实例，单独测即可覆盖行情快照、ATR、
回溯补开仓的「现在」和最近告警读取等边角逻辑。
"""

from __future__ import annotations

from datetime import datetime, time
from types import SimpleNamespace

import pandas as pd
import pytest

from src.scheduler import runner


def _frame(close: float = 100.0, rows: int = 4) -> pd.DataFrame:
    index = pd.date_range("2026-11-24 09:30", periods=rows, freq="5min")
    return pd.DataFrame(
        {
            "high": [close + i + 1 for i in range(rows)],
            "low": [close - i - 1 for i in range(rows)],
            "close": [close + i for i in range(rows)],
        },
        index=index,
    )


def test_safe_last_alert_returns_value_and_swallows_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ops.alerts.last_alert", lambda *a, **k: {"kind": "x"}
    )
    assert runner._safe_last_alert(100.0) == {"kind": "x"}

    def boom(*args, **kwargs):
        raise RuntimeError("events file broken")

    monkeypatch.setattr("src.ops.alerts.last_alert", boom)
    assert runner._safe_last_alert() is None


def test_parse_hhmm_returns_time_object() -> None:
    assert runner._parse_hhmm("09:30") == time(9, 30)
    assert runner._parse_hhmm("21:00") == time(21, 0)


def test_in_trading_session_delegates_to_session_clock() -> None:
    sessions = [["09:00", "15:00"]]
    assert runner.in_trading_session(sessions, datetime(2026, 11, 24, 10, 0)) is True
    assert runner.in_trading_session(sessions, datetime(2026, 11, 24, 16, 0)) is False


def test_bar_now_from_prefix_handles_empty_bad_and_valid_index() -> None:
    assert runner._bar_now_from_prefix(None) is None
    assert runner._bar_now_from_prefix(pd.DataFrame()) is None
    assert runner._bar_now_from_prefix(_frame()) == datetime(2026, 11, 24, 9, 45)

    bad = _frame()
    bad.index = [object()] * len(bad)
    assert runner._bar_now_from_prefix(bad) is None


def test_last_close_reads_last_bar_and_falls_back_to_zero() -> None:
    assert runner._last_close(None) == 0.0
    assert runner._last_close(pd.DataFrame()) == 0.0
    assert runner._last_close(_frame(close=88.0)) == 91.0
    assert runner._last_close(pd.DataFrame({"price": [1.0]})) == 0.0


def test_latest_atr_uses_strategy_period_and_returns_zero_on_bad_bars() -> None:
    strategy = SimpleNamespace(atr_period=3)
    assert runner._latest_atr(_frame(), strategy) > 0

    assert runner._latest_atr(pd.DataFrame({"close": [1.0]}), strategy) == 0.0
    assert runner._latest_atr(_frame(), SimpleNamespace(atr_period="bad")) == 0.0


def test_eval_now_uses_bar_time_only_for_catch_up() -> None:
    frame = _frame()
    assert runner._eval_now(frame, frame, catch_up=True) == datetime(2026, 11, 24, 9, 45)

    now = runner._eval_now(frame, frame, catch_up=False)
    assert now is not None
    assert abs((datetime.now() - now).total_seconds()) < 5


@pytest.mark.parametrize("grace", [90.0, 5.0])
def test_in_trading_session_forwards_grace(monkeypatch, grace: float) -> None:
    calls: list[float] = []

    def fake(sessions, now=None, *, grace_sec=90.0):
        calls.append(grace_sec)
        return False

    monkeypatch.setattr("src.utils.session_clock.in_trading_sessions", fake)
    assert runner.in_trading_session([["09:00", "15:00"]], grace_sec=grace) is False
    assert calls == [grace]
