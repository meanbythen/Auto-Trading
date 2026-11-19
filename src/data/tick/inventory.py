"""
本地 tick 库存总账。

只读已经进 sqlite 的数据，不把每一笔 tick 写进 Excel，也不写进「K线库存.xlsx」。
给人看：哪个合约、来源、连续有数的时间段从哪到哪、这段有多少根。
周末和短假（默认间隔不超过 5 个日历日）算同一段；中间空出一周以上会拆成两段。
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.data.inventory import _write_kv, _write_table
from src.data.quote_check import product_code


LATEST_NAME = "TICK库存.xlsx"
# 相邻两天间隔超过这个日历日数，就算断档（周五到周一是 3，不断；空出一周会拆开）。
DEFAULT_GAP_DAYS = 5


def _as_date(value: Any) -> date | None:
    """把库里的日期/时间文本收成日历日。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10].replace("/", "-")).date()
    except ValueError:
        return None


def _as_datetime_text(value: Any) -> str:
    """库存时间统一成可读文本，去掉时区尾巴。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("T", " ")[:19]


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


def _file_size_mb(path: Path) -> float | None:
    if not path.exists():
        return None
    return round(path.stat().st_size / (1024 * 1024), 2)


def _source_label(gateway: str) -> str:
    """vnpy 入库时 gateway 写成 TQ，界面上显示天勤。"""
    text = str(gateway or "").strip().upper()
    if text in {"", "TQ", "TQSDK", "TQSDK.API"}:
        return "天勤"
    return str(gateway or "未知")


def merge_day_groups(
    days: list[dict[str, Any]],
    *,
    gap_days: int = DEFAULT_GAP_DAYS,
) -> list[dict[str, Any]]:
    """
    把「每天有多少根」收成连续时段。

    输入每一行要有：日历日、起、止、根数。同一合约、同一来源请先自己筛好再传入。
    原理：按日历日排序，下一天与上一段末日期间隔不超过 gap_days 就并进去，
    这样周六日不会把一周拆成两段；中间真缺了一大截才会新开一段。
    """
    ordered = sorted(
        (row for row in days if row.get("日历日") is not None),
        key=lambda row: row["日历日"],
    )
    spans: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    limit = max(1, int(gap_days))
    for row in ordered:
        day: date = row["日历日"]
        start = _as_datetime_text(row.get("起"))
        end = _as_datetime_text(row.get("止"))
        count = int(row.get("根数") or 0)
        if current is None:
            current = {
                "日历日起": day,
                "日历日止": day,
                "起": start,
                "止": end,
                "根数": count,
                "有数天数": 1,
            }
            continue
        gap = (day - current["日历日止"]).days
        if gap > limit:
            spans.append(current)
            current = {
                "日历日起": day,
                "日历日止": day,
                "起": start,
                "止": end,
                "根数": count,
                "有数天数": 1,
            }
            continue
        current["日历日止"] = day
        if end:
            current["止"] = end
        current["根数"] += count
        current["有数天数"] += 1
    if current is not None:
        spans.append(current)
    return spans


def _find_tick_table(conn: sqlite3.Connection) -> str | None:
    """vnpy sqlite 表名一般是 dbtickdata；找不到就在所有表名里挑带 tick 的。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [str(item[0]) for item in rows]
    for name in names:
        if name.lower() == "dbtickdata":
            return name
    for name in names:
        if "tick" in name.lower() and "overview" not in name.lower():
            return name
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(item[1]) for item in info}


def _load_day_rows(database: Path) -> tuple[list[dict[str, Any]], str]:
    """
    按合约、来源、日历日聚合。不把逐笔 tick 读进内存。

    返回 (每日汇总行, 用到的表名)。库文件不存在或没有 tick 表时返回空列表。
    """
    if not database.exists():
        return [], ""
    conn = sqlite3.connect(str(database))
    try:
        table = _find_tick_table(conn)
        if not table:
            return [], ""
        columns = _table_columns(conn, table)
        needed = {"symbol", "exchange", "datetime"}
        if not needed.issubset({name.lower() for name in columns}):
            lower_map = {name.lower(): name for name in columns}
            if not needed.issubset(lower_map):
                return [], table
        lower_map = {name.lower(): name for name in columns}
        symbol_col = lower_map["symbol"]
        exchange_col = lower_map["exchange"]
        datetime_col = lower_map["datetime"]
        gateway_col = lower_map.get("gateway_name")
        gateway_sql = f'"{gateway_col}"' if gateway_col else "''"
        sql = (
            f'SELECT "{symbol_col}", "{exchange_col}", {gateway_sql}, '
            f'date("{datetime_col}"), MIN("{datetime_col}"), MAX("{datetime_col}"), COUNT(*) '
            f'FROM "{table}" '
            f'GROUP BY "{symbol_col}", "{exchange_col}", {gateway_sql}, date("{datetime_col}") '
            f'ORDER BY "{symbol_col}", "{exchange_col}", date("{datetime_col}")'
        )
        out: list[dict[str, Any]] = []
        for symbol, exchange, gateway, day, start, end, count in conn.execute(sql):
            calendar = _as_date(day) or _as_date(start)
            if calendar is None:
                continue
            out.append(
                {
                    "合约": str(symbol or "").strip(),
                    "交易所": str(exchange or "").strip(),
                    "来源码": str(gateway or "").strip(),
                    "数据源": _source_label(str(gateway or "")),
                    "日历日": calendar,
                    "起": _as_datetime_text(start),
                    "止": _as_datetime_text(end),
                    "根数": int(count or 0),
                }
            )
        return out, table
    finally:
        conn.close()


def _parse_history_csv_name(path: Path) -> tuple[str, date, date] | None:
    """
    历史下载文件名：SHFE.fu2701_20260101_20260107.csv
    从右边拆两段日期，前面整段当合约。
    """
    stem = path.stem
    parts = stem.rsplit("_", 2)
    if len(parts) != 3:
        return None
    contract, start_text, end_text = parts
    if len(start_text) != 8 or len(end_text) != 8:
        return None
    try:
        start = datetime.strptime(start_text, "%Y%m%d").date()
        end = datetime.strptime(end_text, "%Y%m%d").date()
    except ValueError:
        return None
    return contract.strip(), start, end


def _history_index(history_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """扫历史 csv 目录，按 交易所.合约 归组。没有目录就当没有历史下载。"""
    index: dict[str, list[dict[str, Any]]] = {}
    if not history_dir.exists():
        return index
    for path in history_dir.rglob("*.csv"):
        parsed = _parse_history_csv_name(path)
        if parsed is None:
            continue
        contract, start, end = parsed
        size = path.stat().st_size if path.exists() else 0
        index.setdefault(contract, []).append(
            {
                "合约全名": contract,
                "文件起": start,
                "文件止": end,
                "路径": str(path),
                "字节": size,
            }
        )
    return index


def _instrument_key(exchange: str, symbol: str) -> str:
    if exchange and symbol:
        return f"{exchange}.{symbol}"
    return symbol


def _history_for(index: dict[str, list[dict[str, Any]]], exchange: str, symbol: str) -> list[dict[str, Any]]:
    key = _instrument_key(exchange, symbol)
    if key in index:
        return index[key]
    # 文件名有时只有合约、没有交易所前缀。
    return index.get(symbol, [])


def _channel_note(history_files: list[dict[str, Any]]) -> str:
    """
    库里的 tick 目前都走天勤网关。有对应历史 csv 就标「含历史下载」，
    没有就标「库内入库」（实时采集或下完后 csv 已删，都算这一类）。
    """
    if history_files:
        return "含历史下载CSV"
    return "库内入库"


def collect_tick_inventory(
    root: Path | str,
    *,
    database: Path | str | None = None,
    history_dir: Path | str | None = None,
    gap_days: int = DEFAULT_GAP_DAYS,
) -> dict[str, Any]:
    """
    扫 tick sqlite 和历史 csv 目录，收成总览 + 按合约 + 连续时段。

    逐笔 tick 仍只在数据库里；这里只写按日聚合后再合并的连续段。
    """
    base = Path(root)
    db_path = Path(database) if database is not None else base / "data" / "ticks" / "vnpy_data.db"
    csv_dir = Path(history_dir) if history_dir is not None else base / "data" / "ticks" / "history"
    day_rows, table = _load_day_rows(db_path)
    history = _history_index(csv_dir)

    by_contract: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in day_rows:
        key = (str(row["交易所"]), str(row["合约"]), str(row["数据源"]))
        by_contract.setdefault(key, []).append(row)

    spans: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    for (exchange, symbol, source), days in sorted(by_contract.items()):
        hist = _history_for(history, exchange, symbol)
        channel = _channel_note(hist)
        merged = merge_day_groups(days, gap_days=gap_days)
        total = sum(int(item.get("根数") or 0) for item in days)
        first = min((item["起"] for item in days if item.get("起")), default="")
        last = max((item["止"] for item in days if item.get("止")), default="")
        data_days = len(days)
        span = _span_days(first, last)
        age = _age_minutes(last)
        daily = round(total / data_days, 1) if data_days else None
        csv_n = len(hist)
        contracts.append(
            {
                "数据源": source,
                "入库途径": channel,
                "交易所": exchange,
                "品种": product_code(symbol) or symbol,
                "合约": symbol,
                "合约全名": _instrument_key(exchange, symbol),
                "根数": total,
                "有数天数": data_days,
                "连续段数": len(merged),
                "从": first,
                "到": last,
                "跨度天数": None if span is None else round(span, 2),
                "日均根数": daily,
                "距今分钟": None if age is None else round(age, 1),
                "历史CSV块数": csv_n,
            }
        )
        prev_end: date | None = None
        for index, item in enumerate(merged, start=1):
            start_day: date = item["日历日起"]
            end_day: date = item["日历日止"]
            if prev_end is not None:
                hole_start = prev_end + timedelta(days=1)
                hole_end = start_day - timedelta(days=1)
                if hole_start <= hole_end:
                    gaps.append(
                        {
                            "数据源": source,
                            "交易所": exchange,
                            "品种": product_code(symbol) or symbol,
                            "合约": symbol,
                            "缺口从": hole_start.isoformat(),
                            "缺口到": hole_end.isoformat(),
                            "缺口日历天": (hole_end - hole_start).days + 1,
                        }
                    )
            prev_end = end_day
            item_span = _span_days(item["起"], item["止"])
            calendar_span = (end_day - start_day).days + 1
            spans.append(
                {
                    "数据源": source,
                    "入库途径": channel,
                    "交易所": exchange,
                    "品种": product_code(symbol) or symbol,
                    "合约": symbol,
                    "第几段": index,
                    "日历日起": start_day.isoformat(),
                    "日历日止": end_day.isoformat(),
                    "起": item["起"],
                    "止": item["止"],
                    "根数": item["根数"],
                    "有数天数": item["有数天数"],
                    "跨度日历天": calendar_span,
                    "跨度天数": None if item_span is None else round(item_span, 2),
                    "日均根数": round(item["根数"] / item["有数天数"], 1)
                    if item["有数天数"]
                    else None,
                }
            )

    by_source: dict[str, dict[str, Any]] = {}
    for row in contracts:
        name = str(row["数据源"])
        box = by_source.setdefault(
            name,
            {"数据源": name, "合约数": 0, "根数": 0, "连续段数": 0, "有数天数": 0},
        )
        box["合约数"] += 1
        box["根数"] += int(row["根数"])
        box["连续段数"] += int(row["连续段数"])
        box["有数天数"] += int(row["有数天数"])

    day_sheet = []
    for row in sorted(day_rows, key=lambda item: (item["交易所"], item["合约"], item["日历日"])):
        day_sheet.append(
            {
                "数据源": row["数据源"],
                "交易所": row["交易所"],
                "品种": product_code(row["合约"]) or row["合约"],
                "合约": row["合约"],
                "日历日": row["日历日"].isoformat(),
                "起": row["起"],
                "止": row["止"],
                "根数": row["根数"],
            }
        )

    sources = sorted({str(row["数据源"]) for row in contracts})
    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "database": str(db_path),
        "history_dir": str(csv_dir),
        "table": table,
        "db_mb": _file_size_mb(db_path),
        "gap_days": int(gap_days),
        "contracts_n": len(contracts),
        "ticks": sum(int(row["根数"]) for row in contracts),
        "spans_n": len(spans),
        "days_n": len(day_rows),
        "gaps_n": len(gaps),
        "sources": sources,
        "history_csv_n": sum(len(items) for items in history.values()),
        "by_source": [by_source[name] for name in sorted(by_source)],
        "by_contract": contracts,
        "spans": spans,
        "gaps": gaps,
        "by_day": day_sheet,
    }


def write_tick_inventory_excel(path: Path, book: dict[str, Any]) -> None:
    """写成给人看的 Excel：总览、来源、合约、连续时段、缺口、按日。没有逐笔 tick。"""
    from openpyxl import Workbook

    wb = Workbook()
    help_ws = wb.active
    help_ws.title = "说明"
    _write_kv(
        help_ws,
        [
            ("这是什么", "电脑里已经存下的 tick 总账。只读本地库，不拉网、不下单。"),
            (
                "和 K 线库存的区别",
                "K 线看「K线库存.xlsx」。tick 的每一笔仍只在 data/ticks/vnpy_data.db，"
                "这张表只写统计，不把逐笔明细搬进 Excel。",
            ),
            ("连续时段", "同一合约里，相邻有数日期间隔不超过 5 个日历日算同一段；中间空一周以上会拆开。"),
            ("来源", "入库网关是天勤。若同目录下还有对应历史 csv，会标「含历史下载CSV」。"),
            ("缺口", "两段连续时段中间空出来的日历区间，方便补数。周末若被算进缺口，是因为前后两段已经拆开。"),
            ("按日", "每个有数的日历日一行，仍是汇总，不是每一笔 tick。"),
            ("检查时间", book.get("checked_at") or ""),
        ],
    )
    overview = wb.create_sheet("总览")
    _write_kv(
        overview,
        [
            ("检查时间", book.get("checked_at") or ""),
            ("数据库", book.get("database") or ""),
            ("库文件MB", book.get("db_mb") if book.get("db_mb") is not None else "（还没有库文件）"),
            ("历史CSV目录", book.get("history_dir") or ""),
            ("历史CSV块数", book.get("history_csv_n") or 0),
            ("数据源", "、".join(book.get("sources") or []) or "（无）"),
            ("合约个数", book.get("contracts_n") or 0),
            ("tick 总根数", book.get("ticks") or 0),
            ("连续时段数", book.get("spans_n") or 0),
            ("有数日历日", book.get("days_n") or 0),
            ("缺口段数", book.get("gaps_n") or 0),
            ("断档阈值日历日", book.get("gap_days") or DEFAULT_GAP_DAYS),
        ],
    )
    def _rows(columns: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 复用 K 线表的写表函数时，空表默认文案是「还没有K线」，这里改成 tick。
        if rows:
            return rows
        return [{columns[0]: "（本地还没有 tick）"}]

    source_cols = ["数据源", "合约数", "根数", "连续段数", "有数天数"]
    _write_table(
        wb.create_sheet("按数据源"),
        source_cols,
        _rows(source_cols, list(book.get("by_source") or [])),
    )
    contract_cols = [
        "数据源",
        "入库途径",
        "交易所",
        "品种",
        "合约",
        "合约全名",
        "根数",
        "有数天数",
        "连续段数",
        "从",
        "到",
        "跨度天数",
        "日均根数",
        "距今分钟",
        "历史CSV块数",
    ]
    _write_table(
        wb.create_sheet("按合约"),
        contract_cols,
        _rows(contract_cols, list(book.get("by_contract") or [])),
    )
    span_cols = [
        "数据源",
        "入库途径",
        "交易所",
        "品种",
        "合约",
        "第几段",
        "日历日起",
        "日历日止",
        "起",
        "止",
        "根数",
        "有数天数",
        "跨度日历天",
        "跨度天数",
        "日均根数",
    ]
    _write_table(
        wb.create_sheet("连续时段"),
        span_cols,
        _rows(span_cols, list(book.get("spans") or [])),
    )
    gap_cols = ["数据源", "交易所", "品种", "合约", "缺口从", "缺口到", "缺口日历天"]
    _write_table(
        wb.create_sheet("缺口"),
        gap_cols,
        _rows(gap_cols, list(book.get("gaps") or [])),
    )
    day_cols = ["数据源", "交易所", "品种", "合约", "日历日", "起", "止", "根数"]
    _write_table(
        wb.create_sheet("按日"),
        day_cols,
        _rows(day_cols, list(book.get("by_day") or [])),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def write_tick_inventory(root: Path | str, book: dict[str, Any] | None = None) -> Path:
    """写出带时间的 Excel，并更新 data/TICK库存.xlsx。不改 K线库存.xlsx。"""
    base = Path(root)
    folder = base / "data"
    folder.mkdir(parents=True, exist_ok=True)
    payload = book if book is not None else collect_tick_inventory(base)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dated = folder / f"TICK库存-{stamp}.xlsx"
    write_tick_inventory_excel(dated, payload)
    try:
        write_tick_inventory_excel(folder / LATEST_NAME, payload)
    except Exception:  # noqa: BLE001
        pass
    return dated


def tick_inventory_message(book: dict[str, Any]) -> str:
    """界面和命令行用的短句。"""
    if not book.get("by_contract"):
        return "本地还没有存下的 tick。请先启动采集或下载历史 tick。"
    sources = "、".join(book.get("sources") or []) or "无"
    mb = book.get("db_mb")
    size = f"库文件 {mb} MB，" if mb is not None else ""
    return (
        f"{size}来源 {sources}，合约 {book.get('contracts_n') or 0} 个，"
        f"共 {book.get('ticks') or 0} 根，连续时段 {book.get('spans_n') or 0} 段，"
        f"缺口 {book.get('gaps_n') or 0} 处。明细请看 Excel，逐笔仍在数据库。"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看本地已保存的 tick 库存，不访问网络、不下单")
    parser.parse_args(argv)
    from src.utils.paths import project_root

    root = project_root()
    book = collect_tick_inventory(root)
    path = write_tick_inventory(root, book)
    print(tick_inventory_message(book))
    print(f"已保存 {path}")
    return 0 if book.get("by_contract") else 1


if __name__ == "__main__":
    raise SystemExit(main())
