"""`tools/perf_report.py` 的单测：用临时结果文件验证比值与结论。"""

from __future__ import annotations

import json
from pathlib import Path

from tools import perf_report


def _write_result(
    directory: Path,
    run_id: str,
    *,
    case: str = "PERF-04",
    p95: float = 10.0,
    median: float | None = None,
    headline: float | None = None,
    unit: str = "ms",
    baseline: float | None = 10.0,
    threshold: float | None = 100.0,
    higher_is_better: bool = False,
    relative_floor: float = 0.0,
    passed: bool = True,
    generated_at: str = "",
) -> Path:
    payload = {
        "run_id": run_id,
        "generated_at": generated_at or run_id,
        "env": {
            "machine": "TEST-PC",
            "platform": "Windows-Test",
            "cpu": "Test CPU",
            "cores": 8,
            "ram_gb": 16.0,
            "python": "3.12.10",
            "deps_hash": "abc123",
            "strict": False,
        },
        "cases": [
            {
                "case": case,
                "unit": unit,
                "samples": [p95],
                "median": p95 if median is None else median,
                **({} if headline is None else {"headline": headline}),
                "p95": p95,
                "min": p95,
                "max": p95,
                "threshold": threshold,
                "higher_is_better": higher_is_better,
                "relative_floor": relative_floor,
                "baseline": baseline,
                "ratio": None,
                "passed": passed,
                "note": "测试用例",
            }
        ],
    }
    path = directory / f"result-{run_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_baseline(directory: Path, cases: dict[str, float]) -> Path:
    path = directory / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00",
                "env": {"deps_hash": "abc123"},
                "cases": cases,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_build_markdown_contains_table_and_pass(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"PERF-04": 10.0})
    _write_result(tmp_path, "20260101-000000", p95=11.0)
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, perf_report.load_baseline(tmp_path / "baseline.json"))

    assert "PERF-04" in report
    assert "| 通过 |" in report
    assert "全部通过" in report
    assert "TEST-PC" in report


def test_failed_case_marks_fail_and_strict_returns_one(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"PERF-04": 10.0})
    _write_result(tmp_path, "20260101-000000", p95=100.0, threshold=50.0, passed=True)
    results = perf_report.load_results(tmp_path)
    baseline = perf_report.load_baseline(tmp_path / "baseline.json")

    report = perf_report.build_markdown(results, baseline)

    assert "| 失败 |" in report
    assert "未通过" in report
    assert perf_report.main(["--results-dir", str(tmp_path), "--strict"]) == 1


def test_strict_returns_zero_when_all_pass_and_writes_output(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"PERF-04": 10.0})
    _write_result(tmp_path, "20260101-000000", p95=10.5)
    output = tmp_path / "report.md"

    code = perf_report.main(["--results-dir", str(tmp_path), "--output", str(output), "--strict"])

    assert code == 0
    assert output.exists()
    assert "性能基准报告" in output.read_text(encoding="utf-8")


def test_latest_only_reads_newest_run(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"PERF-04": 10.0})
    _write_result(tmp_path, "20260101-000000", p95=10.0, generated_at="2026-01-01T00:00:00")
    _write_result(tmp_path, "20260102-000000", p95=20.0, generated_at="2026-01-02T00:00:00")

    results = perf_report.load_results(tmp_path, latest=True)

    assert len(results) == 1
    assert results[0]["cases"][0]["p95"] == 20.0


def test_missing_baseline_reports_no_regression_judgement(tmp_path: Path) -> None:
    _write_result(tmp_path, "20260101-000000", p95=10.0, baseline=None)
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, {})

    assert "没有基线文件" in report
    assert "| — |" in report


def test_relative_slowdown_blames_ratio_gate_not_threshold(tmp_path: Path) -> None:
    """DEF-01：只超相对门禁时，结论不能写成「超过绝对阈值」。"""
    _write_baseline(tmp_path, {"PERF-01": 564.62})
    _write_result(
        tmp_path, "20260101-000000", case="PERF-01", p95=735.89, baseline=None, threshold=3000.0
    )
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, perf_report.load_baseline(tmp_path / "baseline.json"))
    conclusion = report.split("## 5. 结论", 1)[1]

    assert "比值 1.30（超过 1.2 相对门禁）" in conclusion
    assert "绝对阈值" not in conclusion
    assert perf_report.main(["--results-dir", str(tmp_path), "--strict"]) == 1


def test_threshold_failure_does_not_claim_ratio_gate(tmp_path: Path) -> None:
    """只超绝对阈值时，结论不应再提相对门禁。"""
    _write_baseline(tmp_path, {"PERF-04": 100.0})
    _write_result(tmp_path, "20260101-000000", p95=100.5, baseline=None, threshold=50.0)
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, perf_report.load_baseline(tmp_path / "baseline.json"))
    conclusion = report.split("## 5. 结论", 1)[1]

    assert "超过绝对阈值 50.00 ms" in conclusion
    assert "相对门禁" not in conclusion


def test_higher_is_better_regression_uses_lower_bound(tmp_path: Path) -> None:
    """吞吐类指标：本次 / 基线低于 0.85 才失败，且比值按「本次 / 基线」展示。"""
    _write_baseline(tmp_path, {"PERF-07": 3609.8})
    _write_result(
        tmp_path,
        "20260101-000000",
        case="PERF-07",
        p95=2500.0,
        unit="bars/s",
        baseline=None,
        threshold=500.0,
        higher_is_better=True,
    )
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, perf_report.load_baseline(tmp_path / "baseline.json"))
    conclusion = report.split("## 5. 结论", 1)[1]

    assert "| 失败 |" in report
    assert "比值 0.69（低于 0.85 相对门禁）" in conclusion
    assert "绝对阈值" not in conclusion


def test_higher_is_better_uses_median_not_best_run(tmp_path: Path) -> None:
    """吞吐类 3 次采样取中位数：p95 会挑到最好那次，不能拿来判通过。"""
    _write_baseline(tmp_path, {"PERF-07": 3609.8})
    _write_result(
        tmp_path,
        "20260101-000000",
        case="PERF-07",
        p95=5000.0,
        median=2600.0,
        unit="bars/s",
        baseline=None,
        threshold=1500.0,
        higher_is_better=True,
    )
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, perf_report.load_baseline(tmp_path / "baseline.json"))
    conclusion = report.split("## 5. 结论", 1)[1]

    assert "| 失败 |" in report
    assert "比值 0.72（低于 0.85 相对门禁）" in conclusion


def test_cold_start_outlier_does_not_fail_median_headline(tmp_path: Path) -> None:
    """冷启动取中位数：5 次里 1 次被杀毒/索引拖慢，不应直接判红。"""
    _write_baseline(tmp_path, {"PERF-01": 626.424})
    _write_result(
        tmp_path,
        "20260101-000000",
        case="PERF-01",
        p95=926.664,
        median=690.888,
        headline=690.888,
        unit="ms",
        baseline=None,
        threshold=3000.0,
        relative_floor=100.0,
    )
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, perf_report.load_baseline(tmp_path / "baseline.json"))
    conclusion = report.split("## 5. 结论", 1)[1]

    assert "| 通过 |" in report
    assert "| 1.10 |" in report
    assert "失败" not in conclusion


def test_higher_is_better_improvement_passes(tmp_path: Path) -> None:
    """吞吐提升（本次 / 基线 > 1）必须判通过，不能被反向门禁误杀。"""
    _write_baseline(tmp_path, {"PERF-07": 3609.8})
    _write_result(
        tmp_path,
        "20260101-000000",
        case="PERF-07",
        p95=5000.0,
        unit="bars/s",
        baseline=None,
        threshold=500.0,
        higher_is_better=True,
    )
    results = perf_report.load_results(tmp_path)

    report = perf_report.build_markdown(results, perf_report.load_baseline(tmp_path / "baseline.json"))

    assert "全部通过" in report
    assert "| 通过 |" in report


def test_no_results_returns_error_code(tmp_path: Path) -> None:
    assert perf_report.main(["--results-dir", str(tmp_path)]) == 1
