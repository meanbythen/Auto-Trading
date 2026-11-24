"""收盘清仓定时与重试上限的单元测试（白盒，Mixin + Fake）。"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from src.scheduler import flatten_manager
from src.scheduler.flatten_manager import FlattenManagerMixin, in_trading_session


class _Strategy:
    def __init__(self, *, flatten_at_close=True, flatten_times=None, checker=None):
        self.flatten_at_close = flatten_at_close
        self.flatten_times = flatten_times or []
        self._checker = checker

    def is_flatten_time(self, when=None):
        if self._checker is not None:
            return self._checker(when)
        return True


class _FlattenBot(FlattenManagerMixin):
    def __init__(self, strategy, scan_symbols=("bu2612",)):
        self.strategy = strategy
        self.scan_symbols = list(scan_symbols)
        self.cfg = {}
        self.sessions = []
        self.holiday_calendar = None
        self.open_positions = set()
        self._flatten_attempts = {}
        self._flatten_try = {}
        self._flatten_sent = set()
        self._flatten_pending = False
        self.multi = False
        self.clocks = {}

    def _multi_symbol_enabled(self):
        return self.multi

    def _symbol_is_flat(self, symbol):
        return symbol not in self.open_positions

    def _symbol_flatten_clocks(self, symbol):
        return self.clocks.get(symbol, [])


def _bot(**kwargs):
    return _FlattenBot(_Strategy(**kwargs))


@pytest.mark.parametrize(
    ("in_window", "attempts", "expected"),
    [
        (True, 0, False),
        (True, 100, False),
        (False, 0, False),
        (False, 29, False),
        (False, 30, True),
        (False, 31, True),
    ],
)
def test_flatten_retry_limit_boundaries(in_window, attempts, expected):
    bot = _bot()
    bot._flatten_attempts = {"bu2612": attempts}
    assert bot._flatten_retry_limit_reached("bu2612", in_window=in_window) is expected


def test_has_any_position_checks_all_scan_symbols():
    bot = _bot()
    bot.scan_symbols = ["bu2612", "lh2601"]
    bot.open_positions = {"bu2612"}
    assert bot._has_any_position() is True
    bot.open_positions = set()
    assert bot._has_any_position() is False


def test_closed_days_reads_config_and_ignores_malformed_values():
    bot = _bot()
    bot.cfg = {"schedule": {"closed_days": ["2026-11-25"]}}
    assert bot._closed_days() == {date(2026, 11, 25)}

    bot.cfg = {"schedule": {"closed_days": "bad"}}
    assert bot._closed_days() == set()

    bot.cfg = None
    assert bot._closed_days() == set()


def test_next_flatten_at_returns_none_when_disabled_or_no_clock():
    bot = _bot(flatten_at_close=False, flatten_times=[time(15, 0)])
    assert bot._next_flatten_at(datetime(2026, 11, 24, 14, 0)) is None

    bot = _bot(flatten_times=[])
    assert bot._next_flatten_at(datetime(2026, 11, 24, 14, 0)) is None


def test_next_flatten_at_same_day_and_next_trading_day():
    bot = _bot(flatten_times=[time(15, 0)])
    assert bot._next_flatten_at(datetime(2026, 11, 24, 14, 0)) == datetime(2026, 11, 24, 15, 0)
    assert bot._next_flatten_at(datetime(2026, 11, 24, 15, 0)) == datetime(2026, 11, 25, 15, 0)


def test_next_flatten_at_skips_weekend():
    bot = _bot(flatten_times=[time(15, 0)])
    friday_after_close = datetime(2026, 11, 27, 16, 0)
    assert bot._next_flatten_at(friday_after_close) == datetime(2026, 11, 30, 15, 0)


def test_next_flatten_at_skips_configured_closed_day():
    bot = _bot(flatten_times=[time(15, 0)])
    bot.cfg = {"schedule": {"closed_days": ["2026-11-25"]}}
    assert bot._next_flatten_at(datetime(2026, 11, 24, 16, 0)) == datetime(2026, 11, 26, 15, 0)


def test_next_flatten_at_asks_strategy_and_respects_veto():
    bot = _bot(flatten_times=[time(15, 0)], checker=lambda when: False)
    assert bot._next_flatten_at(datetime(2026, 11, 24, 14, 0)) is None


def test_next_flatten_at_survives_old_checker_signature():
    def checker():
        return True

    bot = _bot(flatten_times=[time(15, 0)], checker=checker)
    assert bot._next_flatten_at(datetime(2026, 11, 24, 14, 0)) == datetime(2026, 11, 24, 15, 0)


def test_next_flatten_at_uses_union_of_multi_symbol_clocks():
    bot = _bot()
    bot.multi = True
    bot.scan_symbols = ["fu2601", "lh2601"]
    bot.clocks = {"fu2601": ["14:55", "22:55"], "lh2601": ["14:55"]}
    assert bot._next_flatten_at(datetime(2026, 11, 24, 14, 0)) == datetime(2026, 11, 24, 14, 55)


def test_multi_symbol_strategy_veto_is_ignored():
    bot = _bot(checker=lambda when: False)
    bot.multi = True
    assert bot._strategy_would_flatten_at(datetime(2026, 11, 24, 14, 55)) is True

    bot = _bot(checker=lambda when: False)
    assert bot._strategy_would_flatten_at(datetime(2026, 11, 24, 14, 55)) is False


def test_strategy_without_checker_is_treated_as_willing_to_flatten():
    class _NoChecker:
        flatten_at_close = True

    bot = _FlattenBot(_NoChecker())
    assert bot._strategy_would_flatten_at(datetime(2026, 11, 24, 14, 55)) is True


def test_in_trading_session_wrapper_passes_grace_only_when_custom(monkeypatch):
    calls = []

    def fake(sessions, now=None, *, grace_sec=90.0):
        calls.append((sessions, now, grace_sec))
        return True

    monkeypatch.setattr(flatten_manager, "in_trading_session", in_trading_session)
    monkeypatch.setattr("src.scheduler.runner.in_trading_session", fake)

    assert in_trading_session([["09:00", "15:00"]]) is True
    assert calls[-1][2] == 90.0
    assert in_trading_session([["09:00", "15:00"]], grace_sec=5) is True
    assert calls[-1][2] == 5.0
