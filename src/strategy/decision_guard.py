"""
决策前的公共拦截：通用止损与交割月闸门。

功能：策略自己说「继续持有」时，让公共止损补一刀；到了交割月，有仓必须平、开仓要拒。
原理：实盘扫描（scheduler/runner.py）和回测（backtest/engine.py）以前各写一份同样的判断，
      规则一改就得改两处，容易漂移。这里收成一处，两边只负责成交、记账和各自的日志/事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.risk.delivery import ContractGate, contract_gate
from src.strategy.generic_stop import GenericStopHit, GenericStopOverlay
from src.strategy.types import StrategyAction, StrategyDecision


def apply_generic_stop(
    overlay: GenericStopOverlay,
    symbol: str,
    decision: StrategyDecision,
    position: Any | None,
    bars: pd.DataFrame | None,
    *,
    flat: bool | None = None,
    forming: pd.DataFrame | None = None,
    mode: str = "touch",
    cooldown_sec: float = 0.0,
    meta: dict[str, Any] | None = None,
) -> tuple[StrategyDecision, GenericStopHit | None]:
    """
    策略没让平仓、但还持有仓位时，用公共止损再判一次。

    返回 (决策, 触发的通用止损)。没触发或本来就不该判，第二项为 None。
    flat 不传时只看 side；实盘还要按手数判空仓，可显式传入 `_symbol_is_flat` 的结果。
    触发时返回的是新的 CLOSE 决策，调用方原来的决策不会被改动。
    """
    if decision.action != StrategyAction.HOLD:
        return decision, None
    if flat is None:
        flat = position is None or str(getattr(position, "side", "flat") or "flat") == "flat"
    if flat:
        return decision, None
    hit = overlay.check(
        symbol,
        position,
        bars,
        forming=forming,
        mode=mode,
        cooldown_sec=cooldown_sec,
    )
    if hit is None:
        return decision, None
    return (
        StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            hit.reason,
            stop_price=hit.stop,
            meta=dict(meta or {}),
        ),
        hit,
    )


@dataclass
class DeliveryGateOutcome:
    """交割月闸门处理后的决策，以及调用方要补记的事件信息。"""

    decision: StrategyDecision
    gate: ContractGate
    # 被闸门挡回去的开仓决策；没有拦开仓时为 None
    rejected: StrategyDecision | None = None
    # 这一根是否因交割月强制平掉持仓
    flattened: bool = False


def apply_delivery_gate(
    symbol: str,
    now: datetime | date | None,
    decision: StrategyDecision,
    *,
    flat: bool,
    flatten_meta: dict[str, Any] | None = None,
) -> DeliveryGateOutcome:
    """
    交割月不再交易这一手：有仓平掉，没仓也不许开。

    flat 由调用方按自己的空仓口径给出（实盘会把手数为 0 也算空仓）。
    被拦下的开仓决策放在 rejected 里，调用方可以照原来的格式记拒单。
    """
    gate = contract_gate(symbol, now)
    if gate.flatten and not flat:
        return DeliveryGateOutcome(
            StrategyDecision(
                StrategyAction.CLOSE,
                symbol,
                gate.reason,
                meta=dict(flatten_meta or {}),
            ),
            gate,
            flattened=True,
        )
    if gate.block_open and decision.action in (
        StrategyAction.BUY_OPEN,
        StrategyAction.SELL_OPEN,
    ):
        return DeliveryGateOutcome(
            StrategyDecision(StrategyAction.HOLD, symbol, gate.reason),
            gate,
            rejected=decision,
        )
    return DeliveryGateOutcome(decision, gate)
