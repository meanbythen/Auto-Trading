"""
天勤专业版历史 tick 批量下载。

只给试用/专业版权限用：
1. query_his_cont_quotes 取主连历史主力，切成「真实合约 + 起止日期」；
2. 再按 chunk_days 切块，DataDownloader(dur_sec=0) 下载原始 csv；
3. 断点续传：download_manifest.json 记已完成块，已存在的 csv 不重下；
4. 下载后清洗 → vnpy save_tick_data() 入库 → 合成 tick K 线。

权限不足会直接抛 DownloadPermissionError，不做无意义重试。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from loguru import logger

from src.data.tick.clean import clean_tick_frame
from src.data.tick.recorder import tick_settings
from src.data.tick.synthesize import synthesize_symbol
from src.data.tick.vnpy_store import save_ticks, ticks_from_frame
from src.data.tq_source import config_symbol_from_tq, tq_symbol
from src.utils.holidays import calendar_from_config
from src.utils.session_clock import in_trading_sessions, sessions_for_symbol
from src.utils.trading_day import is_trading_day

_PERMISSION_HINT = "需要天勤专业版或试用权限才能下载历史 tick"


class DownloadPermissionError(RuntimeError):
    """账号没有 tq_dl 权限。"""


@dataclass
class DownloadChunk:
    """一块待下载的 tick：一个真实合约 + 一段日期（end 含当天）。"""

    product: str
    contract: str
    start: date
    end: date
    path: Path

    @property
    def key(self) -> str:
        return f"{self.contract}|{self.start.isoformat()}|{self.end.isoformat()}"


def main_cont_from_symbol(symbol: str) -> str:
    """fu2701 / fu → KQ.m@SHFE.fu，给历史主力查询用。"""
    instrument = tq_symbol(symbol)
    if instrument.startswith("KQ.m@"):
        return instrument
    exchange, code = instrument.split(".", 1)
    product = "".join(ch for ch in code if ch.isalpha())
    return f"KQ.m@{exchange}.{product}"


def main_cont_list_from_symbols(symbols: list[str]) -> list[str]:
    """关注品种收成不重复的天勤主连代码。"""
    out: list[str] = []
    for raw in symbols:
        text = str(raw).strip()
        if not text:
            continue
        try:
            key = main_cont_from_symbol(text)
        except ValueError:
            continue
        if key not in out:
            out.append(key)
    return out


def download_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """data.tq.history_tick 的配置，缺项给默认值。"""
    base = tick_settings(cfg)
    root = Path(base["root"])
    data_cfg = cfg.get("data", {}) or {}
    tq_cfg = data_cfg.get("tq", {}) or {}
    history = tq_cfg.get("history_tick") or {}
    sessions: list[list[str]] = []
    for symbol in base["symbols"]:
        for row in sessions_for_symbol(cfg, symbol):
            item = [str(row[0]), str(row[1])]
            if item not in sessions:
                sessions.append(item)
    return {
        **base,
        "enabled": bool(history.get("enabled", False)),
        "main_cont_list": [str(item) for item in (history.get("main_cont_list") or [])],
        "start": str(history.get("start") or ""),
        "end": str(history.get("end") or ""),
        "chunk_days": max(1, int(history.get("chunk_days", 1))),
        "chunk_pause_sec": max(0.0, float(history.get("chunk_pause_sec", 3))),
        "policy": str(history.get("policy") or "off_hours_only").strip().lower(),
        "auto_resume": bool(history.get("auto_resume", True)),
        "resume_poll_sec": max(5.0, float(history.get("resume_poll_sec", 60))),
        "lookback_days": max(1, int(history.get("lookback_days", 120))),
        "out_dir": root / str(history.get("out_dir", "data/ticks/history")),
        "keep_raw_csv": bool(history.get("keep_raw_csv", True)),
        "trading_sessions": sessions,
        "calendar": calendar_from_config(cfg, root),
    }


def trading_pause_reason(settings: dict[str, Any], moment: datetime | None = None) -> str:
    """off_hours_only 下，交易时段返回暂停原因；周末、节假日和 manual 不拦。"""
    if str(settings.get("policy") or "off_hours_only").lower() != "off_hours_only":
        return ""
    now = moment or datetime.now()
    sessions = settings.get("trading_sessions") or []
    if not sessions:
        return ""
    day = now.date()
    if now.time() < dtime(6, 0):
        day -= timedelta(days=1)
    if not is_trading_day(day, calendar=settings.get("calendar")):
        return ""
    if in_trading_sessions(sessions, now, grace_sec=0):
        return "当前处于交易时段，历史 tick 已暂停；收盘后可从断点继续"
    return ""


def _parse_day(value: Any) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    return pd.Timestamp(value).date()


def _segments(frame: pd.DataFrame, column: str) -> list[tuple[str, date, date]]:
    """把「每日主力标的」压成连续区间：[(SHFE.fu2701, 起, 止), ...]。"""
    if frame is None or len(frame) == 0 or column not in frame.columns:
        return []
    segments: list[tuple[str, date, date]] = []
    current = ""
    start: date | None = None
    end: date | None = None
    for _, row in frame.iterrows():
        day = pd.Timestamp(row.get("date")).date()
        contract = str(row.get(column) or "").strip()
        if not contract:
            continue
        if contract != current:
            if current and start is not None and end is not None:
                segments.append((current, start, end))
            current, start, end = contract, day, day
        else:
            end = day
    if current and start is not None and end is not None:
        segments.append((current, start, end))
    return segments


def _product_of(main_cont: str) -> str:
    """KQ.m@SHFE.fu → fu。"""
    return main_cont.split("@")[-1].split(".")[-1]


def plan_chunks(api: Any, settings: dict[str, Any]) -> list[DownloadChunk]:
    """按主力分段和 chunk_days 列出所有下载块。"""
    begin = _parse_day(settings.get("start"))
    finish = _parse_day(settings.get("end"))
    chunk_days = max(1, int(settings.get("chunk_days", 1)))
    out_dir = Path(settings["out_dir"])
    chunks: list[DownloadChunk] = []
    for main_cont in settings.get("main_cont_list") or []:
        text = str(main_cont).strip()
        if not text:
            continue
        frame = api.query_his_cont_quotes(text, n=9999)
        product = _product_of(text)
        for instrument, seg_start, seg_end in _segments(frame, text):
            low = max(seg_start, begin) if begin else seg_start
            high = min(seg_end, finish) if finish else seg_end
            if low > high:
                continue
            cursor = low
            while cursor <= high:
                block_end = min(cursor + timedelta(days=chunk_days - 1), high)
                path = out_dir / product / instrument / f"{instrument}_{cursor:%Y%m%d}_{block_end:%Y%m%d}.csv"
                chunks.append(DownloadChunk(product, instrument, cursor, block_end, path))
                cursor = block_end + timedelta(days=1)
    return chunks


def _load_manifest(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "download_manifest.json"
    if not path.exists():
        return {"chunks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"chunks": {}}
    if not isinstance(data.get("chunks"), dict):
        data["chunks"] = {}
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _dates_overlap(left_start: date, left_end: date, right_start: date, right_end: date) -> bool:
    """两段闭区间有没有交叉。"""
    return left_start <= right_end and right_start <= left_end


def _contract_matches(stored: str, wanted: str | None) -> bool:
    """SHFE.fu2701 既能对全名，也能对 fu2701。"""
    if not wanted:
        return True
    text = str(stored or "").strip()
    need = str(wanted).strip()
    if not need:
        return True
    if text == need:
        return True
    if "." in text and text.split(".", 1)[-1] == need:
        return True
    if "." in need and need.split(".", 1)[-1] == text:
        return True
    return False


def forget_downloaded_chunks(
    out_dir: Path | str,
    *,
    contract: str | None = None,
    start: date | None = None,
    end: date | None = None,
    delete_csv: bool = True,
) -> dict[str, Any]:
    """
    忘掉这一段的断点续传，让下次「下载历史 tick」真的重新去下。

    原理：下载成功后会在 manifest 里记 done，并且留下 csv。
    只删库里的 tick 不够，下次仍会跳过。这里把交叉的 csv 删掉，并把 done 标记拿掉。
    不改 tick 内容，只删文件和续传记录。
    """
    folder = Path(out_dir)
    manifest = _load_manifest(folder)
    removed_keys: list[str] = []
    kept: dict[str, Any] = {}
    for key, meta in (manifest.get("chunks") or {}).items():
        info = meta if isinstance(meta, dict) else {}
        stored = str(info.get("contract") or "")
        if not stored and "|" in str(key):
            stored = str(key).split("|", 1)[0]
        chunk_start = _parse_day(info.get("start"))
        chunk_end = _parse_day(info.get("end"))
        if chunk_start is None or chunk_end is None:
            parts = str(key).split("|")
            if len(parts) >= 3:
                chunk_start = chunk_start or _parse_day(parts[1])
                chunk_end = chunk_end or _parse_day(parts[2])
        hit_contract = _contract_matches(stored, contract)
        hit_time = True
        if start is not None and end is not None and chunk_start and chunk_end:
            hit_time = _dates_overlap(chunk_start, chunk_end, start, end)
        elif start is not None and chunk_end:
            hit_time = chunk_end >= start
        elif end is not None and chunk_start:
            hit_time = chunk_start <= end
        if hit_contract and hit_time:
            removed_keys.append(str(key))
        else:
            kept[key] = meta
    manifest["chunks"] = kept
    if folder.exists() or removed_keys:
        _write_json(folder / "download_manifest.json", manifest)

    csv_deleted: list[str] = []
    csv_locked: list[str] = []
    if delete_csv and folder.exists():
        from src.data.tick.inventory import _parse_history_csv_name

        for path in folder.rglob("*.csv"):
            parsed = _parse_history_csv_name(path)
            if parsed is None:
                continue
            stored, file_start, file_end = parsed
            if not _contract_matches(stored, contract):
                continue
            hit_time = True
            if start is not None and end is not None:
                hit_time = _dates_overlap(file_start, file_end, start, end)
            elif start is not None:
                hit_time = file_end >= start
            elif end is not None:
                hit_time = file_start <= end
            if not hit_time:
                continue
            try:
                path.unlink()
                csv_deleted.append(str(path))
            except OSError:
                csv_locked.append(str(path))
    return {
        "manifest_cleared": len(removed_keys),
        "csv_deleted": csv_deleted,
        "csv_locked": csv_locked,
    }


def _is_permission_error(exc: Exception) -> bool:
    """天勤可能在创建任务或等待下载时才返回权限错误。"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("不支持下载历史数据", "历史数据权限", "tq_dl", "需要专业版")
    )


def _download_chunk(api: Any, chunk: DownloadChunk, on_progress: Callable[[float], None] | None = None) -> None:
    """阻塞下载一块。没有专业版权限时给出明确提示。"""
    from tqsdk.tools import DataDownloader

    chunk.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        task = DataDownloader(
            api,
            symbol_list=chunk.contract,
            dur_sec=0,
            start_dt=chunk.start,
            end_dt=chunk.end,
            csv_file_name=str(chunk.path),
        )
        last = -1
        while not task.is_finished():
            api.wait_update()
            progress = float(task.get_progress())
            step = int(progress // 10) * 10
            if on_progress is not None and step != last:
                on_progress(progress)
                last = step
    except Exception as exc:  # noqa: BLE001
        if _is_permission_error(exc):
            raise DownloadPermissionError(f"{_PERMISSION_HINT}：{exc}") from exc
        raise
    if not chunk.path.exists():
        raise RuntimeError(f"下载结束但没有生成 csv：{chunk.path}")


def _ingest_chunk(chunk: DownloadChunk, settings: dict[str, Any]) -> dict[str, Any]:
    """原始 csv → 清洗 → vnpy 入库。返回这一块的统计。"""
    if not chunk.path.exists() or chunk.path.stat().st_size == 0:
        return {"raw": 0, "kept": 0, "dropped": 0}
    frame = pd.read_csv(chunk.path)
    cleaned, stats = clean_tick_frame(frame)
    if len(cleaned) == 0:
        return {"raw": int(stats.raw), "kept": 0, "dropped": int(stats.dropped)}
    ticks = ticks_from_frame(cleaned.reset_index(), instrument=chunk.contract)
    saved = save_ticks(ticks, database=settings["database"], batch_size=int(settings["batch_size"]))
    return {"raw": int(stats.raw), "kept": int(saved), "dropped": int(stats.dropped)}


def run_download(
    settings: dict[str, Any],
    *,
    api_factory: Callable[[], Any] | None = None,
    downloader_factory: Callable[[Any, DownloadChunk], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    dry_run: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """
    执行一轮批量下载。

    dry_run 只列计划，不下载、不入库。已完成的块（manifest 标记 done）整块跳过；
    csv 已存在但还没入库的，直接进入清洗入库。默认每块开始前检查交易时段，
    开盘就暂停并保留断点，下一轮接着补。下载完按合约整体合成一次 K 线。
    """
    out_dir = Path(settings["out_dir"])
    manifest_path = out_dir / "download_manifest.json"
    status_path = out_dir / "download_status.json"
    summary: dict[str, Any] = {
        "status": "done",
        "total": 0,
        "downloaded": 0,
        "reused": 0,
        "skipped": 0,
        "failed": 0,
        "symbols": {},
    }
    now = clock or datetime.now
    if not dry_run:
        reason = trading_pause_reason(settings, now())
        if reason:
            summary.update({"status": "paused", "paused": True, "message": reason})
            _write_json(
                status_path,
                {"state": "paused", "total": 0, "finished": 0, "percent": 0.0, "message": reason},
            )
            if on_progress is not None:
                on_progress(reason)
            return summary

    api: Any | None = None
    client = None
    try:
        if api_factory is None:
            user = str(settings.get("user") or "")
            password = str(settings.get("password") or "")
            if not user or not password:
                raise DownloadPermissionError(
                    "天勤需要账号和密码：请填 data.tq.user，密码放 config/secrets.local.yaml 的 tq_password"
                )
            from tqsdk import TqApi, TqAuth

            client = TqApi(auth=TqAuth(user, password))
            api = client
        else:
            api = api_factory()
        try:
            chunks = plan_chunks(api, settings)
        except Exception as exc:  # noqa: BLE001
            if not _is_permission_error(exc):
                raise
            _write_json(
                status_path,
                {"state": "permission_denied", "message": _PERMISSION_HINT, "total": 0},
            )
            raise DownloadPermissionError(f"{_PERMISSION_HINT}：{exc}") from exc

        manifest = _load_manifest(out_dir)
        total = len(chunks)
        summary["total"] = total

        if dry_run:
            for chunk in chunks:
                if on_progress is not None:
                    on_progress(f"[计划] {chunk.contract} {chunk.start} ~ {chunk.end} → {chunk.path}")
            summary.update({"status": "dry_run", "dry_run": True})
            return summary

        download = downloader_factory or (lambda _api, chunk: _download_chunk(_api, chunk))
        for index, chunk in enumerate(chunks, start=1):
            done = manifest["chunks"].get(chunk.key, {})
            if done.get("status") == "done":
                summary["skipped"] += 1
            else:
                reason = trading_pause_reason(settings, now())
                if reason:
                    summary.update(
                        {
                            "status": "paused",
                            "paused": True,
                            "remaining": total - index + 1,
                            "message": reason,
                        }
                    )
                    _write_json(manifest_path, manifest)
                    _write_json(
                        status_path,
                        {
                            "state": "paused",
                            "total": total,
                            "finished": index - 1,
                            "percent": round((index - 1) * 100.0 / total, 1),
                            "next": chunk.contract,
                            "message": reason,
                            "updated": datetime.now().isoformat(timespec="seconds"),
                        },
                    )
                    if on_progress is not None:
                        on_progress(reason)
                    return summary
                try:
                    if chunk.path.exists() and chunk.path.stat().st_size > 0:
                        summary["reused"] += 1
                    else:
                        download(api, chunk)
                        summary["downloaded"] += 1
                    stats = _ingest_chunk(chunk, settings)
                    manifest["chunks"][chunk.key] = {
                        "status": "done",
                        "contract": chunk.contract,
                        "start": chunk.start.isoformat(),
                        "end": chunk.end.isoformat(),
                        "csv": str(chunk.path),
                        **stats,
                        "updated": datetime.now().isoformat(timespec="seconds"),
                    }
                    summary["symbols"][chunk.contract] = summary["symbols"].get(chunk.contract, 0) + int(
                        stats.get("kept", 0)
                    )
                    if not settings.get("keep_raw_csv", True):
                        try:
                            chunk.path.unlink()
                        except OSError:
                            logger.debug("删除原始 csv 失败：{}", chunk.path)
                except DownloadPermissionError:
                    _write_json(
                        status_path,
                        {"state": "permission_denied", "message": _PERMISSION_HINT, "total": total},
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    summary["failed"] += 1
                    manifest["chunks"][chunk.key] = {
                        "status": "failed",
                        "contract": chunk.contract,
                        "start": chunk.start.isoformat(),
                        "end": chunk.end.isoformat(),
                        "error": str(exc),
                        "updated": datetime.now().isoformat(timespec="seconds"),
                    }
                    logger.warning("tick 下载失败 {} {}~{}：{}", chunk.contract, chunk.start, chunk.end, exc)
            _write_json(manifest_path, manifest)
            _write_json(
                status_path,
                {
                    "state": "running",
                    "total": total,
                    "finished": index,
                    "percent": round(index * 100.0 / total, 1) if total else 100.0,
                    "current": chunk.contract,
                    "updated": datetime.now().isoformat(timespec="seconds"),
                },
            )
            if on_progress is not None:
                on_progress(f"[{index}/{total}] {chunk.contract} {chunk.start} ~ {chunk.end}")
            pause = float(settings.get("chunk_pause_sec") or 0)
            if index < total and pause > 0:
                # 单连接串行下载，块间留一点空档，别把行情通道占满。
                time.sleep(pause)

        for contract in sorted({chunk.contract for chunk in chunks}):
            symbol = config_symbol_from_tq(contract)
            if not symbol:
                continue
            try:
                report = synthesize_symbol(
                    symbol,
                    database=settings["database"],
                    bar_store=settings["bar_store"],
                    periods=settings["synth_periods"],
                )
                summary["symbols"][contract] = int(report["kept"])
                if on_progress is not None:
                    on_progress(f"合成 {symbol}：写库 {report['bars']}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("下载后合成 {} 失败：{}", symbol, exc)
        _write_json(
            status_path,
            {"state": "done", "total": total, "finished": total, "percent": 100.0, **summary},
        )
        return summary
    finally:
        if client is not None:
            try:
                client.close()
            except BaseException:  # noqa: BLE001
                logger.debug("关闭下载连接时出错，已忽略")


def run_download_auto_resume(
    settings: dict[str, Any],
    *,
    api_factory: Callable[[], Any] | None = None,
    downloader_factory: Callable[[Any, DownloadChunk], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    dry_run: bool = False,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    stop: Callable[[], bool] | None = None,
    on_pause: Callable[[str], None] | None = None,
    on_wake: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """
    开盘暂停后等到下一个非交易时段自动续传，直到补完。

    `auto_resume=false` 时只跑一轮，行为和 run_download 一致。等待期间不占天勤
    连接，只按 resume_poll_sec 轮询交易时段；stop() 为真时停止等待并保留断点。
    dry_run 只列计划，不等待。on_pause 在进入等待时调用，on_wake 在收盘续传前
    调用，给调用方恢复/暂停实时采集用。
    """
    now = clock or datetime.now
    nap = sleep or time.sleep
    poll = max(5.0, float(settings.get("resume_poll_sec") or 60))
    auto = bool(settings.get("auto_resume", True)) and not dry_run
    status_path = Path(settings["out_dir"]) / "download_status.json"
    # 失败块下一轮会重试，失败数取最后一轮，避免重复累计。
    totals = {"downloaded": 0, "reused": 0}
    rounds = 0

    def _stopped() -> bool:
        return bool(stop is not None and stop())

    while True:
        summary = run_download(
            settings,
            api_factory=api_factory,
            downloader_factory=downloader_factory,
            on_progress=on_progress,
            dry_run=dry_run,
            clock=now,
        )
        rounds += 1
        for key in totals:
            totals[key] += int(summary.get(key) or 0)
        if summary.get("status") != "paused" or not auto:
            if rounds > 1:
                summary.update(totals)
                summary["rounds"] = rounds
            return summary

        remaining = int(summary.get("remaining") or 0)
        total = int(summary.get("total") or 0)
        wait_text = str(summary.get("message") or "当前处于交易时段")
        message = f"{wait_text}；已安排收盘后自动继续"
        _write_json(
            status_path,
            {
                "state": "waiting_resume",
                "total": total,
                "finished": max(0, total - remaining),
                "percent": round(max(0, total - remaining) * 100.0 / total, 1) if total else 0.0,
                "remaining": remaining,
                "message": message,
                "updated": now().isoformat(timespec="seconds"),
            },
        )
        if on_progress is not None:
            on_progress(message)
        if on_pause is not None:
            on_pause(message)
        while True:
            if _stopped():
                summary.update(totals)
                summary.update({"rounds": rounds, "resume_stopped": True})
                _write_json(
                    status_path,
                    {
                        "state": "paused",
                        "total": total,
                        "finished": max(0, total - remaining),
                        "percent": round(max(0, total - remaining) * 100.0 / total, 1)
                        if total
                        else 0.0,
                        "remaining": remaining,
                        "message": "已停止自动续传，断点已保留",
                        "updated": now().isoformat(timespec="seconds"),
                    },
                )
                if on_progress is not None:
                    on_progress("已停止自动续传，断点已保留；下次运行会接着补")
                return summary
            if not trading_pause_reason(settings, now()):
                break
            nap(poll)
        if on_progress is not None:
            on_progress("已收盘，自动继续补历史 tick")
        if on_wake is not None:
            on_wake()
