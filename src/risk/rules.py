"""RiskGuardian 的纯规则：相同状态和时刻必定得到相同结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskPolicy:
    max_daily_loss: float = 1000.0
    min_order_interval_sec: int = 60
    max_orders_per_day: int = 20


@dataclass(frozen=True)
class RiskRuleState:
    current_position: int = 0
    positions: dict[str, int] = field(default_factory=dict)
    daily_pnl: float = 0.0
    orders_today: int = 0
    open_orders_today: int = 0
    last_order_ts: float = 0.0
    last_open_ts: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    halt_kind: str = ""


def total_abs_position(state: RiskRuleState) -> int:
    if state.positions:
        return sum(abs(value) for value in state.positions.values())
    return abs(state.current_position)


def daily_loss_reason(policy: RiskPolicy, state: RiskRuleState) -> str:
    if state.daily_pnl > -abs(policy.max_daily_loss):
        return ""
    return f"当日亏损 {state.daily_pnl:.2f} 已超过限额 {policy.max_daily_loss}"


def evaluate_order(
    policy: RiskPolicy,
    state: RiskRuleState,
    side: str,
    volume: int,
    *,
    symbol: str | None = None,
    force: bool = False,
    now: float,
) -> tuple[bool, str]:
    """只评估，不改状态、不读时钟、不写日志。"""
    if volume <= 0:
        return False, "手数必须大于 0"
    if force and side == "close":
        return True, "强制平仓"
    if side == "close":
        if symbol and state.positions:
            lots = abs(int(state.positions.get(symbol, 0) or 0))
            if lots <= 0:
                key = next(
                    (name for name in state.positions if str(name).lower() == str(symbol).lower()),
                    None,
                )
                lots = abs(int(state.positions.get(key, 0) or 0)) if key is not None else 0
            if lots <= 0:
                return False, f"{symbol} 无持仓，无需平仓"
            return True, "通过"
        if state.current_position == 0 and total_abs_position(state) <= 0:
            return False, "当前无持仓，无需平仓"
        return True, "通过"
    if state.halted:
        return False, f"已熔断: {state.halt_reason}"

    need = int(policy.min_order_interval_sec or 0)
    last = max(float(state.last_order_ts or 0), float(state.last_open_ts or 0))
    if need > 0 and last and (now - last) < need:
        wait = max(1, int(need - (now - last)))
        return False, f"距上一笔下单不足 {need} 秒，请再等 {wait} 秒"
    if state.open_orders_today >= policy.max_orders_per_day:
        return False, f"今日开仓次数已达上限 {policy.max_orders_per_day}"
    return True, "通过"


def apply_fill(
    state: RiskRuleState,
    side: str,
    volume: int,
    *,
    symbol: str | None = None,
    now: float,
) -> RiskRuleState:
    """返回成交后的新状态，不修改传入对象。"""
    sym = symbol or "_default"
    current = state.current_position
    positions = dict(state.positions)
    open_orders = state.open_orders_today
    last_open = state.last_open_ts
    if side == "buy_open":
        current += volume
        positions[sym] = positions.get(sym, 0) + volume
        open_orders += 1
        last_open = now
    elif side == "sell_open":
        current -= volume
        positions[sym] = positions.get(sym, 0) - volume
        open_orders += 1
        last_open = now
    elif side == "close":
        if sym in positions and positions[sym] != 0:
            cur = positions[sym]
            if cur > 0:
                positions[sym] = max(0, cur - volume)
            else:
                positions[sym] = min(0, cur + volume)
        if current > 0:
            current = max(0, current - volume)
        elif current < 0:
            current = min(0, current + volume)
    return RiskRuleState(
        current_position=current,
        positions=positions,
        daily_pnl=state.daily_pnl,
        orders_today=state.orders_today + 1,
        open_orders_today=open_orders,
        last_order_ts=now,
        last_open_ts=last_open,
        halted=state.halted,
        halt_reason=state.halt_reason,
        halt_kind=state.halt_kind,
    )
