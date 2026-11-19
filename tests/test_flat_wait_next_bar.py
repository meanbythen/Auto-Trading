"""人工平仓后等下一根信号K线，不要立刻补仓。"""

from src.scheduler.runner import TradingBot
from src.strategy.factory import create_strategy
from src.strategy.types import StrategyAction, StrategyDecision


def _cfg() -> dict:
    return {
        "_project_root": ".",
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "15",
            "symbols": ["lh2701"],
            "flatten_at_close": True,
        },
        "trade": {"executor": "sim", "volume": 1, "symbol": "lh2701"},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "dry_run": True,
    }


def test_release_mark_keeps_bar_after_manual_close() -> None:
    """刚平完再拦开仓时，不能撤掉「本根已处理」，否则会立刻补空。"""
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = create_strategy(_cfg())
    bot.strategy.note_flat_wait_next_bar("lh2701", "2026-11-19 14:30:00")
    bot._own_fill_too_fresh = lambda _symbol, **_kw: True  # type: ignore[method-assign]
    bot._own_fill_kind = lambda _symbol: "close"  # type: ignore[method-assign]
    decision = StrategyDecision(
        StrategyAction.SELL_OPEN,
        "lh2701",
        "多因子做空",
        meta={"bar_id": "2026-11-19 14:30:00"},
    )
    bot._release_signal_mark(decision)
    assert bot.strategy.get_signaled_bar("lh2701") == "2026-11-19 14:30:00"


def test_failed_open_still_retries_when_not_just_closed() -> None:
    """普通下单失败仍可撤标记，同一根还能再试。"""
    bot = TradingBot.__new__(TradingBot)
    bot.strategy = create_strategy(_cfg())
    bot.strategy.set_signaled_bar("lh2701", "2026-11-19 14:30:00")
    bot._own_fill_too_fresh = lambda _symbol, **_kw: False  # type: ignore[method-assign]
    decision = StrategyDecision(
        StrategyAction.SELL_OPEN,
        "lh2701",
        "多因子做空",
        meta={"bar_id": "2026-11-19 14:30:00"},
    )
    bot._release_signal_mark(decision)
    assert bot.strategy.get_signaled_bar("lh2701") == ""
