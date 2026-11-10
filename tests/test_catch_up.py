"""
启动回溯补开仓的三道门：时间、ATR 偏离、止损路径。
不连真实行情，只用假 K 线和假策略探测结果。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

from src.scheduler.catch_up import (
    CatchUpSettings,
    find_catch_up_open,
    stop_was_touched,
)
from src.strategy.types import StrategyAction, StrategyDecision


def _bars(end: datetime, n: int = 40, step_min: int = 5, base: float = 100.0) -> pd.DataFrame:
    """造一段足够算 ATR 的假 K 线。最后一根收在 end。"""
    idx = [end - timedelta(minutes=step_min * (n - 1 - i)) for i in range(n)]
    rows = []
    price = base
    for i in range(n):
        # 轻微波动，方便 ATR > 0，又不会乱扫止损。
        high = price + 1.0
        low = price - 1.0
        close = price + (0.2 if i % 2 == 0 else -0.2)
        rows.append({"open": price, "high": high, "low": low, "close": close, "volume": 100})
        price = close
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_stop_was_touched_long_and_short() -> None:
    frame = pd.DataFrame(
        {
            "high": [105.0, 106.0],
            "low": [99.0, 98.0],
        }
    )
    assert stop_was_touched(side="long", stop_price=98.5, after_signal=frame) is True
    assert stop_was_touched(side="long", stop_price=90.0, after_signal=frame) is False
    assert stop_was_touched(side="short", stop_price=105.5, after_signal=frame) is True
    assert stop_was_touched(side="short", stop_price=110.0, after_signal=frame) is False


def test_catch_up_rejects_when_signal_too_old() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    closed = _bars(now - timedelta(minutes=25), n=40)
    settings = CatchUpSettings(enabled=True, lookback_bars=3, max_age_minutes=20)

    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓",
        entry_price=100.0,
        stop_price=95.0,
    )

    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_rejects_when_price_moved_too_far() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    closed = _bars(now - timedelta(minutes=5), n=40, base=100.0)
    # 把现价（最后收盘）拉得很远，超过 0.5 ATR。
    closed = closed.copy()
    closed.iloc[-1, closed.columns.get_loc("close")] = 200.0
    closed.iloc[-1, closed.columns.get_loc("high")] = 201.0

    settings = CatchUpSettings(
        enabled=True, lookback_bars=3, max_age_minutes=20, max_entry_atr=0.5, atr_period=14
    )
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓",
        entry_price=100.0,
        stop_price=90.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_rejects_when_stop_already_hit() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    # 信号在倒数第 2 根；最后一根把最低打穿止损。
    closed = _bars(now - timedelta(minutes=5), n=40, base=100.0)
    closed = closed.copy()
    signal_pos = len(closed) - 2
    closed.iloc[signal_pos, closed.columns.get_loc("close")] = 100.0
    closed.iloc[-1, closed.columns.get_loc("low")] = 90.0
    closed.iloc[-1, closed.columns.get_loc("high")] = 101.0
    closed.iloc[-1, closed.columns.get_loc("close")] = 100.5

    settings = CatchUpSettings(enabled=True, lookback_bars=3, max_age_minutes=20)
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓",
        entry_price=float(closed.iloc[signal_pos]["close"]),
        stop_price=95.0,
    )

    def _probe(_cfg, _symbol, prefix, *_a, **_k):
        # 只在截到倒数第 2 根时给出开仓，模拟「最新一根没信号、前一根有」。
        if len(prefix) == signal_pos + 1:
            return open_decision
        return None

    with patch("src.scheduler.catch_up._probe_open", side_effect=_probe):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_hit_when_all_gates_pass() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    closed = _bars(now - timedelta(minutes=5), n=40, base=100.0)
    settings = CatchUpSettings(
        enabled=True, lookback_bars=3, max_age_minutes=20, max_entry_atr=0.5, atr_period=14
    )
    entry = float(closed.iloc[-1]["close"])
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓仍有效",
        entry_price=entry,
        stop_price=entry - 10.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is not None
    assert hit.decision.action == StrategyAction.BUY_OPEN
    assert len(hit.prefix) == len(closed)


def test_catch_up_settings_from_cfg_defaults() -> None:
    s = CatchUpSettings.from_cfg({})
    assert s.enabled is True
    assert s.lookback_bars == 3
    assert s.max_age_minutes == 20
    assert s.max_entry_atr == 0.5
    assert s.max_chase_atr == 2.0

    s2 = CatchUpSettings.from_cfg(
        {"strategy": {"catch_up_on_start": {"enabled": False, "lookback_bars": 1}}}
    )
    assert s2.enabled is False
    assert s2.lookback_bars == 1

    s3 = CatchUpSettings.from_cfg(
        {"strategy": {"catch_up_on_start": {"max_chase_atr": "bad"}}}
    )
    assert s3.max_chase_atr == 2.0


def test_catch_up_rejects_chasing_big_bar_end() -> None:
    """11-05 夜盘：4216→4308 那根收在 4288，止损只差 60 点，补开就是贴着止损进场。"""
    now = datetime(2026, 11, 5, 21, 10, 30)
    closed = _bars(now - timedelta(minutes=0), n=40, base=100.0)
    closed = closed.copy()
    # 最后一根做成大阳线：低点 90，收在 108，振幅远大于 ATR。
    last = len(closed) - 1
    closed.iloc[last, closed.columns.get_loc("low")] = 90.0
    closed.iloc[last, closed.columns.get_loc("close")] = 108.0
    closed.iloc[last, closed.columns.get_loc("high")] = 108.5

    settings = CatchUpSettings(
        enabled=True,
        lookback_bars=1,
        max_age_minutes=20,
        max_entry_atr=0.5,
        max_chase_atr=1.0,
        atr_period=14,
    )
    entry = float(closed.iloc[last]["close"])
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "假开仓追高",
        entry_price=entry,
        stop_price=entry - 10.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="fu2701",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_allows_normal_bar() -> None:
    """普通振幅的 K 线不能被追高门误杀。"""
    now = datetime(2026, 11, 5, 21, 10, 30)
    closed = _bars(now - timedelta(minutes=0), n=40, base=100.0)
    closed = closed.copy()
    last = len(closed) - 1
    entry = float(closed.iloc[last]["close"])
    # 收盘价距最低只有 2 元，远小于 2×ATR。
    closed.iloc[last, closed.columns.get_loc("low")] = entry - 2.0
    closed.iloc[last, closed.columns.get_loc("high")] = entry + 0.5

    settings = CatchUpSettings(
        enabled=True,
        lookback_bars=1,
        max_age_minutes=20,
        max_entry_atr=0.5,
        max_chase_atr=2.0,
        atr_period=14,
    )
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "假开仓正常",
        entry_price=entry,
        stop_price=entry - 10.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="fu2701",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is not None


def test_forming_bar_is_not_used_as_now() -> None:
    """持仓时拼进来的那根写着 22:55，但它还没走完；拿它当现在会在 22:50 就触发清仓。"""
    from src.scheduler.runner import _eval_now

    index = pd.DatetimeIndex(["2026-10-23 22:45", "2026-10-23 22:50"])
    close = pd.Series(5400.0, index=index)
    closed = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 10}
    )
    forming = pd.DataFrame(
        {
            "open": [5400.0],
            "high": [5401.0],
            "low": [5399.0],
            "close": [5400.0],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex(["2026-10-23 22:55"]),
    )
    eval_bars = pd.concat([closed, forming])
    now = _eval_now(eval_bars, closed, catch_up=False)
    assert now is not None
    assert abs((datetime.now() - now).total_seconds()) < 120
    # 只看已收盘的K线时也用墙上时钟：周末过后最后一根可能停在周五收盘，
    # 拿它当现在会误命中 22:55 的清仓窗口。
    now = _eval_now(closed, closed, catch_up=False)
    assert now is not None
    assert abs((datetime.now() - now).total_seconds()) < 120


def test_runner_skips_catch_up_when_already_long() -> None:
    """重启后程序还记着多仓时，第一轮扫描不能再走回溯补开。"""
    from src.scheduler.runner import TradingBot
    from src.strategy.types import PositionState

    cfg = {
        "dry_run": True,
        "trade": {"symbol": "fu2701", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["fu2701"],
            "catch_up_on_start": {"enabled": True, "lookback_bars": 3},
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000},
        "data": {},
        "_project_root": ".",
    }
    bot = TradingBot(cfg)
    bot.strategy.positions["fu2701"] = PositionState(
        side="long",
        volume=1,
        entry_price=5259.0,
        initial_stop=5231.0,
        trail_stop=5231.0,
        symbol="fu2701",
    )
    bot._catch_up_pending = True
    assert bot._symbol_is_flat("fu2701") is False

    called = {"catch_up": False}

    def _fake_find(**_kwargs):
        called["catch_up"] = True
        return None

    with patch("src.scheduler.catch_up.find_catch_up_open", side_effect=_fake_find):
        with patch.object(bot.kline, "fetch", return_value=_bars(datetime(2026, 10, 27, 10, 15), n=40)):
            with patch.object(bot, "_evaluate_symbol") as ev:
                from src.strategy.types import StrategyAction, StrategyDecision

                ev.return_value = StrategyDecision(StrategyAction.HOLD, "fu2701", "观望")
                with patch.object(bot, "_execute"):
                    bot.scan_symbol("fu2701")

    assert called["catch_up"] is False
    assert bot._catch_up_pending is False
