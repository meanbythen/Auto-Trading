# -*- coding: utf-8 -*-
"""上线评审单成本校准：缺失、有效与过期记录。"""
import json
from datetime import datetime, timedelta

from src.review.release_sheet import PASS, WATCH, build_release_sheet, format_release_sheet


def _write_calibration(root, record: dict) -> None:
    path = root / "data" / "slippage_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def _calibrated_record(**changes) -> dict:
    now = datetime.now()
    record = {
        "live_suggested": 2.0,
        "backtest_assumed": 1.0,
        "diff": 1.0,
        "sample_n": 8,
        "effective_date": now.strftime("%Y-%m-%d"),
        "calibrated": True,
        "reason": "已基于真实成交样本校准",
        "generated_at": now.isoformat(timespec="seconds"),
    }
    record.update(changes)
    return record


def test_missing_calibration_marks_cost_section_watch(tmp_path):
    sheet = build_release_sheet(tmp_path)
    section = sheet.section("成本校准")
    assert section is not None
    assert section.status == WATCH
    assert "未校准" in "\n".join(section.lines)


def test_current_calibration_passes_and_formats_difference(tmp_path):
    _write_calibration(tmp_path, _calibrated_record())
    sheet = build_release_sheet(tmp_path)
    section = sheet.section("成本校准")
    assert section is not None
    assert section.status == PASS

    text = format_release_sheet(sheet)
    assert "成本校准" in text
    assert "差值 +1 跳" in text
    assert "回测滑点比实盘乐观 1 跳" in text


def test_calibration_older_than_thirty_days_marks_watch(tmp_path):
    generated_at = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    _write_calibration(tmp_path, _calibrated_record(generated_at=generated_at))
    section = build_release_sheet(tmp_path).section("成本校准")
    assert section is not None
    assert section.status == WATCH
    assert "已超 30 天" in "\n".join(section.lines)
