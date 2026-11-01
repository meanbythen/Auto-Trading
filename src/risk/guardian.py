"""
风控模块（交易机器人的「刹车」）
功能：在真正下单前检查：手数、日亏损、下单频率等。
原理：所有交易请求先过 RiskGuardian.allow()，不通过则拒绝并写日志。

仓位大小由「仓位上限%」在 PositionSizer 里算；这里只拦日亏损熔断和开仓频率。
已不再使用「最大持仓手数」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class RiskGuardian:
    """风控守卫：记住今日状态，决定能不能继续下单。"""

    max_daily_loss: float = 1000.0
    min_order_interval_sec: int = 60
    max_orders_per_day: int = 20

    # 运行时状态
    current_position: int = 0  # 净持仓（多正空负）；兼容旧逻辑
    positions: dict[str, int] = field(default_factory=dict)  # 分品种净仓
    daily_pnl: float = 0.0
    orders_today: int = 0
    open_orders_today: int = 0  # 仅统计开仓次数，便于限额
    last_order_ts: float = 0.0
    last_open_ts: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "RiskGuardian":
        risk = cfg.get("risk", {})
        return cls(
            max_daily_loss=float(risk.get("max_daily_loss", 1000)),
            min_order_interval_sec=int(risk.get("min_order_interval_sec", 60)),
            max_orders_per_day=int(risk.get("max_orders_per_day", 20)),
        )

    def total_abs_position(self) -> int:
        if self.positions:
            return sum(abs(v) for v in self.positions.values())
        return abs(self.current_position)

    def update_pnl(self, pnl_delta: float) -> None:
        """更新当日盈亏；亏损过大时自动熔断（只拦开仓，平仓仍放行）。"""
        self.daily_pnl += pnl_delta
        logger.info("当日盈亏更新: {:+.2f} → 合计 {:.2f}", pnl_delta, self.daily_pnl)
        if self.daily_pnl <= -abs(self.max_daily_loss):
            self.halted = True
            self.halt_reason = f"当日亏损 {self.daily_pnl:.2f} 已超过限额 {self.max_daily_loss}"
            logger.error("风控熔断: {}", self.halt_reason)

    def set_daily_pnl(self, value: float) -> None:
        """
        用账本口径直接覆盖当日盈亏。

        信号账户记完账后调用，避免策略和风控各算一次。覆盖后同样检查熔断，
        已经熔断的不会因为数值回升而自动恢复。
        """
        self.daily_pnl = round(float(value or 0), 2)
        logger.info("当日盈亏按账本校准: {:.2f}", self.daily_pnl)
        if self.daily_pnl <= -abs(self.max_daily_loss):
            self.halted = True
            self.halt_reason = f"当日亏损 {self.daily_pnl:.2f} 已超过限额 {self.max_daily_loss}"
            logger.error("风控熔断: {}", self.halt_reason)

    def allow(
        self,
        side: str,
        volume: int,
        *,
        symbol: str | None = None,
        force: bool = False,
    ) -> tuple[bool, str]:
        if volume <= 0:
            return False, "手数必须大于 0"

        # 强制平仓（紧急清仓）始终放行
        if force and side == "close":
            return True, "强制平仓"

        # 平仓：只看这一品种自己有没有仓。别的品种有仓，不能把空仓品种也放行。
        # 熔断不拦平仓。
        if side == "close":
            if symbol and self.positions:
                if abs(int(self.positions.get(symbol, 0) or 0)) <= 0:
                    return False, f"{symbol} 无持仓，无需平仓"
                return True, "通过"
            if self.current_position == 0 and self.total_abs_position() <= 0:
                return False, "当前无持仓，无需平仓"
            return True, "通过"

        if self.halted:
            return False, f"已熔断: {self.halt_reason}"

        now = time.time()
        if self.last_open_ts and (now - self.last_open_ts) < self.min_order_interval_sec:
            wait = int(self.min_order_interval_sec - (now - self.last_open_ts))
            return False, f"开仓过于频繁，请再等 {wait} 秒"

        if self.open_orders_today >= self.max_orders_per_day:
            return False, f"今日开仓次数已达上限 {self.max_orders_per_day}"

        return True, "通过"

    def on_order_filled(self, side: str, volume: int, symbol: str | None = None) -> None:
        """下单成功后更新内部状态。"""
        self.last_order_ts = time.time()
        self.orders_today += 1
        sym = symbol or "_default"

        if side == "buy_open":
            self.current_position += volume
            self.positions[sym] = self.positions.get(sym, 0) + volume
            self.open_orders_today += 1
            self.last_open_ts = self.last_order_ts
        elif side == "sell_open":
            self.current_position -= volume
            self.positions[sym] = self.positions.get(sym, 0) - volume
            self.open_orders_today += 1
            self.last_open_ts = self.last_order_ts
        elif side == "close":
            # 向 0 靠拢（优先按品种）
            if sym in self.positions and self.positions[sym] != 0:
                cur = self.positions[sym]
                if cur > 0:
                    self.positions[sym] = max(0, cur - volume)
                else:
                    self.positions[sym] = min(0, cur + volume)
            if self.current_position > 0:
                self.current_position = max(0, self.current_position - volume)
            elif self.current_position < 0:
                self.current_position = min(0, self.current_position + volume)

        logger.info(
            "风控状态更新: 净仓={} 分品种={} 今日开仓={} 今日盈亏={:.2f}",
            self.current_position,
            self.positions,
            self.open_orders_today,
            self.daily_pnl,
        )
