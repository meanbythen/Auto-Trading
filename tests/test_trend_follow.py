"""趋势跟随：收盘冲破最近一段才进，止损只收紧，短通道走坏或到收盘就走。"""

from datetime import datetime

import pandas as pd

from src.strategy.trend_follow import TrendFollowStrategy
from src.strategy.types import StrategyAction


def _cfg() -> dict:
    return {
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "5",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "trend_follow": {
                "entry_bars": 5,
                "exit_bars": 3,
                "atr_period": 3,
                "atr_mult": 2.0,
            },
        },
        "trade": {"volume": 1, "symbol": "bu2612"},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def _frame(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """每行是最高、最低、收盘。时间从夜盘开始，每根 5 分钟。"""
    index = pd.date_range("2026-10-20 21:00", periods=len(rows), freq="5min")
    frame = pd.DataFrame(rows, columns=["high", "low", "close"], index=index)
    frame["open"] = frame["close"]
    frame["volume"] = 100
    return frame


def _quiet(n: int, close: float = 100.0) -> list[tuple[float, float, float]]:
    return [(close + 1, close - 1, close)] * n


def test_breakout_close_opens_long() -> None:
    """收盘升破前几根最高价才做多，止损在进场价下面。"""
    bars = _frame(_quiet(8) + [(110, 90, 108)])
    strategy = TrendFollowStrategy(_cfg())
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 21, 40))
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.stop_price is not None
    assert decision.stop_price < 108


def test_close_inside_channel_holds() -> None:
    """收盘还在最近这段高低点里面，不开仓。"""
    bars = _frame(_quiet(9))
    strategy = TrendFollowStrategy(_cfg())
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 21, 45))
    assert decision.action == StrategyAction.HOLD


def test_entry_bar_wick_does_not_stop() -> None:
    """开仓这根自己的下影线不算已经打到止损。"""
    bars = _frame(_quiet(8) + [(110, 90, 108)])
    strategy = TrendFollowStrategy(_cfg())
    now = datetime(2026, 10, 20, 21, 40)
    decision = strategy.evaluate("bu2612", bars, now=now)
    assert decision.action == StrategyAction.BUY_OPEN
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 108, 1, decision.stop_price)
    again = strategy.evaluate("bu2612", bars, now=now)
    assert again.action == StrategyAction.HOLD


def test_stop_only_tightens_then_exit_channel_closes() -> None:
    """价格继续往上时止损跟着上移，不会放宽。收盘跌破短通道就平仓。"""
    rows = _quiet(8) + [(110, 106, 108)]
    bars = _frame(rows)
    strategy = TrendFollowStrategy(_cfg())
    opened = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 21, 40))
    assert opened.action == StrategyAction.BUY_OPEN
    first_stop = float(opened.stop_price)
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 108, 1, first_stop)

    higher = _frame(rows + [(120, 112, 116)])
    held = strategy.evaluate("bu2612", higher, now=datetime(2026, 10, 20, 21, 45))
    assert held.action == StrategyAction.HOLD
    assert held.stop_price is not None
    assert held.stop_price >= first_stop

    # 再往上一点，确认止损不会因为回调里的计算被放宽。
    higher2 = _frame(rows + [(120, 112, 116), (122, 114, 118)])
    held2 = strategy.evaluate("bu2612", higher2, now=datetime(2026, 10, 20, 21, 50))
    assert held2.action == StrategyAction.HOLD
    assert float(held2.stop_price) >= float(held.stop_price)

    # 收盘跌破最近 3 根的低点，这段趋势算走完。
    broken = _frame(rows + [(120, 112, 116), (122, 114, 118), (112, 100, 104)])
    closed = strategy.evaluate("bu2612", broken, now=datetime(2026, 10, 20, 21, 55))
    assert closed.action == StrategyAction.CLOSE


def test_session_bell_does_not_flatten() -> None:
    """默认到了 22:55 也不平。总开关开着也不影响这一套。"""
    rows = _quiet(8) + [(110, 106, 108)]
    bars = _frame(rows)
    strategy = TrendFollowStrategy(_cfg())
    assert strategy.flatten_at_close is False
    opened = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 21, 40))
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 108, 1, opened.stop_price)
    held = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 22, 55))
    assert held.action == StrategyAction.HOLD


def test_own_flatten_switch_still_closes() -> None:
    """只有把这一套自己的开关打开，到点才会平。"""
    cfg = _cfg()
    cfg["strategy"]["trend_follow"]["flatten_at_close"] = True
    rows = _quiet(8) + [(110, 106, 108)]
    bars = _frame(rows)
    strategy = TrendFollowStrategy(cfg)
    opened = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 21, 40))
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 108, 1, opened.stop_price)
    closed = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 22, 55))
    assert closed.action == StrategyAction.CLOSE
    assert "收盘" in closed.reason


def test_daily_bar_at_midnight_can_open() -> None:
    """日线时间是 00:00，不能被当成非交易时段而错过突破。"""
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "D"
    bars = _frame(_quiet(8) + [(110, 90, 108)])
    strategy = TrendFollowStrategy(cfg)
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 0, 0))
    assert decision.action == StrategyAction.BUY_OPEN


def test_restart_keeps_the_farthest_price() -> None:
    """关掉程序再打开，隔夜之后还记得持仓以来走得最远的价格。"""
    from src.utils.state_store import restore_positions, snapshot_positions

    strategy = TrendFollowStrategy(_cfg())
    strategy.positions["bu2612"].side = "long"
    strategy.positions["bu2612"].volume = 1
    strategy.positions["bu2612"].entry_price = 100
    strategy.positions["bu2612"].initial_stop = 90
    strategy.positions["bu2612"].trail_stop = 96
    strategy._extreme["bu2612"] = 120

    fresh = TrendFollowStrategy(_cfg())
    restore_positions(fresh, snapshot_positions(strategy))
    assert fresh.positions["bu2612"].trail_stop == 96
    assert fresh._extreme["bu2612"] == 120


def test_breakout_close_opens_short() -> None:
    """收盘跌破前几根最低价就做空，止损在进场价上面。"""
    bars = _frame(_quiet(8) + [(110, 90, 92)])
    strategy = TrendFollowStrategy(_cfg())
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 20, 21, 40))
    assert decision.action == StrategyAction.SELL_OPEN
    assert decision.stop_price is not None
    assert decision.stop_price > 92
