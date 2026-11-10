"""
资金管理 / 仓位计算

功能：按统一优先级算出「这一笔最多开几手」，避免几条规则各算各的、互相打架。

开仓手数最终取下面几项的最小值（算出来少于最小手数就不开）：

1) 意图手数（想开多少）
   - risk_pct：floor(权益 × 每笔风险% ÷ (止损距离 × 合约乘数))
   - fixed：固定手数
   - 缺入场/止损价：退回固定手数

2) 仓位上限（保证金占用，公共设置）
   - 有每手保证金时：
     floor( (权益 × 仓位上限% − 已占用保证金) ÷ 每手保证金 )
   - 仓位上限% ≤ 0，或本品种没填保证金：本层不限制（≤0 时仍按全部权益估买得起几手）
   - 例：权益 21373、上限 80%、保证金 10500 → 预算约 17098 → 最多 1 手

开仓前另外还要过（不算进手数，只决定能不能下单）：
   - 日最大亏损熔断（只拦开新仓，不自动平仓）
   - 两笔开仓最小间隔、每天最多开仓次数

说明：已不再使用「最大持仓手数」；总仓位靠仓位上限%和日最大亏损管。
长线加减仓另用策略自己的「单元手数×加仓次数」封顶。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

# 国内常见主力：报价「元/吨」，合约乘数「吨/手」→ 价格动1点，每手盈亏≈乘数 元
DEFAULT_CONTRACTS: dict[str, dict[str, float]] = {
    "bu": {"multiplier": 10, "name": "沥青"},
    "c": {"multiplier": 10, "name": "玉米"},
    "rb": {"multiplier": 10, "name": "螺纹"},
}

# 仓位上限默认 80%：权益里最多拿出这么多交保证金，其余留作缓冲。
DEFAULT_MARGIN_USAGE_PCT = 80.0


def bind_run_margin(cfg: dict[str, Any], margin_per_lot: float) -> None:
    """
    回测或优化这一次只认传入的每手保证金。

    配置页可以按品种写不同保证金，那是实盘用的。
    回测页只有一个输入框，所以这一次先把副本里的品种保证金清成 0，
    再使用输入框上的数字。填 0 就是这次不看保证金。不写磁盘。
    """
    risk = cfg.setdefault("risk", {})
    risk["margin_per_lot"] = max(0.0, float(margin_per_lot or 0))
    for item in (risk.get("contracts") or {}).values():
        if isinstance(item, dict):
            item["margin"] = 0


@dataclass
class SizeResult:
    """仓位计算结果。"""

    volume: int
    reason: str
    risk_cash: float = 0.0  # 本笔允许亏损（元）
    risk_per_lot: float = 0.0  # 每手若打止损亏多少（元）
    stop_distance: float = 0.0  # 止损距离（价格点）
    intent_lots: int = 0  # 意图手数（风险或固定）
    margin_lots: int | None = None  # 仓位上限允许的手数；None=本层未启用


class PositionSizer:
    """根据配置与止损距离计算开仓手数。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        risk = cfg.get("risk", {})
        trade = cfg.get("trade", {})
        # fixed = 固定手数；risk_pct = 按风险百分比（推荐）
        self.mode = str(risk.get("sizing_mode", "risk_pct")).lower()
        self.fixed_volume = int(trade.get("volume", 1))
        self.capital = float(risk.get("capital", 1_000_000))
        # 柜台「可用资金」。常驻 CTP 对账后写入；没写时仓位上限仍只看权益。
        # 有数时：保证金预算再和可用资金取较小值，避免按本地估的占用重复扣一遍。
        self.cash_available: float | None = None
        self.risk_pct = float(risk.get("risk_per_trade_pct", 0.5))
        self.min_volume = int(risk.get("min_volume", 1))
        # 每手保证金。0 表示品种表也没写时不看保证金。
        self.margin_per_lot = float(risk.get("margin_per_lot", 0) or 0)
        # 仓位上限%：权益里最多拿出这么多交保证金。≤0 表示本层关闭（按全部权益估）。
        try:
            self.margin_usage_pct = float(
                risk.get("margin_usage_pct", DEFAULT_MARGIN_USAGE_PCT)
            )
        except (TypeError, ValueError):
            self.margin_usage_pct = DEFAULT_MARGIN_USAGE_PCT

        contracts = {**DEFAULT_CONTRACTS}
        custom = risk.get("contracts") or {}
        for k, v in custom.items():
            if isinstance(v, dict):
                contracts[str(k).lower()] = {
                    "multiplier": float(v.get("multiplier", 10)),
                    "name": str(v.get("name", k)),
                    "margin": float(v.get("margin", 0) or 0),
                }
            else:
                contracts[str(k).lower()] = {"multiplier": float(v), "name": str(k)}
        self.contracts = contracts

    def _product_key(self, symbol: str) -> str:
        """
        从合约名取出品种代码。

        bu2612 / BU0 → bu。只认完整的字母段，不用「字符串里包含某个字母」。
        否则 c 会误匹配 cu、sc，玉米的乘数就会套到别的品种上。
        """
        s = symbol.strip().lower()
        alpha = "".join(ch for ch in s if ("a" <= ch <= "z"))
        if alpha in self.contracts:
            return alpha
        return alpha or s

    def multiplier_for(self, symbol: str) -> float:
        key = self._product_key(symbol)
        return float(self.contracts.get(key, {}).get("multiplier", 10))

    def margin_for(self, symbol: str) -> float:
        """
        这个品种 1 手要占用多少保证金。

        品种自己写了数就用自己的。没写就用「每手保证金」那个统一数字。都是 0 就不限制。
        """
        key = self._product_key(symbol)
        specific = float(self.contracts.get(key, {}).get("margin", 0) or 0)
        if specific > 0:
            return specific
        return self.margin_per_lot

    def calc_open(
        self,
        symbol: str,
        entry_price: float | None,
        stop_price: float | None,
        current_abs_position: int = 0,
        positions: dict[str, int] | None = None,
    ) -> SizeResult:
        """
        计算开仓手数。

        参数:
            symbol: 品种代码
            entry_price: 计划入场价（可空）
            stop_price: 策略止损价（可空；空则退回固定手数）
            current_abs_position: 当前已持手数绝对值（用于已占用保证金）
            positions: 各品种净持仓。用来按各自的保证金扣掉已经占用的钱。
        """
        intent, intent_note, risk_cash, risk_per_lot, stop_dist = self._intent_for_symbol(
            symbol, entry_price, stop_price
        )
        margin_lots, lot_margin, margin_note = self._lots_by_margin_cap(
            symbol, current_abs_position, positions
        )

        caps = [intent]
        if margin_lots is not None:
            caps.append(margin_lots)
        vol = min(caps)

        if margin_lots is not None and margin_lots <= 0 and intent > 0:
            return SizeResult(
                0,
                (
                    f"仓位上限不够：{symbol}每手{lot_margin:.0f}元，"
                    f"{margin_note}，最多再开0手"
                ),
                risk_cash=risk_cash,
                risk_per_lot=risk_per_lot,
                stop_distance=stop_dist,
                intent_lots=intent,
                margin_lots=0,
            )

        if vol < self.min_volume:
            if self.mode != "fixed" and intent < self.min_volume and stop_dist > 0:
                why = (
                    f"按风险{self.risk_pct}%只允许约{intent}手，低于最小{self.min_volume}手"
                    f"（止损距{stop_dist:.2f}点，每手亏≈{risk_per_lot:.0f}元，"
                    f"本笔预算{risk_cash:.0f}元）。止损过宽时宁可不做。"
                )
            elif margin_lots is not None and intent > (margin_lots or 0):
                why = (
                    f"按意图约{intent}手，但仓位上限只允许{margin_lots}手"
                    f"（{margin_note}，每手{lot_margin:.0f}元）"
                )
            else:
                why = intent_note or "手数不足最小开仓"
            return SizeResult(
                0,
                why,
                risk_cash=risk_cash,
                risk_per_lot=risk_per_lot,
                stop_distance=stop_dist,
                intent_lots=intent,
                margin_lots=margin_lots,
            )

        reason = f"{intent_note} → 取小后 {vol}手（意图{intent}"
        if margin_lots is not None:
            reason += f" / 仓位上限{margin_lots}；{margin_note}，每手{lot_margin:.0f}元"
        reason += "）"
        logger.info(reason)
        return SizeResult(
            vol,
            reason,
            risk_cash=risk_cash,
            risk_per_lot=risk_per_lot,
            stop_distance=stop_dist,
            intent_lots=intent,
            margin_lots=margin_lots,
        )

    def _intent_for_symbol(
        self,
        symbol: str,
        entry_price: float | None,
        stop_price: float | None,
    ) -> tuple[int, str, float, float, float]:
        """意图手数：风险百分比或固定手数。返回 (手数, 说明, 风险预算, 每手风险, 止损距)。"""
        if self.mode == "fixed":
            vol = max(0, self.fixed_volume)
            return vol, f"固定手数模式 volume={vol}", 0.0, 0.0, 0.0

        if entry_price is None or stop_price is None:
            vol = max(self.min_volume, self.fixed_volume)
            return vol, f"缺少入场/止损价，退回固定手数 {vol}", 0.0, 0.0, 0.0

        stop_dist = abs(float(entry_price) - float(stop_price))
        if stop_dist <= 0:
            return 0, "止损距离为0，无法按风险算仓，跳过开仓", 0.0, 0.0, 0.0

        mult = self.multiplier_for(symbol)
        risk_per_lot = stop_dist * mult
        risk_cash = self.capital * (self.risk_pct / 100.0)
        if risk_per_lot <= 0:
            return 0, "每手风险异常，跳过开仓", risk_cash, 0.0, stop_dist

        raw = int(risk_cash // risk_per_lot)
        note = (
            f"风险仓位: 权益{self.capital:.0f}×{self.risk_pct}%={risk_cash:.0f}元 "
            f"/ 每手≈{risk_per_lot:.0f}元 → 意图{raw}手"
            f"（止损距{stop_dist:.2f}，乘数{mult}）"
        )
        return raw, note, risk_cash, risk_per_lot, stop_dist

    def _lots_by_margin_cap(
        self,
        symbol: str,
        current_abs_position: int,
        positions: dict[str, int] | None,
    ) -> tuple[int | None, float, str]:
        """
        仓位上限层：还能再开几手。

        预算 = 权益 × 仓位上限%。已占用按各品种自己的保证金扣。
        没填保证金 → 返回 None（本层不限制）。
        仓位上限% ≤ 0 → 仍按全部权益估买得起几手（兼容旧「钱够就开」）。
        """
        need = self.margin_for(symbol)
        if need <= 0:
            return None, 0.0, ""

        pct = float(self.margin_usage_pct or 0)
        if pct <= 0:
            budget = self.capital
            note = f"权益{self.capital:.0f}元（仓位上限已关，按全部权益）"
        else:
            pct = min(pct, 100.0)
            budget = self.capital * (pct / 100.0)
            note = f"权益{self.capital:.0f}×仓位上限{pct:g}%={budget:.0f}元"

        if positions:
            used = 0.0
            for held, vol in positions.items():
                held_margin = self.margin_for(str(held))
                if held_margin <= 0:
                    held_margin = need
                used += abs(int(vol)) * held_margin
        else:
            used = abs(current_abs_position) * need

        remain = budget - used
        # 期货公司回报的可用资金已经扣过真实保证金。再和本地估算取较小值，
        # 不会因为两边各扣一次而少开；可用更紧时按柜台为准。
        if self.cash_available is not None:
            try:
                avail = float(self.cash_available)
            except (TypeError, ValueError):
                avail = None
            else:
                remain = min(remain, avail)
                note = note + f"，柜台可用{avail:.0f}元"
        if remain < need:
            return 0, need, note
        return int(remain // need), need, note
