"""调度层下单守卫：无仓不发平仓单，同一根K线不重复开仓。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.scheduler.runner import TradingBot, entry_slip_blocked
from src.strategy.base import StrategyBase
from src.strategy.types import StrategyAction, StrategyDecision


def test_entry_slip_gate_blocks_chasing_and_leaves_the_other_side_alone() -> None:
    """开仓滑点闸门：只拦「追价」方向，关掉或取不到数据时放行。"""
    # 今天那笔：信号价 4359，8 秒后现价 4393，0.5×ATR(19.4)=9.7 点 → 拦下
    blocked, chase, limit = entry_slip_blocked(4359.0, 4393.0, 19.4, is_long=True, limit_atr=0.5)
    assert blocked is True
    assert chase == 34.0
    assert round(limit, 1) == 9.7
    # 做多时现价更低，对持仓有利，不算追价
    assert entry_slip_blocked(4359.0, 4340.0, 19.4, is_long=True, limit_atr=0.5)[0] is False
    # 做空反过来：现价更低才是追价
    assert entry_slip_blocked(4359.0, 4330.0, 19.4, is_long=False, limit_atr=0.5)[0] is True
    assert entry_slip_blocked(4359.0, 4370.0, 19.4, is_long=False, limit_atr=0.5)[0] is False
    # 阈值设为 0（关闭）、信号价缺失、ATR 缺失，都放行
    assert entry_slip_blocked(4359.0, 4393.0, 19.4, is_long=True, limit_atr=0)[0] is False
    assert entry_slip_blocked(0.0, 4393.0, 19.4, is_long=True, limit_atr=0.5)[0] is False
    assert entry_slip_blocked(4359.0, 4393.0, 0.0, is_long=True, limit_atr=0.5)[0] is False


def test_entry_slip_point_cap_and_atr_cap_take_the_stricter_one() -> None:
    """点数上限可以单独用，也可以和 ATR 上限叠着用，取更严的那个。"""
    # 只写点数：11 点偏移、上限 10 点 → 拦
    blocked, chase, limit = entry_slip_blocked(
        4359.0, 4370.0, 0.0, is_long=True, limit_atr=0, limit_points=10
    )
    assert blocked is True and chase == 11.0 and limit == 10.0
    # 只写点数：8 点偏移 → 放行
    assert entry_slip_blocked(4359.0, 4367.0, 0.0, is_long=True, limit_atr=0, limit_points=10)[0] is False
    # 两个都开：0.5×19.4=9.7 比 20 更严，按 9.7 算
    assert entry_slip_blocked(4359.0, 4369.0, 19.4, is_long=True, limit_atr=0.5, limit_points=20)[0] is True
    # 点数更严时按点数：0.5×19.4=9.7 比 5 松，按 5 算
    blocked, _, limit = entry_slip_blocked(
        4359.0, 4365.0, 19.4, is_long=True, limit_atr=0.5, limit_points=5
    )
    assert blocked is True and limit == 5.0


class _Trader:
    def __init__(self) -> None:
        self.close_calls: list[tuple[str, int, object]] = []

    def close_position(self, symbol: str, volume: int, price: object = None) -> None:
        self.close_calls.append((symbol, volume, price))
        raise AssertionError("无仓时不应该发出平仓单")


class _Strategy(StrategyBase):
    def __init__(self) -> None:
        super().__init__()
        self.positions: dict[str, object] = {}


def _bot() -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _Strategy()
    bot.symbols = ["fu2701"]
    bot.volume = 1
    bot._sent_bar = {}
    bot.last_error = ""
    bot.trader = _Trader()
    events: list[tuple[str, dict]] = []
    bot._record_event = lambda event, **kw: events.append((event, kw))
    bot._events = events  # type: ignore[attr-defined]
    return bot


def test_close_without_position_does_not_order() -> None:
    bot = _bot()

    ok = bot._execute(StrategyDecision(StrategyAction.CLOSE, "fu2701", "到点平仓"))

    assert ok is False
    assert bot.trader.close_calls == []  # type: ignore[attr-defined]
    assert bot._events[0][0] == "close_skip_no_position"  # type: ignore[attr-defined]


def test_same_bar_open_is_deduplicated() -> None:
    bot = _bot()
    bar = "2026-11-02 22:55:00"
    bot._sent_bar["fu2701"] = bar

    ok = bot._execute(
        StrategyDecision(
            StrategyAction.BUY_OPEN,
            "fu2701",
            "突破开盘区间",
            meta={"bar_id": bar},
        )
    )

    assert ok is False
    assert bot._events[-1][0] == "order_dedup"  # type: ignore[attr-defined]


def test_ledger_failure_blocks_new_open_but_not_close() -> None:
    """信号账户写失败后不能再开新仓；平仓仍要放行，否则有仓平不掉。"""
    bot = _bot()
    bot._ledger_failed = True

    ok = bot._execute(
        StrategyDecision(
            StrategyAction.SELL_OPEN,
            "fu2701",
            "向下突破",
            meta={"bar_id": "2026-11-02 22:55:00"},
        )
    )
    assert ok is False
    assert bot._events[-1][0] == "ledger_error_block"  # type: ignore[attr-defined]

    # 平仓走到「程序账上无仓」的守卫，说明没有被 ledger 标记拦下。
    ok_close = bot._execute(StrategyDecision(StrategyAction.CLOSE, "fu2701", "到点平仓"))
    assert ok_close is False
    assert bot._events[-1][0] == "close_skip_no_position"  # type: ignore[attr-defined]


class _Alive:
    """假装一条还在跑的审计线程。"""

    def is_alive(self) -> bool:
        return True


def test_gui_real_order_writes_intent_without_dry_run_attribute() -> None:
    """GUI 真下单走 _execute 时不能再引用不存在的 self.dry_run。"""
    class _Result:
        ok = True
        order_id = "1"
        message = "ok"
        assumed_fill = True

    class _GuiTrader:
        def buy_open(self, symbol: str, volume: int, price: object = None) -> _Result:
            return _Result()

    class _Risk:
        def allow(self, side: str, volume: int, symbol: str) -> tuple[bool, str]:
            return True, ""

    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {"dry_run": False, "trade": {"executor": "gui", "volume": 1}}
    bot.strategy = _Strategy()
    bot.symbols = ["fu2701"]
    bot.volume = 1
    bot._sent_bar = {}
    bot.last_error = ""
    bot.gui_assume_fill = True
    bot.trader = _GuiTrader()
    bot.risk = _Risk()
    intents: list[str] = []
    bot._write_order_intent = lambda decision, vol: intents.append(decision.action.value)  # type: ignore[assignment]
    bot._clear_order_intent = lambda: None  # type: ignore[assignment]
    bot._apply_fill_accounting = lambda *args, **kwargs: None  # type: ignore[assignment]
    bot._size_open = lambda decision: (1, "固定1手")  # type: ignore[assignment]
    bot._position_for = lambda symbol: None  # type: ignore[assignment]
    bot._record_event = lambda event, **kw: None  # type: ignore[assignment]

    ok = bot._execute(
        StrategyDecision(
            StrategyAction.BUY_OPEN,
            "fu2701",
            "多因子做多",
            entry_price=4000,
            meta={"bar_id": "2026-11-05 09:05"},
        )
    )

    assert ok is True
    assert intents == ["buy_open"]
    assert bot._sent_bar["fu2701"] == "2026-11-05 09:05"


def test_failed_order_releases_strategy_signal_mark() -> None:
    """下单没发出去时，策略不能把「这根已处理」锁死，否则信号会静默丢掉。"""
    class _Result:
        ok = False
        order_id = ""
        message = "价格框被挡"
        assumed_fill = False

    class _GuiTrader:
        def buy_open(self, symbol: str, volume: int, price: object = None) -> _Result:
            return _Result()

    class _Risk:
        def allow(self, side: str, volume: int, symbol: str) -> tuple[bool, str]:
            return True, ""

    class _MarkedStrategy(_Strategy):
        def __init__(self) -> None:
            super().__init__()
            self.set_signaled_bar("fu2701", "2026-11-05 21:05:00")
            self._pending = {
                "fu2701": {"bar_id": "2026-11-05 21:05:00", "stop": 4000.0}
            }

    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {"dry_run": False, "trade": {"executor": "gui", "volume": 1}}
    bot.strategy = _MarkedStrategy()
    bot.symbols = ["fu2701"]
    bot.volume = 1
    bot._sent_bar = {}
    bot.last_error = ""
    bot.gui_assume_fill = True
    bot.trader = _GuiTrader()
    bot.risk = _Risk()
    bot._write_order_intent = lambda decision, vol: None  # type: ignore[assignment]
    bot._clear_order_intent = lambda: None  # type: ignore[assignment]
    bot._size_open = lambda decision: (1, "固定1手")  # type: ignore[assignment]
    bot._position_for = lambda symbol: None  # type: ignore[assignment]
    bot._record_event = lambda event, **kw: None  # type: ignore[assignment]

    ok = bot._execute(
        StrategyDecision(
            StrategyAction.BUY_OPEN,
            "fu2701",
            "多因子做多",
            entry_price=4000,
            meta={"bar_id": "2026-11-05 21:05:00"},
        )
    )

    assert ok is False
    assert bot.strategy.get_signaled_bar("fu2701") == ""
    assert "fu2701" not in bot.strategy._pending  # type: ignore[attr-defined]
    assert bot._sent_bar == {}


def test_successful_order_keeps_signal_mark() -> None:
    class _Result:
        ok = True
        order_id = "1"
        message = "ok"
        assumed_fill = True

    class _GuiTrader:
        def buy_open(self, symbol: str, volume: int, price: object = None) -> _Result:
            return _Result()

    class _Risk:
        def allow(self, side: str, volume: int, symbol: str) -> tuple[bool, str]:
            return True, ""

    class _MarkedStrategy(_Strategy):
        def __init__(self) -> None:
            super().__init__()
            self.set_signaled_bar("fu2701", "2026-11-05 21:05:00")

    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {"dry_run": False, "trade": {"executor": "gui", "volume": 1}}
    bot.strategy = _MarkedStrategy()
    bot.symbols = ["fu2701"]
    bot.volume = 1
    bot._sent_bar = {}
    bot.last_error = ""
    bot.gui_assume_fill = True
    bot.trader = _GuiTrader()
    bot.risk = _Risk()
    bot._write_order_intent = lambda decision, vol: None  # type: ignore[assignment]
    bot._clear_order_intent = lambda: None  # type: ignore[assignment]
    bot._apply_fill_accounting = lambda *args, **kwargs: None  # type: ignore[assignment]
    bot._size_open = lambda decision: (1, "固定1手")  # type: ignore[assignment]
    bot._position_for = lambda symbol: None  # type: ignore[assignment]
    bot._record_event = lambda event, **kw: None  # type: ignore[assignment]

    ok = bot._execute(
        StrategyDecision(
            StrategyAction.BUY_OPEN,
            "fu2701",
            "多因子做多",
            entry_price=4000,
            meta={"bar_id": "2026-11-05 21:05:00"},
        )
    )

    assert ok is True
    assert bot.strategy.get_signaled_bar("fu2701") == "2026-11-05 21:05:00"


def test_scan_symbol_releases_mark_when_execute_raises_before_dispatch() -> None:
    """下单前报错（例如 11-05 的 dry_run AttributeError）不能吃掉信号，下一轮还要能重试。"""
    from datetime import datetime, timedelta

    import pandas as pd

    cfg = {
        "dry_run": True,
        "trade": {"symbol": "fu2701", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["fu2701"],
            "catch_up_on_start": {"enabled": False},
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000},
        "data": {},
        "_project_root": ".",
    }
    bot = TradingBot(cfg)
    end = datetime.now() - timedelta(minutes=2)
    index = pd.DatetimeIndex([end - timedelta(minutes=5 * (39 - i)) for i in range(40)])
    close = pd.Series(100.0, index=index)
    bars = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10}
    )
    bar_id = str(bars.index[-1])
    bot.strategy.set_signaled_bar("fu2701", bar_id)
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "假开仓",
        entry_price=100.0,
        stop_price=95.0,
        meta={"bar_id": bar_id},
    )

    with patch.object(bot.kline, "fetch", return_value=bars):
        with patch.object(bot.kline, "latest_price", return_value=0.0):
            with patch.object(bot, "_evaluate_symbol", return_value=decision):
                with patch.object(bot, "_execute", side_effect=AttributeError("boom")):
                    with pytest.raises(AttributeError):
                        bot.scan_symbol("fu2701")

    assert bot.strategy.get_signaled_bar("fu2701") == ""


def test_scan_symbol_skips_open_when_price_already_ran_away() -> None:
    """信号价 100、现价 110、ATR 2（0.5×ATR=1）→ 不追，也不点单。"""
    from datetime import datetime, timedelta

    import pandas as pd

    cfg = {
        "dry_run": True,
        "trade": {"symbol": "fu2701", "executor": "sim", "volume": 1, "max_entry_slip_atr": 0.5},
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["fu2701"],
            "catch_up_on_start": {"enabled": False},
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000},
        "data": {},
        "_project_root": ".",
    }
    bot = TradingBot(cfg)
    end = datetime.now() - timedelta(minutes=2)
    index = pd.DatetimeIndex([end - timedelta(minutes=5 * (39 - i)) for i in range(40)])
    close = pd.Series(100.0, index=index)
    bars = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10}
    )
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "假开仓",
        entry_price=100.0,
        stop_price=95.0,
        meta={"bar_id": str(bars.index[-1])},
    )

    with patch.object(bot.kline, "fetch", return_value=bars):
        with patch.object(bot.kline, "latest_price", return_value=110.0):
            with patch.object(bot, "_evaluate_symbol", return_value=decision):
                with patch.object(bot, "_execute") as execute:
                    bot.scan_symbol("fu2701")
    assert not execute.called


def test_audit_skips_when_previous_thread_is_still_running() -> None:
    bot = TradingBot.__new__(TradingBot)
    bot._last_audit_at = None
    original = _Alive()
    bot._audit_thread = original  # type: ignore[assignment]

    bot._run_session_audit("重复触发")

    # 没刷新触发时间，也没换新线程；上一次跑完后下一次自然会再触发。
    assert bot._last_audit_at is None
    assert bot._audit_thread is original


class _StrategyWithoutNow:
    def evaluate(self, symbol, bars, *, trail_bars=None):
        return "old-signature"


class _StrategyWithNow:
    def evaluate(self, symbol, bars, *, trail_bars=None, now=None):
        return f"now={now}"


class _StrategyRaisingTypeError:
    def evaluate(self, symbol, bars, *, trail_bars=None, now=None):
        raise TypeError("策略内部真正的 bug")


def test_evaluate_symbol_passes_now_only_when_signature_accepts_it() -> None:
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _StrategyWithoutNow()
    assert bot._evaluate_symbol("fu2701", None, now="T") == "old-signature"

    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _StrategyWithNow()
    assert bot._evaluate_symbol("fu2701", None, now="T") == "now=T"


def test_evaluate_symbol_no_longer_swallows_type_error() -> None:
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = _StrategyRaisingTypeError()
    with pytest.raises(TypeError, match="策略内部真正的 bug"):
        bot._evaluate_symbol("fu2701", None, now="T")


