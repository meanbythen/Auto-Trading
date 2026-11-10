"""
把过关的统计规律，收成「更像哪一类策略」的倾向。

功能：
  先按常见交易逻辑分成趋势、突破、震荡回归、波段四类。
  再对照本程序已经有的策略，给出可以去回测的名字。
  只给倾向，不改开平仓，也不保证以后赚钱。

原理：
  因子高了后面还顺着走 → 更像趋势/突破。
  因子高了后面往回走 → 更像震荡回归。
  影线+放量、跳空这类结构 → 更像波段。
  「大部分时间一致」看分段窗口里同向显著的比例，不是只看两半各一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.research.scan import Regularity

# 分段里至少这么大比例同向，才说「大部分时间较稳」。四段里要过三段。
MIN_CONSISTENCY = 0.75

STYLE_TREND = "trend"
STYLE_BREAKOUT = "breakout"
STYLE_REVERSION = "reversion"
STYLE_SWING = "swing"

STYLE_LABELS = {
    STYLE_TREND: "趋势跟踪",
    STYLE_BREAKOUT: "突破",
    STYLE_REVERSION: "震荡回归",
    STYLE_SWING: "波段/结构",
}

# 本程序现有策略属于哪一类。没有单独的震荡回归引擎，倾向回归时如实说。
ENGINES_BY_STYLE: dict[str, tuple[tuple[str, str], ...]] = {
    STYLE_TREND: (
        ("dual_ma", "双均线"),
        ("supertrend", "SuperTrend"),
        ("sar_trend", "SAR 波段"),
        ("trend_follow", "海龟突破"),
        ("multi_factor", "多因子 CTA 加权"),
        ("swing_trend", "长线趋势加减仓"),
    ),
    STYLE_BREAKOUT: (
        ("daily_body", "日内实体（开盘箱体收盘突破）"),
        ("morning_box_a", "早盘箱体 A"),
        ("bollinger", "布林突破"),
        ("trend_follow", "海龟突破"),
    ),
    STYLE_REVERSION: (),
    STYLE_SWING: (
        ("jesse_casper", "Jesse Casper 简化 SMC"),
        ("swing_trend", "长线趋势加减仓"),
    ),
}

# 因子编号 → 因子升高时，若后面同向(t>0)投哪一类；反向(t<0)投另一类。
# 空字符串表示这一侧不投票。
_STYLE_WHEN_HIGH_CONTINUES: dict[str, str] = {
    "ret_1": STYLE_TREND,
    "ret_5": STYLE_TREND,
    "consec_up": STYLE_TREND,
    "ma_gap": STYLE_TREND,
    "ret_5|ma_gap": STYLE_TREND,
    "ret_5|volume_ratio": STYLE_TREND,
    "volume_ratio": STYLE_BREAKOUT,
    "range_pos": STYLE_BREAKOUT,
    # 大实体后面还顺着走，更像趋势推进，不是窄区间突破。
    "body_ratio": STYLE_TREND,
    "range_pos|volume_ratio": STYLE_BREAKOUT,
    "body_ratio|ret_1": STYLE_TREND,
    "atr_pct": STYLE_TREND,
    "ma_gap|atr_pct": STYLE_TREND,
    "upper_wick": STYLE_SWING,
    "lower_wick": STYLE_SWING,
    "upper_wick|volume_ratio": STYLE_SWING,
    "lower_wick|volume_ratio": STYLE_SWING,
    "gap": STYLE_SWING,
}

_STYLE_WHEN_HIGH_REVERSES: dict[str, str] = {
    "ret_1": STYLE_REVERSION,
    "ret_5": STYLE_REVERSION,
    "consec_up": STYLE_REVERSION,
    "ma_gap": STYLE_REVERSION,
    "ret_5|ma_gap": STYLE_REVERSION,
    "upper_wick": STYLE_REVERSION,
    "lower_wick": STYLE_REVERSION,
    "ma_gap|atr_pct": STYLE_REVERSION,
    "gap": STYLE_REVERSION,
}


@dataclass
class StyleAdvice:
    """给人看的倾向，不是下单指令。"""

    primary: str
    scores: dict[str, float] = field(default_factory=dict)
    consistency: float = 0.0
    used: int = 0
    skipped_unstable: int = 0
    engines: list[tuple[str, str]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "scores": dict(self.scores),
            "consistency": self.consistency,
            "used": self.used,
            "skipped_unstable": self.skipped_unstable,
            "engines": [{"id": eid, "name": name} for eid, name in self.engines],
            "summary": self.summary,
        }


def _base_id(item_id: str) -> str:
    """ret_5|h6 → ret_5；ret_5|volume_ratio|h6 → ret_5|volume_ratio。"""
    if "|h" in item_id:
        return item_id.rsplit("|h", 1)[0]
    return item_id


def _sign_t(item: Regularity) -> float:
    """两半 t 的平均，用来判断是顺着走还是往回走。"""
    a = float(item.details.get("t_first") or 0.0)
    b = float(item.details.get("t_second") or 0.0)
    return (a + b) / 2.0


def _consistency(item: Regularity) -> float:
    total = float(item.details.get("windows_total") or 0.0)
    ok = float(item.details.get("windows_ok") or 0.0)
    if total <= 0:
        # 旧结果没有分段时，两半都过关就当成 1。
        return 1.0
    return ok / total


def advise_styles(items: list[Regularity]) -> StyleAdvice:
    """
    根据过关规律投票。分段不稳定的条不投票，避免偶然两段显著就下结论。
    """
    scores = {key: 0.0 for key in STYLE_LABELS}
    used = 0
    skipped = 0
    cons_sum = 0.0
    for item in items:
        cons = _consistency(item)
        if cons < MIN_CONSISTENCY:
            skipped += 1
            continue
        t_val = _sign_t(item)
        if t_val == 0:
            skipped += 1
            continue
        base = _base_id(item.id)
        table = _STYLE_WHEN_HIGH_CONTINUES if t_val > 0 else _STYLE_WHEN_HIGH_REVERSES
        style = table.get(base, "")
        if not style:
            continue
        weight = abs(t_val) * cons
        scores[style] += weight
        used += 1
        cons_sum += cons

    if used <= 0:
        return StyleAdvice(
            primary="",
            scores=scores,
            skipped_unstable=skipped,
            summary=(
                "这段里没有「大部分时间都同向」的规律，还不能说该用趋势还是震荡。"
                "可能是样本太短、中间风格在变，或这些因子在这段历史上站不住。"
                "先换一段时间或换一个合约再扫。不要据此换实盘策略。"
            ),
        )

    primary = max(scores, key=lambda key: scores[key])
    if scores[primary] <= 0:
        primary = ""
    avg_cons = cons_sum / used if used else 0.0
    engines = list(ENGINES_BY_STYLE.get(primary, ()))
    label = STYLE_LABELS.get(primary, "不明显")
    engine_txt = "、".join(name for _eid, name in engines) if engines else "本程序没有单独的震荡回归策略"
    extra = ""
    ranked = sorted(scores, key=lambda key: scores[key], reverse=True)
    if len(ranked) >= 2 and scores[ranked[1]] > 0 and scores[ranked[1]] >= 0.45 * scores[primary]:
        extra += f"第二接近的是「{STYLE_LABELS[ranked[1]]}」，可以并列拿去对照。"
    if primary == STYLE_REVERSION:
        extra += (
            "本程序目前没有「高抛低吸」专用引擎。统计若偏回归，先不要用双均线、海龟、多因子硬做趋势；"
            "可以把这条当过滤（乱的时候少开），而不是新开一套反向策略。"
        )
    summary = (
        f"在你选的这段里，分段稳定（至少约 {int(MIN_CONSISTENCY * 100)}% 时间同向）的规律有 {used} 条"
        f"（另有 {skipped} 条两半显著但不稳，没有用来投票）。"
        f"平均分段一致比例 {avg_cons:.0%}。"
        f"倾向更像「{label}」。可以先去交叉测试/回测：{engine_txt}。"
        f"{extra}"
        "这是类型倾向，不是保证赚钱，也不会自动改你正在用的策略。"
    )
    return StyleAdvice(
        primary=primary,
        scores=scores,
        consistency=avg_cons,
        used=used,
        skipped_unstable=skipped,
        engines=engines,
        summary=summary.strip(),
    )


def format_style_section(advice: StyleAdvice) -> str:
    """写在说明书最上面，方便先看结论再看逐条证据。"""
    lines = ["—— 类型倾向（不是买卖点）——", advice.summary]
    if advice.primary:
        lines.append(
            "得分  "
            + "；".join(
                f"{STYLE_LABELS[key]} {advice.scores.get(key, 0):.1f}" for key in STYLE_LABELS
            )
        )
    lines.append("")
    return "\n".join(lines)
