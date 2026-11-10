"""启动时自动清理陈旧 __pycache__ 的自愈逻辑。"""

from __future__ import annotations

from src import clear_pycache


def test_clear_pycache_removes_cache_dirs(tmp_path) -> None:
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "x.cpython-312.pyc").write_bytes(b"fake")
    (tmp_path / "pkg" / "x.py").write_text("x = 1", encoding="utf-8")

    removed = clear_pycache(tmp_path)

    assert removed == 1
    assert not cache.exists()
    # 源文件原样保留
    assert (tmp_path / "pkg" / "x.py").read_text(encoding="utf-8") == "x = 1"


def test_clear_pycache_ignores_dirs_without_cache(tmp_path) -> None:
    (tmp_path / "a.py").write_text("a = 1", encoding="utf-8")

    assert clear_pycache(tmp_path) == 0
    assert (tmp_path / "a.py").exists()


def test_clear_pycache_survives_missing_root(tmp_path) -> None:
    assert clear_pycache(tmp_path / "不存在的目录") == 0
