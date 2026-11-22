"""
策略评估
功能：把回测/成交按同一套门槛打分，给出「通过 / 观察 / 淘汰」结论和不达标项。
原理：门槛取自 docs/策略评估标准/评估指标.md。
      - 红线（fail）：盈利因子过低、滑点后盈利因子掉到 1 以下、卡尔玛过低、
        回撤过大、参数扰动断崖、震荡组大量开仓且持续赚钱。
      - 观察（watch）：没碰红线，但低于最低合格门槛（样本、收益期望、盈利覆盖、连亏等）。
        样本不足只压到观察，不判红线：笔数少说明统计显著性不足，不等于策略无效。
      - 通过（pass）：全部达到最低门槛；到优秀标准时在说明里标「优秀」。
      没有数据可算的检查记 skip，不参与结论。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.backtest.metrics import Performance, TradeRecord, calc_performance, unit_expectancy

PASS = "pass"
WATCH = "watch"
FAIL = "fail"
SKIP = "skip"

CONCLUSION_TEXT = {"pass": "通过", "watch": "观察", "reject": "淘汰"}
STATUS_TEXT = {
    PASS: "达标",
    WATCH: "仅够观察",
    FAIL: "不达标（红线）",
    SKIP: "—",
}


def _num(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(number) or math.isinf(number):
        return float(default)
    return number


def _int(value: Any, default: int) -> int:
    return int(round(_num(value, float(default))))


def _flag(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "是", "开", "on"}:
        return True
    if text in {"0", "false", "no", "n", "否", "关", "off"}:
        return False
    return default


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


@dataclass(frozen=True)
class EvaluationThresholds:
    """评估门槛。字段名和 config.yaml 里的 evaluation 块一致。"""

    enabled: bool = True
    adx_period: int = 14
    adx_trend_min: float = 20.0
    stress_on_backtest: bool = True
    gate_on_optimize: bool = True

    profit_factor_min: float = 1.3
    profit_factor_excellent: float = 1.5
    # 亏损笔数不足时 PF 没有统计意义（全胜时是 99 的哨兵值），不参与判定。
    profit_factor_min_losses: int = 2
    r_expect_min: float = 0.12
    r_expect_excellent: float = 0.20
    calmar_min: float = 1.0
    calmar_excellent: float = 1.5
    calmar_red: float = 0.8
    max_drawdown_pct: float = 0.15
    max_drawdown_pct_excellent: float = 0.10
    max_drawdown_pct_red: float = 0.20
    profit_cover_min: float = 0.0
    trades_min: int = 150
    trades_observe: int = 100
    trades_excellent: int = 250
    max_consecutive_losses: int = 8
    max_consecutive_losses_excellent: int = 5
    range_pf_max: float = 1.0
    range_trade_share_max: float = 0.30
    slippage_extra_ticks: float = 1.0
    slippage_pf_min: float = 1.2
    slippage_pf_red: float = 1.0
    perturbation_step_pct: float = 0.10
    perturbation_pf_ratio: float = 0.80
    perturbation_pf_min: float = 1.10
    stress_min_trades: int = 30

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any] | None) -> "EvaluationThresholds":
        """读配置里的 evaluation 块；缺项、填错都回落到默认值。"""
        raw = (cfg or {}).get("evaluation")
        box = raw if isinstance(raw, dict) else {}
        base = cls()
        return cls(
            enabled=_flag(box.get("enabled"), base.enabled),
            adx_period=max(2, _int(box.get("adx_period"), base.adx_period)),
            adx_trend_min=max(0.0, _num(box.get("adx_trend_min"), base.adx_trend_min)),
            stress_on_backtest=_flag(box.get("stress_on_backtest"), base.stress_on_backtest),
            gate_on_optimize=_flag(box.get("gate_on_optimize"), base.gate_on_optimize),
            profit_factor_min=_clamp(
                _num(box.get("profit_factor_min"), base.profit_factor_min), 0.0, 5.0
            ),
            profit_factor_min_losses=max(
                0,
                _int(
                    box.get("profit_factor_min_losses"), base.profit_factor_min_losses
                ),
            ),
            profit_factor_excellent=_clamp(
                _num(box.get("profit_factor_excellent"), base.profit_factor_excellent), 0.0, 10.0
            ),
            r_expect_min=_clamp(_num(box.get("r_expect_min"), base.r_expect_min), -1.0, 2.0),
            r_expect_excellent=_clamp(
                _num(box.get("r_expect_excellent"), base.r_expect_excellent), -1.0, 5.0
            ),
            calmar_min=_clamp(_num(box.get("calmar_min"), base.calmar_min), 0.0, 10.0),
            calmar_excellent=_clamp(
                _num(box.get("calmar_excellent"), base.calmar_excellent), 0.0, 20.0
            ),
            calmar_red=_clamp(_num(box.get("calmar_red"), base.calmar_red), 0.0, 10.0),
            max_drawdown_pct=_clamp(
                _num(box.get("max_drawdown_pct"), base.max_drawdown_pct), 0.0, 1.0
            ),
            max_drawdown_pct_excellent=_clamp(
                _num(box.get("max_drawdown_pct_excellent"), base.max_drawdown_pct_excellent),
                0.0,
                1.0,
            ),
            max_drawdown_pct_red=_clamp(
                _num(box.get("max_drawdown_pct_red"), base.max_drawdown_pct_red), 0.0, 1.0
            ),
            profit_cover_min=_num(box.get("profit_cover_min"), base.profit_cover_min),
            trades_min=max(0, _int(box.get("trades_min"), base.trades_min)),
            trades_observe=max(0, _int(box.get("trades_observe"), base.trades_observe)),
            trades_excellent=max(0, _int(box.get("trades_excellent"), base.trades_excellent)),
            max_consecutive_losses=max(
                0, _int(box.get("max_consecutive_losses"), base.max_consecutive_losses)
            ),
            max_consecutive_losses_excellent=max(
                0,
                _int(
                    box.get("max_consecutive_losses_excellent"),
                    base.max_consecutive_losses_excellent,
                ),
            ),
            range_pf_max=_num(box.get("range_pf_max"), base.range_pf_max),
            range_trade_share_max=_clamp(
                _num(box.get("range_trade_share_max"), base.range_trade_share_max), 0.0, 1.0
            ),
            slippage_extra_ticks=max(
                0.0, _num(box.get("slippage_extra_ticks"), base.slippage_extra_ticks)
            ),
            slippage_pf_min=_num(box.get("slippage_pf_min"), base.slippage_pf_min),
            slippage_pf_red=_num(box.get("slippage_pf_red"), base.slippage_pf_red),
            perturbation_step_pct=_clamp(
                _num(box.get("perturbation_step_pct"), base.perturbation_step_pct), 0.0, 0.5
            ),
            perturbation_pf_ratio=_clamp(
                _num(box.get("perturbation_pf_ratio"), base.perturbation_pf_ratio), 0.0, 2.0
            ),
            perturbation_pf_min=_num(
                box.get("perturbation_pf_min"), base.perturbation_pf_min
            ),
            stress_min_trades=max(0, _int(box.get("stress_min_trades"), base.stress_min_trades)),
        )

    def to_cfg(self) -> dict[str, Any]:
        """写回配置页用。字段顺序和 config.yaml 保持一致。"""
        return {
            "enabled": self.enabled,
            "adx_period": self.adx_period,
            "adx_trend_min": self.adx_trend_min,
            "stress_on_backtest": self.stress_on_backtest,
            "gate_on_optimize": self.gate_on_optimize,
            "stress_min_trades": self.stress_min_trades,
            "profit_factor_min": self.profit_factor_min,
            "profit_factor_min_losses": self.profit_factor_min_losses,
            "r_expect_min": self.r_expect_min,
            "calmar_min": self.calmar_min,
            "max_drawdown_pct": self.max_drawdown_pct,
            "profit_cover_min": self.profit_cover_min,
            "trades_min": self.trades_min,
            "trades_observe": self.trades_observe,
            "max_consecutive_losses": self.max_consecutive_losses,
            "range_pf_max": self.range_pf_max,
            "range_trade_share_max": self.range_trade_share_max,
            "slippage_extra_ticks": self.slippage_extra_ticks,
            "slippage_pf_min": self.slippage_pf_min,
            "perturbation_step_pct": self.perturbation_step_pct,
            "perturbation_pf_ratio": self.perturbation_pf_ratio,
            "perturbation_pf_min": self.perturbation_pf_min,
        }


@dataclass
class EvaluationCheck:
    """一项检查。value 是给人看的字符串，原始数字放在 EvaluationReport.metrics。"""

    key: str
    label: str
    value: str
    threshold: str
    status: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "threshold": self.threshold,
            "status": self.status,
            "status_text": STATUS_TEXT.get(self.status, self.status),
            "note": self.note,
        }

    def line(self) -> str:
        """写成报告里的一行。"""
        status = STATUS_TEXT.get(self.status, self.status)
        tail = f"{status}  {self.note}" if self.note else status
        return f"{self.label}  {self.value}  {self.threshold}  {tail}".strip()


@dataclass
class EvaluationReport:
    checks: list[EvaluationCheck] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    conclusion: str = "watch"
    skipped: str = ""

    def add_check(self, check: EvaluationCheck) -> None:
        self.checks.append(check)

    def recompute(self) -> None:
        statuses = {c.status for c in self.checks}
        if FAIL in statuses:
            self.conclusion = "reject"
        elif WATCH in statuses:
            self.conclusion = "watch"
        else:
            self.conclusion = "pass"

    def failed_labels(self) -> list[str]:
        return [c.label for c in self.checks if c.status == FAIL]

    def watch_labels(self) -> list[str]:
        return [c.label for c in self.checks if c.status == WATCH]

    def failed_text(self) -> str:
        return "、".join(self.failed_labels())

    def watch_text(self) -> str:
        return "、".join(self.watch_labels())

    def summary_line(self) -> str:
        if self.skipped:
            return f"策略评估  —（{self.skipped}）"
        text = f"策略评估  {CONCLUSION_TEXT.get(self.conclusion, self.conclusion)}"
        fails = self.failed_labels()
        watches = self.watch_labels()
        if fails:
            text += f"（红线：{'、'.join(fails)}）"
        elif watches:
            text += f"（观察：{'、'.join(watches)}）"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "conclusion_text": CONCLUSION_TEXT.get(self.conclusion, self.conclusion),
            "skipped": self.skipped,
            "metrics": {
                key: round(float(val), 6) if isinstance(val, (int, float)) else val
                for key, val in self.metrics.items()
            },
            "checks": [c.to_dict() for c in self.checks],
            "failed_items": self.failed_labels(),
            "watch_items": self.watch_labels(),
        }


def verdict_text(report: EvaluationReport | None) -> str:
    """给表格用的短结论。"""
    if report is None:
        return "未评估"
    if report.skipped:
        return "未评估"
    return CONCLUSION_TEXT.get(report.conclusion, report.conclusion)


def verdict_tag(report: EvaluationReport | None) -> str:
    """带原因数量的短标签，例如「淘汰(3项红线)」。"""
    text = verdict_text(report)
    if report is None or report.skipped:
        return text
    fails = len(report.failed_labels())
    watches = len(report.watch_labels())
    if fails:
        return f"{text}({fails}项红线)"
    if watches:
        return f"{text}({watches}项观察)"
    return text


def _parse_when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("/", "-"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def max_consecutive_losses(trades: list[TradeRecord]) -> int:
    """连续亏损的最长串。按平仓时间排序后数 pnl<=0。"""
    ordered = sorted(trades, key=lambda t: str(getattr(t, "exit_time", "") or ""))
    longest = 0
    current = 0
    for trade in ordered:
        if float(getattr(trade, "pnl", 0) or 0) <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def drawdown_duration(equity: list[float], times: list[str] | None = None) -> int:
    """最大回撤从峰值到谷底隔了多少个自然日。没有时间轴时返回 0。"""
    if not equity or not times or len(times) != len(equity):
        return 0
    peak = float(equity[0])
    peak_at = _parse_when(times[0])
    worst = 0.0
    worst_span = 0
    for value, stamp in zip(equity, times):
        number = float(value or 0)
        moment = _parse_when(stamp)
        if number >= peak:
            peak = number
            peak_at = moment
            continue
        drop = peak - number
        if drop > worst:
            worst = drop
            if peak_at is not None and moment is not None:
                worst_span = max(0, (moment - peak_at).days)
    return worst_span


def _span_years(start: Any, end: Any) -> float | None:
    """覆盖区间折成年。算不出来时返回 None，别拿 0 年去除。"""
    first = _parse_when(start)
    last = _parse_when(end)
    if first is None or last is None:
        return None
    days = (last - first).total_seconds() / 86400.0
    if days <= 0:
        return None
    return days / 365.0


def annual_return(net_pnl: float, capital: float, start: Any, end: Any) -> float:
    """按自然日折算年化。不足一天按一天算。"""
    first = _parse_when(start)
    last = _parse_when(end)
    if first is None or last is None:
        return 0.0
    days = max(1, (last - first).days)
    base = float(capital or 0)
    if base <= 0:
        return 0.0
    return (float(net_pnl or 0) / base) * (252.0 / days)


def adx_groups(
    trades: list[TradeRecord],
    trend_min: float,
) -> dict[str, Any] | None:
    """按开仓 ADX 分趋势组/震荡组。全都没有 ADX 快照时返回 None。"""
    with_adx = [
        trade
        for trade in trades
        if getattr(trade, "adx_at_entry", None) is not None
    ]
    if not with_adx:
        return None
    trend = [t for t in with_adx if float(t.adx_at_entry or 0) >= trend_min]
    range_side = [t for t in with_adx if float(t.adx_at_entry or 0) < trend_min]
    trend_perf = calc_performance(trend, _equity_from_trades(trend), min_trades=2)
    range_perf = calc_performance(range_side, _equity_from_trades(range_side), min_trades=2)
    total = len(with_adx)
    return {
        "total": total,
        "trend_trades": len(trend),
        "trend_pf": float(trend_perf.profit_factor),
        "trend_losses": sum(1 for t in trend if float(getattr(t, "pnl", 0) or 0) < 0),
        "trend_net": float(trend_perf.net_pnl),
        "trend_win_rate": float(trend_perf.win_rate),
        "range_trades": len(range_side),
        "range_pf": float(range_perf.profit_factor),
        "range_losses": sum(1 for t in range_side if float(getattr(t, "pnl", 0) or 0) < 0),
        "range_net": float(range_perf.net_pnl),
        "range_win_rate": float(range_perf.win_rate),
        "range_share": (len(range_side) / total) if total else 0.0,
        "covered": total,
        "missing": len(trades) - total,
    }


def _equity_from_trades(trades: list[TradeRecord]) -> list[float]:
    """没有原始权益曲线时，用成交盈亏累加一条，只用于分组内的回撤/得分计算。"""
    total = 0.0
    curve = [0.0]
    for trade in trades:
        total += float(getattr(trade, "pnl", 0) or 0)
        curve.append(total)
    return curve


def _capital_for(snapshot: dict[str, Any]) -> float:
    """回测本金。为 0 时退回峰值保证金，保证 Calmar 不除零。"""
    capital = float(snapshot.get("capital") or 0)
    if capital > 0:
        return capital
    margin = float(snapshot.get("margin_used") or 0)
    return margin if margin > 0 else 1.0


def _ratio_text(value: float) -> str:
    if math.isinf(value):
        return "∞"
    return f"{value:.2f}"


def _pct_text(value: float) -> str:
    return f"{value:.1%}"


def _pf_status(pf: float, th: EvaluationThresholds) -> tuple[str, str]:
    if pf < th.profit_factor_min:
        return FAIL, f"低于 {th.profit_factor_min:.2f}"
    if pf >= th.profit_factor_excellent:
        return PASS, "优秀"
    return PASS, f"达标，未到优秀 {th.profit_factor_excellent:.2f}"


def _check_sample(
    count: int,
    th: EvaluationThresholds,
    years: float | None = None,
) -> EvaluationCheck:
    """
    样本只作参考，不判红线。

    笔数少说明统计显著性不足，不代表策略无效；长线策略一年只有几次开仓也正常。
    低于参考线最多把结论压到“观察”，并写清覆盖年限和每年笔数，方便判断样本密度。
    """
    pace = ""
    if years is not None and years >= 0.1:
        pace = f"，覆盖 {years:.1f} 年约 {count / years:.0f} 笔/年"
    if count < th.trades_observe:
        status = WATCH
        note = (
            f"样本偏少{pace}，参考 {th.trades_observe}～{th.trades_min} 笔；"
            "统计显著性不足，结论仅供参考"
        )
    elif count < th.trades_min:
        status = WATCH
        note = f"只够观察{pace}，参考 ≥{th.trades_min} 笔"
    elif count >= th.trades_excellent:
        status, note = PASS, "优秀"
    else:
        status, note = PASS, ""
    return EvaluationCheck(
        key="sample",
        label="样本",
        value=f"{count}笔",
        threshold=f"参考 ≥{th.trades_min}",
        status=status,
        note=note,
    )


def _check_profit_factor(
    pf: float, th: EvaluationThresholds, losses: int = 0
) -> EvaluationCheck:
    # 一笔亏损都没有时 PF 只是个哨兵值（99），不能拿它去和门槛比，也不该显示成真实优势。
    if losses < th.profit_factor_min_losses:
        return EvaluationCheck(
            key="profit_factor",
            label="盈利因子",
            value=_ratio_text(pf) if pf > 0 else "—",
            threshold=f"≥{th.profit_factor_min:.2f}",
            status=SKIP,
            note=f"亏损样本只有 {losses} 笔，盈利因子不参与判定",
        )
    status, note = _pf_status(pf, th)
    return EvaluationCheck(
        key="profit_factor",
        label="盈利因子",
        value=_ratio_text(pf),
        threshold=f"≥{th.profit_factor_min:.2f}",
        status=status,
        note=note,
    )


def _check_r_expectancy(value: float, win_rate: float, avg_win: float, avg_loss: float,
                        th: EvaluationThresholds) -> EvaluationCheck:
    if avg_loss <= 1e-9:
        return EvaluationCheck(
            key="r_expectancy",
            label="收益期望",
            value="—",
            threshold=f"≥{th.r_expect_min:.2f}",
            status=SKIP,
            note="没有亏损笔，算不出",
        )
    if value < th.r_expect_min:
        status, note = WATCH, "薄期望，容错不足"
    elif value >= th.r_expect_excellent:
        status, note = PASS, "优秀"
    else:
        status, note = PASS, ""
    return EvaluationCheck(
        key="r_expectancy",
        label="收益期望",
        value=f"{value:.2f}",
        threshold=f"≥{th.r_expect_min:.2f}",
        status=status,
        note=note,
    )


def _check_calmar(value: float | None, th: EvaluationThresholds) -> EvaluationCheck:
    if value is None or not math.isfinite(value):
        return EvaluationCheck(
            key="calmar",
            label="卡尔玛",
            value="—",
            threshold=f"≥{th.calmar_min:.2f}",
            status=SKIP,
            note="没有回撤，算不出",
        )
    if value < th.calmar_red:
        status, note = FAIL, f"低于红线 {th.calmar_red:.2f}"
    elif value < th.calmar_min:
        status, note = WATCH, f"未到 {th.calmar_min:.2f}"
    elif value >= th.calmar_excellent:
        status, note = PASS, "优秀"
    else:
        status, note = PASS, ""
    return EvaluationCheck(
        key="calmar",
        label="卡尔玛",
        value=_ratio_text(value),
        threshold=f"≥{th.calmar_min:.2f}",
        status=status,
        note=note,
    )


def _check_drawdown(pct: float, th: EvaluationThresholds) -> EvaluationCheck:
    if pct > th.max_drawdown_pct_red:
        status, note = FAIL, f"高于红线 {th.max_drawdown_pct_red:.0%}"
    elif pct > th.max_drawdown_pct:
        status, note = WATCH, f"高于 {th.max_drawdown_pct:.0%}"
    elif pct <= th.max_drawdown_pct_excellent:
        status, note = PASS, "优秀"
    else:
        status, note = PASS, ""
    return EvaluationCheck(
        key="drawdown",
        label="最大回撤",
        value=_pct_text(pct),
        threshold=f"≤{th.max_drawdown_pct:.0%}",
        status=status,
        note=note,
    )


def _check_cover(cover: float, th: EvaluationThresholds) -> EvaluationCheck:
    status = WATCH if cover <= th.profit_cover_min else PASS
    note = "净赚盖不住最大回撤" if cover <= th.profit_cover_min else ""
    return EvaluationCheck(
        key="profit_cover",
        label="盈利覆盖",
        value=f"{cover:.0f}元",
        threshold=f">{th.profit_cover_min:.0f}",
        status=status,
        note=note,
    )


def _check_streak(streak: int, th: EvaluationThresholds) -> EvaluationCheck:
    if streak > th.max_consecutive_losses:
        status, note = WATCH, f"超过 {th.max_consecutive_losses} 笔"
    elif streak <= th.max_consecutive_losses_excellent:
        status, note = PASS, "优秀"
    else:
        status, note = PASS, ""
    return EvaluationCheck(
        key="loss_streak",
        label="最大连亏",
        value=f"{streak}笔",
        threshold=f"≤{th.max_consecutive_losses}",
        status=status,
        note=note,
    )


def _check_range(groups: dict[str, Any] | None, th: EvaluationThresholds) -> EvaluationCheck:
    if groups is None:
        return EvaluationCheck(
            key="range_group",
            label="震荡组",
            value="—",
            threshold=f"PF≤{th.range_pf_max:.2f}",
            status=SKIP,
            note="成交里没有 ADX 快照",
        )
    count = int(groups.get("range_trades") or 0)
    share = float(groups.get("range_share") or 0)
    pf = float(groups.get("range_pf") or 0)
    range_losses = int(groups.get("range_losses") or 0)
    value = f"{count}笔({share:.0%}) PF{pf:.2f}"
    if count and range_losses < th.profit_factor_min_losses:
        return EvaluationCheck(
            key="range_group",
            label="震荡组",
            value=value,
            threshold=f"PF≤{th.range_pf_max:.2f}",
            status=SKIP,
            note="震荡组亏损样本太少，PF 不参与判定",
        )
    if count and pf > th.range_pf_max and share > th.range_trade_share_max:
        return EvaluationCheck(
            key="range_group",
            label="震荡组",
            value=value,
            threshold=f"PF≤{th.range_pf_max:.2f}",
            status=FAIL,
            note="震荡里开仓偏多且赚钱，ADX 过滤可能失效",
        )
    if count and pf > th.range_pf_max:
        return EvaluationCheck(
            key="range_group",
            label="震荡组",
            value=value,
            threshold=f"PF≤{th.range_pf_max:.2f}",
            status=WATCH,
            note="震荡组仍赚钱，注意是否过滤不足",
        )
    return EvaluationCheck(
        key="range_group",
        label="震荡组",
        value=value,
        threshold=f"PF≤{th.range_pf_max:.2f}",
        status=PASS,
        note="没有震荡组" if not count else "",
    )


def check_slippage(
    profit_factor: float,
    *,
    base_ticks: float,
    stress_ticks: float,
    thresholds: EvaluationThresholds,
    error: str = "",
) -> EvaluationCheck:
    """滑点 +N 跳后的盈利因子。"""
    if error:
        return EvaluationCheck(
            key="slippage",
            label="滑点压力",
            value="—",
            threshold=f"PF≥{thresholds.slippage_pf_min:.2f}",
            status=SKIP,
            note=error,
        )
    if profit_factor < thresholds.slippage_pf_red:
        status, note = FAIL, f"掉到 {thresholds.slippage_pf_red:.2f} 以下"
    elif profit_factor < thresholds.slippage_pf_min:
        status, note = WATCH, f"低于 {thresholds.slippage_pf_min:.2f}"
    else:
        status, note = PASS, ""
    return EvaluationCheck(
        key="slippage",
        label="滑点压力",
        value=f"PF {profit_factor:.2f}（+{stress_ticks - base_ticks:g}跳）",
        threshold=f"PF≥{thresholds.slippage_pf_min:.2f}",
        status=status,
        note=note,
    )


def check_perturbation(
    *,
    cliff: bool,
    worst_pf: float,
    points: int,
    thresholds: EvaluationThresholds,
    error: str = "",
) -> EvaluationCheck:
    """参数在附近扰动后的最差盈利因子。"""
    if error:
        return EvaluationCheck(
            key="perturbation",
            label="参数扰动",
            value="—",
            threshold="不出现断崖",
            status=SKIP,
            note=error,
        )
    if cliff:
        status, note = FAIL, "参数稍微一动绩效就崩"
    else:
        status, note = PASS, f"{points} 个扰动点都还站得住"
    return EvaluationCheck(
        key="perturbation",
        label="参数扰动",
        value=f"最差PF {worst_pf:.2f}",
        threshold="不出现断崖",
        status=status,
        note=note,
    )


def check_joint_perturbation(
    *,
    cliff: bool,
    worst_pf: float,
    points: int,
    thresholds: EvaluationThresholds,
    error: str = "",
) -> EvaluationCheck:
    """多个参数一起扰动后的最差盈利因子。"""
    if error:
        return EvaluationCheck(
            key="joint_perturbation",
            label="联合扰动",
            value="—",
            threshold="不出现断崖",
            status=SKIP,
            note=error,
        )
    if cliff:
        status, note = FAIL, "两个参数一起动就崩"
    else:
        status, note = PASS, f"{points} 组组合都还站得住"
    return EvaluationCheck(
        key="joint_perturbation",
        label="联合扰动",
        value=f"最差PF {worst_pf:.2f}",
        threshold="不出现断崖",
        status=status,
        note=note,
    )


def _evaluate_core(
    perf: Performance,
    trades: list[TradeRecord],
    *,
    equity: list[float] | None = None,
    times: list[str] | None = None,
    snapshot: dict[str, Any] | None = None,
    thresholds: EvaluationThresholds,
    profile: str = "backtest",
) -> EvaluationReport:
    """回测和当日重放共用的检查流程。profile=day 时跳过样本/资金类门槛。"""
    th = thresholds
    snap = snapshot or {}
    report = EvaluationReport()
    r_value = unit_expectancy(perf.win_rate, perf.avg_win, perf.avg_loss)
    capital = _capital_for(snap)
    start = (times[0] if times else "") or str(snap.get("data_from") or "")
    end = (times[-1] if times else "") or str(snap.get("data_to") or "")
    years = _span_years(start, end)
    annual = annual_return(perf.net_pnl, capital, start, end)
    # 卡尔玛的分子是「按本金折算的年化」，分母也必须用「按本金算的回撤」，
    # 不能用峰值口径的最大回撤%，否则分子分母基准不一致。
    dd_on_capital = (float(perf.max_drawdown) / capital) if capital > 0 else 0.0
    calmar: float | None = annual / dd_on_capital if dd_on_capital > 1e-9 else None
    cover = float(perf.net_pnl) - float(perf.max_drawdown)
    streak = max_consecutive_losses(trades)
    groups = adx_groups(trades, th.adx_trend_min)
    dd_days = drawdown_duration(equity or [], times)

    report.metrics.update(
        {
            "net_pnl": float(perf.net_pnl),
            "profit_factor": float(perf.profit_factor),
            # 对外只叫收益期望；r_expectancy 留给旧报告读同一数字。
            "expectancy": float(r_value),
            "r_expectancy": float(r_value),
            "annual_return": float(annual),
            "calmar": None if calmar is None else float(calmar),
            "max_drawdown": float(perf.max_drawdown),
            "max_drawdown_pct": float(perf.max_drawdown_pct),
            # 批次1可信度底座新增：每笔盈亏口径的风险指标（只展示，不参与门禁）。
            "sharpe": float(perf.sharpe),
            "sortino": float(perf.sortino),
            "sharpe_annualized": bool(getattr(perf, "sharpe_annualized", False)),
            "ulcer": float(perf.ulcer),
            "exposure": float(perf.exposure),
            "turnover": float(perf.turnover),
            "cost_ratio": float(perf.cost_ratio),
            "profit_cover": cover,
            "consecutive_losses": float(streak),
            "drawdown_days": float(dd_days),
            "trades": float(perf.trades),
        }
    )
    if groups is not None:
        report.metrics.update(
            {
                "range_trades": float(groups["range_trades"]),
                "range_pf": float(groups["range_pf"]),
                "range_share": float(groups["range_share"]),
                "trend_trades": float(groups["trend_trades"]),
                "trend_pf": float(groups["trend_pf"]),
            }
        )
    if years is not None and years >= 0.1:
        # 太短的区间折成“每年笔数”会失真，只保留长度够用的。
        report.metrics["years"] = float(years)
        report.metrics["trades_per_year"] = float(perf.trades) / years

    if profile == "day":
        report.add_check(_check_profit_factor(float(perf.profit_factor), th, int(perf.losses)))
        report.add_check(
            _check_r_expectancy(r_value, perf.win_rate, perf.avg_win, perf.avg_loss, th)
        )
        report.add_check(_check_streak(streak, th))
        report.add_check(_check_range(groups, th))
        report.add_check(
            EvaluationCheck(
                key="sample",
                label="样本",
                value=f"{perf.trades}笔",
                threshold=f"参考 ≥{th.trades_min}",
                status=SKIP,
                note="当日重放样本天然少，不参与判定",
            )
        )
        report.add_check(_skip_check("calmar", "卡尔玛", "当日重放不折算年化"))
        report.add_check(_skip_check("drawdown", "最大回撤", "当日重放不做资金门槛"))
        report.add_check(_skip_check("profit_cover", "盈利覆盖", "当日重放不做资金门槛"))
        report.recompute()
        return report

    report.add_check(_check_sample(int(perf.trades), th, years))
    report.add_check(_check_profit_factor(float(perf.profit_factor), th, int(perf.losses)))
    report.add_check(
        _check_r_expectancy(r_value, perf.win_rate, perf.avg_win, perf.avg_loss, th)
    )
    report.add_check(_check_calmar(calmar, th))
    report.add_check(_check_drawdown(float(perf.max_drawdown_pct), th))
    report.add_check(_check_cover(cover, th))
    report.add_check(_check_streak(streak, th))
    report.add_check(_check_range(groups, th))
    report.recompute()
    return report


def _skip_check(key: str, label: str, note: str) -> EvaluationCheck:
    return EvaluationCheck(key=key, label=label, value="—", threshold="", status=SKIP, note=note)


def evaluate_backtest(
    result: Any,
    *,
    thresholds: EvaluationThresholds | None = None,
) -> EvaluationReport:
    """按一个 BacktestResult 出评估。用 duck typing，避免和 engine 循环 import。"""
    th = thresholds or EvaluationThresholds()
    if not th.enabled:
        return EvaluationReport(skipped="策略评估未启用")
    perf = getattr(result, "performance", None)
    trades = list(getattr(result, "trades", []) or [])
    if perf is None or not trades:
        return EvaluationReport(skipped="没有成交，无法评估")
    return _evaluate_core(
        perf,
        trades,
        equity=list(getattr(result, "equity", []) or []),
        times=list(getattr(result, "equity_times", []) or []),
        snapshot=dict(getattr(result, "params_snapshot", {}) or {}),
        thresholds=th,
    )


def evaluate_day(
    trades: list[TradeRecord],
    *,
    thresholds: EvaluationThresholds | None = None,
) -> EvaluationReport:
    """复盘当日重放：只判 PF、R 期望、连亏、ADX 分组。"""
    th = thresholds or EvaluationThresholds()
    if not th.enabled:
        return EvaluationReport(skipped="策略评估未启用")
    if not trades:
        return EvaluationReport(skipped="当天没有重放成交")
    curve = _equity_from_trades(trades)
    perf = calc_performance(trades, curve, min_trades=2)
    return _evaluate_core(
        perf,
        trades,
        equity=curve,
        snapshot={},
        thresholds=th,
        profile="day",
    )


def evaluate_perf(
    perf: Performance,
    trades: list[TradeRecord],
    *,
    snapshot: dict[str, Any] | None = None,
    thresholds: EvaluationThresholds | None = None,
) -> EvaluationReport:
    """已经算好 Performance 的场景（例如优化里的训练/检验段）。"""
    th = thresholds or EvaluationThresholds()
    if not th.enabled:
        return EvaluationReport(skipped="策略评估未启用")
    if not trades:
        return EvaluationReport(skipped="没有成交，无法评估")
    return _evaluate_core(
        perf,
        trades,
        snapshot=snapshot or {},
        thresholds=th,
    )


def mark_stress_not_run(report: EvaluationReport, *, reason: str) -> EvaluationReport:
    """交叉测试等场景不跑压力项时，在报告里标明跳过，避免被当成已经过关。"""
    if report.skipped:
        return report
    existing = {c.key for c in report.checks}
    if "slippage" not in existing:
        report.add_check(_skip_check("slippage", "滑点压力", reason))
    if "perturbation" not in existing:
        report.add_check(_skip_check("perturbation", "参数扰动", reason))
    if "joint_perturbation" not in existing:
        report.add_check(_skip_check("joint_perturbation", "联合扰动", reason))
    # 跳过项不参与结论。不要 recompute：测试/外部可能只写了 conclusion、还没填检查项。
    return report


def apply_stress(
    report: EvaluationReport,
    *,
    slippage: dict[str, Any] | None = None,
    perturbation: dict[str, Any] | None = None,
    joint: dict[str, Any] | None = None,
    thresholds: EvaluationThresholds | None = None,
) -> EvaluationReport:
    """把外部跑出来的滑点/扰动/联合扰动结果并进评估，再重算结论。"""
    th = thresholds or EvaluationThresholds()
    # 只替换本次传入的压力项。连续调用滑点和扰动时，不能把上一次的结果一起删掉。
    drop = set()
    if slippage is not None:
        drop.add("slippage")
    if perturbation is not None:
        drop.add("perturbation")
    if joint is not None:
        drop.add("joint_perturbation")
    report.checks = [c for c in report.checks if c.key not in drop]
    if slippage is not None:
        report.add_check(
            check_slippage(
                float(slippage.get("profit_factor") or 0),
                base_ticks=float(slippage.get("base_ticks") or 0),
                stress_ticks=float(slippage.get("stress_ticks") or 0),
                thresholds=th,
                error=str(slippage.get("error") or ""),
            )
        )
        if slippage.get("profit_factor") is not None:
            report.metrics["slippage_profit_factor"] = float(
                slippage.get("profit_factor") or 0
            )
            report.metrics["slippage_base_ticks"] = float(slippage.get("base_ticks") or 0)
            report.metrics["slippage_stress_ticks"] = float(
                slippage.get("stress_ticks") or 0
            )
            report.metrics["slippage_net_pnl"] = float(slippage.get("net_pnl") or 0)
            report.metrics["slippage_trades"] = float(slippage.get("trades") or 0)
    if perturbation is not None:
        report.add_check(
            check_perturbation(
                cliff=bool(perturbation.get("cliff")),
                worst_pf=float(perturbation.get("worst_pf") or 0),
                points=int(perturbation.get("points_count") or 0),
                thresholds=th,
                error=str(perturbation.get("error") or ""),
            )
        )
        if perturbation.get("worst_pf") is not None:
            report.metrics["perturbation_worst_pf"] = float(
                perturbation.get("worst_pf") or 0
            )
    if joint is not None:
        report.add_check(
            check_joint_perturbation(
                cliff=bool(joint.get("cliff")),
                worst_pf=float(joint.get("worst_pf") or 0),
                points=int(joint.get("points_count") or 0),
                thresholds=th,
                error=str(joint.get("error") or ""),
            )
        )
        if joint.get("worst_pf") is not None:
            report.metrics["joint_perturbation_worst_pf"] = float(
                joint.get("worst_pf") or 0
            )
    report.recompute()
    return report


def format_evaluation_lines(
    report: EvaluationReport | dict[str, Any] | None,
    *,
    title: str = "策略评估",
) -> list[str]:
    """写成报告里的几行。传 dict（例如 JSON 里的 evaluation）也能用。"""
    if report is None:
        return [f"—— {title} ——", "（未评估）"]
    if isinstance(report, dict):
        report = _report_from_dict(report)
    if report.skipped:
        return [f"—— {title} ——", f"（未评估：{report.skipped}）"]
    head = f"—— {title}（结论：{CONCLUSION_TEXT.get(report.conclusion, report.conclusion)}）——"
    return [head, *[check.line() for check in report.checks]]


def _report_from_dict(data: dict[str, Any]) -> EvaluationReport:
    """把 to_dict 的结果还原成报告，只用于展示。"""
    checks = [
        EvaluationCheck(
            key=str(item.get("key") or ""),
            label=str(item.get("label") or ""),
            value=str(item.get("value") or ""),
            threshold=str(item.get("threshold") or ""),
            status=str(item.get("status") or SKIP),
            note=str(item.get("note") or ""),
        )
        for item in (data.get("checks") or [])
        if isinstance(item, dict)
    ]
    metrics = {
        str(key): float(value)
        for key, value in (data.get("metrics") or {}).items()
        if isinstance(value, (int, float))
    }
    return EvaluationReport(
        checks=checks,
        metrics=metrics,
        conclusion=str(data.get("conclusion") or "watch"),
        skipped=str(data.get("skipped") or ""),
    )
