"""
规律候选池：扫完的说明书落在 data/规律候选池.json。

勾选只改 selected 字段。策略引擎不会读这个文件，所以勾了也不会下单。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.research.scan import Regularity

POOL_NAME = "规律候选池.json"
DISCLAIMER = (
    "这里是统计规律说明书，不是买卖点。"
    "扫描层不下单。只有你勾选、并且另外写成完整开平仓规则之后，才能进策略。"
)


def pool_path(root: Path) -> Path:
    """候选池文件路径。"""
    return Path(root) / "data" / POOL_NAME


def load_pool(root: Path) -> dict[str, Any]:
    """读出候选池。文件不存在或坏了就给空池，不报错中断界面。"""
    path = pool_path(root)
    if not path.exists():
        return {"updated_at": "", "disclaimer": DISCLAIMER, "items": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"updated_at": "", "disclaimer": DISCLAIMER, "items": []}
    if not isinstance(raw, dict):
        return {"updated_at": "", "disclaimer": DISCLAIMER, "items": []}
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    clean = [item for item in items if isinstance(item, dict) and item.get("id")]
    extra: dict[str, Any] = {}
    advice = raw.get("style_advice")
    if isinstance(advice, dict):
        extra["style_advice"] = advice
    return {
        "updated_at": str(raw.get("updated_at") or ""),
        "disclaimer": str(raw.get("disclaimer") or DISCLAIMER),
        "items": clean,
        **extra,
    }


def save_pool(
    root: Path,
    items: list[Regularity] | list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> Path:
    """整份覆盖写入。会带上「不是买卖点」的声明。"""
    path = pool_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Regularity):
            rows.append(item.to_dict())
        elif isinstance(item, dict) and item.get("id"):
            rows.append(dict(item))
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": DISCLAIMER,
        "items": rows,
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def mark_selected(root: Path, selected_ids: set[str]) -> dict[str, Any]:
    """只改勾选，不重跑扫描。"""
    pool = load_pool(root)
    for item in pool["items"]:
        item["selected"] = str(item.get("id")) in selected_ids
    extra = {key: pool[key] for key in ("style_advice",) if key in pool}
    # 用 dict 写回，保留说明书原文和类型倾向。
    save_pool(root, pool["items"], extra=extra or None)
    return load_pool(root)
