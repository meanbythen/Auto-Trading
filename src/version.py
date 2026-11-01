"""
程序版本号。

界面标题、日志和更新日志都读项目根目录的 VERSION 这一处。
不要在别的文件里再写一个版本号，避免改了一处、另一处还是旧的。
"""

from __future__ import annotations

from pathlib import Path


def read_version() -> str:
    """
    读 VERSION 文件。文件丢了就返回 0.0.0，避免界面因为这个打不开。

    源码运行时，文件在项目根目录。
    打包成 exe 后，源码文件的位置变了，根目录那一层对不上，所以要改从打包资源目录读。
    资源目录在打包时由 PyInstaller 填好，里面必须带上 VERSION。
    """
    from src.utils.paths import resource_root

    path = resource_root() / "VERSION"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return text or "0.0.0"


# 启动时读一次。改版本号后要重新打开程序才能在窗口上看到。
__version__ = read_version()
