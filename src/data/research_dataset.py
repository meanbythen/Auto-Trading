"""把现有具体合约库存和主力合约表固化成可审计的研究数据集。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from src.backtest.experiment import fingerprint_bars, fingerprint_file
from src.backtest.rollover import (
    clip_periods,
    keep_old_contract_on_rollover_day,
    load_main_periods,
    resolve_main_table,
)
from src.data.bar_store import BarStore, RawBarStore
from src.data.coverage import coverage_report, expected_bar_times
from src.data.periods import canonical_period
from src.data.quote_check import normalize_source, product_code, storage_symbol
from src.risk.delivery import apply_delivery_cutoff

SCHEMA_VERSION = 1
CLEAN_VERSION = "legacy_unversioned_current_store"


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _clip(frame: pd.DataFrame | None, start: date, end: date) -> pd.DataFrame | None:
    if frame is None or len(frame) == 0:
        return None
    left = pd.Timestamp(datetime.combine(start, time.min))
    right = pd.Timestamp(datetime.combine(end + timedelta(days=1), time.min))
    out = frame[(frame.index >= left) & (frame.index < right)].copy(deep=True)
    return out if len(out) else None


def _write_bars(path: Path, frame: pd.DataFrame | None) -> dict[str, Any]:
    if frame is None or len(frame) == 0:
        return {"rows": 0, "path": "", "sha256": "", "file_sha256": ""}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            frame.to_csv(zipped, index_label="datetime", encoding="utf-8")
    info = fingerprint_bars(frame)
    file_info = fingerprint_file(path)
    info.update(
        {
            "path": "",
            "file_sha256": file_info["sha256"],
            "size": file_info["size"],
        }
    )
    return info


def _download_events(
    root: Path,
    clean: BarStore,
    symbols: set[str],
    period: str,
    source: str,
) -> dict[str, list[dict[str, Any]]]:
    events = {symbol: [] for symbol in symbols}
    for row in clean.list_ingests(10_000):
        stored = str(row.get("symbol") or "")
        symbol = next((item for item in symbols if stored in {item, storage_symbol(item, source)}), "")
        if not symbol or canonical_period(str(row.get("period") or period)) != period:
            continue
        if normalize_source(str(row.get("source") or source)) != source:
            continue
        events[symbol].append(
            {
                "started_at": str(row.get("created_at") or ""),
                "received_at": str(row.get("created_at") or ""),
                "evidence": "data/klines/market.sqlite#ingest_log",
            }
        )
    health_path = root / "data" / "market_health.jsonl"
    if not health_path.exists():
        return events
    with health_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            symbol = str(row.get("symbol") or "")
            if symbol not in events or canonical_period(str(row.get("period") or period)) != period:
                continue
            if normalize_source(str(row.get("source") or source)) != source:
                continue
            events[symbol].append(
                {
                    "started_at": str(row.get("fetch_started_at") or ""),
                    "received_at": str(row.get("received_at") or ""),
                    "evidence": "data/market_health.jsonl",
                }
            )
    return events


def _download_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    starts = sorted(str(row.get("started_at") or "") for row in rows if row.get("started_at"))
    received = sorted(str(row.get("received_at") or "") for row in rows if row.get("received_at"))
    return {
        "status": "recorded" if rows else "missing",
        "first_fetch_started_at": starts[0] if starts else "",
        "last_received_at": received[-1] if received else "",
        "event_count": len(rows),
        "evidence": sorted({str(row.get("evidence") or "") for row in rows if row.get("evidence")}),
        "note": "没有下载日志时不用文件修改时间冒充下载时间。" if not rows else "",
    }


def _table_gaps(start: date, end: date, segments: list[tuple[Any, str, str]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    cursor = start
    for _period, raw_start, raw_end in sorted(segments, key=lambda item: item[1]):
        seg_start = date.fromisoformat(raw_start)
        seg_end = date.fromisoformat(raw_end)
        if seg_start > cursor:
            gaps.append({"start": cursor.isoformat(), "end": (seg_start - timedelta(days=1)).isoformat()})
        cursor = max(cursor, seg_end + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append({"start": cursor.isoformat(), "end": end.isoformat()})
    return gaps


def _store_info(root: Path, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": "", "sha256": "", "size": 0}
    info = fingerprint_file(path)
    try:
        info["path"] = path.relative_to(root).as_posix()
    except ValueError:
        pass
    info["mutable_source"] = True
    return info


def build_research_dataset(
    root: Path | str,
    *,
    product: str,
    period: str,
    source: str = "akshare",
    start_date: str,
    end_date: str,
    table_path: Path | str | None = None,
    output_root: Path | str | None = None,
) -> dict[str, Any]:
    """只读现有库存，固化清洗/原样快照；缺段和证据不足不会被自动补成合格。"""
    base = Path(root)
    code = canonical_period(period)
    src = normalize_source(source)
    prod = product_code(product) or product.strip().lower()
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    cfg = {"_project_root": str(base)}
    table = resolve_main_table(cfg, table_path)
    if table is None:
        raise FileNotFoundError("找不到主力合约表，不能建立换月研究数据集")
    all_periods = load_main_periods(table)
    periods = apply_delivery_cutoff(keep_old_contract_on_rollover_day(all_periods.get(prod) or []))
    segments = clip_periods(periods, start.isoformat(), end.isoformat())
    clean = BarStore(base / "data" / "klines" / "market.sqlite")
    raw = RawBarStore(base / "data" / "klines" / "raw.sqlite")
    events = _download_events(base, clean, {row.symbol for row, _, _ in segments}, code, src)

    created = datetime.now().astimezone()
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "product": prod,
                "period": code,
                "source": src,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "table_sha256": fingerprint_file(table)["sha256"],
                "token": uuid.uuid4().hex,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    dataset_id = f"{created:%Y%m%dT%H%M%S}-{request_hash[:10]}"
    destination = Path(output_root) if output_root else base / "data" / "research_datasets"
    folder = destination / dataset_id
    rows: list[dict[str, Any]] = []
    for index, (main, raw_start, raw_end) in enumerate(segments, 1):
        seg_start = date.fromisoformat(raw_start)
        seg_end = date.fromisoformat(raw_end)
        stored = storage_symbol(main.symbol, src)
        clean_bars = _clip(clean.load(stored, code), seg_start, seg_end)
        raw_bars = _clip(raw.load(stored, code, src), seg_start, seg_end)
        cover = coverage_report(
            clean_bars,
            datetime.combine(seg_start, time.min),
            datetime.combine(seg_end, time(23, 59)),
            code,
            max_ranges=None,
        ).as_dict()
        clean_info = _write_bars(folder / "clean" / f"{index:03d}_{main.symbol}.csv.gz", clean_bars)
        raw_info = _write_bars(folder / "raw" / f"{index:03d}_{main.symbol}.csv.gz", raw_bars)
        clean_info["path"] = f"clean/{index:03d}_{main.symbol}.csv.gz" if clean_bars is not None else ""
        raw_info["path"] = f"raw/{index:03d}_{main.symbol}.csv.gz" if raw_bars is not None else ""
        expected_times = set(
            expected_bar_times(
                datetime.combine(seg_start, time.min),
                datetime.combine(seg_end, time(23, 59)),
                code,
            )
        )
        unexpected_times = (
            [stamp for stamp in clean_bars.index if stamp not in expected_times] if clean_bars is not None else []
        )
        rows.append(
            {
                "index": index,
                "symbol": main.symbol,
                "stored_symbol": stored,
                "manual": bool(main.manual),
                "table_start": main.start.isoformat(),
                "table_end": main.end.isoformat(),
                "requested_start": raw_start,
                "requested_end": raw_end,
                "clean": clean_info,
                "raw": raw_info,
                "coverage": cover,
                "unexpected_time_rows": len(unexpected_times),
                "unexpected_times": [str(stamp) for stamp in unexpected_times],
                "downloaded": _download_summary(events.get(main.symbol, [])),
                "clean_version": CLEAN_VERSION,
                "cleaning_applied_by_builder": False,
            }
        )

    table_gaps = _table_gaps(start, end, segments)
    transitions: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        if previous["symbol"] == current["symbol"]:
            continue
        old_clean = previous["clean"]
        new_clean = current["clean"]
        old_close = None
        new_open = None
        if old_clean.get("rows"):
            old_frame = pd.read_csv(folder / old_clean["path"])
            old_close = float(old_frame.iloc[-1]["close"])
        if new_clean.get("rows"):
            new_frame = pd.read_csv(folder / new_clean["path"])
            new_open = float(new_frame.iloc[0]["open"])
        transitions.append(
            {
                "old_symbol": previous["symbol"],
                "old_effective_end": previous["requested_end"],
                "old_last_bar_time": old_clean.get("to", ""),
                "old_last_close": old_close,
                "new_symbol": current["symbol"],
                "new_effective_start": current["requested_start"],
                "new_first_bar_time": new_clean.get("from", ""),
                "new_first_open": new_open,
                "price_difference": None if old_close is None or new_open is None else new_open - old_close,
                "interpretation": "不同具体合约的边界价格，不合成主连收益或虚假跳空。",
            }
        )

    expected_n = sum(int(row["coverage"]["expected_n"]) for row in rows)
    have_n = sum(int(row["coverage"]["have_n"]) for row in rows)
    missing_n = sum(int(row["coverage"]["missing_n"]) for row in rows)
    unexpected_n = sum(int(row["unexpected_time_rows"]) for row in rows)
    data_complete = bool(rows) and not table_gaps and missing_n == 0 and unexpected_n == 0 and have_n > 0
    raw_trace_complete = bool(rows) and all(int(row["raw"]["rows"]) > 0 for row in rows)
    requested_days = (end - start).days + 1
    point_in_time_proven = False
    screening_ready = requested_days >= 182 and data_complete and raw_trace_complete
    validation_ready = requested_days >= 730 and screening_ready and point_in_time_proven
    risks = ["main_table_point_in_time_unproven", "legacy_clean_version_unproven", "usage_authorization_unrecorded"]
    if table_gaps or missing_n or not rows:
        risks.append("clean_data_incomplete")
    if unexpected_n:
        risks.append("unexpected_bar_times")
    if not raw_trace_complete:
        risks.append("raw_snapshot_incomplete")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "created_at": created.isoformat(timespec="seconds"),
        "product": prod,
        "source": src,
        "period": code,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "calendar_days": requested_days},
        "main_contract_table": fingerprint_file(table),
        "stores": {
            "clean": _store_info(base, clean.path),
            "raw": _store_info(base, raw.path),
        },
        "clean_version": CLEAN_VERSION,
        "segments": rows,
        "coverage": {
            "expected_n": expected_n,
            "have_n": have_n,
            "missing_n": missing_n,
            "unexpected_time_rows": unexpected_n,
            "main_table_gaps": table_gaps,
            "model": "现有交易日历与 session_bar_times；缺段只报告，不前向填充。",
        },
        "rollover_audit": {
            "point_in_time_proven": point_in_time_proven,
            "risk": "主力表由历史日终持仓和人工修正生成，缺少逐版本 as-of 证据；次日切换不能单独证明无未来信息。",
            "transitions": transitions,
        },
        "usage": {
            "access_method": "existing_local_sqlite_read_only",
            "network_download_by_builder": False,
            "export_method": "gzip_csv_snapshot",
            "authorization_status": "not_recorded",
            "note": "数据使用权、账号权限和导出权限以供应商/交易所授权为准；现有库存未附授权证明。",
            "source_role": "cross_check_only" if src == "ths" else "research_source",
            "tq_limit_note": "本构建器不联网；天勤长历史只能另用官方分段/下载或授权数据处理。"
            if src == "tq"
            else "",
        },
        "readiness": {
            "data_complete": data_complete,
            "raw_trace_complete": raw_trace_complete,
            "screening_ready": screening_ready,
            "validation_ready": validation_ready,
            "risk_flags": risks,
            "note": "ready 仅指数据覆盖门禁，不代表策略研究、模拟或实盘通过。",
        },
        "artifacts": {"coverage": "coverage.json", "rollover_audit": "rollover_audit.json"},
    }
    digest_payload = {
        "request": manifest["window"] | {"product": prod, "period": code, "source": src},
        "table_sha256": manifest["main_contract_table"]["sha256"],
        "segments": [
            {
                "symbol": row["symbol"],
                "start": row["requested_start"],
                "end": row["requested_end"],
                "clean_sha256": row["clean"]["sha256"],
                "raw_sha256": row["raw"]["sha256"],
            }
            for row in rows
        ],
    }
    manifest["dataset_sha256"] = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    coverage_payload = {"dataset_id": dataset_id, **manifest["coverage"], "segments": rows}
    rollover_payload = {"dataset_id": dataset_id, **manifest["rollover_audit"]}
    _atomic_json(folder / "coverage.json", coverage_payload)
    _atomic_json(folder / "rollover_audit.json", rollover_payload)
    _atomic_json(folder / "manifest.json", manifest)
    index_path = destination / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"datasets": []}
    index["schema_version"] = SCHEMA_VERSION
    index["datasets"] = [row for row in index.get("datasets", []) if row.get("dataset_id") != dataset_id]
    index["datasets"].append(
        {
            "dataset_id": dataset_id,
            "created_at": manifest["created_at"],
            "product": prod,
            "source": src,
            "period": code,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "dataset_sha256": manifest["dataset_sha256"],
            "readiness": manifest["readiness"],
            "manifest": f"{dataset_id}/manifest.json",
        }
    )
    _atomic_json(index_path, index)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--product", required=True)
    parser.add_argument("--period", default="5")
    parser.add_argument("--source", default="akshare")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--table-path")
    args = parser.parse_args()
    manifest = build_research_dataset(
        Path(args.root),
        product=args.product,
        period=args.period,
        source=args.source,
        start_date=args.start_date,
        end_date=args.end_date,
        table_path=args.table_path,
    )
    print(json.dumps({"dataset_id": manifest["dataset_id"], "readiness": manifest["readiness"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
