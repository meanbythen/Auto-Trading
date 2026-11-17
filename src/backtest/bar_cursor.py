"""回测逐根游标（BarCursor）。

从 engine.run_backtest 抽出来：只回答「第 i 根 K 能看到什么」——
截至当根的历史窗口、当前时间与收盘价、下一根开盘价、止损/大周期对齐窗口。
持仓、账本、成交与统计仍留在 run_backtest，游标不碰。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from src.backtest.exit_policy import next_open_price, next_tradable_bar_index
from src.data.periods import canonical_period, last_ohlc_price
from src.utils.trading_day import futures_trading_day


def _ts_to_dt(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if hasattr(ts, "to_pydatetime"):
        return ts.to_pydatetime()
    return pd.Timestamp(ts).to_pydatetime()


def _trend_frame_for_signal(
    bars_trend: pd.DataFrame | None,
    last_signal_ts: pd.Timestamp,
    trend_period: str,
) -> pd.DataFrame | None:
    """
    决策时能看到的大周期K线。

    日线/周线的标签是周期起点（日线是当天日盘 00:00 的标签），夜盘 21:00 之后
    已经属于下一个交易日，这时当天那根日线已经走完，应该能看到；21:00 之前
    当天这根还在形成，不能看到。所以按信号所属的期货交易日切，而不是按自然日。
    """
    if bars_trend is None or len(bars_trend) == 0:
        return None
    last_ts = pd.Timestamp(last_signal_ts)
    if canonical_period(trend_period) in ("D", "W"):
        moment = last_ts.to_pydatetime()
        if getattr(moment, "tzinfo", None) is not None:
            moment = moment.replace(tzinfo=None)
        cutoff = pd.Timestamp(futures_trading_day(moment))
        return bars_trend[bars_trend.index < cutoff]
    return bars_trend[bars_trend.index <= last_ts]


@dataclass(frozen=True, slots=True)
class BarView:
    """某一根 K 的只读视图；同一次回测里不复用，按需生成。"""

    index: int
    signal: pd.DataFrame
    now: datetime
    px: float
    next_open: float | None
    next_index: int | None
    trail: pd.DataFrame | None
    trend: pd.DataFrame | None
    is_last: bool


class BarCursor:
    """
    逐根推进回测 K 线的游标。

    只做取值和对齐，不做决策、不算钱。`at(i)` 对同一个 i 必须给出和旧主循环
    完全相同的窗口与价格；`indices()` 给出行号范围，起点由调用方算好（含 warmup）。
    """

    def __init__(
        self,
        signal: pd.DataFrame,
        *,
        trail: pd.DataFrame | None = None,
        trend: pd.DataFrame | None = None,
        timeframe: str = "5",
        trend_timeframe: str = "D",
        start: int = 0,
        end: int | None = None,
        fill_next_open: bool = True,
        symbol: str = "",
        cfg: dict[str, Any] | None = None,
    ) -> None:
        self.signal = signal
        self.trail = trail
        self.trend = trend
        self.timeframe = timeframe
        self.trend_timeframe = trend_timeframe
        self.start = max(0, int(start))
        self.end = len(signal) if end is None else min(int(end), len(signal))
        self.fill_next_open = fill_next_open
        self.symbol = str(symbol or "")
        self.cfg = cfg

    def __len__(self) -> int:
        return max(0, self.end - self.start)

    def indices(self) -> range:
        return range(self.start, self.end)

    def at(self, i: int) -> BarView | None:
        """取第 i 根 K 的视图；收盘价取不到时返回 None，调用方跳过这一根。"""
        if i < 0 or i >= self.end:
            return None
        signal_df = self.signal.iloc[: i + 1]
        last_ts = signal_df.index[-1]
        px = last_ohlc_price(signal_df, "close")
        if px is None:
            return None

        trail_df: pd.DataFrame | None = None
        if self.trail is not None and len(self.trail) > 0:
            trail_df = self.trail[self.trail.index <= last_ts]
        trend_df = _trend_frame_for_signal(self.trend, last_ts, self.trend_timeframe)
        next_open: float | None = None
        next_index: int | None = None
        if self.fill_next_open:
            next_open = next_open_price(
                self.signal, i, self.timeframe, symbol=self.symbol, cfg=self.cfg
            )
            if next_open is not None:
                code = canonical_period(self.timeframe)
                if code in ("D", "W"):
                    next_index = i + 1 if i + 1 < len(self.signal) else None
                else:
                    next_index = next_tradable_bar_index(
                        self.signal, i, symbol=self.symbol, cfg=self.cfg
                    )

        return BarView(
            index=i,
            signal=signal_df,
            now=_ts_to_dt(last_ts),
            px=float(px),
            next_open=next_open,
            next_index=next_index,
            trail=trail_df,
            trend=trend_df,
            is_last=i >= self.end - 1,
        )
