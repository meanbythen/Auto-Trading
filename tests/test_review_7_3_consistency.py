"""审查报告 §7.3：回测与实盘一致性口径。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from src.backtest.engine import BacktestLimits, _net_pnl_after_fees, run_backtest
from src.data.periods import drop_forming_bar
from src.review.quality import SignalTrade, build_quality_report


def _daily_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2026-11-18", "2026-11-19", "2026-11-20"])
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [10.0, 10.0, 10.0],
        },
        index=index,
    )


def test_daily_forming_bar_is_dropped_during_day_session() -> None:
    """日盘盘中：当天那根日线还没收线，实盘不能拿它定方向。"""
    kept = drop_forming_bar(
        _daily_frame(), now=datetime(2026, 11, 20, 10, 0), period="D"
    )
    assert list(kept.index) == list(pd.to_datetime(["2026-11-18", "2026-11-19"]))


def test_daily_forming_bar_dropped_in_night_session() -> None:
    """周四夜盘已进入周五交易日：周五那根日线还没走完，不能参与判断。"""
    kept = drop_forming_bar(
        _daily_frame(), now=datetime(2026, 11, 19, 21, 30), period="D"
    )
    assert len(kept) == 2
    assert kept.index[-1] == pd.Timestamp("2026-11-19")


def test_complete_daily_bar_kept_on_weekend() -> None:
    """周六看周五的日线：已经收线，应该保留，不能因为日期小于下一交易日就删。"""
    kept = drop_forming_bar(
        _daily_frame(), now=datetime(2026, 11, 22, 10, 0), period="D"
    )
    assert list(kept.index) == list(
        pd.to_datetime(["2026-11-18", "2026-11-19", "2026-11-20"])
    )


def test_daily_period_flag_does_not_change_minute_bar_behavior() -> None:
    """不传 period 时保留旧的右标签口径，分钟线测试不受影响。"""
    index = pd.to_datetime(["2026-11-21 13:30", "2026-11-21 13:35"])
    bars = pd.DataFrame(
        {"open": [1.0, 1.0], "high": [2.0, 2.0], "low": [1.0, 1.0], "close": [1.5, 1.5]},
        index=index,
    )
    kept = drop_forming_bar(bars, now=datetime(2026, 11, 21, 13, 34, 0))
    assert len(kept) == 1


def test_scan_symbol_never_concatenates_forming_bar_for_decisions() -> None:
    """B2 契约：策略评估和通用止损只用已收线，未收线那根只留给健康和回溯。"""
    from inspect import getsource

    from src.scheduler.runner import TradingBot

    source = getsource(TradingBot.scan_symbol)
    assert "pd.concat([closed, forming])" not in source
    assert "use_forming_for_entry" not in source
    assert "use_forming_for_stop" not in source
    assert "forming=None" in source


def _limits_cfg(*, cap: int | None) -> dict:
    ctp: dict[str, object] = {}
    if cap is not None:
        ctp["max_order_volume"] = cap
    return {
        "trade": {"volume": 1, "ctp": ctp},
        "risk": {
            "sizing_mode": "risk_pct",
            "capital": 1_000_000,
            "risk_per_trade_pct": 2,
            "min_volume": 1,
            "contracts": {"bu": {"multiplier": 10}},
        },
    }


def test_backtest_limits_pass_single_order_cap_to_sizer(monkeypatch) -> None:
    """B3：回测手数也要受 trade.ctp.max_order_volume 约束。"""
    seen: dict[str, object] = {}

    def fake_calc(symbol, entry, stop, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(volume=1, reason="ok")

    limits = BacktestLimits(_limits_cfg(cap=5))
    monkeypatch.setattr(limits.sizer, "calc_open", fake_calc)
    limits.size_open("bu", 100.0, 95.0)
    assert seen.get("max_per_order") == 5

    limits_no_cap = BacktestLimits(_limits_cfg(cap=None))
    seen.clear()
    monkeypatch.setattr(limits_no_cap.sizer, "calc_open", fake_calc)
    limits_no_cap.size_open("bu", 100.0, 95.0)
    assert seen.get("max_per_order") is None


def _fee_contracts() -> dict:
    return {
        "bu": {
            "commission": 1.0,
            "commission_close": 10.0,
            "commission_close_today": 30.0,
        }
    }


def _open_meta(lots: list[tuple[str, int]], *, open_fee: float = 0.0) -> dict:
    return {
        "entry_time": lots[0][0],
        "open_fee": open_fee,
        "open_volume": sum(volume for _time, volume in lots),
        "lots": [{"time": time, "volume": volume} for time, volume in lots],
    }


def test_cross_day_add_close_fee_uses_each_lot_date() -> None:
    """B6：跨日加仓后全平，一手平昨、一手平今，不能整笔按首个 entry_time 算。"""
    meta = _open_meta([("2026-11-20T09:05:00", 1), ("2026-11-21T09:05:00", 1)])
    _net, fee = _net_pnl_after_fees(
        _fee_contracts(),
        "bu",
        gross=100.0,
        volume=2,
        held=2,
        open_meta=meta,
        exit_time="2026-11-21T14:55:00",
    )
    assert fee == 40.0
    assert meta["lots"] == []


def test_partial_close_prefers_today_lot_and_keeps_older_lot() -> None:
    """B6：只平一手时优先平今，剩余仍是前一日那一手。"""
    meta = _open_meta([("2026-11-20T09:05:00", 1), ("2026-11-21T09:05:00", 1)])
    _net, fee = _net_pnl_after_fees(
        _fee_contracts(),
        "bu",
        gross=50.0,
        volume=1,
        held=2,
        open_meta=meta,
        exit_time="2026-11-21T14:55:00",
    )
    assert fee == 30.0
    assert meta["lots"] == [{"time": "2026-11-20T09:05:00", "volume": 1}]
    assert meta["entry_time"] == "2026-11-20T09:05:00"


def test_same_day_add_close_fee_counts_both_as_close_today() -> None:
    """B6：同一交易日内加仓后全平，两手都按平今费率。"""
    meta = _open_meta([("2026-11-21T09:05:00", 1), ("2026-11-21T10:35:00", 1)])
    _net, fee = _net_pnl_after_fees(
        _fee_contracts(),
        "bu",
        gross=100.0,
        volume=2,
        held=2,
        open_meta=meta,
        exit_time="2026-11-21T14:55:00",
    )
    assert fee == 60.0


def _trend_cfg(**backtest) -> dict:
    return {
        "dry_run": True,
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": False,
            "market_filter": {"enabled": False},
            "trend_follow": {
                "entry_bars": 20,
                "exit_bars": 8,
                "atr_period": 14,
                "atr_mult": 10.0,
                "flatten_at_close": False,
            },
        },
        "trade": {"volume": 1, "symbol": "bu", "ctp": {"price_tick": 1}},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "risk": {
            "sizing_mode": "fixed",
            "capital": 14000,
            "min_volume": 1,
            "contracts": {"bu": {"multiplier": 10}},
        },
        "backtest": dict(backtest),
    }


def _trend_bars() -> pd.DataFrame:
    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)] + [130.0, 131.0]
    idx = [datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(len(closes))]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=idx,
    )


def test_backtest_snapshot_reports_margin_rate_and_live_gaps() -> None:
    """B4/B7：保证金比例可配置，未模拟的实盘环节写进口径差异。"""
    result = run_backtest(
        _trend_cfg(margin_rate=0.12),
        symbol="bu",
        bars_signal=_trend_bars(),
        warmup=20,
        volume=1,
    )
    snap = result.params_snapshot
    assert snap["margin_rate"] == 0.12
    note = snap["live_gap_note"]
    for keyword in ("组合敞口", "保证金", "K线", "清仓", "12%"):
        assert keyword in note


def test_backtest_margin_rate_defaults_and_rejects_invalid_values() -> None:
    """B7：没配或配错都回退 10%，不能把收益率分母算成 0 或负数。"""
    default = run_backtest(_trend_cfg(), symbol="bu", bars_signal=_trend_bars(), warmup=20)
    assert default.params_snapshot["margin_rate"] == 0.10
    invalid = run_backtest(
        _trend_cfg(margin_rate="abc"), symbol="bu", bars_signal=_trend_bars(), warmup=20
    )
    assert invalid.params_snapshot["margin_rate"] == 0.10


def test_quality_report_marks_points_based_pnl() -> None:
    """B8：质量报告明确说明按点数、不含手续费滑点。"""
    trade = SignalTrade(
        side="long",
        entry_time=datetime(2026, 11, 21, 9, 5),
        exit_time=datetime(2026, 11, 21, 10, 0),
        entry_price=100.0,
        exit_price=110.0,
        stop_price=95.0,
        pnl_points=10.0,
        risk_points=5.0,
        r_multiple=2.0,
    )
    report = build_quality_report([trade])
    text = "\n".join(report.lines)
    assert "按点数统计" in text
    assert "不含手续费、滑点" in text


def test_replay_result_marks_points_based_pnl() -> None:
    """B8：复盘正文明确说明按点数估算，不能和回测/实盘金额直接相加。"""
    from src.review.replay import REPLAY_COST_NOTE

    assert "不含手续费、滑点" in REPLAY_COST_NOTE
    assert "不可直接相加" in REPLAY_COST_NOTE
