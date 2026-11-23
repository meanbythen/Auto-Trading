"""1 分钟拼回 5 分钟时，不能因为备注里的时间表把回测拼崩；30/60 按连续交易分钟累计。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.data.kline import (
    KlineProvider,
    bars_without_attrs,
    rebuilt_stamp_list,
)
from src.data.periods import bars_from_minutes, overlay_minute_bars
from src.strategy.decision_guard import apply_partial_tail_gate
from src.strategy.types import StrategyAction, StrategyDecision


def _ohlc(index: pd.DatetimeIndex) -> pd.DataFrame:
    n = len(index)
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1.0] * n,
        },
        index=index,
    )


def test_datetime_index_in_attrs_cannot_be_compared_as_yes_or_no() -> None:
    """pandas 拼表时会用 == 比较两份备注。备注里如果是一整段时间，这句话就会炸。"""
    idx = pd.date_range("2026-11-06 01:30", periods=8, freq="5min")
    left = {"rebuilt_from_minutes": pd.DatetimeIndex(idx)}
    right = {"rebuilt_from_minutes": pd.DatetimeIndex(idx)}
    with pytest.raises(ValueError, match="ambiguous"):
        left == right


def test_concat_succeeds_after_attrs_are_cleared() -> None:
    """清掉备注再拼，白银这种跨午夜的一段也能接上。"""
    idx = pd.date_range("2026-11-06 01:30", periods=8, freq="5min")
    left = _ohlc(idx[:4])
    right = _ohlc(idx[4:])
    stamp = pd.DatetimeIndex(idx)
    left.attrs["rebuilt_from_minutes"] = stamp
    right.attrs["rebuilt_from_minutes"] = stamp
    out = pd.concat([bars_without_attrs(left), bars_without_attrs(right)]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    assert len(out) == 8


def test_replace_rebuilt_bars_does_not_crash_with_rebuild_attrs(tmp_path: Path) -> None:
    """历史回测写回拼好的 5 分钟时，两张表都带着「改过哪些时间」也不能崩。"""
    idx = pd.date_range("2026-11-06 01:00", periods=12, freq="5min")
    full = _ohlc(idx)
    fresh = _ohlc(idx)
    full.attrs["rebuilt_from_minutes"] = pd.DatetimeIndex(idx)
    fresh.attrs["rebuilt_from_minutes"] = pd.DatetimeIndex(idx)
    trader = KlineProvider({"_project_root": str(tmp_path), "data": {"cache_dir": "klines"}})
    out = trader._replace_rebuilt_bars("ag2702", "5", full, fresh)
    assert len(out) == 12
    assert not getattr(out, "attrs", {})


def test_rebuilt_stamp_list_accepts_datetime_index_or_strings() -> None:
    idx = pd.date_range("2026-11-06 01:30", periods=3, freq="5min")
    from_index = rebuilt_stamp_list(idx)
    from_text = rebuilt_stamp_list(tuple(idx.strftime("%Y-%m-%d %H:%M:%S")))
    assert from_index == from_text
    assert from_text[0] == pd.Timestamp("2026-11-06 01:30:00")


def _session_minutes(day: str, segments: list[tuple[str, str]]) -> pd.DataFrame:
    """按交易时段生成 1 分钟，跳过休息。"""
    stamps: list[pd.Timestamp] = []
    for start_s, end_s in segments:
        start = pd.Timestamp(f"{day} {start_s}")
        end = pd.Timestamp(f"{day} {end_s}")
        current = start + timedelta(minutes=1)
        while current <= end:
            stamps.append(current)
            current += timedelta(minutes=1)
    return _ohlc(pd.DatetimeIndex(stamps))


DAY_SEGMENTS = [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")]


def test_continuous_thirty_and_sixty_day_boundaries() -> None:
    """商品日盘：30 分钟 8 根、尾 15 分钟；60 分钟 4 根、尾 45 分钟。"""
    minutes = _session_minutes("2026-11-20", DAY_SEGMENTS)
    thirty = bars_from_minutes(minutes, "30", symbol="lh2701")
    assert list(thirty.index.strftime("%H:%M")) == [
        "09:30",
        "10:00",
        "10:45",
        "11:15",
        "13:45",
        "14:15",
        "14:45",
        "15:00",
    ]
    assert [int(v) for v in thirty["minute_count"]] == [30, 30, 30, 30, 30, 30, 30, 15]
    sixty = bars_from_minutes(minutes, "60", symbol="lh2701")
    assert list(sixty.index.strftime("%H:%M")) == ["10:00", "11:15", "14:15", "15:00"]
    assert [int(v) for v in sixty["minute_count"]] == [60, 60, 60, 45]


def test_continuous_silver_night_keeps_0230_tail() -> None:
    """白银夜盘连续累计：60 分钟跨零点后仍有 00:00、01:00、02:00，02:30 是 30 分钟尾桶。"""
    night = _ohlc(pd.date_range("2026-11-20 21:00", "2026-11-21 02:30", freq="1min")[1:])
    built = bars_from_minutes(night, "60", symbol="ag2702")
    assert list(built.index.strftime("%H:%M")) == [
        "22:00",
        "23:00",
        "00:00",
        "01:00",
        "02:00",
        "02:30",
    ]
    assert [int(v) for v in built["minute_count"]] == [60, 60, 60, 60, 60, 30]


def test_night_and_day_groups_do_not_carry() -> None:
    """夜盘 23:00 的余量不结转到次日日盘，日盘从 09:00 重新累计。"""
    minutes = pd.concat(
        [
            _session_minutes("2026-11-20", [("21:00", "23:00")]),
            _session_minutes("2026-11-23", DAY_SEGMENTS),
        ]
    )
    built = bars_from_minutes(minutes, "60", symbol="fu2701")
    times = list(built.index.strftime("%Y-%m-%d %H:%M"))
    assert "2026-11-20 22:00" in times
    assert "2026-11-20 23:00" in times
    assert "2026-11-23 10:00" in times
    assert "2026-11-23 11:15" in times
    assert int(built.loc[pd.Timestamp("2026-11-23 10:00"), "minute_count"]) == 60


def test_missing_one_minute_shifts_bucket_by_actual_minutes() -> None:
    """缺 1 根 1 分钟时按实际分钟累计，桶边界顺延，不按墙上时钟硬切。"""
    minutes = _session_minutes("2026-11-20", DAY_SEGMENTS)
    minutes = minutes[minutes.index != pd.Timestamp("2026-11-20 10:00")]
    built = bars_from_minutes(minutes, "60", symbol="lh2701")
    times = list(built.index.strftime("%H:%M"))
    assert times[0] == "10:01"
    assert "11:15" not in times
    assert int(built["minute_count"].iloc[0]) == 60


def test_overlay_drops_conflicting_native_thirty_minute_bars() -> None:
    """30 分钟连续累计后，原生 10:15、11:00、11:30、14:00、14:30 不再保留。"""
    minutes = _session_minutes("2026-11-20", DAY_SEGMENTS)
    built = bars_from_minutes(minutes, "30", symbol="lh2701")
    native = _ohlc(
        pd.DatetimeIndex(
            [
                "2026-11-20 09:00",
                "2026-11-20 10:15",
                "2026-11-20 11:00",
                "2026-11-20 11:30",
                "2026-11-20 14:00",
                "2026-11-20 14:30",
            ]
        )
    )
    merged, _complete = overlay_minute_bars(native, built, 30)
    # 09:00 在覆盖区间 [09:30, 15:00] 之外，保留；区间内的冲突时间清掉。
    assert "2026-11-20 09:00:00" in merged.index
    for stamp in ("10:15", "11:00", "11:30", "14:00", "14:30"):
        assert pd.Timestamp(f"2026-11-20 {stamp}") not in merged.index


def test_partial_tail_gate_blocks_only_new_opens() -> None:
    """尾盘半根：开仓降为观望；平仓、5 分钟、allow=True 都不拦。"""
    tail = _ohlc(pd.DatetimeIndex(["2026-11-20 15:00"]))
    full = _ohlc(pd.DatetimeIndex(["2026-11-20 14:45"]))
    buy = StrategyDecision(StrategyAction.BUY_OPEN, "lh2701", "测试开多")
    blocked, hit = apply_partial_tail_gate(buy, tail, "30", symbol="lh2701")
    assert hit is True
    assert blocked.action == StrategyAction.HOLD
    assert blocked.reason.startswith("尾盘不足整周期")

    kept, hit = apply_partial_tail_gate(buy, full, "30", symbol="lh2701")
    assert hit is False
    assert kept.action == StrategyAction.BUY_OPEN

    close = StrategyDecision(StrategyAction.CLOSE, "lh2701", "测试平仓")
    kept, hit = apply_partial_tail_gate(close, tail, "30", symbol="lh2701")
    assert hit is False
    assert kept.action == StrategyAction.CLOSE

    kept, hit = apply_partial_tail_gate(buy, tail, "5", symbol="lh2701")
    assert hit is False
    assert kept.action == StrategyAction.BUY_OPEN

    kept, hit = apply_partial_tail_gate(
        buy, tail, "30", symbol="lh2701", allow=True
    )
    assert hit is False
    assert kept.action == StrategyAction.BUY_OPEN

