"""
日内实体。

功能：方向看「当前信号周期」K 线。ETH 走完之后，收盘离开开盘区间才开仓。
     站上去做多，跌下去做空，还在区间里就等。
     5/15/30/60 同一套标准；日线、周线没有开盘 ETH，这套不判断。
     急破箱体不追：信号收盘离破位超过 max_chase_atr_mult 倍开盘前 ATR 就先等回抽；
     回到破位允许范围内、且收盘没有有效打穿 EMA20，才按原方向开。
     日盘、夜盘各自有个开仓截止钟点（默认 10:30 / 22:15），过了就不再追。13:30 不重新画区间。
     止损只认开盘箱体：多单守箱底外一跳，空单守箱顶外一跳；开仓后冻结。
     触发方式按全策略共用的 strategy.stop_mode：touch 价格触及对沿就平（默认，实时看影线）；
     close 要等这一根 K 线收盘价跌破/站上对沿才平。两种都算方向作废；
     没触发就顺着捅破方向拿到收盘。
     日盘两个 ETH 窗口都从这段开盘起算，各自画箱子、各自判断：
     第一ETH窗口（eth_minutes）走完后按第一口箱子找突破；
     墙上时间过了第二ETH窗口（eth_minutes2，填 0 关闭）之后，丢掉第一窗口的判断，
     只认第二口箱子。第一窗口开仓被箱沿止损打掉后，截止前也可以按第二窗口再开一次。
     第二次止损后不再开。夜盘只用第一窗口，只开一次。
     公共设置选「夜日连续」时：夜盘+日盘算同一段。三个 ETH 窗口各自画箱子
     （21:00+夜盘ETH、9:00+ETH1、9:00+ETH2），整段最多开三次。
     夜盘开仓不打止损就不在夜盘收盘清仓，拿到日盘 14:55；中途止损后再按当时对应窗口重画。
     开新仓前自带行情过滤：每一轮扫描用当前 K 线重算 ADX/ATR。
     ADX 已经很高（强单边）或波动太极端则不开；盘面拧来拧去反而放行。
     不是开盘拦一次就整天停手。已有持仓仍按箱沿止损。
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

from src.data.periods import (
    canonical_period,
    last_ohlc_price,
    ohlc_series,
    period_label,
    resample_ohlc,
    unique_ohlc_columns,
)
from src.utils.session_clock import cutoff_passed, in_session_windows, last_open_at
from src.utils.trading_day import futures_trading_day

from .base import StrategyBase
from .indicators import average_true_range, ema_series
from .params import section_default
from .session_scope import (
    SCOPE_LINKED,
    SCOPE_NIGHT,
    scope_allows,
    scope_block_reason,
    scope_from_cfg,
    session_kind,
)
from .types import (
    STOP_MODE_CLOSE,
    STOP_MODE_TOUCH,
    PositionState,
    StrategyAction,
    StrategyDecision,
    config_flag,
    stop_mode_from_cfg,
    stop_triggered,
)


def _atr_mult(value: Any, default: float) -> float:
    """配置里的 ATR 倍数。认不出就用出厂值；0 表示关掉这一道闸；上限 20 防止填成跳数。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    if out != out:  # NaN
        return float(default)
    return max(0.0, min(20.0, out))


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
# 交易时段结束时刻往往就是最后一根 K 线的收线时刻（11:30 / 15:00 / 02:30）。
# 墙上时钟刚过这一秒，时段若立刻关掉，这根永远扫不到。再留 90 秒给扫描和下单。
_SESSION_END_GRACE = timedelta(seconds=90)


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


def suggest_stop_price(
    bars: pd.DataFrame,
    *,
    side: str,
    price_tick: float = 1.0,
    stop_extra_ticks: int = 1,
    eth_minutes: int = 30,
    timeframe: str = "5m",
    session_open: dtime = dtime(9, 0),
    now: datetime | None = None,
) -> float | None:
    """
    纯函数：给定 K 线和 daily_body 参数，算出推荐止损价。

    拿不到开盘箱体时返回 None（不推荐）。
    用于 UI 的「手工单录入」弹窗——用户填了品种后提示止损价。
    """
    if bars is None or len(bars) < 2:
        return None
    from datetime import datetime as _dt

    now = _naive(now or _dt.now())
    box_high, box_low, wait = opening_box_levels(
        bars, now, session_open, int(eth_minutes), str(timeframe),
    )
    if box_high is None or box_low is None:
        return None
    tick_pad = max(0.0, float(price_tick)) * max(0, int(stop_extra_ticks))
    return box_edge_stop(side=side, box_low=box_low, box_high=box_high, tick=tick_pad)


def suggest_stop_both(
    bars: pd.DataFrame,
    *,
    price_tick: float = 1.0,
    stop_extra_ticks: int = 1,
    eth_minutes: int = 30,
    timeframe: str = "5m",
    session_open: dtime = dtime(9, 0),
    now: datetime | None = None,
) -> tuple[float | None, float | None]:
    """同时返回 (多单推荐止损, 空单推荐止损)。任一拿不到就返回 None。"""
    long_stop = suggest_stop_price(
        bars, side="long",
        price_tick=price_tick, stop_extra_ticks=stop_extra_ticks,
        eth_minutes=eth_minutes, timeframe=timeframe,
        session_open=session_open, now=now,
    )
    short_stop = suggest_stop_price(
        bars, side="short",
        price_tick=price_tick, stop_extra_ticks=stop_extra_ticks,
        eth_minutes=eth_minutes, timeframe=timeframe,
        session_open=session_open, now=now,
    )
    return long_stop, short_stop


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
    # 最常见的情况：索引本来就是不带时区的 DatetimeIndex，这时不需要整表复制。
    # 下面的调用方只读不写，直接返回原表即可。
    index = bars.index
    if isinstance(index, pd.DatetimeIndex) and index.tz is None:
        return bars
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
    if view.empty or "close" not in view.columns:
        return None, None, f"没有走完的{tag}K线，先看"
    opened = pd.Timestamp(last_open_at(_naive(now), session_open))
    eth_end = opened + pd.Timedelta(minutes=max(0, int(eth_minutes)))
    boxed = view.loc[(view.index > opened) & (view.index <= eth_end)]
    if boxed.empty or not {"high", "low"}.issubset(boxed.columns):
        return None, None, f"ETH里的{tag}K线不够，开盘区间还没画出来，先等"
    box_high = float(ohlc_series(boxed, "high").max())
    box_low = float(ohlc_series(boxed, "low").min())
    if box_high != box_high or box_low != box_low or box_high <= box_low:
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
    最新一根已收盘 K 线，在下一根周期到来之前都按它定方向。
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
    # 扫描很难卡在收线那一秒，数据源也会晚几十秒。
    # 有效窗口 = 这一根信号周期的长度：5 分钟线收到后 5 分钟内都还认它。
    # 超过整整一根周期再拿它开，就是过时信号（午休后也不要拿上午最后一根去开）。
    # 同一根反复发单由调度器按 K 线时间拦住；点单失败时同一根还能再试。
    last_ts = view.index[-1]
    delta = (moment - last_ts).total_seconds()
    if delta < 0:
        return "none", f"{tag}还没走完，先等"
    if delta > minutes * 60:
        return "none", f"最新已收盘的{tag}已经过了整整一根周期，不再用它开仓，等下一根"
    box_high, box_low, wait = opening_box_levels(bars, now, session_open, eth_minutes, timeframe)
    if box_high is None or box_low is None:
        return "none", wait
    opened = pd.Timestamp(last_open_at(_naive(now), session_open))
    eth_end = opened + pd.Timedelta(minutes=max(0, int(eth_minutes)))
    bar_start = last_ts - pd.Timedelta(minutes=minutes)
    if bar_start < eth_end:
        return "none", f"这根{tag}还有一段落在ETH里，等下一根完全走在ETH后面"
    last_close = last_ohlc_price(view, "close")
    if last_close is None:
        return "none", f"没有走完的{tag}收盘价，先看"
    if last_close > box_high:
        return "long", (
            f"{tag}收盘站上开盘区间。区间 {box_low:.2f} 到 {box_high:.2f}，收盘 {last_close:.2f}"
        )
    if last_close < box_low:
        return "short", (
            f"{tag}收盘跌破开盘区间。区间 {box_low:.2f} 到 {box_high:.2f}，收盘 {last_close:.2f}"
        )
    return "none", f"{tag}收盘还在开盘区间 {box_low:.2f} 到 {box_high:.2f} 里面，先等"


# 同一轮判断里，定方向、画箱子、算 ATR/EMA 都要同一份信号周期 K 线。
# 只留最近一次结果，省掉重复切片和重采样。缓存持有 bars 本身，id 不会被回收复用；
# 换了 K 线、时间或周期就重算，最多多算一次。
_SIGNAL_VIEW_CACHE: tuple[pd.DataFrame, pd.Timestamp, str, int, pd.DataFrame | None] | None = None


def _signal_view_bars(bars: pd.DataFrame, now: datetime, timeframe: str) -> pd.DataFrame | None:
    """
    拿出已经走完的信号周期 K 线。

    信号是多少分钟就用多少分钟；传入更细的 K 时先收成信号周期。
    日线、周线返回空（没有开盘后的 ETH 区间这套用法）。
    """
    global _SIGNAL_VIEW_CACHE
    minutes = _signal_minutes(timeframe)
    if minutes <= 0:
        return None
    period = str(minutes)
    moment = pd.Timestamp(_naive(now))
    cached = _SIGNAL_VIEW_CACHE
    if (
        cached is not None
        and cached[0] is bars
        and cached[1] == moment
        and cached[2] == period
        and cached[3] == len(bars)
    ):
        return cached[4]
    frame = _with_clock(bars)
    frame = frame.loc[frame.index <= moment]
    view = frame
    if len(frame) >= 2:
        delta_m = (frame.index[-1] - frame.index[-2]).total_seconds() / 60.0
        if delta_m > 0 and delta_m < minutes * 0.6:
            view = resample_ohlc(frame, period)
            if view is not None and len(view) > 0:
                # 重采样后的右标签可能落在 moment 之后，还是要再切一次。
                view = view.loc[view.index <= moment]
    _SIGNAL_VIEW_CACHE = (bars, moment, period, len(bars), view)
    return view


class DailyBodyStrategy(StrategyBase):
    """方向看信号周期。ETH 走完后，收盘离开开盘区间才开仓。"""

    # 开仓记录里的信号名。子类换自己的名字，避免和日内实体混在同一本账里。
    SIGNAL_NAME = "daily_body"
    # 参数写在配置哪一节。子类改成自己的节，这里的数字不会被带走。
    SECTION = "daily_body"
    _DEFAULT_ETH = 20
    _DEFAULT_ETH2 = 30

    # 方向看信号周期均线/收盘，不再另外要日线。
    uses_trend_bars = False
    trend_timeframe = "15"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        st = cfg.get("strategy", {}) or {}
        trade = cfg.get("trade", {}) or {}
        self.cfg = cfg
        self.timeframe = str(st.get("timeframe", "5"))
        self.trail_timeframe = str(st.get("trail_timeframe", self.timeframe))
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        # 日线字段留着，只为和别的策略调用方式对齐；本策略不算日线方向。
        self.trend_lookback = 10
        self.trend_timeframe = "D"
        body = st.get(self.SECTION) or {}
        # 止损触发方式（全策略共用）：touch=触及箱体对沿立刻平；close=等 K 线收盘价确认。
        self.stop_mode = stop_mode_from_cfg(cfg)
        # 调度层据此决定持仓时要不要把「未收线那根」拼进来。收盘确认方式不能看未收线。
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH
        # 箱沿止损再让几跳；默认 1 个最小跳动。
        default_ticks = int(section_default(self.SECTION, "stop_extra_ticks", 1))
        try:
            self.stop_extra_ticks = max(0, int(body.get("stop_extra_ticks", default_ticks) or 0))
        except (TypeError, ValueError):
            self.stop_extra_ticks = default_ticks
        # 三道闸都用「开盘前」信号周期 ATR 的倍数，0=关闭。换品种时跟着波动走。
        self.skip_gap_atr_mult = _atr_mult(
            body.get("skip_gap_atr_mult"),
            float(section_default(self.SECTION, "skip_gap_atr_mult", 2.5)),
        )
        self.max_box_atr_mult = _atr_mult(
            body.get("max_box_atr_mult"),
            float(section_default(self.SECTION, "max_box_atr_mult", 5.0)),
        )
        self.max_chase_atr_mult = _atr_mult(
            body.get("max_chase_atr_mult"),
            float(section_default(self.SECTION, "max_chase_atr_mult", 1.0)),
        )
        default_atr_period = int(section_default(self.SECTION, "filter_atr_period", 14))
        try:
            self.gate_atr_period = max(2, int(body.get("filter_atr_period", default_atr_period) or 14))
        except (TypeError, ValueError):
            self.gate_atr_period = default_atr_period
        # 等回抽时还要看 EMA：反抽有效打穿这条线，这次突破就作废。ema_period=0 关闭。
        default_ema_period = int(section_default(self.SECTION, "ema_period", 20))
        try:
            self.ema_period = max(0, int(body.get("ema_period", default_ema_period) or 0))
        except (TypeError, ValueError):
            self.ema_period = default_ema_period
        default_ema_buffer = int(section_default(self.SECTION, "ema_buffer_ticks", 2))
        try:
            self.ema_buffer_ticks = max(0, int(body.get("ema_buffer_ticks", default_ema_buffer) or 0))
        except (TypeError, ValueError):
            self.ema_buffer_ticks = default_ema_buffer
        # 急破后等回抽：品种 → 属于哪一段、方向、破位点。换段自动清掉，不跨段等。
        self._chase_wait: dict[str, dict[str, Any]] = {}
        # 开仓截止钟点：过了这个钟点，这一段不再开新仓。配置页可以分开设日盘和夜盘。
        self.day_cutoff = _cutoff(body.get("day_cutoff"), DAY_CUTOFF_DEFAULT)
        self.night_cutoff = _cutoff(body.get("night_cutoff"), NIGHT_CUTOFF_DEFAULT)
        # 公共设置：全时段 / 只做日盘 / 只做夜盘 / 夜日连续。
        self.session_scope = scope_from_cfg(cfg)
        # 箱体突破过滤写在本策略里，开关是 daily_body.filter_enabled。
        self.market_filter = self._make_market_filter(cfg)
        ctp = trade.get("ctp", {}) if isinstance(trade.get("ctp"), dict) else {}
        tick_raw = ctp.get("price_tick", trade.get("price_tick", 1))
        try:
            self.price_tick = float(tick_raw)
        except (TypeError, ValueError):
            self.price_tick = 1.0
        if self.price_tick <= 0:
            self.price_tick = 1.0
        # ETH 观察几分钟：优先读本策略小节；日内实体旧配置若还写在 liquidity_exhaustion 里也能读到。
        raw_eth = body.get("eth_minutes")
        if raw_eth is None and self.SECTION == "daily_body":
            raw_eth = (st.get("liquidity_exhaustion") or {}).get("eth_minutes", self._DEFAULT_ETH)
        if raw_eth is None:
            raw_eth = self._DEFAULT_ETH
        try:
            self.eth_minutes = max(0, int(raw_eth))
        except (TypeError, ValueError):
            self.eth_minutes = int(self._DEFAULT_ETH)
        # 第二ETH窗口分钟数（仅日盘，0=关掉）。和第一窗口一样从开盘起算，过点后只认这口箱子。
        try:
            self.eth_minutes2 = max(0, int(body.get("eth_minutes2", self._DEFAULT_ETH2) or 0))
        except (TypeError, ValueError):
            self.eth_minutes2 = int(self._DEFAULT_ETH2)

        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        # 清仓钟点和交易时段都跟品种表走，不写死 22:55 / 23:00。
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        self.flatten_at_close = config_flag(st.get("flatten_at_close", True), default=True)
        self.entry_sessions = session_tuples(cfg, live)

        self.positions: dict[str, PositionState] = {s: PositionState(symbol=s) for s in self.symbols}
        # 日盘最多两次、夜盘一次（夜日连续整段最多三次）。真正开仓成功才占名额。
        # 重启后由 runner 从磁盘灌回，避免同一段又开第二次。
        self._used_session: dict[str, str] = {}
        # 这一段已经用过哪些 ETH 窗口（night / eth1 / eth2）。夜日连续用来避免夜盘开第二次。
        self._used_kinds: dict[str, set[str]] = {}
        self._pending_kind: dict[str, str] = {}
        self._used_bar: dict[str, str] = {}
        # 这一段开成了几次；以及上一次平仓是不是打破箱沿止损（止损才允许开第二次）。
        self._open_count: dict[str, int] = {}
        self._stop_closed: dict[str, bool] = {}
        # 这一笔开仓当时用的 ETH 分钟数。补止损时要和开仓时同一口箱子，不能中途换成第二段。
        self._pending_eth: dict[str, int] = {}
        self._entry_eth: dict[str, int] = {}
        # 最新一次开仓信号所属的日盘/夜盘。成交后写入 _used_session。
        self._pending_session: dict[str, str] = {}
        self._pending_bar: dict[str, str] = {}
        # 止损平仓的决定已发出、等成交回报；成交后才算「止损出场」。
        # 同时记挂起时间：点单没成交、后来手工平账时，不能把手工平仓当成止损出场。
        self._pending_stop: dict[str, bool] = {}
        self._pending_stop_at: dict[str, datetime] = {}

    def _make_market_filter(self, cfg: dict[str, Any]):
        """主推：区间过滤（ADX 过高不开）。对照策略会换成扩张过滤。"""
        from .market_state import MarketFilter, range_params_for_daily_body

        return MarketFilter(range_params_for_daily_body(cfg))

    def _engine_title(self) -> str:
        """给用户看的策略短名，写进观望原因里。"""
        return "日内实体"

    def _uses_opening_box(self) -> bool:
        """要不要画开盘箱子、等观察窗口。浮盈飘单里部分方向选择器会关掉。"""
        return True

    def _direction_signal(
        self,
        bars: pd.DataFrame,
        now: datetime,
        anchor: dtime,
        eth: int,
        symbol: str = "",
        second: bool = False,
    ) -> tuple[str, str]:
        """
        这一轮做多、做空，还是先等。

        返回 (long|short|none, 给人看的原因)。none 表示还没有方向，不要占开仓名额。
        日内实体：收盘离开开盘箱子。浮盈飘单会换成独立的方向选择器。
        """
        del symbol, second
        return judge_opening_break(bars, now, anchor, eth, self.timeframe)

    def _open_stop(
        self,
        side: str,
        box_low: float,
        box_high: float,
        tick_pad: float,
        entry: float | None = None,
        atr: float | None = None,
    ) -> float:
        """开仓时的结构止损。日内实体守箱体对沿；子类可以改成按 ATR 留宽度。"""
        del entry, atr
        return box_edge_stop(side=side, box_low=box_low, box_high=box_high, tick=tick_pad)

    def _gap_align_block(
        self,
        symbol: str,
        side: str,
        bars: pd.DataFrame,
        now: datetime,
        anchor: dtime,
        text: str,
        second: bool,
    ) -> StrategyDecision | None:
        """跳空方向过滤。日内实体不做这一层，子类需要时再拦反向。"""
        del symbol, side, bars, now, anchor, text, second
        return None

    def _maybe_breakeven(self, pos: PositionState, bars: pd.DataFrame) -> None:
        """浮盈够了以后把止损提到开仓价。日内实体不做保本，子类再开。"""
        del pos, bars

    def _hold_extra(self, pos: PositionState) -> str:
        """持仓观望句里多出来的说明。"""
        del pos
        return ""

    def _stop_hit_text(self, pos: PositionState, stop: float) -> str:
        """止损平仓时给人看的原因。"""
        edge = "下沿" if pos.side == "long" else "上沿"
        verb = "收盘跌破" if pos.side == "long" else "收盘站上"
        if self.stop_mode == STOP_MODE_CLOSE:
            return f"收盘确认止损，{verb}开盘箱体{edge} {stop:.2f}"
        return f"触及止损，打破开盘箱体{edge} {stop:.2f}"

    def _open_stop_text(
        self, side: str, box_low: float, box_high: float, tick_pad: float
    ) -> str:
        """开仓理由里，止损放在哪。"""
        if side == "long":
            return (
                f"开盘箱体下沿 {box_low:.2f} 再减 {self.stop_extra_ticks} 跳"
                f"（{tick_pad:g}），开仓后冻结；只有打破箱底才算方向作废"
            )
        return (
            f"开盘箱体上沿 {box_high:.2f} 再加 {self.stop_extra_ticks} 跳"
            f"（{tick_pad:g}），开仓后冻结；只有打破箱顶才算方向作废"
        )

    def _linked(self) -> bool:
        """交易时段是不是「夜日连续」：夜盘和日盘合成一段。"""
        return self.session_scope == SCOPE_LINKED

    def is_flatten_time(self, now: datetime | None = None) -> bool:
        """收盘清仓窗口。开关关掉时，到点也不平。钟点来自品种表。"""
        if not self.flatten_at_close:
            return False
        from src.utils.session_clock import in_flatten_window

        now = _naive(now or datetime.now())
        if self._linked():
            # 夜日连续：夜盘 22:55 不平仓，只在日盘收盘清仓。周末白天的 14:55 不是交易日。
            day_clocks = [
                clock for clock in self.flatten_times if session_kind(clock) != SCOPE_NIGHT
            ]
            if not in_flatten_window(day_clocks, now):
                return False
            if now.weekday() >= 5:
                return False
            return dtime(8, 0) <= now.time() < dtime(16, 0)
        return in_flatten_window(self.flatten_times, now)

    def in_entry_session(self, now: datetime, symbol: str | None = None) -> bool:
        """不在允许交易的钟点里，不开新仓。已经拿着的单不受这个限制。夜盘可跨零点。"""
        from src.utils.session_clock import parse_hhmm, sessions_for_symbol

        windows = self.entry_sessions
        if symbol:
            rows = sessions_for_symbol(self.cfg, symbol)
            if rows:
                windows = [(parse_hhmm(a), parse_hhmm(b)) for a, b in rows]
        if not windows:
            return True
        return in_session_windows(
            windows,
            _naive(now),
            grace_sec=_SESSION_END_GRACE.total_seconds(),
        )

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
                    self._entry_eth.pop(symbol, None)
                    self._used_kinds[symbol] = set()
                # 开新仓时清掉可能残留的止损挂起（比如止损单没成交、后来手动平账）。
                self._pending_stop.pop(symbol, None)
                self._pending_stop_at.pop(symbol, None)
                self._open_count[symbol] = self._open_count.get(symbol, 0) + 1
                self._used_session[symbol] = session
                self._used_bar[symbol] = bar_id or "filled"
                self._entry_eth[symbol] = self._pending_eth.pop(symbol, self.eth_minutes)
                kind = self._pending_kind.pop(symbol, "")
                if kind:
                    self._used_kinds.setdefault(symbol, set()).add(kind)
            if pos.initial_stop <= 0:
                logger.warning("{} 开仓成交后没有箱沿止损价，持仓将缺少保底", symbol)
            return
        if action == StrategyAction.CLOSE and pos.reduce_close(volume):
            self.positions[symbol] = PositionState(symbol=symbol)
            self._pending_session.pop(symbol, None)
            self._pending_bar.pop(symbol, None)
            self._pending_eth.pop(symbol, None)
            self._pending_kind.pop(symbol, None)
            # 只有打破箱沿止损的平仓，才允许这段再开第二次。
            # 止损挂起超时的（点单没成交、后来手工平账）不算止损出场。
            self._stop_closed[symbol] = self._take_pending_stop(symbol)

    def _stop_pending_expired(self) -> float:
        """止损挂起最多等多久：至少 2 分钟；长周期最多等一根信号 K 线。"""
        return float(max(120, self._bar_minutes() * 60))

    def _pending_stop_alive(self, symbol: str, now: datetime | None = None) -> bool:
        """止损决定还在等成交回报吗。超时的挂起直接作废。"""
        if not self._pending_stop.get(symbol):
            return False
        stamp = self._pending_stop_at.get(symbol)
        clock = _naive(now or datetime.now())
        if stamp is not None and (clock - stamp).total_seconds() > self._stop_pending_expired():
            self._pending_stop.pop(symbol, None)
            self._pending_stop_at.pop(symbol, None)
            logger.info(
                "{} 止损挂起超过 {} 秒仍未成交，不再算止损出场",
                symbol,
                int(self._stop_pending_expired()),
            )
            return False
        return True

    def _take_pending_stop(self, symbol: str) -> bool:
        """取走止损挂起，并判断它还算不算「止损出场」。"""
        alive = self._pending_stop_alive(symbol)
        self._pending_stop.pop(symbol, None)
        self._pending_stop_at.pop(symbol, None)
        return alive

    def _bar_minutes(self) -> int:
        """信号周期有几分钟。日线、周线没有「开盘后几分钟」这种说法。"""
        period = canonical_period(str(self.timeframe))
        if period in {"D", "W"}:
            return 0
        return int(period)

    def _opens_for(self, symbol: str | None) -> tuple[dtime, dtime | None]:
        """品种表里的日盘开盘、夜盘开盘。没有夜盘时第二个为空。"""
        from src.utils.session_clock import bounds_for_symbol

        bounds = bounds_for_symbol(self.cfg, symbol or (self.symbols[0] if self.symbols else ""))
        day_open = bounds.get("day_open") or _DAY_OPEN
        night_open = bounds.get("night_open")
        return day_open, night_open

    def _is_night_anchor(self, anchor: dtime | None, symbol: str | None) -> bool:
        """这段是不是夜盘开盘箱体。"""
        if anchor is None:
            return False
        _day_open, night_open = self._opens_for(symbol)
        return night_open is not None and anchor == night_open

    def _anchor(self, now: datetime, symbol: str | None = None) -> dtime | None:
        """
        当前这段从哪个钟点算开盘。开盘时刻读品种表，不写死 9:00 / 21:00。

        日盘开盘到夜盘开盘之前算日盘。
        夜盘开盘之后算夜盘。凌晨还在表里的夜盘时段里（白银可到 02:30），
        仍看昨天夜盘开盘价，不要当成「还没开盘」。
        """
        day_open, night_open = self._opens_for(symbol)
        clock = now.time()
        if night_open is None:
            if clock >= day_open and self.in_entry_session(now, symbol):
                return day_open
            from src.utils.session_clock import bounds_for_symbol

            close = bounds_for_symbol(self.cfg, symbol).get("day_close") or dtime(15, 0)
            if day_open <= clock <= close:
                return day_open
            return None
        if day_open <= clock < night_open:
            return day_open
        if clock >= night_open:
            return night_open
        if self.in_entry_session(now, symbol):
            return night_open
        return None

    def _eth_end(self, anchor: dtime, minutes: int | None = None) -> dtime:
        """这个 ETH 窗口走完的钟点。两个窗口都从开盘加分钟数，例如 9:00+15=9:15、9:00+30=9:30。"""
        span = self.eth_minutes if minutes is None else max(0, int(minutes))
        moment = datetime(2000, 1, 1, anchor.hour, anchor.minute) + timedelta(minutes=span)
        return moment.time()

    def _session_limit(self, anchor: dtime, symbol: str | None = None) -> int:
        """这一段最多开几次。夜日连续：夜盘一口+日盘两口，最多 3 次。"""
        if self._linked():
            extra = 1 if self.eth_minutes2 > 0 else 0
            return 2 + extra
        if not self._is_night_anchor(anchor, symbol) and self.eth_minutes2 > 0:
            return 2
        return 1

    def _eth_plan(
        self, symbol: str, count: int, now: datetime, anchor: dtime
    ) -> tuple[int, str]:
        """
        这一枪用哪一口箱子。两个窗口都从这段开盘起算，互不套用。

        first：还没过第二ETH窗口，只按第一ETH窗口判断。
        idle：已经过了第二ETH窗口，丢掉第一窗口，只按第二口箱子判断。
        stop：第一窗口已经开过并止损出来，改用第二口箱子再开。
        夜日连续另用 night / eth1 / eth2，三口箱子各自独立。
        """
        if self._linked():
            return self._eth_plan_linked(symbol, now, anchor)
        if self._is_night_anchor(anchor, symbol) or self.eth_minutes2 <= 0:
            return self.eth_minutes, "first"
        if count > 0:
            return self.eth_minutes2, "stop"
        now_clock = _naive(now)
        eth2_done = last_open_at(now_clock, anchor) + timedelta(
            minutes=max(0, int(self.eth_minutes2))
        )
        # 严格「超过」第二窗口才丢掉第一窗口；刚好走到点仍按第一窗口。
        if now_clock > eth2_done:
            return self.eth_minutes2, "idle"
        return self.eth_minutes, "first"

    def _eth_plan_linked(
        self, symbol: str, now: datetime, anchor: dtime
    ) -> tuple[int, str]:
        """
        夜日连续：三口箱子按当前时间挑。

        夜盘只用 21:00 起的第一ETH窗口。
        日盘按顺序：还没用过 ETH1 就用 9:00+ETH1，用过并止损后再用 9:00+ETH2。
        不会因为过了第二窗口墙钟就把 ETH1 丢掉（和普通日盘不同）。
        """
        if self._is_night_anchor(anchor, symbol) or self.eth_minutes2 <= 0:
            kind = "night" if self._is_night_anchor(anchor, symbol) else "eth1"
            return self.eth_minutes, kind
        used: set[str] = set()
        session = self._session_key(now, anchor, symbol)
        if self._used_session.get(symbol) == session:
            used = set(self._used_kinds.get(symbol) or ())
        if "eth1" in used:
            return self.eth_minutes2, "eth2"
        return self.eth_minutes, "eth1"

    def _count_for(self, symbol: str, session: str) -> int:
        """这一段已经开成几次。重启恢复只有「用过」标记时按 1 次算，不会再开第二次。"""
        if self._used_session.get(symbol) != session:
            return 0
        return max(1, self._open_count.get(symbol, 0))

    def _session_key(self, now: datetime, anchor: dtime, symbol: str | None = None) -> str:
        """同一交易日的日盘、夜盘各算一段。夜盘跨零点仍算同一段，不重新开一次。"""
        if self._linked():
            # 周五夜盘和周一白天是同一个期货交易日，连成一段。
            return f"{futures_trading_day(now).isoformat()} linked"
        if self._is_night_anchor(anchor, symbol):
            return f"{futures_trading_day(now).isoformat()} {anchor.strftime('%H:%M')}"
        return f"{now.date().isoformat()} {anchor.strftime('%H:%M')}"

    def _effective_stop(self, pos: PositionState) -> float:
        """真正生效的止损：开仓时冻结的箱沿价，盘中不再改。"""
        return float(pos.initial_stop or pos.trail_stop or 0)

    def _restore_box_stop(
        self,
        symbol: str,
        pos: PositionState,
        bars: pd.DataFrame,
        now: datetime,
        anchor: dtime,
    ) -> float:
        """
        持仓没有箱沿止损时（手工录入没填止损价、或状态文件里的止损丢了），
        按这一段的开盘箱体补一个，补完就冻结，和正常开仓一样。画不出箱体时返回 0。
        """
        minutes = int(self._entry_eth.get(symbol) or 0)
        if minutes <= 0:
            minutes = self.eth_minutes2 if self._open_count.get(symbol, 0) > 1 else self.eth_minutes
        # ETH 还没走完时箱体是半截的，这时候补止损会贴在太近的位置，一等就打到；先等它走完。
        if _naive(now) <= last_open_at(_naive(now), anchor) + timedelta(
            minutes=max(0, int(minutes))
        ):
            return 0.0
        box_high, box_low, _ = opening_box_levels(bars, now, anchor, minutes, self.timeframe)
        if box_high is None or box_low is None:
            return 0.0
        stop = self._open_stop(
            pos.side,
            float(box_low),
            float(box_high),
            self.price_tick * self.stop_extra_ticks,
            entry=float(pos.entry_price or 0) or None,
            atr=self._pre_open_atr(bars, now, anchor),
        )
        pos.initial_stop = stop
        pos.trail_stop = stop
        pos.trail_armed = True
        logger.warning(
            "{} 持仓缺箱沿止损，按这一段开盘箱体补 {:.2f}（{}），已冻结",
            symbol,
            stop,
            "箱底" if pos.side == "long" else "箱顶",
        )
        return stop

    def _pre_open_atr(
        self, bars: pd.DataFrame, now: datetime, anchor: dtime
    ) -> float | None:
        """
        开盘前已经走完的信号周期 ATR。

        跳空、箱宽、急破都用这一根尺子，不把今天 ETH 算进去，避免宽箱子把自己的上限抬高。
        K 线不够或算出来是 0 时返回 None，这一轮先不拦。
        """
        view = _signal_view_bars(bars, now, self.timeframe)
        if view is None or view.empty:
            return None
        opened = pd.Timestamp(last_open_at(_naive(now), anchor))
        prior = view.loc[view.index <= opened]
        need = max(2, int(self.gate_atr_period)) + 1
        if len(prior) < need:
            return None
        series = average_true_range(
            prior["high"], prior["low"], prior["close"], self.gate_atr_period
        )
        cleaned = series.dropna()
        if cleaned.empty:
            return None
        try:
            val = float(cleaned.iloc[-1])
        except (TypeError, ValueError, IndexError):
            return None
        if val != val or val <= 1e-12:
            return None
        return val

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
        opened = pd.Timestamp(last_open_at(_naive(now), anchor))
        before = view.loc[view.index <= opened]
        after = view.loc[view.index > opened]
        if len(before) == 0 or len(after) == 0:
            return None
        try:
            prev_close = last_ohlc_price(before, "close")
            open_s = ohlc_series(after, "open").dropna()
            open_px = float(open_s.iloc[0]) if len(open_s) else None
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if prev_close is None or open_px is None:
            return None
        return open_px - prev_close, open_px, prev_close

    def _stop_hit(self, pos: PositionState, bars: pd.DataFrame) -> bool:
        """
        这一根有没有触发箱沿止损。

        touch：看最高/最低，价格影线碰到对沿就算。
        close：只看收盘价，收盘跌破/站上对沿才算（影线刺出去又收回来不算）。
        """
        stop = self._effective_stop(pos)
        if stop <= 0 or bars is None or len(bars) == 0:
            return False
        close_px = last_ohlc_price(bars, "close")
        high_px = last_ohlc_price(bars, "high")
        low_px = last_ohlc_price(bars, "low")
        if close_px is None or high_px is None or low_px is None:
            return False
        return stop_triggered(
            side=pos.side,
            stop=stop,
            close=close_px,
            high=high_px,
            low=low_px,
            mode=self.stop_mode,
        )

    def _ema_close(self, bars: pd.DataFrame, now: datetime) -> float | None:
        """信号周期收盘价的 EMA，用来判断回抽有没有把突破方向打掉。K 线不够时返回 None。"""
        if self.ema_period <= 0:
            return None
        view = _signal_view_bars(bars, now, self.timeframe)
        if view is None or "close" not in view.columns or len(view) < self.ema_period:
            return None
        closes = ohlc_series(view, "close").dropna()
        if len(closes) < self.ema_period:
            return None
        return float(ema_series(closes, self.ema_period).iloc[-1])

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
        bars = unique_ohlc_columns(bars)
        if bars is None or len(bars) == 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, "没有K线，先看")
        if now is None:
            now = pd.Timestamp(bars.index[-1]).to_pydatetime()
        now = _naive(now)
        if self._bar_minutes() <= 0:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{self._engine_title()}只看分钟线。日线、周线没有开盘后的 ETH 区间，这套不判断",
            )

        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        anchor = self._anchor(now, symbol)
        session = self._session_key(now, anchor, symbol) if anchor is not None else ""
        # 急破等回抽只在同一段里有效；换了日盘/夜盘就清掉，别拿上一段的方向等这一段。
        prior_wait = self._chase_wait.get(symbol)
        if prior_wait and prior_wait.get("session") != session:
            self._chase_wait.pop(symbol, None)

        if pos.side != "flat":
            stop = self._effective_stop(pos)
            if stop <= 0 and anchor is not None:
                # 手工录入没填止损价、或状态文件丢了止损时，按这一段的箱体补一个，补完冻结。
                stop = self._restore_box_stop(symbol, pos, bars, now, anchor)
            if stop <= 0:
                logger.warning("{} 持仓中但箱沿止损为 0，无法按箱体保底，请核对状态文件", symbol)
            self._maybe_breakeven(pos, bars)
            stop = self._effective_stop(pos)
            if self._stop_hit(pos, bars):
                hit_text = self._stop_hit_text(pos, stop)
                logger.info("{} {} {}", symbol, self._engine_title(), hit_text)
                # 止损决定先挂起，成交回报到了才算「止损出场」，日盘才有机会开第二次。
                # 时间戳用真实钟表：数据源晚到时 K 线时间会偏旧，拿它计时会一挂就过期。
                self._pending_stop[symbol] = True
                self._pending_stop_at[symbol] = _naive(datetime.now())
                return StrategyDecision(
                    StrategyAction.CLOSE,
                    symbol,
                    f"{hit_text}，方向作废，先出来，不再拿到收盘",
                    entry_price=None,
                    stop_price=stop,
                    meta={"structure_stop": True},
                )
            if self.is_flatten_time(now):
                return StrategyDecision(
                    StrategyAction.CLOSE,
                    symbol,
                    "收到这段收盘，日内方向拿到这里",
                    entry_price=None,
                )
            side_name = "多" if pos.side == "long" else "空"
            stop_text = f"结构止损在 {stop:.2f}。" if stop > 0 else "结构止损缺失。"
            extra = self._hold_extra(pos)
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"顺着日内方向拿着{side_name}单，{stop_text}{extra}还没有打到止损，继续拿到收盘",
                entry_price=pos.entry_price,
                stop_price=stop or None,
            )

        if self.is_flatten_time(now):
            return StrategyDecision(StrategyAction.HOLD, symbol, "已经到收盘时间，不再开新仓")
        if anchor is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "还没到日盘或夜盘的开盘，先看")
        if not scope_allows(self.session_scope, now):
            return StrategyDecision(StrategyAction.HOLD, symbol, scope_block_reason(self.session_scope))
        if not self.in_entry_session(now, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "不在允许交易的时段，不开")

        # 行情过滤跟时间走：每一轮用当前这串 K 线重算 ADX/ATR。
        # 刚才还在拧拦过，过一会儿确认扩张了还可以开；不是开盘判一次就锁死。
        # 已有持仓不受影响，只拦新开仓。开关关了这一层就不看。
        state = self.market_filter.check(bars)
        if not state.ok:
            return StrategyDecision(
                StrategyAction.HOLD, symbol, f"行情不匹配：{state.reason}"
            )

        bar_id = str(bars.index[-1])
        count = self._count_for(symbol, session)
        limit = self._session_limit(anchor, symbol)
        if count >= limit:
            if self._linked():
                text = "夜日连续这段已经开满，不再开"
            elif limit >= 2:
                text = "日盘这段已经开满两次，不再开"
            elif self._is_night_anchor(anchor, symbol):
                text = "夜盘只开一次，这段已经开过，不再开第二次"
            else:
                text = "第二ETH窗口已关闭，这段不再开第二次"
            return StrategyDecision(StrategyAction.HOLD, symbol, text)
        if (
            self._linked()
            and self._is_night_anchor(anchor, symbol)
            and count >= 1
        ):
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                "夜盘ETH窗口已经开过，等到日盘再按日盘箱子判断",
            )
        second = count > 0
        if second:
            if not self._stop_closed.get(symbol):
                return StrategyDecision(
                    StrategyAction.HOLD, symbol, "第一次不是打破箱沿止损出场，这段不再开第二次"
                )
            if self._used_bar.get(symbol) == bar_id:
                return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经判断过，先看")
        # 未过第二窗口：只判第一口箱子。刚过第二窗口：丢掉第一窗口留下的急破等待。
        # 已经在第二窗口里记下的急破等待还保留，否则回抽确认会被误清掉。
        eth, eth_kind = self._eth_plan(symbol, count, now, anchor)
        first_kinds = {"first", "night", "eth1"}
        if (
            eth_kind not in first_kinds
            and prior_wait
            and prior_wait.get("eth_kind", "first") in first_kinds
        ):
            self._chase_wait.pop(symbol, None)
            prior_wait = None
        start = self._eth_end(anchor, eth)
        end = self.night_cutoff if self._is_night_anchor(anchor, symbol) else self.day_cutoff
        now_clock = _naive(now)
        eth_done = last_open_at(now_clock, anchor) + timedelta(minutes=max(0, int(eth)))
        if now_clock <= eth_done:
            which = "第二ETH窗口" if eth_kind not in {"first", "night", "eth1"} else "第一ETH窗口"
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"{which} 到 {start.strftime('%H:%M')} 才走完（从开盘起算），先把箱子看完",
            )
        if cutoff_passed(now_clock, anchor, end):
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"已经过了 {end.strftime('%H:%M')}，这段不再追",
            )

        del trend_bars
        # 定方向交给可替换的钩子：日内实体看箱子突破，浮盈飘单换成自己的方向选择器。
        side, text = self._direction_signal(
            bars, now, anchor, eth, symbol=symbol, second=second
        )
        if side == "long":
            action = StrategyAction.BUY_OPEN
        elif side == "short":
            action = StrategyAction.SELL_OPEN
        else:
            return StrategyDecision(StrategyAction.HOLD, symbol, text)

        # 跳空过滤只管这一段的第一枪；第二次开仓发生在盘中，不再按开盘跳空拦。
        # ATR 用开盘前的信号周期，倍数 0 表示关掉。
        if not second and self.skip_gap_atr_mult > 0:
            gap = self._session_gap(bars, now, anchor)
            atr = self._pre_open_atr(bars, now, anchor)
            if gap is not None and atr is not None:
                gap_limit = self.skip_gap_atr_mult * atr
                if abs(gap[0]) > gap_limit:
                    return StrategyDecision(
                        StrategyAction.HOLD,
                        symbol,
                        f"{text}。但这一段开盘相对上一段收盘跳空 {gap[0]:+.0f} 点"
                        f"（{abs(gap[0]) / atr:.1f} 倍ATR，上限 {self.skip_gap_atr_mult:.1f} 倍"
                        f" = {limit:.0f} 点），缺口回补时箱沿止损会被一次打穿，这段不追",
                        meta={"blocked": True, "side": side, "block_key": "gap_too_big"},
                    )

        aligned = self._gap_align_block(symbol, side, bars, now, anchor, text, second)
        if aligned is not None:
            return aligned

        use_box = self._uses_opening_box()
        box_high = 0.0
        box_low = 0.0
        if use_box:
            high_raw, low_raw, wait = opening_box_levels(
                bars, now, anchor, eth, self.timeframe
            )
            if high_raw is None or low_raw is None:
                return StrategyDecision(
                    StrategyAction.HOLD, symbol, wait or "开盘箱体画不出来，先不开"
                )
            box_high, box_low = high_raw, low_raw

        if use_box and self.max_box_atr_mult > 0:
            width = float(box_high) - float(box_low)
            atr = self._pre_open_atr(bars, now, anchor)
            if atr is not None:
                box_limit = self.max_box_atr_mult * atr
                if width > box_limit:
                    return StrategyDecision(
                        StrategyAction.HOLD,
                        symbol,
                        f"{text}。但开盘箱体 {box_low:.2f} 到 {box_high:.2f}，宽 {width:.0f} 点"
                        f"（{width / atr:.1f} 倍ATR，上限 {self.max_box_atr_mult:.1f} 倍"
                        f" = {limit:.0f} 点），箱沿止损会跟着变宽，这段不开",
                        meta={"blocked": True, "side": side, "block_key": "box_too_wide"},
                    )

        entry = last_ohlc_price(bars, "close")
        if entry is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, "没有可用的收盘价，先不开")
        if not use_box:
            tick_pad = self.price_tick * self.stop_extra_ticks
            atr = self._pre_open_atr(bars, now, anchor)
            stop = self._open_stop(side, box_low, box_high, tick_pad, entry=entry, atr=atr)
            distance = abs(entry - stop)
            self._pending_session[symbol] = session
            self._pending_bar[symbol] = bar_id
            self._pending_eth[symbol] = eth
            self._pending_kind[symbol] = eth_kind
            stop_text = self._open_stop_text(side, box_low, box_high, tick_pad)
            reason = (
                f"{text}。结构止损 {stop:.2f}，离进场价 {distance:.2f}，{stop_text}。"
                f"中间不再做回撤止损，顺着这个方向拿到收盘"
            )
            logger.info("{} {}", symbol, reason)
            return StrategyDecision(
                action,
                symbol,
                reason,
                stop_price=stop,
                entry_price=entry,
                meta={
                    "signal": self.SIGNAL_NAME,
                    "bar_id": bar_id,
                    "session": session,
                    "side": side,
                    "stop_kind": "atr",
                },
            )
        # 急破不追：信号这根离破位太远，进场等于把箱沿止损拉得很宽。
        # 先记下这一段的方向和破位点，等价格回到破位允许范围内再按原方向开。
        level = float(box_low if side == "short" else box_high)
        edge_name = "下沿" if side == "short" else "上沿"
        chase_dist = abs(entry - level)
        same_wait = bool(
            prior_wait
            and prior_wait.get("session") == session
            and prior_wait.get("side") == side
            and abs(float(prior_wait.get("level", 0.0)) - level) < 1e-9
        )
        if self.max_chase_atr_mult > 0:
            atr = self._pre_open_atr(bars, now, anchor)
            if atr is not None:
                chase_limit = self.max_chase_atr_mult * atr
                if chase_dist > chase_limit:
                    self._chase_wait[symbol] = {
                        "session": session,
                        "side": side,
                        "level": level,
                        "eth_kind": eth_kind,
                    }
                    return StrategyDecision(
                        StrategyAction.HOLD,
                        symbol,
                        f"{text}。但收盘离箱体{edge_name} {level:.2f} 已经有 {chase_dist:.0f} 点"
                        f"（{chase_dist / atr:.1f} 倍ATR，上限 {self.max_chase_atr_mult:.1f} 倍），急破不追；"
                        f"等价格回到破位 {chase_limit:.0f} 点内再按这个方向开",
                        meta={"blocked": True, "side": side, "block_key": "chase_too_far"},
                    )
        retest_note = ""
        if same_wait:
            ema = self._ema_close(bars, now)
            buffer = self.ema_buffer_ticks * self.price_tick
            if ema is not None:
                broken = (
                    (side == "short" and entry > ema + buffer)
                    or (side == "long" and entry < ema - buffer)
                )
                if broken:
                    verb = "站上" if side == "short" else "跌破"
                    return StrategyDecision(
                        StrategyAction.HOLD,
                        symbol,
                        f"{text}。回抽到破位附近了，但收盘已经有效{verb} EMA{self.ema_period}"
                        f"（{ema:.2f}），这次突破作废，先不开",
                        meta={"blocked": True, "side": side, "block_key": "ema_retest"},
                    )
                retest_note = (
                    f"回抽确认：离箱体{edge_name} {chase_dist:.0f} 点，"
                    f"收盘没有有效突破 EMA{self.ema_period}（{ema:.2f}）。"
                )
        tick_pad = self.price_tick * self.stop_extra_ticks
        atr = self._pre_open_atr(bars, now, anchor)
        stop = self._open_stop(side, box_low, box_high, tick_pad, entry=entry, atr=atr)
        distance = abs(entry - stop)
        # 先记下「如果成交，占哪一段」；未成交不写入 _used_session。
        self._pending_session[symbol] = session
        self._pending_bar[symbol] = bar_id
        self._pending_eth[symbol] = eth
        self._pending_kind[symbol] = eth_kind
        stop_text = self._open_stop_text(side, box_low, box_high, tick_pad)
        reason = (
            f"{retest_note}{text}。结构止损 {stop:.2f}，离进场价 {distance:.2f}，{stop_text}。"
            f"中间不再做回撤止损，顺着捅破方向拿到收盘"
        )
        if eth_kind == "stop":
            reason = (
                f"第二ETH窗口开仓（第一窗口止损后，箱子从开盘起算{eth}分钟）：{reason}"
            )
        elif eth_kind == "idle":
            reason = (
                f"已过第二ETH窗口，丢掉第一窗口判断，改用从开盘起{eth}分钟的箱子：{reason}"
            )
        elif eth_kind == "night":
            reason = f"夜盘ETH窗口开仓：{reason}"
        elif eth_kind == "eth1":
            reason = f"日盘第一ETH窗口开仓：{reason}"
        elif eth_kind == "eth2":
            reason = f"日盘第二ETH窗口开仓：{reason}"
        logger.info("{} {}", symbol, reason)
        return StrategyDecision(
            action,
            symbol,
            reason,
            stop_price=stop,
            entry_price=entry,
            meta={
                "signal": self.SIGNAL_NAME,
                "bar_id": bar_id,
                "session": session,
                "side": side,
                "stop_kind": "box",
                "box_high": box_high,
                "box_low": box_low,
            },
        )
