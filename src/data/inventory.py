"""
本地 K 线库存总览。

只读已经存进 sqlite 的数据，不拉网、不下单。
给人看：哪一家、原样还是清洗、哪个合约、什么周期、从哪天到哪天、有多少根。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from src.data.bar_store import BarStore, RawBarStore
from src.data.periods import cache_tag, canonical_period, csv_skip_marker, period_label
from src.data.quote_check import normalize_source, product_code, storage_symbol, stored_source_label


LATEST_NAME = "K线库存.xlsx"


def split_stored_symbol(stored: str) -> tuple[str, str]:
    """
    库存名拆成合约和来源。

    fu2701 → 新浪；fu2701@tq → 天勤；fu2701@ths → 同花顺图。残留的 fu2701@eastmoney 标成已停用。
    """
    text = str(stored or "").strip()
    if text.endswith("@eastmoney"):
        return text[: -len("@eastmoney")], "eastmoney"
    if text.endswith("@tq"):
        return text[: -len("@tq")], "tq"
    if text.endswith("@ths"):
        return text[: -len("@ths")], "ths"
    return text, "akshare"


def _age_minutes(ts_to: str) -> float | None:
    if not ts_to:
        return None
    try:
        last = datetime.fromisoformat(str(ts_to)[:19])
    except ValueError:
        return None
    return (datetime.now() - last).total_seconds() / 60.0


def _span_days(ts_from: str, ts_to: str) -> float | None:
    if not ts_from or not ts_to:
        return None
    try:
        a = datetime.fromisoformat(str(ts_from)[:19])
        b = datetime.fromisoformat(str(ts_to)[:19])
    except ValueError:
        return None
    return max(0.0, (b - a).total_seconds() / 86400.0)


def _row(
    *,
    layer: str,
    stored: str,
    period: str,
    n: int,
    ts_from: str,
    ts_to: str,
    source_code: str | None = None,
) -> dict[str, Any]:
    contract, from_name = split_stored_symbol(stored)
    src = source_code or from_name
    age = _age_minutes(ts_to)
    span = _span_days(ts_from, ts_to)
    return {
        "哪一份": layer,
        "数据源": stored_source_label(src),
        "品种": product_code(contract) or contract,
        "合约": contract,
        "周期": period_label(period),
        "根数": int(n),
        "从": ts_from,
        "到": ts_to,
        "跨度天数": None if span is None else round(span, 2),
        "距今分钟": None if age is None else round(age, 1),
        "库存名": stored,
    }


def collect_kline_inventory(root: Path | str) -> dict[str, Any]:
    """扫清洗库和原样库，收成总览数字 + 明细行。"""
    base = Path(root)
    clean_store = BarStore(base / "data" / "klines" / "market.sqlite")
    raw_store = RawBarStore(base / "data" / "klines" / "raw.sqlite")
    detail: list[dict[str, Any]] = []
    for item in clean_store.summarize():
        detail.append(
            _row(
                layer="清洗后（开仓看这份）",
                stored=item["symbol"],
                period=item["period"],
                n=item["n"],
                ts_from=item["ts_from"],
                ts_to=item["ts_to"],
            )
        )
    for item in raw_store.summarize():
        detail.append(
            _row(
                layer="接口原样",
                stored=item["symbol"],
                period=item["period"],
                n=item["n"],
                ts_from=item["ts_from"],
                ts_to=item["ts_to"],
                source_code=item.get("source"),
            )
        )
    detail.sort(key=lambda row: (str(row["数据源"]), str(row["合约"]), str(row["周期"]), str(row["哪一份"])))
    clean_rows = [row for row in detail if str(row["哪一份"]).startswith("清洗")]
    raw_rows = [row for row in detail if str(row["哪一份"]).startswith("接口")]
    by_source: dict[str, dict[str, Any]] = {}
    for row in detail:
        name = str(row["数据源"])
        box = by_source.setdefault(
            name,
            {
                "数据源": name,
                "清洗组数": 0,
                "清洗根数": 0,
                "原样组数": 0,
                "原样根数": 0,
            },
        )
        if str(row["哪一份"]).startswith("清洗"):
            box["清洗组数"] += 1
            box["清洗根数"] += int(row["根数"])
        else:
            box["原样组数"] += 1
            box["原样根数"] += int(row["根数"])
    by_contract: dict[str, dict[str, Any]] = {}
    for row in detail:
        key = str(row["合约"])
        box = by_contract.setdefault(
            key,
            {
                "合约": key,
                "品种": row["品种"],
                "数据源": set(),
                "周期": set(),
                "清洗根数": 0,
                "原样根数": 0,
            },
        )
        box["数据源"].add(str(row["数据源"]))
        box["周期"].add(str(row["周期"]))
        if str(row["哪一份"]).startswith("清洗"):
            box["清洗根数"] += int(row["根数"])
        else:
            box["原样根数"] += int(row["根数"])
    contract_rows = []
    for box in by_contract.values():
        contract_rows.append(
            {
                "合约": box["合约"],
                "品种": box["品种"],
                "数据源": "、".join(sorted(box["数据源"])),
                "周期": "、".join(sorted(box["周期"])),
                "清洗根数": box["清洗根数"],
                "原样根数": box["原样根数"],
            }
        )
    contract_rows.sort(key=lambda row: str(row["合约"]))
    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "clean_groups": len(clean_rows),
        "clean_bars": sum(int(row["根数"]) for row in clean_rows),
        "raw_groups": len(raw_rows),
        "raw_bars": sum(int(row["根数"]) for row in raw_rows),
        "sources": sorted({str(row["数据源"]) for row in detail}),
        "contracts": sorted({str(row["合约"]) for row in detail if row["合约"]}),
        "detail": detail,
        "by_source": [by_source[name] for name in sorted(by_source)],
        "by_contract": contract_rows,
        "ingests": clean_store.list_ingests(200),
    }


def _write_kv(ws: Any, pairs: list[tuple[str, Any]]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    ws.append(["项目", "内容"])
    ws["A1"].fill = PatternFill("solid", fgColor="1F4E79")
    ws["B1"].fill = PatternFill("solid", fgColor="1F4E79")
    ws["A1"].font = Font(color="FFFFFF", bold=True)
    ws["B1"].font = Font(color="FFFFFF", bold=True)
    for key, value in pairs:
        ws.append([key, value])
        ws[f"B{ws.max_row}"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 70


def _write_table(ws: Any, columns: list[str], rows: list[dict[str, Any]]) -> None:
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="1F4E79")
    head_font = Font(color="FFFFFF", bold=True)
    ws.append(columns)
    for cell in ws[1]:
        cell.fill = head_fill
        cell.font = head_font
    if not rows:
        empty = {col: None for col in columns}
        empty[columns[0]] = "（本地还没有K线）"
        rows = [empty]
    for rec in rows:
        ws.append([rec.get(col) for col in columns])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{ws.max_row}"
    for idx, col in enumerate(ws.columns, start=1):
        longest = 8
        for cell in col[:60]:
            longest = max(longest, min(len(str(cell.value or "")), 28))
        ws.column_dimensions[get_column_letter(idx)].width = longest + 2


def _ingest_row(item: dict[str, Any]) -> dict[str, Any]:
    """一条读图批次转成 Excel 一行。"""

    def _span(start: Any, end: Any) -> str:
        a = str(start or "").strip()
        b = str(end or "").strip()
        if not a and not b:
            return "未指定"
        return f"{a or '起'} ~ {b or '止'}"

    contract, from_name = split_stored_symbol(str(item.get("symbol") or ""))
    expected = int(item.get("expected_n") or 0)
    return {
        "读取时间": item.get("created_at") or "",
        "数据源": stored_source_label(str(item.get("source") or from_name)),
        "合约": contract,
        "周期": period_label(str(item.get("period") or "")),
        "请求时段": _span(item.get("requested_from"), item.get("requested_to")),
        "实读范围": _span(item.get("read_from"), item.get("read_to")),
        "根数": int(item.get("read_n") or 0),
        "新增": int(item.get("added_n") or 0),
        "应有": expected or None,
        "缺失": int(item.get("missing_n") or 0) if expected else None,
        "说明": item.get("note") or "",
    }


def write_inventory_excel(path: Path, book: dict[str, Any]) -> None:
    """写成给人看的 Excel：先总览，再按来源/合约，最后一组一行明细。"""
    from openpyxl import Workbook

    wb = Workbook()
    help_ws = wb.active
    help_ws.title = "说明"
    _write_kv(
        help_ws,
        [
            ("这是什么", "电脑里已经存下的 K 线总账。只读本地库，不拉网、不下单。"),
            ("清洗后", "开仓和回测看这一份。天勤带 @tq，同花顺图带 @ths，新浪不带后缀。"),
            ("接口原样", "刚从网上或图上读下来、还没清洗的那一份。"),
            ("读取记录", "每次从同花顺图读一批就记一行：请求时段、实读范围、根数、新增和缺口。"),
            ("怎么看", "「总览」看一共有多少；「明细」可按数据源、合约、周期筛选。"),
            ("检查时间", book.get("checked_at") or ""),
        ],
    )
    overview = wb.create_sheet("总览")
    _write_kv(
        overview,
        [
            ("检查时间", book.get("checked_at") or ""),
            ("清洗后组数", book.get("clean_groups") or 0),
            ("清洗后总根数", book.get("clean_bars") or 0),
            ("接口原样组数", book.get("raw_groups") or 0),
            ("接口原样总根数", book.get("raw_bars") or 0),
            ("数据源", "、".join(book.get("sources") or []) or "（无）"),
            ("合约个数", len(book.get("contracts") or [])),
            ("合约名单", "见「按合约」表，不在这里挤成一长串"),
        ],
    )
    source_ws = wb.create_sheet("按数据源")
    _write_table(
        source_ws,
        ["数据源", "清洗组数", "清洗根数", "原样组数", "原样根数"],
        list(book.get("by_source") or []),
    )
    contract_ws = wb.create_sheet("按合约")
    _write_table(
        contract_ws,
        ["合约", "品种", "数据源", "周期", "清洗根数", "原样根数"],
        list(book.get("by_contract") or []),
    )
    detail_ws = wb.create_sheet("明细")
    _write_table(
        detail_ws,
        ["哪一份", "数据源", "品种", "合约", "周期", "根数", "从", "到", "跨度天数", "距今分钟", "库存名"],
        list(book.get("detail") or []),
    )
    ingest_ws = wb.create_sheet("读取记录")
    _write_table(
        ingest_ws,
        ["读取时间", "数据源", "合约", "周期", "请求时段", "实读范围", "根数", "新增", "应有", "缺失", "说明"],
        [_ingest_row(item) for item in (book.get("ingests") or [])],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def write_kline_inventory(root: Path | str, book: dict[str, Any] | None = None) -> Path:
    """扫一遍本地库，写出带时间的 Excel，并更新 data/K线库存.xlsx。"""
    base = Path(root)
    folder = base / "data"
    folder.mkdir(parents=True, exist_ok=True)
    payload = book if book is not None else collect_kline_inventory(base)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dated = folder / f"K线库存-{stamp}.xlsx"
    write_inventory_excel(dated, payload)
    try:
        write_inventory_excel(folder / LATEST_NAME, payload)
    except Exception:  # noqa: BLE001
        pass
    return dated


def inventory_message(book: dict[str, Any]) -> str:
    """弹窗里用的短句。合约名单太长，只报个数，明细看 Excel。"""
    if not book.get("detail"):
        return "本地还没有存下的 K 线。请先拉行情或从同花顺图读取。"
    n_contracts = len(book.get("contracts") or [])
    sources = "、".join(book.get("sources") or []) or "无"
    return (
        f"清洗后 {book.get('clean_groups') or 0} 组、共 {book.get('clean_bars') or 0} 根；"
        f"接口原样 {book.get('raw_groups') or 0} 组、共 {book.get('raw_bars') or 0} 根。"
        f"来源 {sources}，合约 {n_contracts} 个。明细请看 Excel。"
    )


def _stamp_bound(value: str, *, end: bool) -> str:
    """日期补成当天 00:00:00 或 23:59:59，方便按时间段删。"""
    text = str(value or "").strip().replace("/", "-")
    if not text:
        return ""
    if len(text) <= 10:
        return text[:10] + (" 23:59:59" if end else " 00:00:00")
    return text[:19]


def _clean_where(
    *,
    source: str | None,
    symbol: str | None,
    period: str | None,
    ts_from: str | None,
    ts_to: str | None,
) -> tuple[str, tuple[Any, ...]]:
    """清洗库按库存名区分来源（新浪没有后缀）。"""
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if period:
        clauses.append("period = ?")
        params.append(canonical_period(period))
    start = _stamp_bound(ts_from or "", end=False)
    stop = _stamp_bound(ts_to or "", end=True)
    if start:
        clauses.append("ts >= ?")
        params.append(start)
    if stop:
        clauses.append("ts <= ?")
        params.append(stop)
    src = normalize_source(source) if source else ""
    name = str(symbol or "").strip()
    if name and src:
        clauses.append("symbol = ?")
        params.append(storage_symbol(name, src))
    elif name:
        clauses.append("(symbol = ? OR symbol LIKE ?)")
        params.extend([name, f"{name}@%"])
    elif src == "akshare":
        clauses.append("symbol NOT LIKE '%@%'")
    elif src:
        suffix = {"eastmoney": "@eastmoney", "tq": "@tq", "ths": "@ths"}.get(src, "")
        clauses.append("symbol LIKE ?")
        params.append(f"%{suffix}")
    return " AND ".join(clauses), tuple(params)


def _raw_where(
    *,
    source: str | None,
    symbol: str | None,
    period: str | None,
    ts_from: str | None,
    ts_to: str | None,
) -> tuple[str, tuple[Any, ...]]:
    """原样库自带 source 列。"""
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if period:
        clauses.append("period = ?")
        params.append(canonical_period(period))
    start = _stamp_bound(ts_from or "", end=False)
    stop = _stamp_bound(ts_to or "", end=True)
    if start:
        clauses.append("ts >= ?")
        params.append(start)
    if stop:
        clauses.append("ts <= ?")
        params.append(stop)
    src = normalize_source(source) if source else ""
    if src:
        clauses.append("source = ?")
        params.append(src)
    name = str(symbol or "").strip()
    if name and src:
        clauses.append("symbol = ?")
        params.append(storage_symbol(name, src))
    elif name:
        clauses.append("(symbol = ? OR symbol LIKE ?)")
        params.extend([name, f"{name}@%"])
    return " AND ".join(clauses), tuple(params)


def preview_purge(
    root: Path | str,
    *,
    source: str | None = None,
    symbol: str | None = None,
    period: str | None = None,
    ts_from: str | None = None,
    ts_to: str | None = None,
    layers: str = "both",
) -> dict[str, Any]:
    """先数会删多少根，还不真删。"""
    if not str(source or "").strip() and not str(symbol or "").strip():
        raise ValueError("请至少选一个数据源，或填一个合约，避免把整个库清空。")
    base = Path(root)
    clean_store = BarStore(base / "data" / "klines" / "market.sqlite")
    raw_store = RawBarStore(base / "data" / "klines" / "raw.sqlite")
    want_clean = layers in {"both", "clean"}
    want_raw = layers in {"both", "raw"}
    c_sql, c_params = _clean_where(
        source=source, symbol=symbol, period=period, ts_from=ts_from, ts_to=ts_to
    )
    r_sql, r_params = _raw_where(
        source=source, symbol=symbol, period=period, ts_from=ts_from, ts_to=ts_to
    )
    clean_n = clean_store.count_where(c_sql, c_params) if want_clean else 0
    raw_n = raw_store.count_where(r_sql, r_params) if want_raw else 0
    return {
        "clean_n": clean_n,
        "raw_n": raw_n,
        "clean_keys": clean_store.keys_where(c_sql, c_params) if want_clean else [],
        "raw_keys": raw_store.keys_where(r_sql, r_params) if want_raw else [],
    }


def _csv_busy_error(exc: BaseException) -> bool:
    """Excel 打开着这份 csv 时，Windows 会拒绝删除或覆盖。"""
    if isinstance(exc, PermissionError):
        return True
    winerr = getattr(exc, "winerror", None)
    return winerr == 32


def _mark_csv_skip(path: Path) -> None:
    """告诉清洗库：这份旧 CSV 不要再补回来。"""
    marker = csv_skip_marker(path)
    marker.write_text("purged\n", encoding="utf-8")


def _clear_csv_skip(path: Path) -> None:
    marker = csv_skip_marker(path)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def _parse_kline_csv_name(filename: str) -> tuple[str, str] | None:
    """从 fu2701@ths_5m.csv 读出库存名和周期。"""
    stem = Path(filename).stem
    if "_" not in stem:
        return None
    stored, tag = stem.rsplit("_", 1)
    if not stored:
        return None
    if tag.endswith("m") and tag[:-1].isdigit():
        return stored, tag[:-1]
    if tag == "1d":
        return stored, "D"
    if tag == "1w":
        return stored, "W"
    return None


def _csv_keys_on_disk(
    cache_dir: Path,
    *,
    source: str | None,
    symbol: str | None,
    period: str | None,
) -> list[tuple[str, str]]:
    """
    库已经空了、CSV 还躺在文件夹里时，preview 数不到 key。
    仍按你选的数据源/合约把这些文件找出来，避免下次读图又补回去。
    """
    src = normalize_source(source) if source else ""
    name = str(symbol or "").strip()
    want_period = canonical_period(period) if period else ""
    stored_exact = storage_symbol(name, src) if name and src else ""
    found: list[tuple[str, str]] = []
    if not cache_dir.is_dir():
        return found
    for path in cache_dir.glob("*.csv"):
        parsed = _parse_kline_csv_name(path.name)
        if parsed is None:
            continue
        stored, csv_period = parsed
        if want_period and csv_period != want_period:
            continue
        if stored_exact:
            if stored.replace("/", "_") != stored_exact.replace("/", "_"):
                continue
        elif name:
            code, _src = split_stored_symbol(stored)
            if code.lower() != name.lower() and not stored.lower().startswith(name.lower() + "@"):
                continue
        elif src == "akshare":
            if "@" in stored:
                continue
        elif src:
            suffix = {"eastmoney": "@eastmoney", "tq": "@tq", "ths": "@ths"}.get(src, "")
            if suffix and not stored.endswith(suffix):
                continue
        found.append((stored, csv_period))
    return found


def _sync_csv(
    cache_dir: Path, store: BarStore, keys: list[tuple[str, str]]
) -> tuple[int, list[str]]:
    """
    删完 sqlite 后，对应的 csv 也改成剩下的，免得下次又补回去。

    返回 (改成功的文件数, 正被 Excel 占用改不了的路径)。
    占用时写下 .skip 标记，不抛错：库已经删了，只提醒你关掉表格再删一次。
    """
    touched = 0
    locked: list[str] = []
    seen: set[tuple[str, str]] = set()
    for stored, period in keys:
        pair = (stored, str(period))
        if pair in seen:
            continue
        seen.add(pair)
        path = cache_dir / f"{stored.replace('/', '_')}_{cache_tag(period)}.csv"
        left = store.load(stored, period)
        try:
            if left is None or len(left) == 0:
                if path.exists():
                    path.unlink()
                    touched += 1
                _clear_csv_skip(path)
                continue
            left.to_csv(path, encoding="utf-8-sig")
            _clear_csv_skip(path)
            touched += 1
        except OSError as exc:
            if not _csv_busy_error(exc):
                raise
            _mark_csv_skip(path)
            locked.append(str(path))
    return touched, locked


def purge_klines(
    root: Path | str,
    *,
    source: str | None = None,
    symbol: str | None = None,
    period: str | None = None,
    ts_from: str | None = None,
    ts_to: str | None = None,
    layers: str = "both",
) -> dict[str, Any]:
    """
    按数据源 / 合约 / 周期 / 时间段删除本地 K 线。

    清洗库、原样库、对应 csv 一起处理。不拉网、不下单。
    """
    preview = preview_purge(
        root,
        source=source,
        symbol=symbol,
        period=period,
        ts_from=ts_from,
        ts_to=ts_to,
        layers=layers,
    )
    base = Path(root)
    cache_dir = base / "data" / "klines"
    clean_store = BarStore(cache_dir / "market.sqlite")
    raw_store = RawBarStore(cache_dir / "raw.sqlite")
    want_clean = layers in {"both", "clean"}
    want_raw = layers in {"both", "raw"}
    c_sql, c_params = _clean_where(
        source=source, symbol=symbol, period=period, ts_from=ts_from, ts_to=ts_to
    )
    r_sql, r_params = _raw_where(
        source=source, symbol=symbol, period=period, ts_from=ts_from, ts_to=ts_to
    )
    clean_deleted = clean_store.delete_where(c_sql, c_params) if want_clean else 0
    raw_deleted = raw_store.delete_where(r_sql, r_params) if want_raw else 0
    csv_n = 0
    csv_locked: list[str] = []
    if want_clean:
        keys = list(preview.get("clean_keys") or [])
        keys.extend(
            _csv_keys_on_disk(
                cache_dir, source=source, symbol=symbol, period=period
            )
        )
        csv_n, csv_locked = _sync_csv(cache_dir, clean_store, keys)
    return {
        "clean_deleted": clean_deleted,
        "raw_deleted": raw_deleted,
        "csv_touched": csv_n,
        "csv_locked": csv_locked,
        "clean_n": preview["clean_n"],
        "raw_n": preview["raw_n"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看本地已保存的K线库存，不访问网络、不下单")
    parser.parse_args(argv)
    from src.utils.paths import project_root

    root = project_root()
    book = collect_kline_inventory(root)
    path = write_kline_inventory(root, book)
    print(inventory_message(book))
    print(f"已保存 {path}")
    return 0 if book.get("detail") else 1


if __name__ == "__main__":
    raise SystemExit(main())
