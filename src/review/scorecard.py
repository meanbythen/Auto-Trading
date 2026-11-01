"""
当日成绩单：有效性 / 稳定性 / 可信度
功能：用三个 0–100 的分数，概括「今天这套策略在真实行情上靠不靠谱」。
原理：
  有效性 = 信号有没有捕捉到点数优势（R、胜率、捕获率）；
  稳定性 = 行为是否极端（不过度交易、不过度回吐、观望原因不过分畸形）；
  可信度 = 数据与执行是否站得住（K线够不够、是否收盘、信号与实盘是否对齐）。
  借鉴交易日志软件的 dashboard：先看分数，再钻缺陷，而不是只读长文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.review.diagnose import Diagnosis
from src.review.gap import GapReport
from src.review.quality import QualityReport
from src.review.replay import StrategyReplay
from src.review.session import SessionWindow, any_still_open
from src.review.technical import TechnicalView


@dataclass
class Scorecard:
    """三个分数 + 一句话总评 + 写进报告抬头的句子。"""

    effectiveness: int = 0
    stability: int = 0
    credibility: int = 0
    summary: str = ""
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "effectiveness": self.effectiveness,
            "stability": self.stability,
            "credibility": self.credibility,
            "summary": self.summary,
        }


def build_scorecard(
    *,
    technicals: list[TechnicalView],
    replay: StrategyReplay,
    quality: QualityReport,
    gap: GapReport,
    diagnosis: Diagnosis,
    windows: list[SessionWindow],
    now,
) -> Scorecard:
    """根据当天材料打三个分。只看盘面时有效性/稳定性给中性分并说明。"""
    card = Scorecard()
    if replay.engine is None:
        card.effectiveness = 50
        card.stability = 50
        card.credibility = _credibility(technicals, quality, gap, windows, now, market_only=True)
        card.summary = "只看盘面：不计策略有效性，可信度只反映行情与日志是否完整。"
        card.lines = _format_lines(card)
        return card

    card.effectiveness = _effectiveness(quality, replay)
    card.stability = _stability(quality, diagnosis, replay)
    card.credibility = _credibility(technicals, quality, gap, windows, now, market_only=False)
    card.summary = _summary_text(card, diagnosis)
    card.lines = _format_lines(card)
    return card


def _effectiveness(quality: QualityReport, replay: StrategyReplay) -> int:
    if not replay.opened:
        # 观望日：没有亏损也没有验证，给中性偏上，避免「空仓=高分」误导
        return 55
    score = 45.0
    closed = quality.closed_count or len(quality.trades)
    if closed <= 0:
        return 50
    wr = quality.win_count / closed
    score += (wr - 0.45) * 40  # 胜率相对 45% 的偏离
    if quality.avg_r is not None:
        score += max(-20.0, min(quality.avg_r * 12.0, 25.0))
    if quality.avg_capture is not None:
        score += (quality.avg_capture - 0.35) * 30
    if quality.net_points > 0:
        score += 5
    elif quality.net_points < 0:
        score -= 8
    return int(max(0, min(round(score), 100)))


def _stability(quality: QualityReport, diagnosis: Diagnosis, replay: StrategyReplay) -> int:
    score = 72.0
    hard = {
        "利润回吐过多",
        "止损可能过紧",
        "当日开仓偏多",
        "方向与收盘相悖",
        "信号与实盘脱节",
        "执行笔数偏差",
    }
    for name in diagnosis.defects:
        score -= 10 if name in hard else 6
    if quality.avg_capture is not None and quality.avg_capture < 0.25 and quality.closed_count:
        score -= 8
    if len(quality.trades) >= 4:
        score -= 6
    if not replay.opened and replay.main_watch_reason:
        # 观望原因清晰，行为稳定
        score += 4
    return int(max(0, min(round(score), 100)))


def _credibility(
    technicals: list[TechnicalView],
    quality: QualityReport,
    gap: GapReport,
    windows: list[SessionWindow],
    now,
    *,
    market_only: bool,
) -> int:
    score = 70.0
    bars = sum(v.bar_count for v in technicals)
    if bars < 8:
        score -= 25
    elif bars < 20:
        score -= 10
    elif bars >= 40:
        score += 5
    if any_still_open(windows, now):
        score -= 15  # 盘未收完，结论临时
    if not market_only:
        if gap.alignment == "matched":
            score += 12
        elif gap.alignment == "signal_only":
            score -= 18
        elif gap.alignment == "actual_only":
            score -= 12
        elif gap.alignment == "none" and quality.closed_count == 0:
            score += 4  # 一致空仓，日志完整意义上可信
    if not any(v.has_bars for v in technicals):
        score = min(score, 25)
    return int(max(0, min(round(score), 100)))


def _summary_text(card: Scorecard, diagnosis: Diagnosis) -> str:
    avg = (card.effectiveness + card.stability + card.credibility) / 3.0
    if avg >= 75:
        head = "今天整体偏健康"
    elif avg >= 55:
        head = "今天中等，有可改进点"
    else:
        head = "今天暴露出较明显问题"
    if diagnosis.defects:
        return f"{head}。优先处理：{diagnosis.defects[0]}。"
    return f"{head}。暂无突出缺陷标签，继续积累多日对照。"


def _format_lines(card: Scorecard) -> list[str]:
    return [
        f"有效性 {card.effectiveness}　稳定性 {card.stability}　可信度 {card.credibility}",
        f"一句话：{card.summary}",
        "读法：有效性看信号赚不赚点；稳定性看行为是否极端；可信度看数据齐不齐、执行对不对得上。"
        "三个分数都是当天快照，要连看多天，并和「策略优化」的检验段一起用。",
    ]
