"""
日内浮盈飘单的测试。

箱子、ETH、截止钟点从日内实体复制；这里核对：
晚开仓窗口、跳空顺向、ATR 宽度止损、浮盈按 ATR 保本。
"""

from datetime import datetime

import pandas as pd

from src.strategy.float_hold import (
    DIRECTION_ETH_TREND,
    DIRECTION_OPENING_BOX,
    DirectionContext,
    DirectionPick,
    FloatHoldStrategy,
    atr_width_stop,
    make_direction,
)
from src.strategy.types import StrategyAction


def _cfg() -> dict:
    return {
        "strategy": {
            "engine": "float_hold",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "market_filter": {"enabled": False},
            "float_hold": {
                "direction": "opening_box",
                "eth_minutes": 20,
                "eth_minutes2": 0,
                "skip_gap_atr_mult": 0,
                "max_box_atr_mult": 0,
                "max_chase_atr_mult": 0,
                "gap_align_atr_mult": 0,
                "stop_atr_mult": 0.5,
                "be_atr_mult": 2.0,
                "filter_enabled": False,
                "filter_atr_period": 4,
            },
        },
        "trade": {"symbol": "bu2612", "volume": 1, "price_tick": 1},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def _breakout(end: str, close: float, freq: str = "5min") -> pd.DataFrame:
    """ETH 里的框是 99 到 101，最后一根收盘由调用方决定。"""
    end_ts = pd.Timestamp(end)
    start_hour = 18 if end_ts.hour >= 21 else 7
    index = pd.date_range(end_ts.normalize() + pd.Timedelta(hours=start_hour), end_ts, freq=freq)
    anchor_hour = 21 if end_ts.hour >= 21 else 9
    opened = end_ts.normalize() + pd.Timedelta(hours=anchor_hour)
    eth_end = opened + pd.Timedelta(minutes=20)
    closes = []
    highs = []
    lows = []
    for stamp in index:
        if opened < stamp <= eth_end:
            closes.append(100.0)
            highs.append(101.0)
            lows.append(99.0)
        else:
            closes.append(100.0)
            highs.append(100.4)
            lows.append(99.6)
    closes[-1] = close
    highs[-1] = max(highs[-1], close + 0.2)
    lows[-1] = min(lows[-1], close - 0.2)
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0] * len(index),
        },
        index=index,
    )


def _gap_bars(end: str, close: float, prev_close: float) -> pd.DataFrame:
    bars = _breakout(end, close)
    end_ts = pd.Timestamp(end)
    opened = end_ts.normalize() + pd.Timedelta(hours=21 if end_ts.hour >= 21 else 9)
    prior = bars.index <= opened
    bars.loc[prior, "open"] = prev_close
    bars.loc[prior, "close"] = prev_close
    bars.loc[prior, "high"] = prev_close + 1.0
    bars.loc[prior, "low"] = prev_close - 1.0
    return bars


def test_atr_width_stop_from_entry() -> None:
    """多单从进场价往下留半倍 ATR，空单往上留，不再贴箱沿。"""
    assert abs(atr_width_stop(side="long", entry=105.0, atr=10.0, mult=0.5, price_tick=1.0) - 100.0) < 1e-6
    assert abs(atr_width_stop(side="short", entry=90.0, atr=10.0, mult=0.5, price_tick=1.0) - 95.0) < 1e-6


def test_long_stop_uses_half_pre_open_atr() -> None:
    """站上开盘区间后，止损离进场价大约 0.5 倍开盘前 ATR，不是箱顶外 1 跳。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    strategy = FloatHoldStrategy(_cfg())
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.stop_price is not None
    assert decision.entry_price is not None
    atr = strategy._last_stop_atr
    assert atr is not None and atr > 0
    width = abs(decision.entry_price - decision.stop_price)
    assert abs(width - 0.5 * atr) <= 1.0
    assert decision.stop_price < decision.entry_price
    assert "倍开盘前ATR" in decision.reason


def test_gap_up_blocks_a_short() -> None:
    """高开够大时，即使收盘跌破箱子也不做空去填缺口。"""
    cfg = _cfg()
    cfg["strategy"]["float_hold"]["gap_align_atr_mult"] = 0.5
    bars = _gap_bars("2026-10-23 09:45", 90.0, 80.0)
    decision = FloatHoldStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "只做多" in decision.reason
    assert decision.meta.get("block_key") == "gap_align"


def test_gap_up_still_allows_a_long() -> None:
    """同样的高开，收盘站上箱子仍然做多。"""
    cfg = _cfg()
    cfg["strategy"]["float_hold"]["gap_align_atr_mult"] = 0.5
    bars = _gap_bars("2026-10-23 09:45", 105.0, 80.0)
    decision = FloatHoldStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_moves_stop_to_entry_after_two_atr() -> None:
    """浮盈达到 2 倍开仓 ATR 后，止损提到开仓价。"""
    strategy = FloatHoldStrategy(_cfg())
    opened = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:45", 105.0), now=datetime(2026, 10, 23, 9, 45)
    )
    assert opened.action == StrategyAction.BUY_OPEN
    strategy.on_filled("bu2612", opened.action, opened.entry_price, 1, opened.stop_price)
    pos = strategy.positions["bu2612"]
    atr = strategy._entry_atr["bu2612"]
    boom = _breakout("2026-10-23 10:00", pos.entry_price + 2 * atr + 1)
    last = boom.index[-1]
    boom.at[last, "low"] = pos.entry_price + 1
    boom.at[last, "high"] = pos.entry_price + 2 * atr + 1
    boom.at[last, "close"] = pos.entry_price + 2 * atr + 1
    held = strategy.evaluate("bu2612", boom, now=datetime(2026, 10, 23, 10, 0))
    assert held.action == StrategyAction.HOLD
    assert "已经保本" in held.reason
    assert abs(float(pos.trail_stop) - float(pos.entry_price)) < 1e-6


def test_day_allows_a_second_try_on_the_same_box() -> None:
    """日盘第一枪止损后，截止前还能用同一口箱子再开。"""
    strategy = FloatHoldStrategy(_cfg())
    opened = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:45", 105.0), now=datetime(2026, 10, 23, 9, 45)
    )
    strategy.on_filled("bu2612", opened.action, opened.entry_price, 1, opened.stop_price)
    crash = _breakout("2026-10-23 10:00", opened.entry_price)
    crash.loc[crash.index[-1], ["open", "high", "low", "close"]] = [
        opened.entry_price,
        opened.entry_price,
        opened.stop_price - 5,
        opened.stop_price - 1,
    ]
    closed = strategy.evaluate("bu2612", crash, now=datetime(2026, 10, 23, 10, 0))
    assert closed.action == StrategyAction.CLOSE
    strategy.on_filled("bu2612", StrategyAction.CLOSE, opened.stop_price, 1)
    again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 90.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert again.action == StrategyAction.SELL_OPEN
    assert "第二ETH窗口开仓" not in again.reason


def test_default_direction_is_eth_trend() -> None:
    """没填选择器时，用观察结束顺大方向。"""
    cfg = _cfg()
    cfg["strategy"]["float_hold"].pop("direction", None)
    strategy = FloatHoldStrategy(cfg)
    assert strategy.direction.key == DIRECTION_ETH_TREND


def test_opening_box_still_works_when_selected() -> None:
    """配置写成开盘箱体时，还是收盘走出箱子才开。"""
    strategy = FloatHoldStrategy(_cfg())
    assert strategy.direction.key == DIRECTION_OPENING_BOX
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.meta.get("direction") == DIRECTION_OPENING_BOX
    assert decision.meta.get("size_source") in {
        "public_risk_pct",
        "public_kelly",
        "public_fixed",
    }


class _AlwaysLong:
    """测试用：不管箱子，一律做多。用来证明方向选择器可以整块换掉。"""

    key = "always_long"
    label = "测试永远做多"

    def pick(self, ctx: DirectionContext) -> DirectionPick:
        del ctx
        return DirectionPick(side="long", reason="测试方向：永远做多")


def test_direction_selector_can_be_swapped() -> None:
    """收盘还在箱子里时，默认选择器不开；换成永远做多就会开多。"""
    inside = _breakout("2026-10-23 09:45", 100.0)
    default = FloatHoldStrategy(_cfg()).evaluate(
        "bu2612", inside, now=datetime(2026, 10, 23, 9, 45)
    )
    assert default.action == StrategyAction.HOLD
    swapped = FloatHoldStrategy(_cfg(), direction=_AlwaysLong()).evaluate(
        "bu2612", inside, now=datetime(2026, 10, 23, 9, 45)
    )
    assert swapped.action == StrategyAction.BUY_OPEN
    assert "永远做多" in swapped.reason


def test_make_direction_unknown_falls_back() -> None:
    """写错名字时退回观察结束顺大方向，避免旧配置打不开。"""
    assert make_direction("还没有的方案").key == DIRECTION_ETH_TREND


def test_macd_timeframe_and_ema_pair_parse() -> None:
    """配置页两个选项：MACD用1分钟还是5分钟，均线对从名单里挑。"""
    from src.strategy.float_hold import (
        ema_pair_label,
        normalize_macd_timeframe,
        parse_ema_pair_label,
    )

    assert normalize_macd_timeframe("1分钟") == "1"
    assert normalize_macd_timeframe("5") == "5"
    assert parse_ema_pair_label("EMA10 / EMA60") == (10, 60)
    from src.strategy.float_hold import parse_macd_combo

    assert parse_macd_combo("5 / 34 / 5") == (5, 34, 5)
    assert ema_pair_label(5, 20) == "EMA5 / EMA20"
    selector = make_direction(
        "macd_cycle",
        {"macd_timeframe": "1", "dir_ema_pair": "EMA20 / EMA60"},
    )
    assert selector.bar_minutes == 1
    assert selector.ema_fast == 20
    assert selector.ema_slow == 60


def test_one_minute_macd_needs_one_minute_bars() -> None:
    """选了1分钟MACD，但手里只有5分钟K，就先等，不要拿粗K线假算。"""
    cfg = _macd_cfg()
    cfg["strategy"]["float_hold"]["macd_timeframe"] = "1"
    cfg["strategy"]["timeframe"] = "5"
    decision = FloatHoldStrategy(cfg).evaluate(
        "bu2612",
        _macd_trend_bars("2026-10-23 09:35"),
        now=datetime(2026, 10, 23, 9, 35),
    )
    assert decision.action == StrategyAction.HOLD
    assert "1分钟" in decision.reason


def test_scan_macd_cycle_waits_for_two_waves() -> None:
    """开盘红柱走完、再走完一波绿柱，才算一轮。"""
    from src.strategy.float_hold.macd_cycle import scan_macd_cycle

    idx = pd.date_range("2026-10-23 09:05", periods=7, freq="5min")
    hist = pd.Series([0.2, 0.3, 0.1, -0.2, -0.3, 0.15, 0.2], index=idx)
    opened = pd.Timestamp("2026-10-23 09:00")
    waiting_first = scan_macd_cycle(hist, opened, pd.Timestamp("2026-10-23 09:15"))
    assert not waiting_first.ready
    assert "红柱" in waiting_first.reason
    waiting_second = scan_macd_cycle(hist, opened, pd.Timestamp("2026-10-23 09:25"))
    assert not waiting_second.ready
    assert "绿柱" in waiting_second.reason
    done = scan_macd_cycle(hist, opened, pd.Timestamp("2026-10-23 09:30"))
    assert done.ready
    assert done.first == 1


def _macd_cfg() -> dict:
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "5"
    hold = cfg["strategy"]["float_hold"]
    hold["direction"] = "macd_cycle"
    hold["macd_timeframe"] = "5"
    hold["macd_fast"] = 2
    hold["macd_slow"] = 4
    hold["macd_signal"] = 2
    hold["dir_ema_fast"] = 2
    hold["dir_ema_slow"] = 4
    hold["skip_gap_atr_mult"] = 0
    hold["max_box_atr_mult"] = 0
    hold["max_chase_atr_mult"] = 0
    hold["gap_align_atr_mult"] = 0
    hold["day_cutoff"] = "11:00"
    return cfg


def _macd_trend_bars(end: str) -> pd.DataFrame:
    """从早盘前一直涨，方便循环走完时短均线在长均线上方。"""
    end_ts = pd.Timestamp(end)
    index = pd.date_range("2026-10-23 07:00", end_ts, freq="5min")
    closes = []
    px = 100.0
    for stamp in index:
        px += 0.4 if stamp.hour >= 9 else 0.05
        closes.append(px)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [100.0] * len(index),
        },
        index=index,
    )


def test_macd_cycle_direction_opens_after_full_loop(monkeypatch) -> None:
    """没走完一轮先等；红绿走完且短均线在上就做多。不等 60 分钟箱子。"""
    from src.strategy.float_hold import macd_cycle as macd_mod

    def _fake_macd(close, **kwargs):
        hist = pd.Series(0.0, index=close.index)
        for ts in close.index:
            clock = ts.hour * 60 + ts.minute
            if clock < 9 * 60:
                hist.loc[ts] = 0.05
            elif clock <= 9 * 60 + 15:
                hist.loc[ts] = 0.3
            elif clock <= 9 * 60 + 25:
                hist.loc[ts] = -0.3
            else:
                hist.loc[ts] = 0.25
        return pd.DataFrame({"dif": hist, "dea": hist * 0.0, "hist": hist}, index=close.index)

    monkeypatch.setattr(macd_mod, "macd_lines", _fake_macd)
    strategy = FloatHoldStrategy(_macd_cfg())
    waiting = strategy.evaluate(
        "bu2612",
        _macd_trend_bars("2026-10-23 09:15"),
        now=datetime(2026, 10, 23, 9, 15),
    )
    assert waiting.action == StrategyAction.HOLD
    assert "ETH窗口" not in waiting.reason
    assert "红柱" in waiting.reason or "循环" in waiting.reason
    opened = strategy.evaluate(
        "bu2612",
        _macd_trend_bars("2026-10-23 09:35"),
        now=datetime(2026, 10, 23, 9, 35),
    )
    assert opened.action == StrategyAction.BUY_OPEN
    assert opened.meta.get("direction") == "macd_cycle"
    assert "做多" in opened.reason


class _FixedSide:
    """测试用：不管 K 线，直接报多或空。"""

    key = "opening_box"
    label = "测试固定方向"
    needs_opening_box = False
    bar_minutes = 5

    def __init__(self, side: str) -> None:
        self._side = side

    def pick(self, ctx):
        return DirectionPick(self._side, "测试固定方向")


def _slope_bars(end: str, *, step: float, n: int = 240, wick: float = 0.3) -> pd.DataFrame:
    """连续涨或跌的 5 分钟线，用来排 EMA60/120 和算倾角。"""
    end_ts = pd.Timestamp(end)
    index = pd.date_range(end_ts - pd.Timedelta(minutes=5 * (n - 1)), end_ts, freq="5min")
    closes = []
    px = 3000.0
    for _ in index:
        px += step
        closes.append(px)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + wick for c in closes],
            "low": [c - wick for c in closes],
            "close": closes,
            "volume": [100.0] * len(index),
        },
        index=index,
    )


def test_ema_angle_deg_known_value() -> None:
    """走了 1 倍波动、横过 5 根，大约 11 度。"""
    import math

    from src.strategy.float_hold.trend_gate import ema_angle_deg

    got = ema_angle_deg(101.0, 100.0, 5, 1.0)
    assert abs(got - math.degrees(math.atan(0.2))) < 1e-9


def test_trend_gate_off_does_not_block() -> None:
    """过滤器默认关，空头排列时仍允许做多。"""
    from src.strategy.float_hold.trend_gate import TrendAlignGate

    gate = TrendAlignGate({"trend_align_enabled": False})
    bars = _slope_bars("2026-10-23 10:00", step=-1.5)
    out = gate.check(
        side="long",
        bars=bars,
        now=datetime(2026, 10, 23, 10, 0),
        timeframe="5",
        macd_minutes=5,
    )
    assert out.ok


def test_trend_gate_blocks_against_alignment() -> None:
    """EMA60 在 EMA120 下方只做空，想做多就拦住。"""
    from src.strategy.float_hold.trend_gate import TrendAlignGate

    gate = TrendAlignGate({"trend_align_enabled": True, "trend_slope_min_deg": 0})
    bars = _slope_bars("2026-10-23 10:00", step=-1.5)
    out = gate.check(
        side="long",
        bars=bars,
        now=datetime(2026, 10, 23, 10, 0),
        timeframe="5",
        macd_minutes=5,
    )
    assert not out.ok
    assert "只做空" in out.reason


def test_trend_gate_blocks_flat_slope() -> None:
    """排列对了但两条线几乎走平，多空都不开。"""
    from src.strategy.float_hold.trend_gate import TrendAlignGate

    gate = TrendAlignGate({"trend_align_enabled": True, "trend_slope_min_deg": 8})
    bars = _slope_bars("2026-10-23 10:00", step=0.02, wick=1.5)
    out = gate.check(
        side="long",
        bars=bars,
        now=datetime(2026, 10, 23, 10, 0),
        timeframe="5",
        macd_minutes=5,
    )
    assert not out.ok
    assert "不够陡" in out.reason


def test_trend_gate_allows_steep_long() -> None:
    """多头排列且两条线够陡，放行做多。"""
    from src.strategy.float_hold.trend_gate import TrendAlignGate

    gate = TrendAlignGate({"trend_align_enabled": True, "trend_slope_min_deg": 8})
    bars = _slope_bars("2026-10-23 10:00", step=1.5)
    out = gate.check(
        side="long",
        bars=bars,
        now=datetime(2026, 10, 23, 10, 0),
        timeframe="5",
        macd_minutes=5,
    )
    assert out.ok
    assert out.allow == "long"


def test_float_hold_blocks_open_when_trend_filter_disagrees() -> None:
    """方向选择器说做多，但大方向是空头，策略仍HOLD。"""
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "5"
    cfg["strategy"]["float_hold"]["trend_align_enabled"] = True
    cfg["strategy"]["float_hold"]["trend_slope_min_deg"] = 0
    cfg["strategy"]["float_hold"]["day_cutoff"] = "11:00"
    strategy = FloatHoldStrategy(cfg, direction=_FixedSide("long"))
    bars = _slope_bars("2026-10-23 10:00", step=-1.5)
    decision = strategy.evaluate(
        "bu2612",
        bars,
        now=datetime(2026, 10, 23, 10, 0),
    )
    assert decision.action == StrategyAction.HOLD
    assert "只做空" in decision.reason or "大方向" in decision.reason


def test_eth_trend_waits_then_opens_with_alignment() -> None:
    """观察没走完先等；走完后顺着 EMA60/120 开，不再等箱子。"""
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "5"
    hold = cfg["strategy"]["float_hold"]
    hold["direction"] = "eth_trend"
    hold["eth_minutes"] = 20
    hold["trend_slope_min_deg"] = 0
    hold["day_cutoff"] = "14:45"
    hold["stop_atr_mult"] = 1.5
    waiting = FloatHoldStrategy(cfg).evaluate(
        "bu2612",
        _slope_bars("2026-10-23 09:10", step=1.5),
        now=datetime(2026, 10, 23, 9, 10),
    )
    assert waiting.action == StrategyAction.HOLD
    opened = FloatHoldStrategy(cfg).evaluate(
        "bu2612",
        _slope_bars("2026-10-23 09:25", step=1.5),
        now=datetime(2026, 10, 23, 9, 25),
    )
    assert opened.action == StrategyAction.BUY_OPEN
    assert opened.meta.get("direction") == "eth_trend"
    assert "顺" in opened.reason or "只做多" in opened.reason
    assert opened.stop_price is not None and opened.entry_price is not None
    width = abs(opened.entry_price - opened.stop_price)
    assert width > 1.0


def test_trading_day_night_counts_as_next_date() -> None:
    """夜盘 21 点开，算第二天的交易日。"""
    from datetime import date

    from src.strategy.float_hold.day_cluster import trading_day_of

    assert trading_day_of(datetime(2026, 10, 22, 21, 5)) == date(2026, 10, 23)
    assert trading_day_of(datetime(2026, 10, 23, 2, 0)) == date(2026, 10, 23)
    assert trading_day_of(datetime(2026, 10, 23, 9, 5)) == date(2026, 10, 23)


def test_find_touch_cluster_picks_the_busy_band() -> None:
    """被反复摸到的窄带，而不是偶尔扫到的远处。"""
    from src.strategy.float_hold.day_cluster import find_touch_cluster

    idx = pd.date_range("2026-10-22 09:00", periods=20, freq="5min")
    closes = [104.0] * 16 + [155.0] * 4
    highs = [108.0] * 16 + [160.0] * 4
    lows = [100.0] * 16 + [150.0] * 4
    day = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [10.0] * 20},
        index=idx,
    )
    found = find_touch_cluster(day, tick=1.0, max_width=10.0)
    assert found is not None
    z_lo, z_hi, hits = found
    assert z_hi - z_lo <= 10.0 + 1e-9
    assert z_lo >= 99.0
    assert z_hi <= 109.0
    assert hits > 0


def _cluster_session_bars(end: str, last_close: float) -> pd.DataFrame:
    """昨天大部分时间在 100～108 里磨，今天收到 last_close。"""
    end_ts = pd.Timestamp(end)
    prev = pd.date_range("2026-10-22 09:00", "2026-10-22 15:00", freq="5min")
    cur = pd.date_range("2026-10-23 09:00", end_ts, freq="5min")
    index = prev.append(cur)
    closes = []
    highs = []
    lows = []
    for stamp in index:
        if stamp.normalize() == pd.Timestamp("2026-10-22"):
            closes.append(104.0)
            highs.append(108.0)
            lows.append(100.0)
        else:
            closes.append(last_close)
            highs.append(last_close + 1.0)
            lows.append(last_close - 1.0)
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [80.0] * len(index),
        },
        index=index,
    )


def _cluster_cfg() -> dict:
    cfg = _cfg()
    hold = cfg["strategy"]["float_hold"]
    hold["direction"] = "day_cluster"
    hold["cluster_max_width"] = 10
    hold["eth_minutes"] = 20
    hold["day_cutoff"] = "14:45"
    return cfg


def test_day_cluster_waits_inside_zone_and_opens_outside() -> None:
    """观察结束后：还在昨日密集区里先等；走到上面做多，走到下面做空。"""
    cfg = _cluster_cfg()
    waiting_eth = FloatHoldStrategy(cfg).evaluate(
        "bu2612",
        _cluster_session_bars("2026-10-23 09:10", 115.0),
        now=datetime(2026, 10, 23, 9, 10),
    )
    assert waiting_eth.action == StrategyAction.HOLD
    inside = FloatHoldStrategy(cfg).evaluate(
        "bu2612",
        _cluster_session_bars("2026-10-23 09:25", 104.0),
        now=datetime(2026, 10, 23, 9, 25),
    )
    assert inside.action == StrategyAction.HOLD
    assert "密集区" in inside.reason
    above = FloatHoldStrategy(cfg).evaluate(
        "bu2612",
        _cluster_session_bars("2026-10-23 09:25", 115.0),
        now=datetime(2026, 10, 23, 9, 25),
    )
    assert above.action == StrategyAction.BUY_OPEN
    assert "上方" in above.reason
    below = FloatHoldStrategy(cfg).evaluate(
        "bu2612",
        _cluster_session_bars("2026-10-23 09:25", 90.0),
        now=datetime(2026, 10, 23, 9, 25),
    )
    assert below.action == StrategyAction.SELL_OPEN
    assert "下方" in below.reason
