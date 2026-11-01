"""版本号只从 VERSION 文件来，并且必须是三段数字。"""

import sys
from pathlib import Path

from src.version import __version__, read_version


def test_version_file_is_three_numbers() -> None:
    text = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text(encoding="utf-8").strip()
    parts = text.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
    assert read_version() == text
    assert __version__ == text


def test_packaged_program_reads_the_bundled_version(tmp_path, monkeypatch) -> None:
    """模拟打包后的环境：版本文件在资源目录里，不在源码那一层。"""
    (tmp_path / "VERSION").write_text("9.8.7\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert read_version() == "9.8.7"


def test_missing_version_file_shows_placeholder(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert read_version() == "0.0.0"
