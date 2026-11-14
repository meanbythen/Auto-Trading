"""
持仓状态（领域对象）。

功能：策略、运行状态和账本共用同一种持仓结构，序列化只走这一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


@dataclass
class PositionState:
    """策略内部记住的持仓状态。"""

    side: Literal["flat", "long", "short"] = "flat"
    volume: int = 0
    entry_price: float = 0.0
    initial_stop: float = 0.0
    trail_stop: float = 0.0
    # 是否已用大周期 SAR「确认方向」并开始移动止损（确认前只守影线端初始止损）
    trail_armed: bool = False
    # 长线箱体开仓时冻住的箱沿。其它策略保持 0。
    box_high: float = 0.0
    box_low: float = 0.0
    box_height: float = 0.0
    symbol: str = ""
    opened_at: str = ""

    def reduce_close(self, volume: int) -> bool:
        """
        按实际成交手数减仓。

        返回 True 表示这一笔之后已经没有仓了。
        平今、平昨拆成两笔时，第一笔只减掉自己的手数，止损和开仓价都留着。
        手数大于等于现有仓位时，调用方再把仓位清成空仓。
        """
        lots = max(0, int(volume or 0))
        held = int(self.volume or 0)
        if self.side == "flat" or held <= 0 or lots >= held:
            return True
        self.volume = held - lots
        return False

    def to_dict(self) -> dict[str, Any]:
        """导出可 JSON 的持仓字典（不含策略私有的 extreme）。"""
        return {
            "side": self.side,
            "volume": int(self.volume or 0),
            "entry_price": float(self.entry_price or 0),
            "initial_stop": float(self.initial_stop or 0),
            "trail_stop": float(self.trail_stop or 0),
            "trail_armed": bool(self.trail_armed),
            "opened_at": str(self.opened_at or ""),
            "box_high": float(self.box_high or 0),
            "box_low": float(self.box_low or 0),
            "box_height": float(self.box_height or 0),
        }

    @classmethod
    def from_dict(cls, symbol: str, raw: Mapping[str, Any]) -> PositionState:
        """从磁盘字典恢复持仓。认不出的方向按空仓，多余键忽略。"""
        side = raw.get("side", "flat")
        if side not in ("flat", "long", "short"):
            side = "flat"
        return cls(
            side=side,
            volume=int(raw.get("volume", 0) or 0),
            entry_price=float(raw.get("entry_price", 0) or 0),
            initial_stop=float(raw.get("initial_stop", 0) or 0),
            trail_stop=float(raw.get("trail_stop", 0) or 0),
            trail_armed=bool(raw.get("trail_armed", False)),
            symbol=str(symbol),
            opened_at=str(raw.get("opened_at", "") or ""),
            box_high=float(raw.get("box_high", 0) or 0),
            box_low=float(raw.get("box_low", 0) or 0),
            box_height=float(raw.get("box_height", 0) or 0),
        )
