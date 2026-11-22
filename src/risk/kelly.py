"""
凯利仓位（可选）

功能：优先按每条策略腿最近的净平仓记录动态计算胜率、盈亏比和凯利目标；
     样本不足退回固定风险，样本充分但没有正期望时拒绝新开仓。
原理：全凯利 f* = 胜率 − 败率÷盈亏比。实盘默认只用四分之一，
     并同时受凯利自身封顶和 risk_per_trade_pct 固定风险硬上限约束。

凯利只决定「意图风险%」。保证金、柜台资金、日亏、下单频率和动态回撤仍继续取小或拦截。
手工胜率/盈亏比只用于 auto_from_ledger=false 的兼容模式。盈亏比不是盈利因子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 盈亏比大到离谱时封顶，避免算出「把全部本金押上」。
MAX_PAYOFF = 5.0
DEFAULT_FRACTION = 0.25
DEFAULT_MAX_F_PCT = 1.0
DEFAULT_MIN_SAMPLES = 30
DEFAULT_WINDOW = 50

FRACTION_LABELS: dict[str, float] = {
    "四分之一（推荐）": 0.25,
    "一半": 0.5,
    "全凯利": 1.0,
}


def fraction_from_label(label: object) -> float:
    """界面中文名换成小数。不认识的按四分之一。"""
    text = str(label or "").strip()
    if text in FRACTION_LABELS:
        return FRACTION_LABELS[text]
    try:
        value = float(text)
    except (TypeError, ValueError):
        return DEFAULT_FRACTION
    if value <= 0:
        return DEFAULT_FRACTION
    return min(1.0, value)


def label_from_fraction(value: object) -> str:
    """小数换成界面中文名。"""
    try:
        num = float(str(value))
    except (TypeError, ValueError):
        num = DEFAULT_FRACTION
    for name, frac in FRACTION_LABELS.items():
        if abs(frac - num) < 1e-9:
            return name
    return "四分之一（推荐）"


def parse_win_rate(raw: object) -> float | None:
    """
    胜率。界面填 46 表示 46%；配置里写成 0.46 也认。
    空着或填错返回 None。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def parse_payoff(raw: object) -> float | None:
    """盈亏比 = 平均盈利 ÷ 平均亏损。必须大于 0。"""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return float(value)


def win_rate_pct_display(rate: float | None) -> str:
    """给输入框看的百分数，例如 46。"""
    if rate is None:
        return ""
    return f"{rate * 100:.4g}"


@dataclass
class KellyPlan:
    """一次算出来的仓位计划，给界面和手数公式用。"""

    ok: bool
    reason: str
    win_rate: float = 0.0
    payoff: float = 0.0
    f_full: float = 0.0
    f_used: float = 0.0
    risk_pct: float = 0.0
    sample_count: int = 0
    source: str = "manual"


def kelly_box(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """取出 risk.kelly 这一节。没有就当空字典。"""
    risk = (cfg or {}).get("risk") if isinstance(cfg, dict) else {}
    box = (risk or {}).get("kelly") if isinstance(risk, dict) else {}
    return box if isinstance(box, dict) else {}


def kelly_enabled(cfg: dict[str, Any] | None) -> bool:
    """配置页「凯利公式」开关是否打开。"""
    return bool(kelly_box(cfg).get("enabled"))


def read_manual_factors(box: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """
    读你填的胜率和盈亏比。

    优先用配置页那两项；旧文件里若还留着自动获取的 snapshot，也能读出来，
    方便你对照后改成手填。
    """
    box = box if isinstance(box, dict) else {}
    snap_raw = box.get("snapshot")
    snap: dict[str, Any] = snap_raw if isinstance(snap_raw, dict) else {}
    rate = parse_win_rate(box.get("win_rate_pct"))
    if rate is None:
        rate = parse_win_rate(box.get("win_rate"))
    if rate is None:
        rate = parse_win_rate(snap.get("win_rate"))
    payoff = parse_payoff(box.get("payoff"))
    if payoff is None:
        payoff = parse_payoff(snap.get("payoff"))
    if payoff is None:
        avg_win = snap.get("avg_win")
        avg_loss = snap.get("avg_loss")
        try:
            win = float(avg_win or 0)
            loss = float(avg_loss or 0)
        except (TypeError, ValueError):
            win, loss = 0.0, 0.0
        if win > 0 and loss > 0:
            payoff = win / loss
    return rate, payoff


def full_kelly_f(win_rate: float, payoff: float) -> float:
    """
    全凯利比例 f* = p − q/b。

    没有正期望时为 0。盈亏比 b 超过上限时按上限算。
    """
    p = max(0.0, min(1.0, float(win_rate or 0)))
    b = min(MAX_PAYOFF, max(0.0, float(payoff or 0)))
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f_star = (b * p - q) / b
    return max(0.0, float(f_star))


def plan_from_stats(
    *,
    win_rate: float,
    payoff: float,
    fraction: float = DEFAULT_FRACTION,
    max_f_pct: float = DEFAULT_MAX_F_PCT,
) -> KellyPlan:
    """用胜率、盈亏比算出实际拿去冒险的百分比。"""
    p = max(0.0, min(1.0, float(win_rate or 0)))
    b = max(0.0, float(payoff or 0))
    f_full = full_kelly_f(p, b)
    shown_b = min(MAX_PAYOFF, b) if b > 0 else 0.0
    if f_full <= 1e-12:
        return KellyPlan(
            False,
            (
                f"填的胜率{p:.0%}、盈亏比{shown_b:.2f}没有正期望，"
                "凯利不开新仓。请用历史回测里「平均盈利÷平均亏损」，不要填盈利因子。"
            ),
            win_rate=p,
            payoff=shown_b,
            f_full=0.0,
        )
    frac = min(1.0, max(0.0, float(fraction or DEFAULT_FRACTION)))
    cap = max(0.05, float(max_f_pct if max_f_pct is not None else DEFAULT_MAX_F_PCT))
    f_used = f_full * frac
    risk_pct = min(cap, f_used * 100.0)
    if risk_pct <= 1e-12:
        return KellyPlan(False, "凯利算出的每笔风险过小，不开新仓。", win_rate=p, payoff=shown_b)
    return KellyPlan(
        True,
        (
            f"凯利：胜率{p:.0%} 盈亏比{shown_b:.2f} "
            f"全凯利{f_full * 100:.2f}% ×{frac:g} → 每笔风险{risk_pct:.2f}%"
            f"（封顶{cap:g}%）"
        ),
        win_rate=p,
        payoff=shown_b,
        f_full=f_full,
        f_used=f_used,
        risk_pct=risk_pct,
    )


def summarize_closed_rows(
    rows: list[dict[str, Any]] | None,
    symbol: str,
    *,
    window: int = DEFAULT_WINDOW,
) -> dict[str, float | int]:
    """把一条腿的净平仓盈亏汇成凯利统计；同一笔开平的部分成交先合并。"""
    target = str(symbol or "").strip().lower()
    grouped: dict[tuple[Any, ...], float] = {}
    for index, row in enumerate(rows or []):
        if target and str(row.get("symbol") or "").strip().lower() != target:
            continue
        try:
            value = float(row.get("net_pnl", row.get("pnl", 0.0)) or 0.0)
        except (TypeError, ValueError):
            continue
        entry = str(row.get("entry_time") or "")
        exit_ = str(row.get("exit_time") or "")
        # 回测只传 pnl 时没有开平时间，每一行本身就是一笔完整平仓，不能互相合并。
        key = (entry, exit_, str(row.get("direction") or row.get("side") or "")) if entry or exit_ else (index,)
        grouped[key] = grouped.get(key, 0.0) + value
    values = list(grouped.values())[-max(1, int(window or DEFAULT_WINDOW)):]
    count = len(values)
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    # 零盈亏按未获胜计入败率，凯利不会因为大量打平单而虚增仓位。
    rate = len(wins) / count if count else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    if avg_win > 0 and avg_loss > 0:
        payoff = min(MAX_PAYOFF, avg_win / avg_loss)
    elif avg_win > 0:
        payoff = MAX_PAYOFF
    else:
        payoff = 0.0
    return {"sample_count": count, "win_rate": rate, "payoff": payoff}


def _fixed_risk_pct(cfg: dict[str, Any] | None) -> float:
    raw = (cfg or {}).get("risk")
    risk: dict[str, Any] = raw if isinstance(raw, dict) else {}
    try:
        return max(0.0, float(risk.get("risk_per_trade_pct", 1.0) or 0.0))
    except (TypeError, ValueError):
        return 1.0


def _with_fixed_cap(plan: KellyPlan, fixed_pct: float) -> KellyPlan:
    """无论动态凯利还是手工凯利，固定风险都是最后一道百分比硬上限。"""
    capped = min(max(0.0, plan.risk_pct), fixed_pct)
    if capped > 1e-12:
        if capped < plan.risk_pct:
            plan.reason += f"；固定风险再封顶{fixed_pct:g}%"
        plan.risk_pct = capped
        return plan
    plan.ok = False
    plan.risk_pct = 0.0
    if fixed_pct <= 1e-12:
        plan.reason = "固定风险上限为 0，不开新仓。"
    return plan


def plan_from_config(
    cfg: dict[str, Any] | None,
    *,
    symbol: str | None = None,
    closed_rows: list[dict[str, Any]] | None = None,
) -> KellyPlan:
    """生成凯利目标。传入 closed_rows 即启用分腿动态统计，空列表也代表真实零样本。"""
    if not kelly_enabled(cfg):
        return KellyPlan(False, "凯利开关未打开")
    box = kelly_box(cfg)
    fixed_pct = _fixed_risk_pct(cfg)
    try:
        fraction = float(box.get("fraction", DEFAULT_FRACTION))
    except (TypeError, ValueError):
        fraction = DEFAULT_FRACTION
    try:
        max_f_pct = float(box.get("max_f_pct", DEFAULT_MAX_F_PCT))
    except (TypeError, ValueError):
        max_f_pct = DEFAULT_MAX_F_PCT
    auto = bool(box.get("auto_from_ledger", True))
    if auto and closed_rows is not None:
        try:
            window = max(1, int(box.get("window", DEFAULT_WINDOW) or DEFAULT_WINDOW))
            min_samples = max(1, int(box.get("min_samples", DEFAULT_MIN_SAMPLES) or DEFAULT_MIN_SAMPLES))
        except (TypeError, ValueError):
            window, min_samples = DEFAULT_WINDOW, DEFAULT_MIN_SAMPLES
        stats = summarize_closed_rows(closed_rows, str(symbol or ""), window=window)
        count = int(stats["sample_count"])
        if count < min_samples:
            plan = KellyPlan(
                fixed_pct > 1e-12,
                f"{symbol or '本腿'}凯利净样本{count}/{min_samples}笔，暂用固定风险{fixed_pct:g}%",
                sample_count=count,
                source="fixed_fallback",
                risk_pct=fixed_pct,
            )
            return _with_fixed_cap(plan, fixed_pct)
        plan = plan_from_stats(
            win_rate=float(stats["win_rate"]),
            payoff=float(stats["payoff"]),
            fraction=fraction,
            max_f_pct=max_f_pct,
        )
        plan.sample_count = count
        plan.source = "leg"
        plan.reason = f"{symbol or '本腿'}最近{count}笔净平仓；" + plan.reason
        return _with_fixed_cap(plan, fixed_pct)
    rate, payoff = read_manual_factors(box)
    if rate is None or payoff is None:
        return KellyPlan(False, "请先填写凯利胜率和凯利盈亏比（从历史回测结果抄）。")
    plan = plan_from_stats(win_rate=rate, payoff=payoff, fraction=fraction, max_f_pct=max_f_pct)
    plan.source = "manual"
    return _with_fixed_cap(plan, fixed_pct)


def status_text(cfg: dict[str, Any] | None) -> str:
    """配置页那一行给人看的说明。"""
    box = kelly_box(cfg)
    if not box.get("enabled"):
        return "凯利关着。手数按固定风险百分比或固定手数模式计算。"
    if box.get("auto_from_ledger", True):
        return "分腿动态凯利已启用：实盘和回测按最近净平仓更新；样本不足用固定风险，负期望停开。"
    return plan_from_config(cfg).reason
