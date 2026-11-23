"""
因子挖掘候选池：扫完的说明书落在 data/因子挖掘候选池.json。

勾选只改 selected 字段。策略引擎不会读这个文件，所以勾了也不会下单。
旧文件 data/规律候选池.json 只读迁移，不删不改。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.research.scan import Regularity

POOL_NAME = "因子挖掘候选池.json"
LEGACY_POOL_NAME = "规律候选池.json"
MODULE_NAME = "factor_mining"
SCHEMA_VERSION = 2
DISCLAIMER = (
    "这里是统计研究结论，不是买卖点。"
    "挖掘层不下单。只有你勾选、并且另外写成完整开平仓规则之后，才能进策略。"
)


def pool_path(root: Path) -> Path:
    """候选池文件路径（新文件）。"""
    return Path(root) / "data" / POOL_NAME


def legacy_pool_path(root: Path) -> Path:
    """旧版候选池路径，只读，不写入。"""
    return Path(root) / "data" / LEGACY_POOL_NAME


def _empty_pool() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "module": MODULE_NAME,
        "updated_at": "",
        "disclaimer": DISCLAIMER,
        "items": [],
        "regimes": [],
    }


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """旧条目补默认字段，id 和 selected 原样保留。"""
    row = dict(item)
    row.setdefault("group", "")
    row.setdefault("verdict", "candidate")
    row.setdefault("selected", False)
    return row


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return raw if isinstance(raw, dict) else None


def load_pool(root: Path) -> dict[str, Any]:
    """先读新池；没有时读旧池做只读迁移。文件坏了就给空池，不报错中断界面。"""
    path = pool_path(root)
    raw = _read_json(path) if path.exists() else None
    if raw is None:
        legacy = legacy_pool_path(root)
        raw = _read_json(legacy) if legacy.exists() else None
    if raw is None:
        return _empty_pool()
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    clean = [
        _normalize_item(item)
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]
    regimes = raw.get("regimes") if isinstance(raw.get("regimes"), list) else []
    extra: dict[str, Any] = {}
    advice = raw.get("style_advice")
    if isinstance(advice, dict):
        extra["style_advice"] = advice
    return {
        "schema_version": int(raw.get("schema_version") or SCHEMA_VERSION),
        "module": str(raw.get("module") or MODULE_NAME),
        "updated_at": str(raw.get("updated_at") or ""),
        "disclaimer": str(raw.get("disclaimer") or DISCLAIMER),
        "items": clean,
        "regimes": [row for row in regimes if isinstance(row, dict)],
        **extra,
    }


def save_pool(
    root: Path,
    items: list[Regularity] | list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> Path:
    """整份覆盖写入新文件。会带上「不是买卖点」的声明。旧文件不删不改。"""
    path = pool_path(root)
    first_write = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Regularity):
            rows.append(item.to_dict())
        elif isinstance(item, dict) and item.get("id"):
            rows.append(dict(item))
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "module": MODULE_NAME,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": DISCLAIMER,
        "items": rows,
        "regimes": [],
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if first_write:
        _log_legacy_migration(root, rows)
    return path


def _log_legacy_migration(root: Path, rows: list[dict[str, Any]]) -> None:
    """第一次写新池时，记录旧池里带过来的勾选条数。"""
    legacy = legacy_pool_path(root)
    if not legacy.exists():
        return
    raw = _read_json(legacy)
    if raw is None:
        return
    old_items = raw.get("items") if isinstance(raw.get("items"), list) else []
    old_selected = {
        str(item.get("id"))
        for item in old_items
        if isinstance(item, dict) and item.get("id") and item.get("selected")
    }
    new_selected = {str(row.get("id")) for row in rows if row.get("selected")}
    carried = len(old_selected & new_selected)
    logger.info("已从旧候选池迁移 {} 条勾选到 {}", carried, POOL_NAME)


def mark_selected(root: Path, selected_ids: set[str]) -> dict[str, Any]:
    """只改勾选，不重跑扫描。"""
    pool = load_pool(root)
    for item in pool["items"]:
        item["selected"] = str(item.get("id")) in selected_ids
    extra = {key: pool[key] for key in ("style_advice",) if key in pool}
    # 用 dict 写回，保留说明书原文和类型倾向。
    save_pool(root, pool["items"], extra=extra or None)
    return load_pool(root)
