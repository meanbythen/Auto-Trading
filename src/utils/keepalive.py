"""
防休眠（挂机必备）
功能：阻止 Windows 在挂机时自动睡眠/息屏，否则 pyautogui 点击与扫描都会停。
原理：调用 Win32 SetThreadExecutionState，声明「系统正在连续运行 + 需要显示」。
      这是微软官方推荐做法（同类还有 caffeine、PowerToys Awake），比模拟鼠标移动更干净。
注意：只在机器人「常驻运行」期间开启；停止后自动恢复系统电源策略。
"""

from __future__ import annotations

import sys
from typing import Any

from loguru import logger

# Win32 常量
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001  # 防止系统睡眠
ES_DISPLAY_REQUIRED = 0x00000002  # 防止显示器关闭（GUI 点击需要屏幕/会话可用）


class KeepAwake:
    """
    上下文/启停式防休眠。

    用法:
        ka = KeepAwake()
        ka.start()   # 挂机开始
        ...
        ka.stop()    # 挂机结束，恢复默认
    """

    def __init__(self, *, keep_display: bool = True) -> None:
        self.keep_display = keep_display
        self._active = False
        self._kernel32: Any = None

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        """开启防休眠。非 Windows 或失败时返回 False（不抛异常，避免卡死主流程）。"""
        if self._active:
            return True
        if sys.platform != "win32":
            logger.warning("当前不是 Windows，跳过 SetThreadExecutionState 防休眠")
            return False
        try:
            import ctypes

            self._kernel32 = ctypes.windll.kernel32
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            if self.keep_display:
                flags |= ES_DISPLAY_REQUIRED
            # 返回值：成功为先前状态；失败为 0
            prev = self._kernel32.SetThreadExecutionState(flags)
            if not prev:
                logger.error("SetThreadExecutionState 失败，电脑仍可能休眠")
                return False
            self._active = True
            logger.info(
                "已开启防休眠（系统{}休眠{}）",
                "不",
                "、屏幕保持唤醒" if self.keep_display else "",
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception("开启防休眠失败: {}", e)
            return False

    def stop(self) -> None:
        """关闭防休眠，交还给系统电源计划。"""
        if not self._active:
            return
        try:
            if self._kernel32 is not None:
                # 只传 CONTINUOUS = 清除本线程之前加的 SYSTEM/DISPLAY 要求
                self._kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            logger.info("已关闭防休眠，恢复系统默认电源策略")
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭防休眠时异常（可忽略）: {}", e)
        finally:
            self._active = False
            self._kernel32 = None

    def __enter__(self) -> "KeepAwake":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
