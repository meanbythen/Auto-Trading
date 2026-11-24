"""一条命令完成 EXE 打包：预检 → 清理 → PyInstaller → 收尾 → 验收 → 元数据 → 冒烟。

为什么要这个脚本：
- 固定 PyInstaller 版本、固定 spec，避免「本机能打、换台机器就缺文件」；
- 只清理 PyInstaller 的 build/dist 子目录，不动 build/testkit 里的覆盖率产物；
- 收尾和验收失败立即中止，不再出现「打包成功但功能坏了」；
- 产物元数据（版本/大小/SHA256/时间）写入 build/packaging/build-info.json，可回溯。

用法：
    python tools/build_exe.py                # 完整流程，含 12 秒启动冒烟
    python tools/build_exe.py --no-smoke     # 不启动 exe（CI 或本机已有实例时）
    python tools/build_exe.py --no-clean     # 保留上次 Analysis 缓存，仅排错用
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "Auto-Trading"
BUILD = ROOT / "build" / "Auto-Trading"
SPEC = ROOT / "Auto-Trading.spec"
EXE = DIST / "Auto-Trading.exe"
INFO_PATH = ROOT / "build" / "packaging" / "build-info.json"

# 与 requirements-build.txt、CI 保持一致；版本漂移是「换台机器就缺依赖」的常见原因。
PINNED_PYINSTALLER = "6.22.3"
SMOKE_SECONDS = 12

REQUIRED = (
    "VERSION",
    "app_entry.py",
    "Auto-Trading.spec",
    "config/config.example.yaml",
    "config/品种与合约.xlsx",
    "config/holidays.yaml",
    "config/多因子复合CTA回测评估与因子衰减监控模板.xlsx",
    "config/rules",
    "strategies",
    "src/account/schema.sql",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def dir_size_mb(path: Path) -> float:
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return round(total / (1024 * 1024), 2)


def missing_required(root: Path = ROOT) -> list[str]:
    """返回缺失的发布必需文件；spec 和 finish_dist 也会再挡一次，这里只是尽早报错。"""
    return [rel for rel in REQUIRED if not (root / rel).exists()]


def _run(cmd: list[str], *, title: str, cwd: Path = ROOT) -> int:
    print(f"\n==> {title}")
    print("    " + " ".join(cmd))
    started = time.monotonic()
    code = subprocess.call(cmd, cwd=cwd)
    print(f"<== {title} 退出码 {code}，用时 {time.monotonic() - started:.1f}s")
    return code


def _pyinstaller_version(python: str) -> str:
    result = subprocess.run(
        [python, "-c", "import PyInstaller; print(PyInstaller.__version__)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _preflight(python: str, *, skip_compile: bool) -> int:
    print(f"Python: {sys.version.split()[0]} ({python})")
    version = _pyinstaller_version(python)
    if not version:
        print("[失败] 未安装 PyInstaller，请先执行：")
        print(f"  {python} -m pip install -r requirements-build.txt")
        return 1
    print(f"PyInstaller: {version}（固定版本 {PINNED_PYINSTALLER}）")
    if version != PINNED_PYINSTALLER:
        print(
            f"[警告] PyInstaller 版本与固定值不一致；发布包请统一为 "
            f"{PINNED_PYINSTALLER}，否则不同机器可能打出不同结果。"
        )

    missing = missing_required()
    if missing:
        print("[失败] 源码树缺少发布必需文件：")
        for rel in missing:
            print(f"  - {rel}")
        return 1

    if not skip_compile:
        code = _run(
            [python, "-m", "compileall", "-q", "src", "tools", "app_entry.py"],
            title="源码编译检查",
        )
        if code != 0:
            return code
    return 0


def _clean() -> None:
    # 只删 PyInstaller 的产物；build/testkit 下的覆盖率与基线 JSON 必须保留。
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"已清理 {path.relative_to(ROOT)}")


def _smoke() -> int:
    if not EXE.exists():
        print(f"[失败] 没有找到 {EXE}")
        return 1
    print(f"\n==> 启动冒烟：等待 {SMOKE_SECONDS} 秒，确认进程不退出")
    process = subprocess.Popen(  # noqa: S603 - 只启动本项目刚打出的 exe
        [str(EXE)],
        cwd=str(DIST),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(SMOKE_SECONDS)
        code = process.poll()
        if code is None:
            print(f"[通过] exe 存活 {SMOKE_SECONDS} 秒")
            return 0
        print(f"[失败] exe 提前退出，退出码 {code}")
        print("  排查：确认没有已在运行的 Auto-Trading（单实例锁会直接退出）；")
        print("  仍失败时临时把 spec 的 console 改为 True 重打，查看 traceback。")
        return 1
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def _write_info() -> dict[str, object]:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    info = {
        "version": version,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exe_size_mb": round(EXE.stat().st_size / (1024 * 1024), 2),
        "dist_size_mb": dir_size_mb(DIST),
        "sha256": sha256(EXE),
        "pyinstaller": PINNED_PYINSTALLER,
        "layout": "onedir",
    }
    INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    INFO_PATH.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_exe", description="Auto-Trading EXE 打包")
    parser.add_argument("--no-smoke", action="store_true", help="跳过启动冒烟")
    parser.add_argument("--no-clean", action="store_true", help="保留 build/dist 旧产物（仅排错用）")
    parser.add_argument("--skip-compile", action="store_true", help="跳过 compileall")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    python = sys.executable
    started = time.monotonic()

    print("=" * 56)
    print("  Auto-Trading 打包为独立运行程序（onedir）")
    print("=" * 56)

    code = _preflight(python, skip_compile=args.skip_compile)
    if code != 0:
        return code

    if not args.no_clean:
        _clean()

    code = _run(
        [python, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean"],
        title="PyInstaller 打包",
    )
    if code != 0:
        print("\n打包失败。先看上面第一条报错；必要时用 --no-clean 保留缓存排查。")
        return code

    for script, title in (
        ("tools/finish_dist.py", "整理发布目录"),
        ("tools/verify_dist.py", "验收发布包"),
    ):
        code = _run([python, script], title=title)
        if code != 0:
            print(f"\n[失败] {title}未通过，未生成可分发版本。")
            return code

    if args.no_smoke:
        print("\n已跳过启动冒烟（--no-smoke）")
    else:
        code = _smoke()
        if code != 0:
            return code

    info = _write_info()
    print("\n" + "=" * 56)
    print("打包完成")
    print(f"  版本      {info['version']}")
    print(f"  exe 大小  {info['exe_size_mb']} MB")
    print(f"  目录大小  {info['dist_size_mb']} MB")
    print(f"  SHA256    {info['sha256']}")
    print(f"  元数据    {INFO_PATH.relative_to(ROOT)}")
    print(f"  总用时    {time.monotonic() - started:.1f}s")
    print("  分发时请拷贝整个 dist\\Auto-Trading 文件夹，不要只拷 exe。")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
