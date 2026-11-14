"""长线趋势加减仓：日线方向 + 60 分钟回调，支持加仓与减仓。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.strategy.factory import create_strategy
from src.strategy.swing_trend import SwingTrendStrategy
from src.strategy.types import StrategyAction


def _bars(n: int, *, start: float = 100.0, drift: float = 0.2, freq: str = "60min") -> pd.DataFrame:
    """造一段单边上行的 60 分钟 K 线。"""
    idx = pd.date_range("2026-01-05 09:00", periods=n, freq=freq)
    rows = []
    price = start
    for i in range(n):
        o = price
        c = price + drift
        h = max(o, c) + 0.3
        low = min(o, c) - 0.3
        rows.append({"open": o, "high": h, "low": low, "close": c, "volume": 1000})
        price = c
    return pd.DataFrame(rows, index=idx)


def _daily_from_minutes(minutes: pd.DataFrame) -> pd.DataFrame:
    """把分钟线粗合成日线，方便测试。"""
    daily = minutes.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return daily


def _cfg(**swing: object) -> dict:
    box = {
        "daily_ma": 5,
        "signal_ma": 5,
        "atr_period": 5,
        "pullback_lookback": 5,
        "touch_atr": 1.0,
        "unit_volume": 1,
        "max_adds": 2,
        "add_min_atr": 0.5,
        "reduce_extend_atr": 1.2,
        "min_core_units": 1,
        "stop_atr": 3.0,
        "flatten_at_close": False,
    }
    box.update(swing)
    return {
        "strategy": {
            "engine": "swing_trend",
            "timeframe": "5",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "swing_trend": box,
        },
        "trade": {"volume": 1, "symbol": "bu2612"},
        "risk": {},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def test_factory_creates_swing_trend() -> None:
    s = create_strategy(_cfg())
    assert isinstance(s, SwingTrendStrategy)
    # _cfg 里公共信号周期是 5 分钟，不再被搭配写成 60。
    assert s.timeframe == "5"
    assert s.trail_timeframe == "5"
    assert s.trend_timeframe == "D"
    assert s.scale == "D_60"
    assert s.uses_trend_bars is True


def test_scale_pair_only_sets_trend_timeframe() -> None:
    """搭配只决定大周期；回调和止损跟公共周期。"""
    s = create_strategy(_cfg(scale="60_15"))
    assert s.scale == "60_15"
    assert s.trend_timeframe == "60"
    assert s.timeframe == "5"
    assert s.trail_timeframe == "5"


def test_missing_public_timeframe_falls_back_to_scale_signal() -> None:
    """公共周期没写时，仍用搭配里的小周期，避免旧 yaml 突然没周期。"""
    cfg = _cfg()
    cfg["strategy"].pop("timeframe", None)
    s = create_strategy(cfg)
    assert s.timeframe == "60"
    assert s.trail_timeframe == "60"
    assert s.trend_timeframe == "D"


def test_follows_public_timeframes() -> None:
    """配置页/回测页的信号周期、止损周期对长线加减仓生效。"""
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "15"
    cfg["strategy"]["trail_timeframe"] = "30"
    s = create_strategy(cfg)
    assert s.timeframe == "15"
    assert s.trail_timeframe == "30"
    assert s.trend_timeframe == "D"


def test_daily_flat_blocks_new_entry() -> None:
    """日线不够时，空仓不开新仓。"""
    s = create_strategy(_cfg())
    minutes = _bars(40)
    decision = s.evaluate("bu2612", minutes, now=minutes.index[-1].to_pydatetime(), trend_bars=None)
    assert decision.action == StrategyAction.HOLD
    assert "日线" in decision.reason or "方向" in decision.reason


def test_first_entry_on_pullback_resume() -> None:
    """日线偏多时，60 分钟先靠近均线再收回上方，可以首开多。"""
    # 日线均线用 2 根，避免合成日线太短算不出方向。
    s = create_strategy(_cfg(daily_ma=2))
    # 先涨一段，再回踩，再抬头。
    up = _bars(30, start=100.0, drift=0.5)
    pull = _bars(6, start=float(up["close"].iloc[-1]), drift=-0.8)
    pull.index = pd.date_range(up.index[-1] + timedelta(hours=1), periods=6, freq="60min")
    resume = _bars(3, start=float(pull["close"].iloc[-1]), drift=1.0)
    resume.index = pd.date_range(pull.index[-1] + timedelta(hours=1), periods=3, freq="60min")
    minutes = pd.concat([up, pull, resume])
    daily = _daily_from_minutes(minutes)
    now = minutes.index[-1].to_pydatetime()
    decision = s.evaluate("bu2612", minutes, now=now, trend_bars=daily)
    assert decision.action == StrategyAction.BUY_OPEN
    assert (decision.meta or {}).get("scale") == "open"
    assert int((decision.meta or {}).get("volume") or 0) == 1


def test_add_and_reduce_volume_in_meta() -> None:
    """持仓后：伸展过远可减仓，meta 带手数。止损放得很远，避免先被止损扫掉。"""
    s = create_strategy(_cfg(daily_ma=2, reduce_extend_atr=0.8, add_min_atr=0.3, max_adds=2, stop_atr=50.0))
    minutes = _bars(50, start=100.0, drift=0.4)
    daily = _daily_from_minutes(minutes)
    now = minutes.index[-1].to_pydatetime()
    from src.strategy.types import PositionState

    s.positions["bu2612"] = PositionState(
        symbol="bu2612",
        side="long",
        volume=2,
        entry_price=100.0,
        initial_stop=1.0,
        trail_stop=1.0,
        trail_armed=True,
    )
    s._adds_done["bu2612"] = 0
    s._units_held["bu2612"] = 2
    s._best_price["bu2612"] = float(minutes["close"].iloc[-1])
    s._reduced_on_extend["bu2612"] = False
    s.set_last_entry_bar("bu2612", str(minutes.index[0]))

    # 把收盘拉到离均线很远，应触发减仓（不是全平）。
    far = minutes.copy()
    far.iloc[-1, far.columns.get_loc("close")] = float(far["close"].iloc[-2]) + 20
    far.iloc[-1, far.columns.get_loc("high")] = float(far["close"].iloc[-1]) + 1
    far.iloc[-1, far.columns.get_loc("low")] = float(far["close"].iloc[-1]) - 0.5
    decision = s.evaluate("bu2612", far, now=now, trend_bars=daily)
    assert decision.action == StrategyAction.CLOSE
    assert (decision.meta or {}).get("scale") == "reduce"
    assert int((decision.meta or {}).get("volume") or 0) >= 1


def test_same_bar_after_entry_still_flattens() -> None:
    """这根已经开过仓，收盘清仓到点仍要平，不能因为「已经处理过」跳过。"""
    s = create_strategy(_cfg(daily_ma=2, flatten_at_close=True, stop_atr=50.0))
    minutes = _bars(40, start=100.0, drift=0.3)
    daily = _daily_from_minutes(minutes)
    last = str(minutes.index[-1])
    from src.strategy.types import PositionState

    s.positions["bu2612"] = PositionState(
        symbol="bu2612",
        side="long",
        volume=1,
        entry_price=100.0,
        initial_stop=1.0,
        trail_stop=1.0,
        trail_armed=True,
    )
    s.set_signaled_bar("bu2612", last)
    s.set_last_entry_bar("bu2612", last)
    s._best_price["bu2612"] = float(minutes["close"].iloc[-1])
    decision = s.evaluate(
        "bu2612",
        minutes,
        now=datetime(2026, 1, 6, 14, 55),
        trend_bars=daily,
    )
    assert decision.action == StrategyAction.CLOSE
    assert (decision.meta or {}).get("scale") == "exit"
