"""
上线评审单：把六项证据收成一页。

功能：数据门禁、样本外、镜像差异率、仿真滑点、成本校准、人工签字，逐项给「通过 / 注意 / 不通过 / 未记录」。
原理：只读现有文件（回测、优化、事件、滑点、两本 CTP 账），不跑新回测、不改配置。
      签字写在 data/release_sheet.json，正文存 data/release_sheet.md；签字不会自动开仓或放宽风控。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.account.slippage_log import load_rows, load_slippage_calibration, product_of, reject_summary, summarize
from src.backtest.audit import audit_results
from src.data.periods import period_label
from src.strategy.types import config_flag

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


def _extra_scope_mismatches(cfg: dict[str, Any] | None, snap: dict[str, Any]) -> list[str]:
    """数据源、滑点、成交方式、手数模式、换月链资金。两边都记录到才比较。"""
    out: list[str] = []
    data = _box((cfg or {}).get("data"))
    bt = _box((cfg or {}).get("backtest"))
    risk = _box((cfg or {}).get("risk"))

    cur_source = str(data.get("source") or "").strip()
    bt_source = str(snap.get("data_source") or "").strip()
    if cur_source and bt_source and cur_source != bt_source:
        out.append(f"数据源 {bt_source}≠{cur_source}")

    if "slippage_ticks" in bt:
        cur_slip = _number(bt.get("slippage_ticks"))
        bt_slip = _number(snap.get("slippage_ticks"))
        if cur_slip is not None and bt_slip is not None and abs(cur_slip - bt_slip) > 1e-9:
            out.append(f"滑点 {bt_slip:g}≠{cur_slip:g}")

    if "fill_on_next_open" in bt and snap.get("fill_on_next_open") is not None:
        cur_fill = config_flag(bt.get("fill_on_next_open"), default=True)
        bt_fill = config_flag(snap.get("fill_on_next_open"), default=True)
        if cur_fill != bt_fill:
            out.append(
                "成交方式 "
                f"{'下一根开盘' if bt_fill else '策略价'}≠{'下一根开盘' if cur_fill else '策略价'}"
            )

    cur_size = str(risk.get("sizing_mode") or "").strip()
    bt_size = str(snap.get("sizing_mode") or "").strip()
    if cur_size and bt_size and cur_size != bt_size:
        out.append(f"手数模式 {bt_size}≠{cur_size}")

    if "chain_capital" in bt and snap.get("chain_capital") is not None:
        cur_chain = config_flag(bt.get("chain_capital"), default=False)
        bt_chain = config_flag(snap.get("chain_capital"), default=False)
        if cur_chain != bt_chain:
            out.append(
                "换月链资金 "
                f"{'连续' if bt_chain else '每段独立'}≠{'连续' if cur_chain else '每段独立'}"
            )
    return out


def _zero_fee_symbols(cfg: dict[str, Any] | None, symbol: str) -> list[str]:
    """评审单要提示：这些品种手续费是 0，回测净盈亏没扣成本。"""
    from src.risk.commission import zero_fee_products

    contracts = ((cfg or {}).get("risk") or {}).get("contracts")
    strategy = ((cfg or {}).get("strategy") or {})
    symbols: list[str] = [symbol] if symbol else []
    extra = strategy.get("symbols") if isinstance(strategy, dict) else None
    if isinstance(extra, list):
        symbols.extend(str(item) for item in extra)
    return zero_fee_products(contracts, symbols)


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
    if symbol and bt_symbol and product_of(symbol) != product_of(bt_symbol):
        mismatches.append(f"品种 {bt_symbol}≠{symbol}")
    if timeframe and bt_timeframe and timeframe != bt_timeframe:
        mismatches.append(f"周期 {bt_timeframe}≠{timeframe}")
    mismatches.extend(_extra_scope_mismatches(cfg, snap))
    data_from = str(snap.get("data_from") or "").strip()
    data_to = str(snap.get("data_to") or "").strip()
    bars = int(_number(snap.get("bars")) or _number(backtest.get("bars")) or 0)
    trades = int(_number(_box(backtest.get("performance")).get("trades")) or 0)
    section.lines.append(
        f"回测  {bt_engine or '—'} / {bt_symbol or '—'} / "
        f"{period_label(bt_timeframe) if bt_timeframe else '—'}"
    )
    section.lines.append(f"数据  {data_from[:10] or '—'} → {data_to[:10] or '—'}，K线 {bars} 根，成交 {trades} 笔")
    missing_scope = [
        label
        for key, label in (
            ("data_source", "数据源"),
            ("slippage_ticks", "滑点"),
            ("fill_on_next_open", "成交方式"),
            ("sizing_mode", "手数模式"),
            ("chain_capital", "换月链资金"),
        )
        if key not in snap or snap.get(key) in (None, "")
    ]
    if missing_scope:
        section.lines.append("口径  " + "、".join(missing_scope) + " 未记录，无法核对")

    audit = audit_results(backtest, _read_json(root / "data" / "optimize_last.json"), cfg or {})
    # 数据门禁只看回测自身的审计项；优化的样本外单独在第 2 节算，避免重复扣分。
    findings = [finding for finding in audit.findings if finding.source == "backtest"]
    n_critical = sum(1 for finding in findings if finding.severity == "critical")
    n_warning = sum(1 for finding in findings if finding.severity == "warning")
    zero_fee = _zero_fee_symbols(cfg, bt_symbol or symbol)
    if zero_fee:
        section.lines.append(
            "成本  " + "、".join(zero_fee) + " 手续费为 0，回测净盈亏没扣这笔钱；按期货公司结算单核对"
        )
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
    elif n_warning or zero_fee or bars < 100:
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
    extra = _number(_box((cfg or {}).get("evaluation")).get("slippage_extra_ticks"))
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


def _cost_calibration(root: Path, cfg: dict[str, Any] | None) -> SheetSection:
    section = SheetSection("成本校准", WATCH)
    uncalibrated = "未校准：当前评审用的是配置里的假设滑点，没有实盘来源。先补真实成交样本，再在复盘页生成校准记录。"
    try:
        record = load_slippage_calibration(root)
        if not record or not record.get("calibrated"):
            section.lines.append(uncalibrated)
            reason = str(record.get("reason") or "").strip()
            if reason:
                section.lines.append(f"原因：{reason}")
            return section

        live = _number(record.get("live_suggested")) or 0.0
        assumed = _number(record.get("backtest_assumed")) or 0.0
        diff = _number(record.get("diff"))
        if diff is None:
            diff = live - assumed
        sample_n = int(_number(record.get("sample_n")) or 0)
        effective_date = str(record.get("effective_date") or "").strip() or "—"
        section.status = PASS
        section.lines.append(f"实盘建议滑点 {live:g} 跳 / 回测假设 {assumed:g} 跳")
        if diff > 0:
            comparison = f"回测滑点比实盘乐观 {diff:g} 跳"
        elif diff < 0:
            comparison = f"回测滑点比实盘保守 {-diff:g} 跳"
        else:
            comparison = "回测滑点与实盘建议一致"
        section.lines.append(f"差值 {diff:+g} 跳（正=实盘比回测假设更不利）：{comparison}")
        section.lines.append(f"生效日期 {effective_date} / 样本 {sample_n} 笔")

        calibrated_at = None
        for value in (record.get("generated_at"), record.get("effective_date")):
            stamp = str(value or "").strip()
            if not stamp:
                continue
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                calibrated_at = parsed
                break
            except (TypeError, ValueError, OverflowError, OSError):
                continue
        if calibrated_at is None:
            if record.get("generated_at") or record.get("effective_date"):
                section.lines.append("校准时间无法判定，请核对 generated_at / effective_date。")
        elif datetime.now() - calibrated_at > timedelta(days=30):
            section.status = WATCH
            section.lines.append("校准记录已超 30 天，建议重新校准")
    except Exception:  # noqa: BLE001 - 评审单只读引用，异常记录不能阻断其它证据
        section.status = WATCH
        section.lines = [uncalibrated, "校准记录读取或解析失败，按未校准处理。"]
    return section


def _governance_trace(root: Path, cfg: dict[str, Any] | None) -> SheetSection:
    """治理追踪：展示假设 / 实验 / 样本外 / 评审单 / 上线策略 / 变更 / 冻结。

    只展示不改变既有结论；无治理数据时标「未记录」，读失败也不抛异常。
    """
    section = SheetSection("治理追踪", MISSING)
    try:
        from src.research import governance

        engine, cfg_symbol, _ = _cfg_scope(cfg)
        backtest = _read_json(root / "data" / "backtest_last.json")
        snap = _box(backtest.get("params_snapshot"))
        bt_engine = str(snap.get("engine") or "").strip()
        bt_symbol = str(backtest.get("symbol") or snap.get("symbol") or "").strip()
        strategy = engine or bt_engine
        symbol = cfg_symbol or bt_symbol
        if not strategy or not symbol:
            section.lines.append("治理数据不可用：缺少策略或品种，无法追踪。")
            return section
        trace = governance.trace_strategy(root, strategy, symbol)
        lines = list(governance.format_trace_lines(trace))
        frozen = trace.get("frozen")
        if isinstance(frozen, (tuple, list)) and len(frozen) >= 2 and frozen[0]:
            lines.append(f"提示：{frozen[1]}")
        section.lines = lines
        has_data = (
            isinstance(trace.get("hypothesis"), dict)
            or bool(trace.get("changes"))
            or isinstance(trace.get("live_strategy"), dict)
            or bool(trace.get("experiments"))
        )
        section.status = PASS if has_data else MISSING
    except Exception:  # noqa: BLE001 - 评审单只读引用，治理读失败不能阻断其它证据
        section.status = MISSING
        section.lines = ["治理数据不可用"]
    return section


def _evidence_sections(sections: list[SheetSection]) -> list[SheetSection]:
    """签名字节以外的证据节。签字动作本身不能改变摘要。

    「治理追踪」只展示、不纳入证据摘要：避免无治理数据时改变既有签字行为
    （缺证据会阻止签「允许小资金挂机」），也避免治理数据波动触发重签。
    """
    return [item for item in sections if item.title not in ("人工签字", "治理追踪")]


def _evidence_digest(sections: list[SheetSection]) -> str:
    """把证据内容压成摘要，签字时存下来，下次生成时比对。"""
    payload = [
        {"title": item.title, "status": item.status, "lines": list(item.lines)}
        for item in _evidence_sections(sections)
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _signature_section(signature: dict[str, Any], digest: str) -> SheetSection:
    section = SheetSection("人工签字", MISSING)
    if not signature:
        section.lines.append("还没有签字。签字只代表你看过以上证据，不会自动改配置或开仓。")
        return section
    line = (
        f"{signature.get('signed_by') or '—'}  {signature.get('signed_at') or '—'}  "
        f"决定：{signature.get('decision') or '—'}"
    )
    stored = str(signature.get("evidence_digest") or "")
    if not stored:
        section.status = WATCH
        section.lines.append(line)
        section.lines.append("旧版签字未绑定证据摘要，只能当历史记录；请重新生成评审单并签字。")
    elif stored != digest:
        section.status = FAIL
        section.lines.append(line)
        section.lines.append(
            f"签字绑定证据 {stored[:8]}，当前证据 {digest[:8]}；"
            "签字后证据已变化，需重新生成并签字。"
        )
    else:
        section.status = PASS
        section.lines.append(line)
        section.lines.append(
            f"证据摘要 {stored[:8]}  生成于 {signature.get('evidence_generated_at') or '—'}"
        )
    note = str(signature.get("note") or "").strip()
    if note:
        section.lines.append(f"备注  {note}")
    return section


def _verdict(sections: list[SheetSection], signature: dict[str, Any]) -> str:
    failed = [item.title for item in sections if item.status == FAIL]
    if failed:
        if signature and "人工签字" in failed:
            return "证据已变化，需重新签字"
        return "不通过（" + "、".join(failed) + "）"
    status = {item.title: item.status for item in sections}
    if status.get("数据门禁") == MISSING or status.get("样本外") == MISSING:
        return "证据不足：先补数据门禁和样本外"
    if signature:
        if status.get("人工签字") == WATCH:
            return "旧签字未绑定证据摘要，需重新签字"
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
        _cost_calibration(base, cfg),
        _governance_trace(base, cfg),
    ]
    sections.append(_signature_section(signature, _evidence_digest(sections)))
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
    """
    写入签字记录，并同步更新正文。签字不改任何交易参数。

    证据有「未记录」时，拒绝签「允许小资金挂机」；签字会带上当时的证据摘要，
    之后证据一变，评审单就会显示需要重新签字。
    """
    if decision == "允许小资金挂机":
        missing = [
            item.title for item in _evidence_sections(sheet.sections) if item.status == MISSING
        ]
        if missing:
            raise ValueError("证据未齐，不能签「允许小资金挂机」：" + "、".join(missing))
    digest = _evidence_digest(sheet.sections)
    payload = {
        "signed_by": signed_by.strip(),
        "signed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decision": decision,
        "note": note.strip(),
        "symbol": sheet.symbol,
        "engine": sheet.engine,
        "verdict": sheet.verdict,
        "generated_at": sheet.generated_at,
        "evidence_generated_at": sheet.generated_at,
        "evidence_digest": digest,
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
            f"证据摘要 {digest[:8]}  生成于 {payload['evidence_generated_at']}",
            *([f"备注  {payload['note']}"] if payload["note"] else []),
        ]
    sheet.verdict = _verdict(sheet.sections, payload)
    save_release_sheet(root, format_release_sheet(sheet))
    return payload
