"""均线波段：60/120 定方向，5/10/20 排好就开，止损跟 EMA120。"""

from datetime import datetime

import pandas as pd

from src.strategy.ema_swing import (
    EmaSwingStrategy,
    _broke_ema120,
    _fast_stacked,
    _stop_ma_too_flat,
    parse_dir_pair,
    parse_ema_len,
    parse_sig_triple,
)
from src.strategy.types import PositionState, StrategyAction


def _cfg(**box) -> dict:
    inner = {
        "flatten_at_close": False,
        "slope_min_deg": 0.0,
        "slope_lookback": 20,
        **box,
    }
    return {
        "strategy": {
            "engine": "ema_swing",
            "timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "stop_mode": "close",
            "ema_swing": inner,
        },
        "trade": {"symbol": "bu2612", "volume": 1},
        "schedule": {"trading_sessions": [["00:00", "23:59"]]},
    }


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-10-20 09:00", periods=len(closes), freq="15min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


def _steady_up() -> list[float]:
    """一路上涨，让 60 上穿 120，同时 5/10/20 自然排成多头。"""
    return [80 + i * 0.6 for i in range(180)]


def test_trail_timeframe_follows_public_stop_period() -> None:
    """止损周期跟左边公共项，不再抄成信号周期。"""
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "15"
    cfg["strategy"]["trail_timeframe"] = "5"
    s = EmaSwingStrategy(cfg)
    assert s.timeframe == "15"
    assert s.trail_timeframe == "5"


def test_parse_sig_triple() -> None:
    assert parse_sig_triple("EMA5 / 10 / 30") == (5, 10, 30)
    assert parse_sig_triple("5/10/20") == (5, 10, 20)
    assert parse_sig_triple("") == (5, 10, 30)
    # 前面比后面大时自动排成从快到慢，计算仍用排好后的根数。
    assert parse_sig_triple(None, 30, 10, 5) == (5, 10, 30)
    assert parse_sig_triple("EMA(8)/EMA(21)/EMA(55)") == (8, 21, 55)


def test_parse_free_ema_numbers() -> None:
    """配置页填 EMA(21) 这种数字时，真的按 21 根去算。"""
    assert parse_ema_len("EMA(21)") == 21
    assert parse_ema_len("21") == 21
    assert parse_ema_len("EMA122") == 122
    assert parse_dir_pair("EMA(21) / EMA(122)", 60, 120) == (21, 122)
    assert parse_dir_pair(None, 122, 21) == (21, 122)
    s = EmaSwingStrategy(_cfg(dir_ema_fast=21, dir_ema_slow=122, dir_ema_pair="", sig_ema_a=8, sig_ema_b=21, sig_ema_c=55))
    assert (s.fast_ma, s.slow_ma) == (21, 122)
    assert (s.sig_a, s.sig_b, s.sig_c) == (8, 21, 55)


def test_fast_stack_and_break() -> None:
    assert _fast_stacked("long", 12.0, 11.0, 10.0) is True
    assert _fast_stacked("long", 11.0, 12.0, 10.0) is False
    assert _fast_stacked("short", 10.0, 11.0, 12.0) is True
    assert _fast_stacked("short", 12.0, 11.0, 10.0) is False
    assert _broke_ema120("long", 99.0, 100.0) is True
    assert _broke_ema120("long", 100.0, 100.0) is True
    assert _broke_ema120("long", 101.0, 100.0) is False
    assert _broke_ema120("short", 101.0, 100.0) is True
    assert _broke_ema120("short", 100.0, 100.0) is True
    assert _broke_ema120("short", 99.0, 100.0) is False


def test_opens_long_when_fast_lines_stacked_after_golden_cross() -> None:
    closes = _steady_up()
    s = EmaSwingStrategy(_cfg())
    opened = None
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        now = bars.index[-1].to_pydatetime()
        decision = s.evaluate("bu2612", bars, now=now)
        if decision.action == StrategyAction.BUY_OPEN:
            opened = decision
            break
    assert opened is not None
    assert opened.stop_price is not None
    assert "多" in opened.reason


def test_opens_after_gap_even_if_cross_bar_skipped() -> None:
    """
    跳空把交叉那根吃掉：前一根还没排好，下一根直接空头排列。
    不要求抓住交叉发生的那一根，只要现在已经 5<10<20 且在 120 下面就能开空。
    """
    # 先跌出死叉和空头排列，再反弹打乱快线，再一根大阴跳空排好。
    down = [200 - i * 0.8 for i in range(150)]
    bounce = [down[-1] + i * 1.2 for i in range(8)]
    gap = [bounce[-1] - 25]
    closes = down + bounce + gap
    s = EmaSwingStrategy(_cfg())
    opened = None
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        now = bars.index[-1].to_pydatetime()
        decision = s.evaluate("bu2612", bars, now=now)
        if decision.action == StrategyAction.SELL_OPEN:
            opened = decision
            s.on_filled("bu2612", decision.action, float(closes[i]), 1, decision.stop_price)
            break
    assert opened is not None
    assert "空" in opened.reason


def test_stop_is_ema120_and_then_follows() -> None:
    closes = _steady_up()
    s = EmaSwingStrategy(_cfg())
    opened = None
    cut = 0
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        now = bars.index[-1].to_pydatetime()
        decision = s.evaluate("bu2612", bars, now=now)
        if decision.action == StrategyAction.BUY_OPEN:
            opened = decision
            cut = i
            break
    assert opened is not None
    s.on_filled("bu2612", StrategyAction.BUY_OPEN, float(closes[cut]), 1, opened.stop_price)
    later = _frame(closes[: cut + 3])
    held = s.evaluate("bu2612", later, now=later.index[-1].to_pydatetime())
    assert held.action == StrategyAction.HOLD
    assert held.stop_price is not None
    assert abs(held.stop_price - s.positions["bu2612"].trail_stop) < 1e-9


def test_keeps_short_bias_when_price_pokes_above_ema120() -> None:
    """死叉后大方向仍是空：价格冲上 120 也不做多。"""
    down = [200 - i * 0.7 for i in range(160)]
    bounce = [down[-1] + i * 0.4 for i in range(20)]
    closes = down + bounce
    s = EmaSwingStrategy(_cfg())
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        now = bars.index[-1].to_pydatetime()
        decision = s.evaluate("bu2612", bars, now=now)
        assert decision.action != StrategyAction.BUY_OPEN, decision.reason


def _commodity_sessions() -> list[list[str]]:
    return [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "23:00"]]


def test_flat_blocks_open_last_30_minutes_before_close() -> None:
    """空仓时，日盘 15:00 前 30 分钟不再开；同一根图下午 14:20 仍可以开。"""
    cfg = _cfg()
    cfg["schedule"]["trading_sessions"] = _commodity_sessions()
    closes = _steady_up()
    opened_i = None
    probe = EmaSwingStrategy(cfg)
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        decision = probe.evaluate(
            "bu2612", bars, now=datetime(2026, 10, 21, 14, 20)
        )
        if decision.action == StrategyAction.BUY_OPEN:
            opened_i = i
            break
    assert opened_i is not None
    blocked = EmaSwingStrategy(cfg)
    late = blocked.evaluate(
        "bu2612",
        _frame(closes[: opened_i + 1]),
        now=datetime(2026, 10, 21, 14, 35),
    )
    assert late.action == StrategyAction.HOLD
    assert "收盘前" in late.reason


def test_close_guard_minutes_from_config() -> None:
    """尾盘不开仓分钟数写在配置里：10 分钟时 14:40 还能开，14:51 拦；填 0 关掉。"""
    cfg = _cfg(no_open_before_close_min=10)
    cfg["schedule"]["trading_sessions"] = _commodity_sessions()
    closes = _steady_up()
    opened_i = None
    probe = EmaSwingStrategy(cfg)
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        decision = probe.evaluate(
            "bu2612", bars, now=datetime(2026, 10, 21, 14, 40)
        )
        if decision.action == StrategyAction.BUY_OPEN:
            opened_i = i
            break
    assert opened_i is not None
    late = EmaSwingStrategy(cfg).evaluate(
        "bu2612",
        _frame(closes[: opened_i + 1]),
        now=datetime(2026, 10, 21, 14, 51),
    )
    assert late.action == StrategyAction.HOLD
    assert "收盘前" in late.reason

    off_cfg = _cfg(no_open_before_close_min=0)
    off_cfg["schedule"]["trading_sessions"] = _commodity_sessions()
    still = EmaSwingStrategy(off_cfg).evaluate(
        "bu2612",
        _frame(closes[: opened_i + 1]),
        now=datetime(2026, 10, 21, 14, 51),
    )
    assert "收盘前" not in still.reason


def test_existing_position_still_managed_before_close() -> None:
    """收盘前 30 分钟只拦空仓新开，手里的单继续跟止损。"""
    cfg = _cfg()
    cfg["schedule"]["trading_sessions"] = _commodity_sessions()
    closes = _steady_up()
    s = EmaSwingStrategy(cfg)
    s.positions["bu2612"] = PositionState(
        symbol="bu2612",
        side="long",
        volume=1,
        entry_price=150,
        initial_stop=140,
        trail_stop=140,
    )
    bars = _frame(closes)
    held = s.evaluate("bu2612", bars, now=datetime(2026, 10, 21, 14, 51))
    assert held.action != StrategyAction.BUY_OPEN
    assert held.action != StrategyAction.SELL_OPEN
    assert "收盘前" not in held.reason


def test_lunch_break_is_not_session_close() -> None:
    """11:25 是午休前，不是收盘，空仓仍允许开。"""
    cfg = _cfg()
    cfg["schedule"]["trading_sessions"] = _commodity_sessions()
    closes = _steady_up()
    s = EmaSwingStrategy(cfg)
    opened = None
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        decision = s.evaluate(
            "bu2612", bars, now=datetime(2026, 10, 21, 11, 25)
        )
        if decision.action == StrategyAction.BUY_OPEN:
            opened = decision
            break
    assert opened is not None


def test_side_stop_slope_can_be_different_lines() -> None:
    """多空分界线、止损、倾角可以不是同一根均线。"""
    s = EmaSwingStrategy(_cfg(side_ema=60, stop_ema=120, slope_ema=60))
    assert s.side_ema == 60
    assert s.stop_ema == 120
    assert s.slope_ema == 60


def test_old_stop_ema_only_config_keeps_three_in_sync() -> None:
    """旧配置只写了止损均线时，多空分界线和倾角先跟它，行为跟拆开前一样。"""
    from src.strategy.factory import create_strategy, sanitize_strategy_config

    cfg = _cfg()
    cfg["strategy"]["ema_swing"] = {"stop_ema": 60, "flatten_at_close": False, "slope_min_deg": 0}
    sanitize_strategy_config(cfg)
    box = cfg["strategy"]["ema_swing"]
    assert box["side_ema"] == 60
    assert box["slope_ema"] == 60
    s = create_strategy(cfg)
    assert s.side_ema == 60
    assert s.stop_ema == 60
    assert s.slope_ema == 60


def test_ignores_root_flatten_switch() -> None:
    s = EmaSwingStrategy(_cfg())
    assert s.flatten_at_close is False
    assert s.is_flatten_time(datetime(2026, 10, 23, 14, 55)) is False


def test_create_via_factory() -> None:
    from src.strategy.factory import create_strategy

    s = create_strategy(_cfg())
    assert isinstance(s, EmaSwingStrategy)
    assert not hasattr(s, "cluster_width")
    assert s.slope_min_deg == 0.0


def test_slope_exit_off_keeps_position_when_flat() -> None:
    """开关关掉时，倾角不够只拦新开仓，已有多单继续拿。"""
    closes = _steady_up()
    s = EmaSwingStrategy(_cfg(slope_min_deg=80.0, slope_exit=False))
    s.positions["bu2612"] = PositionState(
        symbol="bu2612",
        side="long",
        volume=1,
        entry_price=150,
        initial_stop=140,
        trail_stop=140,
    )
    bars = _frame(closes)
    held = s.evaluate("bu2612", bars, now=bars.index[-1].to_pydatetime())
    assert held.action != StrategyAction.CLOSE or "倾角" not in held.reason


def test_slope_exit_on_closes_when_flat() -> None:
    """开关打开且倾角门槛很高时，已有仓因为倾角不够而平，不反手。"""
    closes = _steady_up()
    s = EmaSwingStrategy(_cfg(slope_min_deg=80.0, slope_exit=True))
    s.positions["bu2612"] = PositionState(
        symbol="bu2612",
        side="long",
        volume=1,
        entry_price=150,
        initial_stop=140,
        trail_stop=140,
    )
    bars = _frame(closes)
    decision = s.evaluate("bu2612", bars, now=bars.index[-1].to_pydatetime())
    assert decision.action == StrategyAction.CLOSE
    assert "倾角" in decision.reason


def test_rollover_carry_opens_even_when_slope_blocks() -> None:
    """换月移仓不再等倾角和三线。倾角门槛很高时，普通开仓不开，移仓仍开。"""
    closes = _steady_up()
    s = EmaSwingStrategy(_cfg(slope_min_deg=80.0, slope_lookback=20))
    bars = _frame(closes)
    now = bars.index[-1].to_pydatetime()
    blocked = s.evaluate("bu2612", bars, now=now)
    assert blocked.action == StrategyAction.HOLD
    s.set_rollover_carry("long", 1)
    opened = s.evaluate("bu2612", bars, now=now)
    assert opened.action == StrategyAction.BUY_OPEN
    assert "换月移仓" in opened.reason
    assert opened.meta.get("rollover_carry") is True


def test_blocks_open_when_ema120_flat() -> None:
    """门槛填很大时，黄线再怎么斜也不够，不开仓。"""
    closes = _steady_up()
    s = EmaSwingStrategy(_cfg(slope_min_deg=80.0, slope_lookback=20))
    for i in range(122, len(closes)):
        bars = _frame(closes[: i + 1])
        now = bars.index[-1].to_pydatetime()
        decision = s.evaluate("bu2612", bars, now=now)
        assert decision.action != StrategyAction.BUY_OPEN, decision.reason
        assert decision.action != StrategyAction.SELL_OPEN, decision.reason


def test_slope_method_switch() -> None:
    """默认用回归；填 diff 回旧口径；填错也退回回归。"""
    assert EmaSwingStrategy(_cfg()).slope_method == "reg"
    assert EmaSwingStrategy(_cfg(slope_method="diff")).slope_method == "diff"
    assert EmaSwingStrategy(_cfg(slope_method="DIFF")).slope_method == "diff"
    assert EmaSwingStrategy(_cfg(slope_method="other")).slope_method == "reg"


def test_slope_reg_ignores_last_bar_spike() -> None:
    """EMA 前 40 根走平、最后一根突然抬高：首尾差会误判成陡峭，回归不会。"""
    frame = _frame([100.0] * 41)
    ema = pd.Series([100.0] * 40 + [120.0], index=frame.index)
    blocked_diff, _ = _stop_ma_too_flat(
        "long", ema, frame, frame["close"], lookback=20, min_deg=15.0, period=120, method="diff"
    )
    blocked_reg, _ = _stop_ma_too_flat(
        "long", ema, frame, frame["close"], lookback=20, min_deg=15.0, period=120, method="reg"
    )
    assert blocked_diff is False
    assert blocked_reg is True


def test_slope_reg_passes_steady_rise() -> None:
    """均线稳定上行时，回归口径也要放行。"""
    frame = _frame([100.0] * 41)
    ema = pd.Series([100.0 + i for i in range(41)], index=frame.index)
    for method in ("reg", "diff"):
        blocked, msg = _stop_ma_too_flat(
            "long", ema, frame, frame["close"], lookback=20, min_deg=3.0, period=120, method=method
        )
        assert blocked is False, msg


def test_old_macd_engine_still_falls_back() -> None:
    from src.strategy.daily_body import DailyBodyStrategy
    from src.strategy.factory import create_strategy

    cfg = _cfg()
    cfg["strategy"]["engine"] = "macd"
    assert isinstance(create_strategy(cfg), DailyBodyStrategy)


def test_death_cross_flattens_long() -> None:
    """拿着多单时 60/120 死叉，平掉。"""
    closes = [180 - i * 0.8 for i in range(160)]
    bars = _frame(closes)
    s = EmaSwingStrategy(_cfg())
    s.positions["bu2612"] = PositionState(
        symbol="bu2612",
        side="long",
        volume=1,
        entry_price=150,
        initial_stop=140,
        trail_stop=140,
    )
    now = bars.index[-1].to_pydatetime()
    decision = s.evaluate("bu2612", bars, now=now)
    assert decision.action == StrategyAction.CLOSE
    assert "EMA120" in decision.reason or "死叉" in decision.reason or "反了" in decision.reason


def test_optimizer_accepts_free_ema_numbers() -> None:
    """策略优化表里写 21/122 这种任意根数，会写进配置并用来算均线。"""
    from src.backtest.optimizer import _apply_params

    cfg = _cfg()
    out = _apply_params(
        cfg,
        {
            "dir_ema_pair": "EMA(21)/EMA(122)",
            "sig_ema_triple": "8/21/55",
            "side_ema": "EMA(60)",
            "stop_ema": 90,
            "slope_ema": "45",
        },
        "ema_swing",
    )
    box = out["strategy"]["ema_swing"]
    assert box["dir_ema_fast"] == 21
    assert box["dir_ema_slow"] == 122
    assert box["sig_ema_a"] == 8
    assert box["sig_ema_b"] == 21
    assert box["sig_ema_c"] == 55
    assert box["side_ema"] == 60
    assert box["stop_ema"] == 90
    assert box["slope_ema"] == 45
    s = EmaSwingStrategy(out)
    assert (s.fast_ma, s.slow_ma) == (21, 122)
    assert (s.sig_a, s.sig_b, s.sig_c) == (8, 21, 55)
    assert s.side_ema == 60
