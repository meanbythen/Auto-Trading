# -*- coding: utf-8 -*-
"""上线评审单：五项证据、状态与签字。"""
import json

from src.account.slippage_log import record_fill
from src.review.release_sheet import (
    FAIL,
    MISSING,
    PASS,
    WATCH,
    build_release_sheet,
    format_release_sheet,
    sign_release_sheet,
)


def _cfg(engine: str = "supertrend") -> dict:
    return {
        "strategy": {"engine": engine, "timeframe": "15m"},
        "trade": {"symbol": "fu2701"},
        "backtest": {"slippage_ticks": 3, "evaluation": {"slippage_extra_ticks": 1}},
    }


def _backtest(engine: str = "supertrend", *, gates: bool = False, hits: list | None = None) -> dict:
    return {
        "symbol": "fu2701",
        "timeframe": "15m",
        "params_snapshot": {
            "engine": engine,
            "symbol": "fu2701",
            "timeframe": "15m",
            "data_from": "2025-01-01 09:00",
            "data_to": "2026-05-31 15:00",
            "bars": 500,
            "mirror_live_gates": gates,
            "mirror_gate_hits": hits or [],
        },
        "performance": {"trades": 10},
        "evaluation": {"checks": [], "conclusion": "pass"},
    }


def _write(root, name: str, payload: dict) -> None:
    path = root / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_missing_evidence_is_explicit(tmp_path):
    sheet = build_release_sheet(tmp_path, _cfg())
    status = {section.title: section.status for section in sheet.sections}
    assert status["数据门禁"] == MISSING
    assert status["样本外"] == MISSING
    assert status["仿真滑点"] == MISSING
    assert status["人工签字"] == MISSING
    assert "证据不足" in sheet.verdict

    text = format_release_sheet(sheet)
    for title in ("数据门禁", "样本外", "镜像差异率", "仿真滑点", "人工签字"):
        assert title in text
    assert "不改配置、不放宽风控、不自动开仓" in text


def test_data_gate_passes_when_scope_and_range_match(tmp_path):
    _write(tmp_path, "backtest_last.json", _backtest())
    sheet = build_release_sheet(tmp_path, _cfg())
    gate = sheet.section("数据门禁")
    assert gate is not None and gate.status == PASS
    assert "2025-01-01 → 2026-05-31" in "\n".join(gate.lines)


def test_data_gate_fails_on_engine_mismatch(tmp_path):
    _write(tmp_path, "backtest_last.json", _backtest(engine="daily_body"))
    sheet = build_release_sheet(tmp_path, _cfg())
    gate = sheet.section("数据门禁")
    assert gate is not None and gate.status == FAIL
    assert "口径不一致" in "\n".join(gate.lines)
    assert sheet.verdict.startswith("不通过")


def test_data_gate_fails_without_real_range(tmp_path):
    payload = _backtest()
    payload["params_snapshot"].pop("data_from")
    _write(tmp_path, "backtest_last.json", payload)
    sheet = build_release_sheet(tmp_path, _cfg())
    gate = sheet.section("数据门禁")
    assert gate is not None and gate.status == FAIL


def test_holdout_section_reads_optimize_result(tmp_path):
    _write(
        tmp_path,
        "optimize_last.json",
        {"holdout_performance": {"trades": 40, "profit_factor": 1.25, "net_pnl": 880.0}},
    )
    sheet = build_release_sheet(tmp_path, _cfg())
    holdout = sheet.section("样本外")
    assert holdout is not None and holdout.status == PASS
    assert "成交 40 笔" in "\n".join(holdout.lines)

    _write(
        tmp_path,
        "optimize_last.json",
        {"holdout_performance": {"trades": 0, "profit_factor": 0.0, "net_pnl": 0.0}},
    )
    assert build_release_sheet(tmp_path, _cfg()).section("样本外").status == FAIL


def test_mirror_rate_uses_backtest_gate_hits(tmp_path):
    hits = [{"kind": "slip_block", "label": "开仓滑点过大"}]
    _write(tmp_path, "backtest_last.json", _backtest(gates=True, hits=hits))
    section = build_release_sheet(tmp_path, _cfg()).section("镜像差异率")
    assert section is not None
    assert "放弃 1 次 / 机会 11 次 = 9.1%" in "\n".join(section.lines)
    assert section.status == WATCH


def test_sim_slippage_compares_with_stress(tmp_path):
    for slip in (1, 2, 3):
        record_fill(
            tmp_path,
            symbol="fu2701",
            action="BUY_OPEN",
            volume=1,
            theory_price=3000,
            fill_price=3000 + slip,
            tick=1,
            executor="ctp",
            dry_run=False,
            account="sim",
        )
    sheet = build_release_sheet(tmp_path, _cfg())
    section = sheet.section("仿真滑点")
    assert section is not None and section.status == PASS
    assert "建议 2.5 跳" in "\n".join(section.lines)

    cfg = _cfg()
    cfg["backtest"]["slippage_ticks"] = 1
    section = build_release_sheet(tmp_path, cfg).section("仿真滑点")
    assert section is not None and section.status == WATCH
    assert "高于压力档" in "\n".join(section.lines)


def test_signature_is_saved_and_loaded(tmp_path):
    _write(tmp_path, "backtest_last.json", _backtest())
    _write(
        tmp_path,
        "optimize_last.json",
        {"holdout_performance": {"trades": 40, "profit_factor": 1.25, "net_pnl": 880.0}},
    )
    sheet = build_release_sheet(tmp_path, _cfg())
    assert "待人工签字" in sheet.verdict

    payload = sign_release_sheet(
        tmp_path, sheet, signed_by="张三", decision="允许小资金挂机", note="先 1 手"
    )
    assert payload["signed_by"] == "张三"
    saved = json.loads((tmp_path / "data" / "release_sheet.json").read_text(encoding="utf-8"))
    assert saved["decision"] == "允许小资金挂机"
    assert (tmp_path / "data" / "release_sheet.md").exists()
    assert sheet.verdict == "已签字：允许小资金挂机"

    again = build_release_sheet(tmp_path, _cfg())
    assert again.signature["signed_by"] == "张三"
    assert again.verdict == "已签字：允许小资金挂机"
    assert "张三" in format_release_sheet(again)
