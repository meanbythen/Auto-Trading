"""build_exe 的纯函数：必需文件清单、缺失检测、摘要与体积统计。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tools.build_exe import REQUIRED, dir_size_mb, missing_required, sha256


def test_required_covers_runtime_assets() -> None:
    """回归护栏：schema.sql 与 CTA 模板曾漏打，必须留在必需清单里。"""
    assert "src/account/schema.sql" in REQUIRED
    assert "config/多因子复合CTA回测评估与因子衰减监控模板.xlsx" in REQUIRED
    assert "config/品种与合约.xlsx" in REQUIRED
    assert "config/holidays.yaml" in REQUIRED


def test_missing_required_lists_everything_on_empty_tree(tmp_path: Path) -> None:
    assert missing_required(tmp_path) == list(REQUIRED)


def test_missing_required_empty_when_all_present(tmp_path: Path) -> None:
    for rel in REQUIRED:
        target = tmp_path / rel
        if rel.endswith((".yaml", ".xlsx", ".sql")) or rel in {"VERSION", "app_entry.py", "Auto-Trading.spec"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
    assert missing_required(tmp_path) == []


def test_sha256_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"auto-trading")
    assert sha256(path) == hashlib.sha256(b"auto-trading").hexdigest().upper()


def test_dir_size_mb_sums_nested_files(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.bin").write_bytes(b"0" * (1024 * 1024))
    (tmp_path / "sub" / "b.bin").write_bytes(b"0" * (512 * 1024))
    assert dir_size_mb(tmp_path) == 1.5
