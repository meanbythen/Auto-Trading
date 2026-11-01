"""早盘定方向两套：箱体突破 / 高低递进。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.strategy.morning_edge import (
    MorningBoxAStrategy,
    MorningStructureBStrategy,
    _structure_bias,
)
from src.strategy.types import StrategyAction


def _bars_day(
    *,
    base_open: float = 100.0,
    observe_path: list[tuple[float, float, float, float]] | None = None,
    after_path: list[tuple[float, float, float, float]] | None = None,
    vol: float = 1000.0,
) -> pd.DataFrame:
    """造一天 5 分钟：09:05…09:30 观察，之后可追加突破/回踩。"""
    rows = []
    t0 = datetime(2026, 10, 20, 9, 0)
    # 昨收一根，方便衔接
    rows.append((t0 - timedelta(minutes=5), 99, 99.5, 98.5, 99.0, vol))
    path = observe_path or [
        (100, 100.5, 99.5, 100.2),
        (100.2, 100.8, 100.0, 100.6),
        (100.6, 101.0, 100.4, 100.9),
        (100.9, 101.2, 100.7, 101.0),
        (101.0, 101.5, 100.8, 101.3),
        (101.3, 101.8, 101.0, 101.5),  # 09:30
    ]
    for i, (o, h, l, c) in enumerate(path):
        t = t0 + timedelta(minutes=5 * (i + 1))
        rows.append((t, o, h, l, c, vol))
    if after_path:
        start = len(path)
        for j, (o, h, l, c) in enumerate(after_path):
            t = t0 + timedelta(minutes=5 * (start + j + 1))
            rows.append((t, o, h, l, c, vol * 3 if j == 0 else vol))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.set_index("ts")


def _cfg(engine: str) -> dict:
    section = {
        "morning_box_a": "morning_box_a",
        "morning_structure_b": "morning_structure_b",
    }[engine]
    return {
        "strategy": {
            "engine": engine,
            "timeframe": "5",
            "symbols": ["bu2612"],
            "flatten_at_close": True,
            "flatten_times": ["14:55"],
            section: {
                "be_box_mult": 0.9,
                "vol_confirm_bars": 3,
                "fake_break_bars": 2,
                "no_new_before_close_minutes": 0,
                "pullback_tol_pct": 0.01,
            },
        },
        "trade": {"symbol": "bu2612", "volume": 1, "ctp": {"price_tick": 1}},
    }


def test_factory_two_engines() -> None:
    from src.strategy import create_strategy

    assert type(create_strategy(_cfg("morning_box_a"))).__name__ == "MorningBoxAStrategy"
    assert type(create_strategy(_cfg("morning_structure_b"))).__name__ == "MorningStructureBStrategy"


def test_structure_bias_long() -> None:
    # 前半低、后半抬高
    rows = []
    t0 = datetime(2026, 10, 20, 9, 5)
    for i, (h, l) in enumerate([(100, 98), (100.5, 98.2), (101, 99), (102, 100), (103, 100.5), (104, 101)]):
        rows.append((t0 + timedelta(minutes=5 * i), l + 0.5, h, l, (h + l) / 2, 1000))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    side, _ = _structure_bias(df)
    assert side == "long"


def test_box_a_breakout_then_pullback_entry() -> None:
    """放量突破 H30 后，回踩上沿开多。"""
    # 观察期箱体大约 high=101.8 low=99.5
    after = [
        (101.6, 102.5, 101.5, 102.3),  # 09:35 放量突破
        (102.2, 102.4, 101.7, 101.9),  # 09:40 回踩靠近上沿
    ]
    df = _bars_day(after_path=after)
    strat = MorningBoxAStrategy(_cfg("morning_box_a"))
    # 走到突破根
    d1 = strat.evaluate("bu2612", df.iloc[:8], now=df.index[7].to_pydatetime())
    assert d1.action == StrategyAction.HOLD
    st = strat._day["bu2612"]
    assert st.direction == "long"
    assert st.kind == "day"
    # 回踩根
    d2 = strat.evaluate("bu2612", df.iloc[:9], now=df.index[8].to_pydatetime())
    assert d2.action == StrategyAction.BUY_OPEN
    assert d2.stop_price is not None and d2.stop_price < float(d2.entry_price or 0)


def test_box_a_night_session_same_rules() -> None:
    """夜盘 21:00/21:30 对应日盘 09:00/09:30，同样可突破开仓。"""
    rows = []
    t0 = datetime(2026, 10, 20, 21, 0)
    rows.append((t0 - timedelta(minutes=5), 99, 99.5, 98.5, 99.0, 1000.0))
    path = [
        (100, 100.5, 99.5, 100.2),
        (100.2, 100.8, 100.0, 100.6),
        (100.6, 101.0, 100.4, 100.9),
        (100.9, 101.2, 100.7, 101.0),
        (101.0, 101.5, 100.8, 101.3),
        (101.3, 101.8, 101.0, 101.5),  # 21:30
    ]
    for i, (o, h, l, c) in enumerate(path):
        rows.append((t0 + timedelta(minutes=5 * (i + 1)), o, h, l, c, 1000.0))
    # 21:35 突破，21:40 回踩
    rows.append((t0 + timedelta(minutes=35), 101.6, 102.5, 101.5, 102.3, 3000.0))
    rows.append((t0 + timedelta(minutes=40), 102.2, 102.4, 101.7, 101.9, 1000.0))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    strat = MorningBoxAStrategy(_cfg("morning_box_a"))
    d1 = strat.evaluate("bu2612", df.iloc[:8], now=df.index[7].to_pydatetime())
    assert d1.action == StrategyAction.HOLD
    assert strat._day["bu2612"].kind == "night"
    assert strat._day["bu2612"].direction == "long"
    d2 = strat.evaluate("bu2612", df.iloc[:9], now=df.index[8].to_pydatetime())
    assert d2.action == StrategyAction.BUY_OPEN


def test_night_flatten_at_2255_and_no_new_near_close() -> None:
    """夜盘 22:55 清仓；临清仓 15 分钟不新开。开仓窗内可一直开到 22:30。"""
    cfg = _cfg("morning_box_a")
    cfg["strategy"]["flatten_times"] = ["14:55", "22:55"]
    cfg["strategy"]["morning_box_a"]["no_new_before_close_minutes"] = 15
    strat = MorningBoxAStrategy(cfg)

    # 清仓时刻：22:55 起进入清仓窗（之后 10 分钟内都算）
    assert strat.is_flatten_time(datetime(2026, 10, 20, 22, 55))
    assert not strat.is_flatten_time(datetime(2026, 10, 20, 22, 50))
    assert strat.is_flatten_time(datetime(2026, 10, 20, 23, 0))

    # 临 22:55 的 15 分钟：22:40 起不新开（开仓窗本身 22:30 已结束，此条主要防日盘）
    assert strat._near_flatten(datetime(2026, 10, 20, 22, 40))
    assert strat._near_flatten(datetime(2026, 10, 20, 22, 50))
    assert not strat._near_flatten(datetime(2026, 10, 20, 22, 39))

    # 持仓到 22:55 应强制平多
    rows = []
    t0 = datetime(2026, 10, 20, 21, 0)
    for i in range(8):
        ts = t0 + timedelta(minutes=5 * (i + 1))
        rows.append((ts, 100, 101, 99, 100.5, 1000.0))
    rows.append((datetime(2026, 10, 20, 22, 55), 100.5, 101, 100, 100.8, 1000.0))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    from src.strategy.types import PositionState

    strat.positions["bu2612"] = PositionState(
        symbol="bu2612", side="long", volume=1, entry_price=100.0, trail_stop=99.0
    )
    d = strat.evaluate("bu2612", df, now=datetime(2026, 10, 20, 22, 55))
    assert d.action == StrategyAction.CLOSE
    assert "清仓" in (d.reason or "")
