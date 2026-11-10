"""
历史回测引擎
功能：用历史分钟K线，按与实盘相同的策略逻辑模拟开平仓，输出成交与绩效。
原理：按时间一根根推进；每根已收盘K上调用 strategy.evaluate；成交价取当根收盘价近似。
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.backtest.metrics import MARGIN_RATE, Performance, TradeRecord, calc_performance, peak_margin
from src.strategy.cta_factors import factor_fields_for_trade
from src.data.kline import KlineProvider
from src.data.periods import canonical_period, drop_forming_bar, last_ohlc_price, period_label, unique_ohlc_columns
from src.risk.commission import fee_close, fee_open, is_close_today
from src.strategy import StrategyAction, StrategyDecision, create_strategy
from src.strategy.generic_stop import GenericStopOverlay, generic_stop_snapshot
from src.strategy.session_scope import session_kind
from src.strategy.types import PositionState, config_flag
from src.utils.trading_day import futures_trading_day


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


@dataclass
class RejectedSignal:
    """一次出现了开仓信号、但没有开成的记录。成交明细之外单独列，方便查为什么没做。"""

    symbol: str
    time: str
    side: str  # 多 / 空
    price: float | None  # 信号价（下一根开盘时就是准备成交的价）
    stop: float | None  # 如果开了，止损放在哪
    stage: str  # 策略过滤 / 风控手数 / 开仓限制 / 已有持仓
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    trail_timeframe: str
    bars: int
    performance: Performance
    trades: list[TradeRecord] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    params_snapshot: dict[str, Any] = field(default_factory=dict)
    # 有信号但没开成的记录。为空表示这段行情里每个开仓信号都成交了。
    rejected: list[RejectedSignal] = field(default_factory=list)
    # 这次回测跑完的本地时间。导出 Excel 时原样写进每一行，用来区分前后几次回测。
    ran_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "trail_timeframe": self.trail_timeframe,
            "bars": self.bars,
            "ran_at": self.ran_at,
            "performance": self.performance.to_dict(),
            "trades": [asdict(t) for t in self.trades],
            "rejected": [r.to_dict() for r in self.rejected],
            "equity_last": self.equity[-1] if self.equity else None,
            "params_snapshot": self.params_snapshot,
        }


def _deepcopy_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(cfg)


def _ts_to_dt(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if hasattr(ts, "to_pydatetime"):
        return ts.to_pydatetime()
    return pd.Timestamp(ts).to_pydatetime()


@dataclass(frozen=True)
class FillModel:
    """
    回测成交近似。

    - on_next_open：信号在当根收盘后才确认，按下一根开盘成交（跨休市也按下一根开盘）。
    - slippage_ticks：成交价按买卖方向各让几跳，模拟挂单排队和盘口差价。
    - price_tick：一跳多少钱，默认取交易配置里的 price_tick。
    """

    on_next_open: bool = True
    slippage_ticks: float = 1.0
    price_tick: float = 1.0

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any], symbol: str | None = None) -> "FillModel":
        bt = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
        trade = cfg.get("trade") if isinstance(cfg.get("trade"), dict) else {}
        ctp = trade.get("ctp") if isinstance(trade.get("ctp"), dict) else {}
        tick_raw = bt.get("price_tick")
        if tick_raw in (None, ""):
            # 品种表里填了每跳价格就优先用它，没填才回到全局 price_tick。
            from src.risk.universe_table import product_key

            key = product_key(str(symbol or ""))
            contracts = (cfg.get("risk") or {}).get("contracts") or {}
            box = contracts.get(key) if isinstance(contracts.get(key), dict) else {}
            tick_raw = (box or {}).get("price_tick")
        if tick_raw in (None, ""):
            tick_raw = ctp.get("price_tick", trade.get("price_tick", 1))
        try:
            tick = float(tick_raw)
        except (TypeError, ValueError):
            tick = 1.0
        if tick <= 0:
            tick = 1.0
        raw_slip = bt.get("slippage_ticks", 1)
        try:
            slip_ticks = max(0.0, float(raw_slip))
        except (TypeError, ValueError):
            slip_ticks = 1.0
        return cls(
            on_next_open=config_flag(bt.get("fill_on_next_open"), default=True),
            slippage_ticks=slip_ticks,
            price_tick=tick,
        )

    @property
    def slip(self) -> float:
        """一跳滑点折算成的价格差。"""
        return round(self.slippage_ticks * self.price_tick, 10)

    def buy(self, ref: float) -> float:
        """买入成交价：往上让滑点。"""
        return round(float(ref) + self.slip, 10)

    def sell(self, ref: float) -> float:
        """卖出成交价：往下让滑点。"""
        return round(float(ref) - self.slip, 10)

    def describe(self) -> str:
        """回测结果里说明这次是怎么成交的。"""
        parts = ["信号确认后按下一根开盘成交（跨休市也按下一根开盘）" if self.on_next_open else "成交按策略价，未给则当根收盘"]
        if self.slip > 0:
            parts.append(f"滑点 {self.slippage_ticks:g} 跳（每跳 {self.price_tick:g}）")
        else:
            parts.append("未加滑点")
        parts.append("破止损按止损价成交，跳空按开盘价")
        return "成交近似  " + "；".join(parts)


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
        **extra,
    )


def _next_open_price(bars: pd.DataFrame, i: int, period: str) -> float | None:
    """下一根开盘价。

    跨休市也按下一根开盘成交：真实下单就是等下一次开盘，不能拿上一段收盘价顶替，
    否则跳空低开时会出现当天根本没成交过的开仓价。
    """
    if bars is None or i + 1 >= len(bars):
        return None
    code = canonical_period(period)
    if code in ("D", "W"):
        return float(bars["open"].iloc[i + 1])
    if not code.isdigit():
        return None
    return float(bars["open"].iloc[i + 1])


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


def _position_stop(pos: Any, decision_stop: float | None) -> float:
    """
    平仓用的止损价：优先用策略更新后的尾随止损，没有再退回初始止损/决策止损。

    旧写法把 initial_stop 放在前面，`or` 一短路，尾随止损永远用不上，
    回测里移动止损就形同不存在。
    """
    trail = float(getattr(pos, "trail_stop", 0) or 0)
    initial = float(getattr(pos, "initial_stop", 0) or 0)
    return trail or initial or float(decision_stop or 0)


def _stop_breached(side: str, stop: float, bar: pd.DataFrame) -> bool:
    """这根K线的最高或最低有没有打穿止损。"""
    if stop <= 0 or bar is None or len(bar) == 0:
        return False
    if side == "long":
        return float(bar["low"].iloc[-1]) <= stop
    if side == "short":
        return float(bar["high"].iloc[-1]) >= stop
    return False


def _exit_fill_price(
    *,
    side: str,
    stop: float,
    bar: pd.DataFrame,
    next_open: float | None,
    close_px: float,
    fill: FillModel,
    touch_stop: bool = True,
) -> float:
    """
    平仓成交价。

    touch_stop=True（触及即止损）：这根K线已经打穿止损时按止损价成交（跳空按开盘价，更差）。
    touch_stop=False（收盘价确认）：这次平仓是收盘后才决定的，不能按止损价成交，
    退回下一根开盘价 / 当根收盘价，避免回测把收盘确认方式算得过于理想。
    两种都再按买卖方向让滑点。
    """
    ref = None
    if touch_stop and _stop_breached(side, stop, bar):
        bar_open = float(bar["open"].iloc[-1])
        ref = min(stop, bar_open) if side == "long" else max(stop, bar_open)
    if ref is None:
        ref = next_open if next_open is not None else close_px
    return fill.sell(ref) if side == "long" else fill.buy(ref)


# 国内期货白天三段加上夜盘。K线时间用该段收盘时刻，例如 5 分钟的第一根是 09:05。
# 10:15–10:30、午休、白天到夜盘、过夜和周末都空在这些时段外面，不算数据断开。
_SESSION_SEGMENTS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
    (time(21, 0), time(23, 0)),
)
# 交易所休市的工作日。这些天本来就没有K线，不叫「中间断了」。
_CLOSED_WEEKDAYS = {
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 10, 1),
    date(2026, 10, 2),
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
}


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
) -> pd.DataFrame | None:
    """
    按期货交易日截取K线（含起止当天）。

    开始日 T 从 T-1 晚上 21:00 的夜盘算起，结束日 T 一直算到 T 20:59；
    T 晚上 21:00 之后的K线属于下一个交易日，不计入。周末的夜盘顺延到周一。
    """
    if df is None or len(df) == 0:
        return df
    out = df
    days = _trading_day_index(out.index)
    if start not in (None, ""):
        start_day = pd.Timestamp(futures_trading_day(pd.Timestamp(start).to_pydatetime()))
        out = out[days >= start_day]
        days = days[days >= start_day]
    if end not in (None, ""):
        end_day = pd.Timestamp(futures_trading_day(pd.Timestamp(end).to_pydatetime()))
        out = out[days <= end_day]
    return out


def skip_reason_key(reason: str) -> str:
    """把没开成的原因归成稳定的类别，同一时段同一类只记一次。"""
    text = (reason or "未开仓").strip()
    if "止损过宽" in text or "低于最小" in text or "只允许约" in text:
        return "止损过宽，手数不够"
    if "熔断" in text or "当日亏损" in text:
        return "当日亏损达到上限"
    if "开仓次数" in text:
        return "当天开仓次数达到上限"
    if "间隔" in text:
        return "两笔开仓间隔太近"
    if "仓位上限" in text or "保证金" in text:
        return "仓位上限/保证金不够"
    return text[:24] or "未开仓"


class BacktestLimits:
    """
    回测用的开仓限制，算法和实盘相同。

    实盘的下单间隔用的是电脑时钟。回测一根根K线瞬间就算完，如果也用电脑时钟，
    第二笔就会被当成「刚刚才下过单」而全部拒绝。所以这里改用K线自己的时间。
    换日也按K线所属的期货交易日清零：夜盘 21:00 就是下一交易日，不是等到零点。
    这样和实盘一致，夜盘跨过零点也不会把当日计数清掉。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        from src.risk.guardian import RiskGuardian
        from src.risk.sizing import PositionSizer

        self.sizer = PositionSizer(cfg)
        self.guard = RiskGuardian.from_config(cfg)
        # 回测里日亏熔断只拦这一次回测的开仓，不要写进运行台的「最近告警」。
        self.guard.write_alerts = False
        # 关掉时固定 1 手，不再因为止损远、日亏损或开仓次数把信号丢掉。
        from src.strategy.types import config_flag

        self.apply = config_flag((cfg.get("risk") or {}).get("apply_limits", True), default=True)
        self.day: Any = None
        self.last_open_at: datetime | None = None
        self.skipped: dict[str, int] = {}
        self.closed_pnls: list[float] = []
        self.closed_ats: list[datetime] = []
        # 账户权益最高点，给回撤减仓用。开跑时用初始资金垫上。
        self.equity: float = 0.0
        self.peak_equity: float = 0.0
        self.cfg = cfg

    def note_equity(self, equity: float) -> None:
        """记下当前权益；创新高就抬峰值。回撤按峰值往下量。"""
        now_eq = float(equity)
        self.equity = now_eq
        if now_eq > self.peak_equity:
            self.peak_equity = now_eq

    def on_bar(self, now: datetime) -> None:
        """新交易日开始时，当日亏损和开仓次数清零，熔断解除。持仓不动。"""
        day = futures_trading_day(now)
        if self.day == day:
            return
        self.day = day
        self.guard.daily_pnl = 0.0
        self.guard.orders_today = 0
        self.guard.open_orders_today = 0
        self.guard.halted = False
        self.guard.halt_reason = ""

    def size_open(
        self,
        symbol: str,
        entry: float | None,
        stop: float | None,
    ) -> tuple[int, str]:
        """按权益、每笔风险和止损距离算手数。算不出 1 手就返回 0。限制关掉时固定 1 手。"""
        if not self.apply:
            return 1, "未启用开仓限制，固定1手"
        sized = self.sizer.calc_open(
            symbol,
            entry,
            stop,
            current_abs_position=self.guard.total_abs_position(),
            positions=self.guard.positions,
        )
        return int(sized.volume), sized.reason

    def allow_open(self, now: datetime, volume: int) -> tuple[bool, str]:
        """日亏损、开仓次数、两笔之间的间隔。平仓不走这里。限制关掉时直接放行。"""
        if not self.apply:
            return True, "未启用开仓限制"
        if volume <= 0:
            return False, "手数必须大于 0"
        if self.guard.halted:
            return False, f"已熔断: {self.guard.halt_reason}"
        if self.guard.open_orders_today >= self.guard.max_orders_per_day:
            return False, f"今日开仓次数已达上限 {self.guard.max_orders_per_day}"
        if self.last_open_at is not None:
            gap = (now - self.last_open_at).total_seconds()
            need = self.guard.min_order_interval_sec
            if gap < need:
                return False, f"开仓间隔不足 {need} 秒"
        return True, "通过"

    def note_skip(self, reason: str) -> None:
        """记下没开成的原因，回测结果里汇总给用户看。"""
        key = skip_reason_key(reason)
        self.skipped[key] = self.skipped.get(key, 0) + 1

    def on_open(self, now: datetime, side: str, volume: int, symbol: str) -> None:
        self.last_open_at = now
        self.guard.on_order_filled(side, volume, symbol=symbol)

    def on_close(self, pnl: float, volume: int, symbol: str, at: datetime | None = None) -> None:
        self.guard.update_pnl(pnl)
        self.guard.on_order_filled("close", volume, symbol=symbol)
        self.closed_pnls.append(float(pnl))
        self.closed_ats.append(at if at is not None else datetime.now())


def _cap_open_volume(allowed: int, meta_volume: Any, *, limits_apply: bool) -> tuple[int, str]:
    """
    策略指定手数只能往下压，不能超过资金管理允许的手数。

    关掉开仓限制（apply_limits=false）时才按策略手数，方便做「不限仓」对比。
    """
    if meta_volume is None:
        return int(allowed), ""
    try:
        want = max(0, int(meta_volume))
    except (TypeError, ValueError):
        return int(allowed), "策略手数无效，按风控"
    if want <= 0:
        return 0, f"策略指定{want}手"
    if not limits_apply:
        return want, f"策略指定{want}手（未启用开仓限制）"
    vol = min(int(allowed), want)
    return vol, f"策略指定{want}手，风控允许{allowed}手 → 取小{vol}手"


def _apply_size_scale(volume: int, meta_scale: Any) -> tuple[int, str]:
    """混合版多因子按信号强弱把手数再打一折，不能超过已经算好的风控手数。"""
    if meta_scale is None or int(volume) <= 0:
        return int(volume), ""
    try:
        ratio = float(meta_scale)
    except (TypeError, ValueError):
        return int(volume), ""
    if ratio <= 0:
        return 0, f"信号强度×{ratio:.2f}→0手"
    if ratio >= 0.999:
        return int(volume), ""
    scaled = max(1, int(int(volume) * ratio))
    vol = min(int(volume), scaled)
    return vol, f"信号强度×{ratio:.2f}→{vol}手"


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
    """
    cfg = _deepcopy_cfg(cfg)
    st = cfg.setdefault("strategy", {})
    trade = cfg.setdefault("trade", {})
    risk = cfg.setdefault("risk", {})

    if engine:
        st["engine"] = str(engine)

    symbol = symbol or (st.get("symbols") or [trade.get("symbol", "bu2612")])[0]
    st["symbols"] = [symbol]
    trade["symbol"] = symbol

    # 默认跟实盘同一套：按权益和止损距离算手数。只有调用方明确传入 volume 时才改成固定手数。
    if volume is not None:
        trade["volume"] = int(volume)
        risk["sizing_mode"] = "fixed"
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

    if bars_signal is None:
        bars_signal = provider.fetch(
            symbol,
            period=tf,
            max_bars=max_bars,
            keep_history=True,
            start=start_date,
            end=end_date,
        )
    if bars_trail is None and trail_tf != tf:
        try:
            bars_trail = provider.fetch(
                symbol,
                period=trail_tf,
                max_bars=max_bars,
                keep_history=True,
                start=start_date,
                end=end_date,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("拉取止损周期失败，回测改用信号周期: {}", e)
            bars_trail = None
    bars_trend = None
    if trend_tf and trend_tf != canonical_period(tf):
        try:
            bars_trend = provider.fetch(
                symbol,
                period=trend_tf,
                max_bars=max_bars,
                keep_history=True,
                start=start_date,
                end=end_date,
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
    bars_signal = filter_bars_by_range(bars_signal, start_date, end_date)
    bars_trail = filter_bars_by_range(bars_trail, start_date, end_date)
    # 大周期均线要往前看：开始日期只截信号 K 线，日线仍保留更早的收盘。
    if bars_trend is not None:
        bars_trend = filter_bars_by_range(bars_trend, None, end_date)
    after = 0 if bars_signal is None else len(bars_signal)
    if before and after < before:
        logger.info("按时间段截取K线: {} → {} 根 ({} ~ {})", before, after, start_date or "起", end_date or "止")

    if bars_signal is None or len(bars_signal) < warmup + 5:
        raise RuntimeError(
            f"K线不足，无法回测 symbol={symbol} bars={0 if bars_signal is None else len(bars_signal)}。"
            "可把时间段改成「全部可用」，或检查网络后重试。"
        )

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
    # 确保空仓起点
    strategy.positions[symbol] = PositionState(symbol=symbol)
    if hasattr(strategy, "_last_entry_bar"):
        strategy._last_entry_bar[symbol] = ""
    # 通用止损：和策略自身止损相互独立，谁先触发就按谁平。
    generic_stop = GenericStopOverlay(cfg)

    trades: list[TradeRecord] = []
    equity = [capital0]
    cash = capital0
    open_meta: dict[str, Any] = {}
    limits = BacktestLimits(cfg)
    limits.note_equity(capital0)
    contracts = (cfg.get("risk") or {}).get("contracts") or {}
    # 有开仓信号但没开成的记录。成交明细只写真正成交的，这里把「为什么没做」也留下来。
    rejected: list[RejectedSignal] = []
    # 同一交易时段里，同一层、同方向、同一类原因只留第一条，避免每根K线都重复一遍。
    seen_rejects: set[tuple[str, str, str, str, str]] = set()

    def _note_reject(
        stage: str,
        reason: str,
        side: str,
        *,
        at: datetime,
        price: float | None,
        stop: float | None,
        key: str = "",
    ) -> None:
        session = f"{futures_trading_day(at)}|{session_kind(at)}"
        template = (key or "").strip() or (reason or "未开仓").strip()
        dedupe = (session, stage, side, template)
        if dedupe in seen_rejects:
            return
        seen_rejects.add(dedupe)
        rejected.append(
            RejectedSignal(
                symbol=symbol,
                time=at.isoformat(timespec="seconds"),
                side=side,
                price=float(price) if price is not None and float(price) > 0 else None,
                stop=float(stop) if stop is not None and float(stop) > 0 else None,
                stage=stage,
                reason=(reason or "未开仓").strip(),
            )
        )

    n = len(bars_signal)
    # 回测循环里策略会狂打 info，先关掉这几个模块日志，结束再打开
    for _mod in (
        "src.strategy.trend_follow",
        "src.strategy.supertrend",
        "src.strategy.dual_ma",
        "src.strategy.bollinger_break",
        "src.strategy.sar_trend",
        "src.strategy.multi_factor",
        "src.strategy.multi_factor_adapt",
        "src.strategy.adaptive_weights",
        "src.strategy.daily_body",
        "src.strategy.morning_edge",
        "src.strategy.swing_trend",
        "src.risk.sizing",
        "src.risk.guardian",
        "src.risk.dynamic",
    ):
        logger.disable(_mod)

    try:
        for i in range(warmup, n):
            # 只用第 i 根（含）已收盘的数据做决策；开仓按下一根开盘成交（可配置关闭）。
            signal_df = bars_signal.iloc[: i + 1]
            now = _ts_to_dt(signal_df.index[-1])
            trail_df = None
            if bars_trail is not None and len(bars_trail) > 0:
                trail_df = bars_trail[bars_trail.index <= signal_df.index[-1]]
            trend_df = _trend_frame_for_signal(bars_trend, signal_df.index[-1], trend_tf)

            if getattr(strategy, "uses_trend_bars", False):
                decision = strategy.evaluate(
                    symbol, signal_df, now=now, trail_bars=trail_df, trend_bars=trend_df
                )
            else:
                decision = strategy.evaluate(symbol, signal_df, now=now, trail_bars=trail_df)
            px = last_ohlc_price(signal_df, "close")
            if px is None:
                continue
            # 先按现金+浮动权益更新峰值，再决定这一根能不能开，回撤才跟得上浮亏。
            _pos_eq = strategy.positions.get(symbol)
            _upnl = 0.0
            if _pos_eq and _pos_eq.side == "long" and _pos_eq.volume:
                _upnl = (px - float(_pos_eq.entry_price or 0)) * int(_pos_eq.volume) * mult
            elif _pos_eq and _pos_eq.side == "short" and _pos_eq.volume:
                _upnl = (float(_pos_eq.entry_price or 0) - px) * int(_pos_eq.volume) * mult
            limits.note_equity(cash + _upnl)
            # 通用止损：策略自身止损之外的公共止损，策略没让平仓时由它补一刀。
            held_pos = strategy.positions.get(symbol)
            gs_bar = None
            if (
                decision.action == StrategyAction.HOLD
                and held_pos is not None
                and held_pos.side != "flat"
            ):
                gs_hit = generic_stop.check(
                    symbol,
                    held_pos,
                    trail_df if trail_df is not None and len(trail_df) > 0 else signal_df,
                    mode=getattr(strategy, "stop_mode", "touch"),
                )
                if gs_hit is not None:
                    # 记住触发的那根止损周期K线，成交按它判跳空，而不是按信号K线。
                    gs_bar = gs_hit.bar
                    decision = StrategyDecision(
                        StrategyAction.CLOSE,
                        symbol,
                        gs_hit.reason,
                        stop_price=gs_hit.stop,
                        # 标一下：这是通用止损打掉的，不是策略自己让平。
                        # 成交价要按止损线算，不能退回下一根开盘。
                        meta={"generic_stop": True},
                    )
            # 下一根开盘价用来成交；断开或已到最后一根时退回当根收盘价。
            next_open = _next_open_price(bars_signal, i, tf) if fill.on_next_open else None
            limits.on_bar(now)
            meta = decision.meta or {}
            scale = str(meta.get("scale") or "")

            if decision.action == StrategyAction.HOLD and meta.get("blocked"):
                # 策略已经看到开仓信号，但被自己的一条过滤规则挡回去了
                # （箱体太宽、开盘跳空太大、急破不追等）。没成交也要记下来。
                _note_reject(
                    str(meta.get("block_stage") or "策略过滤"),
                    decision.reason,
                    "多" if str(meta.get("side") or "") == "long" else "空",
                    at=now,
                    price=px,
                    stop=decision.stop_price,
                    key=str(meta.get("block_key") or ""),
                )

            if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
                pos = strategy.positions.get(symbol)
                want = "long" if decision.action == StrategyAction.BUY_OPEN else "short"
                # 同一根先平掉相反仓，再开新方向。没有 reverse 标记的策略行为不变。
                if pos and pos.side not in ("flat", want) and meta.get("reverse"):
                    held = pos.side
                    pos_stop = _position_stop(pos, decision.stop_price)
                    exit_px = _exit_fill_price(
                        side=held,
                        stop=pos_stop,
                        bar=signal_df,
                        next_open=next_open,
                        close_px=px,
                        fill=fill,
                        touch_stop=bool(getattr(strategy, "stop_on_touch", True)),
                    )
                    entry_px = float(pos.entry_price or open_meta.get("entry_price") or exit_px)
                    vol_now = int(pos.volume or 0)
                    gross = (
                        (exit_px - entry_px) * vol_now * mult
                        if held == "long"
                        else (entry_px - exit_px) * vol_now * mult
                    )
                    pnl, fee = _net_pnl_after_fees(
                        contracts,
                        symbol,
                        gross=gross,
                        volume=vol_now,
                        held=vol_now,
                        open_meta=open_meta,
                        exit_time=now.isoformat(timespec="seconds"),
                    )
                    cash += pnl
                    trades.append(
                        _make_trade_record(
                            symbol=symbol,
                            side=held,
                            entry_time=str(open_meta.get("entry_time", "")),
                            exit_time=now.isoformat(timespec="seconds"),
                            entry_price=entry_px,
                            exit_price=exit_px,
                            volume=vol_now,
                            pnl=pnl,
                            reason_entry=str(open_meta.get("reason", "")),
                            reason_exit=decision.reason,
                            commission=fee,
                            open_meta=open_meta,
                            multiplier=mult,
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
                    limits.on_close(pnl, vol_now, symbol, at=now)
                    limits.note_equity(cash)
                    open_meta = {}
                    pos = strategy.positions.get(symbol)
                # 同向加仓：已有仓且 meta.scale=add 时允许继续开；其它情况有仓就跳过。
                is_add = bool(pos and pos.side == want and scale == "add")
                if pos and pos.side != "flat" and not is_add:
                    held = "多" if pos.side == "long" else "空"
                    _note_reject(
                        "已有持仓",
                        f"手里还拿着{held}单，这根的同向开仓不再重复做",
                        "多" if want == "long" else "空",
                        at=now,
                        price=px,
                        stop=decision.stop_price,
                        key="已有持仓",
                    )
                    continue
                # 成交口径统一：开了「下一根开盘」就按下一根开盘（跨休市也一样），
                # 只有最后一根没有下一根时才按当根收盘成交。
                # 不能再用策略内部价顶替，否则跳空时会报出当天根本没成交过的开仓价。
                if fill.on_next_open and next_open is not None:
                    entry_ref = next_open
                    entry_ts = bars_signal.index[i + 1]
                else:
                    entry_ref = px
                    entry_ts = bars_signal.index[i]
                # 开多=买入，开空=卖出，都要让滑点。
                entry = (
                    fill.buy(float(entry_ref))
                    if decision.action == StrategyAction.BUY_OPEN
                    else fill.sell(float(entry_ref))
                )
                stop = decision.stop_price
                # 先按权益/风险算出允许手数；策略指定的单元手数只能往下压，不能绕过。
                allowed_vol, allowed_reason = limits.size_open(symbol, entry, stop)
                open_vol, cap_note = _cap_open_volume(
                    allowed_vol, meta.get("volume"), limits_apply=bool(limits.apply)
                )
                open_vol, scale_note = _apply_size_scale(open_vol, meta.get("size_scale"))
                from src.risk.dynamic import apply_dynamic_open

                open_vol, dyn_note = apply_dynamic_open(
                    limits.cfg,
                    open_vol,
                    signal_df,
                    equity=limits.equity,
                    peak=limits.peak_equity,
                )
                notes = [item for item in (cap_note, scale_note, dyn_note) if item]
                if notes:
                    joined = "；".join(notes)
                    size_reason = f"{joined}（{allowed_reason}）" if allowed_reason else joined
                else:
                    size_reason = allowed_reason
                if open_vol <= 0:
                    limits.note_skip(size_reason)
                    _note_reject(
                        "风控手数",
                        size_reason,
                        "多" if want == "long" else "空",
                        at=now,
                        price=entry,
                        stop=stop,
                        key=skip_reason_key(size_reason),
                    )
                    continue
                allowed, block_reason = limits.allow_open(now, open_vol)
                if not allowed:
                    limits.note_skip(block_reason)
                    _note_reject(
                        "开仓限制",
                        block_reason,
                        "多" if want == "long" else "空",
                        at=now,
                        price=entry,
                        stop=stop,
                        key=skip_reason_key(block_reason),
                    )
                    continue
                strategy.on_filled(
                    symbol,
                    decision.action,
                    entry,
                    open_vol,
                    initial_stop=stop,
                )
                limits.on_open(now, decision.action.value, open_vol, symbol)
                pos_after = strategy.positions.get(symbol)
                # 有些策略（日内实体、早盘箱体）开仓时用真实时钟记 opened_at；
                # 回测里必须改成这根K线的模拟时间，否则通用止损会把这些历史K线
                # 全当成「开仓之前」，整段回测都不会生效。
                if pos_after is not None and pos_after.side != "flat" and not is_add:
                    pos_after.opened_at = now.isoformat(timespec="seconds")
                open_fee = fee_open(contracts, symbol, open_vol)
                if is_add and open_meta:
                    # 加仓后按真实成交价重算均价；不再用策略内部的信号价。
                    prev_vol = int(open_meta.get("open_volume") or 0)
                    prev_avg = float(open_meta.get("entry_price") or entry)
                    total_vol = prev_vol + open_vol
                    open_meta["entry_price"] = (
                        (prev_avg * prev_vol + entry * open_vol) / total_vol if total_vol else entry
                    )
                    open_meta["reason"] = decision.reason
                    open_meta["open_fee"] = float(open_meta.get("open_fee") or 0) + open_fee
                    open_meta["open_volume"] = total_vol
                else:
                    open_meta = {
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
                    }
                if hasattr(strategy, "_last_entry_bar"):
                    bar_id = str(meta.get("bar_id", ""))
                    if bar_id:
                        strategy._last_entry_bar[symbol] = bar_id

            elif decision.action == StrategyAction.CLOSE:
                pos = strategy.positions.get(symbol)
                if not pos or pos.side == "flat":
                    continue
                side = pos.side
                pos_stop = _position_stop(pos, decision.stop_price)
                # 通用止损有明确的止损线，按这条线成交；策略自己平仓才用持仓的尾随止损。
                if meta.get("generic_stop") and decision.stop_price:
                    pos_stop = float(decision.stop_price)
                # 触发它的那根止损周期K线（比如 15 分钟）决定跳空多少，不能用信号K线代判。
                fill_bar = signal_df if gs_bar is None else gs_bar.to_frame().T
                exit_px = _exit_fill_price(
                    side=side,
                    stop=pos_stop,
                    bar=fill_bar,
                    next_open=next_open,
                    close_px=px,
                    fill=fill,
                    touch_stop=bool(getattr(strategy, "stop_on_touch", True)),
                )
                entry = float(pos.entry_price or open_meta.get("entry_price") or exit_px)
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
                    contracts,
                    symbol,
                    gross=gross,
                    volume=vol,
                    held=held,
                    open_meta=open_meta,
                    exit_time=exit_time,
                )
                cash += pnl
                trades.append(
                    _make_trade_record(
                        symbol=symbol,
                        side=side,
                        entry_time=str(open_meta.get("entry_time", "")),
                        exit_time=exit_time,
                        entry_price=entry,
                        exit_price=exit_px,
                        volume=vol,
                        pnl=pnl,
                        reason_entry=str(open_meta.get("reason", "")),
                        reason_exit=decision.reason,
                        commission=fee,
                        open_meta=open_meta,
                        multiplier=mult,
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
                limits.on_close(pnl, vol, symbol, at=now)
                limits.note_equity(cash)
                pos_left = strategy.positions.get(symbol)
                if not pos_left or pos_left.side == "flat" or int(pos_left.volume or 0) <= 0:
                    open_meta = {}
                else:
                    # 部分减仓后，剩余仓仍用均价记账。
                    open_meta["entry_price"] = float(pos_left.entry_price or entry)

            # 浮动权益：现金 + 未实现盈亏
            pos = strategy.positions.get(symbol)
            upnl = 0.0
            if pos and pos.side == "long" and pos.volume:
                upnl = (px - pos.entry_price) * pos.volume * mult
            elif pos and pos.side == "short" and pos.volume:
                upnl = (pos.entry_price - px) * pos.volume * mult
            equity.append(cash + upnl)
    finally:
        for _mod in (
            "src.strategy.trend_follow",
            "src.strategy.supertrend",
            "src.strategy.dual_ma",
            "src.strategy.bollinger_break",
            "src.strategy.sar_trend",
            "src.strategy.multi_factor",
            "src.strategy.multi_factor_adapt",
            "src.strategy.adaptive_weights",
            "src.strategy.daily_body",
            "src.strategy.morning_edge",
            "src.strategy.swing_trend",
            "src.risk.sizing",
            "src.risk.guardian",
        ):
            logger.enable(_mod)

    # 若回测结束仍持仓，按最后收盘强平，便于统计完整
    pos = strategy.positions.get(symbol)
    if pos and pos.side != "flat" and pos.volume:
        px = float(bars_signal["close"].iloc[-1])
        now = _ts_to_dt(bars_signal.index[-1])
        # 用实际成交价算盈亏；策略里的 entry_price 可能是信号价，和真实成交不一致。
        entry = float(open_meta.get("entry_price") or pos.entry_price)
        side = pos.side
        vol = int(pos.volume)
        # 强平同样按卖出/买入方向让滑点，避免把最后一笔算得过于理想。
        exit_px = fill.sell(px) if side == "long" else fill.buy(px)
        gross = (exit_px - entry) * vol * mult if side == "long" else (entry - exit_px) * vol * mult
        exit_time = now.isoformat(timespec="seconds")
        pnl, fee = _net_pnl_after_fees(
            contracts,
            symbol,
            gross=gross,
            volume=vol,
            held=vol,
            open_meta=open_meta,
            exit_time=exit_time,
        )
        cash += pnl
        trades.append(
            _make_trade_record(
                symbol=symbol,
                side=side,
                entry_time=str(open_meta.get("entry_time", "")),
                exit_time=exit_time,
                entry_price=entry,
                exit_price=exit_px,
                volume=vol,
                pnl=pnl,
                reason_entry=str(open_meta.get("reason", "")),
                reason_exit="回测结束强平",
                commission=fee,
                open_meta=open_meta,
                multiplier=mult,
            )
        )
        equity.append(cash)

    # 样本过少时胜率没有统计意义；门槛随实际成交略放宽
    min_trades = 3 if len(trades) >= 3 else 2
    perf = calc_performance(trades, equity, min_trades=min_trades)
    snap = {
        "engine": st.get("engine"),
        # 记下这一次用的行情来源。送到交叉测试时，下拉框可以跟着变成同一家。
        "data_source": (cfg.get("data") or {}).get("source") or "akshare",
        "flatten_at_close": bool(getattr(strategy, "flatten_at_close", True)),
        "timeframe": tf,
        "trail_timeframe": trail_tf,
        "max_bars": max_bars,
        "warmup": warmup,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "available_from": available_from,
        "available_to": available_to,
        "data_from": str(bars_signal.index[0]) if len(bars_signal) else "",
        "data_to": str(bars_signal.index[-1]) if len(bars_signal) else "",
        "coverage_note": _coverage_with_gaps(
            coverage_note(
                start_date,
                end_date,
                available_from,
                available_to,
                str(bars_signal.index[0]) if len(bars_signal) else "",
                str(bars_signal.index[-1]) if len(bars_signal) else "",
                tf,
            ),
            bars_signal,
            tf,
            bars_trail,
            trail_tf,
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
        "sizing_mode": str(risk.get("sizing_mode", "risk_pct")),
        "capital": capital0,
        "risk_per_trade_pct": float(risk.get("risk_per_trade_pct", 0.5)),
        "margin_usage_pct": float(risk.get("margin_usage_pct", 80) or 0),
        "fixed_volume": int(trade.get("volume", 1)),
        "min_volume": int(risk.get("min_volume", 1)),
        "max_daily_loss": float(risk.get("max_daily_loss", 1000)),
        "min_order_interval_sec": int(risk.get("min_order_interval_sec", 60)),
        "max_orders_per_day": int(risk.get("max_orders_per_day", 20)),
        "margin_per_lot": float(risk.get("margin_per_lot", 0) or 0),
        # 这一笔仓最多要交多少保证金。收益率用它做分母，不拿来改手数。
        "margin_used": peak_margin(trades, mult, MARGIN_RATE),
        "margin_rate": MARGIN_RATE,
        "skipped_opens": dict(limits.skipped),
    }
    logger.info(
        "回测结束 {} | bars={} 成交{}笔 胜率{:.0%} 净盈亏{:.0f}",
        symbol,
        n,
        perf.trades,
        perf.win_rate,
        perf.net_pnl,
    )
    return BacktestResult(
        symbol=symbol,
        timeframe=tf,
        trail_timeframe=trail_tf,
        bars=n,
        performance=perf,
        trades=trades,
        equity=equity,
        params_snapshot=snap,
        rejected=rejected,
        # 记到秒。同一次回测的摘要和每一笔成交共用这个时间，再导出也不会变。
        ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def save_backtest_report(result: BacktestResult, out_path: Path | str) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
