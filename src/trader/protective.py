"""柜台保护单适配器接口。CTP 原生条件单暂不可用，先用本地止损 + 看门狗兜底。"""

from __future__ import annotations

from typing import Any, Protocol


class ProtectiveOrderAdapter(Protocol):
    """柜台侧止损单。side 只取 "long" / "short"。"""

    def supported(self) -> bool: ...

    def place_stop(self, symbol: str, side: str, volume: int, stop_price: float) -> bool: ...

    def cancel_stop(self, symbol: str) -> bool: ...


class CtpNativeStopAdapter:
    """CTP 原生条件单。接口先留着，柜台没开通前一律当成不支持。"""

    def __init__(self, gateway: Any = None) -> None:
        self.gateway = gateway

    def supported(self) -> bool:
        return False

    def place_stop(self, symbol: str, side: str, volume: int, stop_price: float) -> bool:
        return False

    def cancel_stop(self, symbol: str) -> bool:
        return False


def create_protective_adapter(trader: Any) -> ProtectiveOrderAdapter:
    """按当前通道挑保护单实现。现在只有 CTP 原生一种占位。"""
    return CtpNativeStopAdapter(getattr(trader, "gateway", None))


def overnight_unprotected_notice(*, live: bool, dry_run: bool = False) -> str:
    """
    进④真实资金时提醒：仓上没有交易所止损单。

    不改开仓规则。华泰条件单还没接，断电/杀进程时券商不会按程序止损价平仓。
    模拟还开着也写这条，免得关掉模拟后忘了。
    """
    if not live:
        return ""
    dry_bit = "现在模拟还开着，策略不会发单；一旦关掉模拟，这条就生效。" if dry_run else ""
    return (
        "仓上没有交易所止损单。关掉本程序、电脑断电或断网时，华泰不会按你的止损价自动平仓。"
        "不要无人值守过夜。电脑还开着、只是程序被杀，可另开冷备守护（默认关）。"
        + dry_bit
    )
