"""周期写法：分钟、日线、周线、自定义分钟，要收成同一种代码。"""

from datetime import datetime

import pandas as pd

from src.data.periods import (
    _session_open_edge_mask,
    bars_from_minutes,
    canonical_period,
    drop_forming_bar,
    fix_gap_spanning_bars,
    minute_base,
    overlay_minute_bars,
    resample_ohlc,
    ui_parts,
)


def test_session_open_edge_mask_flags_only_opening_bars() -> None:
    """整批判断开盘边时，只认各段开盘后的第一根；21:00 这类整除开盘时刻也算。

    商品 21:00 开盘，5 分钟的 21:05 是首根；21:00 本身能整除 5，规则里同样放行。
    """
    index = pd.to_datetime(
        [
            "2026-11-05 09:04:00",
            "2026-11-05 09:05:00",
            "2026-11-05 13:35:00",
            "2026-11-05 21:00:00",
            "2026-11-05 21:05:00",
        ]
    )
    assert [bool(value) for value in _session_open_edge_mask(index, 5)] == [
        False,
        True,
        True,
        True,
        True,
    ]
    assert [bool(value) for value in _session_open_edge_mask(index, 60)] == [
        False,
        False,
        False,
        True,
        False,
    ]


def test_fix_gap_spanning_bar_removes_last_session_high() -> None:
    """跳空低开时，第一根K线的高点常被接口塞成上一段收盘价，要收回本段区间。"""
    bars = pd.DataFrame(
        [
            # 11-04 夜盘最后一根：收盘 4486
            {"open": 4489, "high": 4491, "low": 4481, "close": 4486, "volume": 1200},
            # 11-05 跳空低开 4179，接口把昨晚收盘 4486 当成高点；低点 4105 是本段的
            {"open": 4179, "high": 4486, "low": 4105, "close": 4163, "volume": 79776},
        ],
        index=pd.to_datetime(["2026-11-04 23:00:00", "2026-11-05 09:05:00"]),
    )
    fixed = fix_gap_spanning_bars(bars)
    assert fixed is not None
    assert float(fixed["high"].iloc[1]) == 4179  # max(open, close)，不再含 4486
    assert float(fixed["low"].iloc[1]) == 4105  # 真实低点保留
    # 夜盘那根不跳空，保持原样
    assert float(fixed["high"].iloc[0]) == 4491


def test_fix_gap_spanning_bar_raises_low_on_gap_up() -> None:
    """跳空高开时反过来：低点被塞成上一段收盘价，要抬回本段区间。"""
    bars = pd.DataFrame(
        [
            {"open": 4100, "high": 4110, "low": 4090, "close": 4105, "volume": 900},
            {"open": 4300, "high": 4330, "low": 4105, "close": 4310, "volume": 5000},
        ],
        index=pd.to_datetime(["2026-11-05 15:00:00", "2026-11-05 21:05:00"]),
    )
    fixed = fix_gap_spanning_bars(bars)
    assert fixed is not None
    assert float(fixed["low"].iloc[1]) == 4300  # min(open, close)
    assert float(fixed["high"].iloc[1]) == 4330


def test_fix_gap_spanning_bar_leaves_normal_bars_alone() -> None:
    """没有跳空、或者高点高于上一根收盘（确实打回去了）时不动。"""
    bars = pd.DataFrame(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"open": 105, "high": 110, "low": 104, "close": 108, "volume": 12},
        ],
        index=pd.to_datetime(["2026-11-05 09:05:00", "2026-11-05 09:10:00"]),
    )
    fixed = fix_gap_spanning_bars(bars)
    assert fixed is not None
    assert float(fixed["high"].iloc[1]) == 110


def test_daily_weekly_and_custom_minutes() -> None:
    assert canonical_period("日线") == "D"
    assert canonical_period("周线") == "W"
    assert canonical_period("60分钟") == "60"
    assert canonical_period("自定义", "120") == "120"
    assert canonical_period("4小时") == "240"
    assert minute_base("120") == "60"
    label, custom = ui_parts("120")
    assert label == "自定义"
    assert custom == "120"
    assert ui_parts("D") == ("日线", "")


def test_five_minute_bar_uses_right_label_like_ths() -> None:
    """
    右标签：11:15 这根 = 11:11～11:15（与同花顺结束时刻一致）。
    11:10 单独落在上一根。
    """
    idx = pd.to_datetime(
        [
            "2026-10-23 11:10:00",
            "2026-10-23 11:11:00",
            "2026-10-23 11:12:00",
            "2026-10-23 11:13:00",
            "2026-10-23 11:14:00",
            "2026-10-23 11:15:00",
        ]
    )
    minutes = pd.DataFrame(
        {
            "open": [4726, 4726, 4727, 4722, 4722, 4723],
            "high": [4729, 4727, 4727, 4722, 4725, 4723],
            "low": [4725, 4724, 4723, 4722, 4722, 4720],
            "close": [4725, 4727, 4724, 4722, 4723, 4720],
            "volume": [159, 142, 161, 8, 21, 42],
        },
        index=idx,
    )
    built = bars_from_minutes(minutes, "5")
    bar = built.loc[pd.Timestamp("2026-10-23 11:15:00")]
    assert float(bar["open"]) == 4726
    assert float(bar["high"]) == 4727
    assert float(bar["low"]) == 4720
    assert float(bar["close"]) == 4720
    assert float(bar["volume"]) == 142 + 161 + 8 + 21 + 42
    assert int(bar["minute_count"]) == 5
    native = pd.DataFrame(
        {"open": [4700.0], "high": [4799.0], "low": [4690.0], "close": [4710.0], "volume": [999.0]},
        index=pd.to_datetime(["2026-10-23 11:15:00"]),
    )
    merged, complete = overlay_minute_bars(native, built, 5)
    fixed = merged.loc[pd.Timestamp("2026-10-23 11:15:00")]
    assert float(fixed["close"]) == 4720
    assert pd.Timestamp("2026-10-23 11:15:00") in complete


def test_night_first_15m_kept_when_one_minute_short() -> None:
    """夜盘 21:00 开盘后第一根 15 分钟（戳 21:15）缺 1 分钟也要留下。"""
    # 21:01～21:14 共 14 根 → 右标签 21:15
    idx = pd.date_range("2026-10-22 21:01", periods=14, freq="1min")
    minutes = pd.DataFrame(
        {
            "open": [4950.0] + [4940.0] * 13,
            "high": [4969.0] + [4950.0] * 13,
            "low": [4907.0] + [4930.0] * 13,
            "close": [4965.0] + [4945.0] * 12 + [4965.0],
            "volume": [100.0] * 14,
        },
        index=idx,
    )
    # 修正：第一根 open/high/low/close 表达针
    minutes.iloc[0] = [4950.0, 4950.0, 4907.0, 4916.0, 100.0]
    minutes.iloc[-1] = [4958.0, 4969.0, 4958.0, 4965.0, 100.0]
    built = bars_from_minutes(minutes, "15")
    assert pd.Timestamp("2026-10-22 21:15:00") in built.index
    bar = built.loc[pd.Timestamp("2026-10-22 21:15:00")]
    assert float(bar["low"]) == 4907.0
    assert float(bar["close"]) == 4965.0
    assert int(bar["minute_count"]) == 14


def test_custom_minutes_can_be_built_from_finer_bars() -> None:
    idx = pd.date_range("2026-10-01 09:00", periods=4, freq="60min")
    df = pd.DataFrame(
        {
            "open": [1, 2, 3, 4],
            "high": [2, 3, 4, 5],
            "low": [1, 2, 3, 4],
            "close": [1.5, 2.5, 3.5, 4.5],
            "volume": [10, 10, 10, 10],
        },
        index=idx,
    )
    out = resample_ohlc(df, "120")
    assert len(out) == 2
    assert float(out["high"].iloc[0]) == 3
    assert float(out["close"].iloc[0]) == 2.5


def test_drop_forming_keeps_complete_afternoon_bar_after_lunch() -> None:
    """
    午休后只有上午收盘 + 13:35 已收盘时，不能再盲删最后一根。
    否则会一直以为最新已收盘还是 11:30，止损要等到下一根 13:40 出现才看见。
    """
    idx = pd.to_datetime(["2026-10-27 11:30:00", "2026-10-27 13:35:00"])
    bars = pd.DataFrame(
        {
            "open": [5259.0, 5235.0],
            "high": [5263.0, 5275.0],
            "low": [5252.0, 5227.0],
            "close": [5256.0, 5259.0],
            "volume": [100, 100],
        },
        index=idx,
    )
    # 13:36：13:35 已结束，应留下两根
    kept = drop_forming_bar(bars, now=datetime(2026, 10, 27, 13, 36, 0))
    assert len(kept) == 2
    assert str(kept.index[-1]) == "2026-10-27 13:35:00"

    # 13:34：13:35 还在走，应只留 11:30
    kept_early = drop_forming_bar(bars, now=datetime(2026, 10, 27, 13, 34, 0))
    assert len(kept_early) == 1
    assert str(kept_early.index[-1]) == "2026-10-27 11:30:00"


def test_drop_forming_still_drops_current_incomplete_bucket() -> None:
    idx = pd.to_datetime(
        ["2026-10-27 13:35:00", "2026-10-27 13:40:00"]
    )
    bars = pd.DataFrame(
        {
            "open": [5235.0, 5259.0],
            "high": [5275.0, 5265.0],
            "low": [5227.0, 5255.0],
            "close": [5259.0, 5260.0],
            "volume": [100, 50],
        },
        index=idx,
    )
    kept = drop_forming_bar(bars, now=datetime(2026, 10, 27, 13, 38, 0))
    assert len(kept) == 1
    assert str(kept.index[-1]) == "2026-10-27 13:35:00"


def test_fix_gap_with_duplicate_close_column_does_not_crash() -> None:
    """同一张表有两列 close 时，不能再报 array ambiguous。"""
    bars = pd.DataFrame(
        [
            {"open": 4489, "high": 4491, "low": 4481, "close": 4486, "volume": 1200},
            {"open": 4179, "high": 4486, "low": 4105, "close": 4163, "volume": 79776},
        ],
        index=pd.to_datetime(["2026-11-04 23:00:00", "2026-11-05 09:05:00"]),
    )
    bars = pd.concat([bars, bars[["close"]]], axis=1)
    assert bars.columns.duplicated().any()
    fixed = fix_gap_spanning_bars(bars)
    assert fixed is not None
    assert not fixed.columns.duplicated().any()
    assert float(fixed["high"].iloc[1]) == 4179
