"""
项目路径（兼容源码运行 / PyInstaller 打包）
功能：统一返回「可读写的项目根目录」，以及打包资源目录。
原理：
- 源码：根目录 = Auto-Trading/
- 打包 exe：根目录 = exe 所在文件夹（配置、日志、data 写在这里）
- 内置资源（示例配置等）在 sys._MEIPASS，首次运行可复制到 exe 旁
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否处于 PyInstaller 等打包后的冻结环境。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """
    可读写的项目根目录。

    - 开发：仓库根目录
    - 打包：Auto-Trading.exe 所在目录（不要用 _MEIPASS，那里只读）
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # src/utils/paths.py → parents[2] = 项目根
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """
    只读资源根目录（打包内的默认文件）。

    开发时与 project_root 相同；打包时为 _MEIPASS。
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return project_root()
