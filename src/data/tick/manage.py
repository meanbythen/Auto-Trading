"""
本地 tick 的增删查（不改每一笔的内容）。

查：库存 Excel、这段汇总、头尾几笔样例。
增：采集、下载历史、合成 K 线（仍走原来的按钮）。
删：按合约和时间段从库里拿掉，并忘掉这段的断点续传，才能重新下载。
不提供「改」：基础 tick 不手改、不修补字段。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.data.inventory import _stamp_bound
from src.data.tick.inventory import _as_datetime_text, _find_tick_table, _table_columns
from src.data.tick.vnpy_store import reset_database


def parse_tick_contract(text: str) -> tuple[str, str | None]:
    """
    fu2701 → (fu2701, None)；SHFE.fu2701 → (fu2701, SHFE)。
    带交易所时更准，避免同名合约在两家交易所。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("请填写合约，例如 fu2701 或 SHFE.fu2701")
    if "." in raw:
        left, right = raw.split(".", 1)
        if left.isalpha() and left.upper() == left and right.strip():
            return right.strip(), left.strip().upper()
    return raw, None


def _parse_bound(value: str | None, *, end: bool) -> tuple[datetime | None, bool]:
    """
    把界面上的起止收成北京时间。

    只填日期时：从当天 00:00:00 起，到次日 00:00:00 之前（含夜盘过零点当天的白天，
    不含第二天白天）。填了时分就按这一秒：结束用「这一秒之后」为开区间，避免小数秒漏删。
    返回 (时刻, 是否开区间上界)。
    """
    text = str(value or "").strip()
    if not text:
        return None, False
    bound = _stamp_bound(text, end=end)
    if not bound:
        return None, False
    try:
        moment = datetime.fromisoformat(bound[:19])
    except ValueError as exc:
        raise ValueError("时间请写成 2026-11-13 或 2026-11-13 21:00:00") from exc
    if end and len(text) <= 10:
        return moment + timedelta(days=1), True
    if end and len(text) > 10:
        return moment + timedelta(seconds=1), True
    return moment, False


def _range_sql(
    datetime_col: str,
    ts_from: str | None,
    ts_to: str | None,
) -> tuple[str, list[Any]]:
    """拼 datetime 条件。开区间上界用 <，避免 23:59:59.xxx 删不掉。"""
    clauses: list[str] = []
    params: list[Any] = []
    start, _ = _parse_bound(ts_from, end=False)
    stop, exclusive = _parse_bound(ts_to, end=True)
    if start is not None:
        clauses.append(f'"{datetime_col}" >= ?')
        params.append(start.strftime("%Y-%m-%d %H:%M:%S"))
    if stop is not None:
        op = "<" if exclusive else "<="
        clauses.append(f'"{datetime_col}" {op} ?')
        params.append(stop.strftime("%Y-%m-%d %H:%M:%S"))
    return (" AND ".join(clauses) if clauses else "1=1"), params


def _open_ticks(
    database: Path, *, exclusive: bool = False
) -> tuple[sqlite3.Connection, str, dict[str, str]] | None:
    if not database.exists():
        return None
    if exclusive:
        # 删的时候先关掉 vnpy 连接，避免两头写同一文件。
        reset_database()
    conn = sqlite3.connect(str(database))
    table = _find_tick_table(conn)
    if not table:
        conn.close()
        return None
    columns = _table_columns(conn, table)
    lower_map = {name.lower(): name for name in columns}
    if not {"symbol", "exchange", "datetime"}.issubset(lower_map):
        conn.close()
        return None
    return conn, table, lower_map


def list_tick_contracts(*, database: Path | str) -> list[dict[str, str]]:
    """库里有哪些合约，给下拉框用。"""
    opened = _open_ticks(Path(database))
    if opened is None:
        return []
    conn, table, cols = opened
    try:
        sql = (
            f'SELECT DISTINCT "{cols["exchange"]}", "{cols["symbol"]}" '
            f'FROM "{table}" ORDER BY "{cols["exchange"]}", "{cols["symbol"]}"'
        )
        out = []
        for exchange, symbol in conn.execute(sql):
            out.append(
                {
                    "exchange": str(exchange or "").strip(),
                    "symbol": str(symbol or "").strip(),
                    "instrument": f"{exchange}.{symbol}".strip("."),
                }
            )
        return out
    finally:
        conn.close()


def _resolve_exchange(
    conn: sqlite3.Connection,
    table: str,
    cols: dict[str, str],
    symbol: str,
    exchange: str | None,
) -> str:
    if exchange:
        return str(exchange).strip().upper()
    rows = conn.execute(
        f'SELECT DISTINCT "{cols["exchange"]}" FROM "{table}" WHERE "{cols["symbol"]}" = ?',
        (symbol,),
    ).fetchall()
    names = [str(item[0] or "").strip() for item in rows if str(item[0] or "").strip()]
    if not names:
        raise ValueError(f"库里没有合约 {symbol}。请先看 tick 库存，或写成 SHFE.fu2701。")
    if len(names) > 1:
        raise ValueError(
            f"{symbol} 在库里有多家交易所：{'、'.join(names)}。请写成 交易所.合约，例如 {names[0]}.{symbol}。"
        )
    return names[0]


def _contract_filter(
    cols: dict[str, str],
    symbol: str,
    exchange: str,
    ts_from: str | None,
    ts_to: str | None,
) -> tuple[str, list[Any]]:
    time_sql, time_params = _range_sql(cols["datetime"], ts_from, ts_to)
    sql = f'"{cols["symbol"]}" = ? AND "{cols["exchange"]}" = ? AND {time_sql}'
    return sql, [symbol, exchange, *time_params]


def _overview_table(conn: sqlite3.Connection) -> str | None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND lower(name) = 'dbtickoverview'"
    ).fetchall()
    return str(rows[0][0]) if rows else None


def _rebuild_overview(
    conn: sqlite3.Connection,
    tick_table: str,
    cols: dict[str, str],
    symbol: str,
    exchange: str,
) -> None:
    """删完一段后，按剩下的 tick 重写汇总行，避免库存数字还是旧的。"""
    overview = _overview_table(conn)
    if not overview:
        return
    count, start, end = conn.execute(
        f'SELECT COUNT(*), MIN("{cols["datetime"]}"), MAX("{cols["datetime"]}") '
        f'FROM "{tick_table}" WHERE "{cols["symbol"]}" = ? AND "{cols["exchange"]}" = ?',
        (symbol, exchange),
    ).fetchone()
    conn.execute(
        f'DELETE FROM "{overview}" WHERE symbol = ? AND exchange = ?',
        (symbol, exchange),
    )
    if int(count or 0) > 0:
        conn.execute(
            f'INSERT INTO "{overview}" (symbol, exchange, count, start, end) VALUES (?, ?, ?, ?, ?)',
            (symbol, exchange, int(count), start, end),
        )


def preview_purge_ticks(
    *,
    database: Path | str,
    symbol: str,
    exchange: str | None = None,
    ts_from: str | None = None,
    ts_to: str | None = None,
) -> dict[str, Any]:
    """先数会删多少笔，还不真删。必须指定合约。"""
    name, from_name = parse_tick_contract(symbol)
    exchange = exchange or from_name
    opened = _open_ticks(Path(database))
    if opened is None:
        return {
            "symbol": name,
            "exchange": exchange or "",
            "count": 0,
            "start": "",
            "end": "",
        }
    conn, table, cols = opened
    try:
        resolved = _resolve_exchange(conn, table, cols, name, exchange)
        where, params = _contract_filter(cols, name, resolved, ts_from, ts_to)
        count, start, end = conn.execute(
            f'SELECT COUNT(*), MIN("{cols["datetime"]}"), MAX("{cols["datetime"]}") '
            f'FROM "{table}" WHERE {where}',
            params,
        ).fetchone()
        return {
            "symbol": name,
            "exchange": resolved,
            "count": int(count or 0),
            "start": _as_datetime_text(start),
            "end": _as_datetime_text(end),
        }
    finally:
        conn.close()


def purge_ticks(
    root: Path | str,
    *,
    database: Path | str,
    history_dir: Path | str,
    symbol: str,
    exchange: str | None = None,
    ts_from: str | None = None,
    ts_to: str | None = None,
    also_synth_bars: bool = True,
) -> dict[str, Any]:
    """
    删除这一段 tick，并忘掉对应的历史下载续传。

    不修改留下的 tick。可选同时删这段 tick 合成的 K 线（库存名带 @tick）。
    """
    from src.data.tick.downloader import forget_downloaded_chunks

    preview = preview_purge_ticks(
        database=database, symbol=symbol, exchange=exchange, ts_from=ts_from, ts_to=ts_to
    )
    name = str(preview["symbol"])
    resolved = str(preview["exchange"] or "")
    deleted = 0
    opened = _open_ticks(Path(database), exclusive=True)
    if opened is not None and resolved:
        conn, table, cols = opened
        try:
            where, params = _contract_filter(cols, name, resolved, ts_from, ts_to)
            cursor = conn.execute(f'DELETE FROM "{table}" WHERE {where}', params)
            deleted = int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else preview["count"])
            _rebuild_overview(conn, table, cols, name, resolved)
            conn.commit()
        finally:
            conn.close()
        reset_database()

    start_day, _ = _parse_bound(ts_from, end=False)
    stop, exclusive = _parse_bound(ts_to, end=True)
    start_date = start_day.date() if start_day else None
    end_date = None
    if stop is not None:
        end_date = (stop - timedelta(seconds=1)).date() if exclusive else stop.date()
    resume = forget_downloaded_chunks(
        history_dir,
        contract=f"{resolved}.{name}" if resolved else name,
        start=start_date,
        end=end_date,
        delete_csv=True,
    )

    bars: dict[str, Any] = {}
    if also_synth_bars:
        from src.data.inventory import preview_purge, purge_klines

        try:
            seen = preview_purge(
                root,
                source="tick",
                symbol=name,
                period=None,
                ts_from=ts_from,
                ts_to=ts_to,
                layers="both",
            )
            if int(seen.get("clean_n") or 0) + int(seen.get("raw_n") or 0) > 0:
                bars = purge_klines(
                    root,
                    source="tick",
                    symbol=name,
                    period=None,
                    ts_from=ts_from,
                    ts_to=ts_to,
                    layers="both",
                )
            else:
                bars = {"clean_deleted": 0, "raw_deleted": 0, "skipped": True}
        except ValueError:
            bars = {"clean_deleted": 0, "raw_deleted": 0, "skipped": True}

    return {
        "symbol": name,
        "exchange": resolved,
        "deleted": deleted,
        "start": preview.get("start") or "",
        "end": preview.get("end") or "",
        "manifest_cleared": resume.get("manifest_cleared") or 0,
        "csv_deleted": resume.get("csv_deleted") or [],
        "csv_locked": resume.get("csv_locked") or [],
        "synth_bars": bars,
    }


def inspect_ticks(
    *,
    database: Path | str,
    symbol: str,
    exchange: str | None = None,
    ts_from: str | None = None,
    ts_to: str | None = None,
    sample: int = 5,
) -> dict[str, Any]:
    """
    抽查一段 tick：根数、起止、价格区间、头尾各几笔。

    不把整段逐笔读出来，也不改库。
    """
    name, from_name = parse_tick_contract(symbol)
    exchange = exchange or from_name
    opened = _open_ticks(Path(database))
    empty = {
        "symbol": name,
        "exchange": exchange or "",
        "count": 0,
        "start": "",
        "end": "",
        "min_price": None,
        "max_price": None,
        "head": [],
        "tail": [],
        "days": 0,
    }
    if opened is None:
        return empty
    conn, table, cols = opened
    try:
        resolved = _resolve_exchange(conn, table, cols, name, exchange)
        where, params = _contract_filter(cols, name, resolved, ts_from, ts_to)
        price_col = cols.get("last_price")
        extra = f', MIN("{price_col}"), MAX("{price_col}")' if price_col else ", NULL, NULL"
        count, start, end, min_price, max_price = conn.execute(
            f'SELECT COUNT(*), MIN("{cols["datetime"]}"), MAX("{cols["datetime"]}"){extra} '
            f'FROM "{table}" WHERE {where}',
            params,
        ).fetchone()
        days_row = conn.execute(
            f'SELECT COUNT(DISTINCT date("{cols["datetime"]}")) FROM "{table}" WHERE {where}',
            params,
        ).fetchone()
        select_cols = [cols["datetime"]]
        for key in ("last_price", "volume", "bid_price_1", "ask_price_1"):
            if key in cols:
                select_cols.append(cols[key])
        listed = ", ".join(f'"{item}"' for item in select_cols)
        limit = max(1, int(sample))
        head_rows = conn.execute(
            f'SELECT {listed} FROM "{table}" WHERE {where} ORDER BY "{cols["datetime"]}" ASC LIMIT {limit}',
            params,
        ).fetchall()
        tail_rows = conn.execute(
            f'SELECT {listed} FROM "{table}" WHERE {where} ORDER BY "{cols["datetime"]}" DESC LIMIT {limit}',
            params,
        ).fetchall()
        labels = ["时间"]
        for key in ("last_price", "volume", "bid_price_1", "ask_price_1"):
            if key in cols:
                labels.append({"last_price": "最新价", "volume": "量", "bid_price_1": "买一", "ask_price_1": "卖一"}[key])

        def _pack(rows: list[tuple[Any, ...]], reverse: bool = False) -> list[dict[str, Any]]:
            items = []
            ordered = list(reversed(rows)) if reverse else list(rows)
            for row in ordered:
                rec = {labels[idx]: row[idx] for idx in range(len(labels))}
                rec["时间"] = _as_datetime_text(rec.get("时间"))
                items.append(rec)
            return items

        return {
            "symbol": name,
            "exchange": resolved,
            "instrument": f"{resolved}.{name}",
            "count": int(count or 0),
            "start": _as_datetime_text(start),
            "end": _as_datetime_text(end),
            "min_price": min_price,
            "max_price": max_price,
            "days": int((days_row or [0])[0] or 0),
            "head": _pack(head_rows),
            "tail": _pack(tail_rows, reverse=True),
            "database": str(Path(database)),
        }
    finally:
        conn.close()


def format_tick_inspect(book: dict[str, Any]) -> str:
    """给人看的抽查说明。只读，不改数。"""
    lines = [
        f"合约  {book.get('instrument') or book.get('symbol')}    根数  {book.get('count') or 0}",
        f"从  {book.get('start') or '—'}    到  {book.get('end') or '—'}",
        f"有数日历日  {book.get('days') or 0}",
    ]
    if book.get("min_price") is not None:
        lines.append(f"最新价区间  {book.get('min_price')} ~ {book.get('max_price')}")
    lines.append("")
    lines.append("下面只列头尾各几笔，方便判断这段是不是乱的。不会改库。")
    if not book.get("count"):
        lines.append("这段没有 tick。请先看库存，或换合约/时间。")
        return "\n".join(lines)
    lines.append("")
    lines.append("【最早几笔】")
    for row in book.get("head") or []:
        lines.append("  " + "  ".join(f"{k} {v}" for k, v in row.items()))
    lines.append("【最晚几笔】")
    for row in book.get("tail") or []:
        lines.append("  " + "  ".join(f"{k} {v}" for k, v in row.items()))
    lines.append("")
    lines.append("若这段很乱：点「删除本地tick」清掉后再「下载历史 tick」。基础数据不会被改写，只整段删掉重来。")
    return "\n".join(lines)
