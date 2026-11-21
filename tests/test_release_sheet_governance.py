# -*- coding: utf-8 -*-
"""上线评审单「治理追踪」节：无数据不破坏既有行为，有数据展示假设/变更/冻结。"""
import json
from datetime import datetime

from src.research import governance
from src.review.release_sheet import (
    MISSING,
    PASS,
    _evidence_digest,
    build_release_sheet,
    format_release_sheet,
)


def _cfg() -> dict:
    return {
        "strategy": {"engine": "supertrend", "timeframe": "15m"},
        "trade": {"symbol": "fu2701"},
        "risk": {"contracts": {"fu": {"commission": 2.63}}},
        "backtest": {"slippage_ticks": 3},
        "evaluation": {"slippage_extra_ticks": 1},
    }


def _backtest() -> dict:
    return {
        "symbol": "fu2701",
        "timeframe": "15m",
        "params_snapshot": {
            "engine": "supertrend",
            "symbol": "fu2701",
            "timeframe": "15m",
            "data_from": "2025-01-01 09:00",
            "data_to": "2026-05-31 15:00",
            "bars": 500,
        },
        "performance": {"trades": 10},
        "evaluation": {"checks": [], "conclusion": "pass"},
    }


def _write(root, name: str, payload: dict) -> None:
    path = root / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_experiment(root, exp_id: str, *, symbol: str = "fu2701") -> None:
    manifest_dir = root / "data" / "experiments" / exp_id
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "experiment_id": exp_id,
        "kind": "optimize",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "details": {"symbol": symbol, "trial_count": 1, "failed_count": 0},
    }
    (manifest_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )


def test_governance_section_missing_when_no_data(tmp_path):
    _write(tmp_path, "backtest_last.json", _backtest())
    sheet = build_release_sheet(tmp_path, _cfg())
    section = sheet.section("治理追踪")
    assert section is not None
    assert section.status == MISSING
    text = "\n".join(section.lines)
    assert "治理追踪" in format_release_sheet(sheet)
    # 无治理数据时各环节标「未记录」，不抛异常
    assert "未记录" in text

    # 其它既有节不受影响
    gate = sheet.section("数据门禁")
    assert gate is not None and gate.status == PASS


def test_governance_excluded_from_evidence_digest(tmp_path):
    """「治理追踪」不纳入证据摘要：MISSING 不改变摘要、不阻止既有签字行为。"""
    _write(tmp_path, "backtest_last.json", _backtest())
    sheet = build_release_sheet(tmp_path, _cfg())
    gov = sheet.section("治理追踪")
    assert gov is not None and gov.status == MISSING

    digest_with = _evidence_digest(sheet.sections)
    without_gov = [s for s in sheet.sections if s.title != "治理追踪"]
    digest_without = _evidence_digest(without_gov)
    assert digest_with == digest_without


def test_governance_section_passes_with_data(tmp_path):
    _write(tmp_path, "backtest_last.json", _backtest())
    hyp = governance.register_hypothesis(
        tmp_path,
        title="趋势跟踪在燃油有效",
        statement="supertrend 在 fu 主力 15m 上盈利因子>1",
        symbol="fu2701",
        acceptance="样本外盈利因子>=1",
    )
    _write_experiment(tmp_path, "exp-001")
    governance.link_experiment_to_hypothesis(tmp_path, "exp-001", hyp["id"])
    governance.register_live_strategy(
        tmp_path, strategy="supertrend", symbol="fu2701", experiment_id="exp-001"
    )
    governance.append_change(
        tmp_path,
        strategy="supertrend",
        symbol="fu2701",
        what="收紧止损",
        why="降低回撤",
    )

    sheet = build_release_sheet(tmp_path, _cfg())
    section = sheet.section("治理追踪")
    assert section is not None
    assert section.status == PASS

    text = format_release_sheet(sheet)
    assert "假设" in text
    assert "变更" in text
    assert "冻结" in text
    # 14 天内的变更触发冻结提示
    assert "冻结" in "\n".join(section.lines)


def test_governance_read_failure_does_not_raise(tmp_path, monkeypatch):
    """治理模块抛异常时，评审单仍能生成，该节标「未记录」。"""
    _write(tmp_path, "backtest_last.json", _backtest())

    def _boom(*_args, **_kwargs):
        raise RuntimeError("governance down")

    monkeypatch.setattr(governance, "trace_strategy", _boom)
    sheet = build_release_sheet(tmp_path, _cfg())
    section = sheet.section("治理追踪")
    assert section is not None
    assert section.status == MISSING
    assert "治理数据不可用" in "\n".join(section.lines)
