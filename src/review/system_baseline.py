"""
P0.3 当前系统基线。

功能：把某一天的「策略重放 / 事件日志 / 账本 / 订单时间线」收成一份可重跑的样本，
      差异必须归到固定类别，不能用「可能是滑点」糊过去。
原理：复用 gap.py 的三方计数，再叠时间线的成交种类。GUI 的 assumed_fill
      永远不能写成柜台真实成交。样本是普通 JSON，后续改引擎时重跑同一份对照。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.review.gap import GapReport, build_gap_report
from src.review.quality import SignalTrade
from src.review.session import SessionWindow

SCHEMA_VERSION = 1

# 差异只能落在这些类里。新增类别要同步改测试，避免又变回笼统解释。
CATEGORIES = (
    "matched",
    "data",
    "config",
    "existing_position",
    "risk_block",
    "catch_up",
    "assumed_fill",
    "real_anomaly",
)

CATEGORY_LABELS = {
    "matched": "三方一致",
    "data": "数据差异",
    "config": "配置差异",
    "existing_position": "已有持仓",
    "risk_block": "风控拦截",
    "catch_up": "启动补开仓",
    "assumed_fill": "GUI假定成交",
    "real_anomaly": "真实异常",
}

# 事件 code / 人话关键词 → 类别。先精确 code，再关键词。
_EVENT_CATEGORY = {
    "catch_up_open": "catch_up",
    "catch_up_skip": "catch_up",
    "risk_block": "risk_block",
    "size_skip": "risk_block",
    "order_paused": "risk_block",
    "stale_quote": "data",
    "fallback_source": "data",
    "assumed_fill": "assumed_fill",
    "ledger_reconcile": "real_anomaly",
}

_FILL_KIND_LABEL = {
    "channel_fill": "柜台真实成交",
    "assumed_fill": "GUI假定成交",
    "simulated_fill": "Sim模拟成交",
}


@dataclass
class BaselineCase:
    """一份固定对照样本。字段都是可 JSON 化的普通值。"""

    case_id: str
    symbol: str
    engine: str
    trade_date: str
    replay_opens: int = 0
    event_orders: int = 0
    book_fills: int = 0
    event_codes: list[str] = field(default_factory=list)
    fill_kinds: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    alignment: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BaselineDiff:
    """当前跑出来的样本和冻结样本之间，哪一项变了。"""

    field: str
    frozen: Any
    current: Any
    note: str


def fill_kind_label(kind: str) -> str:
    """成交种类的人话。assumed_fill 不会被叫成柜台成交。"""
    key = str(kind or "").strip()
    if key in _FILL_KIND_LABEL:
        return _FILL_KIND_LABEL[key]
    return key or "未知成交"


def classify_difference(
    *,
    replay_opens: int,
    event_orders: int,
    book_fills: int,
    event_codes: list[str] | None = None,
    fill_kinds: list[str] | None = None,
    gap_notes: list[str] | None = None,
) -> list[str]:
    """
    把三方对不上的原因收成固定类别。

    可以同时有多类（例如既有假定成交，又被风控拦过）。
    GUI 假定成交单独一类，绝不并进「柜台真实成交」。
    """
    found: list[str] = []

    def _add(code: str) -> None:
        if code in CATEGORIES and code not in found:
            found.append(code)

    for kind in fill_kinds or []:
        if str(kind) == "assumed_fill":
            _add("assumed_fill")
        elif str(kind) == "channel_fill":
            pass
        elif str(kind) == "simulated_fill":
            pass
    for code in event_codes or []:
        mapped = _EVENT_CATEGORY.get(str(code))
        if mapped:
            _add(mapped)
    notes = " ".join(str(item) for item in (gap_notes or []))
    if "已有老仓" in notes or "已有持仓" in notes:
        _add("existing_position")
    if "参数和现在不同" in notes or "没点「启动常驻」" in notes or "dry_run" in notes:
        _add("config")
    if "行情太旧" in notes or "来源" in notes:
        _add("data")

    empty = replay_opens == 0 and event_orders == 0 and book_fills == 0
    if empty and not found:
        return ["matched"]
    if replay_opens > 0 and event_orders == 0 and book_fills == 0 and not found:
        _add("config")
    if replay_opens == 0 and (event_orders > 0 or book_fills > 0) and not found:
        _add("existing_position")
    if replay_opens > 0 and (event_orders > 0 or book_fills > 0) and not found:
        if abs(replay_opens - max(event_orders, book_fills)) < 2:
            return ["matched"]
        _add("real_anomaly")
    if not found:
        _add("real_anomaly")
    return found


def build_case(
    *,
    case_id: str,
    symbol: str,
    engine: str,
    trade_date: date | str,
    gap: GapReport,
    event_codes: list[str] | None = None,
    fill_kinds: list[str] | None = None,
) -> BaselineCase:
    """从当天 gap 报告和时间线抽一份基线样本。"""
    kinds = [str(item) for item in (fill_kinds or [])]
    codes = [str(item) for item in (event_codes or [])]
    categories = classify_difference(
        replay_opens=int(gap.replay_opens),
        event_orders=int(gap.event_orders),
        book_fills=int(gap.book_fills),
        event_codes=codes,
        fill_kinds=kinds,
        gap_notes=list(gap.notes) + list(gap.lines),
    )
    evidence = list(gap.lines[:8])
    for kind in kinds:
        evidence.append(f"成交种类 {kind} = {fill_kind_label(kind)}")
    day = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)[:10]
    return BaselineCase(
        case_id=str(case_id),
        symbol=str(symbol),
        engine=str(engine),
        trade_date=day,
        replay_opens=int(gap.replay_opens),
        event_orders=int(gap.event_orders),
        book_fills=int(gap.book_fills),
        event_codes=codes,
        fill_kinds=kinds,
        categories=categories,
        evidence=evidence,
        alignment=str(gap.alignment),
    )


def build_case_from_sources(
    root: Path | str,
    *,
    case_id: str,
    symbol: str,
    engine: str,
    trade_date: date,
    windows: list[SessionWindow],
    trades: list[SignalTrade],
    actual_event_lines: list[str],
    event_codes: list[str] | None = None,
    fill_kinds: list[str] | None = None,
) -> BaselineCase:
    """给复盘页用：现场读账本和事件，再分类。"""
    gap = build_gap_report(
        root,
        symbol=symbol,
        windows=windows,
        trades=trades,
        actual_event_lines=actual_event_lines,
    )
    return build_case(
        case_id=case_id,
        symbol=symbol,
        engine=engine,
        trade_date=trade_date,
        gap=gap,
        event_codes=event_codes,
        fill_kinds=fill_kinds,
    )


def pack_payload(cases: list[BaselineCase], *, note: str = "") -> dict[str, Any]:
    """写成冻结文件的内容。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "fill_kind_labels": dict(_FILL_KIND_LABEL),
        "category_labels": dict(CATEGORY_LABELS),
        "cases": [case.to_dict() for case in cases],
    }


def save_pack(path: Path | str, payload: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_pack(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_cases(frozen: BaselineCase | dict[str, Any], current: BaselineCase) -> list[BaselineDiff]:
    """
    同一 case_id 重跑后，哪些字段变了。

    引擎改动后必须能列出信号/成交变化，而不是只看总盈亏。
    """
    old = frozen.to_dict() if isinstance(frozen, BaselineCase) else dict(frozen)
    new = current.to_dict()
    diffs: list[BaselineDiff] = []
    for key in ("replay_opens", "event_orders", "book_fills", "alignment"):
        if old.get(key) != new.get(key):
            diffs.append(
                BaselineDiff(
                    field=key,
                    frozen=old.get(key),
                    current=new.get(key),
                    note="信号或记录笔数变了，请对到具体改动",
                )
            )
    if list(old.get("categories") or []) != list(new.get("categories") or []):
        diffs.append(
            BaselineDiff(
                field="categories",
                frozen=old.get("categories"),
                current=new.get("categories"),
                note="差异分类变了",
            )
        )
    if list(old.get("fill_kinds") or []) != list(new.get("fill_kinds") or []):
        diffs.append(
            BaselineDiff(
                field="fill_kinds",
                frozen=old.get("fill_kinds"),
                current=new.get("fill_kinds"),
                note="成交种类变了（尤其检查 assumed_fill 有没有被改成柜台成交）",
            )
        )
    return diffs


def _timeline_fill_kinds(root: Path | str, symbol: str, trade_date: date | str) -> list[str]:
    """当天该合约在订单时间线里出现过的成交种类。"""
    from src.trader.order_timeline import load_rows

    day = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)[:10]
    want = str(symbol).strip().lower()
    kinds: list[str] = []
    for row in load_rows(root):
        if str(row.get("symbol") or "").strip().lower() != want:
            continue
        ts = str(row.get("ts") or "")
        if day not in ts:
            continue
        stage = str(row.get("stage") or "")
        if stage == "trade":
            kinds.append("channel_fill")
        elif stage in {"assumed_fill", "simulated_fill"}:
            kinds.append(stage)
    return kinds


def _event_codes_from_file(root: Path | str, symbol: str, trade_date: date | str) -> list[str]:
    """当天该合约事件日志里出现过的 code。"""
    day = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)[:10]
    want = str(symbol).strip().lower()
    path = Path(root) / "data" / "events.jsonl"
    if not path.exists():
        return []
    codes: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        ev_symbol = str(item.get("symbol") or "").strip().lower()
        if ev_symbol and ev_symbol != want:
            continue
        ts = str(item.get("ts") or "")
        if day not in ts:
            continue
        event = str(item.get("event") or "")
        if event in _EVENT_CATEGORY:
            codes.append(event)
    return codes


def save_current_baseline(
    cfg: dict[str, Any],
    *,
    symbol: str,
    trade_date: date,
    scope: str,
    engine: str | None,
    root: Path | str,
) -> Path:
    """
    把当天复盘对照收成一份基线文件。

    会再拉一次当天 K 线做策略重放，和点「生成复盘」用同一套规则。
    文件写在 data/research/baselines/日期_合约.json。
    """
    from src.review.actual import actual_lines
    from src.review.quality import enrich_round_trips
    from src.review.replay import replay_strategy
    from src.review.service import _load_bars
    from src.review.session import windows_for

    root_path = Path(root)
    windows = windows_for(trade_date, scope)
    signal, trail = _load_bars(
        cfg,
        symbol=symbol,
        engine=engine,
        bars_signal=None,
        bars_trail=None,
    )
    replay = replay_strategy(
        cfg,
        symbol=symbol,
        engine=engine,
        bars_signal=signal,
        bars_trail=trail,
        windows=windows,
        pin_count=0,
        bars_trend=None,
    )
    window_end = max((w.end for w in windows), default=None)
    trades = enrich_round_trips(
        getattr(replay, "open_events", []) or [],
        getattr(replay, "close_events", []) or [],
        signal,
        window_end=window_end,
    )
    actual = actual_lines(root_path, symbol, windows)
    case = build_case_from_sources(
        root_path,
        case_id=f"{trade_date.isoformat()}_{symbol}_{engine or 'none'}",
        symbol=symbol,
        engine=str(engine or ""),
        trade_date=trade_date,
        windows=windows,
        trades=trades,
        actual_event_lines=actual,
        event_codes=_event_codes_from_file(root_path, symbol, trade_date),
        fill_kinds=_timeline_fill_kinds(root_path, symbol, trade_date),
    )
    folder = root_path / "data" / "research" / "baselines"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{trade_date.isoformat()}_{symbol}.json"
    payload = pack_payload([case], note="界面「保存当天基线」写入")
    return save_pack(path, payload)
