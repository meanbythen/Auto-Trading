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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from loguru import logger

from src.data.tick.clean import clean_tick_frame
from src.data.tick.recorder import tick_settings
from src.data.tick.synthesize import synthesize_symbol
from src.data.tick.vnpy_store import save_ticks, ticks_from_frame
from src.data.tq_source import config_symbol_from_tq, tq_symbol

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
    return {
        **base,
        "enabled": bool(history.get("enabled", False)),
        "main_cont_list": [str(item) for item in (history.get("main_cont_list") or [])],
        "start": str(history.get("start") or ""),
        "end": str(history.get("end") or ""),
        "chunk_days": max(1, int(history.get("chunk_days", 1))),
        "out_dir": root / str(history.get("out_dir", "data/ticks/history")),
        "keep_raw_csv": bool(history.get("keep_raw_csv", True)),
    }


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
    except Exception as exc:  # noqa: BLE001
        if "不支持下载历史数据" in str(exc) or "tq_dl" in str(exc):
            raise DownloadPermissionError(f"{_PERMISSION_HINT}：{exc}") from exc
        raise
    last = -1
    while not task.is_finished():
        api.wait_update()
        progress = float(task.get_progress())
        step = int(progress // 10) * 10
        if on_progress is not None and step != last:
            on_progress(progress)
            last = step
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
) -> dict[str, Any]:
    """
    执行一轮批量下载。

    dry_run 只列计划，不下载、不入库。已完成的块（manifest 标记 done）整块跳过；
    csv 已存在但还没入库的，直接进入清洗入库。下载完按合约整体合成一次 K 线。
    """
    chunks: list[DownloadChunk] = []
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
        chunks = plan_chunks(api, settings)

        out_dir = Path(settings["out_dir"])
        manifest_path = out_dir / "download_manifest.json"
        manifest = _load_manifest(out_dir)
        status_path = out_dir / "download_status.json"
        total = len(chunks)
        summary = {"total": total, "downloaded": 0, "reused": 0, "skipped": 0, "failed": 0, "symbols": {}}

        if dry_run:
            for chunk in chunks:
                if on_progress is not None:
                    on_progress(f"[计划] {chunk.contract} {chunk.start} ~ {chunk.end} → {chunk.path}")
            summary["dry_run"] = True
            return summary

        download = downloader_factory or (lambda _api, chunk: _download_chunk(_api, chunk))
        for index, chunk in enumerate(chunks, start=1):
            done = manifest["chunks"].get(chunk.key, {})
            if done.get("status") == "done":
                summary["skipped"] += 1
            else:
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
