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
