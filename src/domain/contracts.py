"""
合约乘数。

功能：默认品种乘数与配置合并只在这一处，手数计算和账户说明共用。
"""

from __future__ import annotations

from typing import Any

# 配置没写乘数时的兜底值。全程序只认这一处，不在各页面再写 10。
DEFAULT_MULTIPLIER: float = 10.0

# 回测/统计展示里「没填保证金比例」时的兜底比例（小数，0.10 = 10%）。
# 仅用于收益率、资金使用率、容量等展示估算，不影响实盘开仓手数（实盘缺比例一律拒开）。
# 全程序只认这一处：metrics / engine / portfolio / signal_stats / 界面 / tools 都从这里取。
MARGIN_RATE: float = 0.10

# 国内常见主力：报价「元/吨」，合约乘数「吨/手」→ 价格动1点，每手盈亏≈乘数 元
DEFAULT_CONTRACTS: dict[str, dict[str, Any]] = {
    "bu": {"multiplier": 10, "name": "沥青"},
    "c": {"multiplier": 10, "name": "玉米"},
    "rb": {"multiplier": 10, "name": "螺纹"},
}


def _optional_float(value: Any) -> float | None:
    """可选数字：空值或写错时返回 None，不把 0 当有效数据。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _contract_entry(key: str, value: Any) -> dict[str, Any]:
    """把一个品种/合约配置收成统一字典，保留计价和期权需要的可选字段。"""
    if not isinstance(value, dict):
        return {"multiplier": float(value), "name": str(key)}
    entry: dict[str, Any] = {
        "multiplier": float(value.get("multiplier", DEFAULT_MULTIPLIER)),
        "name": str(value.get("name", key)),
        "margin_rate_pct": float(value.get("margin_rate_pct", 0) or 0),
    }
    for field in ("price_tick", "strike", "seller_margin_ratio", "underlying_price"):
        number = _optional_float(value.get(field))
        if number is not None:
            entry[field] = number
    for field in ("option_type", "expiry", "underlying"):
        text = str(value.get(field) or "").strip()
        if text:
            entry[field] = text
    return entry


def build_contracts(cfg: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """默认品种表叠加配置里的 risk.contracts，键统一成小写。

    期货按品种配（rb）；期权可以按完整合约配（rb2601C3500），
    两种键同时保留，查的时候先按合约、再退回品种。
    """
    contracts: dict[str, dict[str, Any]] = {key: dict(info) for key, info in DEFAULT_CONTRACTS.items()}
    risk = (cfg or {}).get("risk") or {}
    for key, value in (risk.get("contracts") or {}).items():
        contracts[str(key).lower()] = _contract_entry(str(key), value)
    return contracts


def contract_multipliers(cfg: dict[str, Any] | None) -> dict[str, float]:
    """品种代码 → 一手多少吨。账户页算说明文字时用。"""
    if not cfg:
        return {}
    return {key: float(info.get("multiplier", DEFAULT_MULTIPLIER)) for key, info in build_contracts(cfg).items()}
