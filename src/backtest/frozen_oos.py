"""
最终样本外冻结区间。

功能：把所选行情最后一段（默认半年）留着只验收，网格搜索碰不到；看过一次就不能再当验收。
原理：先看答案再填空，就是过拟合。这段只能跑一次。再调参，必须等后面长出还没看过的新行情。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.paths import project_root

DEFAULT_FREEZE_DAYS = 180
MIN_FREEZE_DAYS = 30
MAX_FREEZE_DAYS = 730
# 冻结段至少要有这么多根，太短不能当「最终半年」。
MIN_FROZEN_BARS = 30
# 冻结后前面留给挑参数的，至少还能切开训练/检验。
MIN_SEARCHABLE_BARS = 80


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "是", "开", "on"}:
        return True
    if text in {"0", "false", "no", "n", "否", "关", "off"}:
        return False
    return default


def clamp_freeze_days(days: Any) -> int:
    """冻结天数收成 30～730。空着或乱填用 180。"""
    try:
        value = int(float(days)) if days is not None and str(days).strip() != "" else DEFAULT_FREEZE_DAYS
    except (TypeError, ValueError):
        value = DEFAULT_FREEZE_DAYS
    return min(MAX_FREEZE_DAYS, max(MIN_FREEZE_DAYS, value))


def freeze_enabled_from_cfg(cfg: dict[str, Any] | None) -> bool:
    """配置缺这项时默认打开，避免一打开优化就把最后半年拿去搜参数。"""
    box = (cfg or {}).get("backtest")
    if not isinstance(box, dict):
        return True
    return _flag(box.get("freeze_final_oos"), True)


def freeze_days_from_cfg(cfg: dict[str, Any] | None) -> int:
    box = (cfg or {}).get("backtest")
    raw = box.get("freeze_final_days") if isinstance(box, dict) else None
    return clamp_freeze_days(raw)


def _naive_ts(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        return stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    return stamp


def freeze_start_from_bars(bars: pd.DataFrame, days: int) -> pd.Timestamp:
    """从这段行情最后一根往前推 N 个日历天。"""
    last = _naive_ts(bars.index[-1])
    return last - pd.Timedelta(days=int(days))


def split_searchable_frozen(
    bars: pd.DataFrame | None,
    freeze_start: pd.Timestamp,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """freeze_start 之前用来挑参数，当天及以后只验收。"""
    if bars is None or len(bars) == 0:
        return bars, bars
    start = _naive_ts(freeze_start)
    mask = pd.Series([_naive_ts(idx) < start for idx in bars.index], index=bars.index)
    return bars.loc[mask], bars.loc[~mask]


def describe_freeze_shortage(searchable_n: int, frozen_n: int, days: int) -> str:
    """K 线不够冻结时给人看的原因。"""
    if searchable_n < MIN_SEARCHABLE_BARS:
        return (
            f"冻结最后 {days} 天后，前面只剩 {searchable_n} 根，不够用来挑参数"
            f"（至少 {MIN_SEARCHABLE_BARS} 根）。请把开始日期再往前，"
            "或把冻结天数改小。改小后最终验收变短，防过拟合变弱。"
        )
    if frozen_n < MIN_FROZEN_BARS:
        return (
            f"最后 {days} 天只有 {frozen_n} 根 K 线，不够当最终验收"
            f"（至少 {MIN_FROZEN_BARS} 根）。请把结束日期放到有行情的日子，"
            "或换更细的周期、更长的本地库存。"
        )
    return ""


def attempt_key(
    *,
    engine: str,
    symbol: str,
    period: str,
    freeze_start: str,
    freeze_end: str,
) -> str:
    """同一策略、合约、周期、同一段最终半年，算看过一次。"""
    return "|".join(
        [
            str(engine or "").strip().lower(),
            str(symbol or "").strip().lower(),
            str(period or "").strip().lower(),
            str(freeze_start or "")[:10],
            str(freeze_end or "")[:10],
        ]
    )


def registry_path(cfg: dict[str, Any] | None = None) -> Path:
    box = (cfg or {}).get("backtest") if isinstance(cfg, dict) else None
    raw = ""
    if isinstance(box, dict):
        raw = str(box.get("freeze_registry_path") or "").strip()
    if raw:
        return Path(raw)
    return project_root() / "data" / "frozen_oos_registry.json"


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"attempts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"attempts": {}}
    attempts = data.get("attempts") if isinstance(data, dict) else None
    if not isinstance(attempts, dict):
        return {"attempts": {}}
    return {"attempts": attempts}


def has_seen_freeze(path: Path, key: str) -> bool:
    box = load_registry(path)
    item = (box.get("attempts") or {}).get(key)
    return isinstance(item, dict) and int(item.get("runs") or 0) > 0


def record_freeze_attempt(
    path: Path,
    key: str,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """看过最终半年（无论过关还是没过）都记一笔，下次不能再当验收。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    box = load_registry(path)
    attempts = box.setdefault("attempts", {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prev = attempts.get(key) if isinstance(attempts.get(key), dict) else {}
    runs = int(prev.get("runs") or 0) + 1
    row = {
        "first_seen": prev.get("first_seen") or now,
        "last_seen": now,
        "runs": runs,
    }
    if extra:
        row.update(extra)
    attempts[key] = row
    path.write_text(json.dumps(box, ensure_ascii=False, indent=2), encoding="utf-8")


def format_freeze_lines(report: dict[str, Any] | None) -> list[str]:
    """策略优化结果框里，最终半年这一段怎么写。"""
    box = dict(report or {})
    lines = ["—— 最终半年（只验收，不调参）——"]
    if not box.get("enabled"):
        lines.append("这次没开冻结。最后半年也参与了挑参数，防过拟合变弱。")
        return lines
    lines.append(
        f"冻结 {int(box.get('days') or 0)} 天："
        f"{box.get('start') or '?'} 起，共 {int(box.get('bars') or 0)} 根，不参与挑参数"
    )
    if box.get("already_seen"):
        lines.append("这段最终半年已经看过，不再跑成绩，免得根据数字再调参。")
        return lines
    if box.get("skip_reason"):
        lines.append(str(box.get("skip_reason")))
        return lines
    perf = box.get("performance") if isinstance(box.get("performance"), dict) else {}
    lines.append(
        f"最终半年  成交{int(perf.get('trades') or 0)}笔 "
        f"净盈亏{float(perf.get('net_pnl') or 0):.0f} "
        f"盈利因子{float(perf.get('profit_factor') or 0):.2f} "
        f"最大回撤{float(perf.get('max_drawdown') or 0):.0f}"
    )
    reason = str(box.get("reason") or "")
    if reason:
        lines.append(reason)
    return lines
