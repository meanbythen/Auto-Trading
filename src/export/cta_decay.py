"""
多因子衰减表

功能：把回测/实盘的每一笔四因子得分和分摊盈亏，另存一份「回测评估与因子衰减监控」Excel。
     文件名带策略和时间，不覆盖以前导出的表。
原理：不改表里的公式。只填「交易明细」黄底格子（开平仓、分数、主导因子、ATR、成本），
     初始风险、盈亏、盈亏R、样本归属仍由表格自己算。「因子绩效」就会按主导因子统计衰减。
     分摊金额写在备注里，方便你对照资料里的 |得分| 占比算法。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from openpyxl import load_workbook

from src.strategy.cta_factors import FACTOR_LABELS, allocate_factor_pnl, scores_from_meta

TEMPLATE_NAME = "多因子复合CTA回测评估与因子衰减监控模板.xlsx"
# 程序用的模板放 config，和品种表一样。旧位置只当找不到时的退路。
_TEMPLATE_RELS = (
    Path("config") / TEMPLATE_NAME,
    Path("docs") / "多因子复合 CTA 策略" / TEMPLATE_NAME,
    Path("多因子复合 CTA 策略") / TEMPLATE_NAME,
)
DEST_PREFIX = "多因子衰减监控"
LIVE_LOG_NAME = "cta_factor_trades.jsonl"
_FILE_BAD = re.compile(r'[<>:"/\\|?*]')

# 文件名里用短一点的策略名，避免括号和「主推」把名字拉太长。
_ENGINE_FILE_NAMES = {
    "multi_factor": "加权打分",
    "cta": "加权打分",
}

# 交易明细：第 1～3 行是标题，第 4 行起是数据，最后两行是说明不要动。
_HEADER_ROW = 3
_FIRST_DATA_ROW = 4
_NOTE_ROW = 206

# 黄底要手填的列（1 起算）。L/Q/R/U/V/W 是公式，从第 4 行抄下来改行号。
_COL = {
    "seq": 1,
    "entry_time": 2,
    "symbol": 3,
    "side": 4,
    "entry_price": 5,
    "volume": 6,
    "multiplier": 7,
    "score": 8,
    "dominant": 9,
    "resonance": 10,
    "atr": 11,
    "risk": 12,
    "exit_time": 13,
    "exit_price": 14,
    "reason_exit": 15,
    "cost": 16,
    "pnl": 17,
    "r": 18,
    "bars": 19,
    "adx": 20,
    "streak": 21,
    "sample": 22,
    "days": 23,
    "note": 24,
}
_FORMULA_COLS = (12, 17, 18, 21, 22, 23)


def _safe_file_part(text: str, fallback: str) -> str:
    """Windows 文件名不能有 : * ? 这类符号，空了就用兜底字。"""
    cleaned = _FILE_BAD.sub("", str(text or "")).replace("·", "").strip()
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned or fallback


def _engine_file_name(payload: dict[str, Any] | None) -> str:
    data = payload or {}
    snap = data.get("params_snapshot") if isinstance(data.get("params_snapshot"), dict) else {}
    engine = str(snap.get("engine") or data.get("engine") or "").strip().lower()
    if engine in _ENGINE_FILE_NAMES:
        return _ENGINE_FILE_NAMES[engine]
    if engine:
        try:
            from src.backtest.param_history import strategy_display_name

            return _safe_file_part(strategy_display_name(engine), engine)
        except Exception:  # noqa: BLE001
            return _safe_file_part(engine, "多因子")
    return "多因子"


def _stamp_file_name(payload: dict[str, Any] | None) -> str:
    """时间写成 2026-11-08_00-52-30，不能用冒号。"""
    data = payload or {}
    raw = str(data.get("ran_at") or "").strip().replace("T", " ")
    if len(raw) >= 19 and raw[4] == "-":
        when = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    else:
        when = datetime.now()
    return when.strftime("%Y-%m-%d_%H-%M-%S")


def decay_export_path(root: Path | str, payload: dict[str, Any] | None = None) -> Path:
    """
    每次导出一份新文件：多因子衰减监控-加权打分-2026-11-08_00-52-30.xlsx。
    同名已存在时在后面加 -2、-3，绝不覆盖旧文件。
    """
    folder = Path(root) / "data"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{DEST_PREFIX}-{_engine_file_name(payload)}-{_stamp_file_name(payload)}"
    dest = folder / f"{stem}.xlsx"
    n = 2
    while dest.exists():
        dest = folder / f"{stem}-{n}.xlsx"
        n += 1
    return dest


def live_log_path(root: Path | str | None = None) -> Path:
    from src.utils.paths import project_root

    base = Path(root) if root else project_root()
    return base / "data" / LIVE_LOG_NAME


def template_path(root: Path | str) -> Path:
    """
    衰减监控 Excel 模板在哪。

    正式位置是 config。docs 和项目根下旧资料夹只是以前放过，还能认。
    都没有时返回 config 那条路径，报错时能看出该把文件放哪。
    """
    base = Path(root)
    preferred = base / _TEMPLATE_RELS[0]
    for rel in _TEMPLATE_RELS:
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return preferred


def copy_fresh_decay_workbook(root: Path | str, payload: dict[str, Any] | None = None) -> Path:
    """从资料模板复制一份新表，公式原样保留，不碰已经导出过的文件。"""
    from src.utils.paths import project_root

    dest = decay_export_path(root, payload)
    src = template_path(project_root())
    if not src.exists():
        raise FileNotFoundError(f"找不到衰减监控模板：{src}")
    shutil.copy2(src, dest)
    logger.info("已复制多因子衰减监控表到 {}", dest)
    return dest


def append_live_factor_trade(row: dict[str, Any], root: Path | str | None = None) -> None:
    """实盘/模拟每平一笔追加一行，导出衰减表时会一起写进去。"""
    path = live_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(row)
    payload.setdefault("logged_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_decay_from_backtest(root: Path | str, payload: dict[str, Any] | None = None) -> Path | None:
    """
    用最近一次回测（可再拼上实盘分摊日志）另存一份衰减表。

    没有四因子分数的策略会跳过，避免把日内实体的成交写进这张表。
    每次导出新文件，不覆盖以前的。
    """
    from src.utils.paths import project_root

    base = Path(root) if root else project_root()
    data = payload
    if data is None:
        last = base / "data" / "backtest_last.json"
        if last.exists():
            data = json.loads(last.read_text(encoding="utf-8"))
    rows = _trade_rows(data or {}, base)
    if not rows:
        logger.info("这次没有带四因子分数的成交，衰减表不改")
        return None
    dest = copy_fresh_decay_workbook(base, data or {})
    _write_trade_sheet(dest, rows)
    logger.info("已写入多因子衰减表 {} 笔 → {}", len(rows), dest)
    return dest


def _trade_rows(payload: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload.get("trades") or []:
        mapped = _map_trade(item)
        if mapped is not None:
            rows.append(mapped)
    log_path = live_log_path(root)
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mapped = _map_trade(json.loads(line))
            except json.JSONDecodeError:
                continue
            if mapped is not None:
                rows.append(mapped)
    return rows


def _map_trade(item: dict[str, Any]) -> dict[str, Any] | None:
    """没有因子分数的成交不进衰减表。"""
    if item.get("score_ma") is None and item.get("score") is None:
        return None
    scores = scores_from_meta(item)
    pnl = float(item.get("pnl") or 0.0)
    allocated = allocate_factor_pnl(scores, pnl)
    note_bits = [
        f"{FACTOR_LABELS[key]}{allocated[key]:+.2f}"
        for key in ("ma", "turtle", "boll", "sar")
        if abs(allocated[key]) > 1e-9
    ]
    side_raw = str(item.get("side") or "")
    if side_raw in {"long", "多"}:
        side = "多"
    elif side_raw in {"short", "空"}:
        side = "空"
    else:
        side = side_raw
    return {
        "entry_time": _as_dt(item.get("entry_time")),
        "symbol": str(item.get("symbol") or ""),
        "side": side,
        "entry_price": item.get("entry_price"),
        "volume": item.get("volume"),
        "multiplier": item.get("multiplier") or None,
        "score": item.get("score_total", item.get("score")),
        "dominant": item.get("dominant_factor") or "",
        "resonance": item.get("resonance") or "",
        "atr": item.get("atr"),
        "exit_time": _as_dt(item.get("exit_time")),
        "exit_price": item.get("exit_price"),
        "reason_exit": item.get("reason_exit") or item.get("reason") or "",
        "cost": item.get("commission"),
        "note": "分摊 " + " ".join(note_bits) if note_bits else "",
    }


def _as_dt(value: Any) -> datetime | str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return text


def _write_trade_sheet(path: Path, rows: list[dict[str, Any]]) -> None:
    book = load_workbook(path)
    sheet = book["交易明细"]
    templates = {col: sheet.cell(_FIRST_DATA_ROW, col).value for col in _FORMULA_COLS}
    last_clear = max(_NOTE_ROW - 1, _FIRST_DATA_ROW)
    for row_idx in range(_FIRST_DATA_ROW, last_clear):
        for col in range(1, 25):
            if col in _FORMULA_COLS:
                continue
            sheet.cell(row_idx, col).value = None
    for offset, row in enumerate(rows):
        excel_row = _FIRST_DATA_ROW + offset
        if excel_row >= _NOTE_ROW:
            logger.warning("衰减表「交易明细」最多写到第 {} 行，后面 {} 笔没写进去", _NOTE_ROW - 1, len(rows) - offset)
            break
        sheet.cell(excel_row, _COL["seq"]).value = offset + 1
        sheet.cell(excel_row, _COL["entry_time"]).value = row["entry_time"]
        sheet.cell(excel_row, _COL["symbol"]).value = row["symbol"]
        sheet.cell(excel_row, _COL["side"]).value = row["side"]
        sheet.cell(excel_row, _COL["entry_price"]).value = row["entry_price"]
        sheet.cell(excel_row, _COL["volume"]).value = row["volume"]
        sheet.cell(excel_row, _COL["multiplier"]).value = row["multiplier"]
        sheet.cell(excel_row, _COL["score"]).value = row["score"]
        sheet.cell(excel_row, _COL["dominant"]).value = row["dominant"]
        sheet.cell(excel_row, _COL["resonance"]).value = row["resonance"]
        sheet.cell(excel_row, _COL["atr"]).value = row["atr"]
        sheet.cell(excel_row, _COL["exit_time"]).value = row["exit_time"]
        sheet.cell(excel_row, _COL["exit_price"]).value = row["exit_price"]
        sheet.cell(excel_row, _COL["reason_exit"]).value = row["reason_exit"]
        sheet.cell(excel_row, _COL["cost"]).value = row["cost"]
        sheet.cell(excel_row, _COL["note"]).value = row["note"]
        for col, template in templates.items():
            if isinstance(template, str) and template.startswith("="):
                sheet.cell(excel_row, col).value = _adapt_formula(template, excel_row)
    book.save(path)
    book.close()


def _adapt_formula(template: str, row: int) -> str:
    """把第 4 行公式改成当前行。连亏计数要看上一行，不能把参数表里的 $B$10 改坏。"""
    text = re.sub(r"\$([A-Z]+)4\b", lambda match: f"${match.group(1)}{row}", template)
    return re.sub(r"\$U3\b", f"$U{row - 1}", text)
