"""内核分层：风控只拦不发单，事件互不改对方的字典。"""

from src.kernel.context import BarSnapshot
from src.kernel.events import EventBus, SIGNAL
from src.kernel.risk_chain import TradeIntent, run_pretrade_gates
from src.risk.guardian import RiskGuardian
from src.strategy.types import StrategyAction, StrategyDecision


def test_event_payload_is_copied() -> None:
    """一个订阅者改了字典，另一个拿到的还是发出去时的值。"""
    bus = EventBus()
    seen: list[dict] = []

    def one(event):
        event.data["mutated"] = True  # type: ignore[index]
        seen.append(dict(event.data))

    def two(event):
        seen.append(dict(event.data))

    bus.subscribe(SIGNAL, one)
    bus.subscribe(SIGNAL, two)
    bus.publish(SIGNAL, {"symbol": "bu2612"})
    assert seen[0].get("mutated") is True
    assert "mutated" not in seen[1]
    assert seen[1]["symbol"] == "bu2612"


def test_wait_next_bar_blocks_open_for_every_engine() -> None:
    """平仓后等下一根：拦在公共风控链，不依赖某一套策略自己判断。"""
    g = RiskGuardian(min_order_interval_sec=0)
    intent = TradeIntent(
        symbol="lh2701",
        side="sell_open",
        volume=1,
        bar_id="2026-11-19 14:30:00",
        bar_consumed=True,
    )
    result = run_pretrade_gates(intent, g)
    assert result.ok is False
    assert result.code == "order_wait_next_bar"


def test_wait_next_bar_does_not_block_close() -> None:
    """止损和清仓不受「等下一根」影响。"""
    g = RiskGuardian(min_order_interval_sec=0)
    g.on_order_filled("sell_open", 1, "lh2701")
    intent = TradeIntent(
        symbol="lh2701",
        side="close",
        volume=1,
        bar_id="2026-11-19 14:30:00",
        bar_consumed=True,
        force=True,
    )
    result = run_pretrade_gates(intent, g)
    assert result.ok is True


def test_pause_blocks_before_risk_limit() -> None:
    """暂停报单时风控链拦住开新仓，执行器不该再去组报单。"""
    g = RiskGuardian(min_order_interval_sec=0)
    intent = TradeIntent(symbol="bu2612", side="buy_open", volume=1, orders_paused=True)
    result = run_pretrade_gates(intent, g)
    assert result.ok is False
    assert result.code == "order_paused"


def test_pause_does_not_block_close() -> None:
    """暂停只拦开新仓，止损和清仓仍要通过。"""
    g = RiskGuardian(min_order_interval_sec=0)
    g.on_order_filled("buy_open", 1, "bu2612")
    intent = TradeIntent(symbol="bu2612", side="close", volume=1, orders_paused=True)
    result = run_pretrade_gates(intent, g)
    assert result.ok is True


def test_close_with_no_lots_is_blocked() -> None:
    """账上没仓时风控链拦住平仓，避免执行器拿配置手数去平。"""
    g = RiskGuardian(min_order_interval_sec=0)
    intent = TradeIntent(symbol="bu2612", side="close", volume=0)
    result = run_pretrade_gates(intent, g)
    assert result.ok is False
    assert result.code == "close_skip_no_position"


def test_guardian_still_blocks_daily_halt() -> None:
    """日亏损熔断仍在风控链里，策略信号不会直接变成单。"""
    g = RiskGuardian(min_order_interval_sec=0, max_daily_loss=10)
    g.set_daily_pnl(-20)
    intent = TradeIntent(symbol="bu2612", side="buy_open", volume=1)
    result = run_pretrade_gates(intent, g)
    assert result.ok is False
    assert result.code == "risk_block"


def test_force_close_still_goes_through_when_guardian_has_no_lots() -> None:
    """收盘清仓带 force 时，即使风控净仓没跟上，也要能平。"""
    g = RiskGuardian(min_order_interval_sec=0, max_daily_loss=10)
    g.set_daily_pnl(-20)
    intent = TradeIntent(symbol="bu2612", side="close", volume=1, force=True)
    result = run_pretrade_gates(intent, g)
    assert result.ok is True


def test_pass_open_when_risk_allows() -> None:
    g = RiskGuardian(min_order_interval_sec=0)
    intent = TradeIntent(symbol="bu2612", side="buy_open", volume=1)
    result = run_pretrade_gates(intent, g)
    assert result.ok is True


def test_stage_block_only_stops_new_opens() -> None:
    """上线阶段未批准只拦新开仓；止损、反手和收盘清仓必须放行。"""

    class _Risk:
        def allow(self, side: str, volume: int, symbol: str = "") -> tuple[bool, str]:
            return True, ""

    blocked = TradeIntent(
        symbol="bu2612",
        side="buy_open",
        volume=1,
        can_query_positions=True,
        can_query_orders=True,
        confirms_fill=True,
        stage_block="策略尚未进入实盘阶段",
    )
    closing = TradeIntent(
        symbol="bu2612",
        side="close",
        volume=1,
        can_query_positions=True,
        can_query_orders=True,
        confirms_fill=True,
        stage_block="策略尚未进入实盘阶段",
    )

    assert run_pretrade_gates(blocked, _Risk()).code == "order_blocked_stage"
    assert run_pretrade_gates(closing, _Risk()).ok is True


def test_bar_snapshot_does_not_share_columns() -> None:
    import pandas as pd

    raw = pd.DataFrame({"close": [1.0, 2.0]})
    snap = BarSnapshot.from_bars("bu2612", "5", raw)
    assert snap is not None
    snap.bars["close"] = 0
    assert float(raw["close"].iloc[-1]) == 2.0


def test_signal_event_does_not_change_decision() -> None:
    bus = EventBus()
    got = []
    bus.subscribe(SIGNAL, lambda e: got.append(e.data["action"]))
    decision = StrategyDecision(StrategyAction.BUY_OPEN, "bu2612", "测")
    bus.publish(SIGNAL, {"action": decision.action.value})
    assert decision.action == StrategyAction.BUY_OPEN
    assert got == ["buy_open"]
