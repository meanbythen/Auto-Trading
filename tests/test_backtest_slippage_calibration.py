"""回测读取实盘滑点校准：默认不变、达标才采用、结果区标明来源。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.account.slippage_log import CALIBRATION_FILE
from src.backtest.engine import run_backtest
from src.ui.backtest_actions import BacktestActionsMixin


def _cfg(root: Path, **backtest) -> dict:
    bt = {"slippage_ticks": 1.0}
    bt.update(backtest)
    return {
        "_project_root": str(root),
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
        "risk": {"capital": 14000, "contracts": {"bu": {"multiplier": 10}}},
        "backtest": bt,
    }


def _bars() -> pd.DataFrame:
    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)] + [130.0]
    index = [datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(len(closes))]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=index,
    )


def _write_calibration(root: Path) -> None:
    path = root / "data" / CALIBRATION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "calibrated": True,
                "live_suggested": 2.0,
                "effective_date": "2026-11-20",
                "sample_n": 12,
                "reason": "测试校准记录",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _run_and_format(root: Path, **backtest):
    cfg = _cfg(root, **backtest)
    bars = _bars()
    result = run_backtest(cfg, symbol="bu", bars_signal=bars, warmup=20, volume=1)
    actions = BacktestActionsMixin()
    actions.cfg = cfg
    actions.root_dir = root
    text = actions._backtest_summary_text(result, report_path=root / "data" / "backtest_last.json")
    return cfg, bars, result, text


def test_backtest_default_ignores_calibration_record(tmp_path: Path) -> None:
    _write_calibration(tmp_path)

    cfg, bars, result, text = _run_and_format(tmp_path)

    snap = result.params_snapshot
    assert snap["slippage_ticks"] == 1.0
    assert snap["slippage_source"] == "配置默认"
    assert snap["slippage_calibrated"] is False
    assert "校准开关未开启" in snap["slippage_calibration_reason"]
    assert cfg["backtest"]["slippage_ticks"] == 1.0
    assert result.trades[-1].exit_price == float(bars["close"].iloc[-1]) - 1.0
    assert "成本校准" in text
    assert "未校准" in text
    assert "来源：配置默认" in text


def test_backtest_uses_calibrated_live_suggestion_and_marks_snapshot(tmp_path: Path) -> None:
    _write_calibration(tmp_path)

    cfg, bars, result, text = _run_and_format(tmp_path, use_slippage_calibration=True)

    snap = result.params_snapshot
    assert snap["slippage_ticks"] == 2.0
    assert snap["slippage_source"] == "实盘校准"
    assert snap["slippage_calibrated"] is True
    assert snap["slippage_diff"] == 1.0
    assert snap["slippage_live_suggested"] == 2.0
    assert snap["slippage_backtest_assumed"] == 1.0
    assert snap["slippage_effective_date"] == "2026-11-20"
    assert snap["slippage_sample_n"] == 12
    assert snap["slippage_calibration_reason"] == "测试校准记录"
    assert cfg["backtest"]["slippage_ticks"] == 1.0
    assert result.trades[-1].exit_price == float(bars["close"].iloc[-1]) - 2.0
    assert "成本校准" in text
    assert "来源：实盘校准" in text
    assert "实盘建议  2 跳 vs 回测假设 1 跳" in text
    assert "diff +1 跳" in text
    assert "2026-11-20" in text
    assert "样本数 12 笔" in text


def test_backtest_enabled_without_record_uses_default_and_explains(tmp_path: Path) -> None:
    # 隔离目录下没有校准记录，不读真实 data/。
    cfg, bars, result, text = _run_and_format(
        tmp_path,
        slippage_ticks=1.5,
        use_slippage_calibration=True,
    )

    snap = result.params_snapshot
    assert snap["slippage_ticks"] == 1.5
    assert snap["slippage_source"] == "配置默认"
    assert snap["slippage_calibrated"] is False
    assert "无校准记录" in snap["slippage_calibration_reason"]
    assert cfg["backtest"]["slippage_ticks"] == 1.5
    assert result.trades[-1].exit_price == float(bars["close"].iloc[-1]) - 1.5
    assert "成本校准" in text
    assert "未校准" in text
    assert "无校准记录" in text
