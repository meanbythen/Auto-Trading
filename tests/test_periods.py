"""周期写法：分钟、日线、周线、自定义分钟，要收成同一种代码。"""

from datetime import date, datetime

import pandas as pd

from src.data.periods import (
    _session_open_edge_mask,
    bars_from_minutes,
    canonical_period,
    drop_forming_bar,
    minute_base,
    overlay_minute_bars,
    resample_ohlc,
    session_close_times,
    ui_parts,
    unique_ohlc_columns,
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


def _session_minutes(day: str, segments: list[tuple[str, str]]) -> pd.DataFrame:
    """按交易时段生成 1 分钟，跳过休息。"""
    from datetime import timedelta

    stamps: list[pd.Timestamp] = []
    for start_s, end_s in segments:
        start = pd.Timestamp(f"{day} {start_s}")
        end = pd.Timestamp(f"{day} {end_s}")
        t = start + timedelta(minutes=1)
        while t <= end:
            stamps.append(t)
            t += timedelta(minutes=1)
    n = len(stamps)
    return pd.DataFrame(
        {
            "open": list(range(n)),
            "high": list(range(n)),
            "low": list(range(n)),
            "close": list(range(n)),
            "volume": [1.0] * n,
        },
        index=pd.DatetimeIndex(stamps),
    )


def test_sixty_minute_bars_use_continuous_trading_minutes() -> None:
    """商品 60 分钟按连续交易分钟累计：10:15-10:30、11:30-13:30 不计入。"""
    minutes = _session_minutes(
        "2026-10-27",
        [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")],
    )
    built = bars_from_minutes(minutes, "60", symbol="lh2701")
    assert list(built.index.strftime("%H:%M")) == [
        "10:00",
        "11:15",
        "14:15",
        "15:00",
    ]
    assert [int(v) for v in built["minute_count"]] == [60, 60, 60, 45]

    native = pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "high": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "low": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "volume": [1.0] * 6,
        },
        index=pd.to_datetime(
            [
                "2026-10-27 09:00:00",
                "2026-10-27 11:00:00",
                "2026-10-27 12:00:00",
                "2026-10-27 13:00:00",
                "2026-10-27 14:00:00",
                "2026-10-27 15:30:00",
            ]
        ),
    )
    merged, _complete = overlay_minute_bars(native, built, 60)
    # 覆盖区间 [10:00, 15:00] 内的旧原生时间清掉；区间外的 09:00、15:30 保留。
    assert list(merged.index.strftime("%H:%M")) == [
        "09:00",
        "10:00",
        "11:15",
        "14:15",
        "15:00",
        "15:30",
    ]


def test_thirty_minute_bars_use_continuous_trading_minutes() -> None:
    """30 分钟余量跨 10:15-10:30、11:30-13:30 结转，只有尾盘 15:00 是半根。"""
    minutes = _session_minutes(
        "2026-10-27",
        [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")],
    )
    built = bars_from_minutes(minutes, "30", symbol="lh2701")
    assert list(built.index.strftime("%H:%M")) == [
        "09:30",
        "10:00",
        "10:45",
        "11:15",
        "13:45",
        "14:15",
        "14:45",
        "15:00",
    ]
    assert [int(v) for v in built["minute_count"]] == [30, 30, 30, 30, 30, 30, 30, 15]


def test_night_thirty_and_sixty_are_continuous() -> None:
    """夜盘 21:00-23:00 连续累计：30 分钟 4 根，60 分钟 2 根。"""
    minutes = _session_minutes("2026-10-26", [("21:00", "23:00")])
    thirty = bars_from_minutes(minutes, "30", symbol="fu2701")
    assert list(thirty.index.strftime("%H:%M")) == ["21:30", "22:00", "22:30", "23:00"]
    sixty = bars_from_minutes(minutes, "60", symbol="fu2701")
    assert list(sixty.index.strftime("%H:%M")) == ["22:00", "23:00"]


def test_partial_tail_bar_only_flags_half_bucket() -> None:
    """尾盘判定：30/60 的 15:00 是半根；完整桶和 5 分钟一律不算。"""
    from src.data.periods import partial_tail_bar

    assert partial_tail_bar(pd.Timestamp("2026-10-27 15:00"), "30", symbol="lh2701")
    assert partial_tail_bar(pd.Timestamp("2026-10-27 15:00"), "60", symbol="lh2701")
    assert not partial_tail_bar(pd.Timestamp("2026-10-27 14:45"), "30", symbol="lh2701")
    assert not partial_tail_bar(pd.Timestamp("2026-10-27 14:15"), "60", symbol="lh2701")
    assert not partial_tail_bar(pd.Timestamp("2026-10-27 15:00"), "5", symbol="lh2701")


def test_hog_sixty_has_no_night_bars() -> None:
    """生猪没有夜盘，21 点那根 1 分钟不能拼进 60 分钟。"""
    day = _session_minutes(
        "2026-10-27",
        [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")],
    )
    night = _session_minutes("2026-10-26", [("21:00", "23:00")])
    minutes = pd.concat([night, day]).sort_index()
    built = bars_from_minutes(minutes, "60", symbol="lh2701")
    hours = set(built.index.strftime("%H:%M"))
    assert "22:00" not in hours
    assert "23:00" not in hours


def test_silver_sixty_keeps_0230_stub() -> None:
    """黄金白银夜盘连续跨零点：00:00 也在组内，02:30 是一根 30 分钟的短 K。"""
    index = pd.date_range("2026-10-27 21:00", "2026-10-28 02:30", freq="1min")[1:]
    n = len(index)
    minutes = pd.DataFrame(
        {
            "open": list(range(n)),
            "high": list(range(n)),
            "low": list(range(n)),
            "close": list(range(n)),
            "volume": [1.0] * n,
        },
        index=index,
    )
    built = bars_from_minutes(minutes, "60", symbol="ag2702")
    times = list(built.index.strftime("%Y-%m-%d %H:%M"))
    assert times == [
        "2026-10-27 22:00",
        "2026-10-27 23:00",
        "2026-10-28 00:00",
        "2026-10-28 01:00",
        "2026-10-28 02:00",
        "2026-10-28 02:30",
    ]
    assert [int(v) for v in built["minute_count"]] == [60, 60, 60, 60, 60, 30]


def test_five_and_ten_minute_follow_clock_with_gaps() -> None:
    """5/10 分钟按墙上时钟收，休市没有 K；10 分钟在 10:15 收一根短 K。"""
    minutes = _session_minutes(
        "2026-10-27",
        [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")],
    )
    five = bars_from_minutes(minutes, "5", symbol="lh2701")
    times5 = set(five.index.strftime("%H:%M"))
    assert "10:15" in times5
    assert "10:20" not in times5
    assert "10:25" not in times5
    assert "10:30" not in times5
    assert "10:35" in times5
    assert "12:00" not in times5
    ten = bars_from_minutes(minutes, "10", symbol="lh2701")
    times10 = list(ten.index.strftime("%H:%M"))
    assert "10:10" in times10
    assert "10:15" in times10
    assert "10:20" not in times10
    assert int(ten.loc[pd.Timestamp("2026-10-27 10:15")]["minute_count"]) == 5


def test_daily_bar_groups_night_into_next_day_session() -> None:
    """有夜盘：26 日 21:00～27 日 15:00 合成 27 日这一根；午休分钟不并进去。"""
    from src.data.tick.synthesize import minutes_to_daily

    night = _session_minutes("2026-10-26", [("21:00", "23:00")])
    day = _session_minutes(
        "2026-10-27",
        [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")],
    )
    lunch = pd.DataFrame(
        {"open": [9.0], "high": [9.0], "low": [9.0], "close": [9.0], "volume": [99.0]},
        index=pd.to_datetime(["2026-10-27 12:00"]),
    )
    daily = minutes_to_daily(pd.concat([night, day, lunch]), symbol="fu2701")
    assert list(daily.index) == [pd.Timestamp("2026-10-27")]
    assert float(daily["open"].iloc[0]) == float(night["open"].iloc[0])
    assert float(daily["close"].iloc[0]) == float(day["close"].iloc[-1])
    assert float(daily["volume"].iloc[0]) == float(night["volume"].sum() + day["volume"].sum())

    hog = minutes_to_daily(pd.concat([night, day]), symbol="lh2701")
    assert float(hog["open"].iloc[0]) == float(day["open"].iloc[0])


def test_session_clock_rebuild_is_fast_on_long_minutes() -> None:
    """一年的 1 分钟不能再逐根循环，否则回测会卡在请稍候。"""
    import time

    minutes = _session_minutes(
        "2026-10-27",
        [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")],
    )
    copies = [minutes.copy() for _ in range(80)]
    for i, piece in enumerate(copies):
        piece.index = piece.index + pd.Timedelta(i, unit="D")
    long_minutes = pd.concat(copies).sort_index()
    started = time.perf_counter()
    built = bars_from_minutes(long_minutes, "5", symbol="lh2701")
    elapsed = time.perf_counter() - started
    assert len(built) > 1000
    assert elapsed < 2.5


def test_session_close_times_sixty_matches_continuous_bars() -> None:
    """缺口检查用的应收时刻，要和连续累计拼出来的 60 分钟一致。"""
    day = date(2026, 10, 27)
    times = [stamp.strftime("%H:%M") for stamp in session_close_times(day, 60, include_night=False)]
    assert times == ["10:00", "11:15", "14:15", "15:00"]


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


def test_duplicate_close_column_keeps_last() -> None:
    """同一张表有两列 close 时只留最后一列，开高低收不再改写。"""
    bars = pd.DataFrame(
        [
            {"open": 4489, "high": 4491, "low": 4481, "close": 4486, "volume": 1200},
            {"open": 4179, "high": 4486, "low": 4105, "close": 4163, "volume": 79776},
        ],
        index=pd.to_datetime(["2026-11-04 23:00:00", "2026-11-05 09:05:00"]),
    )
    bars = pd.concat([bars, bars[["close"]]], axis=1)
    assert bars.columns.duplicated().any()
    cleaned = unique_ohlc_columns(bars)
    assert cleaned is not None
    assert not cleaned.columns.duplicated().any()
    assert float(cleaned["high"].iloc[1]) == 4486
