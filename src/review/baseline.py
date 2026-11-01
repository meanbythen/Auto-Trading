"""
相对基准：近几日复盘存档 + 最近一次历史回测
功能：回答「今天这套策略，是偶然一天，还是和近期/回测预期差得很远」。
原理：
  每次生成复盘会额外存一份 JSON 摘要（不含长文）。
  新复盘时读取同品种、同策略最近若干天的摘要，对比有效性/平均R/开仓次数。
  若 data/backtest_last.json 正好是同品种，再把回测胜率、盈利因子当作「历史训练预期」对照。
  这是对策略优化（训练/检验）的盘后补充：用真实行情一天天验收。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from src.review.quality import QualityReport
from src.review.scorecard import Scorecard


@dataclass
class BaselineReport:
    lines: list[str] = field(default_factory=list)
    peers: list[dict[str, Any]] = field(default_factory=list)


def build_baseline_report(
    root: Path | str,
    *,
    symbol: str,
    engine: str | None,
    trade_date: date,
    scorecard: Scorecard,
    quality: QualityReport,
    lookback_days: int = 10,
) -> BaselineReport:
    """对比近几日复盘摘要，以及可选的最近回测。"""
    report = BaselineReport()
    root_path = Path(root)
    if engine is None:
        report.lines.append("只看盘面时不做策略基准对照。选好策略后再生成，才能和近几日、最近回测比。")
        return report

    peers = _load_peer_summaries(
        root_path,
        symbol=symbol,
        engine=engine,
        before=trade_date,
        limit=lookback_days,
    )
    report.peers = peers
    report.lines.append(
        "基准有两层：① 同品种同策略最近若干个交易日的复盘成绩单；"
        "② 若最近一次历史回测正好是这个品种，把它的胜率/盈利因子当作「历史预期」参考。"
        "今天明显差于近期或差于回测，要警惕过拟合或行情切换。"
    )

    if not peers:
        report.lines.append(
            "还没有更早的同策略复盘摘要。多生成几天之后，这里会自动出现「近几日平均有效性」对照。"
        )
    else:
        avg_eff = _avg([p.get("effectiveness") for p in peers])
        avg_stab = _avg([p.get("stability") for p in peers])
        avg_r = _avg([p.get("avg_r") for p in peers])
        report.lines.append(
            f"近 {len(peers)} 个交易日（同品种·同策略）平均："
            f"有效性 {avg_eff:.0f}，稳定性 {avg_stab:.0f}"
            + (f"，平均R {avg_r:+.2f}" if avg_r is not None else "")
            + "。"
        )
        report.lines.append(
            f"今天：有效性 {scorecard.effectiveness}，稳定性 {scorecard.stability}"
            + (
                f"，平均R {quality.avg_r:+.2f}"
                if quality.avg_r is not None
                else ""
            )
            + "。"
        )
        if avg_eff is not None:
            delta = scorecard.effectiveness - avg_eff
            if delta <= -15:
                report.lines.append(
                    f"今天有效性比近几日低约 {abs(delta):.0f} 分，优先看第八节缺陷，"
                    "不要急着把优化页的「最优参数」直接写入配置。"
                )
            elif delta >= 15:
                report.lines.append(
                    f"今天有效性比近几日高约 {delta:.0f} 分。好成绩也要看可信度："
                    "若可信度偏低，可能是样本少或信号与实盘没对上。"
                )
            else:
                report.lines.append("今天与近几日大致同水平，可把今天的质量数字当作滚动样本继续积累。")

    bt = _load_backtest_last(root_path, symbol)
    if bt is None:
        report.lines.append("没有可用的「最近一次历史回测」对照（文件不存在，或品种不一致）。")
    else:
        perf = bt.get("performance") or {}
        wr = float(perf.get("win_rate") or 0)
        pf = float(perf.get("profit_factor") or 0)
        n = int(perf.get("trades") or 0)
        report.lines.append(
            f"最近回测（{bt.get('ran_at') or '时间未知'}，{symbol}，"
            f"信号周期 {bt.get('timeframe')}）："
            f"{n} 笔，胜率 {wr:.0%}，盈利因子 {pf:.2f}。"
        )
        if quality.closed_count:
            day_wr = quality.win_count / quality.closed_count
            report.lines.append(
                f"今天已平仓胜率 {day_wr:.0%}（仅 {quality.closed_count} 笔，不能直接和回测比，只作方向感）。"
            )
            if n >= 5 and day_wr + 0.25 < wr and quality.closed_count >= 2:
                report.lines.append(
                    "今天胜率明显低于最近回测。可能是行情切换，也可能是回测段偏乐观——"
                    "请到策略优化页看检验段，而不是只看训练段。"
                )
        report.lines.append(
            "提醒：回测覆盖很多天，今天只是一天。单日胜负不能推翻回测，但连续多日系统性偏差可以。"
        )
    return report


def save_review_summary(
    root: Path | str,
    *,
    symbol: str,
    trade_date: date,
    engine: str | None,
    scope: str,
    scorecard: Scorecard,
    quality: QualityReport,
    defects: list[str],
    version: str,
) -> Path:
    """
    把当天成绩单摘要存成 JSON，供以后基准对照。
    长文仍在同名 txt；JSON 只存数字和标签，体积小。
    """
    folder = Path(root) / "data" / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in symbol if ch.isalnum() or ch in ("_", "-")) or "symbol"
    path = folder / f"{trade_date.isoformat()}_{safe}.json"
    payload = {
        "date": trade_date.isoformat(),
        "symbol": symbol,
        "engine": engine,
        "scope": scope,
        "version": version,
        "effectiveness": scorecard.effectiveness,
        "stability": scorecard.stability,
        "credibility": scorecard.credibility,
        "summary": scorecard.summary,
        "opens": len(quality.trades),
        "closed": quality.closed_count,
        "win_count": quality.win_count,
        "avg_r": quality.avg_r,
        "avg_capture": quality.avg_capture,
        "net_points": quality.net_points,
        "defects": list(defects),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_peer_summaries(
    root: Path,
    *,
    symbol: str,
    engine: str,
    before: date,
    limit: int,
) -> list[dict[str, Any]]:
    folder = root / "data" / "reviews"
    if not folder.exists():
        return []
    want = str(symbol).strip().lower()
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("symbol") or "").strip().lower() != want:
            continue
        if str(data.get("engine") or "") != str(engine):
            continue
        day_raw = str(data.get("date") or "")
        try:
            day = date.fromisoformat(day_raw)
        except ValueError:
            continue
        if day >= before:
            continue
        rows.append(data)
        if len(rows) >= limit:
            break
    return rows


def _load_backtest_last(root: Path, symbol: str) -> dict[str, Any] | None:
    path = root / "data" / "backtest_last.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("symbol") or "").strip().lower() != str(symbol).strip().lower():
        return None
    return data


def _avg(values: list[Any]) -> float | None:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)
