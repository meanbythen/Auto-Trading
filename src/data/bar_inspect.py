"""
抽查某一根本地 K 线。

功能：按数据源、合约、周期、日期、时间，读清洗库和原样库里这一根（或最接近的一根）。
原理：开仓看清洗库；原样库是接口刚下来、还没对齐重拼的。CSV 是同一组的备份，Excel 能打开。
不下单、不拉网。同花顺信息框里的今结、涨跌、持仓本地没有，只能对开高低收和量。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.bar_store import BarStore, RawBarStore
from src.data.periods import cache_tag, canonical_period, period_label
from src.data.quote_check import normalize_source, source_label, storage_symbol


def parse_bar_when(date_text: str, time_text: str, period: str) -> str:
    """
    把日期和时间收成库里那种「2026-09-22 10:50:00」。

    日线、周线没填时间就用 00:00:00（清洗库日线是交易日零点）。
    分钟线必须填到分钟，秒可省略。
    """
    day = str(date_text or "").strip().replace("/", "-")
    clock = str(time_text or "").strip()
    if not day:
        raise ValueError("请填写日期，例如 2026-09-22")
    parts = day.replace(".", "-").split("-")
    if len(parts) != 3:
        raise ValueError("日期请写成 2026-09-22")
    try:
        year, month, day_n = int(parts[0]), int(parts[1]), int(parts[2])
        datetime(year, month, day_n)
    except ValueError as exc:
        raise ValueError("日期不对，请写成 2026-09-22") from exc
    day = f"{year:04d}-{month:02d}-{day_n:02d}"
    code = canonical_period(period)
    if not clock:
        if code in {"D", "W"}:
            clock = "00:00:00"
        else:
            raise ValueError("分钟线请填写时间，例如 10:50（这根K线的结束时刻）")
    if len(clock) == 5:
        clock = clock + ":00"
    try:
        datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("时间请写成 10:50 或 10:50:00") from exc
    return f"{day} {clock}"


def kline_paths(root: Path | str, stored: str, period: str) -> dict[str, Path]:
    """这一组 K 线对应的本地文件。sqlite 用专业工具打开；CSV 可直接用 Excel。"""
    folder = Path(root) / "data" / "klines"
    tag = cache_tag(period)
    safe = stored.replace("/", "_")
    return {
        "folder": folder,
        "clean_db": folder / "market.sqlite",
        "raw_db": folder / "raw.sqlite",
        "csv": folder / f"{safe}_{tag}.csv",
    }


def _csv_row(csv_path: Path, ts: str) -> dict[str, Any] | None:
    if not csv_path.exists():
        return None
    try:
        frame = pd.read_csv(csv_path)
    except Exception:
        return None
    if frame.empty:
        return None
    time_col = "datetime" if "datetime" in frame.columns else frame.columns[0]
    frame[time_col] = pd.to_datetime(frame[time_col], errors="coerce")
    want = pd.Timestamp(ts)
    hit = frame[frame[time_col] == want]
    if hit.empty:
        return None
    row = hit.iloc[0]
    cols = {str(c).lower(): c for c in frame.columns}

    def _num(name: str) -> float:
        key = cols.get(name)
        if key is None:
            return 0.0
        try:
            return float(row[key] or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "ts": want.strftime("%Y-%m-%d %H:%M:%S"),
        "open": _num("open"),
        "high": _num("high"),
        "low": _num("low"),
        "close": _num("close"),
        "volume": _num("volume"),
    }


def _fmt_bar(bar: dict[str, Any] | None, title: str = "") -> list[str]:
    if not bar:
        return [f"{title}  没有"] if title else ["没有"]
    body = [
        f"时间  {bar.get('ts')}",
        f"开盘  {bar.get('open')}    最高  {bar.get('high')}    "
        f"最低  {bar.get('low')}    收盘  {bar.get('close')}",
        f"成交量  {bar.get('volume')}",
    ]
    if title:
        return [title, *body]
    return body


def inspect_kline(
    root: Path | str,
    *,
    source: str,
    symbol: str,
    period: str,
    date_text: str,
    time_text: str = "",
) -> dict[str, Any]:
    """
    抽查一根本地 K 线。

    返回清洗根、原样根、CSV 是否对上、文件路径。没有精确时刻时用当天最接近的一根。
    """
    code = normalize_source(source)
    period = canonical_period(period)
    contract = str(symbol or "").strip()
    if not contract:
        raise ValueError("请填写合约，例如 fu2701")
    when = parse_bar_when(date_text, time_text, period)
    stored = storage_symbol(contract, code)
    paths = kline_paths(root, stored, period)
    clean_store = BarStore(paths["clean_db"])
    raw_store = RawBarStore(paths["raw_db"])
    exact = clean_store.fetch_one(stored, period, when)
    clean = exact or clean_store.fetch_nearest(stored, period, when)
    raw_exact = raw_store.fetch_one(stored, period, code, when)
    raw = raw_exact or raw_store.fetch_nearest(stored, period, code, when)
    anchor = str((clean or raw or {}).get("ts") or when)
    csv_bar = _csv_row(paths["csv"], anchor) or _csv_row(paths["csv"], when)
    return {
        "when": when,
        "source": code,
        "source_label": source_label(code),
        "symbol": contract,
        "stored": stored,
        "period": period,
        "period_label": period_label(period),
        "exact": bool(exact),
        "clean": clean,
        "raw": raw,
        "raw_exact": bool(raw_exact),
        "clean_prev": clean_store.fetch_prev(stored, period, anchor) if clean else None,
        "clean_next": clean_store.fetch_next(stored, period, anchor) if clean else None,
        "csv": csv_bar,
        "paths": {key: str(path) for key, path in paths.items()},
        "csv_exists": paths["csv"].exists(),
        "folder_exists": paths["folder"].exists(),
    }


def format_kline_inspect(book: dict[str, Any]) -> str:
    """给人看的完整说明，方便和同花顺信息框对开高低收。"""
    lines = [
        f"数据源  {book.get('source_label')}    合约  {book.get('symbol')}    "
        f"周期  {book.get('period_label')}",
        f"库存名  {book.get('stored')}",
        f"你要找的时刻  {book.get('when')}（右标签：这根K线的结束时刻）",
        "",
        "对照同花顺时，只对开盘、最高、最低、收盘、成交量。"
        "信息框里的今结、涨跌、持仓、日增仓本地库没有。",
        "",
    ]
    if book.get("clean"):
        match = "正好是这一根" if book.get("exact") else "没有正好这一刻，下面是最接近的一根"
        lines.append(f"【清洗库】开仓看这份。{match}")
        lines.extend(_fmt_bar(book.get("clean")))
        lines.extend(_fmt_bar(book.get("clean_prev"), "上一根"))
        lines.extend(_fmt_bar(book.get("clean_next"), "下一根"))
    else:
        lines.append("【清洗库】没有这根。请确认数据源、合约、周期，或先拉过这一家。")
    lines.append("")
    if book.get("raw"):
        match = "正好是这一根" if book.get("raw_exact") else "没有正好这一刻，下面是最接近的一根"
        lines.append(f"【接口原样】还没对齐、没重拼。{match}")
        lines.extend(_fmt_bar(book.get("raw")))
    else:
        lines.append("【接口原样】没有这根。")
    lines.append("")
    if book.get("csv"):
        lines.append("【CSV 备份】Excel 能打开这一组文件，下面是同一时刻那一行。")
        lines.extend(_fmt_bar(book.get("csv")))
    elif book.get("csv_exists"):
        lines.append("【CSV 备份】文件在，但这一时刻没有对应行。")
    else:
        lines.append("【CSV 备份】还没有这个文件。可点「打开库存文件夹」看 sqlite。")
    paths = book.get("paths") or {}
    lines.extend(
        [
            "",
            "【文件位置】sqlite 不要用 Excel 硬打开，用下面按钮打开 CSV 或文件夹。",
            f"清洗库  {paths.get('clean_db')}",
            f"原样库  {paths.get('raw_db')}",
            f"CSV     {paths.get('csv')}",
        ]
    )
    return "\n".join(line for line in lines if line is not None).replace("\n\n\n", "\n\n")
