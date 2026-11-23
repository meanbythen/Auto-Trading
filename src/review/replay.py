"""
把程序里的策略在「这一天」上重放一遍
功能：回答「按现在这套参数，今天会开仓还是观望，原因是什么」。
原理：
  和回测同一套 evaluate：每根已收盘K调用一次。
  只统计落在日盘/夜盘窗口里的决定。
  假设这段开始时是空仓，这样「观望」就是今天没有新开仓，而不是在管昨天的老单。
  选择「只看盘面」时不调用任何策略。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
from loguru import logger

from src.backtest.engine import _adx_at, _adx_for_eval, _trend_frame_for_signal
from src.backtest.evaluation import EvaluationThresholds
from src.review.session import SessionWindow
from src.review.technical import as_naive_datetime
from src.strategy import StrategyAction, create_strategy
from src.strategy.engine_labels import (
    ENGINE_BY_LABEL as ENGINE_BY_LABEL,
    LABEL_BY_ENGINE as LABEL_BY_ENGINE,
    NO_STRATEGY_LABEL as NO_STRATEGY_LABEL,
    engine_from_label as engine_from_label,
    label_for_engine as label_for_engine,
)
from src.strategy.types import PositionState


REPLAY_COST_NOTE = (
    "复盘口径：按价格点数估算开平仓，不含手续费、滑点与合约乘数；"
    "净盈亏与回测/实盘金额口径不可直接相加。"
)


@dataclass
class ReplayEvent:
    """一次开仓或平仓。"""

    time: str
    action: str
    price: float
    reason: str
    # 完整时间戳，给质量分析配对 MAE/MFE 用；界面句子仍用 time
    when: object | None = None
    # 开仓时的止损价；平仓事件没有
    stop: float | None = None
    # 开仓时评估用的 ADX，复盘评估据此分趋势/震荡组
    adx: float | None = None


@dataclass
class StrategyReplay:
    """策略对照的全部结论，已经是可以写进报告的句子。"""

    label: str
    engine: str | None
    lines: list[str] = field(default_factory=list)
    opened: bool = False
    # long / short / flat：综合结论用来和盘面方向对照
    direction: str = "flat"
    main_watch_reason: str = ""
    # 结构化开平仓，供信号质量 / 缺陷诊断使用（报告正文仍读 lines）
    open_events: list = field(default_factory=list)
    close_events: list = field(default_factory=list)
    window_end: object | None = None
    # P4 统一运行清单：复盘也留 run_id/trace_id/策略与数据版本。
    run: dict = field(default_factory=dict)


def _finish_replay(
    result: StrategyReplay,
    cfg: dict,
    *,
    symbol: str,
    engine: str | None,
    started_perf: float,
    started_at: str,
) -> StrategyReplay:
    """补统一运行清单并落库；失败只记日志，不影响复盘结果。"""
    from src.research.kernel import build_run_manifest, record_run

    period = str((cfg.get("strategy") or {}).get("timeframe") or "")
    try:
        result.run = build_run_manifest(
            "review",
            cfg,
            symbols=[symbol],
            period=period,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started_perf) * 1000.0,
            metrics={
                "events": len(result.open_events) + len(result.close_events),
                "opened": bool(result.opened),
                "direction": result.direction,
            },
            extra={"engine": engine},
        )
        record_run(result.run, root=cfg.get("_project_root"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("复盘运行清单没写上: {}", exc)
    return result


def coarse_reason(reason: str) -> str:
    """
    把「ETH时间 09:00-09:30，观察中，约剩 12 分钟」收成同一类，方便数出现了几次。
    原理：去掉数字和价格，只留原因类别。
    """
    text = str(reason or "没有写原因").split("|")[0].strip()
    text = re.sub(r"\d+(\.\d+)?", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ，,。")
    return (text[:42] or "没有写原因")


def replay_strategy(
    cfg: dict,
    *,
    symbol: str,
    engine: str | None,
    bars_signal: pd.DataFrame | None,
    bars_trail: pd.DataFrame | None,
    windows: list[SessionWindow],
    pin_count: int,
    bars_trend: pd.DataFrame | None = None,
) -> StrategyReplay:
    """
    在给定K线上重放。

    参数:
        engine: None 表示用户选了「只看盘面」
        bars_signal: 含复盘日之前的历史，指标才算得出来
        pin_count: 技术面数到的针形线数量，用来解释「盘面有形态但策略没做」
        bars_trend: 对照策略用的日线。别的策略不用，可以不传

    价格口径：开仓按策略给出的信号价/当根收盘，平仓若碰到止损按止损价，否则按收盘价；
    都不含滑点和挂单排队。这里回答的是「会不会做、为什么做」，不是真实成交价。
    """
    label = label_for_engine(engine)
    result = StrategyReplay(label=label, engine=engine)
    started_perf = time.perf_counter()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if engine is not None:
        from src.strategy.profile import header_line

        profile = header_line(engine, cfg)
        if profile:
            result.lines.append(profile)
    if engine is None:
        result.lines.append("这次没有套用程序里的任何策略，所以不会出现「策略开仓」或「策略观望」。")
        result.lines.append("上面的针形线、均线、关键位只是盘面现象。要不要做，对照你自己的交易计划，而不是对照某段代码。")
        if pin_count:
            result.lines.append(f"盘面上有 {pin_count} 根针形线。它们本身不是开仓指令。")
        return _finish_replay(
            result, cfg, symbol=symbol, engine=engine,
            started_perf=started_perf, started_at=started_at,
        )

    if bars_signal is None or len(bars_signal) < 40:
        n = 0 if bars_signal is None else len(bars_signal)
        result.lines.append(f"K线只有 {n} 根，策略需要更多历史才能判断，这次没法重放。")
        return _finish_replay(
            result, cfg, symbol=symbol, engine=engine,
            started_perf=started_perf, started_at=started_at,
        )

    covered = _bars_inside(bars_signal, windows)
    if covered == 0:
        result.lines.append("这段没有K线，策略没有可判断的收盘价。")
        return _finish_replay(
            result, cfg, symbol=symbol, engine=engine,
            started_perf=started_perf, started_at=started_at,
        )

    opens, closes, watch, holding = _walk(
        cfg,
        symbol=symbol,
        engine=engine,
        bars_signal=bars_signal,
        bars_trail=bars_trail,
        windows=windows,
        bars_trend=bars_trend,
    )
    result.open_events = list(opens)
    result.close_events = list(closes)
    if windows:
        result.window_end = max(w.end for w in windows)
    result.lines.append(
        f"按「{label}」重放。假设这段开始时是空仓，用的是配置页里的时段、ETH 和形态参数，"
        "周期用复盘页上选的。这里只看策略会不会发出信号，不算手数、保证金和日亏损。"
        "价格按信号价或当根收盘估算（止损触发按止损价），不含滑点、盘口和挂单排队，"
        "是信号演示，不是实际执行价。"
    )
    result.lines.append(f"窗口内一共看了 {covered} 根已收盘K线。")
    result.lines.append(REPLAY_COST_NOTE)

    if opens:
        result.opened = True
        sides = {ev.action for ev in opens}
        if sides == {"开多"}:
            result.direction = "long"
        elif sides == {"开空"}:
            result.direction = "short"
        else:
            result.direction = "mixed"
        result.lines.append(f"会开仓 {len(opens)} 次：")
        for ev in opens:
            stop_bit = ""
            if ev.stop not in (None, 0, 0.0):
                stop_bit = f"，止损约 {float(ev.stop):.0f}"
            result.lines.append(
                f"  · {ev.time} {ev.action}，价格约 {ev.price:.0f}{stop_bit}。原因：{ev.reason}"
            )
    else:
        result.lines.append("全段没有开仓，也就是按这套策略应该观望。")
        if watch:
            reason, times = watch.most_common(1)[0]
            result.main_watch_reason = reason
            result.lines.append(f"观望时出现最多的原因：{reason}（{times} 根K线）。")
            others = watch.most_common(4)[1:]
            if others:
                extra = "；".join(f"{name} {n}次" for name, n in others)
                result.lines.append(f"其他原因：{extra}。")
        else:
            result.lines.append("没有留下观望原因（可能K线都在持仓管理里，或数据太短）。")
        if pin_count:
            result.lines.append(
                f"技术面看到 {pin_count} 根针形线，策略仍然没开仓。"
                "程序不是看见针就做：还要确认K、关键位、量能和允许开仓的时段同时满足。"
            )

    if closes:
        result.lines.append(f"期间平仓 {len(closes)} 次：")
        for ev in closes:
            result.lines.append(f"  · {ev.time} 平仓，价格约 {ev.price:.0f}。原因：{ev.reason}")
    if holding and opens:
        reason, times = holding.most_common(1)[0]
        result.lines.append(f"开仓之后拿单时，说得最多的是：{reason}（{times} 根）。")
    return _finish_replay(
        result, cfg, symbol=symbol, engine=engine,
        started_perf=started_perf, started_at=started_at,
    )


def _bars_inside(bars: pd.DataFrame, windows: list[SessionWindow]) -> int:
    n = 0
    for ts in bars.index:
        now = as_naive_datetime(ts)
        if any(w.contains(now) for w in windows):
            n += 1
    return n


def _walk(
    cfg: dict,
    *,
    symbol: str,
    engine: str,
    bars_signal: pd.DataFrame,
    bars_trail: pd.DataFrame | None,
    windows: list[SessionWindow],
    bars_trend: pd.DataFrame | None = None,
) -> tuple[list[ReplayEvent], list[ReplayEvent], Counter, Counter]:
    """
    逐根推进。窗口外的K线只当作历史，不在那里开仓。
    窗口内的开平仓会记下来，并真的改策略持仓，这样同一套确认K不会连开两次。
    """
    import copy

    local = copy.deepcopy(cfg)
    st = local.setdefault("strategy", {})
    st["engine"] = engine
    st["symbols"] = [symbol]
    local.setdefault("trade", {})["symbol"] = symbol

    strategy = create_strategy(local)
    strategy.positions[symbol] = PositionState(symbol=symbol)
    trend_tf = str(getattr(strategy, "trend_timeframe", "") or "")
    strategy.set_last_entry_bar(symbol, "")
    # 和回测同一套 ADX 口径，复盘评估才能分趋势/震荡组。
    adx_values = _adx_for_eval(bars_signal, EvaluationThresholds.from_cfg(cfg).adx_period)

    opens: list[ReplayEvent] = []
    closes: list[ReplayEvent] = []
    watch: Counter = Counter()
    holding: Counter = Counter()

    modules = (
        "src.strategy.daily_body",
        "src.strategy.trend_follow",
        "src.strategy.supertrend",
        "src.strategy.dual_ma",
        "src.strategy.ema_swing",
        "src.strategy.swing_trend",
        "src.strategy.swing_box",
        "src.strategy.dual_donchian",
        "src.strategy.dual_fractal",
    )
    for mod in modules:
        logger.disable(mod)
    try:
        n = len(bars_signal)
        # 从第一根看起。窗口外的K线只是历史，不在那里开仓；
        # 窗口内如果历史还不够，策略自己会回答「K线不足」。
        for i in range(n):
            signal_df = bars_signal.iloc[: i + 1]
            now = as_naive_datetime(signal_df.index[-1])
            in_window = any(w.contains(now) for w in windows)
            trail_df = None
            if bars_trail is not None and len(bars_trail) > 0:
                trail_df = bars_trail[bars_trail.index <= signal_df.index[-1]]
            # 大周期只切到决策时已经走完的部分。日线标签是当天 00:00，
            # 盘中若带上当天这根，就等于看见了当天收盘价。
            trend_df = _trend_frame_for_signal(bars_trend, signal_df.index[-1], trend_tf)
            if not in_window:
                continue
            if getattr(strategy, "uses_trend_bars", False):
                decision = strategy.evaluate(
                    symbol, signal_df, now=now, trail_bars=trail_df, trend_bars=trend_df
                )
            else:
                decision = strategy.evaluate(symbol, signal_df, now=now, trail_bars=trail_df)
            price = float(signal_df["close"].iloc[-1])
            pos_before = strategy.positions.get(symbol)
            stop_before = 0.0
            if pos_before is not None and pos_before.side != "flat":
                try:
                    stop_before = float(pos_before.trail_stop or pos_before.initial_stop or 0)
                except (TypeError, ValueError):
                    stop_before = 0.0
            # time 给人看；stamp 给质量分析配对用（字段名 when，避免和字符串抢名字）
            time_label = now.strftime("%m-%d %H:%M")
            if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
                pos = strategy.positions.get(symbol)
                if pos and pos.side != "flat":
                    continue
                entry = float(decision.entry_price or price)
                strategy.on_filled(
                    symbol,
                    decision.action,
                    entry,
                    int(getattr(strategy, "volume", 1) or 1),
                    initial_stop=decision.stop_price,
                )
                bar_id = str((decision.meta or {}).get("bar_id", ""))
                if bar_id:
                    strategy.set_last_entry_bar(symbol, bar_id)
                side = "开多" if decision.action == StrategyAction.BUY_OPEN else "开空"
                stop_px = decision.stop_price
                try:
                    stop_val = float(stop_px) if stop_px is not None else None
                except (TypeError, ValueError):
                    stop_val = None
                opens.append(
                    ReplayEvent(
                        time_label,
                        side,
                        entry,
                        decision.reason,
                        when=now,
                        stop=stop_val,
                        adx=_adx_at(adx_values, i),
                    )
                )
            elif decision.action == StrategyAction.CLOSE:
                pos = strategy.positions.get(symbol)
                if not pos or pos.side == "flat":
                    continue
                # 止损被打掉时按止损价估算；这是这根之前已知的止损线，
                # 本根新收紧的线下一根才生效。没碰到止损就按收盘价估算。
                fill_price = price
                bar = signal_df.iloc[-1]
                if stop_before > 0:
                    hit = (
                        pos.side == "long" and float(bar["low"]) <= stop_before
                    ) or (
                        pos.side == "short" and float(bar["high"]) >= stop_before
                    )
                    if hit:
                        fill_price = stop_before
                strategy.on_filled(
                    symbol, StrategyAction.CLOSE, fill_price, int(pos.volume or 1)
                )
                closes.append(
                    ReplayEvent(time_label, "平仓", fill_price, decision.reason, when=now)
                )
            else:
                reason = coarse_reason(decision.reason)
                pos = strategy.positions.get(symbol)
                if pos and pos.side != "flat":
                    holding[reason] += 1
                else:
                    watch[reason] += 1
    finally:
        for mod in modules:
            logger.enable(mod)
    return opens, closes, watch, holding
