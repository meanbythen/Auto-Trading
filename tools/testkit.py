"""项目内测试编排入口：用例基线、单包覆盖率缺口、全量门禁一条命令跑完。

设计原则：
1. 不重造轮子：pytest / coverage / ruff / mypy / import-linter 仍按原样调用；
2. 不引入依赖：只用标准库，Windows 是唯一目标平台；
3. 只读校验：不改 VERSION 和文档，版本该升哪一位仍由人判断。

用法：
    python tools/testkit.py baseline
    python tools/testkit.py cov src/scheduler --target 80
    python tools/testkit.py gate
    python tools/testkit.py gate --coverage          # CI 用：pytest 带覆盖率并校验 fail_under
    python tools/testkit.py gate --only ruff,mypy
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "build" / "testkit"

_BASELINE_BOTH = re.compile(r"(\d+)/(\d+) tests collected \((\d+) deselected\)")
_BASELINE_ONLY = re.compile(r"(\d+) tests collected")
_ROW_BRANCH = re.compile(
    r"^\s*(?P<name>\S.*?)\s+(?P<stmt>\d+)\s+(?P<miss>\d+)\s+"
    r"(?P<branch>\d+)\s+(?P<brpart>\d+)\s+(?P<cover>\d+)%\s*(?P<missing>.*)$"
)
_ROW_PLAIN = re.compile(
    r"^\s*(?P<name>\S.*?)\s+(?P<stmt>\d+)\s+(?P<miss>\d+)\s+"
    r"(?P<cover>\d+)%\s*(?P<missing>.*)$"
)


@dataclass
class CommandResult:
    name: str
    code: int
    seconds: float
    output: str


@dataclass
class CovRow:
    path: str
    stmt: int
    miss: int
    branch: int
    brpart: int
    cover: int
    missing: str

    @property
    def statement_cover(self) -> float:
        return statement_cover(self.stmt, self.miss)


def slug(source: str) -> str:
    """把包路径压成文件名安全的片段，例如 src/scheduler -> src_scheduler。"""
    return re.sub(r"[^0-9A-Za-z]+", "_", source).strip("_")


def statement_cover(stmt: int, miss: int) -> float:
    """语句覆盖率；零语句按 100% 处理，避免除零。"""
    if stmt <= 0:
        return 100.0
    return round((stmt - miss) * 100.0 / stmt, 2)


def parse_baseline(output: str) -> tuple[int, int, int] | None:
    """解析 `pytest --collect-only` 汇总，返回 (选中, 收集总数, 取消选中)。"""
    both = _BASELINE_BOTH.search(output)
    if both:
        return int(both.group(1)), int(both.group(2)), int(both.group(3))
    only = _BASELINE_ONLY.search(output)
    if only:
        count = int(only.group(1))
        return count, count, 0
    return None


def parse_coverage_rows(output: str, *, branch: bool = True) -> list[CovRow]:
    """解析 `coverage report` 的逐文件行；忽略表头、分隔线和 TOTAL。"""
    pattern = _ROW_BRANCH if branch else _ROW_PLAIN
    rows: list[CovRow] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("Name", "---", "TOTAL")):
            continue
        match = pattern.match(line)
        if not match:
            continue
        data = match.groupdict()
        if not data["name"].endswith(".py"):
            continue
        rows.append(
            CovRow(
                path=data["name"],
                stmt=int(data["stmt"]),
                miss=int(data["miss"]),
                branch=int(data.get("branch") or 0),
                brpart=int(data.get("brpart") or 0),
                cover=int(data["cover"]),
                missing=(data.get("missing") or "").strip(),
            )
        )
    return rows


def parse_coverage_total(output: str, *, branch: bool = True) -> CovRow | None:
    """解析 `coverage report` 的 TOTAL 行。"""
    pattern = _ROW_BRANCH if branch else _ROW_PLAIN
    for line in output.splitlines():
        if not line.strip().startswith("TOTAL"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        data = match.groupdict()
        return CovRow(
            path="TOTAL",
            stmt=int(data["stmt"]),
            miss=int(data["miss"]),
            branch=int(data.get("branch") or 0),
            brpart=int(data.get("brpart") or 0),
            cover=int(data["cover"]),
            missing=(data.get("missing") or "").strip(),
        )
    return None


def _run(name: str, cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 1800) -> CommandResult:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=merged,
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        output += f"\n[testkit] 超时（{timeout}s）"
        code = 124
    return CommandResult(name=name, code=code, seconds=round(time.monotonic() - started, 1), output=output)


def _tail(text: str, lines: int = 30) -> str:
    parts = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(parts[-lines:])


def _write_json(name: str, payload: object) -> Path:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cmd_baseline(args: argparse.Namespace) -> int:
    # addopts 会带 -q，这里清空后自己加 marker，口径与 pyproject 的默认排除保持一致。
    result = _run(
        "baseline",
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-o",
            "addopts=",
            "-m",
            "not perf and not soak",
        ],
    )
    parsed = parse_baseline(result.output)
    if result.code != 0 or parsed is None:
        print("[FAIL] 用例收集失败")
        print(_tail(result.output))
        return 1
    selected, total, deselected = parsed
    print(f"选中用例：{selected} 项")
    print(f"收集总数：{total} 项（取消选中 {deselected} 项）")
    path = _write_json(
        "baseline.json",
        {"selected": selected, "total": total, "deselected": deselected, "seconds": result.seconds},
    )
    print(f"已写入 {path.relative_to(ROOT)}")
    return 0


def cmd_cov(args: argparse.Namespace) -> int:
    source = args.source
    branch = not args.no_branch
    COVERAGE_FILE = str(ARTIFACT_DIR / f".coverage-{slug(source)}")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    run_cmd = [sys.executable, "-m", "coverage", "run"]
    if branch:
        run_cmd.append("--branch")
    run_cmd += [f"--source={source}", "-m", "pytest", "-q"]
    run = _run("pytest", run_cmd, env={"COVERAGE_FILE": COVERAGE_FILE})
    if run.code != 0:
        print(f"[WARN] pytest 退出码 {run.code}，仍尝试读取覆盖率")
        print(_tail(run.output, 15))

    report = _run(
        "coverage",
        [sys.executable, "-m", "coverage", "report", "--show-missing", "--fail-under=0"],
        env={"COVERAGE_FILE": COVERAGE_FILE},
    )
    rows = parse_coverage_rows(report.output, branch=branch)
    total = parse_coverage_total(report.output, branch=branch)
    if not rows or total is None:
        print("[FAIL] 没有解析到覆盖率数据")
        print(_tail(report.output))
        return 1

    rows.sort(key=lambda row: row.miss, reverse=True)
    print(f"{'文件':<48}{'语句':>6}{'未覆盖':>7}{'语句覆盖':>10}{'合并覆盖':>10}")
    for row in rows:
        print(f"{row.path:<48}{row.stmt:>6}{row.miss:>7}{row.statement_cover:>9.1f}%{row.cover:>9}%")

    result_code = 0
    target = float(args.target)
    print(
        f"\n合计：语句 {total.stmt}，未覆盖 {total.miss}，"
        f"语句覆盖 {total.statement_cover:.2f}%，合并覆盖 {total.cover}%（目标 {target:.0f}%）"
    )
    if total.statement_cover < target:
        print(f"[FAIL] 语句覆盖率未达目标 {target:.0f}%")
        result_code = 1
    else:
        print("[PASS] 语句覆盖率达标")

    path = _write_json(
        f"cov-{slug(source)}.json",
        {
            "source": source,
            "branch": branch,
            "target": target,
            "total": {**asdict(total), "statement_cover": total.statement_cover},
            "files": [{**asdict(row), "statement_cover": row.statement_cover} for row in rows],
        },
    )
    print(f"已写入 {path.relative_to(ROOT)}")
    return result_code


def gate_steps(*, with_coverage: bool) -> list[tuple[str, list[str]]]:
    """门禁步骤；顺序固定，便于 CI 日志和本地排查对齐。"""
    steps: list[tuple[str, list[str]]] = []
    if with_coverage:
        steps.append(
            (
                "pytest",
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:warnings",
                ],
            )
        )
        steps.append(
            ("coverage", [sys.executable, "-m", "coverage", "report", "--show-missing"])
        )
    else:
        steps.append(("pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings"]))
    steps += [
        ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
        ("mypy", [sys.executable, "-m", "mypy"]),
        ("lint-imports", [sys.executable, "-m", "importlinter.cli", "lint-imports"]),
        ("docs", [sys.executable, "tools/check_doc_versions.py"]),
    ]
    return [(name, cmd) for name, cmd in steps]


def cmd_gate(args: argparse.Namespace) -> int:
    steps = gate_steps(with_coverage=args.coverage)
    only = {item.strip() for item in args.only.split(",") if item.strip()} if args.only else set()
    known = {name for name, _ in steps}
    unknown = only - known
    if unknown:
        print(f"[FAIL] 未知步骤：{', '.join(sorted(unknown))}；可选：{', '.join(sorted(known))}")
        return 2
    if only:
        steps = [(name, cmd) for name, cmd in steps if name in only]

    results: list[CommandResult] = []
    for name, cmd in steps:
        print(f"==> {name}")
        env = {"COVERAGE_FILE": str(ARTIFACT_DIR / ".coverage-gate")} if args.coverage else None
        result = _run(name, cmd, env=env)
        results.append(result)
        state = "PASS" if result.code == 0 else "FAIL"
        print(f"[{state}] {name}（{result.seconds}s）")
        if result.code != 0:
            print(_tail(result.output))

    print("\n门禁汇总：")
    for result in results:
        state = "PASS" if result.code == 0 else "FAIL"
        print(f"  [{state}] {result.name:<12}{result.seconds:>7}s")

    path = _write_json(
        "gate.json",
        [
            {"name": r.name, "code": r.code, "seconds": r.seconds, "tail": _tail(r.output, 15)}
            for r in results
        ],
    )
    print(f"已写入 {path.relative_to(ROOT)}")
    failed = [r.name for r in results if r.code != 0]
    if failed:
        print(f"[FAIL] 未通过：{', '.join(failed)}")
        return 1
    print("[PASS] 全部门禁通过")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testkit",
        description="项目内测试编排：用例基线、覆盖率缺口、全量门禁。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    baseline = sub.add_parser("baseline", help="统计用例规模（选中/收集/取消选中）")
    baseline.set_defaults(func=cmd_baseline)

    cov = sub.add_parser("cov", help="采集单个包的覆盖率并列出缺口")
    cov.add_argument("source", help="coverage --source，例如 src/scheduler")
    cov.add_argument("--target", type=float, default=80.0, help="语句覆盖率目标，默认 80")
    cov.add_argument("--no-branch", action="store_true", help="只统计语句，不统计分支")
    cov.set_defaults(func=cmd_cov)

    gate = sub.add_parser("gate", help="全量门禁：pytest + ruff + mypy + 导入契约 + 文档校验")
    gate.add_argument("--coverage", action="store_true", help="pytest 带覆盖率并校验 fail_under")
    gate.add_argument("--only", default="", help="只跑指定步骤，逗号分隔：pytest,coverage,ruff,mypy,lint-imports,docs")
    gate.set_defaults(func=cmd_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
