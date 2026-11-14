"""
进程级单实例锁。

功能：同一时间只允许一个进程操作同花顺/交易账户，避免两套程序重复下单。
原理：Windows 用命名互斥量；其它平台用临时目录占位文件。同一进程重复获取直接成功。
"""

from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

from loguru import logger

_DEFAULT_NAME = "AutoTrading_SingleInstance"
_ERROR_ALREADY_EXISTS = 183

_handle: object | None = None


def _acquire_windows(name: str) -> object | None:
    """拿一个命名互斥量；已有同名实例时返回 None。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def _acquire_posix(name: str) -> object | None:
    """用一个临时占位文件表示已运行。"""
    path = Path(tempfile.gettempdir()) / f"{name}.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        return None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return path


def acquire(name: str = _DEFAULT_NAME) -> bool:
    """获取单实例锁。已持有或获取成功返回 True，已有其它实例返回 False。"""
    global _handle
    if _handle is not None:
        return True
    if sys.platform == "win32":
        handle = _acquire_windows(name)
    else:
        handle = _acquire_posix(name)
    if handle is None:
        return False
    _handle = handle
    atexit.register(release)
    logger.info("已获取单实例锁: {}", name)
    return True


def release() -> None:
    """释放单实例锁（进程退出时也会自动调用）。"""
    global _handle
    handle = _handle
    _handle = None
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)
        else:
            Path(handle).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("释放单实例锁失败: {}", exc)
