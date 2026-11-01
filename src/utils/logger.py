"""
日志模块
功能：把运行过程写入控制台和 logs 目录文件，方便事后排查「为什么下了单/为什么没下单」。
原理：使用 loguru，一条配置同时输出到屏幕和按日期滚动的日志文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logger(log_dir: str = "logs", level: str = "INFO") -> None:
    """
    初始化全局日志。

    参数:
        log_dir: 日志目录
        level: 日志级别 DEBUG/INFO/WARNING/ERROR
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # 先移除默认 handler，避免重复打印
    logger.remove()

    # 控制台：彩色、易读
    # 注意：PyInstaller 无黑框打包（console=False）时 sys.stderr 可能是 None，
    # 不能传给 loguru，否则报 TypeError: Cannot log to objects of type 'NoneType'
    console = sys.stderr or sys.stdout
    if console is not None:
        logger.add(
            console,
            level=level,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>",
        )

    # 文件：按天切割，保留 14 天（GUI/EXE 主要看这里或界面日志框）
    logger.add(
        Path(log_dir) / "auto_trading_{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
    )
