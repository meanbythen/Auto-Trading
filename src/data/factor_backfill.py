"""因子挖掘的 K 线补齐。

只做三件事：
1. 按研究口径算每个「合约 × 周期」缺多少根、缺多少持仓量、数据是否过期；
2. 缺的才补，已经够的直接跳过；
3. 每补完一格写 manifest，程序中断后下次接着补。

免费天勤的 K 线接口一次最多给服务端上限（实测约 1 万根），这里不做分块下载。
请求超过上限时记 cap_reached，下次不重复死磕同一段。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from loguru import logger

from src.data.periods import canonical_period, period_label
from src.data.quote_check import normalize_source, storage_symbol
from src.research.data_requirements import (
    DEFAULT_PERIODS,
    USABLE_DEDUCTION,
    backfill_window,
    mode_key,
    requirement_for,
)
from src.utils.session_clock import in_trading_sessions, sessions_for_symbol
from src.utils.trading_day import is_trading_day, parse_closed_days

MANIFEST_RELATIVE_PATH = "data/factor_mining/kline_manifest.json"
DEFAULT_MAX_ATTEMPTS = 3
# 免费天勤实测约 1 万根；返回量达到这个数的 98% 就当成已经摸到服务端上限。
DEFAULT_CAP_BARS = 10_000
CAP_RATIO = 0.98

# 先粗后细：日线、60、30、15、5。粗周期小、快，先把绿灯点亮一部分。
_PERIOD_ORDER = ("D", "60", "30", "15", "5", "1", "W")

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "auto_on_start": False,
    "after_close_topup": False,
    "policy": "off_hours_only",
    "max_concurrent": 1,
    "chunk_pause_sec": 3.0,
    "pause_when_trading": True,
    "target_mode": "standard",
    "periods": list(DEFAULT_PERIODS),
    "require_oi": True,
    "cap_bars": DEFAULT_CAP_BARS,
    "max_attempts": DEFAULT_MAX_ATTEMPTS,
}


def backfill_settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """读 data.factor_backfill；没配的用默认值，不在调用方各写一套。"""
    raw = ((cfg or {}).get("data") or {}).get("factor_backfill") or {}
    out = dict(DEFAULT_SETTINGS)
    for key, value in raw.items():
        if value is None:
            continue
        out[key] = value
    out["periods"] = [canonical_period(item) for item in (out.get("periods") or DEFAULT_PERIODS)]
    out["max_concurrent"] = 1  # 免费账号只允许一条下载连接，配置大了也不并发。
    return out


def manifest_path(root_dir: str | Path, settings: dict[str, Any] | None = None) -> Path:
    override = str((settings or {}).get("manifest_path") or "").strip()
    if override:
        return Path(root_dir) / override
    return Path(root_dir) / MANIFEST_RELATIVE_PATH


def load_manifest(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "tasks": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "tasks": {}}
    payload.setdefault("version", 1)
    tasks = payload.get("tasks")
    if not isinstance(tasks, dict):
        payload["tasks"] = {}
    return payload


def save_manifest(path: str | Path, payload: dict[str, Any]) -> None:
    """原子写：中途被杀不会留下半个 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)


def task_key(stored_symbol: str, period: str) -> str:
    return f"{stored_symbol}|{period}"


@dataclass
class BackfillTask:
    symbol: str
    stored_symbol: str
    period: str
    have_bars: int
    required_bars: int
    oi_ratio: float
    required_oi_ratio: float
    first_time: str
    last_time: str
    span_days: float
    need_bars: bool
    need_oi: bool
    need_fresh: bool
    start: str
    end: str
    state: str = "pending"
    note: str = ""
    attempts: int = 0
    cap_reached: bool = False

    @property
    def key(self) -> str:
        return task_key(self.stored_symbol, self.period)

    @property
    def gap_bars(self) -> int:
        return max(0, self.required_bars - self.have_bars)

    @property
    def pending(self) -> bool:
        return bool(self.need_bars or self.need_oi or self.need_fresh)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["key"] = self.key
        payload["gap_bars"] = self.gap_bars
        payload["period_label"] = period_label(self.period)
        return payload


def _period_rank(period: str) -> int:
    try:
        return _PERIOD_ORDER.index(canonical_period(period))
    except ValueError:
        return len(_PERIOD_ORDER)


def _parse_ts(text: str | None) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fresh_ok(last_time: str, period: str, now: datetime) -> bool:
    """分钟线看交易日间隔，日线看自然日间隔。和绿灯口径保持一致。"""
    last = _parse_ts(last_time)
    if last is None:
        return False
    if canonical_period(period) == "D":
        return max(0, (now.date() - last.date()).days) <= 3
    from src.research.data_requirements import trading_days_since

    return trading_days_since(last, now) <= 1


def _coverage_end(end: str | None) -> str | None:
    """BarStore 的 end 是开区间；只写日期时补到当天末尾。"""
    text = str(end or "").strip()
    if not text:
        return None
    return f"{text[:10]} 23:59:59" if len(text) <= 10 else text


def plan_kline_backfill(
    store: Any,
    symbols: Sequence[str],
    periods: Sequence[str] | None = None,
    *,
    mode: str = "standard",
    require_oi: bool = True,
    source: str = "tq",
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> list[BackfillTask]:
    """按本地库存算缺口，不下载。返回的列表里也包含已经达标的格子。"""
    req = requirement_for(mode)
    required = req.min_bars + USABLE_DEDUCTION
    chosen_periods = []
    for item in periods or DEFAULT_PERIODS:
        code = canonical_period(item)
        if code not in chosen_periods:
            chosen_periods.append(code)
    chosen_periods.sort(key=_period_rank)
    source_key = normalize_source(source)
    moment = now or datetime.now()
    tasks: list[BackfillTask] = []
    for symbol in symbols:
        clean = str(symbol or "").strip()
        if not clean or clean == "（还没有）":
            continue
        stored = storage_symbol(clean, source_key)
        for period in chosen_periods:
            cov = store.coverage(stored, period, start, _coverage_end(end))
            bars = int(cov.get("n") or 0)
            oi_ratio = float(cov.get("oi_ratio") or 0.0)
            last_time = str(cov.get("ts_to") or "")
            first_time = str(cov.get("ts_from") or "")
            first = _parse_ts(first_time)
            last = _parse_ts(last_time)
            span = (last - first).total_seconds() / 86400.0 if first and last else 0.0
            window_start, window_end = backfill_window(period, end)
            task = BackfillTask(
                symbol=clean,
                stored_symbol=stored,
                period=period,
                have_bars=bars,
                required_bars=required,
                oi_ratio=oi_ratio,
                required_oi_ratio=req.oi_ratio,
                first_time=first_time,
                last_time=last_time,
                span_days=round(span, 2),
                need_bars=bars < required,
                need_oi=bool(require_oi) and oi_ratio < req.oi_ratio,
                need_fresh=not _fresh_ok(last_time, period, moment),
                start=str(start or window_start),
                end=str(end or window_end),
            )
            if not task.pending:
                task.state = "ok"
            tasks.append(task)
    return tasks


def in_trading_session(
    cfg: dict[str, Any] | None,
    symbols: Sequence[str],
    now: datetime | None = None,
    *,
    grace_sec: float = 60.0,
) -> tuple[bool, str]:
    """现在是不是至少有一个品种在盘里。周末和节假日一律算休市。"""
    moment = now or datetime.now()
    extra_closed = parse_closed_days(((cfg or {}).get("schedule") or {}).get("closed_days"))
    if not is_trading_day(moment.date(), extra_closed=extra_closed):
        return False, ""
    for symbol in symbols:
        clean = str(symbol or "").strip()
        if not clean or clean == "（还没有）":
            continue
        sessions = sessions_for_symbol(cfg, clean)
        if in_trading_sessions(sessions, moment, grace_sec=grace_sec):
            return True, clean
    return False, ""


def _is_canceled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    try:
        return bool(cancel())
    except Exception:  # noqa: BLE001
        return False


def _emit(progress: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if progress is None:
        return
    try:
        progress(payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("补齐进度回调失败（不影响下载）: {}", exc)


def _task_state(after: BackfillTask, source: str, cap_reached: bool) -> tuple[str, str]:
    """补完一格后给出结论。只有真的还缺东西才继续记成未完成。"""
    if not after.pending:
        return "ok", "已达标"
    if cap_reached:
        return "capped", "已到数据源上限，不再重复请求"
    # K 线本身还缺时先说 K 线，不能被「数据源没有持仓量」盖住。
    if after.need_bars or after.need_fresh:
        problems = []
        if after.need_bars:
            problems.append(f"K线 {after.have_bars}/{after.required_bars}")
        if after.need_fresh:
            problems.append("数据过期")
        return "failed", "；".join(problems)
    if after.need_oi and source != "tq":
        return "unsupported", "当前数据源不提供持仓量，OI 灯保持黄色"
    if after.need_oi:
        return "failed", f"持仓量 {after.oi_ratio:.0%}"
    return "failed", "没有拿到新数据"


def _after_task(
    store: Any,
    task: BackfillTask,
    *,
    mode: str,
    require_oi: bool,
    source: str,
    start: str | None,
    end: str | None,
    now: datetime,
) -> BackfillTask:
    """重新量一遍同一格，生成补完后的任务对象。"""
    refreshed = plan_kline_backfill(
        store,
        [task.symbol],
        [task.period],
        mode=mode,
        require_oi=require_oi,
        source=source,
        start=start,
        end=end,
        now=now,
    )
    if refreshed:
        return refreshed[0]
    return task


def run_kline_backfill(
    cfg: dict[str, Any],
    root_dir: str | Path,
    symbols: Sequence[str] | None = None,
    periods: Sequence[str] | None = None,
    *,
    mode: str | None = None,
    require_oi: bool | None = None,
    source: str | None = None,
    start: str | None = None,
    end: str | None = None,
    pause_when_trading: bool | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel: Any = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
    store: Any = None,
    provider: Any = None,
) -> dict[str, Any]:
    """串行补齐 K 线。返回报告，不抛网络异常；单格失败不影响其它格。"""
    from src.data.bar_store import BarStore

    root = Path(root_dir)
    settings = backfill_settings(cfg)
    chosen_mode = mode_key(mode or settings.get("target_mode"))
    require_oi = bool(settings.get("require_oi", True) if require_oi is None else require_oi)
    source_key = normalize_source(source or ((cfg.get("data") or {}).get("source") or "tq"))
    chosen_symbols = [
        str(item).strip()
        for item in (symbols or ((cfg.get("strategy") or {}).get("symbols") or []))
        if str(item).strip()
    ]
    chosen_periods = []
    for item in periods or settings.get("periods") or DEFAULT_PERIODS:
        code = canonical_period(item)
        if code not in chosen_periods:
            chosen_periods.append(code)
    pause = bool(settings.get("pause_when_trading", True) if pause_when_trading is None else pause_when_trading)
    sleeper = sleep or time.sleep
    moment = now or datetime.now()
    policy = str(settings.get("policy") or "off_hours_only").lower()

    report: dict[str, Any] = {
        "status": "done",
        "mode": chosen_mode,
        "source": source_key,
        "symbols": chosen_symbols,
        "periods": chosen_periods,
        "started_at": moment.isoformat(timespec="seconds"),
        "finished_at": "",
        "policy": policy,
        "paused": False,
        "paused_by": "",
        "canceled": False,
        "total": 0,
        "done": 0,
        "skipped": 0,
        "failed": 0,
        "capped": 0,
        "tasks": [],
        "message": "",
    }
    if not bool(settings.get("enabled", True)):
        report["status"] = "disabled"
        report["message"] = "data.factor_backfill.enabled 为 false，补齐已关闭"
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return report

    if not chosen_symbols:
        report["status"] = "empty"
        report["message"] = "还没有关注品种，无法补齐 K 线"
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return report

    if pause:
        trading, who = in_trading_session(cfg, chosen_symbols, moment)
        if trading:
            report["status"] = "paused"
            report["paused"] = True
            report["paused_by"] = who
            report["message"] = f"{who} 正在交易时段，补齐已暂停；收盘后或取消「仅非交易时段」再试"
            report["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _emit(progress, {**report, "event": "paused"})
            return report

    if store is None:
        cache_dir = Path((cfg.get("data") or {}).get("cache_dir", "data/klines"))
        store = BarStore(root / cache_dir / "market.sqlite")
    tasks = plan_kline_backfill(
        store,
        chosen_symbols,
        chosen_periods,
        mode=chosen_mode,
        require_oi=require_oi,
        source=source_key,
        start=start,
        end=end,
        now=moment,
    )
    report["total"] = len(tasks)
    report["tasks"] = [task.to_dict() for task in tasks]
    manifest_file = manifest_path(root, settings)
    manifest = load_manifest(manifest_file)
    max_attempts = max(1, int(settings.get("max_attempts", DEFAULT_MAX_ATTEMPTS)))
    cap_bars = max(100, int(settings.get("cap_bars", DEFAULT_CAP_BARS)))
    today_text = moment.date().isoformat()

    pending = [task for task in tasks if task.pending]
    if not pending:
        save_manifest(manifest_file, manifest)
        report["status"] = "done"
        report["message"] = "数据已达标，不需要补齐"
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _emit(progress, {**report, "event": "done"})
        return report

    if provider is None:
        # 进程内缓存可能留着上一轮的短表。补齐前清一次，确保真的向数据源要一次。
        from src.data.kline import KlineProvider, clear_fetch_cache

        clear_fetch_cache()
        provider_cfg = dict(cfg)
        provider_data = dict(provider_cfg.get("data") or {})
        provider_data["source"] = source_key
        provider_cfg["data"] = provider_data
        provider = KlineProvider(provider_cfg)

    _emit(
        progress,
        {
            "event": "start",
            "total": len(pending),
            "symbols": chosen_symbols,
            "periods": chosen_periods,
            "message": f"开始补齐 {len(pending)} 个合约周期",
        },
    )

    attempted = 0
    results: list[dict[str, Any]] = []
    for index, task in enumerate(pending, 1):
        if _is_canceled(cancel):
            report["status"] = "canceled"
            report["canceled"] = True
            report["message"] = "已取消补齐；已完成的格子留在库里，下次接着补"
            break

        previous = manifest["tasks"].get(task.key) or {}
        prev_state = str(previous.get("state") or "")
        prev_have = int(previous.get("have_bars") or 0)
        prev_oi = float(previous.get("oi_ratio") or 0.0)
        prev_attempts = int(previous.get("attempts") or 0)
        prev_day = str(previous.get("updated_at") or "")[:10]
        if prev_state == "capped" and task.have_bars == prev_have and task.need_bars:
            results.append({**task.to_dict(), "state": "capped", "note": "已到数据源上限，跳过"})
            report["skipped"] += 1
            report["capped"] += 1
            continue
        if prev_state == "capped" and abs(task.oi_ratio - prev_oi) < 1e-9 and task.need_oi:
            results.append({**task.to_dict(), "state": "capped", "note": "持仓量已到上限，跳过"})
            report["skipped"] += 1
            report["capped"] += 1
            continue
        if (
            prev_state == "unsupported"
            and not task.need_bars
            and not task.need_fresh
            and abs(task.oi_ratio - prev_oi) < 1e-9
            and task.need_oi
        ):
            results.append({**task.to_dict(), "state": "unsupported", "note": "数据源不提供持仓量，跳过"})
            report["skipped"] += 1
            continue
        if prev_state == "failed" and prev_attempts >= max_attempts and prev_day == today_text:
            results.append(
                {
                    **task.to_dict(),
                    "state": "failed",
                    "note": f"今天已失败 {prev_attempts} 次，明天再试",
                }
            )
            report["skipped"] += 1
            report["failed"] += 1
            continue

        attempted += 1
        _emit(
            progress,
            {
                "event": "task",
                "index": index,
                "total": len(pending),
                "symbol": task.symbol,
                "period": task.period,
                "period_label": period_label(task.period),
                "message": f"正在补齐 {task.symbol} {period_label(task.period)}",
            },
        )
        state = "failed"
        note = ""
        cap_reached = False
        try:
            # 不带日期：免费天勤一次给服务端上限，与本地合并后入库，幂等。
            frame = provider.fetch(task.symbol, task.period, keep_history=True)
            returned = 0 if frame is None else int(len(frame))
            cap_reached = returned >= int(cap_bars * CAP_RATIO)
            # 正常 KlineProvider 会自己归档；测试或第三方 provider 不一定写库。
            # 库里根数没变时补一次，保证补齐结果真的落到本地。
            now_cov = store.coverage(task.stored_symbol, task.period, start, _coverage_end(end))
            if frame is not None and len(frame) > 0 and int(now_cov.get("n") or 0) <= task.have_bars:
                store.upsert(task.stored_symbol, task.period, frame)
        except Exception as exc:  # noqa: BLE001
            logger.warning("补齐K线失败 {} {}: {}", task.symbol, task.period, exc)
            note = str(exc)
        after = _after_task(
            store,
            task,
            mode=chosen_mode,
            require_oi=require_oi,
            source=source_key,
            start=start,
            end=end,
            now=datetime.now(),
        )
        if not note:
            state, note = _task_state(after, source_key, cap_reached)
        if state == "ok":
            report["done"] += 1
        elif state == "capped":
            report["capped"] += 1
        elif state == "unsupported":
            report["skipped"] += 1
        else:
            report["failed"] += 1
        attempts = prev_attempts + 1
        result = {
            **after.to_dict(),
            "state": state,
            "note": note,
            "attempts": attempts,
            "cap_reached": bool(cap_reached or after.cap_reached),
        }
        results.append(result)
        manifest["tasks"][task.key] = {
            "symbol": task.symbol,
            "stored_symbol": task.stored_symbol,
            "period": task.period,
            "state": state,
            "note": note,
            "attempts": attempts,
            "have_bars": after.have_bars,
            "required_bars": after.required_bars,
            "oi_ratio": after.oi_ratio,
            "required_oi_ratio": after.required_oi_ratio,
            "last_time": after.last_time,
            "cap_reached": bool(cap_reached),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_manifest(manifest_file, manifest)
        _emit(progress, {**result, "event": "result", "index": index, "total": len(pending)})
        if index < len(pending) and not _is_canceled(cancel):
            sleeper(float(settings.get("chunk_pause_sec", 3.0) or 0))

    report["tasks"] = [task.to_dict() for task in tasks]
    report["results"] = results
    report["attempted"] = attempted
    if report["status"] != "canceled":
        report["status"] = "done"
    if not report["message"]:
        report["message"] = (
            f"补齐完成：成功 {report['done']}，失败 {report['failed']}，"
            f"到上限 {report['capped']}，跳过 {report['skipped']}"
        )
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _emit(progress, {**report, "event": "done"})
    return report


__all__ = [
    "BackfillTask",
    "backfill_settings",
    "in_trading_session",
    "load_manifest",
    "manifest_path",
    "plan_kline_backfill",
    "run_kline_backfill",
    "save_manifest",
]
