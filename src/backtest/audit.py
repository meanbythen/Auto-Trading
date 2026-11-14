"""对已读取的回测、优化和配置字典做只读审计。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["critical", "warning", "info"]
REVIEW_NOTE = "30笔、50%和80%仅为人工复核提示线，非统计显著性判断。"


@dataclass(frozen=True)
class AuditFinding:
    source: str
    code: str
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class AuditReport:
    findings: list[AuditFinding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            severity: sum(finding.severity == severity for finding in self.findings)
            for severity in ("critical", "warning", "info")
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "findings": [finding.to_dict() for finding in self.findings],
            "metrics": self.metrics,
            "review_note": REVIEW_NOTE,
        }


def _box(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def _same_text(left: Any, right: Any) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _audit_stress(
    source: str,
    data: dict[str, Any],
    evaluation: dict[str, Any],
    trades: int,
    min_trades: int,
    findings: list[AuditFinding],
) -> None:
    raw_checks = evaluation.get("checks", [])
    checks = [check for check in raw_checks if isinstance(check, dict)] if isinstance(raw_checks, list) else []
    for kind, label in (("slippage", "滑点压力"), ("perturbation", "参数扰动")):
        stress = _box(data.get(kind))
        check = next((item for item in checks if item.get("key") == kind), None)
        details: dict[str, Any] = {
            "kind": kind,
            "trades": trades,
            "stress_min_trades": min_trades,
        }
        if kind == "slippage":
            base_ticks = _number(_box(evaluation.get("metrics")).get("slippage_base_ticks"))
            if base_ticks is not None and base_ticks <= 0:
                findings.append(
                    AuditFinding(
                        source,
                        "slippage_zero_base",
                        "warning",
                        "基准滑点为 0，压力结果只代表额外加跳，不能当作含真实滑点的绩效。",
                        {"base_ticks": base_ticks},
                    )
                )
        error = str(stress.get("error") or "").strip()
        if error:
            details["error"] = error
            findings.append(
                AuditFinding(
                    source,
                    "stress_error",
                    "warning",
                    f"{label}执行失败：{error}，需人工复核压力证据。",
                    details,
                )
            )
            continue
        if check is not None and check.get("status") == "skip":
            details.update(
                {
                    "value": check.get("value", ""),
                    "threshold": check.get("threshold", ""),
                    "note": check.get("note", ""),
                }
            )
            findings.append(
                AuditFinding(
                    source,
                    "stress_check_skipped",
                    "warning",
                    f"{label}对应评估项为 skip，当前成交数已达到压力测试门槛，需补齐证据。",
                    details,
                )
            )
            continue
        if not stress:
            # 有些结果只保存评估明细；有效的 pass/watch/fail 已足以证明执行过。
            if check is None:
                findings.append(
                    AuditFinding(
                        source,
                        "stress_missing",
                        "warning",
                        f"{label}结果和对应评估项均缺失，当前成交数已达到压力测试门槛。",
                        details,
                    )
                )
            continue
        if kind == "perturbation":
            points = stress.get("points")
            if isinstance(points, list):
                valid_points = sum(
                    isinstance(point, dict)
                    and (
                        _number(point.get("profit_factor")) is not None
                        or _number(point.get("net_pnl")) is not None
                    )
                    for point in points
                )
            else:
                valid_points = int(_number(stress.get("points_count")) or 0)
            details["valid_points"] = valid_points
            details["cliff"] = bool(stress.get("cliff"))
            if valid_points == 0:
                findings.append(
                    AuditFinding(
                        source,
                        "stress_no_valid_points",
                        "warning",
                        "参数扰动没有有效扰动点，需人工复核执行证据；cliff 本身不等于执行失败。",
                        details,
                    )
                )
        elif _number(stress.get("profit_factor")) is None and check is None:
            findings.append(
                AuditFinding(
                    source,
                    "stress_no_valid_result",
                    "warning",
                    "滑点压力没有有效盈利因子结果，需人工复核执行证据。",
                    details,
                )
            )


def audit_results(
    backtest: dict[str, Any],
    optimize: dict[str, Any],
    config: dict[str, Any],
) -> AuditReport:
    """只读检查三份已反序列化的数据，不修改输入，也不访问外部资源。"""
    findings: list[AuditFinding] = []
    metrics: dict[str, Any] = {}
    accepted = _flag(optimize.get("accepted"), False)
    evaluations: dict[str, dict[str, Any]] = {}

    for source, data in (("backtest", backtest), ("optimize", optimize)):
        raw_evaluation = data.get("evaluation")
        evaluation = _box(raw_evaluation)
        evaluations[source] = evaluation
        if not evaluation:
            findings.append(
                AuditFinding(
                    source,
                    "evaluation_missing",
                    "warning",
                    "评估结果为空"
                    + ("，但优化结果 accepted=true，禁止直接写入，需核查当时门禁。" if source == "optimize" and accepted else "，可能是旧结果或未启用评估。"),
                    {"accepted": accepted} if source == "optimize" else {},
                )
            )
        skipped = str(evaluation.get("skipped") or "").strip()
        if skipped:
            findings.append(
                AuditFinding(
                    source,
                    "evaluation_skipped",
                    "warning",
                    f"评估报告已跳过：{skipped}"
                    + ("；优化结果仍 accepted=true，禁止直接写入。" if source == "optimize" and accepted else "。"),
                    {
                        "skipped": skipped,
                        **({"accepted": accepted} if source == "optimize" else {}),
                    },
                )
            )
        checks = evaluation.get("checks", [])
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                status = str(check.get("status") or "").strip().lower()
                severity: Severity | None = {
                    "fail": "critical",
                    "watch": "warning",
                    "skip": "info",
                }.get(status)
                if severity is None:
                    continue
                key = str(check.get("key") or "unknown")
                label = str(check.get("label") or key)
                findings.append(
                    AuditFinding(
                        source,
                        f"evaluation_check_{status}",
                        severity,
                        f"评估明细 {label}（{key}）状态为 {status}。",
                        {
                            "key": key,
                            "label": label,
                            "status": status,
                            "value": check.get("value", ""),
                            "threshold": check.get("threshold", ""),
                            "note": check.get("note", ""),
                        },
                    )
                )

    evaluation = evaluations["optimize"]
    evaluation_ok = _flag(optimize.get("evaluation_ok"), True)
    rejected = str(evaluation.get("conclusion") or "").strip().lower() == "reject"
    if accepted and (not evaluation_ok or rejected):
        findings.append(
            AuditFinding(
                "optimize",
                "optimize_gate_conflict",
                "critical",
                "优化结果 accepted=true，但评估未完成或结论为 reject；禁止直接写入，需核查当时门禁。",
                {
                    "accepted": True,
                    "evaluation_ok": evaluation_ok,
                    "conclusion": evaluation.get("conclusion", ""),
                },
            )
        )

    holdout = _box(optimize.get("holdout_performance"))
    holdout_trades_number = _number(holdout.get("trades"))
    if holdout_trades_number is not None:
        holdout_trades = int(holdout_trades_number)
        metrics["holdout_trades"] = holdout_trades
        if holdout_trades == 0:
            findings.append(
                AuditFinding(
                    "optimize",
                    "holdout_zero_trades",
                    "warning",
                    "检验段零成交，不能作为样本外有效性证据。",
                    {"trades": holdout_trades},
                )
            )
        elif 0 < holdout_trades < 30:
            findings.append(
                AuditFinding(
                    "optimize",
                    "holdout_small_sample",
                    "warning",
                    f"检验段仅 {holdout_trades} 笔成交，需人工复核；{REVIEW_NOTE}",
                    {"trades": holdout_trades, "threshold": 30, "note": REVIEW_NOTE},
                )
            )
    holdout_pf = _number(holdout.get("profit_factor"))
    eval_config = _box(config.get("evaluation"))
    pf_min = _number(eval_config.get("profit_factor_min"))
    pf_min = 1.3 if pf_min is None else pf_min
    if holdout_pf is not None:
        metrics["holdout_profit_factor"] = holdout_pf
        if holdout_pf < pf_min:
            findings.append(
                AuditFinding(
                    "optimize",
                    "holdout_low_profit_factor",
                    "critical" if accepted else "warning",
                    f"检验段 PF {holdout_pf:g} 低于当前配置门槛 {pf_min:g}"
                    + ("，但结果仍 accepted=true，禁止直接写入。" if accepted else "，需人工复核。"),
                    {
                        "profit_factor": holdout_pf,
                        "threshold": pf_min,
                        "accepted": accepted,
                    },
                )
            )

    evaluation_enabled = _flag(eval_config.get("enabled"), True)
    stress_enabled = _flag(eval_config.get("stress_on_backtest"), True)
    stress_min_number = _number(eval_config.get("stress_min_trades"))
    stress_min_trades = max(0, int(stress_min_number)) if stress_min_number is not None else 30
    if evaluation_enabled and stress_enabled:
        backtest_trades = int(
            _number(_box(backtest.get("performance")).get("trades"))
            or len(backtest.get("trades", []) if isinstance(backtest.get("trades"), list) else [])
        )
        if backtest_trades >= stress_min_trades:
            _audit_stress(
                "backtest",
                backtest,
                evaluations["backtest"],
                backtest_trades,
                stress_min_trades,
                findings,
            )
        eval_metrics = _box(evaluations["optimize"].get("metrics"))
        optimize_trades_number = _number(eval_metrics.get("trades"))
        if optimize_trades_number is None:
            optimize_trades_number = _number(_box(optimize.get("best_performance")).get("trades"))
        optimize_trades = int(optimize_trades_number or 0)
        if optimize_trades >= stress_min_trades:
            _audit_stress(
                "optimize",
                optimize,
                evaluations["optimize"],
                optimize_trades,
                stress_min_trades,
                findings,
            )

    raw_trades = backtest.get("trades")
    trades = raw_trades if isinstance(raw_trades, list) else []
    pnls = [_number(_box(trade).get("pnl")) or 0.0 for trade in trades]
    wins = sorted((pnl for pnl in pnls if pnl > 0), reverse=True)
    gross_profit = sum(wins)
    top1_profit = wins[0] if wins else 0.0
    top1_share = top1_profit / gross_profit if gross_profit > 0 else 0.0
    net_pnl = sum(pnls)
    net_without_top5 = net_pnl - sum(wins[:5])
    adx_covered = sum(
        _box(trade).get("adx_at_entry") is not None
        and str(_box(trade).get("adx_at_entry")).strip() != ""
        for trade in trades
    )
    adx_coverage = adx_covered / len(trades) if trades else 0.0
    metrics.update(
        {
            "trade_count": len(trades),
            "profitable_trades": len(wins),
            "gross_profit": gross_profit,
            "net_pnl": net_pnl,
            "top1_profit": top1_profit,
            "top1_profit_share": top1_share,
            "net_without_top5": net_without_top5,
            "adx_covered_trades": adx_covered,
            "adx_coverage": adx_coverage,
        }
    )
    same_bar = [
        trade
        for trade in trades
        if str(_box(trade).get("entry_time") or "").strip()
        == str(_box(trade).get("exit_time") or "").strip()
        and str(_box(trade).get("entry_time") or "").strip() != ""
        and abs(_number(_box(trade).get("pnl")) or 0.0) > 0
    ]
    same_bar_pnl = sum(_number(_box(trade).get("pnl")) or 0.0 for trade in same_bar)
    metrics["same_bar_trades"] = len(same_bar)
    metrics["same_bar_pnl"] = round(same_bar_pnl, 2)
    if same_bar:
        findings.append(
            AuditFinding(
                "backtest",
                "same_bar_trade",
                "warning",
                f"有 {len(same_bar)} 笔在同一时间开平，合计盈亏 {same_bar_pnl:g}；"
                "可能是收盘时点开仓后立即强平，需核对成交时点。",
                {
                    "count": len(same_bar),
                    "pnl": round(same_bar_pnl, 2),
                    "net_pnl": net_pnl,
                    "trades": [
                        {
                            "symbol": _box(trade).get("symbol"),
                            "time": _box(trade).get("entry_time"),
                            "pnl": _number(_box(trade).get("pnl")),
                        }
                        for trade in same_bar[:5]
                    ],
                },
            )
        )
    concentrated = len(trades) >= 30 and top1_share > 0.50
    top5_fragile = len(wins) >= 5 and net_without_top5 <= 0
    if concentrated or top5_fragile:
        reasons = []
        if concentrated:
            reasons.append(f"最大盈利笔占总正盈利 {top1_share:.1%}，超过 50%")
        if top5_fragile:
            reasons.append(f"剔除盈利前五笔后净盈亏为 {net_without_top5:g}")
        findings.append(
            AuditFinding(
                "backtest",
                "profit_concentration",
                "warning",
                "盈利集中度需人工复核：" + "；".join(reasons) + f"。{REVIEW_NOTE}",
                {
                    "trade_count": len(trades),
                    "profitable_trades": len(wins),
                    "gross_profit": gross_profit,
                    "top1_profit": top1_profit,
                    "top1_profit_share": top1_share,
                    "net_pnl": net_pnl,
                    "net_without_top5": net_without_top5,
                    "concentrated": concentrated,
                    "top5_fragile": top5_fragile,
                    "note": REVIEW_NOTE,
                },
            )
        )
    if len(trades) >= 30 and adx_coverage < 0.80:
        findings.append(
            AuditFinding(
                "backtest",
                "adx_coverage_low",
                "warning",
                f"ADX 非空覆盖率仅 {adx_coverage:.1%}，低于 80%，需人工复核；{REVIEW_NOTE}",
                {
                    "trade_count": len(trades),
                    "adx_covered_trades": adx_covered,
                    "adx_coverage": adx_coverage,
                    "threshold": 0.80,
                    "note": REVIEW_NOTE,
                },
            )
        )

    current_engine = _box(config.get("strategy")).get("engine")
    current_symbol = _box(config.get("trade")).get("symbol")
    backtest_snapshot = _box(backtest.get("params_snapshot"))
    for source, result_engine, result_symbol in (
        ("backtest", backtest_snapshot.get("engine"), backtest.get("symbol")),
        ("optimize", optimize.get("engine"), optimize.get("symbol")),
    ):
        if current_engine and result_engine and not _same_text(current_engine, result_engine):
            findings.append(
                AuditFinding(
                    source,
                    "config_engine_mismatch",
                    "warning",
                    "结果 engine 与当前配置不一致，不能直接互为样本外证明。",
                    {"current": current_engine, "result": result_engine},
                )
            )
        if current_symbol and result_symbol and not _same_text(current_symbol, result_symbol):
            findings.append(
                AuditFinding(
                    source,
                    "config_symbol_mismatch",
                    "warning",
                    "结果 symbol 与当前配置不一致，不能直接互为样本外证明；不推测连续合约别名。",
                    {"current": current_symbol, "result": result_symbol},
                )
            )

    for source, snapshot in (
        ("backtest", backtest_snapshot),
        ("optimize", _box(optimize.get("best_limits"))),
    ):
        missing = [key for key in ("data_from", "data_to") if not str(snapshot.get(key) or "").strip()]
        if missing:
            findings.append(
                AuditFinding(
                    source,
                    "data_range_missing",
                    "warning",
                    "参数快照缺少实际数据区间"
                    + ("；优化 best_limits 仍表示训练段，需人工核对。" if source == "optimize" else "。"),
                    {"missing_fields": missing, "snapshot": "params_snapshot" if source == "backtest" else "best_limits"},
                )
            )

    performance_blocks = [("backtest", "performance", _box(backtest.get("performance")))]
    performance_blocks.extend(
        ("optimize", name, _box(optimize.get(name)))
        for name in ("best_performance", "train_performance", "holdout_performance")
    )
    for source, name, performance in performance_blocks:
        pf = _number(performance.get("profit_factor"))
        gross_profit_value = _number(performance.get("gross_profit"))
        gross_loss_value = _number(performance.get("gross_loss"))
        wins_value = _number(performance.get("wins"))
        losses_value = _number(performance.get("losses"))
        if gross_profit_value is not None and gross_loss_value is not None:
            no_loss_profit = gross_profit_value > 0 and gross_loss_value == 0
        elif wins_value is not None and losses_value is not None:
            no_loss_profit = wins_value > 0 and losses_value == 0
        else:
            no_loss_profit = False
        if no_loss_profit and pf == 99.0:
            findings.append(
                AuditFinding(
                    source,
                    "profit_factor_placeholder",
                    "warning",
                    f"{name} 有盈利且无亏损，PF=99 是程序占位值，不是可持续的高盈利因子。",
                    {
                        "performance": name,
                        "profit_factor": pf,
                        "gross_profit": gross_profit_value,
                        "gross_loss": gross_loss_value,
                        "wins": wins_value,
                        "losses": losses_value,
                    },
                )
            )

    return AuditReport(findings=findings, metrics=metrics)


def format_audit_text(report: AuditReport) -> str:
    """把审计结果格式化为适合终端阅读的纯文本。"""
    counts = report.counts
    lines = [
        "只读评估审计",
        f"严重级别统计：critical={counts['critical']} warning={counts['warning']} info={counts['info']}",
    ]
    if not report.findings:
        lines.append("未发现审计问题。")
    for finding in report.findings:
        lines.append(
            f"[{finding.severity.upper()}] {finding.source}/{finding.code}：{finding.message}"
        )
        if finding.details:
            lines.append(
                "  详情："
                + json.dumps(finding.details, ensure_ascii=False, sort_keys=True, default=str)
            )
    lines.append(f"人工复核说明：{REVIEW_NOTE}")
    return "\n".join(lines)
