"""
允许用下面两种方式启动图形界面：

  1) 推荐（在项目根目录）:
       python -m src.ui

  2) 在 Cursor 里直接点运行本文件也可以
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_project_root_on_path() -> None:
    """把项目根目录加入 sys.path，这样直接运行本文件时也能 import src。"""
    # 本文件在 src/ui/ 下，向上两级 = Auto-Trading/
    root = Path(__file__).resolve().parents[2]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


_ensure_project_root_on_path()

# 直接运行时没有包上下文，不能用相对导入 from .app
from src.ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
