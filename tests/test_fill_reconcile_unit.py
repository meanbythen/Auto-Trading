"""CTP 成交回报去重与补记的单元测试（白盒，Mixin + Fake）。"""

from __future__ import annotations

from collections import OrderedDict

import pytest

from src.scheduler.fill_reconcile import FillReconcileMixin, ctp_fill_key
from src.strategy import StrategyAction, StrategyDecision


class _FillBot(FillReconcileMixin):
    _RECENT_FILL_LIMIT = 3

    def __init__(self):
        self.calls = []
        self.events = []
        self._close_chase_ticks = {}
        self._recent_fill_keys = None
        self.apply_ok = True

    def _apply_fill_accounting(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.apply_ok

    def _record_event(self, name, **kwargs):
        self.events.append((name, kwargs))


class _Lock:
    def __init__(self, ok=True):
        self.ok = ok
        self.acquired = False
        self.released = False

    def acquire(self, timeout=None):
        self.acquired = True
        return self.ok

    def release(self):
        self.released = True


def _decision(action=StrategyAction.BUY_OPEN, symbol="bu2612"):
    return StrategyDecision(action=action, symbol=symbol, reason="unit")


def _report(**overrides):
    report = {
        "symbol": "bu2612",
        "offset": "0",
        "direction": "0",
        "price": 100.0,
        "filled": 5,
        "order_ref": "ref-1",
    }
    report.update(overrides)
    return report


@pytest.mark.parametrize(
    ("ref", "filled", "expected"),
    [
        ("", 5, ""),
        (None, 5, ""),
        ("   ", 5, ""),
        ("abc", 5, "ctp:abc:5"),
        (" abc ", "7", "ctp:abc:7"),
        ("abc", 7.9, "ctp:abc:7"),
        ("abc", -1, "ctp:abc:-1"),
    ],
)
def test_ctp_fill_key_normalizes_order_ref_and_total(ref, filled, expected):
    assert ctp_fill_key(ref, filled) == expected


@pytest.mark.parametrize(
    ("offset", "direction", "action"),
    [
        ("0", "0", StrategyAction.BUY_OPEN),
        ("0", "1", StrategyAction.SELL_OPEN),
        ("1", "0", StrategyAction.CLOSE),
        ("1", "1", StrategyAction.CLOSE),
    ],
)
def test_on_ctp_fill_maps_offset_and_direction(offset, direction, action):
    bot = _FillBot()
    assert bot._on_ctp_fill_locked(_report(offset=offset, direction=direction)) is True
    assert bot.calls[0][0][1].action == action


@pytest.mark.parametrize(
    ("filled", "accounted", "expected_volume"),
    [(5, 0, 5), (5, 2, 3), (2, 2, 0), (1, 3, 0)],
)
def test_on_ctp_fill_books_only_unaccounted_volume(filled, accounted, expected_volume):
    bot = _FillBot()
    ok = bot._on_ctp_fill_locked(_report(filled=filled, accounted_volume=accounted))
    assert ok is True
    if expected_volume == 0:
        assert bot.calls == []
    else:
        assert bot.calls[0][0][2] == expected_volume


def test_on_ctp_fill_rejects_missing_price():
    bot = _FillBot()
    assert bot._on_ctp_fill_locked(_report(price=0)) is False
    assert bot.calls == []


def test_on_ctp_fill_dedupes_recent_fill_key():
    bot = _FillBot()
    bot._recent_fill_keys = OrderedDict([("ctp:ref-1:5", 1.0)])
    assert bot._on_ctp_fill_locked(_report()) is True
    assert bot.calls == []


def test_on_ctp_fill_does_not_record_when_accounting_fails():
    bot = _FillBot()
    bot.apply_ok = False
    assert bot._on_ctp_fill_locked(_report()) is False
    assert bot._recent_fill_keys is None
    assert bot.events == []


def test_on_ctp_fill_records_recent_key_and_clears_chase_ticks():
    bot = _FillBot()
    bot._close_chase_ticks = {"bu2612": 3}
    assert bot._on_ctp_fill_locked(_report()) is True
    assert "ctp:ref-1:5" in bot._recent_fill_keys
    assert bot._close_chase_ticks == {}
    assert bot.events[0][0] == "order"


def test_on_ctp_fill_passes_stop_price_and_meta():
    bot = _FillBot()
    bot._on_ctp_fill_locked(
        _report(stop_price="95.5", bar_id="bar-1", trace_id="trace-1", meta={"k": "v"})
    )
    args, kwargs = bot.calls[0]
    decision = args[1]
    assert kwargs["stop_price"] == 95.5
    assert decision.meta["bar_id"] == "bar-1"
    assert decision.meta["trace_id"] == "trace-1"
    assert decision.meta["k"] == "v"
    assert kwargs["fill_evidence"]["fill_kind"] == "broker_fill"
    assert kwargs["fill_evidence"]["broker_confirmed"] is True


def test_on_ctp_fill_without_lock_delegates_directly():
    bot = _FillBot()
    assert bot._on_ctp_fill(_report()) is True
    assert len(bot.calls) == 1


def test_on_ctp_fill_returns_false_when_lock_times_out():
    bot = _FillBot()
    lock = _Lock(ok=False)
    bot._trade_lock = lock
    assert bot._on_ctp_fill(_report()) is False
    assert lock.acquired is True
    assert lock.released is False
    assert bot.calls == []
    assert bot.events[0][0] == "fill_lock_timeout"


def test_on_ctp_fill_releases_lock_after_success():
    bot = _FillBot()
    lock = _Lock(ok=True)
    bot._trade_lock = lock
    assert bot._on_ctp_fill(_report()) is True
    assert lock.released is True


def test_book_ctp_leftover_returns_false_without_rows():
    bot = _FillBot()
    assert bot._book_ctp_leftover(_decision(), {}, 100.0, "买") is False


def test_book_ctp_leftover_skips_foreign_and_fully_booked_rows():
    bot = _FillBot()
    raw = {
        "orders": [
            {"order_ref": "a", "filled": 5, "accounted_volume": 5},
            {"order_ref": "b", "filled": 5, "accounted_volume": 0, "foreign": True},
        ]
    }
    assert bot._book_ctp_leftover(_decision(), raw, 100.0, "买") is False
    assert bot.calls == []


def test_book_ctp_leftover_records_order_without_price():
    bot = _FillBot()
    raw = {"orders": [{"order_ref": "", "filled": 2, "accounted_volume": 0, "avg_price": 0}]}
    assert bot._book_ctp_leftover(_decision(), raw, 0.0, "买") is False
    assert bot.events[0][0] == "order_no_price"


def test_book_ctp_leftover_books_valid_rows_and_returns_true():
    bot = _FillBot()
    raw = {
        "orders": [
            {"order_ref": "r1", "filled": 3, "accounted_volume": 1, "avg_price": 101.5},
            {"order_ref": "r2", "filled": 2, "accounted_volume": 0, "avg_price": 102.0},
        ]
    }
    assert bot._book_ctp_leftover(_decision(), raw, 100.0, "买") is True
    assert [c[0][2] for c in bot.calls] == [2, 2]
    assert [c[1]["fill_key"] for c in bot.calls] == ["ctp:r1:3", "ctp:r2:2"]
    assert [e[0] for e in bot.events] == ["order", "order"]
