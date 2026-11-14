"""
日内实体的测试。

方向跟信号周期走。默认用例用 15 分钟（更细的 5 分钟会先收成 15）。
ETH 走完后，收盘离开开盘区间才开。过了 10:30 或 22:15 不追。
"""

from datetime import datetime, time as dtime, timedelta

import pandas as pd

from src.strategy.daily_body import DailyBodyStrategy, box_edge_stop, opening_box_levels
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
            # 测试数据太短（通常只有几十根），ADX/ATR 分位会被区间过滤误拦。
            "market_filter": {"enabled": False},
            "daily_body": {
                "eth_minutes": 20,
                "regime_filter": False,
                # 测试数据短、ATR 不稳定：默认关掉三道 ATR 闸，需要时再单独打开。
                "skip_gap_atr_mult": 0,
                "max_box_atr_mult": 0,
                "max_chase_atr_mult": 0,
                "filter_atr_period": 4,
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


def _cfg_chase(chase: float = 5.0) -> dict:
    """急破不追：收盘离破位超过 chase 倍开盘前 ATR 就先等回抽。"""
    cfg = _cfg5()
    cfg["strategy"]["daily_body"]["max_chase_atr_mult"] = float(chase)
    return cfg


def _next_bar(bars: pd.DataFrame, stamp: str, close: float) -> pd.DataFrame:
    """在已收盘序列后面补一根，模拟信号周期的下一根收线。"""
    row = bars.iloc[[-1]].copy()
    row.index = pd.DatetimeIndex([pd.Timestamp(stamp)])
    row["open"] = close
    row["close"] = close
    row["high"] = close + 0.2
    row["low"] = close - 0.2
    return pd.concat([bars, row])


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


def test_weekend_gap_is_not_close_window() -> None:
    """周一早上拿到的最后一根还是周五 23:00：不能因此判成「已经到收盘时间」。"""
    bars = _breakout("2026-10-30 23:00", 100.0)
    decision = DailyBodyStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 11, 2, 9, 10)
    )
    assert decision.action == StrategyAction.HOLD
    assert "已经到收盘时间" not in decision.reason
    assert "ETH" in decision.reason


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
    assert decision.action != StrategyAction.BUY_OPEN
    assert "站上开盘区间" not in decision.reason


def test_five_minute_signal_opens_on_its_own_bar() -> None:
    """信号改成 5 分钟时，用 5 分钟自己的收盘判断，不再强行收成 15。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(_cfg5()).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN
    assert "5分钟" in decision.reason
    assert "站上开盘区间" in decision.reason


def test_far_breakout_does_not_open_immediately() -> None:
    """收线离箱底 9 跳、超过 5 跳上限：当场不开，也不占这一段的名额。"""
    strategy = DailyBodyStrategy(_cfg_chase())
    decision = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:45", 90.0), now=datetime(2026, 10, 23, 9, 45)
    )
    assert decision.action == StrategyAction.HOLD
    assert "急破不追" in decision.reason
    assert strategy._used_session.get("bu2612", "") == ""


def test_pullback_into_range_opens_in_the_same_direction() -> None:
    """急破后回到箱底 3 跳内、收盘没有有效站上 EMA：按原信号做空。"""
    strategy = DailyBodyStrategy(_cfg_chase())
    bars = _breakout("2026-10-23 09:45", 90.0)
    first = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert first.action == StrategyAction.HOLD
    second = strategy.evaluate(
        "bu2612", _next_bar(bars, "2026-10-23 09:50", 96.0), now=datetime(2026, 10, 23, 9, 50)
    )
    assert second.action == StrategyAction.SELL_OPEN
    assert "回抽确认" in second.reason


def test_pullback_that_breaks_ema_invalidates_the_breakout() -> None:
    """回抽到破位附近，但收盘已经有效站上 EMA：这次突破作废，不接刀。"""
    cfg = _cfg_chase()
    cfg["strategy"]["daily_body"]["ema_period"] = 3
    cfg["strategy"]["daily_body"]["ema_buffer_ticks"] = 1
    strategy = DailyBodyStrategy(cfg)
    bars = _breakout("2026-10-23 09:45", 90.0)
    first = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert first.action == StrategyAction.HOLD
    second = strategy.evaluate(
        "bu2612", _next_bar(bars, "2026-10-23 09:50", 98.5), now=datetime(2026, 10, 23, 9, 50)
    )
    assert second.action == StrategyAction.HOLD
    assert "作废" in second.reason


def test_late_scan_still_uses_the_bar_that_just_closed() -> None:
    """收线后 37 秒才扫到，也算这根：不能因为秒数不是 00 就把整根漏掉。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 9, 45, 37)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert "站上开盘区间" in decision.reason


def test_scan_more_than_one_minute_after_close_still_uses_the_bar() -> None:
    """收线后 70 秒才扫到、数据才到，15 分钟线仍认这根，不能因为超过 60 秒就漏单。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 9, 46, 10)
    )
    assert decision.action == StrategyAction.BUY_OPEN
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
    assert "第二ETH窗口开仓" in again.reason


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
    # 上一段收在 prev_close，但仍留一点高低，好让开盘前 ATR 算得出来。
    bars.loc[prior, "open"] = prev_close
    bars.loc[prior, "close"] = prev_close
    bars.loc[prior, "high"] = prev_close + 1.0
    bars.loc[prior, "low"] = prev_close - 1.0
    return bars


def test_big_gap_blocks_the_open() -> None:
    """上一段收 40、这一段开 100，跳空远大于 2.5 倍开盘前 ATR，方向出来了也不追。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["skip_gap_atr_mult"] = 2.5
    bars = _gap_bars("2026-10-23 09:45", 105.0, 40.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "跳空" in decision.reason
    assert "不追" in decision.reason
    assert decision.meta.get("blocked") is True
    assert decision.meta.get("side") == "long"


def test_small_gap_still_opens() -> None:
    """跳空大约 2 点，小于 2.5 倍开盘前 ATR，照常开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["skip_gap_atr_mult"] = 2.5
    bars = _gap_bars("2026-10-23 09:45", 105.0, 98.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_gap_filter_off_opens_anyway() -> None:
    """skip_gap_atr_mult=0 时关闭过滤，60 点跳空也照开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["skip_gap_atr_mult"] = 0
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
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["skip_gap_atr_mult"] = 2.5
    bars = _gap_bars("2026-10-23 21:45", 98.0, 160.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 45))
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
    """箱体宽 20 点，超过 5 倍开盘前 ATR，方向出来了也不开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["max_box_atr_mult"] = 5.0
    bars = _wide_box("2026-10-23 09:45", 157.0, 20.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.HOLD
    assert "箱体" in decision.reason
    assert "宽 20" in decision.reason
    # 标成「有信号但被过滤」，回测结果才能把这条和原因一起列出来。
    assert decision.meta.get("blocked") is True
    assert decision.meta.get("side") == "long"


def test_narrow_box_still_opens() -> None:
    """箱体宽 3 点，在 5 倍开盘前 ATR 以内，照常开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["max_box_atr_mult"] = 5.0
    bars = _wide_box("2026-10-23 09:45", 143.0, 3.0)
    decision = DailyBodyStrategy(cfg).evaluate("bu2612", bars, now=datetime(2026, 10, 23, 9, 45))
    assert decision.action == StrategyAction.BUY_OPEN


def test_max_box_off_opens_anyway() -> None:
    """max_box_atr_mult=0 时关闭这条限制，110 点的箱体也照开。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["max_box_atr_mult"] = 0
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
    """日盘第一窗口止损后、截止前，用第二ETH窗口的箱子再开一次。"""
    strategy = DailyBodyStrategy(_cfg())
    opened = _open_long_at_0945(strategy)
    assert opened.action == StrategyAction.BUY_OPEN
    _stop_out_long(strategy, datetime(2026, 10, 23, 9, 50))
    second = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 105.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert second.action == StrategyAction.BUY_OPEN
    assert "第二ETH窗口开仓" in second.reason


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
    """eth_minutes2 填 0：关掉第二ETH窗口，日盘回到只开一次。"""
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
    """第二ETH窗口没走完，止损后再开也要等。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["eth_minutes2"] = 60
    strategy = DailyBodyStrategy(cfg)
    _open_long_at_0945(strategy)
    _stop_out_long(strategy, datetime(2026, 10, 23, 9, 50))
    waiting = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:55", 105.0), now=datetime(2026, 10, 23, 9, 55)
    )
    assert waiting.action == StrategyAction.HOLD
    assert "第二ETH窗口" in waiting.reason
    assert "10:00" in waiting.reason
    opened = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:15", 105.0), now=datetime(2026, 10, 23, 10, 15)
    )
    assert opened.action == StrategyAction.BUY_OPEN


def _breakout_eth2_lower(end: str, close: float, extra_low: float = 95.0) -> pd.DataFrame:
    """第一段 ETH 仍是 99～101；9:20 到 9:30 把最低打到 extra_low，第二段箱子更宽。"""
    bars = _breakout(end, close)
    opened = pd.Timestamp(end).normalize() + pd.Timedelta(hours=9)
    eth1 = opened + pd.Timedelta(minutes=20)
    eth2 = opened + pd.Timedelta(minutes=30)
    for stamp in bars.index:
        if eth1 < stamp <= eth2:
            bars.at[stamp, "low"] = extra_low
            bars.at[stamp, "high"] = max(float(bars.at[stamp, "high"]), 101.0)
    last = bars.index[-1]
    bars.at[last, "close"] = close
    bars.at[last, "high"] = max(float(bars.at[last, "high"]), close + 0.2)
    bars.at[last, "low"] = min(float(bars.at[last, "low"]), close - 0.2)
    return bars


def test_eth1_breakout_before_eth2_still_uses_the_first_box() -> None:
    """第一个窗口走完、第二个还没到：有突破仍按第一段箱子开，不等第二段。"""
    strategy = DailyBodyStrategy(_cfg5())
    decision = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:25", 105.0), now=datetime(2026, 10, 23, 9, 25)
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert "丢掉第一窗口" not in decision.reason
    assert decision.stop_price is not None
    assert abs(decision.stop_price - 98.0) < 1e-6


def test_no_open_in_eth1_then_reassess_with_eth2() -> None:
    """第一窗口没开成，过了第二ETH窗口之后，丢掉第一窗口，只认第二口箱子。"""
    strategy = DailyBodyStrategy(_cfg5())
    waiting = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:25", 100.0), now=datetime(2026, 10, 23, 9, 25)
    )
    assert waiting.action == StrategyAction.HOLD
    opened = strategy.evaluate(
        "bu2612",
        _breakout_eth2_lower("2026-10-23 09:35", 105.0, extra_low=95.0),
        now=datetime(2026, 10, 23, 9, 35),
    )
    assert opened.action == StrategyAction.BUY_OPEN
    assert "丢掉第一窗口" in opened.reason
    assert "第二ETH窗口" in opened.reason
    assert opened.stop_price is not None
    # 第二段最低 95，再减 1 跳 → 94；若还用第一段箱子会是 98。
    assert abs(opened.stop_price - 94.0) < 1e-6


def test_before_eth2_still_uses_eth1_even_if_eth1_had_no_fill() -> None:
    """第一窗口没开成，但还没到第二ETH窗口：有突破仍按第一口箱子开。"""
    cfg = _cfg5()
    cfg["strategy"]["daily_body"]["eth_minutes2"] = 60
    strategy = DailyBodyStrategy(cfg)
    still = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:25", 100.0), now=datetime(2026, 10, 23, 9, 25)
    )
    assert still.action == StrategyAction.HOLD
    too_soon = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:40", 105.0), now=datetime(2026, 10, 23, 9, 40)
    )
    assert too_soon.action == StrategyAction.BUY_OPEN
    assert "丢掉第一窗口" not in too_soon.reason
    assert too_soon.stop_price is not None
    assert abs(too_soon.stop_price - 98.0) < 1e-6


def test_eth1_chase_wait_is_dropped_after_eth2() -> None:
    """过了第二ETH窗口后，第一窗口留下的急破等待一并丢掉。"""
    strategy = DailyBodyStrategy(_cfg_chase())
    first = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:25", 90.0), now=datetime(2026, 10, 23, 9, 25)
    )
    assert first.action == StrategyAction.HOLD
    assert "急破不追" in first.reason
    assert "bu2612" in strategy._chase_wait
    later = strategy.evaluate(
        "bu2612",
        _breakout_eth2_lower("2026-10-23 09:35", 105.0, extra_low=95.0),
        now=datetime(2026, 10, 23, 9, 35),
    )
    assert "bu2612" not in strategy._chase_wait
    assert later.action == StrategyAction.BUY_OPEN
    assert "丢掉第一窗口" in later.reason


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


def test_signal_still_counts_within_sixty_seconds() -> None:
    """数据源晚到：9:45 这根收线后 60 秒内仍按它定方向。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 9, 45, 50)
    )
    assert decision.action == StrategyAction.BUY_OPEN


def test_signal_expires_after_one_full_bar() -> None:
    """过了整整一根信号周期再拿它开，就是过时信号（午休后也不用上午最后一根）。"""
    bars = _breakout("2026-10-23 09:45", 105.0)
    decision = DailyBodyStrategy(_cfg()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 10, 0, 1)
    )
    assert decision.action == StrategyAction.HOLD
    assert "整整一根周期" in decision.reason


def test_morning_last_bar_not_used_after_lunch() -> None:
    """11:30 这根到了 13:31 已经过时，不能当下午的开仓信号。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["day_cutoff"] = "15:00"
    bars = _breakout("2026-10-23 11:30", 105.0)
    decision = DailyBodyStrategy(cfg).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 13, 31)
    )
    assert decision.action == StrategyAction.HOLD
    assert "整整一根周期" in decision.reason


def test_flatten_window_crosses_midnight() -> None:
    """清仓钟点设在 23:55 时，次日 00:01 仍应落在 10 分钟窗口里。"""
    cfg = _cfg()
    cfg["strategy"]["flatten_times"] = ["23:55"]
    strategy = DailyBodyStrategy(cfg)
    assert strategy.is_flatten_time(datetime(2026, 10, 23, 23, 56))
    assert strategy.is_flatten_time(datetime(2026, 10, 24, 0, 1))
    assert not strategy.is_flatten_time(datetime(2026, 10, 24, 0, 6))


def test_stale_stop_pending_is_not_a_stop_exit() -> None:
    """止损单一直没成交、挂起超时后手工平仓，这段不该多算一次「止损出场」。"""
    strategy = DailyBodyStrategy(_cfg())
    _open_long_at_0945(strategy)
    strategy._pending_stop["bu2612"] = True
    # 15 分钟周期最多等一根 K 线（15 分钟），挂起 30 分钟一定过期。
    strategy._pending_stop_at["bu2612"] = datetime.now() - timedelta(minutes=30)
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 106.0, 1)
    assert strategy._stop_closed["bu2612"] is False
    again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:00", 108.0), now=datetime(2026, 10, 23, 10, 0)
    )
    assert again.action == StrategyAction.HOLD
    assert "不再开第二次" in again.reason


def test_fresh_stop_pending_still_counts_as_a_stop_exit() -> None:
    """成交回报很快回来时，仍按止损出场处理，日盘允许第二次。"""
    strategy = DailyBodyStrategy(_cfg())
    _open_long_at_0945(strategy)
    strategy._pending_stop["bu2612"] = True
    strategy._pending_stop_at["bu2612"] = datetime.now()
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 97.0, 1)
    assert strategy._stop_closed["bu2612"] is True


def test_held_position_without_stop_restores_the_box_edge() -> None:
    """手工录入没填止损价：按这一段箱体补一个箱沿止损，补完就冻结。"""
    strategy = DailyBodyStrategy(_cfg())
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 105.0, 1, initial_stop=0.0)
    assert strategy._effective_stop(strategy.positions["bu2612"]) == 0.0
    decision = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:30", 105.0), now=datetime(2026, 10, 23, 10, 30)
    )
    assert decision.action == StrategyAction.HOLD
    # _breakout 的 ETH 框是 99~101，多单箱底再减 1 跳 → 98
    assert abs(strategy.positions["bu2612"].initial_stop - 98.0) < 1e-6
    assert strategy.positions["bu2612"].trail_armed is True


def test_restored_box_stop_still_exits_on_a_break() -> None:
    """补出来的箱沿止损照样生效：跌破箱底就平，不用等到收盘。"""
    strategy = DailyBodyStrategy(_cfg())
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 105.0, 1, initial_stop=0.0)
    strategy.evaluate(
        "bu2612", _breakout("2026-10-23 10:30", 105.0), now=datetime(2026, 10, 23, 10, 30)
    )
    crash = _bars("2026-10-23 10:35", [105.0, 97.0])
    decision = strategy.evaluate("bu2612", crash, now=datetime(2026, 10, 23, 10, 35))
    assert decision.action == StrategyAction.CLOSE
    assert "打破" in decision.reason


def test_restored_stop_waits_while_eth_is_still_running() -> None:
    """ETH 没走完时箱体是半截的，先不补止损，免得贴在太近的位置一开就被打。"""
    cfg = _cfg()
    cfg["strategy"]["daily_body"]["eth_minutes"] = 60
    strategy = DailyBodyStrategy(cfg)
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 105.0, 1, initial_stop=0.0)
    decision = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 09:45", 105.0), now=datetime(2026, 10, 23, 9, 45)
    )
    assert decision.action == StrategyAction.HOLD
    assert strategy._effective_stop(strategy.positions["bu2612"]) == 0.0


def _wick_below_stop_but_closes_back() -> pd.DataFrame:
    """09:50 这根影线打到 97（止损 98），收盘 104.5 又回到止损上方。"""
    bars = _bars("2026-10-23 09:45", [100, 104.5])
    bars.loc[bars.index[-1], "low"] = 97
    return bars


def test_touch_mode_stops_on_a_wick_that_closes_back() -> None:
    """默认触及即止损：影线碰到箱底就走，不等这根收盘。"""
    strategy = DailyBodyStrategy(_cfg())
    _open_long_at_0945(strategy)
    decision = strategy.evaluate(
        "bu2612", _wick_below_stop_but_closes_back(), now=datetime(2026, 10, 23, 9, 50)
    )
    assert decision.action == StrategyAction.CLOSE
    assert "打破" in decision.reason


def test_close_mode_ignores_a_wick_that_closes_back() -> None:
    """收盘价确认：影线刺穿箱底、收盘仍站在止损上方，继续拿。"""
    cfg = _cfg()
    cfg["strategy"]["stop_mode"] = "close"
    strategy = DailyBodyStrategy(cfg)
    assert strategy.stop_on_touch is False
    _open_long_at_0945(strategy)
    decision = strategy.evaluate(
        "bu2612", _wick_below_stop_but_closes_back(), now=datetime(2026, 10, 23, 9, 50)
    )
    assert decision.action == StrategyAction.HOLD


def test_close_mode_exits_when_the_bar_closes_below_the_stop() -> None:
    """收盘价确认：收盘跌破箱底才平，理由写成「收盘跌破」。"""
    cfg = _cfg()
    cfg["strategy"]["stop_mode"] = "close"
    strategy = DailyBodyStrategy(cfg)
    _open_long_at_0945(strategy)
    decision = strategy.evaluate(
        "bu2612", _bars("2026-10-23 09:45", [100, 97.0]), now=datetime(2026, 10, 23, 9, 50)
    )
    assert decision.action == StrategyAction.CLOSE
    assert "收盘跌破" in decision.reason


def test_stop_mode_accepts_config_words_and_labels() -> None:
    """配置里写 close 或配置页的中文标签，都认成收盘确认；默认仍是触及即止损。"""
    from src.strategy.types import stop_mode_from_label, stop_mode_label

    assert DailyBodyStrategy(_cfg()).stop_on_touch is True
    cfg = _cfg()
    cfg["strategy"]["stop_mode"] = "收盘价确认"
    assert DailyBodyStrategy(cfg).stop_mode == "close"
    assert stop_mode_from_label(stop_mode_label("close")) == "close"
    assert stop_mode_from_label("触及即止损") == "touch"
    # 旧配置写在 daily_body 里也认，升级后不必改 yaml。
    legacy = _cfg()
    legacy["strategy"]["daily_body"]["stop_mode"] = "close"
    assert DailyBodyStrategy(legacy).stop_mode == "close"


def test_duplicate_close_column_does_not_raise_ambiguous() -> None:
    """两列都叫 close 时仍能判断，不能弹出 array ambiguous。"""
    bars = _breakout("2026-10-23 09:45:00", 103.0)
    bars = pd.concat([bars, bars[["close"]]], axis=1)
    decision = DailyBodyStrategy(_cfg5()).evaluate(
        "bu2612", bars, now=datetime(2026, 10, 23, 9, 45)
    )
    assert decision.action in (StrategyAction.HOLD, StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)


def test_silver_night_after_midnight_still_counts_as_night() -> None:
    """白银夜盘到凌晨：01:00 仍算 21:00 这一段，不会说还没开盘。"""
    cfg = _cfg()
    cfg["schedule"]["trading_sessions"] = [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "02:30"]]
    strategy = DailyBodyStrategy(cfg)
    evening = datetime(2026, 11, 9, 21, 40)
    morning = datetime(2026, 11, 10, 1, 5)
    assert strategy._anchor(morning) == dtime(21, 0)
    assert strategy.in_entry_session(morning)
    assert strategy._session_key(evening, dtime(21, 0)) == strategy._session_key(morning, dtime(21, 0))


def test_opening_box_after_midnight_uses_yesterdays_night_open() -> None:
    """凌晨画夜盘箱子，要用昨天 21:00 的 ETH，不能去找今天晚上还没走的 21:00。"""
    index = pd.date_range("2026-11-09 21:05", periods=4, freq="5min")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 102.0, 101.5, 101.0],
            "low": [99.0, 99.5, 99.0, 99.2],
            "close": [100.0, 101.0, 100.5, 100.2],
            "volume": [100.0] * 4,
        },
        index=index,
    )
    high, low, wait = opening_box_levels(
        bars, datetime(2026, 11, 10, 1, 5), dtime(21, 0), 20, "5"
    )
    assert wait == ""
    assert high == 102.0
    assert low == 99.0


def test_night_cutoff_blocks_chase_after_midnight() -> None:
    """夜盘截止仍是当晚 22:15 时，凌晨不要再开新仓。"""
    cfg = _cfg()
    cfg["schedule"]["trading_sessions"] = [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "02:30"]]
    bars = _bars("2026-11-09 21:00", [100.0] * 50)
    decision = DailyBodyStrategy(cfg).evaluate(
        "bu2612", bars, now=datetime(2026, 11, 10, 1, 5)
    )
    assert decision.action == StrategyAction.HOLD
    assert "不再追" in decision.reason or "过了" in decision.reason


def _linked_cfg() -> dict:
    """交易时段选夜日连续。"""
    cfg = _cfg()
    cfg["schedule"]["session_scope"] = "linked"
    return cfg


def test_linked_does_not_flatten_at_night_close() -> None:
    """夜日连续：夜盘 22:55 不平仓，避免方向对了却在夜盘收盘丢掉跳空。"""
    cfg = _linked_cfg()
    strategy = DailyBodyStrategy(cfg)
    strategy.positions["bu2612"].side = "long"
    strategy.positions["bu2612"].volume = 1
    strategy.positions["bu2612"].initial_stop = 90.0
    bars = _bars("2026-10-23 22:40", [100, 101, 102, 103])
    night = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 22, 56))
    assert night.action == StrategyAction.HOLD
    assert "拿着" in night.reason


def test_linked_flattens_at_day_close_not_weekend() -> None:
    """夜日连续：日盘 14:55 才清仓；周末白天的 14:55 不算。"""
    cfg = _linked_cfg()
    strategy = DailyBodyStrategy(cfg)
    strategy.positions["bu2612"].side = "long"
    strategy.positions["bu2612"].volume = 1
    strategy.positions["bu2612"].initial_stop = 90.0
    bars = _bars("2026-10-24 14:40", [100, 101, 102, 103])
    weekend = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 24, 14, 56))
    assert weekend.action == StrategyAction.HOLD
    monday_bars = _bars("2026-10-26 14:40", [100, 101, 102, 103])
    monday = strategy.evaluate("bu2612", monday_bars, now=datetime(2026, 10, 26, 14, 56))
    assert monday.action == StrategyAction.CLOSE
    assert "收盘" in monday.reason


def test_linked_night_stop_then_day_eth1_can_open() -> None:
    """夜盘开过并止损后，日盘按 9:00+ETH1 再画箱子，不算夜盘第二次。"""
    strategy = DailyBodyStrategy(_linked_cfg())
    opened = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 21:45", 98.0), now=datetime(2026, 10, 23, 21, 45)
    )
    assert opened.action == StrategyAction.SELL_OPEN
    strategy.on_filled("bu2612", opened.action, opened.entry_price, 1, opened.stop_price)
    bars = _bars("2026-10-23 21:45", [100, 101.0])
    stopped = strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 50))
    assert stopped.action == StrategyAction.CLOSE
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 101.0, 1)
    night_again = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 22:00", 98.0), now=datetime(2026, 10, 23, 22, 0)
    )
    assert night_again.action == StrategyAction.HOLD
    assert "等到日盘" in night_again.reason
    day = strategy.evaluate(
        "bu2612", _breakout("2026-10-26 09:45", 105.0), now=datetime(2026, 10, 26, 9, 45)
    )
    assert day.action == StrategyAction.BUY_OPEN
    assert "第一ETH窗口" in day.reason


def test_linked_three_windows_then_stop() -> None:
    """夜日连续整段最多三次：夜盘、日盘ETH1、日盘ETH2。"""
    strategy = DailyBodyStrategy(_linked_cfg())
    night = strategy.evaluate(
        "bu2612", _breakout("2026-10-23 21:45", 98.0), now=datetime(2026, 10, 23, 21, 45)
    )
    strategy.on_filled("bu2612", night.action, night.entry_price, 1, night.stop_price)
    bars = _bars("2026-10-23 21:45", [100, 101.0])
    strategy.evaluate("bu2612", bars, now=datetime(2026, 10, 23, 21, 50))
    strategy.on_filled("bu2612", StrategyAction.CLOSE, 101.0, 1)
    first = strategy.evaluate(
        "bu2612", _breakout("2026-10-26 09:45", 105.0), now=datetime(2026, 10, 26, 9, 45)
    )
    assert first.action == StrategyAction.BUY_OPEN
    strategy.on_filled("bu2612", first.action, first.entry_price, 1, first.stop_price)
    _stop_out_long(strategy, datetime(2026, 10, 26, 9, 50))
    second = strategy.evaluate(
        "bu2612", _breakout("2026-10-26 10:00", 108.0), now=datetime(2026, 10, 26, 10, 0)
    )
    assert second.action == StrategyAction.BUY_OPEN
    assert "第二ETH窗口" in second.reason
    strategy.on_filled("bu2612", second.action, second.entry_price, 1, second.stop_price)
    _stop_out_long(strategy, datetime(2026, 10, 26, 10, 10))
    third = strategy.evaluate(
        "bu2612", _breakout("2026-10-26 10:15", 110.0), now=datetime(2026, 10, 26, 10, 15)
    )
    assert third.action == StrategyAction.HOLD
    assert "开满" in third.reason
