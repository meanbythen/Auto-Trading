"""长稳采样器：启动目标进程，按固定间隔采资源与存储指标。

功能：
    1. 每 30 秒采样 RSS、私有字节、线程数、句柄数、CPU；
    2. 每 1 小时记录 SQLite/WAL、日志、告警、发件箱积压、磁盘剩余空间；
    3. 每 6 小时写一次检查点摘要，结束时写完整 Markdown；
    4. 进程退出、磁盘不足、RSS 持续上升时自动停止并留证。

用法：
    python tools/perf_soak.py --hours 24 --symbols 10
    python tools/perf_soak.py --hours 72 --interval 30
    python tools/perf_soak.py --command ".venv/Scripts/python.exe -m src.main" --hours 24
    python tools/perf_soak.py --dry-run --hours 0 --max-samples 3 --interval 0

产物：
    build/perf/soak-<时间>.jsonl   原始采样，每行一条
    build/perf/soak-<时间>.md      摘要，可直接贴进报告
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SEC = 30.0
STORAGE_SEC = 3600.0
CHECKPOINT_SEC = 6 * 3600.0
WARMUP_SEC = 1800.0
MB = 1024.0 * 1024.0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _mb(value: float | None) -> float | None:
    return None if value is None else round(value / MB, 2)


def _file_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / MB, 2) if path.exists() else 0.0
    except OSError:
        return 0.0


def _dir_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return round(total / MB, 2)


def _outbox_counts(db_path: Path) -> tuple[int | None, int | None]:
    """只读统计待入账与失败事件；库不存在视为 0，读不到返回 None。"""
    if not db_path.exists():
        return 0, 0
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM event_outbox WHERE status IN ('pending', 'ready')"
            ).fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM event_outbox WHERE status = 'failed'").fetchone()[0]
        return int(pending), int(failed)
    except (sqlite3.Error, OSError):
        return None, None


def collect_storage(root: Path) -> dict[str, Any]:
    """采集存储与积压指标，全部只读。"""
    db_path = root / "data" / "trading.db"
    pending, failed = _outbox_counts(db_path)
    try:
        free_gb = round(psutil.disk_usage(str(root)).free / (MB * 1024), 1)
    except OSError:
        free_gb = None
    return {
        "trading_db_mb": _file_mb(db_path),
        "wal_mb": _file_mb(Path(str(db_path) + "-wal")),
        "logs_mb": _dir_mb(root / "logs"),
        "alerts_mb": _file_mb(root / "data" / "alerts.jsonl"),
        "outbox_pending": pending,
        "outbox_failed": failed,
        "disk_free_gb": free_gb,
    }


def sample_process(proc: psutil.Process) -> dict[str, Any]:
    """采一次进程资源。进程已退出时由调用方捕获 psutil.Error。"""
    memory = proc.memory_info()
    private = getattr(memory, "private", None)
    handles: int | None = None
    if hasattr(proc, "num_handles"):
        try:
            handles = proc.num_handles()
        except (AttributeError, psutil.Error):
            handles = None
    return {
        "rss_mb": _mb(memory.rss),
        "private_mb": _mb(private) if private is not None else None,
        "threads": proc.num_threads(),
        "handles": handles,
        "cpu_pct": round(proc.cpu_percent(None), 1),
    }


def _termination_reason(record: dict[str, Any], samples: list[dict[str, Any]]) -> str:
    """按 P3 方案 7.3 的自动终止条件判断。"""
    storage = record.get("storage") or {}
    free_gb = storage.get("disk_free_gb")
    if free_gb is not None and free_gb < 1.0:
        return "磁盘剩余空间低于 1 GB"

    warm = [
        float(item["rss_mb"])
        for item in samples
        if float(item.get("elapsed_sec") or 0.0) >= WARMUP_SEC and item.get("rss_mb") is not None
    ]
    if len(warm) >= 3 and warm[-1] > warm[-2] > warm[-3] and warm[-1] - warm[0] > 200:
        return "RSS 连续 3 个采样点上升且累计超过 200 MB"
    return ""


def _stat(samples: list[dict[str, Any]], key: str) -> dict[str, float] | None:
    values = [float(item[key]) for item in samples if item.get(key) is not None]
    if not values:
        return None
    warm = [float(item[key]) for item in samples if item.get(key) is not None and float(item.get("elapsed_sec") or 0) >= WARMUP_SEC]
    start = warm[0] if warm else values[0]
    end = warm[-1] if warm else values[-1]
    return {"start": start, "end": end, "peak": max(values), "min": min(values)}


def _fmt(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def build_summary(samples: list[dict[str, Any]], meta: dict[str, Any], reasons: list[str]) -> str:
    """生成 Markdown 摘要；samples 可以为空。"""
    env = meta.get("env") or {}
    lines = ["# 长稳采样摘要", ""]
    lines.append(f"- 开始：{meta.get('started_at') or '—'}")
    lines.append(f"- 结束：{meta.get('ended_at') or '—'}")
    lines.append(f"- 模式：{'dry-run（采样器自身）' if meta.get('dry_run') else '目标进程'}")
    lines.append(f"- 目标命令：`{' '.join(meta.get('command') or [])}`")
    lines.append(f"- 品种数：{meta.get('symbols')}，采样间隔：{meta.get('interval_sec')} s")
    lines.append(f"- 原始数据：`{meta.get('jsonl_path') or '—'}`")
    lines.append(f"- 样本数：{len(samples)}")
    lines.append("")

    lines.append("## 环境")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 机器 | {env.get('machine') or '—'} |")
    lines.append(f"| 系统 | {env.get('platform') or '—'} |")
    lines.append(f"| CPU | {env.get('cpu') or '—'} |")
    lines.append(f"| 核数 | {env.get('cores') or '—'} |")
    lines.append(f"| 内存 | {str(env.get('ram_gb') or '—')} GB |")
    lines.append(f"| Python | {env.get('python') or '—'} |")
    lines.append("")

    lines.append("## 资源（预热 30 分钟后统计）")
    lines.append("")
    if samples:
        lines.append("| 指标 | 起点 | 峰值 | 结束 | 变化 |")
        lines.append("|---|---|---|---|---|")
        for key, label, digits in (
            ("rss_mb", "RSS MB", 1),
            ("private_mb", "私有 MB", 1),
            ("threads", "线程", 0),
            ("handles", "句柄", 0),
        ):
            stat = _stat(samples, key)
            if stat is None:
                lines.append(f"| {label} | — | — | — | — |")
                continue
            change = stat["end"] - stat["start"]
            lines.append(
                f"| {label} | {_fmt(stat['start'], digits)} | {_fmt(stat['peak'], digits)} | "
                f"{_fmt(stat['end'], digits)} | {change:+.0f} |"
            )
        cpu_values = [float(item["cpu_pct"]) for item in samples if item.get("cpu_pct") is not None]
        average = sum(cpu_values) / len(cpu_values) if cpu_values else None
        lines.append("")
        lines.append(f"CPU 平均：{_fmt(average)}%；峰值：{_fmt(max(cpu_values) if cpu_values else None)}%")
    else:
        lines.append("没有采集到样本。")
    lines.append("")

    storage_samples = [item for item in samples if item.get("storage")]
    lines.append("## 存储（每小时）")
    lines.append("")
    if storage_samples:
        lines.append(
            "| 时间 | DB MB | WAL MB | 日志 MB | 告警 MB | 发件箱 pending | failed | 磁盘剩余 GB |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for item in storage_samples:
            store = item["storage"]
            lines.append(
                f"| {str(item.get('ts') or '')[11:19]} | {_fmt(store.get('trading_db_mb'), 2)} | "
                f"{_fmt(store.get('wal_mb'), 2)} | {_fmt(store.get('logs_mb'), 1)} | "
                f"{_fmt(store.get('alerts_mb'), 2)} | {_fmt(store.get('outbox_pending'), 0)} | "
                f"{_fmt(store.get('outbox_failed'), 0)} | {_fmt(store.get('disk_free_gb'), 1)} |"
            )
    else:
        lines.append("采样时长不足 1 小时，没有存储采样。")
    lines.append("")

    checkpoints = [item for item in samples if item.get("checkpoint")]
    lines.append("## 检查点")
    lines.append("")
    if checkpoints:
        for item in checkpoints:
            lines.append(
                f"- {item.get('ts')}：RSS {_fmt(item.get('rss_mb'))} MB，"
                f"线程 {_fmt(item.get('threads'), 0)}，句柄 {_fmt(item.get('handles'), 0)}"
            )
    else:
        lines.append("采样时长不足 6 小时，没有 6 小时检查点。")
    lines.append("")

    lines.append("## 终止原因")
    lines.append("")
    if reasons:
        for reason in reasons:
            lines.append(f"- {reason}")
    else:
        lines.append("- 无（按计划时长结束或达到最大样本数）")
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    if not samples:
        lines.append("没有采集到样本，先检查目标进程是否启动成功。")
    elif reasons:
        lines.append("未通过：触发自动终止条件，请保留上述原因与原始 JSONL，按检查表排查。")
    else:
        lines.append("采样期间未触发自动终止条件。仍需按 `docs/perf-soak-checklist.md` 复核内存/句柄趋势、")
        lines.append("SQLite/WAL 增长、日志轮转与发件箱积压后再给出最终结论。")
    return "\n".join(lines) + "\n"


def _parse_command(raw: str) -> list[str]:
    if not raw.strip():
        return [sys.executable, "-m", "src.main"]
    parts = shlex.split(raw, posix=False)
    return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in parts]


def _env() -> dict[str, Any]:
    return {
        "machine": platform.node(),
        "platform": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
        "cores": os.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / (1024**3), 1),
        "python": platform.python_version(),
    }


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = max(0.0, float(args.hours) * 3600.0)
    max_samples = max(0, int(args.max_samples))
    interval = max(0.0, float(args.interval))
    if duration <= 0 and max_samples <= 0:
        print("必须指定 --hours > 0 或 --max-samples > 0", file=sys.stderr)
        return 2
    if duration > 0 and interval <= 0:
        print("--hours > 0 时 --interval 必须大于 0", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = output_dir / f"soak-{stamp}.jsonl"
    md_path = output_dir / f"soak-{stamp}.md"
    command = _parse_command(args.command)

    meta: dict[str, Any] = {
        "started_at": _now(),
        "ended_at": "",
        "dry_run": bool(args.dry_run),
        "command": command,
        "symbols": args.symbols,
        "interval_sec": interval,
        "planned_hours": args.hours,
        "root": str(root),
        "jsonl_path": str(jsonl_path),
        "env": _env(),
    }

    child: subprocess.Popen[bytes] | None = None
    samples: list[dict[str, Any]] = []
    reasons: list[str] = []
    exit_code = 0

    try:
        if not args.dry_run:
            child = subprocess.Popen(command, cwd=str(root))
        target = psutil.Process(child.pid) if child is not None else psutil.Process(os.getpid())
        target.cpu_percent(None)  # 第一次调用只建立基线

        started = time.monotonic()
        deadline = started + duration if duration > 0 else None
        next_storage = started
        next_checkpoint = started + CHECKPOINT_SEC

        with jsonl_path.open("w", encoding="utf-8") as handle:
            while True:
                if max_samples and len(samples) >= max_samples:
                    break
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break
                if child is not None and child.poll() is not None:
                    reasons.append(f"目标进程已退出，返回码 {child.returncode}")
                    exit_code = 1
                    break
                try:
                    record: dict[str, Any] = {
                        "ts": _now(),
                        "elapsed_sec": round(now - started, 1),
                        **sample_process(target),
                    }
                except psutil.Error as exc:
                    reasons.append(f"目标进程不可采样：{exc}")
                    exit_code = 1
                    break

                if now >= next_storage:
                    record["storage"] = collect_storage(root)
                    next_storage = now + STORAGE_SEC
                if now >= next_checkpoint:
                    record["checkpoint"] = True
                    next_checkpoint = now + CHECKPOINT_SEC

                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                samples.append(record)

                reason = _termination_reason(record, samples)
                if reason:
                    reasons.append(reason)
                    exit_code = 1
                    break
                if record.get("checkpoint"):
                    md_path.write_text(build_summary(samples, meta, reasons), encoding="utf-8")

                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(interval, remaining))
                else:
                    time.sleep(interval)
    except KeyboardInterrupt:
        reasons.append("收到 Ctrl+C，人工停止")
        exit_code = 130
    except OSError as exc:
        reasons.append(f"无法启动目标进程：{exc}")
        exit_code = 2
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        meta["ended_at"] = _now()
        md_path.write_text(build_summary(samples, meta, reasons), encoding="utf-8")
        print(f"原始采样：{jsonl_path}")
        print(f"摘要报告：{md_path}")
        if reasons:
            print("终止原因：" + "；".join(reasons))

    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长稳采样：定时记录进程资源与存储增长")
    parser.add_argument("--hours", type=float, default=24.0, help="计划时长（小时，默认 24；0 表示只按 --max-samples）")
    parser.add_argument("--interval", type=float, default=SAMPLE_SEC, help="采样间隔秒数（默认 30）")
    parser.add_argument("--symbols", type=int, default=10, help="当时运行的品种数（仅写入报告）")
    parser.add_argument("--root", default=str(_ROOT), help="项目根目录（默认仓库根）")
    parser.add_argument("--output-dir", default="build/perf", help="输出目录（默认 build/perf）")
    parser.add_argument("--command", default="", help="目标命令，默认当前解释器 -m src.main")
    parser.add_argument("--dry-run", action="store_true", help="不启动目标进程，只采采样器自身")
    parser.add_argument("--max-samples", type=int, default=0, help="最多采多少条，0 表示不限（用于冒烟）")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
