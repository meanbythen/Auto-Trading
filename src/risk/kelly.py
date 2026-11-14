"""
凯利仓位（可选）

功能：你自己跑完历史回测，把胜率和盈亏比填进配置页。打开凯利开关后，
     用这两项算出这一笔最多拿本金的百分之几去冒险。
原理：全凯利 f* = 胜率 − 败率÷盈亏比。全凯利通常太大，默认只用一半，
     并且每笔风险再封顶（默认最多 2%）。没填齐、或算出来没有正期望，不开新仓。

凯利只替换「意图风险%」。仓位上限、日最大亏损、下单间隔、波动暂停仍照旧拦。
盈亏比是「平均盈利 ÷ 平均亏损」，不是回测摘要里的盈利因子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 盈亏比大到离谱时封顶，避免算出「把全部本金押上」。
MAX_PAYOFF = 5.0
DEFAULT_FRACTION = 0.5
DEFAULT_MAX_F_PCT = 2.0

FRACTION_LABELS: dict[str, float] = {
    "四分之一": 0.25,
    "一半（推荐）": 0.5,
    "全凯利": 1.0,
}


def fraction_from_label(label: object) -> float:
    """界面中文名换成小数。不认识的按一半。"""
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
    return "一半（推荐）"


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


def plan_from_config(cfg: dict[str, Any] | None) -> KellyPlan:
    """
    看当前配置能不能按凯利开仓。

    开关关着时 ok=False，调用方应走原来的每笔风险%。
    开关开着但没填胜率或盈亏比：也不开新仓。
    """
    if not kelly_enabled(cfg):
        return KellyPlan(False, "凯利开关未打开")
    box = kelly_box(cfg)
    rate, payoff = read_manual_factors(box)
    if rate is None or payoff is None:
        return KellyPlan(False, "请先填写凯利胜率和凯利盈亏比（从历史回测结果抄）。")
    try:
        fraction = float(box.get("fraction", DEFAULT_FRACTION))
    except (TypeError, ValueError):
        fraction = DEFAULT_FRACTION
    try:
        max_f_pct = float(box.get("max_f_pct", DEFAULT_MAX_F_PCT))
    except (TypeError, ValueError):
        max_f_pct = DEFAULT_MAX_F_PCT
    return plan_from_stats(
        win_rate=rate,
        payoff=payoff,
        fraction=fraction,
        max_f_pct=max_f_pct,
    )


def status_text(cfg: dict[str, Any] | None) -> str:
    """配置页那一行给人看的说明。"""
    box = kelly_box(cfg)
    if not box.get("enabled"):
        return "凯利关着。手数仍按仓位模式和每笔风险%。打开后按你填的胜率、盈亏比自动算手数。"
    return plan_from_config(cfg).reason
