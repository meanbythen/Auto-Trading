"""
复盘总流程
功能：拉K线 → 分时段看盘 → 拉基本面 → 重放策略 → 信号质量 → 对照真实日志与账户 →
      缺陷诊断 → 成绩单 → 近几日/回测基准 → 写成一篇报告。
原理：每一步失败都尽量留下人话原因。报告存 txt；成绩单摘要另存 json，方便以后几天对照。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
from loguru import logger

from src.data.kline import KlineProvider
from src.review.actual import actual_lines
from src.review.baseline import build_baseline_report, save_review_summary
from src.review.diagnose import diagnose
from src.review.fundamental import collect_fundamentals
from src.review.gap import build_gap_report
from src.review.quality import build_quality_report, enrich_round_trips
from src.review.replay import replay_strategy
from src.review.report import render_report
from src.review.scorecard import build_scorecard
from src.review.session import SCOPE_BOTH, windows_for
from src.review.technical import _naive_index, analyze_window


def build_review(
    cfg: dict,
    *,
    symbol: str,
    trade_date: date,
    scope: str = SCOPE_BOTH,
    engine: str | None = None,
    user_note: str = "",
    bars_signal: pd.DataFrame | None = None,
    bars_trail: pd.DataFrame | None = None,
    now: datetime | None = None,
    fetch_remote: bool = True,
    save: bool = True,
    root: Path | str | None = None,
) -> str:
    """
    生成一篇复盘并返回正文。

    参数:
        trade_date: 要回顾的交易日（日盘那一天）
        scope: 日盘 / 夜盘 / 前夜盘+日盘
        engine: 策略引擎名；None 表示只看盘面
        user_note: 你自己贴的当日消息
        bars_signal / bars_trail: 测试时可直接传入，跳过联网拉K线
        fetch_remote: 是否去拉库存、基差、快讯
        save: 是否写入 data/reviews（txt + json 摘要）
    """
    root_path = Path(root or cfg.get("_project_root") or ".")
    now = now or datetime.now()
    windows = windows_for(trade_date, scope)
    logger.info(
        "开始复盘 symbol={} date={} scope={} engine={}",
        symbol,
        trade_date.isoformat(),
        scope,
        engine or "只看盘面",
    )

    signal, trail = _load_bars(
        cfg,
        symbol=symbol,
        engine=engine,
        bars_signal=bars_signal,
        bars_trail=bars_trail,
    )
    technicals = [analyze_window(w.name, signal, w) for w in windows]
    pin_count = sum(v.pin_count for v in technicals)

    fundamental = collect_fundamentals(
        symbol,
        trade_date,
        user_note,
        fetch_remote=fetch_remote,
    )
    # 开仓不再用日线定方向。这里留着空的日线，避免旧的复盘调用还去联网。
    trend = None
    replay = replay_strategy(
        cfg,
        symbol=symbol,
        engine=engine,
        bars_signal=signal,
        bars_trail=trail,
        windows=windows,
        pin_count=pin_count,
        bars_trend=trend,
    )

    # 信号质量：用当天真实高低点扫持仓路径
    window_end = max((w.end for w in windows), default=None)
    trades = enrich_round_trips(
        getattr(replay, "open_events", []) or [],
        getattr(replay, "close_events", []) or [],
        signal,
        window_end=window_end,
    )
    quality = build_quality_report(trades)

    actual = actual_lines(root_path, symbol, windows)
    gap = build_gap_report(
        root_path,
        symbol=symbol,
        windows=windows,
        trades=trades,
        actual_event_lines=actual,
    )
    diagnosis = diagnose(
        technicals=technicals,
        replay=replay,
        quality=quality,
        gap=gap,
    )
    scorecard = build_scorecard(
        technicals=technicals,
        replay=replay,
        quality=quality,
        gap=gap,
        diagnosis=diagnosis,
        windows=windows,
        now=now,
    )
    baseline = build_baseline_report(
        root_path,
        symbol=symbol,
        engine=engine,
        trade_date=trade_date,
        scorecard=scorecard,
        quality=quality,
    )

    text = render_report(
        symbol=symbol,
        trade_date=trade_date,
        scope=scope,
        windows=windows,
        technicals=technicals,
        fundamental=fundamental,
        replay=replay,
        actual=actual,
        quality=quality,
        gap=gap,
        diagnosis=diagnosis,
        scorecard=scorecard,
        baseline=baseline,
        now=now,
    )
    if save:
        out = _save(root_path, symbol, trade_date, text)
        try:
            from src.version import read_version

            ver = read_version()
        except Exception:  # noqa: BLE001
            ver = ""
        summary_path = save_review_summary(
            root_path,
            symbol=symbol,
            trade_date=trade_date,
            engine=engine,
            scope=scope,
            scorecard=scorecard,
            quality=quality,
            defects=diagnosis.defects,
            version=ver,
        )
        text = text + f"\n报告已保存：{out}\n成绩单摘要：{summary_path}\n"
        logger.info("复盘已写入 {} / {}", out, summary_path)
    return text


def _load_bars(
    cfg: dict,
    *,
    symbol: str,
    engine: str | None,
    bars_signal: pd.DataFrame | None,
    bars_trail: pd.DataFrame | None,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """优先用调用方传入的K线；否则按回测同样的长度去拉，保证当天之前有历史。"""
    if bars_signal is not None:
        signal = _naive_index(bars_signal)
        trail = _naive_index(bars_trail) if bars_trail is not None else None
        return signal, trail

    local = dict(cfg)
    if engine:
        local = {**cfg, "strategy": {**(cfg.get("strategy") or {}), "engine": engine}}
    provider = KlineProvider(local)
    st = local.get("strategy") or {}
    tf = str(st.get("timeframe", "15"))
    trail_tf = str(st.get("trail_timeframe", "30"))
    max_bars = int((local.get("backtest") or {}).get("max_bars", 1200))
    try:
        signal = provider.fetch(symbol, period=tf, max_bars=max_bars, keep_history=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("复盘拉取信号K线失败: {}", e)
        return None, None
    signal = _naive_index(signal) if signal is not None else None
    trail = None
    if trail_tf != tf:
        try:
            raw_trail = provider.fetch(symbol, period=trail_tf, max_bars=max_bars, keep_history=True)
            trail = _naive_index(raw_trail) if raw_trail is not None else None
        except Exception as e:  # noqa: BLE001
            logger.info("复盘止损周期K线没有拉到，策略只使用信号周期: {}", e)
    return signal, trail


def _save(root: Path, symbol: str, trade_date: date, text: str) -> Path:
    folder = root / "data" / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    safe_symbol = "".join(ch for ch in symbol if ch.isalnum() or ch in ("_", "-")) or "symbol"
    path = folder / f"{trade_date.isoformat()}_{safe_symbol}.txt"
    path.write_text(text, encoding="utf-8")
    return path
