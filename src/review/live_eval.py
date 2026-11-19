"""
复盘里的两段评估
功能：
  1. 当日重放评估：把当天重放出来的信号按新标准打分，只看 PF、R 期望、连亏和 ADX 分组。
  2. 最近实盘 vs 最近回测：把账本最近若干笔和 data/backtest_last.json 对照。
原理：当天样本天然少，资金类门槛不参与判定；实盘对照直接复用 ops.health 的判定，
      这里只负责把数字和结论写成人话，不改原有告警行为。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.backtest.evaluation import (
    EvaluationThresholds,
    evaluate_day,
    format_evaluation_lines,
)
from src.backtest.metrics import TradeRecord
from src.ops.health import evaluate_health


def build_replay_eval(quality: Any, cfg: dict[str, Any]) -> list[str]:
    """当日重放评估。失败时返回一句原因，不阻断复盘。"""
    try:
        trades = [
            _pseudo_trade(item)
            for item in (getattr(quality, "trades", None) or [])
            if not getattr(item, "still_open", False)
        ]
        thresholds = EvaluationThresholds.from_cfg(cfg)
        report = evaluate_day(trades, thresholds=thresholds)
        lines = format_evaluation_lines(report, title="当日重放评估")
        lines.append(
            "  口径：当天成交少，样本、卡尔玛、盈利覆盖和压力测试不参与判定；"
            "只回答今天这套规则执行得像不像样。"
        )
        return lines
    except Exception as exc:  # noqa: BLE001
        logger.debug("当日重放评估没生成: {}", exc)
        return ["（本次没有生成当日重放评估：" + str(exc) + "）"]


def build_live_vs_backtest(root: Path | str, symbol: str, cfg: dict[str, Any]) -> list[str]:
    """最近实盘和最近回测的对照，外加最近回测的评估结论。"""
    try:
        base = Path(root)
        health = evaluate_health(base)
        lines = ["—— 最近实盘 vs 最近回测 ——"]
        lines.append(f"  对照品种  {symbol}（账本口径，暂不分品种）")
        if health.status == "skip" and health.live_n == 0 and not health.bt_n:
            lines.append("  " + health.message)
            return lines
        live_wr = "—" if health.live_win_rate is None else f"{health.live_win_rate:.0%}"
        live_pf = "—" if health.live_pf is None else f"{health.live_pf:.2f}"
        bt_wr = "—" if health.bt_win_rate is None else f"{health.bt_win_rate:.0%}"
        bt_pf = "—" if health.bt_pf is None else f"{health.bt_pf:.2f}"
        lines.append(f"  实盘近{health.live_n}笔  胜率 {live_wr}  盈利因子 {live_pf}")
        lines.append(f"  最近回测      {health.bt_n}笔  胜率 {bt_wr}  盈利因子 {bt_pf}")
        if health.live_win_rate is not None and health.bt_win_rate is not None:
            gap = health.live_win_rate - health.bt_win_rate
            lines.append(f"  胜率差        {gap:+.0%}（实盘 − 回测）")
        if health.live_pf is not None and health.bt_pf:
            lines.append(f"  盈利因子比值  {health.live_pf / health.bt_pf:.2f}")
        lines.append("  判定  " + health.message)
        verdict = _backtest_verdict(base)
        if verdict:
            lines.append("  最近回测评估  " + verdict)
        extra = _backtest_metric_text(base)
        if extra:
            lines.append("  最近回测指标  " + extra)
        thresholds = EvaluationThresholds.from_cfg(cfg)
        if health.bt_pf is not None and health.bt_pf < thresholds.profit_factor_min:
            lines.append(
                f"  提醒  最近回测盈利因子低于评估门槛 {thresholds.profit_factor_min:.2f}，"
                "实盘前先重新优化或加严过滤。"
            )
        return lines
    except Exception as exc:  # noqa: BLE001
        logger.debug("实盘对照没生成: {}", exc)
        return ["（本次没有生成实盘对照：" + str(exc) + "）"]


def _pseudo_trade(item: Any) -> TradeRecord:
    """把复盘里的一笔信号换成回测成交结构，只为了用同一套评估。"""
    entry = getattr(item, "entry_time", None)
    exit_at = getattr(item, "exit_time", None)
    return TradeRecord(
        symbol="",
        side="long" if str(getattr(item, "side", "")) == "long" else "short",
        entry_time=_stamp(entry),
        exit_time=_stamp(exit_at) or _stamp(entry),
        entry_price=float(getattr(item, "entry_price", 0) or 0),
        exit_price=float(getattr(item, "exit_price", 0) or getattr(item, "entry_price", 0) or 0),
        volume=1,
        pnl=float(getattr(item, "pnl_points", 0) or 0),
        reason_entry=str(getattr(item, "reason_entry", "") or ""),
        reason_exit=str(getattr(item, "reason_exit", "") or ""),
        commission=0.0,
        adx_at_entry=getattr(item, "adx_at_entry", None),
    )


def _stamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _load_backtest(root: Path) -> dict[str, Any]:
    """读最近一次回测结果；文件缺失或格式不对时返回空字典。"""
    path = root / "data" / "backtest_last.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _backtest_verdict(root: Path) -> str:
    """最近一次回测的评估结论，读不到就返回空串。"""
    evaluation = _load_backtest(root).get("evaluation") or {}
    if not isinstance(evaluation, dict) or not evaluation:
        return ""
    text = str(evaluation.get("conclusion_text") or "")
    failed = evaluation.get("failed_items") or []
    if text and failed:
        return f"{text}（红线：{'、'.join(str(x) for x in failed)}）"
    return text


def _backtest_metric_text(root: Path) -> str:
    """最近回测的收益期望、卡尔玛和最大连亏，和历史回测页同一批指标。"""
    evaluation = _load_backtest(root).get("evaluation") or {}
    if not isinstance(evaluation, dict):
        return ""
    metrics = evaluation.get("metrics") or {}
    if not isinstance(metrics, dict):
        return ""
    parts: list[str] = []

    def _num(key: str) -> float | None:
        try:
            return None if metrics.get(key) is None else float(metrics[key])
        except (TypeError, ValueError):
            return None

    r_value = _num("expectancy")
    if r_value is None:
        r_value = _num("r_expectancy")
    if r_value is not None:
        parts.append(f"收益期望 {r_value:.2f}")
    calmar = _num("calmar")
    if calmar is not None:
        parts.append(f"卡尔玛 {calmar:.2f}")
    streak = _num("consecutive_losses")
    if streak is not None:
        parts.append(f"最大连亏 {int(streak)}笔")
    return "  ".join(parts)
