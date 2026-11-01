"""
日内实体的测试。

方向跟信号周期走。默认用例用 15 分钟（更细的 5 分钟会先收成 15）。
ETH 走完后，收盘离开开盘区间才开。过了 10:30 或 22:15 不追。
"""

from datetime import datetime

import pandas as pd

from src.strategy.daily_body import DailyBodyStrategy, box_edge_stop
from src.strategy.types import StrategyAction, StrategyDecision


def _cfg() -> dict:
    return {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": True,
            "daily_body": {
                "eth_minutes": 20,
            },
        },
        "trade": {"symbol": "bu2612", "volume": 1},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
    }


def _bars(start: str, closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    """从某个钟点起，每 5 分钟一根。开盘价默认都用第一根的收盘价。"""
    index = pd.date_range(start, periods=len(closes), freq="5min")
    if opens is None:
        opens = [closes[0]] * len(closes)
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 1 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 1 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


def _daily(up: bool) -> pd.DataFrame:
    """12 根已经走完的日线。向上时最后一根收在平均线上方，向下时在下方。"""
    index = pd.bdate_range(end="2026-10-22", periods=12)
    closes = [100 + i * 4 for i in range(12)] if up else [200 - i * 4 for i in range(12)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=index,
    )


def _aligned(end: str, up: bool) -> pd.DataFrame:
    """
    一段慢慢走的 5 分钟 K 线，收在 end 这根。

    向上时，每根收盘都高于开盘，最后一根在开盘价、分时均线和 K 线均线上面。
    向下时反过来。这样五项里除了日线，其余都能站在同一边。
    """
    index = pd.date_range(end=end, periods=40, freq="5min")
    closes = [100 + i * 0.5 for i in range(40)] if up else [160 - i * 0.5 for i in range(40)]
    opens = [c - 0.3 for c in closes] if up else [c + 0.3 for c in closes]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 0.2 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 0.2 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


def _cfg15() -> dict:
    """与默认相同：15 分钟信号。留着给旧测试名调用。"""
    return _cfg()


def _cfg5() -> dict:
    """信号周期改成 5 分钟，同一套开盘区间规则。"""
    cfg = _cfg()
    cfg["strategy"]["timeframe"] = "5"
    cfg["strategy"]["trail_timeframe"] = "5"
    return cfg


def _breakout(end: str, close: float, freq: str = "5min") -> pd.DataFrame:
    """
    从开盘前排到指定时刻。ETH 里的框是 99 到 101。
    最后一根的收盘价由调用方决定，用来表示走出框、留在框里，或只是影线刺出去。
    """
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


def test_waits_until_eth_finishes() -> None:
    """9:15 这根 15 分钟还有一段落在 ETH 里，即使收盘已经很高，也不开。"""
    bars = _breakout("2026-10-23 09:15", 110.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 15))
    assert decision.action == StrategyAction.HOLD
    assert "ETH" in decision.reason


def test_close_above_the_opening_range_is_long() -> None:
    """9:45 这根 15 分钟走完，收盘站上开盘区间，做多。更细的 5 分钟会先收成 15。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN
    assert "15分钟" in decision.reason
    assert "站上开盘区间" in decision.reason


def test_five_minute_midbar_waits_for_the_fifteen_minute_close() -> None:
    """信号是 15 分钟时：9:50 还在中间，即使 5 分钟已经冲出去，也不提前开。"""
    bars = _breakout("2026-10-23 09:50", 110.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 50))
    assert decision.action == StrategyAction.HOLD
    assert "15分钟还没走完" in decision.reason


def test_five_minute_signal_opens_on_its_own_bar() -> None:
    """信号改成 5 分钟时，用 5 分钟自己的收盘判断，不再强行收成 15。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(_cfg5()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN
    assert "5分钟" in decision.reason
    assert "站上开盘区间" in decision.reason


def test_staying_inside_waits_and_keeps_the_session() -> None:
    """15 分钟收盘还在开盘区间里就先等。这一等不占掉后面走出区间时的开仓。"""
    strategy = DailyBodyStrategy(_cfg())
    waiting = strategy.evaluate("bu2612", _breakout("2026-10-23 09:45", 100.0), now=datetime(2026, 10, 23, 9, 45))
    assert waiting.action == StrategyAction.HOLD
    assert "里面" in waiting.reason
    assert strategy._used_session.get("bu2612", "") == ""
    opened = strategy.evaluate("bu2612", _breakout("2026-10-23 10:00", 105.0), now=datetime(2026, 10, 23, 10, 0))
    assert opened.action == StrategyAction.BUY_OPEN


def test_a_wick_that_comes_back_is_not_a_break() -> None:
    """15 分钟影线刺出开盘区间、收盘回到里面，不算方向，继续等。"""
    bars = _breakout("2026-10-23 09:45", 100.2)
    bars.loc[bars.index[-1], "high"] = 106.0
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "里面" in decision.reason


def test_close_below_the_opening_range_is_short() -> None:
    """15 分钟收盘跌破开盘区间，做空。"""
    bars = _breakout("2026-10-23 09:45", 98.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.SELL_OPEN
    assert "跌破开盘区间" in decision.reason


def test_bar_overlapping_eth_waits() -> None:
    """15 分钟这根还有一段落在 ETH 里，不用它的收盘定方向。"""
    bars = _breakout("2026-10-23 09:30", 110.0, freq="15min")
    decision = DailyBodyStrategy(_cfg15()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 30))
    assert decision.action == StrategyAction.HOLD
    assert "ETH" in decision.reason


def test_same_bar_can_be_scanned_again_before_fill() -> None:
    """同一根还没收成仓时，再看一次仍然是开仓，避免实盘扫第二次就把信号弄丢。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    strategy = DailyBodyStrategy(_cfg())
    moment = datetime(2026, 10, 23, 9, 45)
    first = strategy.evaluate("bu2612", bars, now=moment)
    second = strategy.evaluate("bu2612", bars, now=moment)
    assert first.action == StrategyAction.BUY_OPEN
    assert second.action == StrategyAction.BUY_OPEN


def test_1030_is_the_latest_entry() -> None:
    """10:30 是日盘最晚一根，收盘仍在开盘区间外面就可以开。"""
    bars = _breakout("2026-10-23 10:30", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 10, 30))
    assert decision.action == StrategyAction.BUY_OPEN


def test_does_not_chase_after_1030() -> None:
    """10:35 已经过了 10:30，收盘在区间外面也不追。"""
    bars = _breakout("2026-10-23 10:35", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 10, 35))
    assert decision.action == StrategyAction.HOLD
    assert "不再追" in decision.reason


def test_afternoon_reopen_does_not_start_a_new_direction() -> None:
    """13:30 是午休后开盘，不拿它再画一次开盘区间。"""
    bars = _breakout("2026-10-23 13:45", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 13, 45))
    assert decision.action == StrategyAction.HOLD
    assert decision.action != StrategyAction.BUY_OPEN


def test_night_waits_while_eth_is_still_running() -> None:
    """21:15 这根 15 分钟还有一段落在夜盘 ETH 里，不开。"""
    bars = _breakout("2026-10-23 21:15", 90.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 15))
    assert decision.action == StrategyAction.HOLD
    assert "ETH" in decision.reason


def test_night_opens_after_eth() -> None:
    """夜盘 ETH 走完后，用夜盘自己的开盘区间。收盘跌破就做空。"""
    bars = _breakout("2026-10-23 21:45", 98.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 45))
    assert decision.action == StrategyAction.SELL_OPEN
    assert "跌破开盘区间" in decision.reason


def test_night_does_not_chase_after_2215() -> None:
    """22:20 已经过了晚上十点十五，不再追。"""
    bars = _breakout("2026-10-23 22:20", 98.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 22, 20))
    assert decision.action == StrategyAction.HOLD
    assert "不再追" in decision.reason



def test_holds_without_taking_profit() -> None:
    """拿着多单时，价格继续涨也不止盈，靠箱沿止损或收盘清仓离场。"""
    bars = _bars("2026-10-23 09:00", [100] * 40)
    strategy = DailyBodyStrategy(_cfg())
    strategy.positions["bu2612"].side = "long"
    strategy.positions["bu2612"].entry_price = 100
    strategy.positions["bu2612"].initial_stop = 90
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 10, 30))
    assert decision.action == StrategyAction.HOLD
    assert "继续拿到收盘" in decision.reason




def test_flattens_at_the_session_close() -> None:
    """开着收盘清仓时，14:55 把日内的单平掉。"""
    bars = _bars("2026-10-23 14:40", [100, 101, 102, 103])
    strategy = DailyBodyStrategy(_cfg())
    strategy.positions["bu2612"].side = "long"
    strategy.positions["bu2612"].volume = 1
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 14, 55))
    assert decision.action == StrategyAction.CLOSE
    assert "收盘" in decision.reason


def test_unfinished_bar_does_not_flatten_a_bar_early() -> None:
    """拼进来的还在走的那根写着 22:55，但它表示的 22:50~22:55 还没走完，不能当收盘清仓。"""
    bars = _bars("2026-10-23 22:45", [100, 100, 100])
    strategy = DailyBodyStrategy(_cfg())
    strategy.positions["bu2612"].side = "long"
    strategy.positions["bu2612"].volume = 1
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 22, 50, 30))
    assert decision.action == StrategyAction.HOLD
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 22, 55, 10))
    assert decision.action == StrategyAction.CLOSE


def test_open_sets_box_edge_stop() -> None:
    """多单止损 = 开盘箱体最低再减 1 跳，开仓后冻结。"""
    bars = _breakout("2026-10-23 10:00", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 10, 0))
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.stop_price is not None
    assert decision.entry_price is not None
    # _breakout 的 ETH 最低是 99，默认再减 1 跳 → 98
    assert abs(decision.stop_price - 98.0) < 1e-6
    assert decision.stop_price < decision.entry_price
    assert decision.meta.get("stop_kind") == "box"
    assert "箱体下沿" in decision.reason
    assert "冻结" in decision.reason


def test_box_edge_stop_long_and_short() -> None:
    """多单守箱底外一跳，空单守箱顶外一跳。"""
    assert abs(box_edge_stop(side="long", box_low=99.0, box_high=101.0, tick=1.0) - 98.0) < 1e-6
    assert abs(box_edge_stop(side="short", box_low=99.0, box_high=101.0, tick=1.0) - 102.0) < 1e-6


def test_box_stop_exits_without_waiting_for_the_close() -> None:
    """打破箱沿就走，不用把整天的反向波动拿到收盘。"""
    bars = _breakout("2026-10-23 10:00", 105.0)
    strategy = DailyBodyStrategy(_cfg())
    opened = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 10, 0))
    strategy.on_filled("bu2612", opened.action, opened.entry_price, 1, opened.stop_price)
    crash = bars.copy()
    crash.loc[pd.Timestamp("2026-10-23 10:15")] = {
        "open": opened.entry_price,
        "high": opened.entry_price,
        "low": opened.stop_price - 5,
        "close": opened.stop_price - 1,
        "volume": 100.0,
    }
    decision = strategy.evaluate("bu2612", crash, now=datetime(2026, 10, 23, 10, 15))
    assert decision.action == StrategyAction.CLOSE
    assert "打破开盘箱体" in decision.reason
    strategy.on_filled("bu2612", StrategyAction.CLOSE, opened.stop_price, 1)
    # 止损后、截止前，反向突破第二段箱体可以再开一次（日盘上限两次）。
    again = strategy.evaluate("bu2612", crash, now=datetime(2026, 10, 23, 10, 15))
    assert again.action == StrategyAction.SELL_OPEN
    assert "第二次开仓" in again.reason


def test_pullback_inside_box_does_not_exit() -> None:
    """浮盈后抽回箱内、但没打穿箱沿止损时，继续拿着，不做回撤止损。"""
    strategy = DailyBodyStrategy(_cfg())
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 105.0, 1, 98.0)
    pullback = _bars("2026-10-23 10:05", [105.0] * 40)
    pullback.loc[pullback.index[-1], ["open", "high", "low", "close"]] = [104.0, 105.0, 99.5, 100.5]
    decision = strategy.evaluate("bu2612", pullback, now=datetime(2026, 10, 23, 13, 20))
    assert decision.action == StrategyAction.HOLD
    assert strategy.positions["bu2612"].initial_stop == 98.0


def test_signal_without_fill_does_not_burn_the_session() -> None:
    """只出过开仓信号、还没成交时，不占名额；下一根仍在箱外还能再开。"""
    strategy = DailyBodyStrategy(_cfg())
    first = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:45", 105.0), now=datetime(2026, 10, 23, 9, 45)
    )
    assert first.action == StrategyAction.BUY_OPEN
    assert strategy._used_session.get("bu2612", "") == ""
    second = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 106.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert second.action == StrategyAction.BUY_OPEN


def test_fill_burns_the_session_even_after_close() -> None:
    """成交后占掉这一段；平仓后同一段不能再开第二次。"""
    strategy = DailyBodyStrategy(_cfg())
    opened = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:45", 105.0), now=datetime(2026, 10, 23, 9, 45)
    )
    strategy.on_filled("bu2612", opened.action, opened.entry_price, 1, opened.stop_price)
    assert strategy._used_session.get("bu2612", "").endswith("09:00")
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 110.0, 1)
    again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 108.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert again.action == StrategyAction.HOLD
    assert "不再开第二次" in again.reason




def _gap_bars(end: str, close: float, prev_close: float) -> pd.DataFrame:
    """把开盘之前的收盘整体挪到 prev_close，制造这一段开盘相对上一段的跳空。"""
    bars = _breakout(end, close)
    end_ts = pd.Timestamp(end)
    opened = end_ts.normalize() + pd.Timedelta(hours=21 if end_ts.hour >= 21 else 9)
    prior = bars.index <= opened
    bars.loc[prior, ["open", "high", "low", "close"]] = prev_close
    return bars


def test_big_gap_blocks_the_open() -> None:
    """上一段收 40、这一段开 100，跳空 60 点超过默认 50，方向出来了也不追。"""
    bars = _gap_bars("2026-10-23 09:45", 105.0, 40.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "跳空" in decision.reason
    assert "不追" in decision.reason


def test_small_gap_still_opens() -> None:
    """跳空 20 点，在默认 50 以内，照常开。"""
    bars = _gap_bars("2026-10-23 09:45", 105.0, 80.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_gap_filter_off_opens_anyway() -> None:
    """skip_gap_ticks=0 时关闭过滤，60 点跳空也照开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["skip_gap_ticks"] = 0
    bars = _gap_bars("2026-10-23 09:45", 105.0, 40.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_no_previous_session_does_not_block() -> None:
    """回测区间正好从这一段开盘开始，拿不到上一段收盘，就不按跳空拦。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    bars = bars.loc[bars.index > pd.Timestamp("2026-10-23 09:00")]
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_gap_filter_also_applies_to_the_night() -> None:
    """夜盘同样按日盘最后一根收盘算跳空。"""
    bars = _gap_bars("2026-10-23 21:45", 98.0, 160.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 45))
    assert decision.action == StrategyAction.HOLD
    assert "跳空" in decision.reason


def _wide_box(end: str, close: float, width: float) -> pd.DataFrame:
    """把 ETH 里的开盘箱体撑到指定宽度，用来试箱体宽度上限。"""
    bars = _breakout(end, close)
    end_ts = pd.Timestamp(end)
    opened = end_ts.normalize() + pd.Timedelta(hours=21 if end_ts.hour >= 21 else 9)
    eth_end = opened + pd.Timedelta(minutes=20)
    inside = (bars.index > opened) & (bars.index <= eth_end)
    bars.loc[inside, "high"] = 100.0 + float(width) / 2
    bars.loc[inside, "low"] = 100.0 - float(width) / 2
    return bars


def test_too_wide_box_blocks_the_open() -> None:
    """箱体宽 110 点，超过默认上限 100，方向出来了也不开。"""
    bars = _wide_box("2026-10-23 09:45", 157.0, 110.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "箱体" in decision.reason
    assert "宽 110" in decision.reason


def test_narrow_box_still_opens() -> None:
    """箱体宽 80 点，在上限以内，照常开。"""
    bars = _wide_box("2026-10-23 09:45", 143.0, 80.0)
    decision = DailyBodyStrategy(_cfg()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_max_box_off_opens_anyway() -> None:
    """max_box_ticks=0 时关闭这条限制，110 点的箱体也照开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["max_box_ticks"] = 0
    bars = _wide_box("2026-10-23 09:45", 157.0, 110.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_day_cutoff_setting_blocks_late_open() -> None:
    """日盘开仓截止改成 10:00 后，10:15 这根走完方向出来了也不再追。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["day_cutoff"] = "10:00"
    bars = _breakout("2026-10-23 10:15", 105.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 10, 15))
    assert decision.action == StrategyAction.HOLD
    assert "10:00" in decision.reason


def test_night_cutoff_setting_blocks_late_open() -> None:
    """夜盘开仓截止改成 21:45 后，22:00 这根也不开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["night_cutoff"] = "21:45"
    bars = _breakout("2026-10-23 22:00", 105.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 22, 0))
    assert decision.action == StrategyAction.HOLD
    assert "21:45" in decision.reason


def test_cutoff_before_the_eth_never_opens() -> None:
    """截止钟点早于 ETH 走完时，这一段就完全不开，不会漏成「先等」再开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["day_cutoff"] = "09:10"
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "09:10" in decision.reason


def test_session_scope_day_blocks_the_night() -> None:
    """公共设置选「只做日盘」时，夜盘这段方向出来了也不开。"""
    cfg = _cfg()
    cfg["schedule"]["session_scope"] = "day"
    bars = _breakout("2026-10-23 21:45", 105.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 45))
    assert decision.action == StrategyAction.HOLD
    assert "只做日盘" in decision.reason


def test_session_scope_night_blocks_the_day() -> None:
    """公共设置选「只做夜盘」时，日盘这一段不开。"""
    cfg = _cfg()
    cfg["schedule"]["session_scope"] = "night"
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "只做夜盘" in decision.reason


def test_session_scope_does_not_touch_a_held_position() -> None:
    """已经拿着的仓不受时段范围限制：只做日盘时，夜盘照样按箱沿止损或收盘清仓走。"""
    cfg = _cfg()
    cfg["schedule"]["session_scope"] = "day"
    strategy = DailyBodyStrategy(cfg)
    bars = _breakout("2026-10-23 21:45", 105.0)
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 105.0, 1, initial_stop=98.0)
    decision = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 45))
    assert decision.action == StrategyAction.HOLD
    assert "拿着" in decision.reason


def _open_long_at_0945(strategy: DailyBodyStrategy) -> StrategyDecision:
    """日盘 9:45 站上区间开多，并按信号成交。返回那次决策。"""
    decision = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:45", 105.0), now=datetime(2026, 10, 23, 9, 45)
    )
    strategy.on_filled("bu2612", decision.action, decision.entry_price, 1, decision.stop_price)
    return decision


def _stop_out_long(strategy: DailyBodyStrategy, at: datetime) -> None:
    """把拿着的多单打到箱底止损（箱底 99 再减 1 跳 = 98），并回报平仓成交。"""
    bars = _bars("2026-10-23 09:45", [100, 97.0])
    decision = strategy.evaluate("bu2612", bars, now=at)
    assert decision.action == StrategyAction.CLOSE
    assert "打破" in decision.reason
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 97.0, 1)


def test_day_second_open_after_stop_uses_the_eth2_box() -> None:
    """日盘第一次止损后、截止前，用第二段 ETH 箱体再开一次。"""
    strategy = DailyBodyStrategy(_cfg())
    opened = _open_long_at_0945(strategy)
    assert opened.action == StrategyAction.BUY_OPEN
    _stop_out_long(strategy, datetime(2026, 10, 23, 9, 50))
    second = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 105.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert second.action == StrategyAction.BUY_OPEN
    assert "第二次开仓" in second.reason


def test_second_stop_ends_the_day_session() -> None:
    """第二次开仓再止损，这一段日盘就不开了。"""
    strategy = DailyBodyStrategy(_cfg())
    _open_long_at_0945(strategy)
    _stop_out_long(strategy, datetime(2026, 10, 23, 9, 50))
    second = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 105.0), now=datetime(2026, 10, 23, 10, 0)
    )
    strategy.on_filled("bu2612", second.action, second.entry_price, 1, second.stop_price)
    _stop_out_long(strategy, datetime(2026, 10, 23, 10, 10))
    third = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:15", 108.0), now=datetime(2026, 10, 23, 10, 15)
    )
    assert third.action == StrategyAction.HOLD
    assert "开满两次" in third.reason


def test_night_opens_only_once_even_after_a_stop() -> None:
    """夜盘上限 1 次：就算第一次是止损出场，也不再开第二次。"""
    strategy = DailyBodyStrategy(_cfg())
    opened = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 21:45", 98.0), now=datetime(2026, 10, 23, 21, 45)
    )
    assert opened.action == StrategyAction.SELL_OPEN
    strategy.on_filled("bu2612", opened.action, opened.entry_price, 1, opened.stop_price)
    # 空单箱顶止损 102：来一根最高 102 的 K 线打掉。
    bars = _bars("2026-10-23 21:45", [100, 101.0])
    stopped = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 50))
    assert stopped.action == StrategyAction.CLOSE
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 101.0, 1)
    again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 22:00", 98.0), now=datetime(2026, 10, 23, 22, 0)
    )
    assert again.action == StrategyAction.HOLD
    assert "夜盘只开一次" in again.reason


def test_a_non_stop_exit_does_not_unlock_a_second_open() -> None:
    """第一次不是止损出场（手动平/其它），这段不再开第二次。"""
    strategy = DailyBodyStrategy(_cfg())
    _open_long_at_0945(strategy)
    # 不经过止损决定，直接平仓成交（相当于手动平）。
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 106.0, 1)
    again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 108.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert again.action == StrategyAction.HOLD
    assert "不再开第二次" in again.reason


def test_eth_minutes2_zero_disables_the_second_open() -> None:
    """eth_minutes2 填 0：关闭二次开仓，日盘回到只开一次。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["eth_minutes2"] = 0
    strategy = DailyBodyStrategy(cfg)
    _open_long_at_0945(strategy)
    _stop_out_long(strategy, datetime(2026, 10, 23, 9, 50))
    again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 105.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert again.action == StrategyAction.HOLD
    assert "开满两次" not in again.reason


def test_second_open_waits_until_the_eth2_finishes() -> None:
    """第二段 ETH 没走完，第二次开仓也要等。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["eth_minutes2"] = 60
    strategy = DailyBodyStrategy(cfg)
    _open_long_at_0945(strategy)
    _stop_out_long(strategy, datetime(2026, 10, 23, 9, 50))
    waiting = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:55", 105.0), now=datetime(2026, 10, 23, 9, 55)
    )
    assert waiting.action == StrategyAction.HOLD
    assert "第二段 ETH" in waiting.reason
    assert "10:00" in waiting.reason
    opened = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:15", 105.0), now=datetime(2026, 10, 23, 10, 15)
    )
    assert opened.action == StrategyAction.BUY_OPEN


def test_second_open_still_respects_the_day_cutoff() -> None:
    """止损发生在截止钟点之后：不再开第二次。"""
    strategy = DailyBodyStrategy(_cfg())
    _open_long_at_0945(strategy)
    _stop_out_long(strategy, datetime(2026, 10, 23, 10, 40))
    again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:45", 105.0), now=datetime(2026, 10, 23, 10, 45)
    )
    assert again.action == StrategyAction.HOLD
    assert "不再追" in again.reason


def test_daily_body_is_standalone() -> None:
    """日内实体不再依赖已删除的流动性耗尽。"""
    made = DailyBodyStrategy(_cfg())
    assert type(made) is DailyBodyStrategy
    assert made.uses_trend_bars is False
