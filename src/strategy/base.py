"""所有策略共用的 K 线处理状态与公开读写接口。"""

from __future__ import annotations

from typing import Any


class StrategyBase:
    """保存信号去重和最近开仓 K 线；外部运行层不得直接改私有字典。"""

    def __init__(self) -> None:
        self._signaled_bar: dict[str, str] = {}
        self._last_entry_bar: dict[str, str] = {}

    def get_signaled_bar(self, symbol: str) -> str:
        """读这个品种已处理的信号 K 线；没有时返回空字符串。"""
        return str(self._signaled_bar.get(symbol) or "")

    def set_signaled_bar(self, symbol: str, bar_id: Any) -> None:
        """把这个品种的信号 K 线标成已处理。"""
        self._signaled_bar[str(symbol)] = str(bar_id or "")

    def clear_signaled_bar(self, symbol: str, expected_bar_id: Any = None) -> bool:
        """清除信号标记；指定 expected_bar_id 时只清同一根，避免误删新信号。"""
        if expected_bar_id is not None and self.get_signaled_bar(symbol) != str(expected_bar_id or ""):
            return False
        return self._signaled_bar.pop(symbol, None) is not None

    def get_last_entry_bar(self, symbol: str) -> str:
        """读这个品种最近一次开仓的信号 K 线。"""
        return str(self._last_entry_bar.get(symbol) or "")

    def set_last_entry_bar(self, symbol: str, bar_id: Any) -> None:
        """记录这个品种最近一次开仓的信号 K 线。"""
        self._last_entry_bar[str(symbol)] = str(bar_id or "")

    def get_last_entry_bars(self) -> dict[str, str]:
        """返回持久化用副本，调用方改副本不会影响策略状态。"""
        return dict(self._last_entry_bar)

    def restore_last_entry_bars(self, bars: dict[str, Any] | None) -> None:
        """从磁盘恢复最近开仓记录，保留当前进程里已有的其它品种。"""
        if isinstance(bars, dict):
            self._last_entry_bar.update({str(k): str(v) for k, v in bars.items()})
