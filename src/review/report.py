"""
把多块材料写成一篇能直接读的复盘
功能：盘面、技术、基本面、策略、信号质量、真实日志、信号/实盘对照、缺陷诊断、基准对照，最后综合。
原理：每一块只负责自己的事实；成绩单放在最前面，方便像专业交易软件一样先看分数再钻细节。
"""

from __future__ import annotations

from datetime import date, datetime

from src.review.baseline import BaselineReport
from src.review.diagnose import Diagnosis
from src.review.fundamental import FundamentalView
from src.review.gap import GapReport
from src.review.quality import QualityReport
from src.review.replay import StrategyReplay
from src.review.scorecard import Scorecard
from src.review.session import SessionWindow, any_still_open, closure_lines
from src.review.technical import TechnicalView


def render_report(
    *,
    symbol: str,
    trade_date: date,
    scope: str,
    windows: list[SessionWindow],
    technicals: list[TechnicalView],
    fundamental: FundamentalView,
    replay: StrategyReplay,
    actual: list[str],
    quality: QualityReport | None = None,
    gap: GapReport | None = None,
    diagnosis: Diagnosis | None = None,
    scorecard: Scorecard | None = None,
    baseline: BaselineReport | None = None,
    replay_eval: list[str] | None = None,
    live_eval: list[str] | None = None,
    now: datetime | None = None,
) -> str:
    """拼出完整复盘正文。"""
    now = now or datetime.now()
    quality = quality or QualityReport()
    gap = gap or GapReport(lines=["（未生成对照）"])
    diagnosis = diagnosis or Diagnosis(lines=["（未生成诊断）"])
    scorecard = scorecard or Scorecard(summary="未生成成绩单")
    baseline = baseline or BaselineReport(lines=["（未生成基准）"])

    lines: list[str] = [
        f"【收盘复盘】{symbol}    {trade_date.isoformat()}    {scope}",
        "这是盘后回顾：用当天真实行情检验策略稳不稳、有没有效、值不值得信。",
        "它补充「历史回测 / 策略优化」的训练与检验，不是下单指令，也不构成投资建议。",
        "",
        "收盘情况：",
    ]
    lines.extend(f"  · {x}" for x in closure_lines(windows, now))
    if any_still_open(windows, now):
        lines.append("  · 有盘还在走，下面的综合结论是临时的。")

    lines.extend(["", "【当日成绩单】"])
    for row in scorecard.lines:
        lines.append(f"  {row}")

    lines.extend(["", "一、当天盘面怎么走（不看策略）"])
    for view in technicals:
        lines.append(f"【{view.name}】")
        if not view.facts:
            lines.append("  （没有内容）")
        for fact in view.facts:
            lines.append(f"  {fact}")

    lines.extend(["", "二、技术面"])
    for view in technicals:
        lines.append(f"【{view.name}】")
        points = view.tech_points or ["这段没有足够K线，技术面先空着。"]
        for point in points:
            lines.append(f"  · {point}")

    lines.extend(["", f"三、当日基本面（{fundamental.name}）"])
    for row in fundamental.lines:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", f"四、策略对照：{replay.label}"])
    for row in replay.lines:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "五、信号质量（MAE / MFE / R / 捕获率）"])
    for row in quality.lines:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "六、程序当天真实记录"])
    for row in actual:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "七、信号与实盘对照"])
    for row in gap.lines:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "八、缺陷诊断与改进建议"])
    for row in diagnosis.lines:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "九、相对基准（近几日复盘 / 最近回测）"])
    for row in baseline.lines:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "十、当日重放评估"])
    for row in replay_eval or ["（未生成）"]:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "十一、最近实盘 vs 最近回测"])
    for row in live_eval or ["（未生成）"]:
        lines.append(f"  {row}" if not row.startswith("  ") else row)

    lines.extend(["", "十二、综合：开仓还是观望，为什么，以及今天信不信这套规则"])
    for row in _conclusion(
        technicals,
        fundamental,
        replay,
        scorecard,
        diagnosis,
        still_open=any_still_open(windows, now),
    ):
        lines.append(f"  {row}")

    lines.append("")
    lines.append(
        "验证路径：同一天可到「历史回测」再跑一遍核对信号；"
        "要把参数改得更稳，用「策略优化」的检验段或滚动前进；"
        "真实盈亏以「账户分析」为准。复盘摘要已另存 JSON，供以后几天对照。"
    )
    return "\n".join(lines)


def _conclusion(
    technicals: list[TechnicalView],
    fundamental: FundamentalView,
    replay: StrategyReplay,
    scorecard: Scorecard,
    diagnosis: Diagnosis,
    *,
    still_open: bool,
) -> list[str]:
    """把盘面方向、库存方向、策略动作、成绩单放在一起说。"""
    out: list[str] = []
    if still_open:
        out.append("盘还没走完，先别把这段当成全天结论。")

    biases = [v.bias for v in technicals if v.has_bars]
    if not biases:
        tape = "技术面上没有这段K线，谈不上偏多或偏空。"
        tape_bias = "mixed"
    elif all(b == "up" for b in biases):
        tape = "技术面上，这几段都偏强或有收回。"
        tape_bias = "up"
    elif all(b == "down" for b in biases):
        tape = "技术面上，这几段都偏弱或冲高没守住。"
        tape_bias = "down"
    else:
        tape = "技术面上方向不一致，或者就是震荡。"
        tape_bias = "mixed"
    out.append(tape)

    if fundamental.bias == "up":
        out.append("基本面这边，最近仓单在减少，供给故事偏多。")
        fund_bias = "up"
    elif fundamental.bias == "down":
        out.append("基本面这边，最近仓单在增加，供给故事偏空。")
        fund_bias = "down"
    else:
        out.append("基本面没有形成明确的多空（没有可用的库存增减，或你没补充消息）。")
        fund_bias = "unknown"

    if fund_bias in ("up", "down") and tape_bias in ("up", "down") and fund_bias != tape_bias:
        out.append("技术面和库存基本面不是一边的。这种日子更容易来回抽，不适合凭一个理由重仓。")
    elif fund_bias in ("up", "down") and fund_bias == tape_bias:
        out.append("技术面和库存方向大体一致，但一致也不等于一定要做。")

    out.append(
        f"成绩单：有效性 {scorecard.effectiveness} / 稳定性 {scorecard.stability} / "
        f"可信度 {scorecard.credibility}。{scorecard.summary}"
    )
    if diagnosis.defects:
        out.append(f"今天优先盯的缺陷：{'、'.join(diagnosis.defects[:3])}。细则在第八节。")

    if replay.engine is None:
        out.append("你选了只看盘面，所以这里不下「该开仓」的结论。上面是走势和背景，决定权在你的交易计划。")
        return out

    if replay.opened:
        side = {"long": "做多", "short": "做空", "mixed": "多空都做过"}.get(replay.direction, "开仓")
        out.append(f"按程序策略，这段会{side}。开仓原因写在第四节每一笔后面，是策略自己给出的条件，不是看涨跌猜的。")
        if replay.direction == "long" and tape_bias == "down":
            out.append("注意：策略做多时，盘面收势并不强，要回头看那一笔是不是假突破后的针，而不是追涨。")
        if replay.direction == "short" and tape_bias == "up":
            out.append("注意：策略做空时，盘面收势并不弱，要回头看那一笔是不是假突破后的针，而不是追跌。")
    else:
        why = replay.main_watch_reason or "没有留下具体原因"
        out.append(f"按程序策略，这段应该观望，不开仓。最主要卡在：{why}。")
        out.append("观望不是错过行情的同义词。这套策略只在形态、位置、时段都齐的时候才做，单边上涨但没有它要的信号，就会空仓看着。")
    return out
