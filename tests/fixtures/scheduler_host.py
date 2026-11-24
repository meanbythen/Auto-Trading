"""scheduler 包的轻量 TradingBot 宿主测试夹具。

真实 ``TradingBot.__init__`` 会创建登录器、风控、仓位计算、交易通道和行情源，
单测里直接用成本极高。这里按项目既有做法（``_FlattenBot`` / ``_FillBot``）
把各 Mixin 组合成一个只依赖少数属性的宿主，配套 FakeTrader/FakeKline/FakeStrategy，
让下单、记账、登录、清仓、委托意图这些方法能被直接调用。

最小用法::

    from tests.fixtures.scheduler_host import LightHost, FakeCfg, make_position

    host = LightHost(FakeCfg(dry_run=True))
    host.strategy.positions["bu2612"] = make_position(side="long", volume=2, entry_price=100.0)
    row, balance = host.manual_close("bu2612", 110.0)
    assert row["symbol"] == "bu2612"

约定：
- 宿主不读真实 config/data 目录；需要落盘的用例把 ``root`` / ``intent_dir`` 指到 tmp_path。
- ``_record_event`` 只追加到 ``host.events``，``_record_audit`` 追加到 ``host.audits``。
- 告警一律走 ``src.ops.alerts.emit_alert``，用例里用 monkeypatch 拦截。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from src.scheduler.flatten_manager import FlattenManagerMixin
from src.scheduler.login_flow import LoginFlowMixin
from src.scheduler.order_executor import OrderExecutorMixin
from src.scheduler.session_audit import SessionAuditMixin
from src.trader.base import DEFAULT_CAPABILITIES


class FakeCfg(dict):
    """配置替身：普通 dict 即可，保留 dict 语义方便 ``setdefault`` / ``update``。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


def make_position(
    *,
    side: str = "flat",
    volume: int = 0,
    entry_price: float = 0.0,
    opened_at: str = "",
    initial_stop: float = 0.0,
) -> SimpleNamespace:
    """程序记账持仓替身，字段与 ``PositionState`` 对齐（可变属性）。"""
    return SimpleNamespace(
        side=side,
        volume=volume,
        entry_price=entry_price,
        opened_at=opened_at,
        initial_stop=initial_stop,
        trail_stop=initial_stop,
        trail_armed=initial_stop > 0,
    )


class FakeResult:
    """交易通道下单结果替身。"""

    def __init__(
        self,
        *,
        ok: bool = True,
        order_id: str = "ord-1",
        message: str = "ok",
        assumed_fill: bool = False,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.ok = ok
        self.order_id = order_id
        self.message = message
        self.assumed_fill = assumed_fill
        self.raw = raw if raw is not None else {}


class FakeGateway:
    """柜台网关替身：持仓、行情、暂停标志和成交笔数说明。"""

    def __init__(
        self,
        *,
        md: Any = None,
        positions: dict[str, Any] | None = None,
        positions_ready: bool = True,
        activity: Any = None,
        instrument: dict[str, Any] | None = None,
    ) -> None:
        self.md = md
        self.positions = positions if positions is not None else {}
        self.positions_ready = positions_ready
        self.activity = activity
        self._instrument = instrument
        self.trading_paused = False


class FakeTrader:
    """交易通道替身，记录下单/平仓/撤单调用。"""

    def __init__(
        self,
        *,
        caps: str | None = "sim",
        md: Any = None,
        gateway: FakeGateway | None = None,
        settings: Any = None,
        result: FakeResult | None = None,
        connect_ok: bool = True,
        connect_message: str = "ok",
        reconcile_report: Any = None,
        positions: dict[str, Any] | None = None,
    ) -> None:
        self.capabilities = DEFAULT_CAPABILITIES.get(caps or "", DEFAULT_CAPABILITIES["sim"])
        self.md = md
        if gateway is not None:
            self.gateway = gateway
        self.settings = settings
        self.positions = positions if positions is not None else {}
        self.last_broker_snapshot: dict[str, Any] | None = None
        self.last_broker_equity: float | None = None
        self._result = result or FakeResult()
        self._connect_ok = connect_ok
        self._connect_message = connect_message
        self._reconcile_report = reconcile_report
        self.place_calls: list[Any] = []
        self.buy_calls: list[tuple[str, int, Any]] = []
        self.sell_calls: list[tuple[str, int, Any]] = []
        self.close_calls: list[tuple[str, int, Any]] = []
        self.cancel_calls: list[tuple[str, int]] = []
        self.connect_calls = 0
        self.fill_handlers: list[Any] = []
        self.reconcile_calls = 0
        self.reconcile_unknown: list[str] = []

    # --- 通道能力便捷属性 ---------------------------------------------------
    @property
    def can_query_positions(self) -> bool:
        return bool(self.capabilities.can_query_positions)

    @property
    def can_query_orders(self) -> bool:
        return bool(self.capabilities.can_query_orders)

    # --- 生命周期 -----------------------------------------------------------
    def connect(self) -> tuple[bool, str]:
        self.connect_calls += 1
        return self._connect_ok, self._connect_message

    def reconcile(self) -> Any:
        self.reconcile_calls += 1
        return self._reconcile_report

    def mark_reconcile_unknown(self, reason: str) -> None:
        self.reconcile_unknown.append(reason)

    def set_fill_handler(self, handler: Any) -> None:
        self.fill_handlers.append(handler)

    def has_working_order(self, symbol: str | None = None) -> bool:
        return False

    def blocks_new_opens(self) -> bool:
        return False

    # --- 下单 ---------------------------------------------------------------
    def place_order(self, request: Any) -> FakeResult:
        self.place_calls.append(request)
        return self._result

    def order(self, symbol: str, side: str, volume: int, price: Any = None) -> FakeResult:
        self.place_calls.append((symbol, side, volume, price))
        return self._result

    def buy_open(self, symbol: str, volume: int, price: Any = None) -> FakeResult:
        self.buy_calls.append((symbol, volume, price))
        return self._result

    def sell_open(self, symbol: str, volume: int, price: Any = None) -> FakeResult:
        self.sell_calls.append((symbol, volume, price))
        return self._result

    def close(self, symbol: str, volume: int, price: Any = None) -> FakeResult:
        self.close_calls.append((symbol, volume, price))
        return self._result

    def close_position(self, symbol: str, volume: int, price: Any = None) -> FakeResult:
        return self.close(symbol, volume, price)

    def cancel(self, symbol: str, order_id: int = 0) -> FakeResult:
        self.cancel_calls.append((symbol, order_id))
        return self._result


class FakeKline:
    """行情源替身：``fetch`` 返回带 DatetimeIndex 的 DataFrame。"""

    def __init__(
        self,
        *,
        latest: float = 100.0,
        frame: pd.DataFrame | None = None,
        raise_latest: bool = False,
        raise_fetch: bool = False,
    ) -> None:
        self._latest = latest
        self._frame = frame if frame is not None else bars_frame()
        self._raise_latest = raise_latest
        self._raise_fetch = raise_fetch
        self.fetched: list[tuple[str, str]] = []

    def latest_price(self, symbol: str) -> float:
        if self._raise_latest:
            raise RuntimeError("quote down")
        return self._latest

    def fetch(self, symbol: str, period: str = "5") -> pd.DataFrame | None:
        self.fetched.append((symbol, period))
        if self._raise_fetch:
            raise RuntimeError("kline down")
        return self._frame


def bars_frame(*, close: float = 100.0, periods: int = 3) -> pd.DataFrame:
    """构造一段已收盘K线，索引是分钟时间戳。"""
    index = pd.date_range("2026-11-24 09:30", periods=periods, freq="5min")
    return pd.DataFrame(
        {
            "open": [close] * periods,
            "high": [close + 1] * periods,
            "low": [close - 1] * periods,
            "close": [close] * periods,
        },
        index=index,
    )


class FakeStrategy:
    """策略替身：持仓、信号标记、成交回调都记录到 ``calls``。"""

    def __init__(
        self,
        *,
        symbols: list[str] | None = None,
        positions: dict[str, Any] | None = None,
        timeframe: str = "5",
        flatten_at_close: bool = False,
        flatten_times: list[Any] | None = None,
        raise_trade_closed: bool = False,
        raise_factor_pnl: bool = False,
        raise_clock: bool = False,
    ) -> None:
        self.symbols = list(symbols or ["bu2612"])
        self.positions = positions if positions is not None else {}
        self.timeframe = timeframe
        self.flatten_at_close = flatten_at_close
        self.flatten_times = list(flatten_times or [])
        self.name = "fake"
        self._pending: dict[str, Any] = {}
        self._raise_trade_closed = raise_trade_closed
        self._raise_factor_pnl = raise_factor_pnl
        self._raise_clock = raise_clock
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.last_entry_bars: dict[str, str] = {}
        self.cleared_bars: list[tuple[str, str]] = []
        self.note_flat_calls: list[tuple[str, str]] = []
        self.signaled_bars: list[tuple[str, str]] = []

    def _log(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def on_filled(self, symbol: str, action: Any, price: float, volume: int, **kwargs: Any) -> None:
        self._log("on_filled", symbol, action, price, volume, **kwargs)

    def on_trade_closed(self, symbol: str, pnl: float, **kwargs: Any) -> None:
        self._log("on_trade_closed", symbol, pnl, **kwargs)
        if self._raise_trade_closed:
            raise RuntimeError("trade closed hook boom")

    def note_factor_pnl(self, symbol: str, pnl: float, **kwargs: Any) -> None:
        self._log("note_factor_pnl", symbol, pnl, **kwargs)
        if self._raise_factor_pnl:
            raise RuntimeError("factor pnl boom")

    def get_last_entry_bar(self, symbol: str) -> str:
        return str(self.last_entry_bars.get(symbol) or "")

    def get_last_entry_bars(self) -> dict[str, str]:
        return dict(self.last_entry_bars)

    def set_last_entry_bar(self, symbol: str, bar_id: str) -> None:
        self.last_entry_bars[symbol] = bar_id
        self._log("set_last_entry_bar", symbol, bar_id)

    def clear_signaled_bar(self, symbol: str, *, expected_bar_id: str = "") -> bool:
        self.cleared_bars.append((symbol, expected_bar_id))
        self._log("clear_signaled_bar", symbol, expected_bar_id)
        return True

    def set_signaled_bar(self, symbol: str, bar_id: str) -> None:
        self.signaled_bars.append((symbol, bar_id))

    def note_flat_wait_next_bar(self, symbol: str, bar_id: str) -> None:
        self.note_flat_calls.append((symbol, bar_id))

    def restore_last_entry_bars(self, raw: dict[str, str]) -> None:
        self.last_entry_bars.update(raw)

    def bind_symbol_sessions(self, symbol: str) -> None:
        self._log("bind_symbol_sessions", symbol)

    def is_flatten_time(self, when: Any = None) -> bool:
        if self._raise_clock:
            raise RuntimeError("clock boom")
        return True

    def evaluate(self, symbol: str, bars: Any, **kwargs: Any) -> Any:
        self._log("evaluate", symbol, **kwargs)
        return None


class FakeRisk:
    """风控替身：净仓、当日盈亏、熔断状态。"""

    def __init__(self) -> None:
        self.positions: dict[str, int] = {}
        self.daily_pnl = 0.0
        self.halted = False
        self.halt_kind = ""
        self.halt_reason = ""
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def total_abs_position(self) -> int:
        return sum(abs(int(value)) for value in self.positions.values())

    def on_order_filled(self, side: str, volume: int, **kwargs: Any) -> None:
        self.calls.append(("on_order_filled", (side, volume), kwargs))

    def update_pnl(self, pnl: float) -> None:
        self.daily_pnl += float(pnl)
        self.calls.append(("update_pnl", (pnl,), {}))

    def set_daily_pnl(self, pnl: float) -> None:
        self.daily_pnl = float(pnl)
        self.calls.append(("set_daily_pnl", (pnl,), {}))

    def set_halt(self, kind: str, reason: str = "") -> None:
        self.halted = True
        self.halt_kind = kind
        self.halt_reason = reason
        self.calls.append(("set_halt", (kind, reason), {}))

    def clear_halt(self, kind: str | None = None) -> None:
        self.halted = False
        self.halt_kind = ""
        self.halt_reason = ""
        self.calls.append(("clear_halt", (kind,), {}))

    def save_state(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("save_state", args, kwargs))

    def allow(self, side: str, volume: int, **kwargs: Any) -> tuple[bool, str]:
        return True, ""


class FakeSizer:
    """仓位计算替身。"""

    def __init__(self, *, capital: float = 100000.0, multiplier: float = 10.0, volume: int = 1) -> None:
        self.capital = capital
        self._multiplier = multiplier
        self._volume = volume
        self.reason = "sized"
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def multiplier_for(self, symbol: str) -> float:
        return self._multiplier

    def calc_open(self, symbol: str, entry: float, stop: float, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("calc_open", (symbol, entry, stop), kwargs))
        return SimpleNamespace(volume=self._volume, reason=self.reason)


class FakeLoginManager:
    """登录器替身。"""

    def __init__(self, *, login_ok: bool = True, window_exists: bool = True) -> None:
        self._login_ok = login_ok
        self._window_exists = window_exists
        self.login_calls = 0

    def login(self) -> bool:
        self.login_calls += 1
        return self._login_ok

    def window_exists(self) -> bool:
        return self._window_exists


class LightHost(SessionAuditMixin, LoginFlowMixin, OrderExecutorMixin, FlattenManagerMixin):
    """把 scheduler 各 Mixin 组合起来的最小宿主。"""

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        *,
        trader: FakeTrader | None = None,
        strategy: FakeStrategy | None = None,
        kline: FakeKline | None = None,
        risk: FakeRisk | None = None,
        sizer: FakeSizer | None = None,
        login_mgr: FakeLoginManager | None = None,
        root: Path | str | None = None,
        symbols: list[str] | None = None,
        scan_symbols: list[str] | None = None,
        sessions: list[list[str]] | None = None,
        gui_assume_fill: bool = True,
        multi_enabled: bool = False,
        intent_dir: Path | None = None,
        intent_path: Path | None = None,
        order_timeline: Any = None,
    ) -> None:
        self.cfg = FakeCfg(
            dry_run=True,
            trade={"executor": "sim"},
            risk={"contracts": {}},
            schedule={},
            runtime={},
        )
        if cfg:
            self.cfg.update(cfg)
        self.root = Path(root) if root is not None else Path(".")
        self.strategy = strategy or FakeStrategy(symbols=list(symbols or ["bu2612"]))
        self.trader = trader or FakeTrader()
        self.kline = kline or FakeKline()
        self.risk = risk or FakeRisk()
        self.sizer = sizer or FakeSizer()
        self.login_mgr = login_mgr or FakeLoginManager()

        self.symbols = list(symbols or self.strategy.symbols or ["bu2612"])
        self.default_symbol = self.symbols[0]
        self.scan_symbols = list(scan_symbols or self.symbols)
        self.volume = 1
        self.sessions = list(sessions or [])
        self.gui_assume_fill = bool(gui_assume_fill)

        # --- 运行状态标志 -------------------------------------------------
        self.logged_in = False
        self.last_error = ""
        self.run_id = "test-run"
        self.trading_day = "2026-11-24"
        self._gui_price_ready = True
        self._ctp_retry_at: datetime | None = None
        self._orders_paused = False
        self._ledger_failed = False
        self._order_dispatch_started = False
        self._order_unclean = False
        self._sent_bar: dict[str, str] = {}
        self._health_blocked = False
        self._health_block_reason = ""
        self._dynamic_note = ""
        self._last_signal_bars: dict[str, Any] = {}
        self._trade_lock = None
        self._order_timeline = order_timeline
        self._close_attempt_at: dict[str, float] = {}
        self._close_chase_ticks: dict[str, int] = {}
        self._recent_fill_keys = None

        # --- 船载/清仓状态 ------------------------------------------------
        self._own_fills_at: dict[str, float] = {}
        self._own_fill_kinds: dict[str, str] = {}
        self._flatten_try: dict[str, float] = {}
        self._flatten_attempts: dict[str, int] = {}
        self._flatten_sent: set[str] = set()
        self._flatten_pending = False
        self._flatten_retry_sec = 10.0
        self.holiday_calendar = None
        self._multi_enabled = bool(multi_enabled)
        self._multi_symbols: list[str] = []

        # --- 对账/委托意图 ------------------------------------------------
        self._frozen_symbols: set[str] = set()
        self._frozen_noted: set[str] = set()
        self._reconcile_state: dict[str, Any] = {}
        self._phantom_rounds: dict[str, int] = {}
        self._startup_reconcile_pending = False
        self._pending_intent: dict[str, Any] | None = None
        self._pending_intents: list[dict[str, Any]] | None = None
        self._pending_intent_files: list[Path] | None = None
        self.intent_dir = Path(intent_dir) if intent_dir is not None else None
        self.intent_path = Path(intent_path) if intent_path is not None else None

        # --- 审计 ----------------------------------------------------------
        self._last_audit_at: datetime | None = None
        self._audit_thread: Any = None

        # --- 记录器 --------------------------------------------------------
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.audits: list[tuple[str, dict[str, Any]]] = []
        self.persisted = 0
        self._reconcile_report: Any = None
        self._applied_reconcile: list[tuple[Any, str]] = []
        self._symbol_sessions: dict[str, list[list[str]]] = {}
        self._symbol_flatten_times: dict[str, list[str]] = {}

    # --- 记录器 -------------------------------------------------------------
    def _record_event(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def _record_audit(self, *args: Any, **kwargs: Any) -> bool:
        self.audits.append((str(args[0]) if args else "", kwargs))
        return True

    def _persist_state(self) -> None:
        self.persisted += 1

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    # --- runner 里的共享方法（Mixin 会调用）---------------------------------
    def _position_for(self, symbol: str) -> Any | None:
        positions = getattr(self.strategy, "positions", {}) or {}
        if symbol in positions:
            return positions.get(symbol)
        key = next(
            (name for name in positions if str(name).lower() == str(symbol).lower()),
            None,
        )
        return positions.get(key) if key is not None else None

    def _symbol_is_flat(self, symbol: str) -> bool:
        pos = self._position_for(symbol)
        if pos is None:
            return True
        side = str(getattr(pos, "side", "flat") or "flat")
        volume = int(getattr(pos, "volume", 0) or 0)
        return side == "flat" or volume <= 0

    def _strategy_for(self, symbol: str) -> Any:
        return self.strategy

    def _refresh_scan_symbols(self) -> None:
        return None

    def _multi_symbol_enabled(self) -> bool:
        return bool(getattr(self, "_multi_enabled", False))

    def _symbol_sessions_for(self, symbol: str) -> list[list[str]]:
        sessions = self._symbol_sessions.get(symbol)
        return list(sessions) if sessions is not None else list(self.sessions)

    def _symbol_flatten_clocks(self, symbol: str) -> list[str]:
        return list(self._symbol_flatten_times.get(symbol, []))

    def _symbol_flatten_due(self, symbol: str, now: datetime | None = None) -> bool:
        return False

    def _any_flatten_due(self, now: datetime | None = None) -> bool:
        return False

    def _any_symbol_in_session(self, symbols: list[str], now: datetime | None = None) -> bool:
        return True

    # --- 登录相关钩子 -------------------------------------------------------
    def _call_reconcile(self) -> Any:
        return self.trader.reconcile()

    def _apply_reconcile(self, report: Any, *, phase: str = "runtime") -> None:
        self._applied_reconcile.append((report, phase))
