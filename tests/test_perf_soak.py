"""`tools/perf_soak.py` 的单测：存储统计、终止条件与 dry-run 冒烟。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tools import perf_soak


def _make_outbox(db_path: Path, statuses: list[str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE event_outbox (seq INTEGER PRIMARY KEY, status TEXT NOT NULL)")
        conn.executemany("INSERT INTO event_outbox(status) VALUES (?)", [(status,) for status in statuses])


def test_collect_storage_counts_outbox_and_files(tmp_path: Path) -> None:
    _make_outbox(tmp_path / "data" / "trading.db", ["pending", "ready", "pending", "done", "failed"])
    log_file = tmp_path / "logs" / "2026" / "app.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_bytes(b"x" * (1024 * 1024))

    storage = perf_soak.collect_storage(tmp_path)

    assert storage["outbox_pending"] == 3
    assert storage["outbox_failed"] == 1
    assert storage["trading_db_mb"] > 0
    assert storage["logs_mb"] == 1.0
    assert storage["disk_free_gb"] > 1.0


def test_collect_storage_without_database_is_zero(tmp_path: Path) -> None:
    storage = perf_soak.collect_storage(tmp_path)

    assert storage["outbox_pending"] == 0
    assert storage["outbox_failed"] == 0
    assert storage["trading_db_mb"] == 0.0


def test_termination_reason_detects_low_disk() -> None:
    record = {"storage": {"disk_free_gb": 0.2}, "rss_mb": 100.0, "elapsed_sec": 0.0}

    assert "磁盘剩余空间" in perf_soak._termination_reason(record, [record])


def test_termination_reason_detects_rss_climb() -> None:
    samples = [
        {"elapsed_sec": 1800.0, "rss_mb": 100.0},
        {"elapsed_sec": 1830.0, "rss_mb": 200.0},
        {"elapsed_sec": 1860.0, "rss_mb": 310.0},
    ]

    assert "RSS 连续 3 个采样点" in perf_soak._termination_reason(samples[-1], samples)


def test_termination_reason_ignores_warmup_noise() -> None:
    samples = [
        {"elapsed_sec": 0.0, "rss_mb": 100.0},
        {"elapsed_sec": 30.0, "rss_mb": 500.0},
        {"elapsed_sec": 60.0, "rss_mb": 600.0},
    ]

    assert perf_soak._termination_reason(samples[-1], samples) == ""


def test_build_summary_contains_resource_and_checkpoint_sections() -> None:
    samples = [
        {
            "ts": "2026-11-24T10:00:00",
            "elapsed_sec": 0.0,
            "rss_mb": 100.0,
            "private_mb": 80.0,
            "threads": 10,
            "handles": 100,
            "cpu_pct": 1.0,
            "storage": {
                "trading_db_mb": 2.0,
                "wal_mb": 0.1,
                "logs_mb": 3.0,
                "alerts_mb": 0.0,
                "outbox_pending": 2,
                "outbox_failed": 0,
                "disk_free_gb": 50.0,
            },
        },
        {
            "ts": "2026-11-24T16:00:00",
            "elapsed_sec": 21600.0,
            "rss_mb": 130.0,
            "private_mb": 100.0,
            "threads": 11,
            "handles": 110,
            "cpu_pct": 2.0,
            "checkpoint": True,
        },
    ]
    meta = {
        "started_at": "2026-11-24T10:00:00",
        "ended_at": "2026-11-24T16:00:00",
        "dry_run": True,
        "command": ["python", "-m", "src.main"],
        "symbols": 10,
        "interval_sec": 30.0,
        "jsonl_path": "soak.jsonl",
        "env": {"machine": "TEST-PC", "platform": "Windows", "cpu": "CPU", "cores": 8, "ram_gb": 16, "python": "3.12"},
    }

    summary = perf_soak.build_summary(samples, meta, [])

    assert "长稳采样摘要" in summary
    assert "RSS MB" in summary
    assert "检查点" in summary
    assert "2026-11-24T16:00:00" in summary
    assert "未触发自动终止条件" in summary


def test_parse_command_default_and_quoted() -> None:
    default = perf_soak._parse_command("")
    assert default[-2:] == ["-m", "src.main"]

    parsed = perf_soak._parse_command('"C:\\Program Files\\Python\\python.exe" -m src.main')
    assert parsed[0] == "C:\\Program Files\\Python\\python.exe"
    assert parsed[1:] == ["-m", "src.main"]


def test_dry_run_writes_jsonl_and_markdown(tmp_path: Path) -> None:
    output_dir = tmp_path / "perf"

    code = perf_soak.main(
        [
            "--dry-run",
            "--hours",
            "0",
            "--interval",
            "0",
            "--max-samples",
            "3",
            "--root",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert code == 0
    jsonl_files = list(output_dir.glob("soak-*.jsonl"))
    md_files = list(output_dir.glob("soak-*.md"))
    assert len(jsonl_files) == 1
    assert len(md_files) == 1

    records = [json.loads(line) for line in jsonl_files[0].read_text(encoding="utf-8").splitlines()]
    assert len(records) == 3
    assert "rss_mb" in records[0]
    assert "storage" in records[0]
    assert "长稳采样摘要" in md_files[0].read_text(encoding="utf-8")


def test_missing_duration_and_max_samples_returns_two(tmp_path: Path) -> None:
    code = perf_soak.main(["--hours", "0", "--root", str(tmp_path), "--output-dir", str(tmp_path / "perf")])

    assert code == 2
