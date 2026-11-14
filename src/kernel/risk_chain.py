"""
发单前的风控链。

功能：只回答「要不要做」，不真正下单。执行器只负责「怎么做」。
原理：责任链按固定顺序逐条检查，任一不通过就停。规则顺序和常驻原来的判断一致，
避免重构后开仓机会对不上。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class TradeIntent:
    """准备发单时的只读意图。不把整个机器人对象传来传去。"""

    symbol: str
    side: str
    volume: int
    bar_id: str = ""
    orders_paused: bool = False
    ledger_failed: bool = False
    already_sent_this_bar: bool = False
    executor: str = "sim"
    stage_block: str = ""
    has_working_order: bool = False
    blocks_new_opens: bool = False
    books_note: str = ""
    detail: str = ""
    force: bool = False


@dataclass(frozen=True)
class GateResult:
    """风控链的结论。ok=True 才能交给执行器。"""

    ok: bool
    code: str
    reason: str = ""


def _is_open(side: str) -> bool:
    return str(side or "") in {"buy_open", "sell_open"}


def _paused(intent: TradeIntent, _risk: Any) -> GateResult | None:
    # 暂停只拦开新仓。止损、反手平仓、收盘清仓仍要能发。
    if intent.orders_paused and _is_open(intent.side):
        return GateResult(False, "order_paused", "已暂停报单，本笔没有发出")
    return None


def _ledger(intent: TradeIntent, _risk: Any) -> GateResult | None:
    if intent.ledger_failed and _is_open(intent.side):
        return GateResult(
            False,
            "ledger_error_block",
            "信号账户上一次写入失败，暂不允许开新仓；平仓不受影响。请检查磁盘空间和 data 目录权限。",
        )
    return None


def _dedup(intent: TradeIntent, _risk: Any) -> GateResult | None:
    if _is_open(intent.side) and intent.bar_id and intent.already_sent_this_bar:
        return GateResult(False, "order_dedup", "这根K线已经发过开仓单，跳过重复发单")
    return None


def _volume(intent: TradeIntent, _risk: Any) -> GateResult | None:
    if int(intent.volume or 0) <= 0:
        if _is_open(intent.side):
            return GateResult(False, "size_skip", intent.detail or "仓位为0，跳过开仓")
        return GateResult(
            False,
            "close_skip_no_position",
            intent.detail or "策略要求平仓，但程序账上无持仓，跳过",
        )
    return None


def _stage(intent: TradeIntent, _risk: Any) -> GateResult | None:
    # 上线阶段只拦新开仓。已经持有的仓必须能止损、反手和收盘清仓，
    # 否则阶段没批下来时，旧仓在夜里没人管。
    if intent.executor == "ctp" and intent.stage_block and _is_open(intent.side):
        return GateResult(False, "order_blocked_stage", intent.stage_block)
    return None


def _working(intent: TradeIntent, _risk: Any) -> GateResult | None:
    if intent.executor == "ctp" and intent.has_working_order:
        return GateResult(False, "order_working", "上一笔还没有最终回报，先不报新单")
    return None


def _books(intent: TradeIntent, _risk: Any) -> GateResult | None:
    if intent.executor == "ctp" and _is_open(intent.side) and intent.blocks_new_opens:
        return GateResult(
            False,
            "risk_block",
            intent.books_note or "柜台对账未通过",
        )
    return None


def _guardian(intent: TradeIntent, risk: Any) -> GateResult | None:
    allow = getattr(risk, "allow", None)
    if allow is None:
        return None
    kwargs: dict[str, Any] = {"symbol": intent.symbol}
    try:
        params = inspect.signature(allow).parameters
        if "force" in params:
            kwargs["force"] = bool(intent.force)
    except (TypeError, ValueError):
        pass
    ok, reason = allow(intent.side, int(intent.volume), **kwargs)
    if not ok:
        return GateResult(False, "risk_block", str(reason or "风控拒绝"))
    return None


# 发单前两段：先拦暂停/记账/重复信号，执行器可能还要先平反向仓，再拦手数和柜台。
# 顺序必须和常驻原来的判断一致，不能对调。
EARLY_RULES: tuple[Callable[[TradeIntent, Any], GateResult | None], ...] = (
    _paused,
    _ledger,
    _dedup,
)
SEND_RULES: tuple[Callable[[TradeIntent, Any], GateResult | None], ...] = (
    _volume,
    _stage,
    _working,
    _books,
    _guardian,
)


def _run(intent: TradeIntent, risk: Any, rules: tuple[Callable[..., GateResult | None], ...]) -> GateResult:
    for rule in rules:
        hit = rule(intent, risk)
        if hit is not None and not hit.ok:
            return hit
    return GateResult(True, "pass", "通过")


def run_early_gates(intent: TradeIntent, risk: Any) -> GateResult:
    """暂停开新仓、记账失败、同一根 K 线重复开仓。平仓不看暂停。还没算手数、也还没反手。"""
    return _run(intent, risk, EARLY_RULES)


def run_send_gates(intent: TradeIntent, risk: Any) -> GateResult:
    """手数、上线阶段、未完成委托、持仓对账、日亏损与频率。通过后才允许真正发单。"""
    return _run(intent, risk, SEND_RULES)


def run_pretrade_gates(intent: TradeIntent, risk: Any) -> GateResult:
    """
    整条链（测试和没有反手步骤时用）。

    执行器看到 ok 之后才组报单，不再自己判断「今天还能不能开」。
    """
    early = run_early_gates(intent, risk)
    if not early.ok:
        return early
    return run_send_gates(intent, risk)
