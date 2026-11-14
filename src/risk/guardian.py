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
    # 熔断原因分类：daily_loss / pending_intent / ledger / reconcile / intent_write_failed。
    # 日切只自动解除 daily_loss；其余必须人工核对后解除。
    halt_kind: str = ""
    # 同一段熔断只写一条告警：当日盈亏每变一次就重写一条旧限额告警，界面会反复删。
    halt_alerted: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # 实盘熔断才写运行台告警。回测/测试关掉，避免把模拟里的「限额 50」刷到界面上。
    write_alerts: bool = True

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
        self._check_daily_loss()

    def set_daily_pnl(self, value: float) -> None:
        """
        用账本口径直接覆盖当日盈亏。

        信号账户记完账后调用，避免策略和风控各算一次。覆盖后同样检查熔断，
        已经熔断的不会因为数值回升而自动恢复。
        """
        self.daily_pnl = round(float(value or 0), 2)
        logger.info("当日盈亏按账本校准: {:.2f}", self.daily_pnl)
        self._check_daily_loss()

    def _check_daily_loss(self) -> None:
        """当日亏损是否已触发熔断。同一段熔断只写一条告警。"""
        if self.daily_pnl > -abs(self.max_daily_loss):
            return
        reason = f"当日亏损 {self.daily_pnl:.2f} 已超过限额 {self.max_daily_loss}"
        if self.halted and self.halt_kind == "daily_loss":
            self.halt_reason = reason
            return
        self.set_halt("daily_loss", reason)

    def set_max_daily_loss(self, value: float) -> None:
        """
        运行中更新日亏限额（配置页保存后调用）。

        新限额下已不再超限就解除熔断；仍超限则保持熔断，但同一段熔断不会
        再补写告警，避免界面反复清理旧限额的熔断记录。
        """
        try:
            limit = float(value)
        except (TypeError, ValueError):
            return
        self.max_daily_loss = limit
        if self.daily_pnl > -abs(limit):
            if self.halted and self.halt_kind in ("", "daily_loss"):
                logger.info(
                    "日亏限额改为 {:.0f}，当日盈亏 {:.2f} 未再超限，解除熔断",
                    limit,
                    self.daily_pnl,
                )
            self.clear_halt("daily_loss")
            return
        self._check_daily_loss()

    def set_halt(self, kind: str, reason: str) -> None:
        """按原因分类熔断。日切只自动解除 daily_loss，其余等人工处理。"""
        if self.halted and self.halt_kind and self.halt_kind != kind:
            # 已经在崩溃/遗留委托类熔断里，不能被后来的日亏覆盖成可自动解除的原因。
            logger.error(
                "已有 {} 熔断，保留原原因不覆盖: {}（本次: {}）",
                self.halt_kind,
                self.halt_reason,
                reason,
            )
            return
        self.halted = True
        self.halt_kind = str(kind or "")
        self.halt_reason = str(reason or "")
        logger.error("风控熔断[{}]: {}", self.halt_kind, self.halt_reason)
        self._emit_halt()

    def clear_halt(self, kind: str | None = None) -> bool:
        """
        解除熔断。

        kind=None 表示人工确认，无条件解除；否则只解除同类熔断。
        返回是否真的解除了，避免把崩溃/意图类熔断顺手清掉。
        """
        if kind is not None and self.halted and self.halt_kind != kind:
            # halt_kind 为空表示旧文件里的「未知原因」熔断，同样不能自动解除。
            logger.warning(
                "熔断原因是 {}，不能按 {} 自动解除: {}",
                self.halt_kind or "未知",
                kind,
                self.halt_reason,
            )
            return False
        self.halted = False
        self.halt_kind = ""
        self.halt_reason = ""
        self.halt_alerted = False
        return True

    def _emit_halt(self) -> None:
        """熔断时记一条告警。同一段熔断只写一次；回测不写。"""
        if not self.write_alerts or self.halt_alerted:
            return
        self.halt_alerted = True
        try:
            from src.ops.alerts import emit_alert

            emit_alert("halt", self.halt_reason, level="error")
        except Exception as exc:  # noqa: BLE001
            logger.debug("熔断告警没写上: {}", exc)

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

        # 强制平仓始终放行（收盘清仓等场景使用 force=True）
        if force and side == "close":
            return True, "强制平仓"

        # 平仓：只看这一品种自己有没有仓。别的品种有仓，不能把空仓品种也放行。
        # 熔断不拦平仓。
        if side == "close":
            if symbol and self.positions:
                lots = abs(int(self.positions.get(symbol, 0) or 0))
                if lots <= 0:
                    key = next(
                        (name for name in self.positions if str(name).lower() == str(symbol).lower()),
                        None,
                    )
                    lots = abs(int(self.positions.get(key, 0) or 0)) if key is not None else 0
                if lots <= 0:
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
