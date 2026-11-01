"""
日内实体。

功能：方向看「当前信号周期」K 线。ETH 走完之后，收盘离开开盘区间才开仓。
     站上去做多，跌下去做空，还在区间里就等。
     5/15/30/60 同一套标准；日线、周线没有开盘 ETH，这套不判断。
     日盘、夜盘各自有个开仓截止钟点（默认 10:30 / 22:15），过了就不再追。13:30 不重新画区间。
     止损只认开盘箱体：多单守箱底外一跳，空单守箱顶外一跳；开仓后冻结。
     价格打穿箱体对沿就平仓（算方向作废）；否则顺着捅破方向拿到收盘。
     日盘最多开两次：第一段 ETH（默认 20 分钟）开仓打到止损后、截止钟点以前，
     用第二段 ETH（默认 30 分钟，可改，填 0 关闭）的箱体再开一次；第二次止损后不再开。
     夜盘仍只开一次。
     没有盈利回撤止损，也不用 ATR 另算一道。
原理：开盘后的 ETH 是噪音。用信号周期画框和判断有没有走出框。
     框画完之前不开。影线刺出框、收盘又回到框里，也不开。夜盘一段只开一次；日盘最多两次。
     离场动作尽量少：箱沿保底，或收盘清仓。
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import canonical_period, period_label, resample_ohlc

from .session_scope import scope_allows, scope_block_reason, scope_from_cfg
from .types import PositionState, StrategyAction, StrategyDecision, config_flag


def _signal_minutes(timeframe: str) -> int:
    """信号周期有几分钟。日线、周线返回 0。"""
    period = canonical_period(str(timeframe))
    if period in {"D", "W"} or not period.isdigit():
        return 0
    return int(period)


# 日盘 9:00、夜盘 21:00 开始画开盘区间。ETH 走完才能开仓。
# 开仓截止钟点的出厂值：日盘 10:30，夜盘 22:15。过了就不再追，配置页可以改。
_DAY_OPEN = dtime(9, 0)
_NIGHT_OPEN = dtime(21, 0)
DAY_CUTOFF_DEFAULT = dtime(10, 30)
NIGHT_CUTOFF_DEFAULT = dtime(22, 15)


def box_edge_stop(*, side: str, box_low: float, box_high: float, tick: float) -> float:
    """
    开仓时按开盘箱体对沿算止损，再让出若干跳。

    多单：箱体最低 − 跳数（打破箱底才算方向作废）。
    空单：箱体最高 + 跳数（打破箱顶才算方向作废）。
    """
    step = max(0.0, float(tick))
    if side == "long":
        return float(box_low) - step
    if side == "short":
        return float(box_high) + step
    return float(box_low)


def _parse_hhmm(value: str) -> dtime:
    hour, minute = str(value).split(":")
    return dtime(int(hour), int(minute))


def _cutoff(value: Any, fallback: dtime) -> dtime:
    """开仓截止钟点。空着或写错就用出厂值。"""
    try:
        return _parse_hhmm(str(value))
    except (TypeError, ValueError):
        return fallback


def _naive(moment: datetime) -> datetime:
    """去掉时区，方便和 K 线起点比较。"""
    stamp = pd.Timestamp(moment)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.to_pydatetime().replace(microsecond=0)


def _with_clock(bars: pd.DataFrame) -> pd.DataFrame:
    """把 K 线时间收成不带时区的时钟，后面按 9:00、9:30 分段才不会对错。"""
    out = bars.copy()
    index = pd.DatetimeIndex(pd.to_datetime(out.index))
    if index.tz is not None:
        index = index.tz_localize(None)
    out.index = index
    return out


def opening_box_levels(
    bars: pd.DataFrame,
    now: datetime,
    session_open: dtime,
    eth_minutes: int,
    timeframe: str,
) -> tuple[float | None, float | None, str]:
    """
    算出当前信号周期下的开盘箱体最高、最低。

    开盘区间是 ETH 里已经走完的信号 K 线最高价和最低价。
    画不出来时返回 (None, None, 原因)。
    """
    tag = period_label(str(timeframe))
    minutes = _signal_minutes(timeframe)
    view = _signal_view_bars(bars, now, timeframe)
    if view is None or minutes <= 0:
        return None, None, f"日内实体只看分钟线。当前周期 {tag} 没有开盘ETH区间，先不开"
    moment = pd.Timestamp(_naive(now))
    if view.empty or "close" not in view.columns:
        return None, None, f"没有走完的{tag}K线，先看"
    day = moment.normalize()
    opened = day + pd.Timedelta(hours=session_open.hour, minutes=session_open.minute)
    eth_end = opened + pd.Timedelta(minutes=max(0, int(eth_minutes)))
    boxed = view.loc[(view.index > opened) & (view.index <= eth_end)]
    if boxed.empty or not {"high", "low"}.issubset(boxed.columns):
        return None, None, f"ETH里的{tag}K线不够，开盘区间还没画出来，先等"
    box_high = float(pd.to_numeric(boxed["high"], errors="coerce").max())
    box_low = float(pd.to_numeric(boxed["low"], errors="coerce").min())
    if pd.isna(box_high) or pd.isna(box_low) or box_high <= box_low:
        return None, None, "开盘区间看不清，先等"
    return box_high, box_low, ""


def judge_opening_break(
    bars: pd.DataFrame,
    now: datetime,
    session_open: dtime,
    eth_minutes: int,
    timeframe: str,
) -> tuple[str, str]:
    """
    用已经走完的信号周期 K 线，看收盘有没有离开开盘区间。

    返回 long / short / none。none 表示先等，调用方不要把这段记成已经开过。
    只在信号周期刚好收完的这一刻定方向；中间时刻不拿上一根再开一次。
    收盘还在框里，或只是影线刺出去，也不开。
    """
    tag = period_label(str(timeframe))
    minutes = _signal_minutes(timeframe)
    view = _signal_view_bars(bars, now, timeframe)
    if view is None or minutes <= 0:
        return "none", f"日内实体只看分钟线。当前周期 {tag} 没有开盘ETH区间，先不开"
    moment = pd.Timestamp(_naive(now))
    if view.empty or "close" not in view.columns:
        return "none", f"没有走完的{tag}K线，先看"
    # 只在信号周期刚好走完的这一分钟定方向。中间时刻不拿刚才那根再开一次。
    last_ts = view.index[-1]
    if last_ts != moment:
        return "none", f"{tag}还没走完，先等"
    box_high, box_low, wait = opening_box_levels(bars, now, session_open, eth_minutes, timeframe)
    if box_high is None or box_low is None:
        return "none", wait
    day = moment.normalize()
    opened = day + pd.Timedelta(hours=session_open.hour, minutes=session_open.minute)
    eth_end = opened + pd.Timedelta(minutes=max(0, int(eth_minutes)))
    bar_start = last_ts - pd.Timedelta(minutes=minutes)
    if bar_start < eth_end:
        return "none", f"这根{tag}还有一段落在ETH里，等下一根完全走在ETH后面"
    last_close = float(pd.to_numeric(view["close"], errors="coerce").iloc[-1])
    if last_close > box_high:
        return "long", (
            f"{tag}收盘站上开盘区间。区间 {box_low:.2f} 到 {box_high:.2f}，收盘 {last_close:.2f}"
        )
    if last_close < box_low:
        return "short", (
            f"{tag}收盘跌破开盘区间。区间 {box_low:.2f} 到 {box_high:.2f}，收盘 {last_close:.2f}"
        )
    return "none", f"{tag}收盘还在开盘区间 {box_low:.2f} 到 {box_high:.2f} 里面，先等"


def _signal_view_bars(bars: pd.DataFrame, now: datetime, timeframe: str) -> pd.DataFrame | None:
    """
    拿出已经走完的信号周期 K 线。

    信号是多少分钟就用多少分钟；传入更细的 K 时先收成信号周期。
    日线、周线返回空（没有开盘后的 ETH 区间这套用法）。
    """
    minutes = _signal_minutes(timeframe)
    if minutes <= 0:
        return None
    frame = _with_clock(bars)
    moment = pd.Timestamp(_naive(now))
    frame = frame.loc[frame.index <= moment]
    if frame.empty:
        return frame
    period = str(minutes)
    if len(frame) >= 2:
        delta_m = (frame.index[-1] - frame.index[-2]).total_seconds() / 60.0
        if delta_m > 0 and delta_m < minutes * 0.6:
            view = resample_ohlc(frame, period)
        else:
            view = frame
    else:
        view = frame
    if view is None or len(view) == 0:
        return view
    return view.loc[view.index <= moment]


class DailyBodyStrategy:
    """方向看信号周期。ETH 走完后，收盘离开开盘区间才开仓。"""

    # 方向看信号周期均线/收盘，不再另外要日线。
    uses_trend_bars = False
    trend_timeframe = "15"

    def __init__(self, cfg: dict[str, Any]) -> None:
        st = cfg.get("strategy", {}) or {}
        trade = cfg.get("trade", {}) or {}
        self.timeframe = str(st.get("timeframe", "5"))
        self.trail_timeframe = str(st.get("trail_timeframe", self.timeframe))
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        # 日线字段留着，只为和别的策略调用方式对齐；本策略不算日线方向。
        self.trend_lookback = 10
        self.trend_timeframe = "D"
        body = st.get("daily_body") or {}
        # 箱沿止损再让几跳；默认 1 个最小跳动。
        try:
            self.stop_extra_ticks = max(0, int(body.get("stop_extra_ticks", 1) or 0))
        except (TypeError, ValueError):
            self.stop_extra_ticks = 1
        # 开盘跳空超过多少跳就不开这段。0=关闭。跳空太大时箱沿止损会被缺口一次打穿。
        try:
            raw_gap = body.get("skip_gap_ticks", 50)
            self.skip_gap_ticks = max(0, int(0 if raw_gap in (None, "") else raw_gap))
        except (TypeError, ValueError):
            self.skip_gap_ticks = 50
        # 开盘箱体最宽多少跳。超过就不开：箱体越高低差越大，箱沿止损离进场价就越远。0=关闭。
        try:
            raw_box = body.get("max_box_ticks", 100)
            self.max_box_ticks = max(0, int(0 if raw_box in (None, "") else raw_box))
        except (TypeError, ValueError):
            self.max_box_ticks = 100
        # 开仓截止钟点：过了这个钟点，这一段不再开新仓。配置页可以分开设日盘和夜盘。
        self.day_cutoff = _cutoff(body.get("day_cutoff"), DAY_CUTOFF_DEFAULT)
        self.night_cutoff = _cutoff(body.get("night_cutoff"), NIGHT_CUTOFF_DEFAULT)
        # 公共设置：全时段 / 只做日盘 / 只做夜盘。只拦开新仓，持仓照旧。
        self.session_scope = scope_from_cfg(cfg)
        ctp = trade.get("ctp", {}) if isinstance(trade.get("ctp"), dict) else {}
        tick_raw = ctp.get("price_tick", trade.get("price_tick", 1))
        try:
            self.price_tick = float(tick_raw)
        except (TypeError, ValueError):
            self.price_tick = 1.0
        if self.price_tick <= 0:
            self.price_tick = 1.0
        # ETH 观察几分钟：优先读 daily_body.eth_minutes；旧配置若还写在 liquidity_exhaustion 里也能读到。
        raw_eth = body.get("eth_minutes")
        if raw_eth is None:
            raw_eth = (st.get("liquidity_exhaustion") or {}).get("eth_minutes", 20)
        try:
            self.eth_minutes = max(0, int(raw_eth))
        except (TypeError, ValueError):
            self.eth_minutes = 20
        # 第二段 ETH：第一段止损后、截止前，用这段更长的箱体再开一次（仅日盘）。0=关闭二次开仓。
        try:
            self.eth_minutes2 = max(0, int(body.get("eth_minutes2", 30) or 0))
        except (TypeError, ValueError):
            self.eth_minutes2 = 30

        self.flatten_times = [_parse_hhmm(x) for x in st.get("flatten_times", ["14:55", "22:55"])]
        # 拿到收盘，听配置页的收盘清仓总开关。开着就在 14:55、22:55 平。
        self.flatten_at_close = config_flag(st.get("flatten_at_close", True), default=True)
        sessions = (cfg.get("schedule") or {}).get("trading_sessions") or []
        self.entry_sessions = [(_parse_hhmm(a), _parse_hhmm(b)) for a, b in sessions]

        self.positions: dict[str, PositionState] = {s: PositionState(symbol=s) for s in self.symbols}
        # 日盘最多两次、夜盘一次。以真正开仓成功为准；只出信号未成交不占名额。
        # 重启后由 runner 从磁盘灌回，避免同一段又开第二次。
        self._used_session: dict[str, str] = {}
        self._used_bar: dict[str, str] = {}
        # 这一段开成了几次；以及上一次平仓是不是打破箱沿止损（止损才允许开第二次）。
        self._open_count: dict[str, int] = {}
        self._stop_closed: dict[str, bool] = {}
        # 最新一次开仓信号所属的日盘/夜盘。成交后写入 _used_session。
        self._pending_session: dict[str, str] = {}
        self._pending_bar: dict[str, str] = {}
        # 止损平仓的决定已发出、等成交回报；成交后才算「止损出场」。
        self._pending_stop: dict[str, bool] = {}

    def is_flatten_time(self, now: datetime | None = None) -> bool:
        """收盘清仓窗口。开关关掉时，到点也不平。"""
        if not self.flatten_at_close:
            return False
        now = _naive(now or datetime.now())
        current = now.replace(second=0, microsecond=0)
        for item in self.flatten_times:
            start = datetime.combine(current.date(), item)
            end = start + timedelta(minutes=10)
            if start <= current <= end:
                return True
        return False

    def in_entry_session(self, now: datetime) -> bool:
        """不在允许交易的钟点里，不开新仓。已经拿着的单不受这个限制。"""
        if not self.entry_sessions:
            return True
        clock = now.time()
        return any(start <= clock <= end for start, end in self.entry_sessions)

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        """成交之后记住仓位。止损就是开仓时冻结的箱沿，不再盘中收紧。"""
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            pos.side = "long" if action == StrategyAction.BUY_OPEN else "short"
            pos.volume = volume
            pos.entry_price = price
            pos.initial_stop = float(initial_stop or 0)
            pos.trail_stop = pos.initial_stop
            pos.trail_armed = pos.initial_stop > 0
            pos.opened_at = datetime.now().isoformat(timespec="seconds")
            # 真正开成后才占掉这一段名额；点单失败不占，后面的 K 线还能再试。
            session = self._pending_session.pop(symbol, "")
            bar_id = self._pending_bar.pop(symbol, "")
            if session:
                if self._used_session.get(symbol) != session:
                    # 新的一段：次数清零，上一段的止损标记也不再算数。
                    self._open_count[symbol] = 0
                    self._stop_closed[symbol] = False
                # 开新仓时清掉可能残留的止损挂起（比如止损单没成交、后来手动平账）。
                self._pending_stop.pop(symbol, None)
                self._open_count[symbol] = self._open_count.get(symbol, 0) + 1
                self._used_session[symbol] = session
                self._used_bar[symbol] = bar_id or "filled"
            if pos.initial_stop <= 0:
                logger.warning("{} 开仓成交后没有箱沿止损价，持仓将缺少保底", symbol)
            return
        if action == StrategyAction.CLOSE and pos.reduce_close(volume):
            self.positions[symbol] = PositionState(symbol=symbol)
            self._pending_session.pop(symbol, None)
            self._pending_bar.pop(symbol, None)
            # 只有打破箱沿止损的平仓，才允许这段再开第二次。
            self._stop_closed[symbol] = bool(self._pending_stop.pop(symbol, False))

    def _bar_minutes(self) -> int:
        """信号周期有几分钟。日线、周线没有「开盘后几分钟」这种说法。"""
        period = canonical_period(str(self.timeframe))
        if period in {"D", "W"}:
            return 0
        return int(period)

    def _anchor(self, now: datetime) -> dtime | None:
        """
        当前这段从哪个钟点算开盘。

        9:00 到 21:00 之前算日盘，看早上的开盘价。
        21:00 之后算夜盘，看夜盘开盘价。凌晨还没到 9 点，不属于这两段。
        """
        clock = now.time()
        if _DAY_OPEN <= clock < _NIGHT_OPEN:
            return _DAY_OPEN
        if clock >= _NIGHT_OPEN:
            return _NIGHT_OPEN
        return None

    def _eth_end(self, anchor: dtime, minutes: int | None = None) -> dtime:
        """这段 ETH 走完的钟点。9:00 加 20 分钟是 9:20，21:00 加 20 分钟是 21:20。"""
        span = self.eth_minutes if minutes is None else max(0, int(minutes))
        moment = datetime(2000, 1, 1, anchor.hour, anchor.minute) + timedelta(minutes=span)
        return moment.time()

    def _session_limit(self, anchor: dtime) -> int:
        """这一段最多开几次：日盘两段 ETH 最多 2 次；夜盘只开 1 次。"""
        if anchor == _DAY_OPEN and self.eth_minutes2 > 0:
            return 2
        return 1

    def _count_for(self, symbol: str, session: str) -> int:
        """这一段已经开成几次。重启恢复只有「用过」标记时按 1 次算，不会再开第二次。"""
        if self._used_session.get(symbol) != session:
            return 0
        return max(1, self._open_count.get(symbol, 0))

    def _session_key(self, now: datetime, anchor: dtime) -> str:
        """同一天的日盘、夜盘各算一段，互不影响。"""
        return f"{now.date().isoformat()} {anchor.strftime('%H:%M')}"

    def _effective_stop(self, pos: PositionState) -> float:
        """真正生效的止损：开仓时冻结的箱沿价，盘中不再改。"""
        return float(pos.initial_stop or pos.trail_stop or 0)

    def _session_gap(
        self, bars: pd.DataFrame, now: datetime, anchor: dtime
    ) -> tuple[float, float, float] | None:
        """
        这一段开盘相对上一段收盘跳了多少（价格点）。

        日盘拿昨晚最后一根的收盘当参照，夜盘拿日盘最后一根的收盘当参照。
        拿不到上一段的K线（比如回测区间正好从这一段的开盘开始）就返回 None，当没有跳空。
        """
        view = _signal_view_bars(bars, now, self.timeframe)
        if view is None or len(view) == 0:
            return None
        opened = pd.Timestamp(_naive(now)).normalize() + pd.Timedelta(
            hours=anchor.hour, minutes=anchor.minute
        )
        before = view.loc[view.index <= opened]
        after = view.loc[view.index > opened]
        if len(before) == 0 or len(after) == 0:
            return None
        try:
            prev_close = float(pd.to_numeric(before["close"], errors="coerce").iloc[-1])
            open_px = float(pd.to_numeric(after["open"], errors="coerce").iloc[0])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if pd.isna(prev_close) or pd.isna(open_px):
            return None
        return open_px - prev_close, open_px, prev_close

    def _stop_hit(self, pos: PositionState, bars: pd.DataFrame) -> bool:
        """这根 K 线的最低或最高有没有碰到箱沿止损。"""
        stop = self._effective_stop(pos)
        if stop <= 0 or bars is None or len(bars) == 0:
            return False
        if pos.side == "long":
            return float(bars["low"].iloc[-1]) <= stop
        if pos.side == "short":
            return float(bars["high"].iloc[-1]) >= stop
        return False

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        now: datetime | None = None,
        trail_bars: pd.DataFrame | None = None,
        trend_bars: pd.DataFrame | None = None,
    ) -> StrategyDecision:
        """
        先管已经拿着的仓，再看信号周期收盘有没有离开开盘区间。

        持仓：打穿箱沿就平；否则拿到收盘。不再做回撤收紧。
        """
        del trail_bars
        if bars is None or len(bars) == 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "没有K线，先看")
        if now is None:
            now = pd.Timestamp(bars.index[-1]).to_pydatetime()
        now = _naive(now)
        if self._bar_minutes() <= 0:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                "日内实体只看分钟线。日线、周线没有开盘后的 ETH 区间，这套不判断",
            )

        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        anchor = self._anchor(now)
        session = self._session_key(now, anchor) if anchor is not None else ""

        if pos.side != "flat":
            stop = self._effective_stop(pos)
            if stop <= 0:
                logger.warning("{} 持仓中但箱沿止损为 0，无法按箱体保底，请核对状态文件", symbol)
            if self._stop_hit(pos, bars):
                edge = "下沿" if pos.side == "long" else "上沿"
                logger.info("{} 日内实体打破开盘箱体{} {:.2f}", symbol, edge, stop)
                # 止损决定先挂起，成交回报到了才算「止损出场」，日盘才有机会开第二次。
                self._pending_stop[symbol] = True
                return StrategyDecision(
                    StrategyAction.CLOSE,
                    symbol,
                    f"打破开盘箱体{edge} {stop:.2f}，方向作废，先出来，不再拿到收盘",
                    entry_price=None,
                    stop_price=stop,
                )
            if self.is_flatten_time(now):
                return StrategyDecision(
                    StrategyAction.CLOSE,
                    symbol,
                    "收到这段收盘，日内方向拿到这里",
                    entry_price=None,
                )
            side_name = "多" if pos.side == "long" else "空"
            stop_text = f"箱沿止损在 {stop:.2f}。" if stop > 0 else "箱沿止损缺失。"
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"顺着日内方向拿着{side_name}单，{stop_text}还没有打破箱体对沿，继续拿到收盘",
                entry_price=pos.entry_price,
                stop_price=stop or None,
            )

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "已经到收盘时间，不再开新仓")
        if anchor is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "还没到日盘或夜盘的开盘，先看")
        if not scope_allows(self.session_scope, now):
            return StrategyDecision(StrategyAction.HOLD, symbol, scope_block_reason(self.session_scope))
        if not self.in_entry_session(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "不在允许交易的时段，不开")

        bar_id = str(bars.index[-1])
        count = self._count_for(symbol, session)
        limit = self._session_limit(anchor)
        if count >= limit:
            if limit >= 2:
                text = "日盘这段已经开满两次，不再开"
            elif anchor == _NIGHT_OPEN:
                text = "夜盘只开一次，这段已经开过，不再开第二次"
            else:
                text = "二次开仓已关闭，这段不再开第二次"
            return StrategyDecision(StrategyAction.HOLD, symbol, text)
        second = count > 0
        if second:
            if not self._stop_closed.get(symbol):
                return StrategyDecision(
                    StrategyAction.HOLD, symbol, "第一次不是打破箱沿止损出场，这段不再开第二次"
                )
            if self._used_bar.get(symbol) == bar_id:
                return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经判断过，先看")
        # 第一次用第一段 ETH；第二次用第二段 ETH 的箱体。截止钟点两段共用。
        eth = self.eth_minutes2 if second else self.eth_minutes
        start = self._eth_end(anchor, eth)
        end = self.night_cutoff if anchor == _NIGHT_OPEN else self.day_cutoff
        if now.time() <= start:
            which = "第二段 ETH" if second else "ETH"
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{which} 到 {start.strftime('%H:%M')} 才走完，先把开盘区间看完",
            )
        if now.time() > end:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"已经过了 {end.strftime('%H:%M')}，这段不再追",
            )

        del trend_bars
        side, text = judge_opening_break(bars, now, anchor, eth, self.timeframe)
        if side == "long":
            action = StrategyAction.BUY_OPEN
        elif side == "short":
            action = StrategyAction.SELL_OPEN
        else:
            return StrategyDecision(StrategyAction.HOLD, symbol, text)

        # 跳空过滤只管这一段的第一枪；第二次开仓发生在盘中，不再按开盘跳空拦。
        if not second and self.skip_gap_ticks > 0:
            gap = self._session_gap(bars, now, anchor)
            limit = self.skip_gap_ticks * self.price_tick
            if gap is not None and abs(gap[0]) > limit:
                return StrategyDecision(
                    StrategyAction.HOLD,
                    symbol,
                    f"{text}。但这一段开盘相对上一段收盘跳空 {gap[0]:+.0f} 点"
                    f"（上限 {limit:.0f} 跳），缺口回补时箱沿止损会被一次打穿，这段不追",
                )

        box_high, box_low, wait = opening_box_levels(
            bars, now, anchor, eth, self.timeframe
        )
        if box_high is None or box_low is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, wait or "开盘箱体画不出来，先不开")

        if self.max_box_ticks > 0:
            width = float(box_high) - float(box_low)
            limit = self.max_box_ticks * self.price_tick
            if width > limit:
                return StrategyDecision(
                    StrategyAction.HOLD,
                    symbol,
                    f"{text}。但开盘箱体 {box_low:.2f} 到 {box_high:.2f}，宽 {width:.0f} 点"
                    f"（上限 {self.max_box_ticks} 跳 = {limit:.0f}），箱沿止损会跟着变宽，这段不开",
                )

        entry = float(bars["close"].iloc[-1])
        tick_pad = self.price_tick * self.stop_extra_ticks
        stop = box_edge_stop(side=side, box_low=box_low, box_high=box_high, tick=tick_pad)
        distance = abs(entry - stop)
        # 先记下「如果成交，占哪一段」；未成交不写入 _used_session。
        self._pending_session[symbol] = session
        self._pending_bar[symbol] = bar_id
        if side == "long":
            stop_text = (
                f"开盘箱体下沿 {box_low:.2f} 再减 {self.stop_extra_ticks} 跳"
                f"（{tick_pad:g}），开仓后冻结；只有打破箱底才算方向作废"
            )
        else:
            stop_text = (
                f"开盘箱体上沿 {box_high:.2f} 再加 {self.stop_extra_ticks} 跳"
                f"（{tick_pad:g}），开仓后冻结；只有打破箱顶才算方向作废"
            )
        reason = (
            f"{text}。箱沿止损 {stop:.2f}，离进场价 {distance:.2f}，{stop_text}。"
            f"中间不再做回撤止损，顺着捅破方向拿到收盘"
        )
        if second:
            reason = f"第二次开仓（第一段止损后，用第二段 ETH{eth} 箱体）：{reason}"
        logger.info("{} {}", symbol, reason)
        return StrategyDecision(
            action,
            symbol,
            reason,
            stop_price=stop,
            entry_price=entry,
            meta={
                "signal": "daily_body",
                "bar_id": bar_id,
                "session": session,
                "side": side,
                "stop_kind": "box",
                "box_high": box_high,
                "box_low": box_low,
            },
        )
