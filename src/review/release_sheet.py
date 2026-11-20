"""
上线评审单：把五项证据收成一页。

功能：数据门禁、样本外、镜像差异率、仿真滑点、人工签字，逐项给「通过 / 注意 / 不通过 / 未记录」。
原理：只读现有文件（回测、优化、事件、滑点、两本 CTP 账），不跑新回测、不改配置。
      签字写在 data/release_sheet.json，正文存 data/release_sheet.md；签字不会自动开仓或放宽风控。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.account.slippage_log import load_rows, reject_summary, summarize
from src.backtest.audit import audit_results
from src.data.periods import period_label

PASS = "通过"
WATCH = "注意"
FAIL = "不通过"
MISSING = "未记录"

SHEET_MD = "release_sheet.md"
SHEET_JSON = "release_sheet.json"
DECISIONS = ("继续观察", "允许小资金挂机")


@dataclass
class SheetSection:
    title: str
    status: str
    lines: list[str] = field(default_factory=list)


@dataclass
class ReleaseSheet:
    symbol: str
    engine: str
    timeframe: str
    generated_at: str
    sections: list[SheetSection] = field(default_factory=list)
    verdict: str = ""
    signature: dict[str, Any] = field(default_factory=dict)

    def section(self, title: str) -> SheetSection | None:
        return next((item for item in self.sections if item.title == title), None)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _box(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cfg_scope(cfg: dict[str, Any] | None) -> tuple[str, str, str]:
    strategy = _box((cfg or {}).get("strategy"))
    trade = _box((cfg or {}).get("trade"))
    symbol = str(trade.get("symbol") or "").strip()
    if not symbol:
        symbols = trade.get("symbols")
        if isinstance(symbols, list) and symbols:
            symbol = str(symbols[0] or "").strip()
    return (
        str(strategy.get("engine") or "").strip(),
        symbol,
        str(strategy.get("timeframe") or "").strip(),
    )


def _data_gate(
    root: Path,
    cfg: dict[str, Any] | None,
    backtest: dict[str, Any],
    *,
    symbol: str = "",
) -> SheetSection:
    section = SheetSection("数据门禁", MISSING)
    if not backtest:
        section.lines.append("还没有最近一次回测（data/backtest_last.json），先跑一把再评审。")
        return section
    snap = _box(backtest.get("params_snapshot"))
    engine, cfg_symbol, timeframe = _cfg_scope(cfg)
    # 复盘页点了具体品种时，按那个品种评；没点才退回配置页的第一个。
    symbol = symbol or cfg_symbol
    bt_engine = str(snap.get("engine") or "").strip()
    bt_symbol = str(backtest.get("symbol") or snap.get("symbol") or "").strip()
    bt_timeframe = str(backtest.get("timeframe") or snap.get("timeframe") or "").strip()
    mismatches: list[str] = []
    if engine and bt_engine and engine != bt_engine:
        mismatches.append(f"策略 {bt_engine}≠{engine}")
    if symbol and bt_symbol and symbol != bt_symbol:
        mismatches.append(f"品种 {bt_symbol}≠{symbol}")
    if timeframe and bt_timeframe and timeframe != bt_timeframe:
        mismatches.append(f"周期 {bt_timeframe}≠{timeframe}")
    data_from = str(snap.get("data_from") or "").strip()
    data_to = str(snap.get("data_to") or "").strip()
    bars = int(_number(snap.get("bars")) or 0)
    trades = int(_number(_box(backtest.get("performance")).get("trades")) or 0)
    section.lines.append(
        f"回测  {bt_engine or '—'} / {bt_symbol or '—'} / "
        f"{period_label(bt_timeframe) if bt_timeframe else '—'}"
    )
    section.lines.append(f"数据  {data_from[:10] or '—'} → {data_to[:10] or '—'}，K线 {bars} 根，成交 {trades} 笔")

    audit = audit_results(backtest, _read_json(root / "data" / "optimize_last.json"), cfg or {})
    # 数据门禁只看回测自身的审计项；优化的样本外单独在第 2 节算，避免重复扣分。
    findings = [finding for finding in audit.findings if finding.source == "backtest"]
    n_critical = sum(1 for finding in findings if finding.severity == "critical")
    n_warning = sum(1 for finding in findings if finding.severity == "warning")
    if n_critical:
        section.lines.append(f"审计  严重 {n_critical} 条，先处理")
        for finding in findings:
            if finding.severity == "critical":
                section.lines.append(f"  · {finding.message}")
    elif n_warning:
        section.lines.append(f"审计  提醒 {n_warning} 条，签字前请人工复核")
        for finding in findings:
            if finding.severity == "warning":
                section.lines.append(f"  · {finding.message}")
    else:
        section.lines.append("审计  没有严重或提醒项")

    if mismatches:
        section.status = FAIL
        section.lines.insert(0, "口径不一致：" + "，".join(mismatches) + "。回测不是当前这套配置。")
    elif not data_from or not data_to:
        section.status = FAIL
        section.lines.insert(0, "回测没记录实际数据区间，不能证明用了真实历史。")
    elif n_critical:
        section.status = FAIL
    elif n_warning or bars < 100:
        section.status = WATCH
    else:
        section.status = PASS
    return section


def _out_of_sample(root: Path) -> SheetSection:
    section = SheetSection("样本外", MISSING)
    data = _read_json(root / "data" / "optimize_last.json")
    holdout = _box(data.get("holdout_performance"))
    trades = int(_number(holdout.get("trades")) or 0)
    if not data:
        section.lines.append("还没有优化结果（data/optimize_last.json）。")
        return section
    if not holdout:
        section.lines.append("优化结果里没有检验段绩效，样本外无效。")
        section.status = FAIL
        return section
    pf = _number(holdout.get("profit_factor"))
    pnl = _number(holdout.get("net_pnl"))
    section.lines.append(
        f"冻结段  成交 {trades} 笔，盈亏 {pnl:+.2f}" if pnl is not None else f"冻结段  成交 {trades} 笔"
    )
    if pf is not None:
        section.lines.append(f"盈利因子  {pf:.2f}")
    if trades >= 30 and (pf is None or pf >= 1.0):
        section.status = PASS
    elif trades > 0:
        section.status = WATCH
        section.lines.append("检验段样本或盈利因子偏弱，只能当参考，不能当有效性证明。")
    else:
        section.status = FAIL
        section.lines.append("检验段零成交，不能作为样本外证据。")
    return section


def _mirror_gate(backtest: dict[str, Any], root: Path) -> SheetSection:
    section = SheetSection("镜像差异率", MISSING)
    if not backtest:
        section.lines.append("没有最近回测，算不了镜像差异。")
        return section
    snap = _box(backtest.get("params_snapshot"))
    enabled = bool(snap.get("mirror_live_gates"))
    hits = [row for row in (snap.get("mirror_gate_hits") or []) if isinstance(row, dict)]
    trades = int(_number(_box(backtest.get("performance")).get("trades")) or 0)
    if not enabled:
        section.lines.append("最近回测没开镜像闸门，差异率没有口径。重跑时打开 backtest.mirror_live_gates。")
    else:
        total = trades + len(hits)
        rate = (len(hits) / total * 100.0) if total else 0.0
        section.lines.append(
            f"回测闸门  放弃 {len(hits)} 次 / 机会 {total} 次 = {rate:.1f}%"
            f"（追价 {sum(1 for row in hits if row.get('kind') == 'slip_block')}，"
            f"过期 {sum(1 for row in hits if row.get('kind') == 'stale_block')}）"
        )
        section.status = PASS if rate <= 5.0 else WATCH
        if rate > 5.0:
            section.lines.append("放弃率高于 5%，先查 K线过期和追价设置，再谈上线。")
    rejects = reject_summary(root)
    if rejects["total"]:
        reasons = "、".join(f"{item['key']} {item['n']}" for item in rejects["by_reason"])
        section.lines.append(f"实盘近 30 天被拦 {rejects['total']} 次（{reasons}）")
    else:
        section.lines.append("实盘近 30 天没有被拦记录（或还没跑常驻）。")
    return section


def _sim_slippage(root: Path, cfg: dict[str, Any] | None) -> SheetSection:
    section = SheetSection("仿真滑点", MISSING)
    info = summarize(root, account="sim")
    rows = load_rows(root)
    unclassified = sum(1 for row in rows if not str(row.get("account") or "").strip())
    if info["n"] < 3:
        section.lines.append(f"仿真真实成交 {info['n']} 笔，不足 3 笔，先不给滑点结论。")
        if unclassified:
            section.lines.append(f"另有 {unclassified} 条旧记录没有账户列，无法分清仿真/真实。")
        return section
    suggested = float(info["suggested"] or 0)
    section.lines.append(
        f"仿真真实成交 {info['n']} 笔：中位 {info['median']:+.2f} 跳，70分位 {info['p70']:+.2f} 跳，建议 {suggested:g} 跳"
    )
    bt = _box((cfg or {}).get("backtest"))
    base = _number(bt.get("slippage_ticks")) or 0.0
    extra = _number(_box(bt.get("evaluation")).get("slippage_extra_ticks"))
    stress = base + (extra if extra is not None else 1.0)
    section.lines.append(f"回测口径  基线 {base:g} 跳 / 压力 {stress:g} 跳")
    if suggested > stress:
        section.status = WATCH
        section.lines.append("仿真滑点高于压力档，回测成绩偏乐观，建议先抬高滑点重跑。")
    else:
        section.status = PASS
    if unclassified:
        section.lines.append(f"另有 {unclassified} 条旧记录没有账户列，未计入。")
    return section


def _signature_section(signature: dict[str, Any]) -> SheetSection:
    section = SheetSection("人工签字", MISSING)
    if not signature:
        section.lines.append("还没有签字。签字只代表你看过以上证据，不会自动改配置或开仓。")
        return section
    section.status = PASS
    section.lines.append(
        f"{signature.get('signed_by') or '—'}  {signature.get('signed_at') or '—'}  "
        f"决定：{signature.get('decision') or '—'}"
    )
    note = str(signature.get("note") or "").strip()
    if note:
        section.lines.append(f"备注  {note}")
    return section


def _verdict(sections: list[SheetSection], signature: dict[str, Any]) -> str:
    failed = [item.title for item in sections if item.status == FAIL]
    if failed:
        return "不通过（" + "、".join(failed) + "）"
    status = {item.title: item.status for item in sections}
    if status.get("数据门禁") == MISSING or status.get("样本外") == MISSING:
        return "证据不足：先补数据门禁和样本外"
    if signature:
        decision = str(signature.get("decision") or "")
        return f"已签字：{decision}" if decision else "已签字"
    return "证据已齐，待人工签字"


def build_release_sheet(
    root: Path | str,
    cfg: dict[str, Any] | None = None,
    *,
    symbol: str = "",
) -> ReleaseSheet:
    """读现有文件生成评审单。symbol 只用于抬头，不改筛选口径。"""
    base = Path(root)
    backtest = _read_json(base / "data" / "backtest_last.json")
    snap = _box(backtest.get("params_snapshot"))
    engine, cfg_symbol, timeframe = _cfg_scope(cfg)
    signature = _read_json(base / "data" / SHEET_JSON)
    sections = [
        _data_gate(base, cfg, backtest, symbol=symbol),
        _out_of_sample(base),
        _mirror_gate(backtest, base),
        _sim_slippage(base, cfg),
        _signature_section(signature),
    ]
    return ReleaseSheet(
        symbol=symbol or cfg_symbol,
        engine=engine or str(snap.get("engine") or ""),
        timeframe=timeframe,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        sections=sections,
        verdict=_verdict(sections, signature),
        signature=signature,
    )


def format_release_sheet(sheet: ReleaseSheet) -> str:
    """一页正文。每节第一行都写状态，避免只看到「通过」两个字。"""
    lines = [
        f"【上线评审单】{sheet.symbol or '—'}    {sheet.generated_at}",
        f"策略 {sheet.engine or '—'} / 周期 {period_label(sheet.timeframe) if sheet.timeframe else '—'}",
        f"结论  {sheet.verdict}",
        "",
    ]
    for index, section in enumerate(sheet.sections, start=1):
        lines.append(f"{'一二三四五六七八九十'[index - 1]}、{section.title}  [{section.status}]")
        lines.extend(f"  {row}" for row in section.lines)
        lines.append("")
    lines.append("签字只代表看过以上证据；不改配置、不放宽风控、不自动开仓。")
    return "\n".join(lines)


def save_release_sheet(root: Path | str, text: str) -> Path:
    path = Path(root) / "data" / SHEET_MD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sign_release_sheet(
    root: Path | str,
    sheet: ReleaseSheet,
    *,
    signed_by: str,
    decision: str,
    note: str = "",
) -> dict[str, Any]:
    """写入签字记录，并同步更新正文。签字不改任何交易参数。"""
    payload = {
        "signed_by": signed_by.strip(),
        "signed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decision": decision,
        "note": note.strip(),
        "symbol": sheet.symbol,
        "engine": sheet.engine,
        "verdict": sheet.verdict,
        "generated_at": sheet.generated_at,
    }
    path = Path(root) / "data" / SHEET_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sheet.signature = payload
    section = sheet.section("人工签字")
    if section is not None:
        section.status = PASS
        section.lines = [
            f"{payload['signed_by']}  {payload['signed_at']}  决定：{payload['decision']}",
            *([f"备注  {payload['note']}"] if payload["note"] else []),
        ]
    sheet.verdict = _verdict(sheet.sections, payload)
    save_release_sheet(root, format_release_sheet(sheet))
    return payload
