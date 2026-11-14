"""长线箱体：拐点、收盘突破、冻结箱沿、假突破回箱离场。"""

from datetime import datetime, timedelta

import pandas as pd

from src.strategy.swing_box import SwingBoxStrategy, find_pivots, form_box
from src.strategy.types import StrategyAction


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """每行是开高低收。索引从 2024-01-02 起每个交易日一根。"""
    start = datetime(2024, 1, 2)
    idx = [start + timedelta(days=i) for i in range(len(rows))]
    data = {
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "volume": [1000] * len(rows),
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def test_pivots_need_neighbors_on_both_sides() -> None:
    """左右各 2 根都更低，才算有效高点；窗口最边上的尖不算。"""
    highs = [1, 2, 5, 2, 1, 2, 6, 2, 1, 3]
    lows = [0, 1, 2, 1, 0, 1, 2, 1, 0, 1]
    peaks, valleys = find_pivots(highs, lows, peak_look=2)
    assert [p[0] for p in peaks] == [2, 6]
    assert [v[0] for v in valleys] == [4]


def _range_then(last: tuple[float, float, float, float], n: int = 50) -> list[tuple[float, float, float, float]]:
    """一段高低分开的震荡，方便画出合格箱子。"""
    rows: list[tuple[float, float, float, float]] = []
    for i in range(n):
        cycle = i % 8
        if cycle == 3:
            rows.append((110, 120, 108, 112))
        elif cycle == 7:
            rows.append((90, 92, 80, 88))
        else:
            rows.append((100, 105, 95, 100))
    rows.append(last)
    return rows


def test_wick_break_does_not_form_entry() -> None:
    """盘中刺破上沿、收盘仍在箱子里，不算突破。"""
    bars = _bars(_range_then((104, 130, 103, 105)))
    cfg = {
        "strategy": {
            "engine": "swing_box",
            "symbols": ["c2701"],
            "swing_box": {"adx_min": 0, "height_atr": 0, "lookback": 40},
        },
        "trade": {"volume": 1, "symbol": "c2701"},
    }
    s = SwingBoxStrategy(cfg)
    d = s.evaluate("c2701", bars)
    assert d.action == StrategyAction.HOLD
    assert "箱子里" in d.reason or "还没有" in d.reason


def test_close_break_opens_and_freezes_box() -> None:
    """收盘站上箱顶才开多；开仓后箱沿写进持仓，后面 K 线不再改。"""
    bars = _bars(_range_then((111, 128, 110, 126)))
    cfg = {
        "strategy": {
            "engine": "swing_box",
            "symbols": ["c2701"],
            "stop_mode": "close",
            "swing_box": {"adx_min": 0, "height_atr": 0, "lookback": 40},
        },
        "trade": {"volume": 1, "symbol": "c2701"},
    }
    s = SwingBoxStrategy(cfg)
    formed = form_box(
        bars,
        lookback=40,
        peak_look=2,
        min_gap=5,
        atr_period=20,
        height_atr=0,
        adx_period=14,
        adx_min=0,
    )
    assert formed is not None
    d = s.evaluate("c2701", bars)
    assert d.action == StrategyAction.BUY_OPEN
    frozen_high = d.meta["box_high"]
    s.on_filled("c2701", d.action, float(d.entry_price or 126), 1, d.stop_price)
    assert s.positions["c2701"].box_high == frozen_high
    combo = pd.concat([bars, _bars([(130, 140, 129, 135)])])
    s.evaluate("c2701", combo)
    assert s.positions["c2701"].box_high == frozen_high


def test_back_inside_box_closes() -> None:
    """开多之后收盘回到箱顶以内，按失效离场，不扛。"""
    cfg = {
        "strategy": {
            "engine": "swing_box",
            "symbols": ["c2701"],
            "stop_mode": "close",
            "swing_box": {"adx_min": 0, "height_atr": 0},
        },
        "trade": {"volume": 1, "symbol": "c2701"},
    }
    s = SwingBoxStrategy(cfg)
    s.positions["c2701"].side = "long"
    s.positions["c2701"].volume = 1
    s.positions["c2701"].entry_price = 120
    s.positions["c2701"].initial_stop = 100
    s.positions["c2701"].trail_stop = 100
    s.positions["c2701"].box_high = 110
    s.positions["c2701"].box_low = 90
    s.positions["c2701"].box_height = 20
    s.set_last_entry_bar("c2701", "1999-01-01")
    rows = [(105, 108, 100, 105)] * 60
    rows.append((108, 109, 104, 108))  # 收盘 108 < 箱顶 110
    bars = _bars(rows)
    d = s.evaluate("c2701", bars)
    assert d.action == StrategyAction.CLOSE
    assert "失效" in d.reason


def test_apply_params_writes_timeframe_to_public_keys() -> None:
    """优化若搜到周期，要写到公共 timeframe，不能塞进 swing_box 小节。"""
    from src.backtest.optimizer import _apply_params

    out = _apply_params(
        {"strategy": {"engine": "swing_box", "swing_box": {"lookback": 40}}},
        {"timeframe": "60", "lookback": 30},
        "swing_box",
    )
    assert out["strategy"]["timeframe"] == "60"
    assert out["strategy"]["trail_timeframe"] == "60"
    assert out["strategy"]["swing_box"]["lookback"] == 30
    assert "timeframe" not in out["strategy"]["swing_box"]


def test_create_engine() -> None:
    from src.strategy.factory import create_strategy

    s = create_strategy(
        {
            "strategy": {"engine": "swing_box", "symbols": ["fu2701"]},
            "trade": {"volume": 1, "symbol": "fu2701"},
        }
    )
    assert isinstance(s, SwingBoxStrategy)
    assert s.timeframe == "D"
    assert s.trail_timeframe == "D"
    assert s.flatten_at_close is False


def test_follows_public_timeframes() -> None:
    """配置页/回测页的信号周期、止损周期对长线箱体生效，不再写死日线。"""
    s = SwingBoxStrategy(
        {
            "strategy": {
                "engine": "swing_box",
                "timeframe": "60",
                "trail_timeframe": "15",
                "symbols": ["sa2701"],
            },
            "trade": {"volume": 1, "symbol": "sa2701"},
        }
    )
    assert s.timeframe == "60"
    assert s.trail_timeframe == "15"
