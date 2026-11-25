"""组合回测：把多个单品种回测按资金权重合成一条组合权益。

功能：权重分配（等权 / 波动率倒数 / 风险预算 / 固定上限）、按交易日对齐权益、
      汇总保证金峰值、组合回撤、品种贡献、相关性矩阵、同时持仓数。
原理：只复用已跑完的单品种结果，按权重缩放后相加；不重跑撮合、不改单品种结论。
      所有函数只读，不写配置、不碰交易链路。
"""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from loguru import logger

from src.backtest.metrics import MARGIN_RATE, calc_performance
from src.backtest.risk_metrics import format_risk_lines, risk_summary

WEIGHT_MODES = ("equal", "inverse_vol", "risk_budget", "fixed")
MODE_LABELS = {
    "equal": "等权",
    "inverse_vol": "波动率倒数",
    "risk_budget": "风险预算",
    "fixed": "固定上限",
}
# 波动率至少要有这么多个交易日的收益才算得出来；不足回退等权。
MIN_VOL_DAYS = 3
# 日收益相关性的分档：≥0.7 当同一风险来源，0.3–0.7 可持有但别都加满，<0.3 分散有效。
CORR_HIGH = 0.7
CORR_MID = 0.3


def _resolve_engine(value: Any, fallback: str) -> str:
    """把策略腿里的引擎名收成正式名；认不出时退回全局引擎，不静默换成别的策略。"""
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        from src.strategy.factory import normalize_engine

        name = normalize_engine(raw)
    except Exception:  # noqa: BLE001
        name = ""
    if name:
        return name
    logger.warning("策略腿的引擎 {} 认不出，改用 {}", raw, fallback or "全局默认")
    return fallback


def parse_legs(cfg: dict[str, Any] | None) -> list[dict[str, str]]:
    """读「策略腿」：每条腿 = 合约 + 引擎 + 信号周期（可选止损周期）。

    strategy.legs 没写时退回旧口径：关注品种各一条腿，共用 strategy.engine/timeframe。
    同一合约只保留第一条腿 —— 现有持仓是每合约一个方向净仓，同品种多腿会互相覆盖止损。
    """
    st = _box((cfg or {}).get("strategy"))
    base_engine = str(st.get("engine") or "").strip()
    base_tf = str(st.get("timeframe") or "").strip()
    base_trail = str(st.get("trail_timeframe") or "").strip()
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(symbol: Any, engine: Any, timeframe: Any, trail: Any) -> None:
        text = str(symbol or "").strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            logger.warning("策略腿 {} 重复，只保留第一条", text)
            return
        seen.add(key)
        raw_tf = str(timeframe or "").strip()
        tf = raw_tf or base_tf
        raw_trail = str(trail or "").strip()
        if raw_trail:
            trail_tf = raw_trail
        elif raw_tf:
            # 只写了一个周期时，信号和止损用同一档。
            trail_tf = raw_tf
        else:
            trail_tf = base_trail
        out.append(
            {
                "symbol": text,
                "engine": _resolve_engine(engine, base_engine),
                "timeframe": tf,
                "trail_timeframe": trail_tf,
            }
        )

    raw = st.get("legs")
    if isinstance(raw, list):
        for item in raw:
            box = _box(item)
            _add(box.get("symbol"), box.get("engine"), box.get("timeframe"), box.get("trail_timeframe"))
    if not out:
        for symbol in st.get("symbols") or []:
            _add(symbol, base_engine, base_tf, base_trail)
    return out


def _box(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _day_key(value: Any) -> str:
    """时间戳取日期部分。认不出日期就返回空串，调用方据此跳过这一腿。"""
    text = str(value or "").strip().replace("T", " ")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _symbol_value(mapping: dict[str, Any] | None, symbol: str) -> float | None:
    """按「合约 → 品种代码 → 小写」顺序取值；没配就返回 None，交给调用方兜底。"""
    if not mapping or not symbol:
        return None
    from src.risk.universe_table import product_key

    keys = [symbol, str(symbol).lower(), product_key(symbol)]
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        for candidate in (key, str(key).lower()):
            if candidate in mapping:
                return _number(mapping[candidate])
            if candidate in lowered:
                return _number(lowered[candidate])
    return None


def _daily_curve(result: Any) -> list[tuple[str, float]]:
    """把一次回测的权益曲线压成「交易日 → 当日最后一个权益」。"""
    equity = list(getattr(result, "equity", None) or [])
    times = list(getattr(result, "equity_times", None) or [])
    if not equity:
        return []
    if not times:
        return []
    if len(times) < len(equity):
        times = times + [times[-1]] * (len(equity) - len(times))
    curve: dict[str, float] = {}
    order: list[str] = []
    for value, stamp in zip(equity, times):
        day = _day_key(stamp)
        if not day:
            continue
        if day not in curve:
            order.append(day)
        curve[day] = _number(value)
    return [(day, curve[day]) for day in order]


def _daily_returns(curve: list[tuple[str, float]]) -> dict[str, float]:
    """日收益：当日权益 ÷ 上一日权益 − 1。首日没有收益。"""
    out: dict[str, float] = {}
    prev: float | None = None
    for day, value in curve:
        if prev is not None and abs(prev) > 1e-12:
            out[day] = value / prev - 1.0
        prev = value
    return out


def _volatility(curve: list[tuple[str, float]]) -> float | None:
    """日收益的总体标准差。样本不足或完全不动时返回 None，由调用方回退等权。"""
    rets = list(_daily_returns(curve).values())
    if len(rets) < MIN_VOL_DAYS:
        return None
    mean = sum(rets) / len(rets)
    var = sum((item - mean) ** 2 for item in rets) / len(rets)
    vol = math.sqrt(var)
    return vol if vol > 1e-12 else None


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 1e-12:
        return weights
    return {key: value / total for key, value in weights.items()}


def allocate_weights(
    legs: Iterable[Any],
    mode: str = "equal",
    *,
    caps: dict[str, float] | None = None,
    risk_budget: dict[str, float] | None = None,
) -> dict[str, Any]:
    """按模式给各腿分配资金权重。

    返回 {"weights": {symbol: w}, "mode": str, "note": str}。
    权重和可能小于 1（固定上限留出闲钱），差额在合并时按现金计入。
    """
    legs = list(legs)
    symbols = [str(getattr(leg, "symbol", "") or "") for leg in legs]
    symbols = [item for item in symbols if item]
    mode = str(mode or "equal").strip().lower()
    if mode not in WEIGHT_MODES:
        mode = "equal"
    note = ""
    if not symbols:
        return {"weights": {}, "mode": mode, "note": "没有可用腿，权重为空"}

    if mode == "equal":
        return {"weights": {symbol: 1.0 / len(symbols) for symbol in symbols}, "mode": mode, "note": note}

    curves = {str(getattr(leg, "symbol", "")): _daily_curve(leg.result) for leg in legs}
    vols = {symbol: _volatility(curves.get(symbol) or []) for symbol in symbols}

    if mode in ("inverse_vol", "risk_budget"):
        missing = [symbol for symbol in symbols if vols.get(symbol) is None]
        if missing:
            note = f"{'、'.join(missing)} 波动率样本不足，已回退等权"
            return {"weights": {symbol: 1.0 / len(symbols) for symbol in symbols}, "mode": "equal", "note": note}
        shares = {symbol: 1.0 for symbol in symbols}
        if mode == "risk_budget":
            raw = {symbol: _number((risk_budget or {}).get(symbol)) for symbol in symbols}
            if all(value > 0 for value in raw.values()) and abs(sum(raw.values()) - 1.0) <= 0.5:
                shares = raw
            else:
                note = "风险预算份额缺失或合计偏离 1，按等份额处理"
        weights = {symbol: shares[symbol] / float(vols[symbol] or 0.0) for symbol in symbols}
        return {"weights": _normalize(weights), "mode": mode, "note": note}

    # fixed：先等分，再按上限截断；剩下的闲钱留在组合现金里。
    raw_caps = {symbol: _number((caps or {}).get(symbol)) for symbol in symbols}
    equal = 1.0 / len(symbols)
    if not any(0.0 < value < 1.0 for value in raw_caps.values()):
        note = "固定上限未配置或无效，按等权处理"
    limits = {
        symbol: (raw_caps[symbol] if 0.0 < raw_caps[symbol] < 1.0 else equal)
        for symbol in symbols
    }
    weights: dict[str, float] = dict.fromkeys(symbols, 0.0)
    remaining = 1.0
    open_symbols = list(symbols)
    # 水位法：均分后超过上限的腿按上限锁住，剩余资金再在其余腿之间均分。
    while open_symbols:
        even = remaining / len(open_symbols)
        capped = [symbol for symbol in open_symbols if limits[symbol] <= even + 1e-12]
        if not capped:
            for symbol in open_symbols:
                weights[symbol] = even
            break
        for symbol in capped:
            weights[symbol] = limits[symbol]
            remaining -= limits[symbol]
            open_symbols.remove(symbol)
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        note = f"固定上限合计 {sum(weights.values()):.2%}，其余留作现金"
    if not weights or sum(weights.values()) <= 1e-12:
        return {"weights": {symbol: equal for symbol in symbols}, "mode": "equal", "note": "上限配置无效，回退等权"}
    return {"weights": weights, "mode": mode, "note": note}


def _pearson(left: dict[str, float], right: dict[str, float]) -> float:
    """两列日收益的相关系数。共同样本不足或任一列不动时返回 0，不产生 NaN。"""
    days = sorted(set(left) & set(right))
    if len(days) < MIN_VOL_DAYS:
        return 0.0
    xs = [left[day] for day in days]
    ys = [right[day] for day in days]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 1e-18 or vy <= 1e-18:
        return 0.0
    return cov / math.sqrt(vx * vy)


def _parse_day(value: Any) -> datetime | None:
    day = _day_key(value)
    if not day:
        return None
    try:
        return datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None


def _open_margin(
    result: Any,
    symbol: str,
    day: datetime,
    *,
    margin_rate: float | None,
) -> tuple[float, bool]:
    """这一腿在某一天占用的保证金合计，以及当天是否有仓。

    只按「保证金比例 × 开仓价 × 乘数」算；没填比例的品种按 10% 估算并会在结果里点名。
    不再使用任何每手保证金固定金额。
    """
    total = 0.0
    holding = False
    for trade in list(getattr(result, "trades", None) or []):
        start = _parse_day(getattr(trade, "entry_time", ""))
        end = _parse_day(getattr(trade, "exit_time", ""))
        if start is None:
            continue
        if day < start or (end is not None and day > end):
            continue
        holding = True
        volume = _number(getattr(trade, "volume", 0))
        if volume <= 0:
            continue
        price = _number(getattr(trade, "entry_price", 0))
        mult = _number(getattr(trade, "multiplier", 0))
        if margin_rate and margin_rate > 0 and price > 0 and mult > 0:
            total += price * mult * volume * margin_rate
        elif price > 0 and mult > 0:
            total += price * mult * volume * MARGIN_RATE
    return total, holding


def _trade_margin(
    trade: Any,
    *,
    margin_rate: float | None,
) -> float:
    """一笔成交开仓时占用的保证金。口径与 _open_margin 保持一致。"""
    volume = _number(getattr(trade, "volume", 0))
    if volume <= 0:
        return 0.0
    price = _number(getattr(trade, "entry_price", 0))
    mult = _number(getattr(trade, "multiplier", 0))
    if margin_rate and margin_rate > 0 and price > 0 and mult > 0:
        return price * mult * volume * margin_rate
    if price > 0 and mult > 0:
        return price * mult * volume * MARGIN_RATE
    return 0.0


def simulate_shared_capital(
    legs: Iterable[Any],
    *,
    initial_capital: float,
    margin_rates: dict[str, float] | None = None,
    max_margin_usage: float | None = None,
    daily_loss_limit: float | None = None,
    risk_pct: float = 0.01,
    slippage_by_symbol: dict[str, float] | None = None,
    allowed_slip_ticks: float = 2.0,
    max_heat_pct: float | None = None,
) -> dict[str, Any]:
    """共享资金账：所有腿抢同一个保证金池，开不起就记「拒开」。

    这是对既有「各腿独立跑满资金、再按权重缩放」的补充，不改原组合权益。
    保证金占用按开仓价 × 乘数 × 比例逐笔累加；平仓日当天仍占用，次一交易日释放。

    同一天多腿同时触发时按第 4.3 节的顺序重放：
    风险预算占用率（高→低）→ 资金效率（高→低）→ 相关性惩罚（高相关排后）
    → 滑点余量（预计滑点 ÷ 允许滑点，大的先做）→「交易日 + 品种」稳定哈希。
    日亏达到 daily_loss_limit 后，当天后续开仓全部拒绝（对应 D6 只做日亏熔断）。
    配置 max_heat_pct 后，已持仓 + 本笔的预计风险不得超过该比例权益，超了拒开。
    """
    capital = _number(initial_capital)
    rates = dict(margin_rates or {})
    usage = 100.0 if max_margin_usage is None else _number(max_margin_usage)
    if usage <= 0:
        usage = 100.0
    budget = capital * usage / 100.0
    loss_limit = _number(daily_loss_limit)
    risk_rate = max(0.0, _number(risk_pct))
    risk_budget = capital * risk_rate
    heat_rate = max(0.0, _number(max_heat_pct))
    heat_budget = capital * heat_rate / 100.0
    slip_allow = max(1e-9, _number(allowed_slip_ticks))
    returns_by_symbol: dict[str, dict[str, float]] = {}

    items: list[dict[str, Any]] = []
    for leg in legs:
        symbol = str(getattr(leg, "symbol", "") or "")
        result = getattr(leg, "result", None)
        returns_by_symbol[symbol] = _daily_returns(_daily_curve(result))
        for trade in list(getattr(result, "trades", None) or []):
            entry = _parse_day(getattr(trade, "entry_time", ""))
            if entry is None:
                continue
            exit_day = _parse_day(getattr(trade, "exit_time", "")) or entry
            margin = _trade_margin(
                trade,
                margin_rate=rates.get(symbol),
            )
            pnl = _number(getattr(trade, "pnl", 0))
            # 预计亏损优先用 1 倍 ATR 的止损距离；没有 ATR 时退回「保证金 × 风险比例」。
            atr = _number(getattr(trade, "atr", 0))
            multiplier = _number(getattr(trade, "multiplier", 0))
            volume = _number(getattr(trade, "volume", 0))
            if atr > 0 and multiplier > 0 and volume > 0:
                projected_risk = atr * multiplier * volume
            else:
                projected_risk = margin * risk_rate
            slip_ticks = _symbol_value(slippage_by_symbol, symbol)
            items.append(
                {
                    "id": len(items),
                    "symbol": symbol,
                    "entry": entry,
                    "exit": exit_day,
                    "margin": margin,
                    "pnl": pnl,
                    "efficiency": (pnl / margin) if margin > 0 else 0.0,
                    "risk_usage": (projected_risk / risk_budget) if risk_budget > 0 else 0.0,
                    "risk": projected_risk,
                    "slip_ratio": (slip_ticks / slip_allow) if slip_ticks is not None else 0.0,
                    "rank": int(
                        hashlib.md5(
                            f"{entry.strftime('%Y-%m-%d')}|{symbol}".encode()
                        ).hexdigest()[:8],
                        16,
                    ),
                }
            )

    if not items:
        return {
            "initial_capital": capital,
            "margin_budget": budget,
            "margin_usage_pct": usage,
            "trades_total": 0,
            "trades_accepted": 0,
            "trades_rejected": 0,
            "accepted_pnl": 0.0,
            "rejected_pnl": 0.0,
            "unconstrained_pnl": 0.0,
            "pnl_gap": 0.0,
            "final_equity": capital,
            "unconstrained_equity": capital,
            "margin_peak": 0.0,
            "margin_peak_pct": 0.0,
            "heat_budget": heat_budget,
            "heat_peak": 0.0,
            "heat_peak_pct": 0.0,
            "blocked_days": [],
            "rejected": [],
            "by_symbol": {},
            "priority": "风险预算占用率 → 资金效率 → 相关性惩罚 → 滑点余量 → 稳定哈希",
            "risk_per_trade_pct": round(risk_rate * 100.0, 4),
            "allowed_slip_ticks": slip_allow,
        }

    days = sorted({item["entry"] for item in items} | {item["exit"] for item in items})
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for item in items:
        by_day.setdefault(item["entry"], []).append(item)

    def _corr_penalty(item: dict[str, Any], open_symbols: set[str]) -> int:
        """与已持仓品种日收益相关 ≥0.7 时排后；没有已持仓则不罚。"""
        if not open_symbols:
            return 0
        own = returns_by_symbol.get(item["symbol"]) or {}
        best = 0.0
        for symbol in open_symbols:
            if symbol == item["symbol"]:
                return 1
            best = max(best, _pearson(own, returns_by_symbol.get(symbol) or {}))
        return 1 if best >= CORR_HIGH else 0

    open_items: list[dict[str, Any]] = []
    accepted: set[int] = set()
    rejected: list[dict[str, Any]] = []
    realized = 0.0
    peak = 0.0
    heat_peak = 0.0
    blocked_days: list[str] = []
    equity: list[float] = []
    equity_days: list[str] = []

    for day in days:
        # 昨天及以前已平仓的，今天释放保证金。
        open_items = [item for item in open_items if item["exit"] >= day]
        used = sum(item["margin"] for item in open_items)
        used_heat = sum(_number(item.get("risk")) for item in open_items)
        day_realized = sum(
            item["pnl"] for item in items if item["id"] in accepted and item["exit"] == day
        )
        realized += day_realized
        blocked = loss_limit > 0 and day_realized <= -loss_limit
        if blocked:
            blocked_days.append(day.strftime("%Y-%m-%d"))
        open_symbols = {item["symbol"] for item in open_items}
        pending = list(by_day.get(day, []))
        # 每接受一笔就更新已持仓，再给剩下的候选重新排一次，相关性惩罚才跟得上。
        while pending:
            pending.sort(
                key=lambda item: (
                    -item["risk_usage"],
                    -item["efficiency"],
                    _corr_penalty(item, open_symbols),
                    -item["slip_ratio"],
                    item["rank"],
                )
            )
            item = pending.pop(0)
            if blocked:
                rejected.append({**item, "reason": "日亏熔断"})
                continue
            if used + item["margin"] > budget + 1e-9:
                rejected.append(
                    {
                        **item,
                        "reason": "保证金预算不足",
                        "available": max(0.0, budget - used),
                    }
                )
                continue
            if heat_budget > 0 and used_heat + _number(item.get("risk")) > heat_budget + 1e-9:
                rejected.append(
                    {
                        **item,
                        "reason": "组合风险热度超限",
                        "heat_available": max(0.0, heat_budget - used_heat),
                    }
                )
                continue
            accepted.add(item["id"])
            open_items.append(item)
            open_symbols.add(item["symbol"])
            used += item["margin"]
            used_heat += _number(item.get("risk"))
        peak = max(peak, used)
        heat_peak = max(heat_peak, used_heat)
        equity.append(capital + realized)
        equity_days.append(day.strftime("%Y-%m-%d"))

    accepted_items = [item for item in items if item["id"] in accepted]
    accepted_pnl = sum(item["pnl"] for item in accepted_items)
    rejected_pnl = sum(item["pnl"] for item in rejected)
    total_pnl = sum(item["pnl"] for item in items)
    by_symbol: dict[str, dict[str, Any]] = {}
    for item in items:
        bucket = by_symbol.setdefault(
            item["symbol"],
            {"accepted": 0, "rejected": 0, "accepted_pnl": 0.0, "rejected_pnl": 0.0},
        )
        if item["id"] in accepted:
            bucket["accepted"] += 1
            bucket["accepted_pnl"] += item["pnl"]
        else:
            bucket["rejected"] += 1
            bucket["rejected_pnl"] += item["pnl"]
    for bucket in by_symbol.values():
        bucket["accepted_pnl"] = round(bucket["accepted_pnl"], 2)
        bucket["rejected_pnl"] = round(bucket["rejected_pnl"], 2)

    preview = []
    for item in rejected[:30]:
        preview.append(
            {
                "day": item["entry"].strftime("%Y-%m-%d"),
                "symbol": item["symbol"],
                "required_margin": round(item["margin"], 2),
                "available": round(_number(item.get("available")), 2),
                "risk_usage": round(item["risk_usage"], 4),
                "risk_cash": round(_number(item.get("risk")), 2),
                "slip_ratio": round(item["slip_ratio"], 4),
                "reason": item["reason"],
            }
        )
    return {
        "initial_capital": capital,
        "margin_budget": budget,
        "margin_usage_pct": usage,
        "trades_total": len(items),
        "trades_accepted": len(accepted_items),
        "trades_rejected": len(rejected),
        "accepted_pnl": round(accepted_pnl, 2),
        "rejected_pnl": round(rejected_pnl, 2),
        "unconstrained_pnl": round(total_pnl, 2),
        "pnl_gap": round(total_pnl - accepted_pnl, 2),
        "final_equity": round(capital + accepted_pnl, 2),
        "unconstrained_equity": round(capital + total_pnl, 2),
        "margin_peak": round(peak, 2),
        "margin_peak_pct": (peak / capital) if capital > 1e-9 else 0.0,
        "heat_budget": round(heat_budget, 2),
        "heat_peak": round(heat_peak, 2),
        "heat_peak_pct": (heat_peak / capital) if capital > 1e-9 else 0.0,
        "equity": equity,
        "equity_times": equity_days,
        "blocked_days": blocked_days,
        "rejected": preview,
        "by_symbol": by_symbol,
        "priority": "风险预算占用率 → 资金效率 → 相关性惩罚 → 滑点余量 → 稳定哈希",
        "risk_per_trade_pct": round(risk_rate * 100.0, 4),
        "allowed_slip_ticks": slip_allow,
    }


def combine_results(
    legs: Iterable[Any],
    *,
    initial_capital: float,
    weights: dict[str, float] | None = None,
    mode: str = "equal",
    margin_rates: dict[str, float] | None = None,
    max_margin_usage: float | None = None,
    daily_loss_limit: float | None = None,
    risk_pct: float = 0.01,
    slippage_by_symbol: dict[str, float] | None = None,
    allowed_slip_ticks: float = 2.0,
    max_heat_pct: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    """按权重合并多条单品种权益，输出组合层面的汇总。

    legs 需带 symbol / result / weight（weight 缺省时读 weights 字典）。
    组合权益 = Σ 分配资金 × 该腿当日权益 ÷ 该腿起始权益；未分配的部分按现金持有。
    """
    legs = [leg for leg in legs if str(getattr(leg, "symbol", "") or "")]
    capital = _number(initial_capital)
    result: dict[str, Any] = {
        "symbols": [],
        "weights": {},
        "mode": mode,
        "initial_capital": capital,
        "equity": [],
        "equity_times": [],
        "performance": None,
        "start": "",
        "end": "",
        "margin_peak": 0.0,
        "margin_peak_pct": 0.0,
        "margin_source": "",
        "max_concurrent": 0,
        "avg_concurrent": 0.0,
        "contributions": {},
        "correlation": {},
        "cash": 0.0,
        "skipped": [],
        "notes": [],
        "max_margin_usage": max_margin_usage,
        "margin_ok": None,
        "margin_stress_peak": 0.0,
        "margin_stress_pct": 0.0,
        "margin_stress_ok": None,
        "risk": None,
    }
    notes: list[str] = [note] if note else []
    notes.append("各腿按交易日对齐，缺失日按上一交易日值前向填充")
    curves: dict[str, list[tuple[str, float]]] = {}
    kept: list[Any] = []
    for leg in legs:
        symbol = str(getattr(leg, "symbol", "") or "")
        curve = _daily_curve(getattr(leg, "result", None))
        if not curve:
            result["skipped"].append({"symbol": symbol, "reason": "权益曲线缺时间戳或为空"})
            continue
        curves[symbol] = curve
        kept.append(leg)
    if not kept:
        result["notes"] = notes + ["没有可用腿"]
        return result

    weight_of = {str(getattr(leg, "symbol", "")): _number(getattr(leg, "weight", 0.0)) for leg in kept}
    if weights:
        weight_of = {symbol: _number(weights.get(symbol, weight_of.get(symbol, 0.0))) for symbol in weight_of}
    allocated = sum(weight_of.values())
    cash = max(0.0, 1.0 - allocated) * capital
    if cash > 1e-9:
        notes.append(f"未分配资金 {cash:,.2f} 按现金计入组合")

    days: list[str] = sorted({day for curve in curves.values() for day, _ in curve})
    if not days:
        result["notes"] = notes + ["没有可用交易日"]
        return result

    # 每腿按日期前向填充；首日之前用该腿起始权益。
    mapping_of = {symbol: dict(curve) for symbol, curve in curves.items()}
    result_of = {str(getattr(leg, "symbol", "")): leg.result for leg in kept}
    last_value: dict[str, float] = {}
    portfolio_equity: list[float] = []
    for day in days:
        total = cash
        for symbol, curve in curves.items():
            mapping = mapping_of[symbol]
            if day in mapping:
                last_value[symbol] = mapping[day]
            if symbol not in last_value:
                last_value[symbol] = curve[0][1]
            base = curve[0][1]
            if abs(base) <= 1e-12:
                continue
            total += weight_of.get(symbol, 0.0) * capital * (last_value[symbol] / base)
        portfolio_equity.append(total)

    # 保证金与同时持仓。
    rates = dict(margin_rates or {})
    by_rate_symbols = [symbol for symbol in curves if symbol in rates]
    estimated = [symbol for symbol in curves if symbol not in rates]
    parts: list[str] = []
    if by_rate_symbols:
        parts.append("配置保证金比例×开仓价×乘数")
    if estimated:
        parts.append("10% 估算（" + "、".join(estimated) + "）")
    margin_source = "；".join(parts) if parts else "没有保证金数据"
    margin_series: list[float] = []
    concurrent: list[int] = []
    for day in days:
        stamp = _parse_day(day)
        if stamp is None:
            margin_series.append(0.0)
            concurrent.append(0)
            continue
        day_margin = 0.0
        holding = 0
        for symbol in curves:
            amount, has_position = _open_margin(
                result_of[symbol],
                symbol,
                stamp,
                margin_rate=rates.get(symbol),
            )
            day_margin += amount * weight_of.get(symbol, 0.0)
            if has_position:
                holding += 1
        margin_series.append(day_margin)
        concurrent.append(holding)

    # 组合层面的成交盈亏要按权重缩放，否则和已缩放的权益曲线对不上。
    # 权重为 0 的腿不参与组合，成交也不计入，避免 0 盈亏成交拉低胜率。
    trades: list[Any] = []
    for leg in kept:
        symbol = str(getattr(leg, "symbol", ""))
        weight = weight_of.get(symbol, 0.0)
        if weight <= 0:
            continue
        for item in list(getattr(leg.result, "trades", None) or []):
            scaled = copy.copy(item)
            scaled.pnl = _number(getattr(item, "pnl", 0)) * weight
            trades.append(scaled)
    # 按开仓时间排序，保证汇总绩效与输入顺序无关。
    trades.sort(key=lambda item: (str(getattr(item, "entry_time", "")), str(getattr(item, "symbol", ""))))
    performance = calc_performance(trades, portfolio_equity, times=days)

    contributions: dict[str, dict[str, float]] = {}
    total_profit = 0.0
    for symbol, curve in curves.items():
        base = curve[0][1]
        end_value = last_value.get(symbol, base)
        weight = weight_of.get(symbol, 0.0)
        profit = weight * capital * (end_value / base - 1.0) if abs(base) > 1e-12 else 0.0
        total_profit += profit
        leg_trades = next((leg.result for leg in kept if str(getattr(leg, "symbol", "")) == symbol), None)
        net_pnl = sum(_number(getattr(t, "pnl", 0)) for t in list(getattr(leg_trades, "trades", None) or []))
        contributions[symbol] = {
            "net_pnl": net_pnl * weight,
            "profit": profit,
            "weight": weight,
        }
    for symbol, item in contributions.items():
        item["profit_share"] = (item["profit"] / total_profit) if abs(total_profit) > 1e-9 else 0.0

    returns = {symbol: _daily_returns(curve) for symbol, curve in curves.items()}
    correlation = {
        left: {right: (1.0 if left == right else _pearson(returns[left], returns[right])) for right in curves}
        for left in curves
    }
    # 相关性分档：同向越高越像同一笔风险；负相关反而能对冲，算分散有效。
    correlation_pairs: list[dict[str, Any]] = []
    if len(curves) >= 2:
        names = list(curves)
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                value = _number(correlation[left].get(right))
                if value >= CORR_HIGH:
                    level, hint = "高", "同向，按同一风险来源控制总仓位"
                elif value >= CORR_MID:
                    level, hint = "中", "可同时持有，但两条腿别都加到满仓"
                elif value <= -CORR_MID:
                    level, hint = "反向", "能对冲，分散有效"
                else:
                    level, hint = "低", "分散有效"
                correlation_pairs.append(
                    {"a": left, "b": right, "value": value, "level": level, "hint": hint}
                )
        high = [
            f"{item['a']}×{item['b']} {item['value']:+.2f}"
            for item in correlation_pairs
            if item["level"] == "高"
        ]
        if high:
            notes.append(f"{'、'.join(high)} 日收益相关性偏高，按同一风险来源控制总仓位")

    peak_margin = max(margin_series) if margin_series else 0.0
    result.update(
        {
            "symbols": list(curves),
            "weights": {symbol: weight_of.get(symbol, 0.0) for symbol in curves},
            "equity": portfolio_equity,
            "equity_times": days,
            "performance": performance,
            "start": days[0],
            "end": days[-1],
            "margin_peak": peak_margin,
            "margin_peak_pct": (peak_margin / capital) if capital > 1e-9 else 0.0,
            "margin_source": margin_source,
            "max_concurrent": max(concurrent) if concurrent else 0,
            "avg_concurrent": (sum(concurrent) / len(concurrent)) if concurrent else 0.0,
            "contributions": contributions,
            "correlation": correlation,
            "correlation_pairs": correlation_pairs,
            "cash": cash,
            "notes": notes,
        }
    )
    if max_margin_usage is not None and max_margin_usage > 0:
        # 配置里 margin_usage_pct 是百分数（80 表示 80%），这里换算后再比。
        result["margin_ok"] = (result["margin_peak_pct"] * 100.0) <= float(max_margin_usage) + 1e-9
    # 压力：交易所把保证金比例上调 50% 时，同样的持仓需要多少资金。
    stress_peak = peak_margin * 1.5
    result["margin_stress_peak"] = stress_peak
    result["margin_stress_pct"] = (stress_peak / capital) if capital > 1e-9 else 0.0
    if max_margin_usage is not None and max_margin_usage > 0:
        result["margin_stress_ok"] = (result["margin_stress_pct"] * 100.0) <= float(max_margin_usage) + 1e-9
    # 共享资金账：所有腿抢同一个保证金池，开不起就拒开。只做附加诊断，不改上面的组合权益。
    result["shared_capital"] = simulate_shared_capital(
        kept,
        initial_capital=capital,
        margin_rates=rates,
        max_margin_usage=max_margin_usage if max_margin_usage is not None else 100.0,
        daily_loss_limit=daily_loss_limit,
        risk_pct=risk_pct,
        slippage_by_symbol=slippage_by_symbol,
        allowed_slip_ticks=allowed_slip_ticks,
        max_heat_pct=max_heat_pct,
    )
    result["risk"] = risk_summary(portfolio_equity, days, correlation)
    return result


@dataclass
class PortfolioLeg:
    """一条腿：单品种结果 + 分配权重。"""

    symbol: str
    result: Any
    weight: float = 0.0
    skipped: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _margin_inputs(
    cfg: dict[str, Any] | None, symbols: list[str]
) -> dict[str, float]:
    """从配置读保证金比例（小数，如 0.2274）。

    这是唯一口径：占用 = 开仓价 × 每手数量 × 比例。
    没填比例的品种合并时按 10% 估算并在结果里点名。
    """
    from src.account.slippage_log import product_of

    contracts = _box((cfg or {}).get("risk")).get("contracts") or {}
    by_rate: dict[str, float] = {}
    for symbol in symbols:
        row = _box(contracts.get(product_of(symbol)))
        rate = _number(row.get("margin_rate_pct"))
        if rate > 0:
            by_rate[symbol] = rate / 100.0
    return by_rate


def run_portfolio_backtest(
    cfg: dict[str, Any],
    symbols: Iterable[str] | None = None,
    *,
    legs: Iterable[Any] | None = None,
    engine: str | None = None,
    mode: str = "equal",
    caps: dict[str, float] | None = None,
    risk_budget: dict[str, float] | None = None,
    initial_capital: float | None = None,
    runner: Callable[..., Any] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """逐腿跑单品种回测，再按权重合成组合结果。

    symbols：各腿都用全局 engine / 配置周期；legs：逐腿指定合约、引擎、周期（优先于 symbols）。
    每腿都用同一份初始资金跑（不预先切分），合并时再按权重缩放；
    这样单腿数值与历史回测页完全一致，组合权益也能逐日复算。
    """
    if runner is None:
        from src.backtest.engine import run_backtest
        from src.backtest.rollover import run_backtest_rollover

        backtest_cfg = _box((cfg or {}).get("backtest"))
        use_rollover = bool(backtest_cfg.get("rollover_main", True))
        chain_capital = bool(backtest_cfg.get("chain_capital", False))

        def runner(prepared: dict[str, Any], **call: Any) -> Any:
            call.setdefault("chain_capital", chain_capital)
            if use_rollover:
                return run_backtest_rollover(prepared, **call)
            return run_backtest(prepared, **call)

    capital = _number(initial_capital) if initial_capital is not None else _number(
        _box((cfg or {}).get("risk")).get("capital")
    )
    if capital <= 0:
        capital = 100_000.0

    skipped: list[dict[str, str]] = []
    wanted: list[dict[str, str]] = []
    if legs is not None:
        for item in legs:
            box = _box(item)
            symbol = str(box.get("symbol") or "").strip()
            if not symbol:
                continue
            wanted.append(
                {
                    "symbol": symbol,
                    "engine": _resolve_engine(box.get("engine"), str(engine or "")),
                    "timeframe": str(box.get("timeframe") or "").strip(),
                    "trail_timeframe": str(box.get("trail_timeframe") or "").strip(),
                }
            )
    else:
        for item in symbols or []:
            symbol = str(item).strip()
            if not symbol:
                continue
            wanted.append(
                {"symbol": symbol, "engine": str(engine or ""), "timeframe": "", "trail_timeframe": ""}
            )
    # 同一合约只跑一条腿：持仓是每合约一个方向净仓，两条腿会互相覆盖止损。
    unique: list[dict[str, str]] = []
    seen_symbols: set[str] = set()
    for item in wanted:
        key = item["symbol"].lower()
        if key in seen_symbols:
            skipped.append({"symbol": item["symbol"], "reason": "同一合约重复，只保留第一条腿"})
            continue
        seen_symbols.add(key)
        unique.append(item)
    wanted = unique

    legs_out: list[PortfolioLeg] = []
    total = len(wanted)
    for index, item in enumerate(wanted, start=1):
        symbol = item["symbol"]
        if cancel is not None and cancel():
            skipped.append({"symbol": symbol, "reason": "已取消"})
            continue
        if on_progress is not None:
            on_progress(index, total, symbol)
        call: dict[str, Any] = {"symbol": symbol, "initial_capital": capital}
        if item.get("engine"):
            call["engine"] = item["engine"]
        elif engine:
            call["engine"] = engine
        if item.get("timeframe"):
            call["timeframe"] = item["timeframe"]
        if item.get("trail_timeframe"):
            call["trail_timeframe"] = item["trail_timeframe"]
        try:
            one = runner(cfg, **call, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("组合回测单腿失败 {}: {}", symbol, exc)
            skipped.append({"symbol": symbol, "reason": str(exc) or exc.__class__.__name__})
            continue
        legs_out.append(
            PortfolioLeg(
                symbol=symbol,
                result=one,
                extra={"engine": item.get("engine", ""), "timeframe": item.get("timeframe", "")},
            )
        )
    legs = legs_out

    allocated = allocate_weights(legs, mode, caps=caps, risk_budget=risk_budget)
    weight_of = allocated.get("weights") or {}
    for leg in legs:
        leg.weight = _number(weight_of.get(leg.symbol))
    usage_value = _number(_box((cfg or {}).get("risk")).get("margin_usage_pct"))
    usage = usage_value if usage_value > 0 else None
    loss_value = _number(_box((cfg or {}).get("risk")).get("max_daily_loss"))
    risk_rate = _number(_box((cfg or {}).get("risk")).get("risk_per_trade_pct"))
    risk_pct = (risk_rate / 100.0) if risk_rate > 0 else 0.01
    backtest_box = _box((cfg or {}).get("backtest"))
    raw_slip = backtest_box.get("slippage_by_symbol")
    slip_map = raw_slip if isinstance(raw_slip, dict) else None
    allowed_slip = _number(backtest_box.get("allowed_slip_ticks")) or 2.0
    portfolio_box = _box(_box((cfg or {}).get("risk")).get("portfolio"))
    heat_value = _number(portfolio_box.get("max_heat_pct"))
    by_rate = _margin_inputs(cfg, [leg.symbol for leg in legs])
    combined = combine_results(
        legs,
        initial_capital=capital,
        weights=weight_of,
        mode=str(allocated.get("mode") or mode),
        margin_rates=by_rate,
        max_margin_usage=usage,
        daily_loss_limit=loss_value if loss_value > 0 else None,
        risk_pct=risk_pct,
        slippage_by_symbol=slip_map,
        allowed_slip_ticks=allowed_slip,
        max_heat_pct=heat_value if heat_value > 0 else None,
        note=str(allocated.get("note") or ""),
    )
    combined["skipped"] = skipped + list(combined.get("skipped") or [])
    combined["requested"] = [item["symbol"] for item in wanted]
    combined["engine"] = str(engine or "")
    combined["legs"] = [
        {
            "symbol": leg.symbol,
            "weight": leg.weight,
            "engine": str((leg.extra or {}).get("engine") or ""),
            "timeframe": str((leg.extra or {}).get("timeframe") or ""),
        }
        for leg in legs
    ]
    return combined


def _leg_text(item: dict[str, Any]) -> str:
    """一条腿的中文描述：策略中文名 · 周期 · 权重。认不出的部分跳过。"""
    from src.data.periods import canonical_period, period_label
    from src.strategy.factory import engine_label

    parts: list[str] = []
    engine = str(item.get("engine") or "")
    if engine:
        parts.append(engine_label(engine))
    tf = str(item.get("timeframe") or "")
    if tf:
        try:
            parts.append(period_label(canonical_period(tf)))
        except Exception:  # noqa: BLE001
            parts.append(tf)
    weight = _number(item.get("weight"))
    if weight > 0:
        parts.append(f"{weight:.0%}")
    return " · ".join(parts)


def format_portfolio_lines(portfolio: dict[str, Any]) -> list[str]:
    """把组合结果收成中文摘要行。只陈述事实，不给通过/不通过结论。"""
    if not portfolio or not portfolio.get("symbols"):
        reason = "、".join(
            f"{item.get('symbol', '?')}（{item.get('reason', '无结果')}）"
            for item in (portfolio or {}).get("skipped") or []
        )
        return [f"没有可用腿：{reason or '每个品种都没跑出结果'}"]
    perf = portfolio.get("performance")
    symbols = list(portfolio.get("symbols") or [])
    capital = _number(portfolio.get("initial_capital"))
    equity = list(portfolio.get("equity") or [])
    end_value = _number(equity[-1]) if equity else 0.0
    ret = (end_value / capital - 1.0) if capital > 1e-9 else 0.0
    mode = str(portfolio.get("mode") or "equal")
    label = MODE_LABELS.get(mode, mode)
    lines = [
        f"组合回测  {len(symbols)} 个品种 · {label} · 初始资金 {capital:,.2f} · "
        f"{portfolio.get('start') or '—'} → {portfolio.get('end') or '—'}（{len(equity)} 个交易日）",
        f"组合权益  期末 {end_value:,.2f}（{ret:+.2%}）"
        + (
            f"，最大回撤 {perf.max_drawdown:,.2f}（{perf.max_drawdown_pct:.2%}）"
            if perf is not None
            else ""
        ),
    ]
    described = [
        f"{item.get('symbol')} {_leg_text(item)}".strip()
        for item in portfolio.get("legs") or []
        if item.get("symbol")
    ]
    if described:
        lines.insert(1, "腿配置    " + " ｜ ".join(described))
    margin = _number(portfolio.get("margin_peak"))
    margin_pct = _number(portfolio.get("margin_peak_pct"))
    lines.append(
        f"保证金    峰值 {margin:,.2f}（占初始资金 {margin_pct:.1%}，{portfolio.get('margin_source') or '口径未知'}）"
        f"，同时持仓最多 {int(_number(portfolio.get('max_concurrent')))} 个品种"
    )
    stress_pct = _number(portfolio.get("margin_stress_pct"))
    if stress_pct > 0:
        stress_ok = portfolio.get("margin_stress_ok")
        verdict = "在上限内" if stress_ok else ("超出上限" if stress_ok is False else "上限未知")
        lines.append(f"压力     保证金比例上调 50% 后占用 {stress_pct:.1%}（{verdict}）")
    shared = portfolio.get("shared_capital") or {}
    total_trades = int(_number(shared.get("trades_total")))
    if total_trades > 0:
        accepted = int(_number(shared.get("trades_accepted")))
        rejected = int(_number(shared.get("trades_rejected")))
        peak = _number(shared.get("margin_peak"))
        peak_pct = _number(shared.get("margin_peak_pct"))
        gap = _number(shared.get("pnl_gap"))
        lines.append(
            f"共享资金  按保证金竞争重放 {total_trades} 笔：接受 {accepted} 笔、拒开 {rejected} 笔"
            f"，实际盈亏 {_number(shared.get('accepted_pnl')):+,.2f}"
            f"（不拒开 {_number(shared.get('unconstrained_pnl')):+,.2f}，差额 {gap:+,.2f}）"
            f"，保证金峰值 {peak:,.2f}（{peak_pct:.1%}）"
        )
        if shared.get("priority"):
            risk_pct = _number(shared.get("risk_per_trade_pct"))
            suffix = f"（单笔风险 {risk_pct:.2f}%）" if risk_pct > 0 else ""
            lines.append(f"优先级    {shared.get('priority')}{suffix}")
        heat_budget = _number(shared.get("heat_budget"))
        if heat_budget > 0:
            heat_peak = _number(shared.get("heat_peak"))
            heat_pct = _number(shared.get("heat_peak_pct"))
            lines.append(
                f"风险热度  峰值 {heat_peak:,.0f} 元（上限 {heat_budget:,.0f} 元，"
                f"占初始资金 {heat_pct:.1%}）"
            )
        blocked = list(shared.get("blocked_days") or [])
        if blocked:
            lines.append(f"日亏熔断  {len(blocked)} 个交易日停止开仓：{'、'.join(blocked[:5])}")
    lines.extend(format_risk_lines(portfolio.get("risk")))
    shares = []
    for symbol, item in (portfolio.get("contributions") or {}).items():
        shares.append(f"{symbol} {_number(item.get('profit')):+,.2f}")
    if shares:
        lines.append("品种贡献  " + " · ".join(shares))
    corr = portfolio.get("correlation") or {}
    graded = portfolio.get("correlation_pairs") or []
    if graded:
        pairs = [
            f"{item.get('a')}×{item.get('b')} {_number(item.get('value')):+.2f}（{item.get('level')}）"
            for item in graded
        ]
    else:
        pairs = []
        for i, left in enumerate(symbols):
            for right in symbols[i + 1 :]:
                value = _number((corr.get(left) or {}).get(right))
                pairs.append(f"{left}×{right} {value:+.2f}")
    if pairs:
        lines.append("相关性    " + " · ".join(pairs))
    weights = portfolio.get("weights") or {}
    if weights:
        lines.append(
            "权重      " + " · ".join(f"{symbol} {_number(weight):.0%}" for symbol, weight in weights.items())
        )
    notes = [str(item) for item in (portfolio.get("notes") or []) if str(item)]
    if notes:
        lines.append("口径      " + "；".join(notes))
    skipped = portfolio.get("skipped") or []
    if skipped:
        lines.append(
            "已跳过    "
            + "；".join(f"{item.get('symbol', '?')}（{item.get('reason', '无结果')}）" for item in skipped)
        )
    return lines
