# -*- coding: utf-8 -*-
"""滑点校准记录 build_slippage_calibration：字段齐全、空数据与仅 GUI 假定成交分支。"""
import csv

from src.account.slippage_log import FILE_NAME, build_slippage_calibration

_FIELDS = [
    "ts", "symbol", "action", "volume", "theory_price", "fill_price",
    "slip_ticks", "tick", "executor", "dry_run", "account",
]

_EXPECTED_KEYS = {
    "live_median", "live_p70", "live_suggested", "backtest_assumed", "diff",
    "sample_n", "real_rows", "used_real", "effective_date", "calibrated", "reason",
}


def _write_csv(root, rows):
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / FILE_NAME).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_FIELDS)
        writer.writerows(rows)


def test_calibration_with_real_fills(tmp_path):
    # 5 笔 CTP 真实成交，slip_ticks 非零：[1,2,3,4,5]
    rows = [
        ["2026-11-01T09:00:00", "fu2701", "BUY_OPEN", 1, "3000", "3001", "1", "1", "ctp", "0", "live"],
        ["2026-11-01T09:05:00", "fu2701", "SELL_OPEN", 1, "3000", "2998", "2", "1", "ctp", "0", "live"],
        ["2026-11-01T09:10:00", "fu2701", "BUY_OPEN", 1, "3000", "3003", "3", "1", "ctp", "0", "live"],
        ["2026-11-01T09:15:00", "fu2701", "SELL_OPEN", 1, "3000", "2996", "4", "1", "ctp", "0", "live"],
        ["2026-11-01T09:20:00", "fu2701", "BUY_OPEN", 1, "3000", "3005", "5", "1", "ctp", "0", "live"],
    ]
    _write_csv(tmp_path, rows)

    result = build_slippage_calibration(tmp_path, backtest_assumed=1.0)

    assert set(result) == _EXPECTED_KEYS
    assert result["calibrated"] is True
    assert result["sample_n"] >= 3
    assert result["used_real"] is True
    assert result["real_rows"] == 5
    assert result["diff"] == result["live_suggested"] - 1.0
    # 5 笔 [1..5]：中位 3，70 分位 3.8，建议值向上取 0.5 → 4.0
    assert result["live_median"] == 3.0
    assert result["live_suggested"] == 4.0
    assert result["diff"] == 3.0
    assert result["effective_date"]


def test_calibration_empty_dir(tmp_path):
    # 没有 csv / 没有任何记录
    result = build_slippage_calibration(tmp_path, backtest_assumed=1.0)

    assert set(result) == _EXPECTED_KEYS
    assert result["calibrated"] is False
    assert result["reason"]


def test_calibration_only_gui_assumed(tmp_path):
    # 只有 GUI 假定成交（dry_run=1），slip_ticks 恒为 0
    rows = [
        ["2026-11-01T09:00:00", "fu2701", "BUY_OPEN", 1, "3000", "3000", "0", "1", "gui", "1", ""],
        ["2026-11-01T09:05:00", "fu2701", "SELL_OPEN", 1, "3000", "3000", "0", "1", "gui", "1", ""],
        ["2026-11-01T09:10:00", "fu2701", "BUY_OPEN", 1, "3000", "3000", "0", "1", "gui", "1", ""],
    ]
    _write_csv(tmp_path, rows)

    result = build_slippage_calibration(tmp_path, backtest_assumed=1.0)

    assert result["calibrated"] is False
    assert result["used_real"] is False
    assert result["reason"]


def _live_row(ts, slip):
    # 真实实盘成交：dry_run=0、executor=ctp、account=live。
    return [ts, "fu2701", "BUY_OPEN", 1, "3000", "3001", str(slip), "1", "ctp", "0", "live"]


def _sim_row(ts, slip):
    # 仿真 CTP 柜台成交：executor 同样是 ctp，只有 account=sim 能区分。
    return [ts, "fu2701", "BUY_OPEN", 1, "3000", "3001", str(slip), "1", "ctp", "0", "sim"]


def test_calibration_excludes_sim_by_default(tmp_path):
    # 审查复现：1 笔 live=1 跳 + 2 笔 sim=5 跳。默认只用 live，样本不足 -> 未校准。
    rows = [
        _live_row("2026-11-01T09:00:00", 1),
        _sim_row("2026-11-01T09:05:00", 5),
        _sim_row("2026-11-01T09:10:00", 5),
    ]
    _write_csv(tmp_path, rows)

    result = build_slippage_calibration(tmp_path, backtest_assumed=1.0)

    assert result["calibrated"] is False
    assert result["sample_n"] == 1
    assert result["used_real"] is True
    assert result["real_rows"] == 1


def test_calibration_live_suggested_unaffected_by_sim_noise(tmp_path):
    # 5 笔 live[1..5]（建议 4.0）+ 3 笔 sim=10 跳。默认只算 live，建议值不被 sim 拉高。
    rows = [_live_row(f"2026-11-01T09:0{i}:00", slip) for i, slip in enumerate([1, 2, 3, 4, 5])]
    rows += [_sim_row(f"2026-11-01T10:0{i}:00", 10) for i in range(3)]
    _write_csv(tmp_path, rows)

    result = build_slippage_calibration(tmp_path, backtest_assumed=1.0)

    assert result["calibrated"] is True
    assert result["sample_n"] == 5
    assert result["real_rows"] == 5
    assert result["live_suggested"] == 4.0


def test_calibration_account_none_mixes_sim(tmp_path):
    # 对照：显式 account=None 才统计全部账户，此时 sim 会被计入（docstring 约定的行为）。
    rows = [
        _live_row("2026-11-01T09:00:00", 1),
        _sim_row("2026-11-01T09:05:00", 5),
        _sim_row("2026-11-01T09:10:00", 5),
    ]
    _write_csv(tmp_path, rows)

    result = build_slippage_calibration(tmp_path, backtest_assumed=1.0, account=None)

    assert result["sample_n"] == 3
    assert result["calibrated"] is True
