"""
收盘清仓按时钟触发：到点直接平，不等新K线。
不连行情、不点窗口，只验证触发条件和重试节奏。
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from src.scheduler.runner import TradingBot

# 测试统一按交易日（2026-11-02 周一）的钟点跑，避免在周末/节假日执行用例时被交易日历挡掉。
NOW = datetime(2026, 11, 2, 14, 55)


class _Pos:
    def __init__(self, side: str, volume: int) -> None:
        self.side = side
        self.volume = volume


class _Strategy:
    def __init__(
        self,
        due: bool,
        positions: dict[str, _Pos],
        at_close: bool = True,
        times: tuple[dtime, ...] = (dtime(14, 55), dtime(22, 55)),
    ) -> None:
        self._due = due
        self.positions = positions
        self.flatten_at_close = at_close
        self.flatten_times = list(times)

    def is_flatten_time(self, now: object = None) -> bool:
        # 预约下一枪时传入具体钟点：品种表上的清仓点默认都认。
        # flatten_if_due 不带 now 时，仍用 due 表示「现在是否在窗口里」。
        if now is None:
            return self._due
        stamp = getattr(now, "time", lambda: None)()
        if stamp is None:
            return self._due
        for clock in self.flatten_times:
            if stamp.hour == clock.hour and stamp.minute == clock.minute:
                return True
        return False


def _bot(due: bool, positions: dict[str, _Pos]) -> tuple[TradingBot, list[tuple[str, str]]]:
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _Strategy(due, positions)
    bot.symbols = ["fu2701"]
    bot.scan_symbols = ["fu2701"]
    bot.volume = 1
    bot._flatten_retry_sec = 10.0
    bot._flatten_try = {}
    bot._flatten_pending = False
    bot._flatten_attempts = {}
    bot._flatten_sent = set()
    bot._sent_bar = {}
    bot.sessions = []
    calls: list[tuple[str, str]] = []
    bot._execute = lambda decision: calls.append((decision.action.value, decision.symbol))
    bot._record_event = lambda *args, **kwargs: None
    bot.last_error = ""
    return bot, calls


def test_flatten_due_closes_without_waiting_for_a_new_bar() -> None:
    bot, calls = _bot(True, {"fu2701": _Pos("long", 1)})

    bot.flatten_if_due(now=NOW)

    assert calls == [("close", "fu2701")]


def test_flatten_skips_retry_right_after_own_close() -> None:
    """刚平仓成交后，即使内存还挂着仓，也不要马上再报一次平仓。"""
    bot, calls = _bot(True, {"fu2701": _Pos("short", 1)})
    bot._note_own_fill("fu2701", kind="close")

    bot.flatten_if_due(now=NOW)

    assert calls == []


def test_flatten_due_waits_before_retrying_and_stops_once_flat() -> None:
    positions = {"fu2701": _Pos("long", 1)}
    bot, calls = _bot(True, positions)

    bot.flatten_if_due(now=NOW)
    bot.flatten_if_due(now=NOW)  # 10 秒内不重复点
    assert len(calls) == 1

    positions["fu2701"] = _Pos("flat", 0)
    bot.flatten_if_due(now=NOW)
    assert len(calls) == 1
    assert bot._flatten_pending is False


def test_flatten_due_does_nothing_outside_the_window() -> None:
    bot, calls = _bot(False, {"fu2701": _Pos("long", 1)})

    bot.flatten_if_due(now=NOW)

    assert calls == []


def test_next_flatten_at_uses_the_nearest_clock_time() -> None:
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _Strategy(False, {})

    assert bot._next_flatten_at(datetime(2026, 11, 2, 14, 50)) == datetime(2026, 11, 2, 14, 55)
    assert bot._next_flatten_at(datetime(2026, 11, 2, 15, 0)) == datetime(2026, 11, 2, 22, 55)
    assert bot._next_flatten_at(datetime(2026, 11, 2, 23, 10)) == datetime(2026, 11, 3, 14, 55)


def test_next_flatten_at_skips_clocks_the_strategy_rejects() -> None:
    """夜日连续否掉 22:55 时，墙上时钟不能再预约夜盘强平。"""

    class _DayOnly(_Strategy):
        def is_flatten_time(self, now: object = None) -> bool:
            if now is None:
                return self._due
            stamp = now.time()
            return stamp.hour == 14 and stamp.minute == 55

    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _DayOnly(False, {})

    assert bot._next_flatten_at(datetime(2026, 11, 2, 14, 50)) == datetime(2026, 11, 2, 14, 55)
    assert bot._next_flatten_at(datetime(2026, 11, 2, 15, 0)) == datetime(2026, 11, 3, 14, 55)


def test_next_flatten_at_skips_the_weekend() -> None:
    """周五收盘后不能把清仓预约到周六；要找到下一个交易日。"""
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _Strategy(False, {})

    # 2026-11-06 是周五，11-09 是下周一
    assert bot._next_flatten_at(datetime(2026, 11, 6, 23, 10)) == datetime(2026, 11, 9, 14, 55)
    assert bot._next_flatten_at(datetime(2026, 11, 7, 10, 0)) == datetime(2026, 11, 9, 14, 55)


def test_next_flatten_at_finds_next_trading_day_when_strategy_rejects_weekend() -> None:
    """策略自己按工作日否掉周末时，也要继续往后找，而不是让定时器直接失效。"""

    class _WeekdayOnly(_Strategy):
        def is_flatten_time(self, now: object = None) -> bool:
            if now is None:
                return self._due
            stamp = now
            return bool(getattr(stamp, "weekday")() < 5 and stamp.hour == 14 and stamp.minute == 55)

    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _WeekdayOnly(False, {})

    assert bot._next_flatten_at(datetime(2026, 11, 6, 15, 0)) == datetime(2026, 11, 9, 14, 55)


def test_next_flatten_at_skips_exchange_holidays() -> None:
    """国庆连休：9 月 30 日收盘后不能把清仓预约到 10 月 1 日，要等到假期后。"""
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _Strategy(False, {})

    # 2026-10-01 起连休到 10-07，10-08（周四）才开市。
    assert bot._next_flatten_at(datetime(2026, 9, 30, 23, 10)) == datetime(2026, 10, 8, 14, 55)
    assert bot._next_flatten_at(datetime(2026, 10, 2, 10, 0)) == datetime(2026, 10, 8, 14, 55)


def test_next_flatten_at_skips_configured_closed_days() -> None:
    """临时休市/调休写在 schedule.closed_days 里：那天不预约清仓。"""
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _Strategy(False, {})
    bot.cfg = {"schedule": {"closed_days": ["2026-11-03"]}}

    assert bot._next_flatten_at(datetime(2026, 11, 2, 15, 0)) == datetime(2026, 11, 2, 22, 55)
    assert bot._next_flatten_at(datetime(2026, 11, 2, 23, 10)) == datetime(2026, 11, 4, 14, 55)


def test_flatten_skips_configured_closed_days() -> None:
    """临时休市当天即使到点也不点单，待办保留到下一个交易日。"""
    bot, calls = _bot(True, {"fu2701": _Pos("long", 1)})
    bot.cfg = {"schedule": {"closed_days": ["2026-11-03"]}}

    bot.flatten_if_due(now=datetime(2026, 11, 3, 14, 55))

    assert calls == []


def test_next_flatten_at_is_none_when_close_rule_is_off() -> None:
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _Strategy(False, {}, at_close=False)

    assert bot._next_flatten_at(datetime(2026, 11, 2, 14, 50)) is None


def test_flatten_pending_keeps_retrying_after_the_window() -> None:
    """窗口过了还没平掉：待办保留，继续重试，不再把钟点推到下一个。"""
    bot, calls = _bot(False, {"fu2701": _Pos("long", 1)})
    bot.sessions = [["00:00", "23:59"]]
    bot._flatten_pending = True

    bot.flatten_if_due(now=NOW)

    assert calls == [("close", "fu2701")]
    assert bot._flatten_pending is True


def test_flatten_waits_for_the_next_session_when_market_is_closed(monkeypatch) -> None:
    """收盘后的深夜不再反复点同花顺，待办保留到下一节开盘。"""
    import src.scheduler.runner as runner

    monkeypatch.setattr(runner, "in_trading_session", lambda sessions, now=None: False)
    bot, calls = _bot(False, {"fu2701": _Pos("long", 1)})
    bot.sessions = [["09:00", "15:00"]]
    bot._flatten_pending = True

    bot.flatten_if_due(now=NOW)

    assert calls == []
    assert bot._flatten_pending is True


def test_flatten_does_not_reclick_when_assume_fill_is_off() -> None:
    """点过平仓但程序账还当有仓时，不再连点，避免点出反向。"""
    bot, calls = _bot(True, {"fu2701": _Pos("long", 1)})
    bot.gui_assume_fill = False
    bot._execute = lambda decision: (calls.append((decision.action.value, decision.symbol)), True)[1]

    bot.flatten_if_due(now=NOW)
    bot._flatten_try = {}
    bot.flatten_if_due(now=NOW)

    assert len(calls) == 1
    assert "fu2701" in bot._flatten_sent


def test_flatten_stops_after_repeated_sent_but_unconfirmed() -> None:
    """GUI 点了但内部账一直不空：到上限后停止重试，避免整夜反复点平仓。"""
    bot, calls = _bot(False, {"fu2701": _Pos("long", 1)})
    bot.sessions = [["00:00", "23:59"]]
    bot._flatten_pending = True
    bot._execute = lambda decision: (calls.append((decision.action.value, decision.symbol)), True)[1]
    bot._flatten_attempts = {"fu2701": 29}

    bot.flatten_if_due(now=NOW)
    assert len(calls) == 1
    assert "fu2701" in bot._flatten_sent

    bot._flatten_try = {}
    bot.flatten_if_due(now=NOW)
    assert len(calls) == 1
    assert bot._flatten_pending is False
