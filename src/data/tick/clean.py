"""
tick 清洗。

天勤快照 tick 在断线重连、集合竞价、网络抖动时会冒出脏数据。入库和合成前先过一遍：
空时间、非正价格、坏盘口、交叉盘口（买一 > 卖一）、负成交量/持仓、重复时间戳。
去重保留第一条，最后按时间升序排好。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# 天勤原始 CSV 和 vnpy 库存的列名不一样，先收成一套。
_ALIASES = {
    "bid_price1": "bid_price_1",
    "bid_volume1": "bid_volume_1",
    "ask_price1": "ask_price_1",
    "ask_volume1": "ask_volume_1",
}

_PRICE_COLUMNS = ("last_price", "bid_price_1", "ask_price_1")
_SORT_COLUMNS = ("last_price", "volume", "open_interest", "bid_price_1", "ask_price_1")


@dataclass
class CleanStats:
    """这一批 tick 清洗前后的数量，以及每一种脏数据各丢掉多少。"""

    raw: int = 0
    kept: int = 0
    dropped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "kept": self.kept,
            "dropped": self.dropped,
            "reasons": dict(self.reasons),
        }


def clean_tick_frame(
    frame: pd.DataFrame | None,
    *,
    price_min: float = 0.0,
    keep: str = "first",
) -> tuple[pd.DataFrame, CleanStats]:
    """
    清洗一批 tick，返回（清洗后的表，统计）。

    价格必须大于 price_min（默认 0）；盘口有值就必须为正，且买一不能高于卖一。
    成交量、持仓量不能为负。重复时间戳按 keep 保留。输入表不被修改。
    """
    stats = CleanStats()
    empty = pd.DataFrame()
    if frame is None or len(frame) == 0:
        return empty, stats

    data = frame.copy()
    if "datetime" in data.columns:
        data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
        data = data.set_index("datetime")
    else:
        data.index = pd.to_datetime(data.index, errors="coerce")
    if getattr(data.index, "tz", None) is not None:
        data.index = data.index.tz_convert("Asia/Shanghai").tz_localize(None)
    data.index.name = "datetime"
    stats.raw = len(data)

    def _drop(mask: pd.Series, reason: str) -> None:
        nonlocal data
        lost = int(mask.sum())
        if lost <= 0:
            return
        stats.reasons[reason] = stats.reasons.get(reason, 0) + lost
        data = data.loc[~mask]

    _drop(pd.isna(data.index), "bad_time")
    data = data.rename(columns={old: new for old, new in _ALIASES.items() if old in data.columns})
    for column in set(_SORT_COLUMNS) | {"highest", "lowest"}:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    if "last_price" not in data.columns:
        raise ValueError("tick 表里没有 last_price 列")

    price = data["last_price"]
    _drop(price.isna() | (price <= float(price_min)), "bad_price")

    book_columns = [name for name in ("bid_price_1", "ask_price_1") if name in data.columns]
    for column in book_columns:
        # 天勤历史 tick 有盘口；只有 K 线来源的 tick 没有，vnpy 补的 0 表示「没有这一档」。
        # 0 不当作坏数据，负数和交叉盘口才丢。
        values = pd.to_numeric(data[column], errors="coerce").fillna(0.0)
        data[column] = values
        _drop(values < 0, "bad_book")
    if len(book_columns) == 2:
        crossed = (data["bid_price_1"] > 0) & (data["ask_price_1"] > 0) & (data["bid_price_1"] > data["ask_price_1"])
        _drop(crossed, "crossed_book")

    if "volume" in data.columns:
        _drop(data["volume"] < 0, "negative_volume")
    if "open_interest" in data.columns:
        _drop(data["open_interest"] < 0, "negative_oi")

    duplicate = data.index.duplicated(keep=keep)
    _drop(pd.Series(duplicate, index=data.index), "duplicate")

    data = data.sort_index()
    stats.kept = len(data)
    stats.dropped = stats.raw - stats.kept
    return data, stats
