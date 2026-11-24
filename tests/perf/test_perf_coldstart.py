"""PERF-01：冷启动里最容易被依赖拖慢的一段——导入主程序模块。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_perf_import_main_app(perf_recorder) -> None:
    samples: list[float] = []
    for _ in range(5):
        # 单独起进程，避免本进程已导入的模块把耗时抹平。
        started = time.perf_counter()
        subprocess.run(
            [sys.executable, "-c", "import src.ui.app"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
        )
        samples.append((time.perf_counter() - started) * 1000.0)
    perf_recorder.record(
        "PERF-01",
        samples,
        unit="ms",
        threshold=3000,
        relative_floor=100,
        statistic="median",
        note="子进程导入 src.ui.app，5 次取中位数（含解释器启动与 tkinter/customtkinter）",
    )
