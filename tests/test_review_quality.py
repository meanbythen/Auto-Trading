"""复盘升级：信号质量、成绩单、缺陷诊断（不访问外网）。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd

from src.review.baseline import save_review_summary, build_baseline_report
from src.review.diagnose import diagnose
from src.review.gap import GapReport
from src.review.live_eval import build_live_vs_backtest, build_replay_eval
from src.review.quality import (
    QualityReport,
    SignalTrade,
    build_quality_report,
    enrich_round_trips,
)
from src.review.replay import ReplayEvent, StrategyReplay
from src.review.scorecard import Scorecard
from src.review.service import build_review
from src.review.technical import TechnicalView


def _day_bars(day: date, *, start_price: float, step: float) -> pd.DataFrame:
    times = []
    cursor = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=0)
    end = datetime.combine(day, datetime.min.time()).replace(hour=15, minute=0)
    while cursor <= end:
        times.append(cursor)
        cursor += timedelta(minutes=15)
    rows = []
    price = start_price
    for _ in times:
        o = price
        c = price + step
        rows.append(
            {
                "open": o,
                "high": max(o, c) + 5,
                "low": min(o, c) - 3,
                "close": c,
                "volume": 100,
            }
        )
        price = c
    return pd.DataFrame(rows, index=pd.DatetimeIndex(times))


def test_mae_mfe_for_long_round_trip() -> None:
    """多单：持仓期间低点算 MAE，高点算 MFE，R 用止损距离。"""
    day = date(2026, 10, 14)
    bars = _day_bars(day, start_price=3000, step=2)
    entry_t = datetime(2026, 10, 14, 9, 30)
    exit_t = datetime(2026, 10, 14, 11, 0)
    opens = [
        ReplayEvent(
            "10-14 09:30",
            "开多",
            3002.0,
            "测试开多",
            when=entry_t,
            stop=2990.0,
            adx=24.0,
        )
    ]
    closes = [
        ReplayEvent("10-14 11:00", "平仓", 3020.0, "测试平仓", when=exit_t)
    ]
    trades = enrich_round_trips(opens, closes, bars, window_end=datetime(2026, 10, 14, 15, 0))
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "long"
    assert t.adx_at_entry == 24.0
    assert t.mfe > 0
    assert t.mae >= 0
    assert t.r_multiple is not None
    report = build_quality_report(trades)
    assert "MAE" in "\n".join(report.lines)
    assert report.closed_count == 1


def test_giveback_defect_is_flagged() -> None:
    """捕获率很低时，诊断应标出利润回吐。"""
    trade = SignalTrade(
        side="long",
        entry_time=datetime(2026, 10, 14, 10, 0),
        exit_time=datetime(2026, 10, 14, 11, 0),
        entry_price=100.0,
        exit_price=102.0,
        stop_price=95.0,
        still_open=False,
        mae=2.0,
        mfe=20.0,
        pnl_points=2.0,
        risk_points=5.0,
        r_multiple=0.4,
        capture_ratio=0.10,
    )
    quality = build_quality_report([trade])
    replay = StrategyReplay(label="测试", engine="daily_body", opened=True, direction="long")
    tech = TechnicalView(name="日盘", has_bars=True, bias="up", character="偏强", pin_count=0)
    gap = GapReport(alignment="matched", lines=["ok"])
    result = diagnose(technicals=[tech], replay=replay, quality=quality, gap=gap)
    assert "利润回吐过多" in result.defects
    assert result.improvements


def test_scorecard_and_full_report_sections(tmp_path) -> None:
    """整篇报告含成绩单和新章节；保存 json 摘要。"""
    day = date(2026, 10, 14)
    prev = _day_bars(date(2026, 10, 13), start_price=2800, step=1)
    today = _day_bars(day, start_price=3100, step=3)
    bars = pd.concat([prev, today])
    cfg = {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "daily_body": {
                "eth_minutes": 0,
            },
        },
        "trade": {"symbol": "bu2612", "volume": 1, "executor": "sim"},
        "risk": {"capital": 1_000_000},
        "data": {"source": "demo"},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
    }
    text = build_review(
        cfg,
        symbol="bu2612",
        trade_date=day,
        scope="日盘",
        engine="daily_body",
        bars_signal=bars,
        bars_trail=bars,
        now=datetime(2026, 10, 14, 16, 0),
        fetch_remote=False,
        save=True,
        root=tmp_path,
    )
    assert "【当日成绩单】" in text
    assert "五、信号质量" in text
    assert "七、信号与实盘对照" in text
    assert "八、缺陷诊断" in text
    assert "九、相对基准" in text
    assert "十、当日重放评估" in text
    assert "十一、最近实盘 vs 最近回测" in text
    assert "十二、综合" in text
    summary = tmp_path / "data" / "reviews" / "2026-10-14_bu2612.json"
    assert summary.exists()


def test_baseline_reads_peer_summaries(tmp_path) -> None:
    """近几日摘要能被基准模块读到。"""
    card = Scorecard(effectiveness=80, stability=70, credibility=75, summary="好")

    q = QualityReport(avg_r=0.5, closed_count=2, win_count=1, trades=[])
    save_review_summary(
        tmp_path,
        symbol="bu2612",
        trade_date=date(2026, 10, 13),
        engine="daily_body",
        scope="日盘",
        scorecard=card,
        quality=q,
        defects=[],
        version="2.37.0",
    )
    today_card = Scorecard(effectiveness=50, stability=60, credibility=70, summary="一般")
    report = build_baseline_report(
        tmp_path,
        symbol="bu2612",
        engine="daily_body",
        trade_date=date(2026, 10, 14),
        scorecard=today_card,
        quality=q,
    )
    assert any("近" in line and "平均" in line for line in report.lines)


def test_replay_eval_uses_adx_from_replay() -> None:
    """开仓 ADX 带进复盘评估后，震荡组检查不再永远跳过。"""
    trades = [
        SignalTrade(
            side="long",
            entry_time=datetime(2026, 10, 14, 9, 30),
            exit_time=datetime(2026, 10, 14, 10, 30),
            entry_price=100.0,
            exit_price=102.0,
            stop_price=98.0,
            pnl_points=2.0,
            adx_at_entry=28.0,
        ),
        SignalTrade(
            side="long",
            entry_time=datetime(2026, 10, 14, 13, 30),
            exit_time=datetime(2026, 10, 14, 14, 30),
            entry_price=100.0,
            exit_price=102.0,
            stop_price=98.0,
            pnl_points=2.0,
            adx_at_entry=14.0,
        ),
    ]
    lines = build_replay_eval(QualityReport(trades=trades), {})
    text = "\n".join(lines)
    assert "震荡组" in text
    assert "跳过" not in text.split("震荡组", 1)[1].split("\n", 1)[0]


def test_live_vs_backtest_shows_new_metrics(tmp_path) -> None:
    """实盘对照要带出最近回测的 R 期望、卡尔玛和最大连亏。"""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "backtest_last.json").write_text(
        json.dumps(
            {
                "performance": {"trades": 120, "win_rate": 0.5, "profit_factor": 1.4},
                "evaluation": {
                    "conclusion_text": "观察",
                    "failed_items": [],
                    "metrics": {
                        "r_expectancy": 0.12,
                        "calmar": 0.85,
                        "consecutive_losses": 6,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    text = "\n".join(build_live_vs_backtest(tmp_path, "bu2612", {}))

    assert "最近回测评估  观察" in text
    assert "收益期望 0.12" in text
    assert "卡尔玛 0.85" in text
    assert "最大连亏 6笔" in text
