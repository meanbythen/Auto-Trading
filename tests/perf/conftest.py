"""P3.1 性能基准夹具。

约定：
- 所有用例标 `perf`，默认 `pytest` 不收集；用 `-m perf tests/perf` 单独跑。
- 结果写到 `build/perf/result-<时间>.json`，首次可用 `PERF_WRITE_BASELINE=1` 固化成基线。
- 默认只记录、超阈值给 warning；`PERF_STRICT=1` 时才断言失败，避免开发机噪声误伤。
- 固定数据、固定种子、不联网、不读写真实 `data/`。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import statistics
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "build" / "perf"
BASELINE_PATH = RESULTS_DIR / "baseline.json"
STRICT = os.environ.get("PERF_STRICT", "") == "1"


def _percentile(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _deps_hash() -> str:
    parts = []
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml"):
        path = PROJECT_ROOT / name
        if path.exists():
            parts.append(path.read_bytes())
    return hashlib.sha256(b"".join(parts)).hexdigest()[:12]


class PerfRecorder:
    """收集一次会话的所有指标，结束后落 JSON。"""

    def __init__(self) -> None:
        self.env = self._env()
        self.records: list[dict[str, Any]] = []
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.baseline = self._load_baseline()

    @staticmethod
    def _env() -> dict[str, Any]:
        import psutil

        return {
            "machine": platform.node(),
            "platform": platform.platform(),
            "cpu": platform.processor() or platform.machine(),
            "cores": os.cpu_count(),
            "ram_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "python": platform.python_version(),
            "deps_hash": _deps_hash(),
            "strict": STRICT,
        }

    @staticmethod
    def _load_baseline() -> dict[str, float]:
        if not BASELINE_PATH.exists():
            return {}
        try:
            raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        cases = raw.get("cases") if isinstance(raw, dict) else None
        if not isinstance(cases, dict):
            return {}
        return {str(key): float(value) for key, value in cases.items() if value is not None}

    def record(
        self,
        case: str,
        samples: list[float],
        *,
        unit: str,
        threshold: float | None = None,
        higher_is_better: bool = False,
        relative_floor: float = 0.0,
        statistic: str = "p95",
        note: str = "",
    ) -> dict[str, Any]:
        values = [float(value) for value in samples]
        median = statistics.median(values) if values else 0.0
        p95 = _percentile(values, 0.95)
        # 吞吐类取中位数——3 次采样里 p95 恰好是最好那次，会偏乐观；
        # 冷启动这类"少量重复进程"用 statistic="median"，避免单次抖动（杀毒/索引）直接判红。
        headline = median if higher_is_better or statistic == "median" else p95
        baseline = self.baseline.get(case)
        # 比值统一为「本次 / 基线」：耗时类要求 ≤1.2，吞吐类要求 ≥0.85。
        ratio = None
        if baseline and baseline > 0:
            ratio = headline / baseline
        threshold_pass = True
        if threshold is not None:
            threshold_pass = headline >= threshold if higher_is_better else headline <= threshold
        ratio_pass = True
        # 亚毫秒级指标靠比率门禁没有意义，差异小于噪声下限时只看绝对阈值。
        noisy = baseline is not None and abs(headline - baseline) <= relative_floor
        if ratio is not None and not noisy:
            ratio_pass = ratio >= 0.85 if higher_is_better else ratio <= 1.2
        passed = threshold_pass and ratio_pass
        record = {
            "case": case,
            "unit": unit,
            "samples": [round(value, 3) for value in values],
            "median": round(median, 3),
            "p95": round(p95, 3),
            "headline": round(headline, 3),
            "statistic": "median" if higher_is_better or statistic == "median" else "p95",
            "min": round(min(values), 3) if values else 0.0,
            "max": round(max(values), 3) if values else 0.0,
            "threshold": threshold,
            "higher_is_better": higher_is_better,
            "relative_floor": relative_floor,
            "baseline": baseline,
            "ratio": None if ratio is None else round(ratio, 3),
            "passed": passed,
            "note": note,
        }
        self.records.append(record)
        if not passed:
            reasons = []
            if not threshold_pass:
                reasons.append(f"阈值 {threshold}{unit}，实测 {headline:.3f}{unit}")
            if not ratio_pass:
                reasons.append(f"基线 {baseline}{unit}，比值 {ratio:.3f}")
            message = f"{case} 未通过：" + "；".join(reasons)
            if STRICT:
                raise AssertionError(message)
            warnings.warn(message, stacklevel=2)
        return record

    def flush(self) -> Path:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "env": self.env,
            "cases": self.records,
        }
        path = RESULTS_DIR / f"result-{self.run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[perf] 结果已写入 {path}")
        for record in self.records:
            status = "PASS" if record["passed"] else "FAIL"
            print(
                f"[perf] {status} {record['case']} "
                f"median={record['median']} p95={record['p95']} {record['unit']}"
            )
        return path


@pytest.fixture(scope="session")
def perf_recorder() -> PerfRecorder:
    recorder = PerfRecorder()
    yield recorder
    path = recorder.flush()
    if not STRICT and os.environ.get("PERF_WRITE_BASELINE", "") == "1":
        cases = {row["case"]: row["headline"] for row in recorder.records}
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "env": recorder.env,
                    "cases": cases,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[perf] 基线已写入 {BASELINE_PATH}（来自 {path.name}）")


@pytest.fixture(scope="session")
def perf_cfg() -> dict[str, Any]:
    return {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "daily_body": {"eth_minutes": 0, "regime_filter": False},
        },
        "trade": {"symbol": "bu2612", "volume": 1, "executor": "sim"},
        "risk": {"capital": 1_000_000, "contracts": {"bu": {"multiplier": 10}}},
        "data": {"source": "demo", "cache_dir": "data/klines", "min_bars": 60},
        "schedule": {"trading_sessions": [["09:00", "23:00"]]},
        "backtest": {"chain_capital": False},
    }


@pytest.fixture(scope="session")
def perf_bars() -> pd.DataFrame:
    """4000 根固定 15 分钟 K 线，固定种子，不依赖网络与 demo 的进程内哈希。"""
    count = 4000
    index = pd.date_range("2024-01-02 09:00", periods=count, freq="15min", name="datetime")
    rng = np.random.default_rng(20261124)
    returns = rng.normal(0.0, 0.0008, count)
    close = 3800.0 * np.exp(np.cumsum(returns))
    open_ = np.concatenate(([close[0]], close[:-1]))
    spread = np.abs(rng.normal(0.0, 1.5, count)) + 0.5
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(50, 500, count).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


@pytest.fixture
def perf_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把相对路径写入限制在临时目录，避免碰真实 data/。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(scope="session")
def perf_timer():
    def _measure(func, repeats: int = 1) -> list[float]:
        samples = []
        for _ in range(repeats):
            started = time.perf_counter()
            func()
            samples.append((time.perf_counter() - started) * 1000.0)
        return samples

    return _measure
