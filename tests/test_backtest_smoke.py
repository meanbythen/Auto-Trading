"""回测引擎冒烟测试：用演示K线跑通，不依赖外网。"""

from __future__ import annotations

from src.backtest.engine import run_backtest
from src.data.kline import KlineProvider


def test_backtest_runs_on_demo_bars() -> None:
    cfg = {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "daily_body": {
                "eth_minutes": 0,
                "regime_filter": False,
            },
        },
        "trade": {"symbol": "bu2612", "volume": 1, "executor": "sim"},
        "risk": {"capital": 1_000_000, "contracts": {"bu": {"multiplier": 10}}},
        "data": {"source": "demo", "cache_dir": "data/klines", "min_bars": 60},
        "schedule": {"trading_sessions": [["09:00", "23:00"]]},
    }
    provider = KlineProvider(cfg)
    bars = provider.fetch("bu2612", "15")
    result = run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)
    assert result.bars >= 60
    assert result.performance.trades >= 0
    assert isinstance(result.performance.score, float)
    # 评估需要权益时间轴；成交里统一带开仓 ADX 字段（预热期可能为 None）。
    assert len(result.equity_times) == len(result.equity)
    assert result.evaluation is not None
    assert "evaluation" in result.to_dict()
    assert all(hasattr(trade, "adx_at_entry") for trade in result.trades)


def test_filter_bars_by_range_uses_futures_trading_day() -> None:
    """开始日带上前一晚夜盘；结束日晚上的夜盘属于下一交易日，不算进来。"""
    import pandas as pd

    from src.backtest.engine import filter_bars_by_range

    idx = pd.date_range("2026-09-17 09:00", "2026-09-21 15:00", freq="15min")
    close = pd.Series(1.0, index=idx)
    df = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1}
    )
    cut = filter_bars_by_range(df, "2026-09-18", "2026-09-18")
    assert cut.index[0] == pd.Timestamp("2026-09-17 21:00")
    assert cut.index[-1] == pd.Timestamp("2026-09-18 20:45")
    # 09-18 凌晨那根还属于 09-18 这个交易日。
    assert pd.Timestamp("2026-09-18 00:30") in cut.index
    # 09-18 21:15 已是 09-21（周末顺延）这个交易日。
    assert pd.Timestamp("2026-09-18 21:15") not in cut.index


def test_filter_bars_keep_before_leaves_lookback() -> None:
    """开始日之前留下的K线只给均线预热，结束日不变。"""
    import pandas as pd

    from src.backtest.engine import filter_bars_by_range

    idx = pd.date_range("2026-09-17 09:00", "2026-09-18 15:00", freq="15min")
    close = pd.Series(1.0, index=idx)
    df = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1}
    )
    tight = filter_bars_by_range(df, "2026-09-18", "2026-09-18")
    wide = filter_bars_by_range(df, "2026-09-18", "2026-09-18", keep_before=3)
    assert wide.index[-1] == tight.index[-1]
    assert len(wide) == len(tight) + 3
    assert wide.index[0] < tight.index[0]


def test_filled_dates_cut_bars_and_early_start_is_explained() -> None:
    """开始日期早于行情时，结果要说明实际从哪天开始；区间落在行情内时必须截短。"""
    import pandas as pd

    from src.backtest.engine import coverage_note, filter_bars_by_range

    idx = pd.date_range("2026-09-16 09:00", periods=400, freq="15min")
    close = pd.Series(range(len(idx)), index=idx, dtype=float) + 4000
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 100,
        }
    )
    cut = filter_bars_by_range(df, "2026-09-18", "2026-09-18")
    assert len(cut) < len(df)
    # 开始日早上算前一晚 21:00 起，结束日晚上 21:00 之后归下一交易日。
    assert str(cut.index[0]).startswith("2026-09-17")
    assert str(cut.index[-1]).startswith("2026-09-18")

    note = coverage_note(
        "2026-01-01",
        "2026-10-15",
        "2026-09-16 09:50:00",
        "2026-10-15 15:00:00",
        "2026-09-16 09:50:00",
        "2026-10-15 15:00:00",
    )
    assert "2026-01-01" in note
    assert "2026-09-16" in note

    cfg = {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55"],
            "daily_body": {"eth_minutes": 0},
        },
        "trade": {"symbol": "bu2612", "volume": 1, "executor": "sim"},
        "risk": {"capital": 1_000_000, "contracts": {"bu": {"multiplier": 10}}},
        "data": {"source": "demo"},
        "schedule": {"trading_sessions": [["09:00", "23:00"]]},
        "backtest": {"max_bars": 1200, "warmup": 20},
    }
    result = run_backtest(
        cfg,
        symbol="bu2612",
        bars_signal=df,
        bars_trail=df,
        warmup=20,
        start_date="2026-09-18",
        end_date="2026-09-18",
    )
    snap = result.params_snapshot
    # 09-18 这个交易日从前一晚 21:00 的夜盘开始。
    assert str(snap["data_from"]).startswith("2026-09-17 21:00")
    assert str(snap["available_from"]).startswith("2026-09-16")
    assert "已按你填的日期截取" in snap["coverage_note"]


def test_session_break_is_not_called_a_gap_but_a_hole_is() -> None:
    """10:15 休息和国庆休市不叫断开；交易时段里缺了半小时要写明。"""
    from datetime import date

    from src.backtest.engine import describe_bar_gaps, session_bar_times

    def _frame(times):
        import pandas as pd

        index = pd.DatetimeIndex(times)
        close = pd.Series(4600.0, index=index)
        return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 10})

    # 9月30日白天接到10月8日。国庆休市，且休市前一天没有夜盘，都不算断开。
    sep30_day = [stamp for stamp in session_bar_times(date(2026, 9, 30), 5) if stamp.hour < 21]
    across_holiday = sep30_day + session_bar_times(date(2026, 10, 8), 5)
    assert describe_bar_gaps(_frame(across_holiday), "5") == ""
    # 6月19日端午休市，6月18日晚上同样没有夜盘，也不算断开。
    jun18_day = [stamp for stamp in session_bar_times(date(2026, 6, 18), 5) if stamp.hour < 21]
    across_duanwu = jun18_day + session_bar_times(date(2026, 6, 22), 5)
    assert describe_bar_gaps(_frame(across_duanwu), "5") == ""
    complete = session_bar_times(date(2026, 10, 16), 5, include_night=True)
    assert describe_bar_gaps(_frame(complete), "5") == ""

    holed = [
        stamp
        for stamp in session_bar_times(date(2026, 10, 16), 5)
        if not ((stamp.hour == 9 and stamp.minute >= 35) or (stamp.hour == 10 and stamp.minute <= 10))
    ]
    note = describe_bar_gaps(_frame(holed), "5")
    assert "中间断了" in note
    assert "09:35" in note
    assert "空档里的涨跌没有算进去" in note
