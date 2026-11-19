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
from src.utils.session_clock import bar_stamp_fillable
from src.utils.trading_day import futures_trading_day


def _precompute_next_fills(
    signal: pd.DataFrame,
    *,
    timeframe: str,
    symbol: str,
    cfg: dict[str, Any] | None,
) -> tuple[list[int | None], list[float | None]]:
    """从后往前记下每一根后面第一根能成交的 K，回测循环里直接取。"""
    n = len(signal)
    nxt_i: list[int | None] = [None] * n
    nxt_o: list[float | None] = [None] * n
    if n == 0:
        return nxt_i, nxt_o
    code = canonical_period(timeframe)
    opens = pd.to_numeric(signal["open"], errors="coerce")
    if code in ("D", "W"):
        for j in range(n - 1):
            nxt_i[j] = j + 1
            nxt_o[j] = float(opens.iloc[j + 1])
        return nxt_i, nxt_o
    if not str(code).isdigit():
        return nxt_i, nxt_o
    fillable = [
        bar_stamp_fillable(signal.index[j], symbol=symbol, cfg=cfg) for j in range(n)
    ]
    next_fill: int | None = None
    for j in range(n - 1, -1, -1):
        nxt_i[j] = next_fill
        if next_fill is not None:
            nxt_o[j] = float(opens.iloc[next_fill])
        if fillable[j]:
            next_fill = j
    return nxt_i, nxt_o


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
        # 下一根可成交位置先整表算完，避免每根再往后扫一遍休市。
        self._next_index: list[int | None] | None = None
        self._next_open: list[float | None] | None = None
        if fill_next_open:
            self._next_index, self._next_open = _precompute_next_fills(
                signal, timeframe=timeframe, symbol=self.symbol, cfg=self.cfg
            )

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
            if self._next_open is not None and self._next_index is not None:
                next_open = self._next_open[i]
                next_index = self._next_index[i]
            else:
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
