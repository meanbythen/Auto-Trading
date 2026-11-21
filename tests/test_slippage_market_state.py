# -*- coding: utf-8 -*-
"""滑点市场状态分桶、降级与拒单时段统计。"""
import csv
import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

import src.account.slippage_log as slippage_log
from src.account.slippage_log import quality_text, reject_summary, summarize_by_market_state
from src.strategy import market_state

_FIELDS = [
    "ts",
    "symbol",
    "action",
    "volume",
    "theory_price",
    "fill_price",
    "slip_ticks",
    "tick",
    "executor",
    "dry_run",
    "account",
]

_CFG = {
    "strategy": {
        "timeframe": "15",
        "market_filter": {"adx_period": 2, "atr_period": 2, "lookback": 4},
    }
}


def _synthetic_bars():
    index = pd.date_range("2026-11-19 09:15", periods=8, freq="15min")
    close = [100.0] * 6 + [110.0, 90.0]
    return pd.DataFrame(
        {
            "open": close,
            "high": [value + 2 for value in close],
            "low": [value - 2 for value in close],
            "close": close,
            "volume": [1.0] * len(close),
        },
        index=index,
    )


def _write_fills(root, fills):
    path = root / "data" / "slippage_log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_FIELDS)
        for stamp, slip in fills:
            writer.writerow(
                [stamp, "fu2701", "BUY_OPEN", 1, "3000", "3001", slip, "1", "ctp", "0", "live"]
            )


def _fake_state(bars, _params):
    is_trend = float(bars["close"].iloc[-1]) > 100
    return SimpleNamespace(adx=30.0 if is_trend else 10.0, atr_rank=0.8 if is_trend else 0.2)


def test_market_state_groups_combined_trend_and_volatility_labels(tmp_path, monkeypatch):
    bars = _synthetic_bars()
    trend_ts = bars.index[-2].isoformat()
    range_ts = bars.index[-1].isoformat()
    _write_fills(tmp_path, [(trend_ts, slip) for slip in (1, 2, 3)] + [(range_ts, slip) for slip in (4, 5, 6)])
    loaded = []

    def fake_load(_root, _cfg, symbol):
        loaded.append(symbol)
        return bars

    monkeypatch.setattr(slippage_log, "_load_market_bars", fake_load)
    monkeypatch.setattr(market_state, "evaluate_market_state", _fake_state)

    result = {item["key"]: item for item in summarize_by_market_state(tmp_path, _CFG)}

    assert set(result) == {"趋势/高波动", "震荡/低波动"}
    assert result["趋势/高波动"]["n"] == 3
    assert result["震荡/低波动"]["n"] == 3
    assert loaded == ["fu2701"]


def test_market_state_returns_empty_for_small_sample_or_missing_bars(tmp_path, monkeypatch):
    bars = _synthetic_bars()
    stamp = bars.index[-1].isoformat()
    _write_fills(tmp_path, [(stamp, 1), (stamp, 2)])
    monkeypatch.setattr(slippage_log, "_load_market_bars", lambda *_args: bars)
    monkeypatch.setattr(market_state, "evaluate_market_state", _fake_state)
    assert summarize_by_market_state(tmp_path, _CFG) == []

    _write_fills(tmp_path, [(stamp, 1), (stamp, 2), (stamp, 3)])
    monkeypatch.setattr(slippage_log, "_load_market_bars", lambda *_args: None)
    assert summarize_by_market_state(tmp_path, _CFG) == []
    assert "市场状态样本不足或缺少行情，先不下结论" in quality_text(tmp_path, _CFG)


def test_reject_summary_includes_day_night_and_other_sessions(tmp_path):
    today = datetime.now().replace(minute=0, second=0, microsecond=0)
    events = [
        (today.replace(hour=10), "order_fail"),
        (today.replace(hour=11), "risk_block"),
        (today.replace(hour=22), "order_no_price"),
        (today.replace(hour=16), "strategy_health"),
    ]
    path = tmp_path / "data" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for stamp, event in events:
            fh.write(json.dumps({"ts": stamp.isoformat(), "event": event, "symbol": "fu2701"}) + "\n")

    result = reject_summary(tmp_path)

    assert result["total"] == 4
    assert result["by_session"] == [
        {"key": "日盘", "n": 2},
        {"key": "夜盘", "n": 1},
        {"key": "其他", "n": 1},
    ]
    assert "分时段  日盘 2 次、夜盘 1 次、其他 1 次" in quality_text(tmp_path)
