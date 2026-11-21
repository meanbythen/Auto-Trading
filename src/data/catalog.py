"""数据目录：合约总表快照 + K 线/tick 库存清单。

定位：
1. 合约表（Excel）是唯一权威，按内容 hash 固化成不可变快照，研究/回测只带 snapshot_id；
2. K 线和 tick 只记录“哪个库、哪一组、多少根、从哪到哪”，不复制行情；
3. 所有写入都在 data/trading.db，关闭 storage.sqlite_enabled 时整体静默。

目录指纹 digest 是“行数 + 起止时间”的摘要，不是逐行内容 hash；
逐行内容指纹由 src/backtest/experiment.py 负责，两者互补。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.account.trading_db import DB_LOCK, connect, connect_readonly, json_text, storage_settings

KLINE_MARKET = Path("data") / "klines" / "market.sqlite"
KLINE_RAW = Path("data") / "klines" / "raw.sqlite"
TICK_DB = Path("data") / "ticks" / "vnpy_data.db"

_INSTRUMENT_COLUMNS = (
    "snapshot_id",
    "symbol",
    "product",
    "name",
    "multiplier",
    "margin_rate_pct",
    "margin",
    "commission",
    "commission_close",
    "commission_close_today",
    "price_tick",
    "trading_sessions",
    "flatten_times",
    "enabled",
    "payload_json",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def enabled(root: str | Path, cfg: dict[str, Any] | None = None) -> bool:
    return bool(storage_settings(root, cfg).sqlite_enabled)


def _db_path(root: str | Path, cfg: dict[str, Any] | None = None) -> Path:
    return storage_settings(root, cfg).db_path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _read_conn(
    root: str | Path, cfg: dict[str, Any] | None = None
) -> sqlite3.Connection | None:
    """只读不建库：库文件不在就返回 None，避免回测凭空生成 trading.db。"""
    return connect_readonly(_db_path(root, cfg))


def latest_instrument_snapshot(
    root: str | Path, cfg: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if not enabled(root, cfg):
        return None
    try:
        conn = _read_conn(root, cfg)
        if conn is None:
            return None
        with closing(conn):
            row = conn.execute(
                """
                SELECT snapshot_id, source_path, source_sha256, row_count, created_at
                  FROM instrument_snapshots
                 ORDER BY created_at DESC, snapshot_id DESC
                 LIMIT 1
                """
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取合约快照失败: {}", exc)
        return None
    return dict(row) if row else None


def write_instruments_snapshot(
    root: str | Path,
    rows: list[dict[str, Any]],
    *,
    source_path: str = "",
    source_sha256: str = "",
    cfg: dict[str, Any] | None = None,
    snapshot_id: str = "",
) -> str:
    """把一个已经解析好的合约列表落成快照；供 refresh_instruments 和测试复用。"""
    if not enabled(root, cfg):
        return ""
    stamp = _now()
    sid = snapshot_id or f"inst-{datetime.now():%Y%m%dT%H%M%S}-{str(source_sha256)[:8] or 'manual'}"
    values = []
    for row in rows:
        values.append(
            (
                sid,
                str(row.get("symbol") or ""),
                str(row.get("product") or ""),
                str(row.get("name") or ""),
                row.get("multiplier"),
                row.get("margin_rate_pct"),
                row.get("margin"),
                row.get("commission"),
                row.get("commission_close"),
                row.get("commission_close_today"),
                row.get("price_tick"),
                str(row.get("trading_sessions") or ""),
                str(row.get("flatten_times") or ""),
                1 if row.get("enabled", True) else 0,
                json_text(row),
            )
        )
    settings = storage_settings(root, cfg)
    try:
        with DB_LOCK, closing(connect(settings.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO instrument_snapshots
                    (snapshot_id, source_path, source_sha256, row_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, str(source_path), str(source_sha256), len(values), stamp),
            )
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO instruments ({", ".join(_INSTRUMENT_COLUMNS)})
                VALUES ({", ".join("?" for _ in _INSTRUMENT_COLUMNS)})
                """,
                values,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("合约快照写入失败: {}", exc)
        return ""
    return sid


def refresh_instruments(
    root: str | Path,
    cfg: dict[str, Any] | None = None,
    table_path: str | Path | None = None,
    *,
    force: bool = False,
) -> str:
    """把品种与合约表固化成快照；文件内容没变时直接返回最近快照 ID。"""
    if not enabled(root, cfg):
        return ""
    base = Path(root)
    try:
        from src.risk.universe_table import product_key, read_universe_table, resolve_universe_path

        path = Path(table_path) if table_path else resolve_universe_path(cfg, base)
        if not path.exists():
            return ""
        sha = _file_sha256(path)
        latest = latest_instrument_snapshot(base, cfg)
        if latest and not force and latest.get("source_sha256") == sha:
            return str(latest.get("snapshot_id") or "")
        rows = read_universe_table(path)
        for row in rows:
            row["product"] = product_key(str(row.get("symbol") or ""))
        return write_instruments_snapshot(
            base,
            rows,
            source_path=str(path),
            source_sha256=sha,
            cfg=cfg,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("合约表导入快照失败（不影响交易）: {}", exc)
        return ""


def list_instruments(
    root: str | Path,
    snapshot_id: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按快照查合约行；不传快照就用最近一次。"""
    if not enabled(root, cfg):
        return []
    sid = str(snapshot_id or "")
    if not sid:
        latest = latest_instrument_snapshot(root, cfg)
        if latest is None:
            return []
        sid = str(latest.get("snapshot_id") or "")
    try:
        conn = _read_conn(root, cfg)
        if conn is None:
            return []
        with closing(conn):
            rows = conn.execute(
                f"SELECT {', '.join(_INSTRUMENT_COLUMNS)} FROM instruments WHERE snapshot_id = ? ORDER BY symbol",
                (sid,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取合约快照行失败: {}", exc)
        return []
    return [dict(row) for row in rows]


def _catalog_write(
    root: str | Path,
    cfg: dict[str, Any] | None,
    entries: list[dict[str, Any]],
) -> int:
    if not enabled(root, cfg) or not entries:
        return 0
    settings = storage_settings(root, cfg)
    stamp = _now()
    values = [
        (
            str(entry["catalog_id"]),
            str(entry["kind"]),
            str(entry.get("source") or ""),
            str(entry.get("symbol") or ""),
            str(entry.get("period") or ""),
            int(entry.get("rows") or 0),
            str(entry.get("ts_from") or ""),
            str(entry.get("ts_to") or ""),
            str(entry.get("digest") or ""),
            stamp,
            json_text(entry.get("payload") or {}),
        )
        for entry in entries
    ]
    try:
        with DB_LOCK, closing(connect(settings.db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO data_catalog
                    (catalog_id, kind, source, symbol, period, rows,
                     ts_from, ts_to, digest, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(catalog_id) DO UPDATE SET
                    kind = excluded.kind,
                    source = excluded.source,
                    symbol = excluded.symbol,
                    period = excluded.period,
                    rows = excluded.rows,
                    ts_from = excluded.ts_from,
                    ts_to = excluded.ts_to,
                    digest = excluded.digest,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                values,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据目录写入失败: {}", exc)
        return 0
    return len(values)


def refresh_kline_catalog(root: str | Path, cfg: dict[str, Any] | None = None) -> int:
    """把清洗库和原样库的库存范围写进 data_catalog，可重复执行。"""
    if not enabled(root, cfg):
        return 0
    base = Path(root)
    entries: list[dict[str, Any]] = []
    try:
        from src.data.bar_store import BarStore, RawBarStore
        from src.data.inventory import split_stored_symbol

        clean = BarStore(base / KLINE_MARKET)
        for item in clean.summarize():
            stored = str(item["symbol"])
            contract, source = split_stored_symbol(stored)
            entries.append(
                {
                    "catalog_id": f"kline:clean:{stored}:{item['period']}",
                    "kind": "kline",
                    "source": source,
                    "symbol": contract,
                    "period": str(item["period"]),
                    "rows": item["n"],
                    "ts_from": item["ts_from"],
                    "ts_to": item["ts_to"],
                    "digest": _digest("kline", "clean", stored, item["period"], item["n"], item["ts_from"], item["ts_to"]),
                    "payload": {"layer": "clean", "stored": stored},
                }
            )
        raw = RawBarStore(base / KLINE_RAW)
        for item in raw.summarize():
            entries.append(
                {
                    "catalog_id": f"kline:raw:{item['symbol']}:{item['period']}:{item['source']}",
                    "kind": "kline",
                    "source": str(item["source"]),
                    "symbol": str(item["symbol"]),
                    "period": str(item["period"]),
                    "rows": item["n"],
                    "ts_from": item["ts_from"],
                    "ts_to": item["ts_to"],
                    "digest": _digest(
                        "kline", "raw", item["symbol"], item["period"], item["source"],
                        item["n"], item["ts_from"], item["ts_to"],
                    ),
                    "payload": {"layer": "raw"},
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("K 线目录刷新失败: {}", exc)
        return 0
    return _catalog_write(base, cfg, entries)


def _tick_database(root: str | Path, cfg: dict[str, Any] | None) -> Path:
    data = (cfg or {}).get("data")
    tick = data.get("tick") if isinstance(data, dict) else None
    raw = tick.get("database") if isinstance(tick, dict) else None
    path = Path(str(raw)) if raw else TICK_DB
    return path if path.is_absolute() else Path(root) / path


def refresh_tick_catalog(root: str | Path, cfg: dict[str, Any] | None = None) -> int:
    """把 tick 库的每个合约库存写进 data_catalog；没有库文件就是空目录。"""
    if not enabled(root, cfg):
        return 0
    base = Path(root)
    database = _tick_database(base, cfg)
    if not database.exists():
        return 0
    try:
        from src.data.tick.vnpy_store import tick_overview

        rows = tick_overview(database=database)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tick 目录刷新失败: {}", exc)
        return 0
    entries: list[dict[str, Any]] = []
    for item in rows:
        symbol = str(item.get("symbol") or "")
        exchange = str(item.get("exchange") or "")
        entries.append(
            {
                "catalog_id": f"tick:{exchange}.{symbol}",
                "kind": "tick",
                "source": "vnpy",
                "symbol": symbol,
                "period": "tick",
                "rows": item.get("count"),
                "ts_from": item.get("start"),
                "ts_to": item.get("end"),
                "digest": _digest(
                    "tick", exchange, symbol, item.get("count"), item.get("start"), item.get("end")
                ),
                "payload": {"exchange": exchange, "database": str(database)},
            }
        )
    return _catalog_write(base, cfg, entries)


def refresh_all(root: str | Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """合约快照 + K 线目录 + tick 目录，CLI 和启动脚本共用。"""
    return {
        "instruments": refresh_instruments(root, cfg),
        "klines": refresh_kline_catalog(root, cfg),
        "ticks": refresh_tick_catalog(root, cfg),
    }


def catalog_reference(root: str | Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """回测留痕用的数据版本：最近合约快照 + 各层目录的整体指纹。"""
    if not enabled(root, cfg):
        return {}
    try:
        conn = _read_conn(root, cfg)
        if conn is None:
            return {}
        with closing(conn):
            snap = conn.execute(
                """
                SELECT snapshot_id FROM instrument_snapshots
                 ORDER BY created_at DESC, snapshot_id DESC LIMIT 1
                """
            ).fetchone()
            groups = conn.execute(
                """
                SELECT kind,
                       COUNT(*) AS groups,
                       COALESCE(SUM(rows), 0) AS total,
                       COALESCE(MIN(ts_from), '') AS ts_from,
                       COALESCE(MAX(ts_to), '') AS ts_to
                  FROM data_catalog
                 GROUP BY kind
                """
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取数据目录引用失败: {}", exc)
        return {}
    out: dict[str, Any] = {}
    if snap is not None:
        out["instruments_snapshot"] = str(snap["snapshot_id"])
    for row in groups:
        kind = str(row["kind"])
        out[f"{kind}_digest"] = _digest(
            kind, row["groups"], row["total"], row["ts_from"], row["ts_to"]
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="刷新合约快照与 K 线/tick 目录")
    parser.add_argument("--root", default=".", help="项目根目录")
    parser.add_argument("--instruments", action="store_true", help="只导合约快照")
    parser.add_argument("--klines", action="store_true", help="只刷 K 线目录")
    parser.add_argument("--ticks", action="store_true", help="只刷 tick 目录")
    parser.add_argument("--all", action="store_true", help="全部刷新（默认）")
    args = parser.parse_args(argv)
    root = Path(args.root)
    anything = args.instruments or args.klines or args.ticks or args.all
    result: dict[str, Any] = {}
    if args.all or not anything:
        result = refresh_all(root)
    else:
        if args.instruments:
            result["instruments"] = refresh_instruments(root)
        if args.klines:
            result["klines"] = refresh_kline_catalog(root)
        if args.ticks:
            result["ticks"] = refresh_tick_catalog(root)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
