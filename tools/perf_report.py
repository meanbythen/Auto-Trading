"""性能报告：把 `build/perf/result-*.json` 汇总成一份 Markdown。

功能：
    1. 读取一次或多次性能基准结果，按当前基线重算比值与通过状态；
    2. 输出环境卡、指标总表、趋势表与结论，便于回答「这次比上次慢了吗」；
    3. `--strict` 时有失败用例退出码 1，可直接挂到夜间任务。

用法：
    python tools/perf_report.py
    python tools/perf_report.py --latest
    python tools/perf_report.py --output build/perf/report.md
    python tools/perf_report.py --strict

只读结果 JSON 与基线文件，不写库、不修改结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = _ROOT / "build" / "perf"


def load_baseline(path: Path) -> dict[str, Any]:
    """读取基线文件；不存在或损坏时返回空 dict。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_results(results_dir: Path, latest: bool = False) -> list[dict[str, Any]]:
    """按生成时间读取结果文件，坏文件跳过。"""
    results: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("result-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"[warn] 跳过无法解析的结果文件：{path}", file=sys.stderr)
            continue
        if not isinstance(raw, dict):
            continue
        raw["_path"] = str(path)
        results.append(raw)
    results.sort(key=lambda item: str(item.get("generated_at") or item.get("run_id") or ""))
    if latest and results:
        return [results[-1]]
    return results


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.1f} {unit}"
    if abs(value) >= 10:
        return f"{value:.2f} {unit}"
    return f"{value:.3f} {unit}"


def _fmt_ratio(ratio: float | None) -> str:
    return "—" if ratio is None else f"{ratio:.2f}"


def evaluate_case(case: dict[str, Any], baseline_cases: dict[str, float]) -> dict[str, Any]:
    """按当前基线重算单个用例的比值与结论。

    结果 JSON 里已经带了一次判定，但基线可能事后更新；报告以基线文件为准，
    阈值仍用用例当时记录的绝对阈值。噪声下限内的差异只比绝对阈值。
    """
    case_id = str(case.get("case") or "")
    unit = str(case.get("unit") or "")
    p95 = float(case.get("p95") or 0.0)
    median = case.get("median")
    higher_is_better = bool(case.get("higher_is_better"))
    floor = float(case.get("relative_floor") or 0.0)
    threshold = case.get("threshold")
    # 结果 JSON 的 headline 已按用例选好统计量（耗时类 p95、吞吐类或冷启动取中位数）；
    # 老结果没有该字段时，吞吐类退回中位数，其余退回 p95。
    if case.get("headline") is not None:
        headline = float(case["headline"])
    else:
        headline = float(median) if higher_is_better and median is not None else p95

    baseline = case.get("baseline")
    if baseline is None:
        baseline = baseline_cases.get(case_id)
    baseline_value = float(baseline) if baseline else None

    # 比值统一为「本次 / 基线」：耗时类越小越好，要求 ≤1.2；吞吐类越大越好，要求 ≥0.85。
    ratio = None
    if baseline_value and baseline_value > 0 and headline > 0:
        ratio = headline / baseline_value

    threshold_ok = True
    if threshold is not None:
        threshold_ok = headline >= float(threshold) if higher_is_better else headline <= float(threshold)

    ratio_ok = True
    noisy = baseline_value is not None and abs(headline - baseline_value) <= floor
    if ratio is not None and not noisy:
        ratio_ok = ratio >= 0.85 if higher_is_better else ratio <= 1.2

    return {
        "case": case_id,
        "unit": unit,
        "value": headline,
        "p95": p95,
        "baseline": baseline_value,
        "ratio": ratio,
        "threshold": None if threshold is None else float(threshold),
        "higher_is_better": higher_is_better,
        "threshold_ok": threshold_ok,
        "ratio_ok": ratio_ok,
        "passed": threshold_ok and ratio_ok,
        "note": str(case.get("note") or ""),
    }


def _env_table(env: dict[str, Any]) -> list[str]:
    rows = [
        ("机器", env.get("machine")),
        ("系统", env.get("platform")),
        ("CPU", env.get("cpu")),
        ("核数", env.get("cores")),
        ("内存", f"{env.get('ram_gb')} GB" if env.get("ram_gb") else None),
        ("Python", env.get("python")),
        ("依赖哈希", env.get("deps_hash")),
        ("门禁模式", "严格" if env.get("strict") else "仅记录"),
    ]
    lines = ["| 项目 | 值 |", "|---|---|"]
    for key, value in rows:
        lines.append(f"| {key} | {value if value not in (None, '') else '—'} |")
    return lines


def _trend_table(results: list[dict[str, Any]], limit: int) -> list[str]:
    recent = results[-limit:]
    case_ids: list[str] = []
    for result in recent:
        for case in result.get("cases") or []:
            case_id = str(case.get("case") or "")
            if case_id and case_id not in case_ids:
                case_ids.append(case_id)
    if not case_ids:
        return ["没有可用的历史结果。"]

    header = ["运行"] + case_ids
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for result in recent:
        by_case = {str(case.get("case")): case for case in result.get("cases") or []}
        row = [str(result.get("run_id") or "—")]
        for case_id in case_ids:
            case = by_case.get(case_id)
            if not case:
                row.append("—")
                continue
            if case.get("headline") is not None:
                value = case["headline"]
            else:
                median = case.get("median")
                value = median if case.get("higher_is_better") and median is not None else case.get("p95")
            row.append(str(value))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("表格内为各用例本次头条值：耗时类取 p95，吞吐类与冷启动取中位数；单位见上方指标总表。")
    return lines


def build_markdown(results: list[dict[str, Any]], baseline: dict[str, Any], limit: int = 10) -> str:
    """生成 Markdown 报告。results 至少一条。"""
    latest = results[-1]
    env = latest.get("env") or {}
    baseline_cases = baseline.get("cases") if isinstance(baseline.get("cases"), dict) else {}
    cases = [evaluate_case(case, baseline_cases) for case in latest.get("cases") or []]
    failed = [case for case in cases if not case["passed"]]

    lines: list[str] = ["# 性能基准报告", ""]
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"结果来源：`{latest.get('_path', '—')}`")
    lines.append(f"覆盖运行：{len(results)} 次（趋势表取最近 {min(len(results), limit)} 次）")
    lines.append("")

    lines.append("## 1. 环境卡")
    lines.append("")
    lines.extend(_env_table(env))
    lines.append("")

    lines.append("## 2. 基线")
    lines.append("")
    baseline_env = baseline.get("env") or {}
    lines.append(f"- 生成时间：{baseline.get('generated_at') or '未找到'}")
    lines.append(f"- 用例数：{len(baseline_cases)}")
    if baseline_cases and baseline_env.get("deps_hash") and baseline_env.get("deps_hash") != env.get("deps_hash"):
        lines.append(
            f"- 注意：依赖哈希与本次不同（基线 {baseline_env.get('deps_hash')}，"
            f"本次 {env.get('deps_hash')}），比值只看趋势，不作为绝对结论。"
        )
    if not baseline_cases:
        lines.append("- 注意：没有基线文件，本次只记录数值，无法判断回归。")
    lines.append("")

    lines.append("## 3. 指标总表")
    lines.append("")
    lines.append("| 用例 | 指标 | 本次值 | 基线 | 比值 | 绝对阈值 | 结论 |")
    lines.append("|---|---|---|---|---|---|---|")
    for case in cases:
        direction = "≥" if case["higher_is_better"] else "≤"
        threshold = "—" if case["threshold"] is None else f"{direction} {_fmt(case['threshold'], case['unit'])}"
        lines.append(
            f"| {case['case']} | {case['unit']} | {_fmt(case['value'], case['unit'])} | "
            f"{_fmt(case['baseline'], case['unit'])} | {_fmt_ratio(case['ratio'])} | "
            f"{threshold} | {'通过' if case['passed'] else '失败'} |"
        )
    lines.append("")

    notes = [case for case in cases if case["note"]]
    if notes:
        lines.append("用例说明：")
        lines.append("")
        for case in notes:
            lines.append(f"- {case['case']}：{case['note']}")
        lines.append("")

    lines.append("## 4. 趋势")
    lines.append("")
    lines.extend(_trend_table(results, limit))
    lines.append("")

    lines.append("## 5. 结论")
    lines.append("")
    if failed:
        lines.append(f"{len(cases)} 个用例中 {len(failed)} 个未通过：")
        lines.append("")
        for case in failed:
            reasons = []
            if not case["threshold_ok"]:
                direction = "低于" if case["higher_is_better"] else "超过"
                reasons.append(f"{direction}绝对阈值 {_fmt(case['threshold'], case['unit'])}")
            if not case["ratio_ok"]:
                if case["higher_is_better"]:
                    reasons.append(
                        f"基线 {_fmt(case['baseline'], case['unit'])}，"
                        f"比值 {_fmt_ratio(case['ratio'])}（低于 0.85 相对门禁）"
                    )
                else:
                    reasons.append(
                        f"基线 {_fmt(case['baseline'], case['unit'])}，"
                        f"比值 {_fmt_ratio(case['ratio'])}（超过 1.2 相对门禁）"
                    )
            lines.append(f"- {case['case']}：{'；'.join(reasons) or '未通过'}")
    else:
        lines.append(f"{len(cases)} 个用例全部通过。")
    lines.append("")
    lines.append("> 同机相对比较有效；换机器、换 Python 大版本或关键依赖后应重设基线。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总性能基准结果，生成 Markdown 报告")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR), help="结果目录（默认 build/perf）")
    parser.add_argument("--baseline", default="", help="基线文件（默认 <results-dir>/baseline.json）")
    parser.add_argument("--latest", action="store_true", help="只报告最新一次结果")
    parser.add_argument("--output", default="", help="写入文件；不填则打印到终端")
    parser.add_argument("--limit", type=int, default=10, help="趋势表最多显示最近几次（默认 10）")
    parser.add_argument("--strict", action="store_true", help="有失败用例时退出码 1")
    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir)
    baseline_path = Path(args.baseline) if args.baseline else results_dir / "baseline.json"
    results = load_results(results_dir, latest=args.latest)
    if not results:
        print(f"没有找到结果文件：{results_dir}\\result-*.json", file=sys.stderr)
        return 1

    report = build_markdown(results, load_baseline(baseline_path), limit=max(1, args.limit))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"报告已写入 {output}")
    else:
        print(report)

    if args.strict:
        latest_failed = [
            case
            for case in (
                evaluate_case(item, (load_baseline(baseline_path).get("cases") or {}))
                for item in (results[-1].get("cases") or [])
            )
            if not case["passed"]
        ]
        return 1 if latest_failed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
