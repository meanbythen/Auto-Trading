"""因子挖掘的基础数据口径与绿灯判断。

研究页和数据管理页都从这里取门槛，避免两边各写一套阈值。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from loguru import logger

from src.data.periods import PERIOD_LABELS, canonical_period
from src.data.quote_check import normalize_source, storage_symbol
from src.utils.trading_day import is_trading_day

# 因子预热 60 根 + 最大预测周期 24 根。门槛按「可用根数」算，落库要按原始根数补。
USABLE_DEDUCTION = 84
DEFAULT_PERIODS: tuple[str, ...] = ("5", "15", "30", "60", "D")


@dataclass(frozen=True)
class Requirement:
    key: str
    label: str
    min_bars: int
    recommended_bars: int
    min_span_days: int
    min_contracts: int
    oi_ratio: float
    lock_bars: int


MODE_REQUIREMENTS: dict[str, Requirement] = {
    "quick": Requirement("quick", "快速", 600, 1000, 40, 2, 0.90, 0),
    "standard": Requirement("standard", "标准", 1000, 2000, 60, 2, 0.90, 0),
    "strict": Requirement("strict", "严格", 1500, 3000, 120, 3, 0.95, 200),
    "regime": Requirement("regime", "状态识别", 1200, 2500, 90, 3, 0.90, 150),
}
MODE_LABELS: dict[str, str] = {key: item.label for key, item in MODE_REQUIREMENTS.items()}
MODE_BY_LABEL: dict[str, str] = {item.label: key for key, item in MODE_REQUIREMENTS.items()}

# 补数时往前要多少自然日。真正能拿到多少仍取决于数据源上限。
PERIOD_LOOKBACK_DAYS: dict[str, int] = {
    "1": 60,
    "5": 180,
    "15": 270,
    "30": 365,
    "60": 730,
    "D": 1825,
}
UNSUPPORTED_PERIODS: dict[str, str] = {
    "1": "1分钟约只有25个交易日，跨度不够，不建议做主研究",
}

STATUS_RELATIVE_PATH = "data/factor_mining/data_status.json"

# 具体月份合约：fu2701、SHFE.fu2701、MA605；含 @、主连、连续 的都算连续合约。
_CONTRACT_RE = re.compile(r"^(?:[A-Za-z]{2,6}\.)?[A-Za-z]{1,3}\d{3,4}$")


def is_specific_contract(symbol: str) -> bool:
    """持仓量/成交量因子只在具体合约上算，主力连续换月会跳变。"""
    text = str(symbol or "").strip()
    if not text or "@" in text or "主" in text or "连续" in text:
        return False
    return bool(_CONTRACT_RE.match(text))


def mode_key(value: str | None) -> str:
    """界面可能传中文名，统一成内部键。"""
    text = str(value or "").strip()
    if text in MODE_REQUIREMENTS:
        return text
    return MODE_BY_LABEL.get(text, "standard")


def requirement_for(mode: str | None) -> Requirement:
    return MODE_REQUIREMENTS[mode_key(mode)]


def period_label(period: str) -> str:
    return PERIOD_LABELS.get(str(period), str(period))


def required_raw_bars(mode: str | None) -> int:
    """落库至少要有多少根，才能留下要求的可用根数。"""
    return requirement_for(mode).min_bars + USABLE_DEDUCTION


def lookback_days(period: str) -> int:
    return PERIOD_LOOKBACK_DAYS.get(canonical_period(period), 180)


def backfill_window(period: str, end: str | None = None) -> tuple[str, str]:
    """补数请求区间：结束日（含）往前推该周期的观察天数。"""
    end_day = date.fromisoformat(str(end)[:10]) if end else date.today()
    start_day = end_day - timedelta(days=lookback_days(period))
    return start_day.isoformat(), end_day.isoformat()


def _parse_ts(text: str | None) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return None


def trading_days_since(moment: datetime | None, now: datetime | None = None) -> int:
    """从 moment 那天之后到今天，隔了几个交易日。用于判断数据新不新。"""
    if moment is None:
        return 10**6
    today = (now or datetime.now()).date()
    day = moment.date()
    if day >= today:
        return 0
    count = 0
    cursor = day + timedelta(days=1)
    while cursor <= today:
        if is_trading_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


def _end_bound(end: str | None) -> str | None:
    """BarStore 的 end 是开区间；传日期时补到当天末尾。"""
    if not end:
        return None
    text = str(end).strip()
    if len(text) <= 10:
        return f"{text[:10]} 23:59:59"
    return text


@dataclass
class ReadinessRow:
    symbol: str
    stored_symbol: str
    period: str
    bars: int
    usable_bars: int
    required_bars: int
    oi_bars: int
    oi_ratio: float
    first_time: str
    last_time: str
    span_days: float
    meets_bars: bool
    meets_span: bool
    meets_oi: bool
    meets_lock: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_for(
    store: Any,
    symbol: str,
    period: str,
    req: Requirement,
    *,
    source: str,
    start: str | None,
    end: str | None,
    require_oi: bool,
) -> ReadinessRow:
    stored = storage_symbol(symbol, source)
    cov = store.coverage(stored, period, start, _end_bound(end))
    bars = int(cov.get("n") or 0)
    usable = max(0, bars - USABLE_DEDUCTION)
    first = _parse_ts(cov.get("ts_from"))
    last = _parse_ts(cov.get("ts_to"))
    span = (last - first).total_seconds() / 86400.0 if first and last else 0.0
    oi_ratio = float(cov.get("oi_ratio") or 0.0)
    meets_oi = (not require_oi) or oi_ratio >= req.oi_ratio
    lock_need = req.lock_bars
    meets_lock = (lock_need <= 0) or (usable * 0.15 >= lock_need)
    return ReadinessRow(
        symbol=symbol,
        stored_symbol=stored,
        period=period,
        bars=bars,
        usable_bars=usable,
        required_bars=req.min_bars,
        oi_bars=int(cov.get("oi_n") or 0),
        oi_ratio=oi_ratio,
        first_time=str(cov.get("ts_from") or ""),
        last_time=str(cov.get("ts_to") or ""),
        span_days=round(span, 2),
        meets_bars=usable >= req.min_bars,
        meets_span=span >= req.min_span_days,
        meets_oi=meets_oi,
        meets_lock=meets_lock,
    )


def evaluate_readiness(
    store: Any,
    symbols: Sequence[str],
    periods: Sequence[str] | None = None,
    *,
    mode: str = "standard",
    require_oi: bool = True,
    require_cross: bool = True,
    start: str | None = None,
    end: str | None = None,
    source: str = "tq",
    now: datetime | None = None,
) -> dict[str, Any]:
    """按本地库存判断因子挖掘能不能启动。只读，不下载。"""
    req = requirement_for(mode)
    chosen_periods = []
    for item in periods or DEFAULT_PERIODS:
        code = canonical_period(item)
        if code not in chosen_periods:
            chosen_periods.append(code)
    chosen_symbols = [s for s in symbols if s and s != "（还没有）"]
    source_key = normalize_source(source)
    rows = [
        _row_for(
            store,
            symbol,
            period,
            req,
            source=source_key,
            start=start,
            end=end,
            require_oi=require_oi,
        )
        for symbol in chosen_symbols
        for period in chosen_periods
    ]

    lights: dict[str, dict[str, str]] = {}
    missing: list[str] = []

    bad_bars = [r for r in rows if not r.meets_bars]
    bad_span = [r for r in rows if r.meets_bars and not r.meets_span]
    if not rows:
        lights["kline"] = {"status": "red", "label": "K线", "detail": "还没有选择品种或周期"}
    elif bad_bars:
        worst = min(bad_bars, key=lambda r: r.usable_bars / max(1, r.required_bars))
        lights["kline"] = {
            "status": "red",
            "label": "K线",
            "detail": f"{worst.symbol} {period_label(worst.period)} {worst.usable_bars}/{worst.required_bars} 根",
        }
        for row in bad_bars:
            missing.append(
                f"{row.symbol} {period_label(row.period)} 只有 {row.usable_bars} 根可用（需要 {row.required_bars}）"
            )
    elif bad_span:
        worst = min(bad_span, key=lambda r: r.span_days)
        lights["kline"] = {
            "status": "red",
            "label": "K线",
            "detail": f"{worst.symbol} {period_label(worst.period)} 跨度 {worst.span_days:.0f}/{req.min_span_days} 天",
        }
        for row in bad_span:
            missing.append(
                f"{row.symbol} {period_label(row.period)} 只有 {row.span_days:.0f} 天跨度（需要 {req.min_span_days} 天）"
            )
    else:
        least = min(rows, key=lambda r: r.usable_bars)
        lights["kline"] = {
            "status": "green",
            "label": "K线",
            "detail": f"最少 {least.usable_bars}/{least.required_bars} 根",
        }

    if not require_oi:
        lights["oi"] = {"status": "off", "label": "持仓量", "detail": "本次不研究持仓量"}
    else:
        continuous = [s for s in chosen_symbols if not is_specific_contract(s)]
        if continuous:
            # 主力连续在换月点持仓量会跳变，OI 因子只能看具体合约。
            lights["oi"] = {
                "status": "yellow",
                "label": "持仓量",
                "detail": f"{continuous[0]} 不是具体合约",
            }
            missing.append("持仓量/成交量因子只支持具体合约；主力连续请换成具体月份合约")
        else:
            bad_oi = [r for r in rows if not r.meets_oi]
            if bad_oi:
                worst = min(bad_oi, key=lambda r: r.oi_ratio)
                lights["oi"] = {
                    "status": "yellow",
                    "label": "持仓量",
                    "detail": f"{worst.symbol} {period_label(worst.period)} {worst.oi_ratio:.0%}（需要 {req.oi_ratio:.0%}）",
                }
                missing.append(
                    f"{worst.symbol} {period_label(worst.period)} 持仓量覆盖 {worst.oi_ratio:.0%}（需要 {req.oi_ratio:.0%}）"
                )
            else:
                lights["oi"] = {"status": "green", "label": "持仓量", "detail": "覆盖达标"}

    symbol_ok: dict[str, bool] = {}
    for symbol in chosen_symbols:
        part = [r for r in rows if r.symbol == symbol]
        symbol_ok[symbol] = bool(part) and all(r.meets_bars and r.meets_span for r in part)
    ready_symbols = [s for s, ok in symbol_ok.items() if ok]
    if not require_cross:
        lights["cross"] = {"status": "off", "label": "跨品种", "detail": "本次只扫一个品种"}
    elif len(ready_symbols) >= req.min_contracts:
        lights["cross"] = {
            "status": "green",
            "label": "跨品种",
            "detail": f"{len(ready_symbols)} 个合约达标",
        }
    else:
        lights["cross"] = {
            "status": "red",
            "label": "跨品种",
            "detail": f"{len(ready_symbols)}/{req.min_contracts} 个合约达标",
        }
        missing.append(f"至少需要 {req.min_contracts} 个合约的数据达标")

    last_times = [t for t in (_parse_ts(r.last_time) for r in rows) if t]
    if not last_times:
        lights["fresh"] = {"status": "red", "label": "新鲜度", "detail": "没有可用的K线"}
    else:
        last = max(last_times)
        only_daily = all(p == "D" for p in chosen_periods)
        if only_daily:
            # 日线按自然日算：周末、节假日不会把上周五的日线判成过期。
            gap_days = max(0, ((now or datetime.now()).date() - last.date()).days)
            fresh_ok = gap_days <= 3
            age_text = f"{gap_days} 个自然日"
        else:
            gap_days = trading_days_since(last, now)
            fresh_ok = gap_days <= 1
            age_text = f"{gap_days} 个交易日"
        lights["fresh"] = {
            "status": "green" if fresh_ok else "red",
            "label": "新鲜度",
            "detail": f"最后一根 {last.strftime('%Y-%m-%d')}",
        }
        if not fresh_ok:
            missing.append(f"最后一根K线是 {last.strftime('%Y-%m-%d')}，已过去 {age_text}，数据过期")

    if req.lock_bars <= 0:
        lights["lock"] = {"status": "off", "label": "样本外", "detail": "当前模式不要求 Lock 集"}
    else:
        bad_lock = [r for r in rows if not r.meets_lock]
        if bad_lock:
            worst = min(bad_lock, key=lambda r: r.usable_bars)
            lights["lock"] = {
                "status": "red",
                "label": "样本外",
                "detail": f"可用 {worst.usable_bars} 根，留不出 {req.lock_bars} 根 Lock",
            }
            missing.append(f"样本外至少需要 {req.lock_bars} 根 Lock 数据")
        else:
            lights["lock"] = {"status": "green", "label": "样本外", "detail": f"Lock ≥{req.lock_bars} 根"}

    mandatory = ("kline", "cross", "fresh", "lock")
    if any(lights[key]["status"] == "red" for key in mandatory):
        overall = "red"
    elif lights["oi"]["status"] == "yellow":
        overall = "yellow"
    else:
        overall = "green"

    if overall == "green":
        summary = f"绿灯：{req.label}模式数据已满足，可以开始因子挖掘"
    elif overall == "yellow":
        summary = "黄灯：价量数据够，持仓量覆盖不足；可以先跑价量，OI 组会关闭"
    else:
        summary = "红灯：数据还不够启动；请先补齐 K 线或调整研究范围"

    return {
        "mode": req.key,
        "mode_label": req.label,
        "checked_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "source": source_key,
        "symbols": chosen_symbols,
        "periods": chosen_periods,
        "require_oi": bool(require_oi),
        "require_cross": bool(require_cross),
        "overall": overall,
        "summary": summary,
        "lights": lights,
        "rows": [row.to_dict() for row in rows],
        "missing": missing,
    }


def status_path(root_dir: str | Path) -> Path:
    return Path(root_dir) / STATUS_RELATIVE_PATH


def save_status(root_dir: str | Path, report: dict[str, Any]) -> Path:
    path = status_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_status(root_dir: str | Path) -> dict[str, Any]:
    path = status_path(root_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def format_light_line(report: dict[str, Any]) -> str:
    """给界面/日志用的一行摘要。"""
    lights = report.get("lights") or {}
    order = ("kline", "oi", "cross", "fresh", "lock")
    parts = []
    for key in order:
        item = lights.get(key) or {}
        mark = {"green": "绿", "yellow": "黄", "red": "红", "off": "—"}.get(
            str(item.get("status") or ""), "?"
        )
        parts.append(f"{item.get('label', key)}={mark}")
    return " ".join(parts)


def log_report(report: dict[str, Any]) -> None:
    logger.info(
        "因子数据检查 {} {} {}",
        report.get("overall"),
        format_light_line(report),
        report.get("summary", ""),
    )


def iter_unsupported(periods: Iterable[str]) -> list[str]:
    return [UNSUPPORTED_PERIODS[p] for p in periods if p in UNSUPPORTED_PERIODS]
