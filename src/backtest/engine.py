"""
历史回测引擎
功能：用历史分钟K线，按与实盘相同的策略逻辑模拟开平仓，输出成交与绩效。
原理：按时间一根根推进；每根已收盘K上调用 strategy.evaluate；成交价取当根收盘价近似。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.backtest.bar_cursor import BarCursor, _ts_to_dt
from src.backtest.bar_cursor import _trend_frame_for_signal  # noqa: F401  旧调用方仍从 engine 取这个名字
from src.backtest.evaluation import EvaluationThresholds, evaluate_backtest
from src.backtest.exit_policy import (
    carry_stop_price as _carry_stop_price,
    exit_fill_price as _exit_fill_price,
    next_open_price as _next_open_price,  # noqa: F401  旧调用方和测试仍从 engine 取这个名字
    position_stop as _position_stop,
    stop_breached as _stop_breached,  # noqa: F401  旧调用方和测试仍从 engine 取这个名字
)
from src.backtest.fill import FillModel
from src.backtest.liquidity import LiquidityModel
from src.backtest.limits import (
    BacktestLimits,
    apply_size_scale as _apply_size_scale,
    cap_open_volume as _cap_open_volume,
    skip_reason_key,
)
from src.backtest.metrics import MARGIN_RATE, TradeRecord, calc_performance, peak_margin
from src.backtest.models import BacktestResult, RejectedSignal
from src.strategy.cta_factors import factor_fields_for_trade
from src.strategy.market_state import adx_series
from src.data.kline import KlineProvider
from src.data.periods import canonical_period, drop_forming_bar, period_label, unique_ohlc_columns
from src.risk.commission import fee_close, fee_open, is_close_today
from src.strategy import StrategyAction, StrategyDecision, create_strategy
from src.strategy.decision_guard import apply_delivery_gate, apply_generic_stop
from src.strategy.generic_stop import GenericStopOverlay, generic_stop_snapshot
from src.strategy.session_scope import session_kind
from src.strategy.types import PositionState
from src.utils.trading_day import EXCHANGE_HOLIDAYS, futures_trading_day

# 同一进程内同一 (symbol, period, start, end) 的 K 线只拉一次。
# stress 评估 / 参数扰动 / 多次回测同区间会反复用，避免每轮都读 DB+CSV+upsert。
_BARS_CACHE: dict[tuple[str, str, str, str], pd.DataFrame] = {}
_MAX_BARS_CACHE = 64


def _bars_cache_key(symbol: str, period: str, start: str | None, end: str | None) -> tuple[str, str, str, str]:
    return (symbol, period, str(start or ""), str(end or ""))


def _fetch_bars_cached(
    provider: Any,
    symbol: str,
    period: str,
    *,
    max_bars: int,
    start: str | None,
    end: str | None,
) -> pd.DataFrame:
    """带缓存的 provider.fetch。命中时秒级返回；miss 才走完整 I/O。"""
    key = _bars_cache_key(symbol, period, start, end)
    cached = _BARS_CACHE.get(key)
    if cached is not None:
        return cached
    bars = provider.fetch(
        symbol,
        period=period,
        max_bars=max_bars,
        keep_history=True,
        start=start,
        end=end,
    )
    if bars is not None and len(bars) > 0 and len(_BARS_CACHE) >= _MAX_BARS_CACHE:
        # 简单 LRU：超过上限就清一半。bars 不会变（同区间同合约），所以不需要 TTL。
        for k in list(_BARS_CACHE.keys())[: _MAX_BARS_CACHE // 2]:
            _BARS_CACHE.pop(k, None)
    if bars is not None and len(bars) > 0:
        _BARS_CACHE[key] = bars
    return bars


def _net_pnl_after_fees(
    contracts: dict[str, Any],
    symbol: str,
    *,
    gross: float,
    volume: int,
    held: int,
    open_meta: dict[str, Any],
    exit_time: str,
) -> tuple[float, float]:
    """
    价差盈亏扣掉本笔分摊的开仓费 + 平仓费。

    返回 (净盈亏, 本笔手续费合计)。部分平仓时按手数分摊开仓费。
    """
    open_fee_left = float(open_meta.get("open_fee") or 0)
    held_lots = max(1, int(held or volume or 1))
    cut = max(0, min(int(volume or 0), held_lots))
    open_share = round(open_fee_left * cut / held_lots, 2) if open_fee_left else 0.0
    today = is_close_today(str(open_meta.get("entry_time") or ""), exit_time)
    close_fee = fee_close(contracts, symbol, cut, close_today=today)
    total_fee = round(open_share + close_fee, 2)
    open_meta["open_fee"] = round(max(0.0, open_fee_left - open_share), 2)
    if "open_volume" in open_meta:
        open_meta["open_volume"] = max(0, int(open_meta.get("open_volume") or 0) - cut)
    return round(float(gross) - total_fee, 2), total_fee


def _deepcopy_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(cfg)


def _adx_for_eval(bars: pd.DataFrame | None, period: int) -> list[float | None]:
    """按信号K线算一条 ADX，给评估分趋势/震荡用。算不出来返回空列表。"""
    if bars is None or len(bars) == 0:
        return []
    try:
        adx, _plus, _minus = adx_series(
            bars["high"],
            bars["low"],
            bars["close"],
            period=max(2, int(period)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("评估用 ADX 没算出来: {}", exc)
        return []
    values: list[float | None] = []
    for raw in adx.tolist():
        try:
            number = float(raw)
        except (TypeError, ValueError):
            values.append(None)
            continue
        values.append(None if pd.isna(number) else number)
    return values


def _adx_at(values: list[float | None], index: int) -> float | None:
    """取第 index 根K线的 ADX；没有就返回 None。"""
    if not values or index < 0 or index >= len(values):
        return None
    return values[index]


def _make_trade_record(
    *,
    symbol: str,
    side: str,
    entry_time: str,
    exit_time: str,
    entry_price: float,
    exit_price: float,
    volume: int,
    pnl: float,
    reason_entry: str,
    reason_exit: str,
    commission: float,
    open_meta: dict[str, Any],
    multiplier: float,
    adx_at_entry: float | None = None,
) -> TradeRecord:
    """记一笔已平仓交易；多因子会附上四因子得分和分摊盈亏。"""
    extra = factor_fields_for_trade(open_meta, pnl)
    return TradeRecord(
        symbol=symbol,
        side=side,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        volume=volume,
        pnl=pnl,
        reason_entry=reason_entry,
        reason_exit=reason_exit,
        commission=commission,
        multiplier=float(multiplier or 0.0) or None,
        adx_at_entry=adx_at_entry,
        **extra,
    )


# 国内期货白天三段加上夜盘。K线时间用该段收盘时刻，例如 5 分钟的第一根是 09:05。
# 10:15–10:30、午休、白天到夜盘、过夜和周末都空在这些时段外面，不算数据断开。
_SESSION_SEGMENTS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
    (time(21, 0), time(23, 0)),
)
# 交易所休市的工作日由交易日历统一给出（src.utils.trading_day），这里不再单独维护一份。
_CLOSED_WEEKDAYS = EXCHANGE_HOLIDAYS


def session_bar_times(day: date, step_minutes: int, *, include_night: bool = True) -> list[datetime]:
    """
    这一天按交易时段应该有的K线收盘时间。

    只生成连续交易的那些分钟。午休和 10:15 之后的休息不生成，所以正常休盘不会被当成缺口。
    """
    if day.weekday() >= 5 or day in _CLOSED_WEEKDAYS:
        return []
    step = max(int(step_minutes), 1)
    # 第二天休市时，前一天晚上通常没有夜盘。没有夜盘不算数据断开。
    night_closed = (day + timedelta(days=1)) in _CLOSED_WEEKDAYS
    segments = _SESSION_SEGMENTS if include_night and not night_closed else _SESSION_SEGMENTS[:3]
    closes: list[datetime] = []
    for start, end in segments:
        cursor = datetime.combine(day, start) + timedelta(minutes=step)
        last = datetime.combine(day, end)
        while cursor <= last:
            closes.append(cursor)
            cursor += timedelta(minutes=step)
    return closes


def describe_bar_gaps(df: pd.DataFrame | None, period: str) -> str:
    """
    说明回测用的K线中间有没有断开。

    正常的休盘不提。交易时段里缺了至少半小时的行情才写出来，
    免得一根对不齐的K线也让结果看起来像数据坏了。
    有断开时仍会接着算，但结果里写明空档，避免把两段行情当成连续走势。
    """
    if df is None or len(df) < 2:
        return ""
    code = canonical_period(period)
    if code in ("D", "W") or (code.isdigit() and int(code) > 60):
        return _describe_wide_gaps(df, 21 if code == "W" else 10)
    if not code.isdigit():
        return ""
    step = int(code)
    stamps = _naive_stamps(df)
    if len(stamps) < 2:
        return ""
    include_night = any(stamp.hour >= 21 for stamp in stamps)
    present = set(stamps)
    first, last = stamps[0], stamps[-1]
    missing: list[datetime] = []
    day = first.date()
    while day <= last.date():
        for close_ts in session_bar_times(day, step, include_night=include_night):
            if first < close_ts < last and close_ts not in present:
                missing.append(close_ts)
        day += timedelta(days=1)
    min_missing = max(2, 30 // step)
    groups = _group_missing(missing, stamps)
    groups = [group for group in groups if len(group) >= min_missing]
    return _format_gaps(groups)


def _naive_stamps(df: pd.DataFrame) -> list[datetime]:
    """索引收成不带时区、秒数为 0 的时间，方便和交易时段对齐。"""
    stamps: list[datetime] = []
    for raw in df.index:
        stamp = pd.Timestamp(raw).to_pydatetime()
        if stamp.tzinfo is not None:
            stamp = stamp.replace(tzinfo=None)
        stamps.append(stamp.replace(second=0, microsecond=0))
    return sorted(set(stamps))


def _group_missing(missing: list[datetime], present: list[datetime]) -> list[list[datetime]]:
    """中间如果还有一根真实K线，就分成两处断开。"""
    if not missing:
        return []
    present_sorted = sorted(present)
    pointer = 0
    groups = [[missing[0]]]
    for stamp in missing[1:]:
        while pointer < len(present_sorted) and present_sorted[pointer] <= groups[-1][-1]:
            pointer += 1
        if pointer < len(present_sorted) and present_sorted[pointer] < stamp:
            groups.append([stamp])
        else:
            groups[-1].append(stamp)
    return groups


def _format_gaps(groups: list[list[datetime]]) -> str:
    """把断开的起止时间写成结果区能直接看的一句话。"""
    if not groups:
        return ""
    shown = groups[:3]
    parts = [
        f"{group[0].strftime('%Y-%m-%d %H:%M')} 到 {group[-1].strftime('%Y-%m-%d %H:%M')}"
        for group in shown
    ]
    body = "；".join(parts)
    if len(groups) > 3:
        body += f" 等共 {len(groups)} 处"
    title = "中间断了" if len(groups) == 1 else f"中间断了 {len(groups)} 处"
    return (
        f"{title}：{body} 没有K线。"
        "回测仍把前后连着计算，空档里的涨跌没有算进去。"
    )


def _describe_wide_gaps(df: pd.DataFrame, max_days: int) -> str:
    """日线、周线不套分钟时段。两根之间隔得太久，才说明中间没有行情。"""
    stamps = _naive_stamps(df)
    groups: list[list[datetime]] = []
    for prev, nxt in zip(stamps, stamps[1:]):
        if (nxt.date() - prev.date()).days > max_days:
            groups.append([prev, nxt])
    return _format_gaps(groups)


def coverage_note(
    requested_start: str | None,
    requested_end: str | None,
    available_from: str,
    available_to: str,
    used_from: str,
    used_to: str,
    period: str = "",
) -> str:
    """
    用人话说明「你填的日期」和「实际用到的K线」为什么可能不一样。

    公开分钟线通常只有最近大约一个月。开始日期填得再早，也只能从行情里最早的那根算起。
    """
    avail_day = str(available_from)[:10]
    avail_end = str(available_to)[:10]
    used_day = str(used_from)[:10]
    used_end = str(used_to)[:10]
    req_s = str(requested_start or "").strip()[:10]
    req_e = str(requested_end or "").strip()[:10]
    if not avail_day:
        return ""
    if not req_s and not req_e:
        code = canonical_period(period) if period else ""
        if code in ("D", "W"):
            hint = f"这次用的是{period_label(code)}，历史通常比分钟线长。"
        else:
            hint = "网上分钟线一般只有最近一段。电脑里存过的更早K线会接上；两边都没有的日子仍然用不了。"
        return (
            f"这次没有用开始/结束日期，回测用的是行情里全部K线：{avail_day} 到 {avail_end}。"
            f"{hint}"
            "想只测其中几天，直接改开始日期和结束日期后再点回测。"
        )
    notes: list[str] = []
    if req_s and avail_day and req_s < avail_day:
        kind = period_label(period) if period else "分钟"
        notes.append(
            f"你填的开始日期是 {req_s}，{kind}最早只有 {avail_day}，"
            f"再早没有K线，所以实际从 {used_day} 开始。"
        )
    if req_e and avail_end and req_e > avail_end and used_end == avail_end:
        notes.append(f"结束日期 {req_e} 晚于最新一根K线 {avail_end}，实际停在 {used_end}。")
    if req_s and req_s >= avail_day and used_day >= req_s:
        notes.append(f"已按你填的日期截取，实际是 {used_day} 到 {used_end}。")
    if not notes and used_day:
        notes.append(f"已按你填的日期截取，实际是 {used_day} 到 {used_end}。")
    return "".join(notes)


def _as_mask(flag: Any):
    """比较日期时，有的是 Series，有的已经是 numpy 数组。"""
    if hasattr(flag, "to_numpy"):
        return flag.to_numpy()
    return flag


def _trading_day_index(index: pd.Index) -> pd.DatetimeIndex:
    """
    每根K线属于哪个期货交易日，统一表示成那一天的 00:00。

    夜盘 21:00 及以后算下一交易日；周六、周日顺延到周一。
    这样开始日能带上前一晚的夜盘，结束日晚上 21:00 之后的K线会归到下一日、不混进来。
    """
    stamps = pd.DatetimeIndex(index)
    days = stamps.normalize()
    days = days + pd.to_timedelta((stamps.hour >= 21).astype(int), unit="D")
    weekend = pd.Series(days.weekday).map({5: 2, 6: 1}).fillna(0).astype(int)
    return days + pd.to_timedelta(weekend.to_numpy(), unit="D")


def filter_bars_by_range(
    df: pd.DataFrame | None,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    keep_before: int = 0,
) -> pd.DataFrame | None:
    """
    按期货交易日截取K线（含起止当天）。

    开始日 T 从 T-1 晚上 21:00 的夜盘算起，结束日 T 一直算到 T 20:59；
    T 晚上 21:00 之后的K线属于下一个交易日，不计入。周末的夜盘顺延到周一。

    keep_before：开始日之前再留几根，只给均线预热，不当作这段要测的行情。
    换月后新主力必须用它自己更早的K线把 EMA120 算热，否则和同花顺打开这张合约对不上。
    """
    if df is None or len(df) == 0:
        return df
    out = df
    keep_before = max(0, int(keep_before or 0))
    days = _trading_day_index(out.index)
    if start not in (None, ""):
        start_day = pd.Timestamp(futures_trading_day(pd.Timestamp(start).to_pydatetime()))
        in_range = _as_mask(days >= start_day)
        if keep_before > 0 and bool(in_range.any()):
            first = int(in_range.nonzero()[0][0])
            out = out.iloc[max(0, first - keep_before) :]
        else:
            out = out.iloc[in_range]
        days = _trading_day_index(out.index)
    if end not in (None, "") and len(out) > 0:
        end_day = pd.Timestamp(futures_trading_day(pd.Timestamp(end).to_pydatetime()))
        out = out.iloc[_as_mask(days <= end_day)]
    return out


def _range_start_index(df: pd.DataFrame | None, start: str | datetime | None) -> int:
    """截完之后，真正开始交易的第一根下标。前面留下的是均线预热。"""
    if df is None or len(df) == 0 or start in (None, ""):
        return 0
    days = _trading_day_index(df.index)
    start_day = pd.Timestamp(futures_trading_day(pd.Timestamp(start).to_pydatetime()))
    hits = _as_mask(days >= start_day)
    if not bool(hits.any()):
        return 0
    return int(hits.nonzero()[0][0])


def _coverage_with_gaps(
    note: str,
    bars_signal: pd.DataFrame | None,
    signal_period: str,
    bars_trail: pd.DataFrame | None,
    trail_period: str,
) -> str:
    """日期说明后面补上断开说明。信号和止损周期断在同一处时只写一遍。"""
    signal_gap = describe_bar_gaps(bars_signal, signal_period)
    trail_gap = ""
    if bars_trail is not None and canonical_period(trail_period) != canonical_period(signal_period):
        trail_gap = describe_bar_gaps(bars_trail, trail_period)
    pieces = [note.strip()] if note.strip() else []
    if signal_gap:
        pieces.append(signal_gap if not trail_gap or trail_gap == signal_gap else f"信号{period_label(signal_period)}{signal_gap}")
    if trail_gap and trail_gap != signal_gap:
        pieces.append(f"止损{period_label(trail_period)}{trail_gap}")
    return "".join(pieces)


@dataclass
class _BacktestSetup:
    """一次回测的只读准备结果：配置、策略、K线和循环起点。"""

    cfg: dict[str, Any]
    st: dict[str, Any]
    trade: dict[str, Any]
    risk: dict[str, Any]
    symbol: str
    capital0: float
    mult: float
    strategy: Any
    tf: str
    trail_tf: str
    trend_tf: str
    max_bars: int
    warmup: int
    fill: FillModel
    liquidity: LiquidityModel
    bars_signal: pd.DataFrame
    bars_trail: pd.DataFrame | None
    bars_trend: pd.DataFrame | None
    available_from: str
    available_to: str
    start_date: str | None
    end_date: str | None
    trade_from: int
    loop_start: int
    carry: dict[str, Any] | None


@dataclass
class _BacktestRun:
    """逐根循环期间不断变化的状态。"""

    cfg: dict[str, Any]
    st: dict[str, Any]
    trade: dict[str, Any]
    risk: dict[str, Any]
    symbol: str
    mult: float
    strategy: Any
    fill: FillModel
    generic_stop: GenericStopOverlay
    contracts: dict[str, Any]
    bars_signal: pd.DataFrame
    adx_values: list[Any]
    limits: BacktestLimits
    liquidity: LiquidityModel
    cash: float
    carry: dict[str, Any] | None = None
    carry_pending: bool = False
    # 上一根涨跌停没平掉，这一根接着试。
    pending_exit: StrategyDecision | None = None
    trades: list[TradeRecord] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    equity_times: list[str] = field(default_factory=list)
    open_meta: dict[str, Any] = field(default_factory=dict)
    rejected: list[RejectedSignal] = field(default_factory=list)
    # 同一交易时段里，同一层、同方向、同一类原因只留第一条。
    seen_rejects: set[tuple[str, str, str, str, str]] = field(default_factory=set)

    def note_reject(
        self,
        stage: str,
        reason: str,
        side: str,
        *,
        at: datetime,
        price: float | None,
        stop: float | None,
        key: str = "",
    ) -> None:
        """记录「有信号但没开成」。同一时段同一原因只留一条。"""
        session = f"{futures_trading_day(at)}|{session_kind(at)}"
        template = (key or "").strip() or (reason or "未开仓").strip()
        dedupe = (session, stage, side, template)
        if dedupe in self.seen_rejects:
            return
        self.seen_rejects.add(dedupe)
        self.rejected.append(
            RejectedSignal(
                symbol=self.symbol,
                time=at.isoformat(timespec="seconds"),
                side=side,
                price=float(price) if price is not None and float(price) > 0 else None,
                stop=float(stop) if stop is not None and float(stop) > 0 else None,
                stage=stage,
                reason=(reason or "未开仓").strip(),
            )
        )


@dataclass
class _BarContext:
    """一根K线的决策结果，开/平仓函数只依赖这一份。"""

    i: int
    signal_df: pd.DataFrame
    now: datetime
    px: float
    trail_df: pd.DataFrame | None
    trend_df: pd.DataFrame | None
    next_open: float | None
    stop_before: float
    decision: StrategyDecision
    meta: dict[str, Any]
    scale: str
    gs_bar: Any = None


def _prepare_backtest(
    cfg: dict[str, Any],
    *,
    symbol: str | None,
    bars_signal: pd.DataFrame | None,
    bars_trail: pd.DataFrame | None,
    initial_capital: float | None,
    volume: int | None,
    warmup: int,
    start_date: str | None,
    end_date: str | None,
    engine: str | None,
    chain_capital: bool | None,
    inherit_position: dict[str, Any] | None,
) -> _BacktestSetup:
    """深拷贝配置、定品种/周期/手数口径，拉齐信号/止损/大周期K线并截取时间段。"""
    cfg = _deepcopy_cfg(cfg)
    st = cfg.setdefault("strategy", {})
    trade = cfg.setdefault("trade", {})
    risk = cfg.setdefault("risk", {})
    bt = cfg.setdefault("backtest", {})
    # 调用方传入时盖过配置；换月分段和单合约共用这一个开关，不再另设「手数按起始资金」。
    if chain_capital is not None:
        bt["chain_capital"] = bool(chain_capital)

    if engine:
        st["engine"] = str(engine)

    symbol = symbol or (st.get("symbols") or [trade.get("symbol", "bu2612")])[0]
    st["symbols"] = [symbol]
    trade["symbol"] = symbol

    # 默认跟实盘同一套：按权益和止损距离算手数。只有调用方明确传入 volume 时才改成固定手数。
    if volume is not None:
        trade["volume"] = int(volume)
        risk["sizing_mode"] = "fixed"
        # 固定手数对照时不要再走凯利，否则 1 手测试会被凯利改掉。
        box = dict(risk.get("kelly") or {}) if isinstance(risk.get("kelly"), dict) else {}
        box["enabled"] = False
        risk["kelly"] = box
    capital0 = float(initial_capital if initial_capital is not None else risk.get("capital", 1_000_000))
    risk["capital"] = capital0
    # 从品种字母取乘数
    from src.risk.sizing import PositionSizer

    mult = PositionSizer(cfg).multiplier_for(symbol)

    provider = KlineProvider(cfg)
    # 先建策略，才能知道它固定用什么周期（长线策略自带 60 分钟 + 日线）。
    strategy = create_strategy(cfg)
    tf = str(getattr(strategy, "timeframe", None) or st.get("timeframe", "15"))
    trail_tf = str(getattr(strategy, "trail_timeframe", None) or st.get("trail_timeframe", tf))
    trend_tf = ""
    if getattr(strategy, "uses_trend_bars", False):
        trend_tf = str(getattr(strategy, "trend_timeframe", "") or "")
    # 回测需要足够长的历史；旧逻辑只留约100根 → 经常只有1笔成交、胜率失真
    bt_cfg = cfg.get("backtest") or {}
    max_bars = int(bt_cfg.get("max_bars", 1200))
    warmup = int(bt_cfg.get("warmup", warmup))
    # 策略自带的最少 K 线（含行情过滤回看窗口）比默认预热更长时，把前面那些根直接跳过，
    # 不参加回测循环。不这么做也能跑，只是会重复回放一堆「K线不足」的空决策。
    need = getattr(strategy, "required_bars", None)
    if need is not None:
        try:
            warmup = max(warmup, int(need))
        except (TypeError, ValueError):
            pass
    # 成交近似：下一根开盘、破止损按止损价、按跳滑点。
    fill = FillModel.from_cfg(cfg, symbol)
    # 涨跌停/流动性：默认关闭，只有配置里打开才拦开仓、顺延平仓。
    liquidity = LiquidityModel.from_cfg(cfg)

    if bars_signal is None:
        bars_signal = _fetch_bars_cached(
            provider, symbol, tf, max_bars=max_bars, start=start_date, end=end_date
        )
    if bars_trail is None and trail_tf != tf:
        try:
            bars_trail = _fetch_bars_cached(
                provider, symbol, trail_tf, max_bars=max_bars, start=start_date, end=end_date
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("拉取止损周期失败，回测改用信号周期: {}", e)
            bars_trail = None
    bars_trend = None
    if trend_tf and trend_tf != canonical_period(tf):
        try:
            bars_trend = _fetch_bars_cached(
                provider, symbol, trend_tf, max_bars=max_bars, start=start_date, end=end_date
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("拉取大周期K线失败，这次回测看不清方向，不开新仓: {}", e)
            bars_trend = None
    # 最后一根可能还没走完（行情源会给正在走的那根）。回测只用收盘价，先去掉；
    # 各行情源口径统一，不用每家自己删一次。
    bars_signal = unique_ohlc_columns(drop_forming_bar(bars_signal))
    bars_trail = unique_ohlc_columns(drop_forming_bar(bars_trail))
    if bars_trend is not None:
        bars_trend = unique_ohlc_columns(drop_forming_bar(bars_trend))
    # 先记下行情实际能给的头尾，再按用户日期截。截完后的区间可能比填写的短。
    available_from = str(bars_signal.index[0]) if bars_signal is not None and len(bars_signal) else ""
    available_to = str(bars_signal.index[-1]) if bars_signal is not None and len(bars_signal) else ""
    before = 0 if bars_signal is None else len(bars_signal)
    # 开始日之前留下 warmup 根，只给均线预热。换月后新主力才对得上同花顺图。
    bars_signal = filter_bars_by_range(bars_signal, start_date, end_date, keep_before=warmup)
    bars_trail = filter_bars_by_range(bars_trail, start_date, end_date, keep_before=warmup)
    # 大周期均线要往前看：开始日期只截信号 K 线，日线仍保留更早的收盘。
    if bars_trend is not None:
        bars_trend = filter_bars_by_range(bars_trend, None, end_date)
    after = 0 if bars_signal is None else len(bars_signal)
    if before and after < before:
        logger.info("按时间段截取K线: {} → {} 根 ({} ~ {})", before, after, start_date or "起", end_date or "止")

    trade_from = _range_start_index(bars_signal, start_date)
    loop_start = max(int(trade_from), int(warmup))
    if bars_signal is None or len(bars_signal) <= loop_start:
        raise RuntimeError(
            f"K线不足，无法回测 symbol={symbol} bars={0 if bars_signal is None else len(bars_signal)}。"
            "可把时间段改成「全部可用」，或检查网络后重试。"
        )
    carry = None
    if isinstance(inherit_position, dict):
        side = str(inherit_position.get("side") or "").strip().lower()
        if side in {"long", "short"}:
            try:
                lots = max(1, int(inherit_position.get("volume") or 1))
            except (TypeError, ValueError):
                lots = 1
            carry = {"side": side, "volume": lots}
            logger.info("换月移仓 {} 沿用上一段{} {}手", symbol, "多" if side == "long" else "空", lots)

    logger.info(
        "回测数据就绪 symbol={} engine={} 信号K={}根 止损K={}根 大周期K={}根 warmup={} max_bars={}",
        symbol,
        st.get("engine"),
        len(bars_signal),
        0 if bars_trail is None else len(bars_trail),
        0 if bars_trend is None else len(bars_trend),
        warmup,
        max_bars,
    )
    return _BacktestSetup(
        cfg=cfg,
        st=st,
        trade=trade,
        risk=risk,
        symbol=symbol,
        capital0=capital0,
        mult=mult,
        strategy=strategy,
        tf=tf,
        trail_tf=trail_tf,
        trend_tf=trend_tf,
        max_bars=max_bars,
        warmup=warmup,
        fill=fill,
        liquidity=liquidity,
        bars_signal=bars_signal,
        bars_trail=bars_trail,
        bars_trend=bars_trend,
        available_from=available_from,
        available_to=available_to,
        start_date=start_date,
        end_date=end_date,
        trade_from=trade_from,
        loop_start=loop_start,
        carry=carry,
    )


def _decide_bar(run: _BacktestRun, i: int, view: Any) -> _BarContext:
    """这一根做什么：换月接仓 / 策略评估 / 通用止损 / 交割闸门 / 开仓过滤。"""
    strategy = run.strategy
    symbol = run.symbol
    # 只用第 i 根（含）已收盘的数据做决策；开仓按下一根开盘成交（可配置关闭）。
    signal_df, now, px = view.signal, view.now, view.px
    trail_df, trend_df, next_open = view.trail, view.trend, view.next_open
    # evaluate 会更新尾随止损。止损判定和成交都用这根之前已知的止损，
    # 这根新算出来的止损只对下一根生效，避免用当根K线自己算出的线平自己。
    pos_before = strategy.positions.get(symbol)
    stop_before = 0.0
    if pos_before is not None and pos_before.side != "flat":
        try:
            stop_before = float(pos_before.trail_stop or pos_before.initial_stop or 0)
        except (TypeError, ValueError):
            stop_before = 0.0
    decision = None
    if run.carry_pending:
        # 换月移仓：只在新段第一根按上一段的仓位补回来，用后清空。
        # 以前只有 macd_swing 自己认接仓，别的策略在换月点会静默丢掉持仓。
        run.carry_pending = False
        side = str(run.carry["side"]) if run.carry else "long"
        lots = int(run.carry["volume"]) if run.carry else 1
        run.carry = None
        stop = _carry_stop_price(signal_df, px, side)
        decision = StrategyDecision(
            StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN,
            symbol,
            f"换月移仓：沿用上一段{'多' if side == 'long' else '空'}单 {lots} 手，保护止损按 2 倍 ATR",
            stop_price=stop,
            entry_price=px,
            meta={"rollover_carry": True, "volume": lots},
        )
    if decision is None:
        if getattr(strategy, "uses_trend_bars", False):
            decision = strategy.evaluate(
                symbol, signal_df, now=now, trail_bars=trail_df, trend_bars=trend_df
            )
        else:
            decision = strategy.evaluate(symbol, signal_df, now=now, trail_bars=trail_df)
    # 先按现金+浮动权益更新峰值，再决定这一根能不能开，回撤才跟得上浮亏。
    _pos_eq = strategy.positions.get(symbol)
    _upnl = 0.0
    if _pos_eq and _pos_eq.side == "long" and _pos_eq.volume:
        _upnl = (px - float(_pos_eq.entry_price or 0)) * int(_pos_eq.volume) * run.mult
    elif _pos_eq and _pos_eq.side == "short" and _pos_eq.volume:
        _upnl = (float(_pos_eq.entry_price or 0) - px) * int(_pos_eq.volume) * run.mult
    run.limits.note_equity(run.cash + _upnl)
    # 通用止损：策略自身止损之外的公共止损，策略没让平仓时由它补一刀。
    held_pos = strategy.positions.get(symbol)
    stop_bars = trail_df if trail_df is not None and len(trail_df) > 0 else signal_df
    decision, gs_hit = apply_generic_stop(
        run.generic_stop,
        symbol,
        decision,
        held_pos,
        stop_bars,
        mode=getattr(strategy, "stop_mode", "touch"),
        # 标一下：这是通用止损打掉的，不是策略自己让平。
        # 成交价要按止损线算，不能退回下一根开盘。
        meta={"generic_stop": True},
    )
    # 记住触发的那根止损周期K线，成交按它判跳空，而不是按信号K线。
    gs_bar = gs_hit.bar if gs_hit is not None else None
    # 交割月不再交易这一手：日盘收盘后若夜盘已算交割月，当根收盘平掉，也不再开。
    held_for_roll = strategy.positions.get(symbol)
    flat_now = held_for_roll is None or held_for_roll.side == "flat"
    outcome = apply_delivery_gate(
        symbol,
        now,
        decision,
        flat=flat_now,
        flatten_meta={"delivery_flatten": True},
    )
    if outcome.rejected is not None:
        rejected = outcome.rejected
        run.note_reject(
            "交割换月",
            outcome.gate.reason,
            "多" if rejected.action == StrategyAction.BUY_OPEN else "空",
            at=now,
            price=px,
            stop=rejected.stop_price,
            key="交割月不开仓",
        )
    decision = outcome.decision
    run.limits.on_bar(now)
    meta = decision.meta or {}
    scale = str(meta.get("scale") or "")

    if decision.action == StrategyAction.HOLD and meta.get("blocked"):
        # 策略已经看到开仓信号，但被自己的一条过滤规则挡回去了
        # （箱体太宽、开盘跳空太大、急破不追等）。没成交也要记下来。
        run.note_reject(
            str(meta.get("block_stage") or "策略过滤"),
            decision.reason,
            "多" if str(meta.get("side") or "") == "long" else "空",
            at=now,
            price=px,
            stop=decision.stop_price,
            key=str(meta.get("block_key") or ""),
        )
    return _BarContext(
        i=i,
        signal_df=signal_df,
        now=now,
        px=px,
        trail_df=trail_df,
        trend_df=trend_df,
        next_open=next_open,
        stop_before=stop_before,
        decision=decision,
        meta=meta,
        scale=scale,
        gs_bar=gs_bar,
    )


def _execution_bar(run: _BacktestRun, ctx: _BarContext) -> Any:
    """这次成交用的是哪根K线：开了「下一根开盘」就取下一根，否则取信号这根。"""
    if run.fill.on_next_open and ctx.next_open is not None:
        idx = ctx.i + 1
        if 0 <= idx < len(run.bars_signal):
            return run.bars_signal.iloc[idx]
    if ctx.signal_df is not None and len(ctx.signal_df):
        return ctx.signal_df.iloc[-1]
    return None


def _open_position(run: _BacktestRun, ctx: _BarContext) -> bool | None:
    """执行开仓/反手/加仓。True=已开仓，False=处理完没开，None=这根直接跳过。"""
    strategy = run.strategy
    symbol = run.symbol
    mult = run.mult
    decision = ctx.decision
    meta, scale = ctx.meta, ctx.scale
    now, px, signal_df, next_open, stop_before = (
        ctx.now,
        ctx.px,
        ctx.signal_df,
        ctx.next_open,
        ctx.stop_before,
    )
    pos = strategy.positions.get(symbol)
    want = "long" if decision.action == StrategyAction.BUY_OPEN else "short"
    # 涨跌停一字板：这根没有可成交价，开仓和反手都不做。
    exec_bar = _execution_bar(run, ctx)
    if run.liquidity.locked(exec_bar):
        run.note_reject(
            "涨跌停",
            "成交这根没有价格波动，开仓不成交",
            "多" if want == "long" else "空",
            at=now,
            price=px,
            stop=stop_before or decision.stop_price,
            key="涨跌停不开仓",
        )
        return None
    # 同一根先平掉相反仓，再开新方向。没有 reverse 标记的策略行为不变。
    if pos and pos.side not in ("flat", want) and meta.get("reverse"):
        held = pos.side
        # 用这根之前已知的止损判定和成交；本根新算的止损下一根才生效。
        pos_stop = stop_before or _position_stop(pos, decision.stop_price)
        exit_px = _exit_fill_price(
            side=held,
            stop=pos_stop,
            bar=signal_df,
            next_open=next_open,
            close_px=px,
            fill=run.fill,
            touch_stop=bool(getattr(strategy, "stop_on_touch", True)),
        )
        entry_px = float(pos.entry_price or run.open_meta.get("entry_price") or exit_px)
        vol_now = int(pos.volume or 0)
        gross = (
            (exit_px - entry_px) * vol_now * mult
            if held == "long"
            else (entry_px - exit_px) * vol_now * mult
        )
        pnl, fee = _net_pnl_after_fees(
            run.contracts,
            symbol,
            gross=gross,
            volume=vol_now,
            held=vol_now,
            open_meta=run.open_meta,
            exit_time=now.isoformat(timespec="seconds"),
        )
        run.cash += pnl
        run.trades.append(
            _make_trade_record(
                symbol=symbol,
                side=held,
                entry_time=str(run.open_meta.get("entry_time", "")),
                exit_time=now.isoformat(timespec="seconds"),
                entry_price=entry_px,
                exit_price=exit_px,
                volume=vol_now,
                pnl=pnl,
                reason_entry=str(run.open_meta.get("reason", "")),
                reason_exit=decision.reason,
                commission=fee,
                open_meta=run.open_meta,
                multiplier=mult,
                adx_at_entry=run.open_meta.get("adx_at_entry"),
            )
        )
        strategy.on_filled(symbol, StrategyAction.CLOSE, exit_px, vol_now)
        if hasattr(strategy, "on_trade_closed"):
            try:
                strategy.on_trade_closed(
                    symbol,
                    float(pnl),
                    volume=int(vol_now),
                    side=str(held or ""),
                    entry_price=float(entry_px or 0),
                    exit_price=float(exit_px or 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("{} 平仓后进化没跑完: {}", symbol, exc)
        run.limits.on_close(pnl, vol_now, symbol, at=now)
        run.limits.note_equity(run.cash)
        run.open_meta = {}
        pos = strategy.positions.get(symbol)
    # 同向加仓：已有仓且 meta.scale=add 时允许继续开；其它情况有仓就跳过。
    is_add = bool(pos and pos.side == want and scale == "add")
    if pos and pos.side != "flat" and not is_add:
        held = "多" if pos.side == "long" else "空"
        run.note_reject(
            "已有持仓",
            f"手里还拿着{held}单，这根的同向开仓不再重复做",
            "多" if want == "long" else "空",
            at=now,
            price=px,
            stop=decision.stop_price,
            key="已有持仓",
        )
        return None
    # 成交口径统一：开了「下一根开盘」就按下一根开盘（跨休市也一样），
    # 只有最后一根没有下一根时才按当根收盘成交。
    # 不能再用策略内部价顶替，否则跳空时会报出当天根本没成交过的开仓价。
    if run.fill.on_next_open and next_open is not None:
        entry_ref = next_open
        entry_ts = run.bars_signal.index[ctx.i + 1]
        # 下一根已经是收盘清仓时点：按开盘成交再按同一根收盘强平，
        # 实盘不会这样下单。换月移仓不算新开仓，照旧放行。
        flatten = getattr(strategy, "is_flatten_time", None)
        moving = bool(meta.get("rollover_carry") or (run.carry and want == run.carry.get("side")))
        if not moving and callable(flatten):
            try:
                at_close = bool(flatten(_ts_to_dt(entry_ts)))
            except Exception:  # noqa: BLE001
                at_close = False
            if at_close:
                run.note_reject(
                    "开仓限制",
                    "下一根已是收盘清仓时点，不再开新仓",
                    "多" if want == "long" else "空",
                    at=now,
                    price=entry_ref,
                    stop=decision.stop_price,
                    key="收盘时点",
                )
                return None
    else:
        entry_ref = px
        entry_ts = run.bars_signal.index[ctx.i]
    # 开多=买入，开空=卖出，都要让滑点。
    entry = (
        run.fill.buy(float(entry_ref))
        if decision.action == StrategyAction.BUY_OPEN
        else run.fill.sell(float(entry_ref))
    )
    stop = decision.stop_price
    # 先按权益/风险算出允许手数；策略指定的单元手数只能往下压，不能绕过。
    allowed_vol, allowed_reason = run.limits.size_open(symbol, entry, stop)
    open_vol, cap_note = _cap_open_volume(
        allowed_vol, meta.get("volume"), limits_apply=bool(run.limits.apply)
    )
    open_vol, scale_note = _apply_size_scale(open_vol, meta.get("size_scale"))
    from src.risk.dynamic import apply_dynamic_open

    open_vol, dyn_note = apply_dynamic_open(
        run.limits.cfg,
        open_vol,
        signal_df,
        equity=run.limits.equity,
        peak=run.limits.peak_equity,
    )
    notes = [item for item in (cap_note, scale_note, dyn_note) if item]
    if notes:
        joined = "；".join(notes)
        size_reason = f"{joined}（{allowed_reason}）" if allowed_reason else joined
    else:
        size_reason = allowed_reason
    if meta.get("rollover_carry"):
        # 换月移仓：你本来就拿着仓，不是新开冒险。止损跟着新主力均线，
        # 距离可能宽过每笔风险%，仍按上一段手数移过去。
        # carry 只在段首这一根生效，用后清空，之后的开仓照常走手数限制。
        try:
            want_vol = max(1, int(meta.get("volume") or 1))
        except (TypeError, ValueError):
            want_vol = 1
        open_vol = want_vol
        cap_note = f"换月接仓{open_vol}手"
        size_reason = (
            f"换月移仓：止损宽于每笔风险%，仍按{open_vol}手把仓移到新主力"
            + (f"（{allowed_reason}）" if allowed_reason else "")
        )
    # 流动性：单根新开仓手数不能超过这根成交量的约定比例。
    if open_vol > 0 and run.liquidity.max_volume_pct > 0:
        capped = run.liquidity.cap_open_volume(open_vol, exec_bar)
        if capped <= 0:
            reason = "这根成交量太小，开仓不成交"
            run.limits.note_skip(reason)
            run.note_reject(
                "流动性",
                reason,
                "多" if want == "long" else "空",
                at=now,
                price=entry,
                stop=stop,
                key=reason,
            )
            return None
        if capped < open_vol:
            note = f"单根成交量上限{capped}手"
            size_reason = f"{note}（{size_reason}）" if size_reason else note
            open_vol = capped
    if open_vol <= 0:
        run.limits.note_skip(size_reason)
        run.note_reject(
            "风控手数",
            size_reason,
            "多" if want == "long" else "空",
            at=now,
            price=entry,
            stop=stop,
            key=skip_reason_key(size_reason),
        )
        return None
    allowed, block_reason = run.limits.allow_open(now, open_vol)
    if not allowed:
        run.limits.note_skip(block_reason)
        run.note_reject(
            "开仓限制",
            block_reason,
            "多" if want == "long" else "空",
            at=now,
            price=entry,
            stop=stop,
            key=skip_reason_key(block_reason),
        )
        return None
    strategy.on_filled(
        symbol,
        decision.action,
        entry,
        open_vol,
        initial_stop=stop,
    )
    run.limits.on_open(now, decision.action.value, open_vol, symbol)
    pos_after = strategy.positions.get(symbol)
    # 有些策略（日内实体）开仓时用真实时钟记 opened_at；
    # 回测里必须改成这根K线的模拟时间，否则通用止损会把这些历史K线
    # 全当成「开仓之前」，整段回测都不会生效。
    if pos_after is not None and pos_after.side != "flat" and not is_add:
        pos_after.opened_at = now.isoformat(timespec="seconds")
    open_fee = fee_open(run.contracts, symbol, open_vol)
    if is_add and run.open_meta:
        # 加仓后按真实成交价重算均价；不再用策略内部的信号价。
        prev_vol = int(run.open_meta.get("open_volume") or 0)
        prev_avg = float(run.open_meta.get("entry_price") or entry)
        total_vol = prev_vol + open_vol
        run.open_meta["entry_price"] = (
            (prev_avg * prev_vol + entry * open_vol) / total_vol if total_vol else entry
        )
        run.open_meta["reason"] = decision.reason
        run.open_meta["open_fee"] = float(run.open_meta.get("open_fee") or 0) + open_fee
        run.open_meta["open_volume"] = total_vol
    else:
        run.open_meta = {
            "side": "long" if decision.action == StrategyAction.BUY_OPEN else "short",
            "entry_time": _ts_to_dt(entry_ts).isoformat(timespec="seconds"),
            "entry_price": float(entry),
            "reason": decision.reason,
            "open_fee": open_fee,
            "open_volume": open_vol,
            "score": meta.get("score"),
            "score_ma": meta.get("score_ma"),
            "score_turtle": meta.get("score_turtle"),
            "score_boll": meta.get("score_boll"),
            "score_sar": meta.get("score_sar"),
            "atr": meta.get("atr"),
            "adx_at_entry": _adx_at(run.adx_values, ctx.i),
        }
    bar_id = str(meta.get("bar_id", ""))
    if bar_id:
        strategy.set_last_entry_bar(symbol, bar_id)
    return True


def _close_position(run: _BacktestRun, ctx: _BarContext) -> bool:
    """执行策略/通用止损平仓。False=没有仓可平，这根直接跳过。"""
    strategy = run.strategy
    symbol = run.symbol
    mult = run.mult
    decision = ctx.decision
    meta, now, px, signal_df, next_open, stop_before = (
        ctx.meta,
        ctx.now,
        ctx.px,
        ctx.signal_df,
        ctx.next_open,
        ctx.stop_before,
    )
    pos = strategy.positions.get(symbol)
    if not pos or pos.side == "flat":
        return False
    side = pos.side
    pos_stop = stop_before or _position_stop(pos, decision.stop_price)
    # 通用止损有明确的止损线，按这条线成交；策略自己平仓才用持仓的尾随止损。
    if meta.get("generic_stop") and decision.stop_price:
        pos_stop = float(decision.stop_price)
    # 收盘清仓是到点主动走，按当根收盘成交；拖到下一根开盘（可能是夜盘）
    # 就不是收盘清仓了。之前这里按下一根开盘算，等于让策略多持有一整段。
    # 只看策略给的原因，避免清仓窗口内的止损单也被按收盘价成交。
    flatten_close = "收盘清仓" in str(decision.reason)
    # 触发它的那根止损周期K线（比如 15 分钟）决定跳空多少，不能用信号K线代判。
    fill_bar = signal_df if ctx.gs_bar is None else ctx.gs_bar.to_frame().T
    # 交割月换月必须当根收盘走，不能拖到夜盘开盘（那根已经算交割月）；
    # 收盘清仓同样当根收盘走。
    exit_next_open = None if (meta.get("delivery_flatten") or flatten_close) else next_open
    # 涨跌停一字板：这根没有可成交价，平仓顺延到下一根继续试。
    exit_bar = _execution_bar(run, ctx) if exit_next_open is not None else fill_bar
    if run.liquidity.locked(exit_bar):
        run.pending_exit = decision
        run.note_reject(
            "涨跌停",
            "成交这根没有价格波动，平仓顺延到下一根",
            "多" if side == "long" else "空",
            at=now,
            price=px,
            stop=pos_stop,
            key="涨跌停不平仓",
        )
        return False
    exit_px = _exit_fill_price(
        side=side,
        stop=pos_stop,
        bar=fill_bar,
        next_open=exit_next_open,
        close_px=px,
        fill=run.fill,
        touch_stop=False if flatten_close else bool(getattr(strategy, "stop_on_touch", True)),
    )
    entry = float(pos.entry_price or run.open_meta.get("entry_price") or exit_px)
    held = int(pos.volume or 0)
    # 减仓：meta.volume / close_volume 指定平多少；否则全平。
    raw_cut = meta.get("close_volume", meta.get("volume"))
    if raw_cut is not None:
        try:
            vol = max(1, min(held, int(raw_cut)))
        except (TypeError, ValueError):
            vol = held
    else:
        vol = held
    gross = (exit_px - entry) * vol * mult if side == "long" else (entry - exit_px) * vol * mult
    exit_time = now.isoformat(timespec="seconds")
    pnl, fee = _net_pnl_after_fees(
        run.contracts,
        symbol,
        gross=gross,
        volume=vol,
        held=held,
        open_meta=run.open_meta,
        exit_time=exit_time,
    )
    run.cash += pnl
    run.trades.append(
        _make_trade_record(
            symbol=symbol,
            side=side,
            entry_time=str(run.open_meta.get("entry_time", "")),
            exit_time=exit_time,
            entry_price=entry,
            exit_price=exit_px,
            volume=vol,
            pnl=pnl,
            reason_entry=str(run.open_meta.get("reason", "")),
            reason_exit=decision.reason,
            commission=fee,
            open_meta=run.open_meta,
            multiplier=mult,
            adx_at_entry=run.open_meta.get("adx_at_entry"),
        )
    )
    strategy.on_filled(symbol, StrategyAction.CLOSE, exit_px, vol)
    if hasattr(strategy, "on_trade_closed"):
        try:
            strategy.on_trade_closed(
                symbol,
                float(pnl),
                volume=int(vol),
                side=str(side or ""),
                entry_price=float(entry or 0),
                exit_price=float(exit_px or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("{} 平仓后进化没跑完: {}", symbol, exc)
    run.limits.on_close(pnl, vol, symbol, at=now)
    run.limits.note_equity(run.cash)
    pos_left = strategy.positions.get(symbol)
    if not pos_left or pos_left.side == "flat" or int(pos_left.volume or 0) <= 0:
        run.open_meta = {}
        run.pending_exit = None
    else:
        # 部分减仓后，剩余仓仍用均价记账。
        run.open_meta["entry_price"] = float(pos_left.entry_price or entry)
    return True


def _step_backtest_bar(run: _BacktestRun, i: int, view: Any) -> None:
    """处理一根K线：决策 → 开/平仓 → 记资金曲线。"""
    ctx = _decide_bar(run, i, view)
    # 上一根涨跌停没平掉，这一根先接着平，不等策略再发一次信号。
    if run.pending_exit is not None:
        held_now = run.strategy.positions.get(run.symbol)
        if held_now is not None and held_now.side != "flat":
            ctx.decision = run.pending_exit
        else:
            run.pending_exit = None
    opened_this_bar = False
    if ctx.decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
        opened = _open_position(run, ctx)
        if opened is None:
            return
        opened_this_bar = opened
    elif ctx.decision.action == StrategyAction.CLOSE:
        if not _close_position(run, ctx) and run.pending_exit is None:
            return
    # 浮动权益：现金 + 未实现盈亏。刚开的仓按下一根开盘成交，当根还不持有，不算浮盈。
    pos = run.strategy.positions.get(run.symbol)
    upnl = 0.0
    if opened_this_bar:
        upnl = 0.0
    elif pos and pos.side == "long" and pos.volume:
        upnl = (ctx.px - pos.entry_price) * pos.volume * run.mult
    elif pos and pos.side == "short" and pos.volume:
        upnl = (pos.entry_price - ctx.px) * pos.volume * run.mult
    run.equity.append(run.cash + upnl)
    run.equity_times.append(ctx.now.isoformat(timespec="seconds"))


def _force_close_at_end(run: _BacktestRun) -> None:
    """回测结束仍持仓，按最后一根收盘强平，便于统计完整。"""
    strategy = run.strategy
    symbol = run.symbol
    pos = strategy.positions.get(symbol)
    if not pos or pos.side == "flat" or not pos.volume:
        return
    px = float(run.bars_signal["close"].iloc[-1])
    now = _ts_to_dt(run.bars_signal.index[-1])
    # 用实际成交价算盈亏；策略里的 entry_price 可能是信号价，和真实成交不一致。
    entry = float(run.open_meta.get("entry_price") or pos.entry_price)
    side = pos.side
    vol = int(pos.volume)
    # 强平同样按卖出/买入方向让滑点，避免把最后一笔算得过于理想。
    exit_px = run.fill.sell(px) if side == "long" else run.fill.buy(px)
    gross = (
        (exit_px - entry) * vol * run.mult
        if side == "long"
        else (entry - exit_px) * vol * run.mult
    )
    exit_time = now.isoformat(timespec="seconds")
    pnl, fee = _net_pnl_after_fees(
        run.contracts,
        symbol,
        gross=gross,
        volume=vol,
        held=vol,
        open_meta=run.open_meta,
        exit_time=exit_time,
    )
    run.cash += pnl
    run.trades.append(
        _make_trade_record(
            symbol=symbol,
            side=side,
            entry_time=str(run.open_meta.get("entry_time", "")),
            exit_time=exit_time,
            entry_price=entry,
            exit_price=exit_px,
            volume=vol,
            pnl=pnl,
            reason_entry=str(run.open_meta.get("reason", "")),
            reason_exit="回测结束强平",
            commission=fee,
            open_meta=run.open_meta,
            multiplier=run.mult,
            adx_at_entry=run.open_meta.get("adx_at_entry"),
        )
    )
    run.equity.append(run.cash)
    run.equity_times.append(exit_time)


def _params_snapshot(setup: _BacktestSetup, run: _BacktestRun) -> dict[str, Any]:
    """回测参数快照：结果区据此说明这次用了哪些口径。"""
    cfg, st, strategy = run.cfg, run.st, run.strategy
    trade, risk = run.trade, run.risk
    limits, fill = run.limits, run.fill
    bars_signal, bars_trail = run.bars_signal, setup.bars_trail
    trade_from = setup.trade_from
    return {
        "engine": st.get("engine"),
        # 记下这一次用的行情来源。送到交叉测试时，下拉框可以跟着变成同一家。
        "data_source": (cfg.get("data") or {}).get("source") or "akshare",
        "flatten_at_close": bool(getattr(strategy, "flatten_at_close", True)),
        "timeframe": setup.tf,
        "trail_timeframe": setup.trail_tf,
        "max_bars": setup.max_bars,
        "warmup": setup.warmup,
        "start_date": setup.start_date or "",
        "end_date": setup.end_date or "",
        "available_from": setup.available_from,
        "available_to": setup.available_to,
        "data_from": str(bars_signal.index[trade_from]) if len(bars_signal) else "",
        "data_to": str(bars_signal.index[-1]) if len(bars_signal) else "",
        "lookback_bars": int(trade_from),
        "coverage_note": _coverage_with_gaps(
            coverage_note(
                setup.start_date,
                setup.end_date,
                setup.available_from,
                setup.available_to,
                str(bars_signal.index[trade_from]) if len(bars_signal) else "",
                str(bars_signal.index[-1]) if len(bars_signal) else "",
                setup.tf,
            ),
            bars_signal,
            setup.tf,
            bars_trail,
            setup.trail_tf,
        ),
        # 日内实体相关：ETH 观察分钟、箱沿让跳（写在 daily_body 小节）。
        "eth_minutes": (st.get("daily_body") or {}).get("eth_minutes"),
        "stop_extra_ticks": (st.get("daily_body") or {}).get("stop_extra_ticks"),
        # 止损触发方式（全策略共用）：touch=触及即止损，close=收盘价确认。
        "stop_mode": getattr(strategy, "stop_mode", None),
        # 开仓触发方式（全策略共用）：trigger=触到即进，close=收盘确认（默认保守）。
        "entry_mode": getattr(strategy, "entry_mode", None),
        # 通用止损（独立于策略自身止损）：记下这次用的是哪一种，结果能对齐。
        "generic_stop": generic_stop_snapshot(cfg),
        "apply_limits": bool(limits.apply),
        # 成交近似参数，结果区会原样说明，避免历史结果和新规则混在一起看不出原因。
        "fill_on_next_open": fill.on_next_open,
        "slippage_ticks": fill.slippage_ticks,
        "price_tick": fill.price_tick,
        "fill_note": fill.describe(),
        # 涨跌停/流动性口径。默认关闭时会原样写明，便于和历史结果对照。
        "liquidity": run.liquidity.snapshot(),
        "sizing_mode": str(risk.get("sizing_mode", "risk_pct")),
        "kelly_in_backtest": bool(
            isinstance(risk.get("kelly"), dict) and risk["kelly"].get("enabled")
        ),
        "capital": setup.capital0,
        "chain_capital": bool(limits.chain_capital),
        "risk_per_trade_pct": float(risk.get("risk_per_trade_pct", 0.5)),
        "margin_usage_pct": float(risk.get("margin_usage_pct", 80) or 0),
        "fixed_volume": int(trade.get("volume", 1)),
        "min_volume": int(risk.get("min_volume", 1)),
        "max_daily_loss": float(risk.get("max_daily_loss", 1000)),
        "min_order_interval_sec": int(risk.get("min_order_interval_sec", 60)),
        "max_orders_per_day": int(risk.get("max_orders_per_day", 20)),
        "margin_per_lot": float(risk.get("margin_per_lot", 0) or 0),
        # 这一笔仓最多要交多少保证金。收益率用它做分母，不拿来改手数。
        "margin_used": peak_margin(run.trades, run.mult, MARGIN_RATE),
        "margin_rate": MARGIN_RATE,
        "skipped_opens": dict(limits.skipped),
    }


def run_backtest(
    cfg: dict[str, Any],
    *,
    symbol: str | None = None,
    bars_signal: pd.DataFrame | None = None,
    bars_trail: pd.DataFrame | None = None,
    initial_capital: float | None = None,
    volume: int | None = None,
    warmup: int = 40,
    start_date: str | None = None,
    end_date: str | None = None,
    engine: str | None = None,
    chain_capital: bool | None = None,
    inherit_position: dict[str, Any] | None = None,
) -> BacktestResult:
    """
    对单个品种跑完整回测。

    参数:
        cfg: 与实盘相同的配置（会深拷贝，不污染原配置）
        symbol: 合约，默认取 strategy.symbols[0]
        bars_signal / bars_trail: 可直接传入K线；否则自动拉取
        initial_capital: 初始资金（默认用 risk.capital）
        volume: 若传入，则这次回测改成固定手数，不再按风险算仓
        start_date / end_date: 可选，格式 YYYY-MM-DD；空则用全部已拉到的K线
        engine: 可选，临时指定策略引擎（不改磁盘配置）
        chain_capital: 真实账户口径。None=读 backtest.chain_capital（默认关）。
            开=手数跟着剩余权益走；关=每一笔都按本段起始资金。
        inherit_position: 换月移仓。上一段结束时还拿着仓，就带
            {"side": "long"|"short", "volume": 手数}，新主力第一根按同方向开。
    """
    setup = _prepare_backtest(
        cfg,
        symbol=symbol,
        bars_signal=bars_signal,
        bars_trail=bars_trail,
        initial_capital=initial_capital,
        volume=volume,
        warmup=warmup,
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        chain_capital=chain_capital,
        inherit_position=inherit_position,
    )
    cfg, st, trade, risk = setup.cfg, setup.st, setup.trade, setup.risk
    symbol, capital0, mult = setup.symbol, setup.capital0, setup.mult
    strategy, tf, trail_tf = setup.strategy, setup.tf, setup.trail_tf
    trend_tf = setup.trend_tf
    fill, bars_signal, bars_trail = setup.fill, setup.bars_signal, setup.bars_trail
    bars_trend = setup.bars_trend
    trade_from, loop_start = setup.trade_from, setup.loop_start
    # 确保空仓起点
    strategy.positions[symbol] = PositionState(symbol=symbol)
    strategy.set_last_entry_bar(symbol, "")
    # 通用止损：和策略自身止损相互独立，谁先触发就按谁平。
    generic_stop = GenericStopOverlay(cfg)
    limits = BacktestLimits(cfg)
    limits.note_equity(capital0)
    n = len(bars_signal)
    # 评估用的开仓 ADX。算不出来就留空，不影响回测本身。
    eval_thresholds = EvaluationThresholds.from_cfg(cfg)
    adx_values = _adx_for_eval(bars_signal, eval_thresholds.adx_period)
    run = _BacktestRun(
        cfg=cfg,
        st=st,
        trade=trade,
        risk=risk,
        symbol=symbol,
        mult=mult,
        strategy=strategy,
        fill=fill,
        generic_stop=generic_stop,
        contracts=(cfg.get("risk") or {}).get("contracts") or {},
        bars_signal=bars_signal,
        adx_values=adx_values,
        limits=limits,
        liquidity=setup.liquidity,
        cash=capital0,
        carry=setup.carry,
        carry_pending=setup.carry is not None,
        equity=[capital0],
        equity_times=[str(bars_signal.index[min(max(loop_start, 0), n - 1)])],
    )
    # 回测循环里策略会狂打 info，先关掉这几个模块日志，结束再打开
    for _mod in (
        "src.strategy.trend_follow",
        "src.strategy.supertrend",
        "src.strategy.dual_ma",
        "src.strategy.macd_swing",
        "src.strategy.bollinger_break",
        "src.strategy.sar_trend",
        "src.strategy.multi_factor",
        "src.strategy.daily_body",
        "src.strategy.swing_trend",
        "src.strategy.swing_box",
        "src.risk.sizing",
        "src.risk.guardian",
        "src.risk.dynamic",
    ):
        logger.disable(_mod)

    try:
        # 逐根游标：当前K、下一根开盘价、止损/大周期对齐都在这里算，主循环只管「这一根做什么」。
        cursor = BarCursor(
            bars_signal,
            trail=bars_trail,
            trend=bars_trend,
            timeframe=tf,
            trend_timeframe=trend_tf,
            start=loop_start,
            fill_next_open=fill.on_next_open,
        )
        for i in cursor.indices():
            view = cursor.at(i)
            if view is None:
                continue
            _step_backtest_bar(run, i, view)
    finally:
        for _mod in (
            "src.strategy.trend_follow",
            "src.strategy.supertrend",
            "src.strategy.dual_ma",
            "src.strategy.macd_swing",
            "src.strategy.bollinger_break",
            "src.strategy.sar_trend",
            "src.strategy.multi_factor",
            "src.strategy.daily_body",
            "src.strategy.swing_trend",
            "src.strategy.swing_box",
            "src.risk.sizing",
            "src.risk.guardian",
        ):
            logger.enable(_mod)

    _force_close_at_end(run)
    trades, equity, equity_times = run.trades, run.equity, run.equity_times

    # 样本过少时胜率没有统计意义；门槛随实际成交略放宽
    min_trades = 3 if len(trades) >= 3 else 2
    perf = calc_performance(trades, equity, min_trades=min_trades)
    snap = _params_snapshot(setup, run)
    logger.info(
        "回测结束 {} | bars={} 成交{}笔 胜率{:.0%} 盈亏比{:.2f} 净盈亏{:.0f}",
        symbol,
        n,
        perf.trades,
        perf.win_rate,
        perf.payoff,
        perf.net_pnl,
    )
    result = BacktestResult(
        symbol=symbol,
        timeframe=tf,
        trail_timeframe=trail_tf,
        bars=max(0, n - int(trade_from)),
        performance=perf,
        trades=trades,
        equity=equity,
        equity_times=equity_times,
        params_snapshot=snap,
        rejected=run.rejected,
        # 记到秒。同一次回测的摘要和每一笔成交共用这个时间，再导出也不会变。
        ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if eval_thresholds.enabled:
        result.evaluation = evaluate_backtest(result, thresholds=eval_thresholds)
    return result


def save_backtest_report(result: BacktestResult, out_path: Path | str) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
