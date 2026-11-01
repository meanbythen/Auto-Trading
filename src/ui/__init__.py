# 图形界面子包
"""Auto-Trading 桌面控制台。"""

__all__ = ["run_app"]


def run_app() -> None:
    """启动图形界面（延迟导入，避免无 GUI 环境误加载）。"""
    from .app import main

    main()
