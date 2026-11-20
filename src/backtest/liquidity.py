"""
回测里的涨跌停、部分成交与挂单超时近似。

功能：执行K线是一字板（最高=最低）时不开新仓，平仓顺延到下一根能成交的K线；
      开仓挂单最多等 entry_timeout_bars 根，等到超时就当撤单；
      还可以按这根K线的成交量给单笔开仓手数设上限，超出部分按部分成交、余量当根撤掉。
原理：回测只有K线，没有委托队列。用「一根K线完全没有价格波动」代表涨跌停锁死，
      用成交量乘一个参与率代表最多能吃到多少。
      三项默认开启，让回测净盈亏和成交笔数更贴近实盘；要复现旧成绩时在配置里改成 false/0。

配置写在 `backtest.liquidity` 下：
    block_limit_locked: true/false   一字板不成交（默认 true）
    max_volume_pct: 0.1              单根最多吃该根成交量的 10%（0=不限）
    entry_timeout_bars: 1            开仓挂单最多等几根可成交K线（默认 1=下一根仍锁板就撤单）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategy.types import config_flag


def _field(bar: Any, key: str) -> Any:
    """取一根K线的字段；重名列会返回 Series，取第一列就够用。"""
    value = bar[key]
    if hasattr(value, "iloc"):
        return value.iloc[0]
    return value


@dataclass(frozen=True)
class LiquidityModel:
    """涨跌停/部分成交/挂单超时口径。"""

    block_limit_locked: bool = True
    max_volume_pct: float = 0.1
    entry_timeout_bars: int = 1

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> LiquidityModel:
        bt = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
        box = bt.get("liquidity") if isinstance(bt.get("liquidity"), dict) else {}
        try:
            pct = max(0.0, float(box.get("max_volume_pct", 0.1) or 0))
        except (TypeError, ValueError):
            pct = 0.1
        try:
            wait = int(box.get("entry_timeout_bars", 1) or 1)
        except (TypeError, ValueError):
            wait = 1
        return cls(
            block_limit_locked=config_flag(box.get("block_limit_locked"), default=True),
            max_volume_pct=pct,
            entry_timeout_bars=max(1, wait),
        )

    def locked(self, bar: Any) -> bool:
        """这根K线的最高=最低，按涨跌停一字板处理，不能成交。"""
        if not self.block_limit_locked or bar is None:
            return False
        try:
            high = float(_field(bar, "high"))
            low = float(_field(bar, "low"))
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        return abs(high - low) <= 1e-9

    def wait_fill_index(
        self,
        bars: Any,
        start: int | None,
        *,
        allow_wait: bool = True,
    ) -> int | None:
        """
        从 start 这根开始找第一根能成交的K线。

        allow_wait=False（成交不走下一根开盘）时只看 start 这一根。
        锁板检查关掉时直接返回 start。找不到返回 None，调用方按「超时撤单」处理。
        """
        if start is None:
            return None
        try:
            first = int(start)
        except (TypeError, ValueError):
            return None
        try:
            total = len(bars)
        except TypeError:
            return None
        if first < 0 or first >= total:
            return None
        if not (allow_wait and self.block_limit_locked):
            return first
        last = min(first + self.entry_timeout_bars, total)
        for idx in range(first, last):
            if not self.locked(bars.iloc[idx]):
                return idx
        return None

    def cap_open_volume(self, volume: int, bar: Any) -> int:
        """按这根K线的成交量压一下开仓手数；0 表示不限。"""
        wanted = int(volume)
        if self.max_volume_pct <= 0 or wanted <= 0 or bar is None:
            return wanted
        try:
            traded = float(_field(bar, "volume"))
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
            "entry_timeout_bars": self.entry_timeout_bars,
        }

    def describe(self) -> str:
        """回测结果里说明这次用的成交约束。"""
        parts = []
        if self.block_limit_locked:
            parts.append(f"一字板不成交，挂单最多等 {self.entry_timeout_bars} 根")
        if self.max_volume_pct > 0:
            parts.append(f"单根最多吃成交量 {self.max_volume_pct:g}，余量撤单")
        if not parts:
            return "成交约束  未开（一字板可成交、不限手数）"
        return "成交约束  " + "；".join(parts)
