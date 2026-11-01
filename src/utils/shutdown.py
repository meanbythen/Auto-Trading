"""
收盘后关机（可选）
功能：当日最后一节交易/清仓结束后，预约 Windows 关机，方便挂机省电。
原理：调用系统命令 shutdown /s /t 秒数；可用 shutdown /a 取消。
注意：默认关闭；有夜盘时不会在下午 14:55 清仓后关机，会等到夜盘最后清仓之后。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
from typing import Optional


def _parse_hhmm(value: str) -> dtime:
    hour, minute = str(value).split(":")
    return dtime(int(hour), int(minute))


def last_close_time(
    sessions: list[list[str]] | None,
    flatten_times: list[str] | None,
) -> Optional[dtime]:
    """
    算出「今天可以关机」的时刻：取「最晚清仓」与「最晚交易时段结束」两者更晚的那个。

    白话：日盘+夜盘都做时，大约在夜盘收完（如 22:55/23:00）之后才关机；
         只有日盘时，大约在 14:55/15:00 之后关机。
    """
    ends: list[dtime] = []
    for x in flatten_times or []:
        try:
            ends.append(_parse_hhmm(x))
        except Exception:  # noqa: BLE001
            continue
    for pair in sessions or []:
        if len(pair) < 2:
            continue
        try:
            ends.append(_parse_hhmm(pair[1]))
        except Exception:  # noqa: BLE001
            continue
    if not ends:
        return None
    return max(ends)


def ready_for_shutdown(
    *,
    sessions: list[list[str]] | None,
    flatten_times: list[str] | None,
    now: datetime | None = None,
    grace_minutes: int = 5,
) -> tuple[bool, str]:
    """
    是否已到收盘后可关机窗口。

    参数:
        grace_minutes: 过了最后收盘时刻后再等几分钟，给最后一轮扫描/清仓留时间
    """
    now = now or datetime.now()
    close_t = last_close_time(sessions, flatten_times)
    if close_t is None:
        return False, "未配置清仓/交易时段，不自动关机"
    close_dt = datetime.combine(now.date(), close_t) + timedelta(minutes=max(grace_minutes, 0))
    if now < close_dt:
        return False, f"未到收盘后关机点（约 {close_dt.strftime('%H:%M')}）"
    return True, f"已过当日最后收盘（{close_t.strftime('%H:%M')}+{grace_minutes}分钟）"


def schedule_windows_shutdown(delay_sec: int = 180) -> tuple[bool, str]:
    """
    预约关机。

    返回: (成功?, 说明文字)
    """
    delay_sec = int(max(delay_sec, 30))  # 至少 30 秒，方便看到日志后取消
    if sys.platform != "win32":
        return False, "当前不是 Windows，跳过关机"
    try:
        # /s 关机；/t 倒计时秒；/c 注释给系统关机对话框看
        subprocess.run(
            [
                "shutdown",
                "/s",
                "/t",
                str(delay_sec),
                "/c",
                f"Auto-Trading 收盘后自动关机（{delay_sec}秒内可在命令行执行 shutdown /a 取消）",
            ],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True, f"已预约 {delay_sec} 秒后关机（取消请执行: shutdown /a）"
    except Exception as e:  # noqa: BLE001
        return False, f"预约关机失败: {e}"


def cancel_windows_shutdown() -> tuple[bool, str]:
    """取消已预约的关机。"""
    if sys.platform != "win32":
        return False, "当前不是 Windows"
    try:
        subprocess.run(
            ["shutdown", "/a"],
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True, "已尝试取消关机预约（shutdown /a）"
    except Exception as e:  # noqa: BLE001
        return False, f"取消关机失败: {e}"
