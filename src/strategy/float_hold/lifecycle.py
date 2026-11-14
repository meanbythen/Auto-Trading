"""
日内浮盈飘单 · 仓单生命周期。

功能：管什么时候允许开新仓、这段能开几次、什么时候必须清仓。
原理：钟点仍读策略配置和配置页的收盘清仓，不在这里另写一套时间表。
     方向对不对、止损放哪，都不归这一块。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class SessionLifecycle:
    """
    开仓窗口 + 截止钟点 + 收盘清仓 + 一段几次。

    host 是浮盈飘单策略。观察几分钟、日盘/夜盘截止，都已经写在配置页这一套里。
    """

    key = "session_window"
    label = "开仓窗口与收盘清仓"

    def flatten_now(self, host: Any, now: datetime) -> bool:
        """到了品种表里的清仓钟点，并且配置页开了收盘清仓。"""
        return bool(host.is_flatten_time(now))

    def describe(self, host: Any) -> str:
        """给人看：这套单从什么时候能开，开到几点，拿到哪里。"""
        day = getattr(host, "day_cutoff", None)
        night = getattr(host, "night_cutoff", None)
        eth = getattr(host, "eth_minutes", 60)
        day_text = day.strftime("%H:%M") if day is not None else "10:30"
        night_text = night.strftime("%H:%M") if night is not None else "22:15"
        flat = "听配置页收盘清仓" if getattr(host, "flatten_at_close", True) else "到点也不自动清仓"
        return (
            f"从这段开盘起观察 {eth} 分钟才允许开仓；"
            f"日盘 {day_text}、夜盘 {night_text} 之后不再开新仓；"
            f"日盘同一口箱子最多两枪；{flat}。"
        )
