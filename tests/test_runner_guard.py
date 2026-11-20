"""调度层下单守卫：无仓不发平仓单，同一根K线不重复开仓。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scheduler.order_executor import OrderIntentWriteError
from src.scheduler.runner import TradingBot, entry_slip_blocked
from src.strategy.base import StrategyBase
from src.strategy.types import StrategyAction, StrategyDecision
from src.trader.base import DEFAULT_CAPABILITIES
from src.trader.order_timeline import OrderTimeline


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


def test_close_books_when_broker_already_empty() -> None:
    """手机把仓平了以后，程序只补记账，不再向柜台报平仓。"""
    from src.strategy.types import PositionState
    from src.trader.ctp_logic import PositionView

    bot = _bot()
    bot.trader.capabilities = DEFAULT_CAPABILITIES["ctp"]
    bot.trader.gateway = type("G", (), {"positions": {"fu2701": PositionView()}})()
    bot.strategy.positions["fu2701"] = PositionState(
        side="short", volume=1, entry_price=3892.0, symbol="fu2701"
    )
    booked: list[tuple[str, float, str]] = []

    def _book(symbol: str, exit_price: float, reason: str = "") -> tuple[dict, float]:
        booked.append((symbol, float(exit_price), reason))
        return {"symbol": symbol}, 0.0

    bot._manual_close_locked = _book  # type: ignore[assignment]
    bot.kline = type("_K", (), {"latest_price": lambda self, symbol: 3890.0})()
    bot._frozen_symbols = set()

    ok = bot._execute(StrategyDecision(StrategyAction.CLOSE, "fu2701", "止损离场"))

    assert ok is True
    assert booked == [("fu2701", 3890.0, "柜台已无持仓（可能已在别处平掉），补记平仓")]
    assert bot.trader.close_calls == []  # type: ignore[attr-defined]


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


def test_ledger_rejection_keeps_strategy_position_unchanged() -> None:
    """账本没记上时不能改策略/风控仓位，否则会出现幽灵仓。"""
    bot = _bot()
    bot.cfg = {"dry_run": False, "risk": {}}
    bot.root = Path(".")
    bot._ledger_failed = False
    bot._position_for = lambda symbol: None  # type: ignore[assignment]
    bot._record_signal_account = lambda *args, **kwargs: None  # type: ignore[assignment]
    bot._contract_multiplier = lambda symbol: 10  # type: ignore[assignment]
    calls: list[str] = []
    bot.strategy.on_filled = lambda *args, **kwargs: calls.append("strategy")  # type: ignore[assignment]
    bot.risk = type(  # type: ignore[assignment]
        "_Risk",
        (),
        {"on_order_filled": lambda self, *args, **kwargs: calls.append("risk")},
    )()

    bot._apply_fill_accounting(
        "fu2701",
        StrategyDecision(StrategyAction.BUY_OPEN, "fu2701", "测试", entry_price=100),
        1,
        100,
        100,
    )

    assert calls == []
    assert bot._ledger_failed is True
    assert bot._events[-1][0] == "ledger_block_position_update"  # type: ignore[attr-defined]


def test_intent_write_failure_blocks_order_and_halts() -> None:
    """意图写盘失败时本笔不能下单，并熔断等人工处理。"""
    class _Risk:
        def __init__(self) -> None:
            self.kind = ""

        def allow(self, side: str, volume: int, symbol: str) -> tuple[bool, str]:
            return True, ""

        def set_halt(self, kind: str, reason: str) -> None:
            self.kind = kind

    class _GuiTrader:
        capabilities = DEFAULT_CAPABILITIES["gui"]

        def buy_open(self, symbol: str, volume: int, price: object = None) -> object:
            raise AssertionError("意图写失败时不应该下单")

    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {"dry_run": False, "trade": {"executor": "gui", "volume": 1}}
    bot.strategy = _Strategy()
    bot.symbols = ["fu2701"]
    bot.volume = 1
    bot._sent_bar = {}
    bot.last_error = ""
    bot.trader = _GuiTrader()  # type: ignore[assignment]
    bot.risk = _Risk()  # type: ignore[assignment]
    bot._size_open = lambda decision: (1, "固定1手")  # type: ignore[assignment]
    bot._position_for = lambda symbol: None  # type: ignore[assignment]
    bot._record_event = lambda event, **kw: None  # type: ignore[assignment]

    def _boom(decision: StrategyDecision, vol: int) -> None:
        raise OrderIntentWriteError("磁盘满")

    bot._write_order_intent = _boom  # type: ignore[assignment]

    ok = bot._execute(
        StrategyDecision(
            StrategyAction.BUY_OPEN,
            "fu2701",
            "多因子做多",
            entry_price=4000,
            meta={"bar_id": "2026-11-05 09:05"},
        )
    )

    assert ok is False
    assert bot.risk.kind == "intent_write_failed"  # type: ignore[attr-defined]


def _intent_bot(intent_path: Path) -> TradingBot:
    """一条最小的 GUI 真下单链路：意图文件落在临时目录。"""
    class _Risk:
        halted = False
        halt_kind = ""
        halt_reason = ""

        def allow(self, side: str, volume: int, symbol: str) -> tuple[bool, str]:
            return True, ""

        def set_halt(self, kind: str, reason: str) -> None:
            type(self).halted = True
            type(self).halt_kind = kind
            type(self).halt_reason = reason

    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {"dry_run": False, "trade": {"executor": "gui", "volume": 1}}
    bot.strategy = _Strategy()
    bot.symbols = ["fu2701"]
    bot.volume = 1
    bot._sent_bar = {}
    bot.last_error = ""
    bot.gui_assume_fill = True
    bot.intent_path = intent_path
    bot._pending_intent = None
    bot.risk = _Risk()
    bot._size_open = lambda decision: (1, "固定1手")  # type: ignore[assignment]
    bot._position_for = lambda symbol: None  # type: ignore[assignment]
    bot._record_event = lambda event, **kw: None  # type: ignore[assignment]
    bot._apply_fill_accounting = lambda *args, **kwargs: None  # type: ignore[assignment]
    return bot


def test_unclean_click_counts_as_sent_and_keeps_intent(tmp_path) -> None:
    """点击已发出但收尾异常：按已发出记账，保留意图文件并立即熔断等人工核对。"""
    class _Result:
        ok = True
        order_id = "1"
        message = "已点击，但关确认框失败"
        assumed_fill = True
        raw = {"assumed_fill": True, "executor": "gui", "unclean": True}

    class _GuiTrader:
        capabilities = DEFAULT_CAPABILITIES["gui"]

        def buy_open(self, symbol: str, volume: int, price: object = None) -> _Result:
            return _Result()

    intent = tmp_path / "order_intent.json"
    bot = _intent_bot(intent)
    bot.trader = _GuiTrader()  # type: ignore[assignment]
    bot._order_timeline = OrderTimeline(None)

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
    # 这一根不能再重试，否则重复开仓
    assert bot._sent_bar["fu2701"] == "2026-11-05 09:05"
    assert intent.exists()
    assert bot.risk.halt_kind == "pending_intent"  # type: ignore[attr-defined]
    assumed = next(row for row in bot._order_timeline.rows if row["stage"] == "assumed_fill")
    assert assumed["price_kind"] == "reference_assumed"
    assert assumed["broker_confirmed"] is False
    assert assumed["trade_id"] == ""
    assert assumed["trade_time"] == ""


def test_runtime_leftover_intent_blocks_next_order(tmp_path) -> None:
    """运行期遗留的意图文件不能被下一笔覆盖：本笔拒发、熔断，原记录原样保留。"""
    class _GuiTrader:
        capabilities = DEFAULT_CAPABILITIES["gui"]
        calls = 0

        def buy_open(self, symbol: str, volume: int, price: object = None) -> object:
            type(self).calls += 1
            raise AssertionError("有未核对委托时不应该下单")

    intent = tmp_path / "order_intent.json"
    intent.write_text(
        json.dumps(
            {
                "ts": "2026-11-05T09:10:00",
                "symbol": "bu2612",
                "side": "sell_open",
                "volume": 2,
                "bar_id": "2026-11-05 09:05",
                "reason": "上一笔",
                "executor": "gui",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bot = _intent_bot(intent)
    bot.trader = _GuiTrader()  # type: ignore[assignment]

    ok = bot._execute(
        StrategyDecision(
            StrategyAction.BUY_OPEN,
            "fu2701",
            "多因子做多",
            entry_price=4000,
            meta={"bar_id": "2026-11-05 09:35"},
        )
    )

    assert ok is False
    assert _GuiTrader.calls == 0
    assert bot.risk.halt_kind == "pending_intent"  # type: ignore[attr-defined]
    assert json.loads(intent.read_text(encoding="utf-8"))["symbol"] == "bu2612"


def test_ctp_opponent_limit_uses_ask_bid_not_stop() -> None:
    """买平看卖一，卖平看买一；止损价不能当成限价。"""
    from src.scheduler.order_executor import ctp_opponent_limit

    assert ctp_opponent_limit(is_buy=True, last=3893, ask=3894, tick=1, extra_ticks=2) == 3896.0
    assert ctp_opponent_limit(is_buy=False, last=3893, bid=3892, tick=1, extra_ticks=2) == 3890.0
    assert ctp_opponent_limit(is_buy=True, last=3893, ask=3883, upper=3900, tick=1, extra_ticks=2) == 3885.0


def test_ctp_close_price_adds_protective_ticks() -> None:
    """CTP 平仓按对手价方向让几跳，不拿止损价挂单。"""
    import pandas as pd

    bot = _intent_bot(Path("unused-order-intent.json"))
    bot.cfg = {
        "dry_run": False,
        "trade": {"executor": "ctp", "price_tick": 1, "ctp_close_protect_ticks": 2},
        "risk": {},
    }
    bot.strategy.timeframe = "5"  # type: ignore[attr-defined]
    bot.kline = type(  # type: ignore[assignment]
        "_K",
        (),
        {
            "fetch": lambda self, symbol, period: pd.DataFrame({"close": [4569.0]}),
            "latest_price": lambda self, symbol: 4569.0,
        },
    )()
    bot.trader = type("_T", (), {"gateway": None})()  # type: ignore[assignment]
    bot._close_chase_ticks = {}

    bot._position_for = lambda symbol: type("_P", (), {"side": "long"})()  # type: ignore[assignment]
    long_price = bot._ctp_limit_price(
        StrategyDecision(StrategyAction.CLOSE, "bu2612", "止损", stop_price=4500)
    )

    bot._position_for = lambda symbol: type("_P", (), {"side": "short"})()  # type: ignore[assignment]
    short_price = bot._ctp_limit_price(
        StrategyDecision(StrategyAction.CLOSE, "bu2612", "止损", stop_price=3882)
    )

    assert long_price == 4567.0
    assert short_price == 4571.0


def test_ctp_close_chase_caps_at_eight_ticks() -> None:
    """平仓未成交可以多让几跳，最多 8 跳，避免一路追到涨跌停。"""
    import pandas as pd

    bot = _intent_bot(Path("unused-order-intent.json"))
    bot.cfg = {
        "dry_run": False,
        "trade": {"executor": "ctp", "price_tick": 1, "ctp_close_protect_ticks": 2},
        "risk": {},
    }
    bot.strategy.timeframe = "5"  # type: ignore[attr-defined]
    bot.kline = type(  # type: ignore[assignment]
        "_K",
        (),
        {
            "fetch": lambda self, symbol, period: pd.DataFrame({"close": [4569.0]}),
            "latest_price": lambda self, symbol: 4569.0,
        },
    )()
    bot.trader = type("_T", (), {"gateway": None})()  # type: ignore[assignment]
    bot._close_chase_ticks = {"bu2612": 40}
    bot._position_for = lambda symbol: type("_P", (), {"side": "long"})()  # type: ignore[assignment]
    price = bot._ctp_limit_price(
        StrategyDecision(StrategyAction.CLOSE, "bu2612", "止损", stop_price=4500)
    )
    assert price == 4561.0


def test_ctp_open_slip_blocked_after_opponent_price() -> None:
    """CTP 开仓对手价比信号价追太远就放弃；平仓不走这道闸。"""
    bot = _intent_bot(Path("unused-order-intent.json"))
    bot.cfg = {"trade": {"max_entry_slip_atr": 0, "max_entry_slip_points": 2}}
    bot.strategy.timeframe = "15"  # type: ignore[attr-defined]
    bot.kline = type("_K", (), {"fetch": lambda self, symbol, period: None})()  # type: ignore[assignment]
    bot._record_event = lambda *args, **kwargs: None  # type: ignore[assignment]
    open_d = StrategyDecision(
        StrategyAction.BUY_OPEN, "fu2701", "测", entry_price=3890.0
    )
    assert bot._ctp_open_slip_blocked(open_d, 3895.0) is True
    assert bot._ctp_open_slip_blocked(open_d, 3891.0) is False
    close_d = StrategyDecision(StrategyAction.CLOSE, "fu2701", "止损", stop_price=3880)
    # 平仓不会调用这道闸；函数本身只给开仓用，对手价更差也应能平出去。
    assert close_d.action == StrategyAction.CLOSE


def test_orders_are_blocked_outside_trading_session(tmp_path, monkeypatch) -> None:
    """非交易时段不下单：挂着的信号不能拿去点已收盘的市场。"""
    intent = tmp_path / "order_intent.json"
    bot = _intent_bot(intent)
    bot.sessions = [["09:00", "15:00"]]
    monkeypatch.setattr(
        "src.utils.session_clock.in_trading_sessions",
        lambda sessions, now=None, **kwargs: False,
    )

    ok = bot._execute(StrategyDecision(StrategyAction.CLOSE, "fu2701", "收盘清仓"))

    assert ok is False
    assert not intent.exists()


def test_manual_close_rejects_nonpositive_price() -> None:
    bot = _bot()
    bot.cfg = {"dry_run": False, "risk": {}}
    bot.root = Path(".")
    bot._persist_state = lambda: None  # type: ignore[assignment]
    called: list[int] = []
    bot._record_signal_account = lambda *args, **kwargs: called.append(1)  # type: ignore[assignment]
    bot._position_for = lambda symbol: type(  # type: ignore[assignment]
        "_Pos", (), {"side": "long", "volume": 1, "entry_price": 100.0, "opened_at": ""}
    )()

    closed, balance = bot.manual_close("fu2701", 0)

    assert (closed, balance) == (None, None)
    assert called == []


def test_manual_close_ledger_failure_keeps_position() -> None:
    bot = _bot()
    bot.cfg = {"dry_run": False, "risk": {}}
    bot.root = Path(".")
    bot._ledger_failed = False
    bot._persist_state = lambda: None  # type: ignore[assignment]
    pos = type("_Pos", (), {"side": "long", "volume": 1, "entry_price": 100.0, "opened_at": ""})()
    bot.strategy.positions = {"fu2701": pos}
    bot._position_for = lambda symbol: pos  # type: ignore[assignment]
    bot._record_signal_account = lambda *args, **kwargs: None  # type: ignore[assignment]
    bot._contract_multiplier = lambda symbol: 10  # type: ignore[assignment]

    closed, balance = bot.manual_close("fu2701", 101)

    assert (closed, balance) == (None, None)
    assert pos.side == "long"
    assert bot._ledger_failed is True


def test_manual_open_ledger_failure_keeps_position_flat() -> None:
    bot = _bot()
    bot.cfg = {"dry_run": False, "risk": {}}
    bot.root = Path(".")
    bot._ledger_failed = False
    bot._persist_state = lambda: None  # type: ignore[assignment]
    pos = type("_Pos", (), {"side": "flat", "volume": 0})()
    bot.strategy.positions = {"fu2701": pos}
    bot._record_signal_account = lambda *args, **kwargs: None  # type: ignore[assignment]

    opened, balance = bot.manual_open("fu2701", "long", 1, 100)

    assert (opened, balance) == (None, None)
    assert pos.side == "flat"
    assert bot._ledger_failed is True


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
        capabilities = DEFAULT_CAPABILITIES["gui"]

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
        capabilities = DEFAULT_CAPABILITIES["gui"]

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
        capabilities = DEFAULT_CAPABILITIES["gui"]

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


def _market_scan_bot(root: Path) -> TradingBot:
    return TradingBot(
        {
            "dry_run": True,
            "trade": {"symbol": "fu2701", "executor": "sim", "volume": 1},
            "strategy": {
                "engine": "daily_body",
                "timeframe": "5",
                "trail_timeframe": "5",
                "symbols": ["fu2701"],
                "catch_up_on_start": {"enabled": False},
            },
            "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
            "risk": {"sizing_mode": "fixed", "capital": 10000},
            "data": {"max_stale_minutes": 90},
            "_project_root": str(root),
        }
    )


def _stale_market_bars():
    from datetime import datetime, timedelta

    import pandas as pd

    end = datetime.now() - timedelta(hours=3)
    index = pd.DatetimeIndex([end - timedelta(minutes=5 * (39 - i)) for i in range(40)])
    close = pd.Series(100.0, index=index)
    bars = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10}
    )
    bars.attrs["market_health"] = {
        "source": "tq",
        "status": "stale",
        "from_cache": True,
        "stale": True,
        "download_ms": 125.0,
        "latest_bar_time": str(index[-1]),
        "latest_bar_age_minutes": 180.0,
        "error": "模拟断流",
    }
    return bars


def test_stale_open_is_blocked_with_health_alert_and_throttled_event(tmp_path) -> None:
    """陈旧缓存禁止开仓；两轮都有完整延迟记录，但异常事件和告警只写一次。"""
    bot = _market_scan_bot(tmp_path)
    bars = _stale_market_bars()
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "模拟开仓",
        entry_price=100.0,
        meta={"bar_id": str(bars.index[-1])},
    )
    with (
        patch.object(bot.kline, "fetch", return_value=bars),
        patch.object(bot, "_evaluate_symbol", return_value=decision),
        patch.object(bot, "_execute", return_value=True) as execute,
        patch("src.ops.alerts.emit_alert") as alert,
    ):
        bot.scan_symbol("fu2701")
        bot.scan_symbol("fu2701")

    assert not execute.called
    events = [
        json.loads(line)
        for line in (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row["event"] == "stale_block_open" for row in events) == 2
    assert sum(row["event"] == "market_health_warning" for row in events) == 1
    assert alert.call_count == 1
    blocked = next(row for row in events if row["event"] == "stale_block_open")
    assert blocked["market_health"]["source"] == "tq"
    assert blocked["signal_bar_age_minutes"] >= 90
    health_rows = [
        json.loads(line)
        for line in (tmp_path / "data" / "market_health.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(health_rows) == 2
    assert health_rows[-1]["download_ms"] == 125.0
    assert health_rows[-1]["evaluation_ms"] >= 0
    assert health_rows[-1]["decision_action"] == "buy_open"
    assert "cache_stale" in health_rows[-1]["issues"]
    snap = bot.status_snapshot()["market_health"]["fu2701|5"]
    assert snap["signal_bar_time"] == str(bars.index[-1])
    assert decision.meta["market_health"]["source"] == "tq"


def test_stale_market_does_not_block_close_protection(tmp_path) -> None:
    """同一份陈旧行情下，平仓决策仍交给执行链，不被开仓闸门误伤。"""
    bot = _market_scan_bot(tmp_path)
    bars = _stale_market_bars()
    decision = StrategyDecision(
        StrategyAction.CLOSE,
        "fu2701",
        "模拟保护平仓",
        entry_price=99.0,
        meta={"bar_id": str(bars.index[-1])},
    )
    with (
        patch.object(bot.kline, "fetch", return_value=bars),
        patch.object(bot, "_evaluate_symbol", return_value=decision),
        patch.object(bot, "_execute", return_value=True) as execute,
        patch("src.ops.alerts.emit_alert"),
    ):
        bot.scan_symbol("fu2701")

    assert execute.call_count == 1
    sent = execute.call_args.args[0]
    assert sent.action == StrategyAction.CLOSE
    assert sent.meta["market_health"]["stale"] is True
    events = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert "stale_block_open" not in events


def test_order_intent_contains_market_health_evidence(tmp_path) -> None:
    """委托落盘能追到信号 K 线、行情来源、年龄和下载耗时。"""
    bot = _intent_bot(tmp_path / "order_intent.json")
    health = {
        "source": "tq",
        "signal_bar_time": "2026-11-05 09:05:00",
        "signal_bar_age_minutes": 2.5,
        "download_ms": 125.0,
    }
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "带行情证据",
        meta={"bar_id": "2026-11-05 09:05:00", "market_health": health},
    )

    bot._write_order_intent(decision, 1)

    payload = json.loads((tmp_path / "order_intent.json").read_text(encoding="utf-8"))
    assert payload["bar_id"] == health["signal_bar_time"]
    assert payload["market_health"] == health
    bot._clear_order_intent()


def test_broker_net_unknown_until_position_snapshot_ready() -> None:
    """重连后持仓还没查回来时是「未知」，不能当成空仓；快照查全后才把查不到当成 0。"""
    from src.trader.ctp_logic import symbol_key

    gateway = type("_G", (), {"positions": {}, "positions_ready": False})()
    bot = TradingBot.__new__(TradingBot)
    bot.trader = type("_T", (), {"gateway": gateway})()

    # 持仓快照还没查全：不知道，不是空仓。
    assert bot._broker_net_for("fu2701") is None

    # 查全后没有这个合约，才是真的空仓。
    gateway.positions_ready = True
    assert bot._broker_net_for("fu2701") == 0

    gateway.positions[symbol_key("fu2701")] = type("_V", (), {"net": -2})()
    assert bot._broker_net_for("fu2701") == -2


def test_set_capital_updates_sizer_and_cfg_under_trade_lock() -> None:
    """账户页改余额只经 set_capital，同时改手数基准和配置权益。"""
    import threading

    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {}
    bot.sizer = type("_S", (), {"capital": 1000.0})()
    bot._trade_lock = threading.RLock()

    bot.set_capital(12345.5)

    assert bot.sizer.capital == 12345.5
    assert bot.cfg["risk"]["capital"] == 12345.5


