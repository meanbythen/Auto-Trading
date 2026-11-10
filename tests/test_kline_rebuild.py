"""1 分钟拼回 5 分钟时，不能因为备注里的时间表把回测拼崩。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.data.kline import KlineProvider, bars_without_attrs, rebuilt_stamp_list


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
    rebuilt = tuple(idx.strftime("%Y-%m-%d %H:%M:%S"))
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
