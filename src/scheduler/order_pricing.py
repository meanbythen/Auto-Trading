"""CTP 订单定价：对手价限价、保护跳数、盘口快照与开仓滑点拦截。

方法挂在 OrderPricingMixin 上，共享状态由 TradingBot 提供；本模块不持有运行状态。
"""

from __future__ import annotations

from loguru import logger

from src.strategy import StrategyAction, StrategyDecision
from src.trader.order_timeline import now_iso


def ctp_opponent_limit(
    *,
    is_buy: bool,
    last: float,
    bid: float = 0.0,
    ask: float = 0.0,
    upper: float = 0.0,
    lower: float = 0.0,
    tick: float = 1.0,
    extra_ticks: int = 2,
) -> float | None:
    """
    CTP 限价按对手价方向挂，贴近同花顺「对手价」。

    买入用卖一（没有卖一就用最新价），再往上让几跳，更容易立刻成交。
    卖出用买一，再往下让几跳。最后夹在涨跌停之间，并对齐最小变动价位。
    """
    step = float(tick or 1)
    if step <= 0:
        step = 1.0
    extra = max(0, int(extra_ticks or 0)) * step
    if is_buy:
        start = float(ask or 0) or float(last or 0)
        if start <= 0:
            return None
        price = start + extra
        cap = float(upper or 0)
        if cap > 0:
            price = min(price, cap)
    else:
        start = float(bid or 0) or float(last or 0)
        if start <= 0:
            return None
        price = start - extra
        floor = float(lower or 0)
        if floor > 0:
            price = max(price, floor)
    if price <= 0:
        return None
    from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

    quant = Decimal(str(step))
    raw = Decimal(str(price))
    rounded = (raw / quant).to_integral_value(
        rounding=ROUND_CEILING if is_buy else ROUND_FLOOR
    ) * quant
    out = float(rounded)
    return out if out > 0 else None


def entry_slip_blocked(
    entry_price: float,
    live_price: float,
    atr: float,
    *,
    is_long: bool,
    limit_atr: float,
    limit_points: float = 0.0,
) -> tuple[bool, float, float]:
    """
    开仓滑点闸门：信号价和现价差太多就不追。

    返回 (是否拦下, 反向偏移点数, 允许的上限点数)。
    上限有两种算法，同时开着就取更严的那个：limit_atr×ATR，或直接写死的点数。
    追价成本只算「不利于持仓方向」的那一边：做多现价更高、做空现价更低才算。
    数据不全（价格取不到）时一律放行，宁可漏拦也不要卡死下单。
    """
    if entry_price <= 0 or live_price <= 0:
        return False, 0.0, 0.0
    limit = 0.0
    if limit_atr > 0 and atr > 0:
        limit = float(limit_atr) * float(atr)
    if limit_points > 0:
        limit = min(limit, float(limit_points)) if limit > 0 else float(limit_points)
    if limit <= 0:
        return False, 0.0, 0.0
    chase = (live_price - entry_price) if is_long else (entry_price - live_price)
    return chase > limit, chase, limit


class OrderPricingMixin:
    """价格、盘口与滑点闸门；只读 cfg/trader/kline，不改账本。"""

    def _price_tick_for(self, symbol: str) -> float:
        """每跳价格：先看品种表，再看 CTP/交易配置，最后退回 1。"""
        from src.risk.universe_table import product_key

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        box = contracts.get(product_key(symbol)) if isinstance(contracts, dict) else {}
        raw = (box or {}).get("price_tick")
        if raw in (None, ""):
            trade = self.cfg.get("trade") or {}
            raw_ctp = trade.get("ctp")
            ctp_box = raw_ctp if isinstance(raw_ctp, dict) else {}
            raw = ctp_box.get("price_tick", trade.get("price_tick", 1))
        try:
            tick = float(raw or 1)
        except (TypeError, ValueError):
            tick = 1.0
        return tick if tick > 0 else 1.0

    def _close_protect_ticks(self) -> int:
        """平仓保护滑点跳数。对手价再让几跳，避免刚好挂在盘口外侧。"""
        trade = self.cfg.get("trade") or {}
        try:
            return max(0, int(trade.get("ctp_close_protect_ticks", 2)))
        except (TypeError, ValueError):
            return 2

    def _open_protect_ticks(self) -> int:
        """开仓保护滑点跳数。默认比平仓少，避免开仓追得太远。"""
        trade = self.cfg.get("trade") or {}
        try:
            return max(0, int(trade.get("ctp_open_protect_ticks", 1)))
        except (TypeError, ValueError):
            return 1

    def _ctp_quote_snapshot(self, symbol: str) -> dict[str, float]:
        """盘口：优先柜台行情，没有就用 1 分钟最新价，再没有用信号周期收盘。"""
        last = bid = ask = upper = lower = 0.0
        gateway = getattr(getattr(self, "trader", None), "gateway", None)
        md = getattr(gateway, "md", None) or getattr(getattr(self, "trader", None), "md", None)
        row = None
        if md is not None:
            latest_for = getattr(md, "latest_for", None)
            if callable(latest_for):
                row = latest_for(symbol)
            # 没有按合约取价的旧行情对象，才退回单一 latest；
            # 多合约订阅后不能拿别的合约的报价来顶替。
            if row is None and not getattr(md, "wanted_symbols", None):
                row = getattr(md, "latest", None)
        if isinstance(row, dict):
            last = float(row.get("last") or 0)
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            upper = float(row.get("upper") or 0)
            lower = float(row.get("lower") or 0)
        band = getattr(gateway, "_instrument", None) if gateway is not None else None
        if isinstance(band, dict):
            upper = upper or float(band.get("upper") or band.get("UpperLimitPrice") or 0)
            lower = lower or float(band.get("lower") or band.get("LowerLimitPrice") or 0)
        if last <= 0:
            try:
                last = float(self.kline.latest_price(symbol) or 0)
            except Exception:  # noqa: BLE001
                last = 0.0
        if last <= 0:
            try:
                period = str(getattr(self.strategy, "timeframe", "5") or "5")
                df = self.kline.fetch(symbol, period=period)
                if df is not None and len(df) > 0:
                    last = float(df["close"].iloc[-1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 {} 最新价失败: {}", symbol, exc)
        return {
            "last": last,
            "bid": bid,
            "ask": ask,
            "upper": upper,
            "lower": lower,
        }

    def _ctp_is_buy(self, decision: StrategyDecision) -> bool:
        """这一笔柜台方向是不是买入。平空、买开都是买。"""
        if decision.action == StrategyAction.BUY_OPEN:
            return True
        if decision.action == StrategyAction.SELL_OPEN:
            return False
        pos = self._position_for(decision.symbol)
        return str(getattr(pos, "side", "") or "") == "short"

    def _ctp_limit_price(self, decision: StrategyDecision) -> float | None:
        """
        CTP 限价按对手价挂，和同花顺价格框选「对手价」同一思路。

        止损价只用来判断要不要平，不能拿去当限价：空单止损在现价下方时，
        买平挂在止损价会永远不成交，撤了又挂。
        """
        quote = self._ctp_quote_snapshot(decision.symbol)
        last = float(quote.get("last") or 0)
        decision.meta = {
            **(decision.meta or {}),
            "latest_price": last,
            "latest_price_at": now_iso(),
            "latest_price_source": "ctp_opponent",
            "theory_price": (
                decision.entry_price
                if decision.action != StrategyAction.CLOSE
                else decision.stop_price
            ),
        }
        extra = self._close_protect_ticks() if decision.action == StrategyAction.CLOSE else self._open_protect_ticks()
        if decision.action == StrategyAction.CLOSE:
            chased = int((getattr(self, "_close_chase_ticks", {}) or {}).get(
                str(decision.symbol or "").strip().lower()
            ) or 0)
            extra = min(8, extra + max(0, chased))
        price = ctp_opponent_limit(
            is_buy=self._ctp_is_buy(decision),
            last=last,
            bid=float(quote.get("bid") or 0),
            ask=float(quote.get("ask") or 0),
            upper=float(quote.get("upper") or 0),
            lower=float(quote.get("lower") or 0),
            tick=self._price_tick_for(decision.symbol),
            extra_ticks=extra,
        )
        if price is not None:
            logger.info(
                "CTP {} 对手价限价 {}（买={} 最新={} 买一={} 卖一={} 让{}跳）",
                decision.symbol,
                price,
                self._ctp_is_buy(decision),
                last,
                quote.get("bid"),
                quote.get("ask"),
                extra,
            )
        return price

    def _ctp_open_slip_blocked(self, decision: StrategyDecision, order_price: float) -> bool:
        """开仓对手价比信号价追得太远就放弃这一根，平仓不拦。"""
        trade = (getattr(self, "cfg", None) or {}).get("trade") or {}
        try:
            limit_atr = float(trade.get("max_entry_slip_atr", 0.5) or 0)
        except (TypeError, ValueError):
            limit_atr = 0.5
        try:
            limit_points = float(trade.get("max_entry_slip_points", 0) or 0)
        except (TypeError, ValueError):
            limit_points = 0.0
        atr = 0.0
        try:
            bars = self.kline.fetch(decision.symbol, period=str(self.strategy.timeframe))
            if bars is not None and len(bars) > 0 and hasattr(self.strategy, "_atr"):
                got = self.strategy._atr(bars)
                atr = float(got or 0)
        except Exception:  # noqa: BLE001
            atr = 0.0
        blocked, chase, limit = entry_slip_blocked(
            float(decision.entry_price or 0),
            float(order_price or 0),
            atr,
            is_long=decision.action == StrategyAction.BUY_OPEN,
            limit_atr=limit_atr,
            limit_points=limit_points,
        )
        if blocked:
            logger.warning(
                "{} 对手价滑点过大，放弃开仓 | 信号价 {:.2f} 报单价 {:.2f} 追 {:.2f} 点 > 上限 {:.2f}",
                decision.symbol,
                float(decision.entry_price or 0),
                float(order_price or 0),
                chase,
                limit,
            )
            self._record_event("slip_block_open", symbol=decision.symbol, reason="对手价滑点过大")
        return blocked

