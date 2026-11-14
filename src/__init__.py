# 包标识：让 Python 可以把 src 当作一个包来导入
# 原理：有了 __init__.py，就能使用 python -m src.main 这种方式启动
"""Auto-Trading 期货自动交易机器人包。"""

# !!! 必须在导入任何 src 子模块之前清理陈旧缓存
# 改过代码后，旧 __pycache__ 可能让报错行号对不上、界面行为和代码对不上。
# 包初始化是 python -m src.main / python -m src.ui / 测试的共同入口，
# 在这里清掉 src/ 下所有 __pycache__，代价只是每次启动重新编译（不足一秒）。
import shutil as _shutil
from pathlib import Path as _Path


def _pycache_stale(base: _Path) -> bool:
    """是否存在比对应 .py 更新的 .pyc；没有就不删缓存，避免每次启动都重编译。"""
    for cache in base.rglob("__pycache__"):
        py_dir = cache.parent
        for pyc in cache.glob("*.pyc"):
            stem = pyc.name.split(".")[0]
            py_file = py_dir / f"{stem}.py"
            if py_file.exists() and py_file.stat().st_mtime > pyc.stat().st_mtime:
                return True
    return False


def clear_pycache(root: _Path | None = None) -> int:
    """删掉 root（默认 src/ 目录）下所有 __pycache__，返回删掉的个数。

    只删缓存目录，不动 .py 源文件；任何错误都不影响启动。
    默认 root（src/ 包目录）时先检查缓存是否过期，没过期就跳过——避免每次 import 都重编译。
    显式传入 root 时无条件清（调用方就是想清）。
    """
    base = _Path(root) if root is not None else _Path(__file__).resolve().parent
    removed = 0
    try:
        if root is None and not _pycache_stale(base):
            return 0
        for cache in base.rglob("__pycache__"):
            if cache.is_dir():
                _shutil.rmtree(cache, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed


clear_pycache()

from src.version import __version__  # noqa: F401
