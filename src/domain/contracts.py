"""
合约乘数。

功能：默认品种乘数与配置合并只在这一处，手数计算和账户说明共用。
"""

from __future__ import annotations

from typing import Any

# 配置没写乘数时的兜底值。全程序只认这一处，不在各页面再写 10。
DEFAULT_MULTIPLIER: float = 10.0

# 国内常见主力：报价「元/吨」，合约乘数「吨/手」→ 价格动1点，每手盈亏≈乘数 元
DEFAULT_CONTRACTS: dict[str, dict[str, Any]] = {
    "bu": {"multiplier": 10, "name": "沥青"},
    "c": {"multiplier": 10, "name": "玉米"},
    "rb": {"multiplier": 10, "name": "螺纹"},
}


def build_contracts(cfg: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """默认品种表叠加配置里的 risk.contracts，键统一成小写品种代码。"""
    contracts: dict[str, dict[str, Any]] = {key: dict(info) for key, info in DEFAULT_CONTRACTS.items()}
    risk = (cfg or {}).get("risk") or {}
    for key, value in (risk.get("contracts") or {}).items():
        if isinstance(value, dict):
            contracts[str(key).lower()] = {
                "multiplier": float(value.get("multiplier", DEFAULT_MULTIPLIER)),
                "name": str(value.get("name", key)),
                "margin_rate_pct": float(value.get("margin_rate_pct", 0) or 0),
            }
        else:
            contracts[str(key).lower()] = {"multiplier": float(value), "name": str(key)}
    return contracts


def contract_multipliers(cfg: dict[str, Any] | None) -> dict[str, float]:
    """品种代码 → 一手多少吨。账户页算说明文字时用。"""
    if not cfg:
        return {}
    return {key: float(info.get("multiplier", DEFAULT_MULTIPLIER)) for key, info in build_contracts(cfg).items()}
