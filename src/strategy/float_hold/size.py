"""
日内浮盈飘单 · 仓位控制。

功能：本策略不算另一套手数。开仓意图交给配置页左边已经有的每笔风险%、凯利公式、仓位上限。
原理：回测和挂机本来就会用公共风控算手数。这里只标明「用公共仓位」，避免以后又在策略里写死 1 手。
"""

from __future__ import annotations

from typing import Any

from src.strategy.types import StrategyAction, StrategyDecision


class PublicRiskSize:
    """
    接入公共凯利 / 每笔风险%。

    打开凯利：意图风险改由凯利算，仓位上限和日亏熔断仍拦。
    关掉凯利：按每笔风险% 和止损距离算手数。
    """

    key = "public_risk"
    label = "公共凯利或每笔风险"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or {}

    def source_name(self) -> str:
        """这一笔手数最终听哪一套公共设置。"""
        risk = self.cfg.get("risk") if isinstance(self.cfg.get("risk"), dict) else {}
        kelly = risk.get("kelly") if isinstance(risk.get("kelly"), dict) else {}
        if kelly.get("enabled"):
            return "public_kelly"
        mode = str(risk.get("sizing_mode") or "risk_pct").strip().lower()
        if mode == "fixed":
            return "public_fixed"
        return "public_risk_pct"

    def note(self) -> str:
        name = self.source_name()
        if name == "public_kelly":
            return "手数跟配置页左边的凯利公式，再和仓位上限取小"
        if name == "public_fixed":
            return "手数跟配置页左边的固定手数，再和仓位上限取小"
        return "手数跟配置页左边的每笔风险%，再和仓位上限取小"

    def annotate(self, decision: StrategyDecision) -> StrategyDecision:
        """开仓决定上记下仓位来源，方便扫描解说和以后对照。"""
        if decision.action not in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            return decision
        decision.meta.setdefault("size_source", self.source_name())
        decision.meta.setdefault("size_note", self.note())
        return decision
