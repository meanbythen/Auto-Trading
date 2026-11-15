"""通道能力：新增「只能下单、不能查询」的通道，不改风控与调度也能跑通开平仓。"""

from __future__ import annotations

from src.domain.position import PositionState
from src.kernel.risk_chain import TradeIntent, run_pretrade_gates
from src.scheduler.runner import TradingBot
from src.strategy.base import StrategyBase
from src.strategy.types import StrategyAction, StrategyDecision
from src.trader.base import Capabilities, TraderBase, capabilities_for
from src.trader.base import OrderRequest, OrderResult


class _BlindTrader(TraderBase):
    """只能下单、不能查持仓/委托，也不确认成交。"""

    def __init__(self) -> None:
        self.calls: list[OrderRequest] = []

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.calls.append(req)
        return OrderResult(
            ok=True,
            message="已发出（本通道不查询、不确认成交）",
            order_id=f"blind-{len(self.calls)}",
            assumed_fill=True,
        )


class _Strategy(StrategyBase):
    def __init__(self) -> None:
        super().__init__()
        self.positions: dict[str, PositionState] = {}

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            pos.side = "long" if action == StrategyAction.BUY_OPEN else "short"
            pos.volume = volume
            pos.entry_price = price
            pos.initial_stop = float(initial_stop if initial_stop is not None else price)
        elif action == StrategyAction.CLOSE:
            if pos.reduce_close(volume):
                self.positions[symbol] = PositionState(symbol=symbol)


class _Risk:
    def __init__(self) -> None:
        self.positions: dict[str, float] = {}
        self.daily_pnl = 0.0
        self.halt_kind = ""

    def total_abs_position(self) -> int:
        return 0

    def allow(self, side: str, volume: int, **_kw: object) -> tuple[bool, str]:
        return True, ""

    def on_order_filled(self, side: str, volume: int, symbol: str = "") -> None:
        return None

    def set_daily_pnl(self, value: float) -> None:
        self.daily_pnl = float(value)

    def update_pnl(self, delta: float) -> None:
        self.daily_pnl += float(delta)

    def set_halt(self, kind: str, message: str) -> None:
        self.halt_kind = kind


class _Sizer:
    capital = 100000.0

    def multiplier_for(self, symbol: str) -> float:
        return 10.0


class _Kline:
    def fetch(self, symbol: str, period: str = "") -> None:
        return None


def _bot() -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {"dry_run": True, "risk": {}, "trade": {}}
    bot.strategy = _Strategy()
    bot.risk = _Risk()
    bot.sizer = _Sizer()
    bot.kline = _Kline()
    bot.trader = _BlindTrader()
    bot.symbols = ["fu2701"]
    bot.volume = 1
    bot.gui_assume_fill = True
    bot._sent_bar = {}
    bot.last_error = ""
    bot._size_open = lambda decision: (1, "")
    events: list[tuple[str, dict]] = []
    bot._record_event = lambda event, **kw: events.append((event, kw))
    bot._events = events  # type: ignore[attr-defined]
    return bot


def test_channel_capability_defaults() -> None:
    """三个内置通道各自声明能力；认不出来的通道按「只能下单」处理。"""
    assert capabilities_for("sim").confirms_fill is True
    assert capabilities_for("gui").window_driven is True
    assert capabilities_for("gui").confirms_fill is False

    ctp = capabilities_for("ctp")
    assert ctp.orders_via_broker is True
    assert ctp.can_query_positions and ctp.can_query_orders

    blind = capabilities_for("some_new_channel")
    assert blind == Capabilities()
    assert blind.orders_via_broker is False


def test_blind_channel_runs_open_and_close_with_assumed_fill() -> None:
    """只声明「不能查询」的通道，不用改风控/调度，就能开仓、平仓并按假定成交记账。"""
    bot = _bot()
    trader = bot.trader

    opened = bot._execute(
        StrategyDecision(
            StrategyAction.BUY_OPEN,
            "fu2701",
            "突破",
            entry_price=100.0,
            stop_price=90.0,
            meta={"bar_id": "2026-11-02 10:00:00"},
        )
    )
    assert opened is True
    assert len(trader.calls) == 1
    assert bot.strategy.positions["fu2701"].side == "long"

    closed = bot._execute(StrategyDecision(StrategyAction.CLOSE, "fu2701", "到点平仓"))
    assert closed is True
    assert len(trader.calls) == 2
    assert getattr(bot.strategy.positions.get("fu2701"), "side", "flat") in {"flat", None}

    orders = [kw for name, kw in bot._events if name == "order"]  # type: ignore[attr-defined]
    assert len(orders) == 2
    assert all(row["assumed_fill"] is True for row in orders)


def test_blind_channel_skips_broker_only_gates() -> None:
    """柜台才需要的阶段/未完成委托/对账闸门，不会拦住只下单通道。"""
    intent = TradeIntent(
        symbol="fu2701",
        side="buy_open",
        volume=1,
        stage_block="策略尚未进入实盘阶段",
        has_working_order=True,
        blocks_new_opens=True,
    )
    assert run_pretrade_gates(intent, _Risk()).ok is True

    broker = TradeIntent(
        symbol="fu2701",
        side="buy_open",
        volume=1,
        can_query_positions=True,
        can_query_orders=True,
        confirms_fill=True,
        stage_block="策略尚未进入实盘阶段",
    )
    assert run_pretrade_gates(broker, _Risk()).code == "order_blocked_stage"
