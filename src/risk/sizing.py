"""
资金管理 / 仓位计算

功能：按统一优先级算出「这一笔最多开几手」，避免几条规则各算各的、互相打架。

开仓手数最终取下面几项的最小值（算出来少于最小手数就不开）：

1) 意图手数（想开多少）
   - 凯利开关打开：按该腿最近净平仓算动态目标，样本不足用固定风险，负期望不开
   - risk_pct：floor(权益 × 每笔风险% ÷ 每手完整风险)
   - 每手完整风险 = 止损距离×乘数 + 开平手续费 + 开平各一次预计滑点
   - 固定风险%永远是硬上限，凯利不能把它调大
   - fixed：固定手数只作为策略上限，仍先按风险%算仓再取较小值，不能绕过止损

2) 仓位上限（保证金占用，公共设置）
   - 填了保证金比例：每手占用 = 开仓价 × 每手数量 × 比例%
     价格高了同一手要交的钱也变多
   - 没填比例、只写了每手保证金金额：仍按那个固定金额
   - 有占用数时：floor( (权益 × 仓位上限% − 已占用) ÷ 这一手占用 )
   - 仓位上限% ≤ 0，或本品种没填比例也没填金额：本层不限制
   - 例：燃油 4000 点、每手 10 吨、比例 23% → 每手 9200 元

3) 单笔上限（柜台渠道）
   - 由调用方传入当前交易对象的 max_order_volume；回测不传，历史结果不变
   - 委托超过这个数会在发单前被本机拦住，所以算仓时直接压到上限

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

from src.domain.contracts import DEFAULT_CONTRACTS as DEFAULT_CONTRACTS
from src.domain.contracts import DEFAULT_MULTIPLIER, build_contracts

# 仓位上限默认 80%：权益里最多拿出这么多交保证金，其余留作缓冲。
DEFAULT_MARGIN_USAGE_PCT = 80.0


def bind_run_margin(cfg: dict[str, Any], margin_per_lot: float) -> None:
    """
    这一次运行改成同一个每手保证金金额，并关掉品种比例。

    交叉测试要比策略、不要被高保证金品种卡住时，传入 0。
    历史回测跟品种表走时不要调用这里，否则价格一变手数还是按死金额算。
    不写磁盘。
    """
    risk = cfg.setdefault("risk", {})
    risk["margin_per_lot"] = max(0.0, float(margin_per_lot or 0))
    for item in (risk.get("contracts") or {}).values():
        if isinstance(item, dict):
            item["margin"] = 0
            item["margin_rate_pct"] = 0


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
        self._cfg = cfg
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

        self.contracts = build_contracts(cfg)

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
        return float(self.contracts.get(key, {}).get("multiplier", DEFAULT_MULTIPLIER))

    def margin_for(self, symbol: str, price: float | None = None) -> float:
        """
        这个品种 1 手现在要占用多少保证金。

        填了比例：开仓价 × 每手数量 × 比例%。没填比例：用表里的固定金额。
        再没有就用配置页兜底金额。都是 0 就不限制。
        """
        key = self._product_key(symbol)
        box = self.contracts.get(key, {})
        rate = float(box.get("margin_rate_pct", 0) or 0)
        qty = float(box.get("multiplier", DEFAULT_MULTIPLIER) or DEFAULT_MULTIPLIER)
        if rate > 0 and price is not None:
            try:
                px = float(price)
            except (TypeError, ValueError):
                px = 0.0
            if px > 0 and qty > 0:
                return px * qty * (rate / 100.0)
        specific = float(box.get("margin", 0) or 0)
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
        max_per_order: int | None = None,
        kelly_rows: list[dict[str, Any]] | None = None,
    ) -> SizeResult:
        """
        计算开仓手数。

        参数:
            symbol: 品种代码
            entry_price: 计划入场价（可空）
            stop_price: 策略止损价（可空；按风险百分比算仓时必须有效，否则拒单）
            current_abs_position: 当前已持手数绝对值（用于已占用保证金）
            positions: 各品种净持仓。用来按各自的保证金扣掉已经占用的钱。
            max_per_order: 本机单笔上限（交易对象的 max_order_volume）。
            kelly_rows: 当前账本已平仓配对；传入空列表也表示这条腿确实没有样本。
        """
        intent, intent_note, risk_cash, risk_per_lot, stop_dist = self._intent_for_symbol(
            symbol, entry_price, stop_price, kelly_rows=kelly_rows
        )
        margin_lots, lot_margin, margin_note = self._lots_by_margin_cap(
            symbol, current_abs_position, positions, entry_price
        )

        caps = [intent]
        if margin_lots is not None:
            caps.append(margin_lots)
        order_cap: int | None = None
        if max_per_order is not None and int(max_per_order) > 0:
            order_cap = int(max_per_order)
            caps.append(order_cap)
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
            if intent < self.min_volume and stop_dist > 0:
                why = (
                    f"按风险{(risk_cash / self.capital * 100 if self.capital > 0 else 0):g}%只允许约{intent}手，低于最小{self.min_volume}手"
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
            reason += f" / 仓位上限{margin_lots}"
        if order_cap is not None:
            reason += f" / 单笔上限{order_cap}"
        if margin_lots is not None:
            reason += f"；{margin_note}，每手{lot_margin:.0f}元"
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
        *,
        kelly_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[int, str, float, float, float]:
        """意图手数：风险百分比或固定手数。返回 (手数, 说明, 风险预算, 每手风险, 止损距)。"""
        from src.risk.kelly import kelly_enabled, plan_from_config

        risk_pct = self.risk_pct
        kelly_note = ""
        if kelly_enabled(self._cfg):
            plan = plan_from_config(self._cfg, symbol=symbol, closed_rows=kelly_rows)
            if not plan.ok:
                return 0, plan.reason, 0.0, 0.0, 0.0
            # 固定风险只读配置，不把某次凯利结果写回运行状态，避免污染下一品种。
            risk_pct = min(plan.risk_pct, self.risk_pct)
            kelly_note = plan.reason + "；"

        if (
            entry_price is None
            or stop_price is None
            or float(entry_price) <= 0
            or float(stop_price) == 0
        ):
            # 按风险算仓必须有止损价（0 也视为没设），fixed 上限模式也一样。
            return 0, "缺少有效的入场价/止损价，按风险算仓不开无止损仓", 0.0, 0.0, 0.0

        stop_dist = abs(float(entry_price) - float(stop_price))
        if stop_dist <= 0:
            return 0, "止损距离为0，无法按风险算仓，跳过开仓", 0.0, 0.0, 0.0

        from src.backtest.fill import FillModel
        from src.risk.commission import close_commission_per_lot, open_commission_per_lot

        mult = self.multiplier_for(symbol)
        # build_contracts 只保留乘数/保证金；手续费必须读同一份原始品种表，不能在这里被丢成 0。
        fee_contracts = (self._cfg.get("risk") or {}).get("contracts") or {}
        open_fee = open_commission_per_lot(fee_contracts, symbol)
        # 开仓时还不知道持有多久，按平今费率预估，和账本/回测手续费配置保持同源。
        close_fee = close_commission_per_lot(fee_contracts, symbol, close_today=True)
        fill = FillModel.from_cfg(self._cfg, symbol)
        slippage_cost = 2 * fill.slippage_ticks * fill.price_tick * mult
        fee_cost = open_fee + close_fee
        risk_per_lot = stop_dist * mult + fee_cost + slippage_cost
        risk_cash = self.capital * (risk_pct / 100.0)
        if risk_per_lot <= 0:
            return 0, "每手风险异常，跳过开仓", risk_cash, 0.0, stop_dist

        raw = int(risk_cash // risk_per_lot)
        note = (
            f"{kelly_note}风险仓位: 权益{self.capital:.0f}×{risk_pct:g}%={risk_cash:.0f}元 "
            f"/ 每手≈{risk_per_lot:.0f}元 → 意图{raw}手"
            f"（止损距{stop_dist:.2f}，乘数{mult}，开平费{fee_cost:.2f}，双边滑点{slippage_cost:.2f}）"
        )
        if self.mode == "fixed":
            # fixed 不再绕过风险算仓：固定手数只是策略上限，风险手数更小时按风险。
            cap = max(0, self.fixed_volume)
            raw = min(raw, cap)
            note = f"固定手数上限{cap}手；" + note
        return raw, note, risk_cash, risk_per_lot, stop_dist

    def _lots_by_margin_cap(
        self,
        symbol: str,
        current_abs_position: int,
        positions: dict[str, int] | None,
        entry_price: float | None = None,
    ) -> tuple[int | None, float, str]:
        """
        仓位上限层：还能再开几手。

        预算 = 权益 × 仓位上限%。已占用按各品种自己的保证金扣。
        有比例时按这笔开仓价现算每手占用。
        没填保证金 → 返回 None（本层不限制）。
        仓位上限% ≤ 0 → 仍按全部权益估买得起几手（兼容旧「钱够就开」）。
        """
        need = self.margin_for(symbol, entry_price)
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
            this_key = self._product_key(symbol)
            for held, vol in positions.items():
                held_price = entry_price if self._product_key(str(held)) == this_key else None
                held_margin = self.margin_for(str(held), held_price)
                if held_margin <= 0:
                    held_margin = need
                used += abs(int(vol)) * held_margin
        else:
            used = abs(current_abs_position) * need

        remain = budget - used
        # 期货公司回报的可用资金已经扣过真实保证金。再和本地估算取较小值，
        # 不会因为两边各扣一次而少开；可用更紧时按柜台为准。
        if self.cash_available is not None:
            avail: float | None
            try:
                avail = float(self.cash_available)
            except (TypeError, ValueError):
                avail = None
            if avail is not None:
                remain = min(remain, avail)
                note = note + f"，柜台可用{avail:.0f}元"
        if remain < need:
            return 0, need, note
        return int(remain // need), need, note
