"""检查版本号与系统全貌文档的统计数字是否同步。

每次发版前运行：

    python tools/check_doc_versions.py

检查项：
- VERSION 是 X.Y.Z；
- README、CHANGELOG、TODO、架构报告都写着同一个版本号；
- 架构报告与整体设计方案里的 src 文件数、行数、测试文件数，与实际一致。

全部一致时退出码 0；有不一致打印明细并退出码 1，不修改任何文件。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
TODO = ROOT / "docs" / "TODO.md"
ARCHITECTURE = ROOT / "docs" / "auto-trading-architecture" / "自动交易系统完整架构.html"
DESIGN = ROOT / "docs" / "project-review-2026" / "整体设计方案.html"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

failures: list[str] = []


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check(label: str, ok: bool, detail: str) -> None:
    if ok:
        print(f"[OK]   {label}")
        return
    print(f"[FAIL] {label}: {detail}")
    failures.append(label)


def count_stats() -> tuple[int, int, int]:
    src_files = sorted((ROOT / "src").rglob("*.py"))
    src_lines = sum(len(path.read_text(encoding="utf-8", errors="ignore").splitlines()) for path in src_files)
    test_files = sorted((ROOT / "tests").rglob("test_*.py"))
    return len(src_files), src_lines, len(test_files)


def main() -> int:
    version = read(VERSION_FILE).strip()
    check("VERSION 格式", bool(VERSION_RE.match(version)), f"当前值 {version!r}，应为 X.Y.Z")
    if failures:
        print("\n版本号格式不正确，后续检查已跳过。")
        return 1

    readme = read(README)
    check("README 版本出现 3 次", readme.count(version) >= 3, f"只找到 {readme.count(version)} 次")

    changelog = read(CHANGELOG)
    top = re.search(r"^## (\d+\.\d+\.\d+)", changelog, flags=re.MULTILINE)
    check("CHANGELOG 顶部版本", bool(top) and top.group(1) == version, f"顶部是 {top.group(1) if top else '空'}，应为 {version}")

    todo = read(TODO)
    check("TODO 版本", f"对应版本 **{version}**" in todo, "顶部「对应版本」与 VERSION 不一致")

    architecture = read(ARCHITECTURE)
    chip = re.search(r'<span class="chip">版本 <b>(\d+\.\d+\.\d+)</b></span>', architecture)
    check("架构报告版本", bool(chip) and chip.group(1) == version, f"封面是 {chip.group(1) if chip else '空'}，应为 {version}")

    files, lines, tests = count_stats()
    for label, text in (("架构报告", architecture), ("整体设计方案", read(DESIGN))):
        check(f"{label} 文件数", f"{files} 个 Python 文件" in text, f"实际 {files}，请更新文档")
        check(f"{label} 行数", f"{lines:,} 行" in text, f"实际 {lines:,}，请更新文档")
    check("架构报告测试数", f"{tests} 个测试文件" in architecture or "测试文件" not in architecture, f"实际 {tests}")

    print()
    if failures:
        print(f"共 {len(failures)} 项不一致，请按上方提示更新文档后重跑。")
        return 1
    print(f"版本 {version} 与文档统计一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
