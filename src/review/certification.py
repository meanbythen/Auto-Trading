"""合约参数体检与再认证标记。

功能：检查关注合约的保证金比例、每手保证金、乘数、手续费是否缺失或异常；
      把策略、风控、合约参数收成指纹，和上次认证结果比对，判断是否需要重新认证。
原理：只读配置与品种总表，写一个 JSON 快照；不改配置、不碰交易链路。
      保证金比例和每手保证金都为空时，组合回测会按 10% 估算；乘数缺失按默认值，
      这两类填错会直接让手数和保证金占用算偏，所以体检放在组合与实盘之前。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.risk.universe_table import product_key
from src.utils.paths import project_root

SNAPSHOT_REL = "data/review/certification.json"
ERROR = "error"
WARN = "warn"

# 参数不同但仍算「同一套配置」的键不参与指纹：符号列表不算规则，改关注品种不必重新认证。
TRACKED_RISK_KEYS = (
    "sizing_mode",
    "capital",
    "risk_per_trade_pct",
    "margin_usage_pct",
    "max_daily_loss",
    "min_volume",
)
TRACKED_CONTRACT_KEYS = (
    "margin_rate_pct",
    "margin",
    "multiplier",
    "commission",
    "commission_close",
    "commission_close_today",
    "price_tick",
)


def _box(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def watch_symbols(cfg: dict[str, Any] | None) -> list[str]:
    st = _box((cfg or {}).get("strategy"))
    return [str(item).strip() for item in (st.get("symbols") or []) if str(item).strip()]


def check_contracts(cfg: dict[str, Any] | None) -> list[dict[str, str]]:
    """逐关注合约查参数。返回问题列表，每条带 symbol / field / severity / message。"""
    cfg = cfg or {}
    contracts = _box(_box(cfg.get("risk")).get("contracts"))
    issues: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(symbol: str, field: str, severity: str, message: str) -> None:
        issues.append({"symbol": symbol, "field": field, "severity": severity, "message": message})

    for symbol in watch_symbols(cfg):
        code = product_key(symbol)
        if not code or code in seen:
            continue
        seen.add(code)
        box = _box(contracts.get(code))
        if not box:
            _add(symbol, "contracts", ERROR, "总表里没有这个品种，回测会按 10% 保证金估算")
            continue
        rate = _number(box.get("margin_rate_pct"))
        lot = _number(box.get("margin"))
        if rate <= 0 and lot <= 0:
            _add(symbol, "margin", ERROR, "保证金比例和每手保证金都没填，按 10% 估算")
        if _number(box.get("multiplier")) <= 0:
            _add(symbol, "multiplier", ERROR, "每手数量没填，乘数会按默认值算")
        if _number(box.get("commission")) <= 0:
            _add(symbol, "commission", WARN, "开仓手续费为 0，可能是没填")
        if _number(box.get("price_tick")) <= 0:
            _add(symbol, "price_tick", WARN, "每跳价格没填，会用全局 price_tick")
    return issues


def certification_snapshot(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """收成一份可比较的快照：策略、风控、关注合约参数。"""
    cfg = cfg or {}
    st = _box(cfg.get("strategy"))
    risk = _box(cfg.get("risk"))
    contracts = _box(risk.get("contracts"))
    legs = []
    for item in st.get("legs") or []:
        box = _box(item)
        legs.append({key: str(box.get(key) or "") for key in ("symbol", "engine", "timeframe", "trail_timeframe")})
    params: dict[str, dict[str, Any]] = {}
    for symbol in watch_symbols(cfg):
        box = _box(contracts.get(product_key(symbol)))
        params[symbol] = {key: _number(box.get(key)) for key in TRACKED_CONTRACT_KEYS}
    return {
        "strategy": {
            "engine": str(st.get("engine") or ""),
            "timeframe": str(st.get("timeframe") or ""),
            "trail_timeframe": str(st.get("trail_timeframe") or ""),
            "legs": legs,
        },
        "risk": {key: risk.get(key) for key in TRACKED_RISK_KEYS},
        "contracts": params,
    }


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _flatten(value: Any, prefix: str, out: list[tuple[str, Any]]) -> None:
    if isinstance(value, dict):
        for key in sorted(value):
            _flatten(value[key], f"{prefix}.{key}" if prefix else str(key), out)
    else:
        out.append((prefix, value))


def diff_snapshot(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """逐项列出变化的字段，最多返回 20 条，避免一次改很多时刷屏。"""
    if not old:
        return []
    left: list[tuple[str, Any]] = []
    right: list[tuple[str, Any]] = []
    _flatten(old, "", left)
    _flatten(new, "", right)
    old_map = dict(left)
    new_map = dict(right)
    changes = [
        f"{key}: {old_map.get(key)!r} → {new_map.get(key)!r}"
        for key in sorted(set(old_map) | set(new_map))
        if old_map.get(key) != new_map.get(key)
    ]
    if len(changes) > 20:
        return changes[:20] + [f"……另有 {len(changes) - 20} 项变化"]
    return changes


def _snapshot_path(cfg: dict[str, Any], root: Path | str | None) -> Path:
    base = Path(root) if root else Path(str(cfg.get("_project_root") or project_root()))
    return base / SNAPSHOT_REL


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("认证快照读取失败，将重新生成：{}", exc)
        return {}
    return data if isinstance(data, dict) else {}


def run_certification(
    cfg: dict[str, Any] | None,
    *,
    root: Path | str | None = None,
    write: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """体检 + 和上次快照比对。只读配置，除了写快照文件不动任何东西。"""
    cfg = cfg or {}
    snapshot = certification_snapshot(cfg)
    fingerprint = snapshot_fingerprint(snapshot)
    issues = check_contracts(cfg)
    path = _snapshot_path(cfg, root)
    previous = _read_json(path)
    changes = diff_snapshot(previous.get("snapshot"), snapshot)
    if not previous:
        status = "首次认证"
    elif previous.get("fingerprint") == fingerprint:
        status = "未变化"
    else:
        status = "需要再认证"
    report = {
        "fingerprint": fingerprint,
        "checked_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "changes": changes,
        "issues": issues,
        "errors": sum(1 for item in issues if item["severity"] == ERROR),
        "warnings": sum(1 for item in issues if item["severity"] == WARN),
        "path": str(path),
    }
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": fingerprint,
            "checked_at": report["checked_at"],
            "issues": issues,
            "snapshot": snapshot,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def format_certification_lines(report: dict[str, Any]) -> list[str]:
    """把体检结果写成几行中文，命令行和界面共用。"""
    errors = int(report.get("errors") or 0)
    warnings = int(report.get("warnings") or 0)
    lines = [
        f"再认证    {report.get('status') or '未认证'} · 检查时间 {report.get('checked_at') or '—'} · "
        f"指纹 {report.get('fingerprint') or '—'}"
    ]
    changes = list(report.get("changes") or [])
    if changes:
        lines.append("变化      " + "；".join(changes))
    issues = list(report.get("issues") or [])
    if issues:
        label = {"error": "错误", "warn": "注意"}
        lines.append(
            "参数体检  "
            + "；".join(f"{item.get('symbol')} {label.get(item.get('severity'), item.get('severity'))}：{item.get('message')}" for item in issues)
        )
    else:
        lines.append("参数体检  关注合约的保证金、乘数、手续费都已填写")
    lines.append(f"结论      {'有错误，先补参数' if errors else ('有注意事项' if warnings else '可以进入下一步')}")
    return lines
