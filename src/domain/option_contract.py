"""期权合约静态信息（来自配置 / 品种与合约表）。

功能：把「按合约」配置的乘数、最小变动、到期日、行权价、保证金比例读成一份对象，
      供算仓、计价、到期检查共用。
原理：期货按品种配置就够；期权每个行权价都是独立合约，必须支持完整合约代码作为键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.domain.contracts import DEFAULT_MULTIPLIER
from src.domain.options import option_strike, parse_option_symbol


def contract_box(cfg: dict[str, Any] | None, symbol: str) -> dict[str, Any]:
    """取这个合约的配置：先按完整合约，再退回品种。"""
    contracts = ((cfg or {}).get("risk") or {}).get("contracts") or {}
    if not isinstance(contracts, dict):
        return {}
    key = str(symbol or "").strip().lower()
    box = contracts.get(key)
    if isinstance(box, dict):
        return box
    # 配置里的合约键可能写成大写 C/P，按不区分大小写再找一遍。
    for raw_key, value in contracts.items():
        if str(raw_key).strip().lower() == key and isinstance(value, dict):
            return value
    parsed = parse_option_symbol(symbol)
    product = parsed.product if parsed is not None else "".join(
        ch for ch in key if "a" <= ch <= "z"
    )
    box = contracts.get(product)
    return box if isinstance(box, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class OptionContract:
    """一个期权合约的静态信息。"""

    symbol: str
    underlying: str
    kind: str  # C / P
    strike: float
    expiry: str  # 到期日/最后交易日，格式由配置决定；空串表示没填
    multiplier: float
    price_tick: float
    margin_rate_pct: float
    seller_margin_ratio: float  # 卖方保证金比例（按标的的名义金额）
    underlying_price: float  # 算卖方保证金时用的标的价；0 表示没配
    _cfg_box: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_call(self) -> bool:
        return self.kind == "C"

    def premium_per_lot(self, premium: float) -> float:
        """买方一手要付的权利金。"""
        return max(0.0, float(premium)) * self.multiplier

    def seller_margin_per_lot(self) -> float:
        """卖方一手占用的保证金；缺数据返回 0，由算仓层拒绝开仓。"""
        fixed = _as_float(self._cfg_box.get("seller_margin_per_lot"), 0.0)
        if fixed <= 0:
            fixed = _as_float(self._cfg_box.get("margin"), 0.0)
        if fixed > 0:
            return fixed
        ratio = self.seller_margin_ratio or self.margin_rate_pct
        # 没有标的价时用行权价近似（平值附近两者接近），宁可高估保证金。
        price_ref = self.underlying_price or self.strike
        if ratio > 0 and price_ref > 0 and self.multiplier > 0:
            return price_ref * self.multiplier * (ratio / 100.0)
        return 0.0


def contract_from_cfg(cfg: dict[str, Any] | None, symbol: str) -> OptionContract | None:
    """读出期权合约；不是期权返回 None。"""
    parsed = parse_option_symbol(symbol)
    if parsed is None:
        return None
    box = contract_box(cfg, symbol)
    multiplier = _as_float(box.get("multiplier"), DEFAULT_MULTIPLIER) or DEFAULT_MULTIPLIER
    price_tick = _as_float(box.get("price_tick"), 0.0)
    margin_rate = _as_float(box.get("margin_rate_pct"), 0.0)
    seller_ratio = _as_float(box.get("seller_margin_ratio"), 0.0)
    underlying_price = _as_float(box.get("underlying_price"), 0.0)
    expiry = str(
        box.get("expiry")
        or box.get("expire_date")
        or box.get("last_trading_day")
        or ""
    ).strip()
    contract = OptionContract(
        symbol=str(symbol).strip(),
        underlying=parsed.product,
        kind=str(box.get("option_type") or parsed.kind).upper(),
        strike=_as_float(box.get("strike"), parsed.strike) or option_strike(symbol),
        expiry=expiry,
        multiplier=multiplier,
        price_tick=price_tick,
        margin_rate_pct=margin_rate,
        seller_margin_ratio=seller_ratio,
        underlying_price=underlying_price,
        _cfg_box=dict(box),
    )
    return contract
