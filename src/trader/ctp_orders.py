"""
常驻策略和交易台共用的 CTP 报单请求。

功能：只负责把「买开 / 卖开 / 平仓 / 反手」收成同一份 OrderRequest。
原理：真正发到柜台的只有 CtpTrader.place_order。交易台点按钮、常驻收到策略信号，
     最后都走进这一份请求，再由同一套平今平昨和限价申报发出去。
     价格由操作者（人填盘口，或策略给出信号价）决定，不在这里另写一套指令。
"""

from __future__ import annotations

from typing import Any

from src.trader.base import OrderRequest, Side
from src.trader.ctp_logic import (
    PositionView,
    close_sides_for_view,
    reverse_open_side,
)
from src.trader.order_timeline import new_trace_id


def ctp_order(
    symbol: str,
    side: Side,
    volume: int,
    price: float,
    *,
    position_side: str | None = None,
    stop_price: float | None = None,
    bar_id: str = "",
    remark: str = "",
    trace_id: str = "",
    meta: dict[str, Any] | None = None,
) -> OrderRequest:
    """组一笔和常驻相同字段的限价单。stop_price / bar_id 只记在本地，不改变柜台报单格式。"""
    return OrderRequest(
        symbol=str(symbol or "").strip(),
        side=side,
        volume=int(volume),
        price=float(price),
        position_side=position_side,
        stop_price=stop_price,
        bar_id=str(bar_id or ""),
        remark=str(remark or ""),
        trace_id=str(trace_id or (meta or {}).get("trace_id") or "") or new_trace_id(),
        meta=dict(meta or {}),
    )


def flatten_order_steps(view: PositionView | None) -> list[dict[str, Any]]:
    """
    平掉柜台这一合约的每一边。锁仓会得到两步（平多、平空）。

    常驻收盘清仓通常只有程序账上的一边；交易台按柜台真实持仓平，两边都能测到同一套平仓报单。
    """
    if view is None:
        return []
    steps: list[dict[str, Any]] = []
    for side in close_sides_for_view(view):
        lots = view.long_lots if side == "long" else view.short_lots
        steps.append({"side": "close", "position_side": side, "volume": int(lots)})
    return steps


def reverse_order_steps(view: PositionView | None) -> tuple[str, list[dict[str, Any]]]:
    """
    策略反手：先平旧仓，再开反向。

    返回 (错误说明, 步骤)。锁仓或空仓时步骤为空，调用方不要发单。
    """
    if view is None or (view.long_lots <= 0 and view.short_lots <= 0):
        return "没有持仓，反手没有可平的仓。", []
    if view.long_lots > 0 and view.short_lots > 0:
        return "锁仓时请先平掉一边，再点反手。策略也是先平旧仓再开反向。", []
    pos_side = "long" if view.long_lots > 0 else "short"
    lots = view.long_lots if pos_side == "long" else view.short_lots
    return "", [
        {"side": "close", "position_side": pos_side, "volume": int(lots)},
        {"side": reverse_open_side(pos_side), "volume": int(lots)},
    ]


def prepare_live_ctp_cfg(cfg: dict[str, Any], kind: str) -> dict[str, Any]:
    """
    交易台连柜台：关掉模拟、换操作账。

    等待秒数、超时自动撤单跟配置里常驻用的那一份，不再在这里改短或关掉。
    """
    trade = cfg.setdefault("trade", {})
    ctp = trade.setdefault("ctp", {})
    cfg["dry_run"] = False
    ctp["account_kind"] = kind
    # 只在内存里标记：这是交易台会话。不改 yaml 里的等待秒数。
    # 挂单留给人撤，方便华泰测手撤、批量撤、重复报单；常驻仍一次只挂一笔并超时撤。
    ctp["desk_session"] = True
    return cfg
