"""
日志模块
功能：把运行过程写入控制台和 logs 目录文件，方便事后排查「为什么下了单/为什么没下单」。
原理：使用 loguru，一条配置同时输出到屏幕、按日期滚动的文本日志和结构化 JSONL。
     每条日志都带上当前 trace_id，和 signals/audit_log 里的关联号一致。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger

from src.kernel.trace import get_trace_id


def _inject_trace(record: Any) -> None:
    """给每条日志补 trace_id；没有上下文时写 -，避免格式缺字段。"""
    extra = record.setdefault("extra", {})
    extra.setdefault("trace_id", get_trace_id() or "-")


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
    logger.configure(patcher=_inject_trace)

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
            "<cyan>{extra[trace_id]}</cyan> | "
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

    # 结构化日志：一行一个 JSON，供审计/指标聚合，不替代上面的文本日志。
    logger.add(
        Path(log_dir) / "structured_{time:YYYY-MM-DD}.jsonl",
        level=level,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        serialize=True,
    )
