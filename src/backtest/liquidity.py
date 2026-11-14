"""
回测里的涨跌停与流动性近似。

功能：执行K线是一字板（最高=最低）时不开新仓，平仓顺延到下一根能成交的K线；
      还可以按这根K线的成交量给单笔开仓手数设上限。
原理：回测只有K线，没有委托队列。用「一根K线完全没有价格波动」代表涨跌停锁死，
      用成交量乘一个参与率代表最多能吃到多少。
      两项默认关闭，只有配置里显式打开才会改变回测结果。

配置写在 `backtest.liquidity` 下：
    block_limit_locked: true/false   一字板不成交（默认 false）
    max_volume_pct: 0.1              单根最多吃该根成交量的 10%（0=不限）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategy.types import config_flag


@dataclass(frozen=True)
class LiquidityModel:
    """涨跌停/流动性口径。"""

    block_limit_locked: bool = False
    max_volume_pct: float = 0.0

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> LiquidityModel:
        bt = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
        box = bt.get("liquidity") if isinstance(bt.get("liquidity"), dict) else {}
        try:
            pct = max(0.0, float(box.get("max_volume_pct", 0) or 0))
        except (TypeError, ValueError):
            pct = 0.0
        return cls(
            block_limit_locked=config_flag(box.get("block_limit_locked"), default=False),
            max_volume_pct=pct,
        )

    def locked(self, bar: Any) -> bool:
        """这根K线的最高=最低，按涨跌停一字板处理，不能成交。"""
        if not self.block_limit_locked or bar is None:
            return False
        try:
            high = float(bar["high"])
            low = float(bar["low"])
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        return abs(high - low) <= 1e-9

    def cap_open_volume(self, volume: int, bar: Any) -> int:
        """按这根K线的成交量压一下开仓手数；0 表示不限。"""
        wanted = int(volume)
        if self.max_volume_pct <= 0 or wanted <= 0 or bar is None:
            return wanted
        try:
            traded = float(bar["volume"])
        except (KeyError, IndexError, TypeError, ValueError):
            return wanted
        if traded <= 0:
            return 0
        allowed = int(traded * self.max_volume_pct)
        return max(0, min(wanted, allowed))

    def snapshot(self) -> dict[str, Any]:
        """写进回测结果，方便事后对照。"""
        return {
            "block_limit_locked": self.block_limit_locked,
            "max_volume_pct": self.max_volume_pct,
        }
