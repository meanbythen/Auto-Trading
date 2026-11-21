# -*- coding: utf-8 -*-
"""滑点校准记录持久化与回测滑点解析。"""
import json

from src.account.slippage_log import (
    CALIBRATION_FILE,
    load_slippage_calibration,
    record_fill,
    resolve_backtest_slippage,
    save_slippage_calibration,
)

_RESULT_KEYS = {
    "value",
    "source",
    "calibrated",
    "reason",
    "diff",
    "effective_date",
    "sample_n",
    "live_suggested",
    "backtest_assumed",
}


def _save_calibrated_record(root):
    for slip in (1, 2, 3):
        record_fill(
            root,
            symbol="fu2701",
            action="BUY_OPEN",
            volume=1,
            theory_price=3000,
            fill_price=3000 + slip,
            tick=1,
            executor="ctp",
            dry_run=False,
            account="live",
        )
    return save_slippage_calibration(root, backtest_assumed=1.0, account="live")


def test_calibration_save_load_round_trip(tmp_path):
    saved = _save_calibrated_record(tmp_path)

    path = tmp_path / "data" / CALIBRATION_FILE
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    assert load_slippage_calibration(tmp_path) == saved
    assert saved["calibrated"] is True
    assert saved["generated_at"]


def test_resolve_without_record_uses_configured_default(tmp_path):
    result = resolve_backtest_slippage(
        tmp_path,
        {"backtest": {"slippage_ticks": 1.5}},
        use_calibration=True,
    )

    assert set(result) == _RESULT_KEYS
    assert result["value"] == 1.5
    assert result["backtest_assumed"] == 1.5
    assert result["source"] == "配置默认"
    assert result["calibrated"] is False
    assert result["diff"] == 0.0
    assert "无校准记录" in result["reason"]


def test_resolve_uses_live_suggestion_when_enabled(tmp_path):
    saved = _save_calibrated_record(tmp_path)

    result = resolve_backtest_slippage(
        tmp_path,
        {
            "backtest": {
                "slippage_ticks": 1.0,
                "use_slippage_calibration": "true",
            }
        },
    )

    assert result["value"] == saved["live_suggested"]
    assert result["source"] == "实盘校准"
    assert result["calibrated"] is True
    assert result["diff"] == saved["live_suggested"] - 1.0
    assert result["sample_n"] == 3


def test_resolve_keeps_configured_default_when_switch_is_off(tmp_path):
    saved = _save_calibrated_record(tmp_path)

    result = resolve_backtest_slippage(
        tmp_path,
        {
            "backtest": {
                "slippage_ticks": 1.0,
                "use_slippage_calibration": True,
            }
        },
        use_calibration=False,
    )

    assert result["value"] == 1.0
    assert result["source"] == "配置默认"
    assert result["calibrated"] is False
    assert result["live_suggested"] == saved["live_suggested"]
    assert "开关未开启" in result["reason"]
