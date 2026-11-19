"""所有策略共用的 K 线处理状态与公开读写接口。"""

from __future__ import annotations

from typing import Any


def same_signal_bar(left: Any, right: Any) -> bool:
    """两根信号K线是不是同一根。各策略写法可能略有差别，比较时只认字符串。"""
    a = str(left or "").strip()
    b = str(right or "").strip()
    return bool(a and b and a == b)


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

    def note_flat_wait_next_bar(self, symbol: str, bar_id: Any = "") -> None:
        """
        仓已平（含手机/交易台人工平）。本根信号K线不再开仓。

        下一根已收盘K线再按策略评估：还符合才开，不符合就等。
        不要因为柜台和账本对不上而立刻把仓补回去。
        """
        key = str(symbol or "")
        bid = str(bar_id or "")
        if not key:
            return
        if bid:
            self._signaled_bar[key] = bid
            self._last_entry_bar[key] = bid
            used = getattr(self, "_used_bar", None)
            if isinstance(used, dict):
                used[key] = bid
