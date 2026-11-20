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
from typing import Any, Callable

from loguru import logger

from src.risk.rules import (
    RiskPolicy,
    RiskRuleState,
    apply_fill,
    daily_loss_reason,
    evaluate_order,
)


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
    # 纯规则由调用方注入时钟；默认行为仍是墙上时钟。
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "RiskGuardian":
        risk = cfg.get("risk", {})
        return cls(
            max_daily_loss=float(risk.get("max_daily_loss", 1000)),
            min_order_interval_sec=int(risk.get("min_order_interval_sec", 60)),
            max_orders_per_day=int(risk.get("max_orders_per_day", 20)),
        )

    def _policy(self) -> RiskPolicy:
        return RiskPolicy(
            max_daily_loss=self.max_daily_loss,
            min_order_interval_sec=self.min_order_interval_sec,
            max_orders_per_day=self.max_orders_per_day,
        )

    def _rule_state(self) -> RiskRuleState:
        return RiskRuleState(
            current_position=self.current_position,
            positions=dict(self.positions),
            daily_pnl=self.daily_pnl,
            orders_today=self.orders_today,
            open_orders_today=self.open_orders_today,
            last_order_ts=self.last_order_ts,
            last_open_ts=self.last_open_ts,
            halted=self.halted,
            halt_reason=self.halt_reason,
            halt_kind=self.halt_kind,
        )

    def _apply_rule_state(self, state: RiskRuleState) -> None:
        self.current_position = state.current_position
        self.positions = dict(state.positions)
        self.daily_pnl = state.daily_pnl
        self.orders_today = state.orders_today
        self.open_orders_today = state.open_orders_today
        self.last_order_ts = state.last_order_ts
        self.last_open_ts = state.last_open_ts
        self.halted = state.halted
        self.halt_reason = state.halt_reason
        self.halt_kind = state.halt_kind

    def total_abs_position(self) -> int:
        if self.positions:
            return sum(abs(v) for v in self.positions.values())
        return abs(self.current_position)

    def update_pnl(self, pnl_delta: float) -> None:
        """更新当日盈亏；亏损过大时自动熔断（只拦开仓，平仓仍放行）。"""
        self.daily_pnl += pnl_delta
        logger.info("当日盈亏更新: {:+.2f} → 合计 {:.2f}", pnl_delta, self.daily_pnl)
        self._check_daily_loss()

    def set_daily_pnl(self, value: float, *, log: bool = True) -> None:
        """
        用账本口径直接覆盖当日盈亏。

        信号账户记完账后调用，避免策略和风控各算一次。覆盖后同样检查熔断，
        已经熔断的不会因为数值回升而自动恢复。
        常驻盯市每轮都会对一次；那种小跳动不要打日志（log=False）。
        """
        self.daily_pnl = round(float(value or 0), 2)
        if log:
            logger.info("当日盈亏按账本校准: {:.2f}", self.daily_pnl)
        self._check_daily_loss()

    def _check_daily_loss(self) -> None:
        """当日亏损是否已触发熔断。同一段熔断只写一条告警。"""
        reason = daily_loss_reason(self._policy(), self._rule_state())
        if not reason:
            return
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
        now: float | None = None,
    ) -> tuple[bool, str]:
        # 熔断不拦平仓；其它判断全部在纯函数里，测试可显式给 now。
        return evaluate_order(
            self._policy(),
            self._rule_state(),
            side,
            volume,
            symbol=symbol,
            force=force,
            now=self.clock() if now is None else float(now),
        )

    def on_order_filled(
        self,
        side: str,
        volume: int,
        symbol: str | None = None,
        *,
        now: float | None = None,
    ) -> None:
        """下单成功后用纯状态迁移更新内部状态。"""
        state = apply_fill(
            self._rule_state(),
            side,
            volume,
            symbol=symbol,
            now=self.clock() if now is None else float(now),
        )
        self._apply_rule_state(state)
        logger.info(
            "风控状态更新: 净仓={} 分品种={} 今日开仓={} 今日盈亏={:.2f}",
            self.current_position,
            self.positions,
            self.open_orders_today,
            self.daily_pnl,
        )
