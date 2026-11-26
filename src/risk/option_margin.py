"""期权权利金 / 保证金口径。

功能：把「买方付权利金、卖方交保证金」翻译成算仓层能直接用的每手占用。
原理：期货的「价格 × 乘数 × 保证金比例%」对期权买方正好等于权利金，
      对卖方却不成立；卖方必须按标的名义金额的比例或柜台给的每手保证金算。
      缺数据时返回 0，由 PositionSizer 拒绝开仓，绝不估算放行。
"""

from __future__ import annotations

from typing import Any

from src.domain.option_contract import contract_from_cfg

_BUY_SIDES = {"long", "buy", "buy_open", "买", "买入", "多"}
_SELL_SIDES = {"short", "sell", "sell_open", "卖", "卖出", "空"}


def is_buy_side(side: str | None) -> bool:
    """开仓方向是不是买方（付权利金）。"""
    text = str(side or "").strip().lower()
    if text in _BUY_SIDES:
        return True
    if text in _SELL_SIDES:
        return False
    # 拿不准时按卖方算：保证金口径更大，宁可少开也不低估风险。
    return False


def lot_margin(
    cfg: dict[str, Any] | None,
    symbol: str,
    price: float | None,
    side: str | None = None,
) -> tuple[float, str]:
    """
    期权开仓一手的资金占用。

    返回 (每手占用, 说明)。不是期权或数据不全时占用为 0。
    """
    contract = contract_from_cfg(cfg, symbol)
    if contract is None:
        return 0.0, "不是期权合约"
    try:
        premium = float(price or 0)
    except (TypeError, ValueError):
        premium = 0.0
    if is_buy_side(side):
        if premium <= 0 or contract.multiplier <= 0:
            return 0.0, f"{symbol} 买方权利金缺价格或乘数，拒绝开仓"
        cost = contract.premium_per_lot(premium)
        return cost, (
            f"买方权利金 {premium:g}×{contract.multiplier:g}={cost:.0f}元"
        )
    margin = contract.seller_margin_per_lot()
    if margin <= 0:
        return 0.0, (
            f"{symbol} 卖方保证金缺数据：请在品种与合约表填"
            "「每手保证金」或「卖方保证金比例% + 标的价」，拒绝开仓"
        )
    return margin, f"卖方保证金 {margin:.0f}元"
