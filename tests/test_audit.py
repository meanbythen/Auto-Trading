"""只读评估审计：合成字典与临时目录，不依赖真实 data。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.backtest.audit import REVIEW_NOTE, audit_results, format_audit_text

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "audit_evaluation.py"


def _config(**evaluation_overrides: Any) -> dict[str, Any]:
    evaluation = {
        "enabled": True,
        "stress_on_backtest": False,
        "stress_min_trades": 30,
        "profit_factor_min": 1.3,
    }
    evaluation.update(evaluation_overrides)
    return {
        "strategy": {"engine": "daily_body"},
        "trade": {"symbol": "bu2612"},
        "evaluation": evaluation,
    }


def _performance(trades: int = 40, profit_factor: float = 1.5) -> dict[str, Any]:
    wins = trades // 2
    return {
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "gross_profit": 150.0,
        "gross_loss": 100.0,
        "net_pnl": 50.0,
        "profit_factor": profit_factor,
    }


def _backtest(trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "symbol": "bu2612",
        "performance": _performance(),
        "trades": trades or [],
        "params_snapshot": {
            "engine": "daily_body",
            "data_from": "2026-01-01",
            "data_to": "2026-06-30",
        },
        "evaluation": {"conclusion": "pass", "skipped": "", "checks": []},
    }


def _optimize() -> dict[str, Any]:
    return {
        "symbol": "bu2612",
        "engine": "daily_body",
        "accepted": False,
        "evaluation_ok": True,
        "best_performance": _performance(),
        "train_performance": _performance(),
        "holdout_performance": _performance(),
        "best_limits": {"data_from": "2026-01-01", "data_to": "2026-04-30"},
        "evaluation": {"conclusion": "pass", "skipped": "", "checks": []},
        "slippage": {},
        "perturbation": {},
    }


def _finding(report: Any, source: str, code: str) -> Any:
    return next(item for item in report.findings if item.source == source and item.code == code)


def test_missing_skipped_and_check_expansion() -> None:
    backtest = _backtest()
    backtest["evaluation"] = {}
    optimize = _optimize()
    optimize["accepted"] = True
    optimize["evaluation"] = {
        "conclusion": "watch",
        "skipped": "全区间复跑失败",
        "checks": [
            {"key": "profit_factor", "label": "盈利因子", "status": "fail", "value": 0.8, "threshold": 1.3, "note": "红线"},
            {"key": "sample", "label": "样本", "status": "watch", "value": 20, "threshold": 150, "note": "观察"},
            {"key": "range_group", "label": "震荡组", "status": "skip", "value": "—", "threshold": "", "note": "无ADX"},
            {"key": "calmar", "status": "pass", "value": 2, "threshold": 1},
        ],
    }

    report = audit_results(backtest, optimize, _config())

    assert _finding(report, "backtest", "evaluation_missing").severity == "warning"
    skipped = _finding(report, "optimize", "evaluation_skipped")
    assert skipped.severity == "warning"
    assert skipped.details["accepted"] is True
    fail = _finding(report, "optimize", "evaluation_check_fail")
    assert fail.severity == "critical"
    assert fail.details["value"] == 0.8
    assert fail.details["threshold"] == 1.3
    assert fail.details["note"] == "红线"
    assert _finding(report, "optimize", "evaluation_check_watch").severity == "warning"
    skip = _finding(report, "optimize", "evaluation_check_skip")
    assert skip.severity == "info"
    assert skip.details["note"] == "无ADX"
    assert not any(item.code == "evaluation_check_pass" for item in report.findings)


@pytest.mark.parametrize(("evaluation_ok", "conclusion"), [(False, "pass"), (True, "reject")])
def test_accepted_conflicts_with_evaluation(evaluation_ok: bool, conclusion: str) -> None:
    optimize = _optimize()
    optimize["accepted"] = True
    optimize["evaluation_ok"] = evaluation_ok
    optimize["evaluation"]["conclusion"] = conclusion

    report = audit_results(_backtest(), optimize, _config())

    conflict = _finding(report, "optimize", "optimize_gate_conflict")
    assert conflict.severity == "critical"
    assert "禁止直接写入" in conflict.message


@pytest.mark.parametrize(
    ("trades", "code"),
    [(0, "holdout_zero_trades"), (12, "holdout_small_sample")],
)
def test_holdout_zero_and_small_sample(trades: int, code: str) -> None:
    optimize = _optimize()
    optimize["holdout_performance"] = _performance(trades=trades)

    report = audit_results(_backtest(), optimize, _config())

    finding = _finding(report, "optimize", code)
    assert finding.severity == "warning"
    assert report.metrics["holdout_trades"] == trades
    if trades:
        assert "非统计显著性判断" in finding.message


@pytest.mark.parametrize(("accepted", "severity"), [(False, "warning"), (True, "critical")])
def test_holdout_low_profit_factor(accepted: bool, severity: str) -> None:
    optimize = _optimize()
    optimize["accepted"] = accepted
    optimize["holdout_performance"] = _performance(profit_factor=1.1)

    report = audit_results(_backtest(), optimize, _config(profit_factor_min=1.3))

    finding = _finding(report, "optimize", "holdout_low_profit_factor")
    assert finding.severity == severity
    assert finding.details["threshold"] == 1.3


def test_stress_missing_and_skip_are_warnings() -> None:
    backtest = _backtest()
    backtest["performance"]["trades"] = 30
    backtest["evaluation"]["checks"] = [
        {"key": "slippage", "status": "skip", "value": "—", "threshold": "PF≥1.2", "note": "未执行"}
    ]
    optimize = _optimize()
    optimize["best_performance"]["trades"] = 0

    report = audit_results(backtest, optimize, _config(stress_on_backtest=True))

    skipped = _finding(report, "backtest", "stress_check_skipped")
    assert skipped.severity == "warning"
    assert skipped.details["note"] == "未执行"
    assert _finding(report, "backtest", "stress_missing").details["kind"] == "perturbation"
    assert not any(
        item.code == "stress_missing" and item.details["kind"] == "slippage"
        for item in report.findings
    )


def test_valid_stress_checks_prevent_false_missing_and_fail_is_not_duplicated() -> None:
    optimize = _optimize()
    optimize["best_performance"]["trades"] = 30
    optimize["evaluation"]["checks"] = [
        {"key": "slippage", "status": "pass", "value": "PF 1.4", "threshold": "PF≥1.2", "note": ""},
        {"key": "perturbation", "status": "fail", "value": "PF 0.5", "threshold": "无断崖", "note": "cliff"},
    ]
    backtest = _backtest()
    backtest["performance"]["trades"] = 0

    report = audit_results(backtest, optimize, _config(stress_on_backtest=True))

    assert not any(item.code.startswith("stress_") for item in report.findings)
    failures = [item for item in report.findings if item.code == "evaluation_check_fail"]
    assert len(failures) == 1
    assert failures[0].details["key"] == "perturbation"


def test_stress_error_and_no_valid_points() -> None:
    optimize = _optimize()
    optimize["best_performance"]["trades"] = 30
    optimize["slippage"] = {"ok": False, "error": "行情缺失"}
    optimize["perturbation"] = {"ok": False, "cliff": False, "points": [], "points_count": 0, "error": ""}
    backtest = _backtest()
    backtest["performance"]["trades"] = 0

    report = audit_results(backtest, optimize, _config(stress_on_backtest=True))

    error = _finding(report, "optimize", "stress_error")
    assert error.details["kind"] == "slippage"
    assert error.details["error"] == "行情缺失"
    assert _finding(report, "optimize", "stress_no_valid_points").severity == "warning"


def test_cliff_with_valid_points_is_not_execution_failure() -> None:
    optimize = _optimize()
    optimize["best_performance"]["trades"] = 30
    optimize["slippage"] = {"ok": True, "profit_factor": 1.1, "error": ""}
    optimize["perturbation"] = {
        "ok": False,
        "cliff": True,
        "points": [{"profit_factor": 0.5, "net_pnl": -10}],
        "points_count": 1,
        "error": "",
    }
    backtest = _backtest()
    backtest["performance"]["trades"] = 0

    report = audit_results(backtest, optimize, _config(stress_on_backtest=True))

    assert not any(item.code.startswith("stress_") for item in report.findings)


def test_stress_not_required_below_trade_threshold() -> None:
    backtest = _backtest()
    backtest["performance"]["trades"] = 29
    optimize = _optimize()
    optimize["best_performance"]["trades"] = 29

    report = audit_results(backtest, optimize, _config(stress_on_backtest=True))

    assert not any(item.code.startswith("stress_") for item in report.findings)


def test_concentration_metrics_are_calculated_from_trades() -> None:
    trades = [{"pnl": value, "adx_at_entry": 20} for value in [100, 80, 60, 40, 20, -250]]

    report = audit_results(_backtest(trades), _optimize(), _config())

    assert report.metrics["trade_count"] == 6
    assert report.metrics["profitable_trades"] == 5
    assert report.metrics["gross_profit"] == 300
    assert report.metrics["top1_profit"] == 100
    assert report.metrics["top1_profit_share"] == pytest.approx(1 / 3)
    assert report.metrics["net_pnl"] == 50
    assert report.metrics["net_without_top5"] == -250
    finding = _finding(report, "backtest", "profit_concentration")
    assert finding.details["top5_fragile"] is True
    assert finding.details["concentrated"] is False


def test_top1_concentration_requires_thirty_trades() -> None:
    trades = [{"pnl": 600, "adx_at_entry": 20}]
    trades.extend({"pnl": 10, "adx_at_entry": 20} for _ in range(29))

    report = audit_results(_backtest(trades), _optimize(), _config())

    finding = _finding(report, "backtest", "profit_concentration")
    assert finding.details["concentrated"] is True
    assert finding.details["top5_fragile"] is False
    assert report.metrics["top1_profit_share"] == pytest.approx(600 / 890)
    assert report.metrics["net_without_top5"] == 250
    assert "非统计显著性判断" in finding.details["note"]


def test_small_sample_top1_does_not_trigger_concentration() -> None:
    trades = [{"pnl": value, "adx_at_entry": 20} for value in [100, 1, 1, -102]]

    report = audit_results(_backtest(trades), _optimize(), _config())

    assert report.metrics["top1_profit_share"] > 0.5
    assert not any(item.code == "profit_concentration" for item in report.findings)


def test_same_bar_trade_is_flagged() -> None:
    trades = [
        {
            "pnl": 100,
            "entry_time": "2026-03-11 15:00",
            "exit_time": "2026-03-11 15:00",
            "adx_at_entry": 20,
        },
        {
            "pnl": -20,
            "entry_time": "2026-03-12 09:00",
            "exit_time": "2026-03-12 10:00",
            "adx_at_entry": 20,
        },
    ]

    report = audit_results(_backtest(trades), _optimize(), _config())

    finding = _finding(report, "backtest", "same_bar_trade")
    assert finding.severity == "warning"
    assert report.metrics["same_bar_trades"] == 1
    assert report.metrics["same_bar_pnl"] == 100


def test_zero_base_slippage_is_flagged() -> None:
    backtest = _backtest()
    backtest["evaluation"]["checks"] = [
        {"key": "slippage", "status": "pass", "value": "PF 1.2", "threshold": "PF≥1.2", "note": ""}
    ]
    backtest["evaluation"]["metrics"] = {"slippage_base_ticks": 0.0}

    report = audit_results(backtest, _optimize(), _config(stress_on_backtest=True))

    finding = _finding(report, "backtest", "slippage_zero_base")
    assert finding.severity == "warning"
    assert finding.details["base_ticks"] == 0.0


@pytest.mark.parametrize(("covered", "expected_warnings"), [(24, 0), (23, 1)])
def test_adx_zero_is_valid_and_coverage_boundary(covered: int, expected_warnings: int) -> None:
    trades = [
        {"pnl": -1, "adx_at_entry": 0 if index < covered else None}
        for index in range(30)
    ]

    report = audit_results(_backtest(trades), _optimize(), _config())

    assert report.metrics["adx_covered_trades"] == covered
    assert report.metrics["adx_coverage"] == pytest.approx(covered / 30)
    warnings = [item for item in report.findings if item.code == "adx_coverage_low"]
    assert len(warnings) == expected_warnings


def test_config_comparability_only_strips_and_ignores_case() -> None:
    backtest = _backtest()
    backtest["symbol"] = " BU2612 "
    backtest["params_snapshot"]["engine"] = " Daily_Body "
    optimize = _optimize()
    optimize["symbol"] = " BU2612"
    optimize["engine"] = "DAILY_BODY"

    report = audit_results(backtest, optimize, _config())

    assert not any(item.code.startswith("config_") for item in report.findings)


def test_config_mismatches_and_missing_data_ranges() -> None:
    backtest = _backtest()
    backtest["symbol"] = "bu2701"
    backtest["params_snapshot"]["engine"] = "trend_follow"
    backtest["params_snapshot"]["data_from"] = ""
    optimize = _optimize()
    optimize["symbol"] = "fu2701"
    optimize["engine"] = "ema_swing"
    optimize["best_limits"]["data_to"] = ""

    report = audit_results(backtest, optimize, _config())

    mismatches = [item for item in report.findings if item.code.startswith("config_")]
    assert len(mismatches) == 4
    assert all(item.severity == "warning" for item in mismatches)
    ranges = [item for item in report.findings if item.code == "data_range_missing"]
    assert len(ranges) == 2
    assert _finding(report, "backtest", "data_range_missing").details["missing_fields"] == ["data_from"]
    optimize_range = _finding(report, "optimize", "data_range_missing")
    assert optimize_range.details["missing_fields"] == ["data_to"]
    assert "训练段" in optimize_range.message


def test_profit_factor_99_placeholder_warning() -> None:
    backtest = _backtest()
    backtest["performance"] = {
        "trades": 1,
        "wins": 1,
        "losses": 0,
        "gross_profit": 100,
        "gross_loss": 0,
        "profit_factor": 99,
    }
    optimize = _optimize()
    optimize["holdout_performance"] = {"wins": 2, "losses": 0, "profit_factor": 99}

    report = audit_results(backtest, optimize, _config())

    placeholders = [item for item in report.findings if item.code == "profit_factor_placeholder"]
    assert {(item.source, item.details["performance"]) for item in placeholders} == {
        ("backtest", "performance"),
        ("optimize", "holdout_performance"),
    }
    assert all("占位值" in item.message for item in placeholders)


def test_audit_does_not_mutate_inputs_and_serializes() -> None:
    backtest = _backtest([{"pnl": 10, "adx_at_entry": 0}])
    backtest["evaluation"]["checks"] = [
        {"key": "sample", "status": "watch", "value": "1笔", "threshold": "≥150", "note": "观察"}
    ]
    optimize = _optimize()
    config = _config()
    originals = copy.deepcopy((backtest, optimize, config))

    report = audit_results(backtest, optimize, config)

    assert (backtest, optimize, config) == originals
    payload = report.to_dict()
    assert payload["counts"] == report.counts
    assert sum(payload["counts"].values()) == len(payload["findings"])
    assert payload["metrics"]["adx_coverage"] == 1.0
    assert payload["review_note"] == REVIEW_NOTE
    json.dumps(payload, ensure_ascii=False)
    text = format_audit_text(report)
    assert "严重级别统计" in text
    assert "backtest/evaluation_check_watch" in text
    assert REVIEW_NOTE in text


def _write_cli_inputs(
    root: Path,
    backtest: dict[str, Any],
    optimize: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Path]:
    paths = {
        "backtest": root / "data" / "backtest_last.json",
        "optimize": root / "data" / "optimize_last.json",
        "config": root / "config" / "config.yaml",
    }
    paths["backtest"].parent.mkdir(parents=True)
    paths["config"].parent.mkdir(parents=True)
    paths["backtest"].write_text(json.dumps(backtest, ensure_ascii=False), encoding="utf-8")
    paths["optimize"].write_text(json.dumps(optimize, ensure_ascii=False), encoding="utf-8")
    paths["config"].write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return paths


def _run_cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_cli_text_zero_strict_one_json_and_input_files_unchanged(tmp_path: Path) -> None:
    backtest = _backtest()
    backtest["evaluation"]["checks"] = [
        {"key": "profit_factor", "status": "fail", "value": 0.9, "threshold": 1.3, "note": "红线"}
    ]
    paths = _write_cli_inputs(tmp_path, backtest, _optimize(), _config())
    before = {name: path.read_bytes() for name, path in paths.items()}

    normal = _run_cli(tmp_path)
    assert normal.returncode == 0, normal.stderr
    assert "严重级别统计" in normal.stdout
    assert "critical=1" in normal.stdout
    assert normal.stderr == ""

    strict = _run_cli(tmp_path, "--strict", "--json")
    assert strict.returncode == 1, strict.stderr
    payload = json.loads(strict.stdout)
    assert payload["counts"]["critical"] == 1
    assert payload["findings"][0]["code"] == "evaluation_check_fail"
    assert payload["review_note"] == REVIEW_NOTE
    assert {name: path.read_bytes() for name, path in paths.items()} == before


def test_cli_clean_json_returns_zero(tmp_path: Path) -> None:
    _write_cli_inputs(tmp_path, _backtest(), _optimize(), _config())

    completed = _run_cli(tmp_path, "--json")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["counts"] == {"critical": 0, "warning": 0, "info": 0}
    assert payload["findings"] == []
    assert payload["metrics"]["trade_count"] == 0


def test_cli_missing_file_returns_two(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "审计输入错误" in completed.stderr


@pytest.mark.parametrize("bad_file", ["backtest", "config", "non_dict"])
def test_cli_parse_and_non_dict_errors_return_two(tmp_path: Path, bad_file: str) -> None:
    paths = _write_cli_inputs(tmp_path, _backtest(), _optimize(), _config())
    if bad_file == "backtest":
        paths["backtest"].write_text("{", encoding="utf-8")
    elif bad_file == "config":
        paths["config"].write_text("evaluation: [\n", encoding="utf-8")
    else:
        paths["optimize"].write_text("[]", encoding="utf-8")

    completed = _run_cli(tmp_path)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "审计输入错误" in completed.stderr


def test_cli_argument_error_returns_two(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path, "--unknown")

    assert completed.returncode == 2
    assert "unrecognized arguments" in completed.stderr
