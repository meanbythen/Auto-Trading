"""回测成交近似：下一根开盘、破止损穿价、按跳滑点。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from src.backtest.engine import (
    FillModel,
    _exit_fill_price,
    _next_open_price,
    _position_stop,
    _stop_breached,
    _trend_frame_for_signal,
    run_backtest,
)
from src.strategy.trend_base import TrendStrategyBase
from src.strategy.types import PositionState, StrategyAction


def _frame(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """按 (时间, 开, 高, 低) 造K线，收盘取开盘值。"""
    index = pd.to_datetime([item[0] for item in rows])
    return pd.DataFrame(
        {
            "open": [item[1] for item in rows],
            "high": [item[2] for item in rows],
            "low": [item[3] for item in rows],
            "close": [item[1] for item in rows],
            "volume": [100.0 for _ in rows],
        },
        index=index,
    )


def test_next_open_spans_session_break_and_returns_next_bar_open() -> None:
    """跨午休/夜盘断开也按下一根开盘成交，不能拿上一段收盘价顶替。"""
    bars = _frame(
        [
            ("2026-10-16 09:00", 100, 101, 99),
            ("2026-10-16 09:05", 102, 103, 101),
            ("2026-10-16 09:10", 104, 105, 103),
        ]
    )
    assert _next_open_price(bars, 0, "5") == 102
    assert _next_open_price(bars, 1, "5") == 104
    assert _next_open_price(bars, 2, "5") is None

    gap = _frame(
        [
            ("2026-10-16 11:25", 100, 101, 99),
            ("2026-10-16 13:35", 102, 103, 101),
        ]
    )
    assert _next_open_price(gap, 0, "5") == 102


def test_fill_model_reads_config_and_defaults_to_one_tick() -> None:
    """默认开启下一根开盘和 1 跳滑点；配置可关掉，也支持自定义每跳价格。"""
    default = FillModel.from_cfg({})
    assert default.on_next_open is True
    assert default.slippage_ticks == 1.0
    assert default.price_tick == 1.0
    assert default.buy(100) == 101
    assert default.sell(100) == 99

    off = FillModel.from_cfg(
        {"backtest": {"fill_on_next_open": False, "slippage_ticks": 0, "price_tick": 0.1}}
    )
    assert off.on_next_open is False
    assert off.slip == 0
    assert off.buy(100) == 100


def test_stop_fill_uses_stop_price_and_gap_open() -> None:
    """没跳空时按止损价成交；跳空时按更差的开盘价成交。"""
    bar = _frame([("2026-10-16 09:00", 101, 102, 99)])
    fill = FillModel(slippage_ticks=0)
    assert _stop_breached("long", 100, bar) is True
    assert _stop_breached("short", 103, bar) is False

    # 多单：开在止损上方，K线内跌破 → 按止损价 100 卖出
    assert _exit_fill_price(side="long", stop=100, bar=bar, next_open=None, close_px=99, fill=fill) == 100
    # 多单：开在止损下方（跳空）→ 按开盘价 98，比止损更差
    jumped = _frame([("2026-10-16 09:00", 98, 99, 97)])
    assert _exit_fill_price(side="long", stop=100, bar=jumped, next_open=None, close_px=97, fill=fill) == 98
    # 空单：跳空高开 → 按开盘价 103
    up = _frame([("2026-10-16 09:00", 103, 104, 102)])
    assert _exit_fill_price(side="short", stop=100, bar=up, next_open=None, close_px=104, fill=fill) == 103


def test_close_confirmed_stop_exits_at_next_open_not_stop_price() -> None:
    """收盘确认止损：这根虽然影线打穿止损，平仓价也不能按止损价成交。"""
    bar = _frame([("2026-10-16 09:00", 101, 102, 99)])
    fill = FillModel(slippage_ticks=0)
    assert (
        _exit_fill_price(side="long", stop=100, bar=bar, next_open=98, close_px=99, fill=fill) == 100
    )
    assert (
        _exit_fill_price(
            side="long", stop=100, bar=bar, next_open=98, close_px=99, fill=fill, touch_stop=False
        )
        == 98
    )


def test_exit_without_stop_uses_next_open_then_slippage() -> None:
    """没打到止损时按下一根开盘价成交，再按卖出方向让 1 跳。"""
    bar = _frame([("2026-10-16 09:00", 100, 106, 96)])
    fill = FillModel(slippage_ticks=1, price_tick=1)
    assert _exit_fill_price(
        side="long", stop=90, bar=bar, next_open=105, close_px=104, fill=fill
    ) == 104
    assert _exit_fill_price(
        side="short", stop=110, bar=bar, next_open=105, close_px=104, fill=fill
    ) == 106


def test_exit_stop_prefers_the_updated_trail_stop() -> None:
    """平仓止损优先用尾随止损，不能被初始止损抢先。"""
    pos = SimpleNamespace(initial_stop=90.0, trail_stop=100.0)
    assert _position_stop(pos, 80.0) == 100.0
    pos.trail_stop = 0.0
    assert _position_stop(pos, None) == 90.0
    assert _position_stop(SimpleNamespace(initial_stop=0.0, trail_stop=0.0), 75.0) == 75.0


def test_daily_trend_bars_use_the_futures_trading_day() -> None:
    """盘中决策不能看到当天这根日线；但夜盘 21:00 后它已经走完，应该能看到。"""
    daily = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0]},
        index=pd.to_datetime(["2026-10-28", "2026-10-29", "2026-10-30"]),
    )
    frame = _trend_frame_for_signal(daily, pd.Timestamp("2026-10-30 09:05"), "D")
    assert frame is not None
    assert list(frame.index)[-1] == pd.Timestamp("2026-10-29")

    # 周五 21:30 的夜盘已属下周一的交易日，周五这根日线走完了。
    frame = _trend_frame_for_signal(daily, pd.Timestamp("2026-10-30 21:30"), "D")
    assert frame is not None
    assert list(frame.index)[-1] == pd.Timestamp("2026-10-30")

    # 分钟级大周期照旧包含已收盘的当根，不受日线规则影响。
    minutes = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-10-31 09:00", "2026-10-31 10:00"]),
    )
    frame = _trend_frame_for_signal(minutes, pd.Timestamp("2026-10-31 10:00"), "60")
    assert frame is not None and len(frame) == 2


def test_entry_fills_at_next_open_when_enabled() -> None:
    """开启「下一根开盘」后，开仓价就是下一根开盘价，不再用信号当根的策略价。"""
    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)] + [130.0, 131.0, 132.0]
    idx = [datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(len(closes))]
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=idx,
    )
    result = run_backtest(
        _cfg(slippage_ticks=0),
        symbol="bu",
        bars_signal=bars,
        warmup=20,
        volume=1,
    )
    assert result.performance.trades >= 1
    first = result.trades[0]
    # 突破信号在 130 那根确认，成交在下一根开盘 131。
    assert first.entry_price == 131.0


def _trend_bars() -> pd.DataFrame:
    """前 40 根在 90/110 来回，最后一根创出新高，触发趋势跟随做多。"""
    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)] + [130.0]
    idx = [datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(len(closes))]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=idx,
    )


def _cfg(**backtest) -> dict:
    return {
        "dry_run": True,
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": False,
            "market_filter": {"enabled": False},
            "trend_follow": {
                "entry_bars": 20,
                "exit_bars": 8,
                "atr_period": 14,
                "atr_mult": 10.0,
                "flatten_at_close": False,
            },
        },
        "trade": {"volume": 1, "symbol": "bu", "ctp": {"price_tick": 1}},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "risk": {"capital": 14000, "contracts": {"bu": {"multiplier": 10}}},
        "backtest": dict(backtest),
    }


def test_backtest_records_fill_note_and_slips_forced_close() -> None:
    """回测结束强平按卖出方向让 1 跳；结果里写明这次的成交近似。"""
    bars = _trend_bars()
    result = run_backtest(_cfg(), symbol="bu", bars_signal=bars, warmup=20, volume=1)
    assert result.performance.trades >= 1
    last = result.trades[-1]
    assert last.reason_exit == "回测结束强平"
    assert last.exit_price == float(bars["close"].iloc[-1]) - 1
    assert "下一根开盘成交" in result.params_snapshot["fill_note"]
    assert "滑点 1 跳" in result.params_snapshot["fill_note"]


def test_slippage_and_next_open_can_be_switched_off() -> None:
    """关掉滑点后强平按收盘价；关掉的参数也会写进结果快照。"""
    bars = _trend_bars()
    result = run_backtest(
        _cfg(fill_on_next_open=False, slippage_ticks=0),
        symbol="bu",
        bars_signal=bars,
        warmup=20,
        volume=1,
    )
    last = result.trades[-1]
    assert last.exit_price == float(bars["close"].iloc[-1])
    snap = result.params_snapshot
    assert snap["fill_on_next_open"] is False
    assert snap["slippage_ticks"] == 0
    assert "未加滑点" in snap["fill_note"]


def test_open_is_blocked_on_the_flatten_bar() -> None:
    """下一根已经是收盘清仓时点：不开仓，避免同一根开平。"""
    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)] + [130.0, 131.0]
    idx = [datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(40)]
    idx += [datetime(2026, 10, 16, 14, 50), datetime(2026, 10, 16, 15, 0)]
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=idx,
    )
    cfg = _cfg()
    cfg["strategy"]["flatten_at_close"] = True
    cfg["strategy"]["flatten_times"] = ["15:00"]
    cfg["strategy"]["trend_follow"]["flatten_at_close"] = True

    result = run_backtest(cfg, symbol="bu", bars_signal=bars, warmup=20, volume=1)

    assert result.performance.trades == 0
    assert any("收盘清仓时点" in item.reason for item in result.rejected)


def test_backtest_symbol_follows_the_watch_list() -> None:
    """不传合约时，用关注列表的第一个；trade.symbol 不会抢先。"""
    cfg = _cfg()
    cfg["strategy"]["symbols"] = ["bu", "c"]
    cfg["trade"]["symbol"] = "fu2701"
    result = run_backtest(cfg, bars_signal=_trend_bars(), warmup=20, volume=1)
    assert result.symbol == "bu"


def test_first_symbol_uses_the_watched_contract() -> None:
    """关注列表只有一个合约时，回测就用它，而不是配置里原来的下单合约。"""
    cfg = _cfg()
    cfg["strategy"]["symbols"] = ["c"]
    cfg["trade"]["symbol"] = "bu2612"
    result = run_backtest(cfg, bars_signal=_trend_bars(), warmup=20, volume=1)
    assert result.symbol == "c"


def test_symbol_margin_wins_over_the_generic_number() -> None:
    """品种表里写了保证金就用它；没写才用统一的「每手保证金」。"""
    from src.risk.sizing import PositionSizer

    cfg = {
        "risk": {
            "capital": 100000,
            "margin_per_lot": 9000,
            "contracts": {
                "bu": {"multiplier": 10, "margin": 4000},
                "c": {"multiplier": 10},
            },
        },
        "strategy": {"symbols": ["bu"]},
    }
    sizer = PositionSizer(cfg)
    assert sizer.margin_for("bu2612") == 4000
    assert sizer.margin_for("c2701") == 9000


def test_symbol_tick_wins_over_the_global_tick() -> None:
    """品种表里写了每跳价格就用它；没写才用 trade.ctp.price_tick。"""
    cfg = {
        "trade": {"ctp": {"price_tick": 1}},
        "risk": {"contracts": {"bu": {"price_tick": 0.5}, "c": {"multiplier": 10}}},
    }
    bu = FillModel.from_cfg(cfg, "bu2612")
    assert bu.price_tick == 0.5
    assert bu.buy(100) == 100.5
    assert bu.sell(100) == 99.5

    corn = FillModel.from_cfg(cfg, "c2701")
    assert corn.price_tick == 1.0


def test_universe_table_round_trips_tick_column(tmp_path) -> None:
    """品种表新增「每跳价格」列：写进去能读回来，并落到 risk.contracts。"""
    from src.risk.universe_table import parse_universe_rows, read_universe_table, write_universe_table

    path = tmp_path / "品种与合约.xlsx"
    write_universe_table(
        path,
        [
            {
                "symbol": "bu2612",
                "name": "沥青",
                "margin": 4000,
                "multiplier": 10,
                "commission": 3,
                "commission_close_today": None,
                "price_tick": 0.5,
                "enabled": True,
            },
            {
                "symbol": "c2701",
                "name": "玉米",
                "margin": 2000,
                "multiplier": 10,
                "commission": 1,
                "enabled": True,
            },
        ],
    )
    rows = read_universe_table(path)
    assert rows[0]["price_tick"] == 0.5
    assert rows[1]["price_tick"] is None
    _symbols, contracts = parse_universe_rows(rows)
    assert contracts["bu"]["price_tick"] == 0.5
    assert "price_tick" not in contracts["c"]


def _intraday_breakout_bars() -> pd.DataFrame:
    """09:00~12:15 在 90/110 来回，12:20 突破，之后一路小涨到 14:55，15:00 大幅低开。"""
    idx: list[datetime] = [
        datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(40)
    ]
    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)]
    idx.append(datetime(2026, 10, 16, 12, 20))
    closes.append(130.0)
    stamp = datetime(2026, 10, 16, 12, 25)
    price = 130.0
    while stamp <= datetime(2026, 10, 16, 14, 55):
        price += 0.5
        closes.append(price)
        idx.append(stamp)
        stamp += timedelta(minutes=5)
    idx.append(datetime(2026, 10, 16, 15, 0))
    closes.append(70.0)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=idx,
    )


def test_flatten_close_fills_at_the_current_close_not_next_open() -> None:
    """收盘清仓按当根收盘成交，不能拖到下一根（这里下一根直接低开 60 点）。"""
    bars = _intraday_breakout_bars()
    cfg = _cfg(slippage_ticks=0)
    cfg["strategy"]["flatten_at_close"] = True
    cfg["strategy"]["flatten_times"] = ["14:55"]
    cfg["strategy"]["trend_follow"]["flatten_at_close"] = True

    result = run_backtest(cfg, symbol="bu", bars_signal=bars, warmup=20, volume=1)

    assert result.performance.trades >= 1
    last = result.trades[-1]
    assert "收盘清仓" in last.reason_exit
    expected = float(bars.loc[pd.Timestamp("2026-10-16 14:55"), "close"])
    assert last.exit_price == expected


def test_inherit_position_opens_at_segment_start_for_any_engine() -> None:
    """换月接仓不再只对 macd_swing 生效：新段第一根就按上一段手数把仓补回来。"""
    bars = _intraday_breakout_bars()
    result = run_backtest(
        _cfg(slippage_ticks=0),
        symbol="bu",
        bars_signal=bars,
        warmup=20,
        volume=1,
        inherit_position={"side": "long", "volume": 3},
    )

    assert result.performance.trades >= 1
    first = result.trades[0]
    assert first.volume == 3
    assert first.side == "long"
    # warmup=20，段首就是第 21 根；成交在下一根开盘，最多晚一根。
    assert pd.Timestamp(first.entry_time) <= pd.Timestamp(bars.index[20]) + timedelta(minutes=5)


class _RaiseStop(TrendStrategyBase):
    """测试用：每根都把止损推到本根最高价上方，模拟大阳线抬高止损。"""

    label = "测试"

    def prepare(self, bars: pd.DataFrame) -> dict[str, object]:
        return {}

    def entry_signal(self, bars: pd.DataFrame, bar: pd.Series, ind: dict[str, object]):
        return None

    def trail_stop(
        self, bars: pd.DataFrame, bar: pd.Series, pos: PositionState, ind: dict[str, object]
    ) -> float:
        pos.trail_stop = float(bar["high"]) + 1
        return pos.trail_stop


def _stop_test_cfg() -> dict:
    return {
        "strategy": {
            "engine": "supertrend",
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": [],
            "flatten_at_close": False,
        },
        "trade": {"symbol": "bu", "volume": 1},
        "schedule": {"trading_sessions": []},
    }


def test_new_trail_stop_only_applies_from_the_next_bar() -> None:
    """本根新算的止损不能拿来判本根：大阳线自己抬高止损，不该当场把自己打掉。"""
    strat = _RaiseStop(_stop_test_cfg())
    pos = PositionState(
        symbol="bu", side="long", volume=1, entry_price=100, initial_stop=90, trail_stop=90
    )
    first = pd.DataFrame(
        {"open": [100.0], "high": [110.0], "low": [99.0], "close": [105.0], "volume": [100]},
        index=[pd.Timestamp("2026-10-16 09:00")],
    )
    decision = strat._manage(
        "bu", first, first.iloc[0], str(first.index[0]), pos, {}, datetime(2026, 10, 16, 9, 0)
    )
    assert decision.action != StrategyAction.CLOSE
    assert pos.trail_stop == 111.0

    second = pd.DataFrame(
        {"open": [105.0], "high": [115.0], "low": [89.0], "close": [95.0], "volume": [100]},
        index=[pd.Timestamp("2026-10-16 09:05")],
    )
    decision = strat._manage(
        "bu", second, second.iloc[0], str(second.index[0]), pos, {}, datetime(2026, 10, 16, 9, 5)
    )
    assert decision.action == StrategyAction.CLOSE
    assert decision.stop_price == 111.0


