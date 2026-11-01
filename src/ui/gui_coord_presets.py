"""
同花顺下单坐标存档。

功能：家里外接显示器和笔记本自带屏幕的点击位置不一样，各留一份。
可以改名、选用其中一份当当前坐标，也可以删掉不要的。
原理：只保存价格框、手数框和买卖按钮这些点。模拟开关、成交假设仍留在主配置里。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# 和标定窗口里的项目一致。别的 trade.gui 开关不进这份存档。
COORD_KEYS = (
    "price_input",
    "volume_input",
    "buy_open_btn",
    "sell_open_btn",
    "close_btn",
    "confirm_btn",
    "symbol_input",
)
FILE_NAME = "gui_coord_presets.json"
MAX_KEEP = 30


def presets_path(root: Path | str) -> Path:
    return Path(root) / "data" / FILE_NAME


def extract_coords(gui: dict[str, Any] | None) -> dict[str, list[int]]:
    """从 trade.gui 里只取出已经标定的点。不是两个数字的项丢掉。"""
    found: dict[str, list[int]] = {}
    for key in COORD_KEYS:
        raw = (gui or {}).get(key)
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            found[key] = [int(raw[0]), int(raw[1])]
        except (TypeError, ValueError):
            continue
    return found


def load_coord_presets(root: Path | str) -> list[dict[str, Any]]:
    path = presets_path(root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取坐标存档失败: {}", exc)
        return []
    items = raw.get("presets") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def save_coord_presets(root: Path | str, items: list[dict[str, Any]]) -> Path:
    path = presets_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"presets": items[:MAX_KEEP]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def add_coord_preset(
    root: Path | str,
    gui: dict[str, Any] | None,
    *,
    name: str = "",
    when: datetime | None = None,
) -> dict[str, Any]:
    """把当前屏幕上的点存成一份。没有价格框或买卖按钮时拒绝，避免存下一套空坐标。"""
    coords = extract_coords(gui)
    missing = [key for key in ("price_input", "buy_open_btn", "sell_open_btn", "close_btn") if key not in coords]
    if missing:
        raise ValueError("当前坐标还不完整，请先标定价格框、买开、卖开和平仓")
    now = when or datetime.now()
    items = load_coord_presets(root)
    taken = {str(item.get("name") or "") for item in items}
    label = str(name or "").strip() or f"下单坐标-{now.strftime('%Y-%m-%d %H:%M:%S')}"
    if label in taken:
        extra = 2
        candidate = f"{label}-{extra}"
        while candidate in taken:
            extra += 1
            candidate = f"{label}-{extra}"
        label = candidate
    vid = now.strftime("%Y%m%d_%H%M%S")
    taken_ids = {str(item.get("id") or "") for item in items}
    if vid in taken_ids:
        vid = f"{vid}_{now.strftime('%f')[:3]}"
    entry = {
        "id": vid,
        "name": label,
        "time": now.isoformat(timespec="seconds"),
        "coords": coords,
    }
    items.insert(0, entry)
    save_coord_presets(root, items)
    logger.info("已保存下单坐标 {}", label)
    return entry


def get_coord_preset(root: Path | str, preset_id: str) -> dict[str, Any] | None:
    for item in load_coord_presets(root):
        if str(item.get("id") or "") == str(preset_id):
            return item
    return None


def rename_coord_preset(root: Path | str, preset_id: str, name: str) -> str:
    """只改显示名字，不改坐标，也不改正在使用的配置。"""
    label = str(name or "").strip()
    if not label:
        raise ValueError("名字不能是空的")
    pid = str(preset_id or "").strip()
    items = load_coord_presets(root)
    found = False
    for item in items:
        if str(item.get("id") or "") == pid:
            item["name"] = label
            found = True
            break
    if not found:
        raise ValueError("找不到这份坐标")
    save_coord_presets(root, items)
    logger.info("下单坐标已改名 {}", label)
    return label


def delete_coord_preset(root: Path | str, preset_id: str) -> str:
    """删掉一份坐标备份。不改当前正在用的下单坐标。"""
    pid = str(preset_id or "").strip()
    if not pid:
        raise ValueError("请先选一份坐标")
    items = load_coord_presets(root)
    kept: list[dict[str, Any]] = []
    removed = ""
    for item in items:
        if str(item.get("id") or "") == pid and not removed:
            removed = str(item.get("name") or pid)
            continue
        kept.append(item)
    if not removed:
        raise ValueError("找不到这份坐标")
    save_coord_presets(root, kept)
    logger.info("已删除下单坐标 {}", removed)
    return removed


def apply_coords_to_config(cfg: dict[str, Any], coords: dict[str, Any]) -> dict[str, Any]:
    """
    把选中的点写进配置里的 trade.gui。

    只替换标定过的那些点。确认方式、是否假定成交这些开关保持原样。
    这一份里没有的点会从当前配置拿掉，避免家里的坐标还留在笔记本上。
    """
    import copy

    out = copy.deepcopy(cfg)
    gui = out.setdefault("trade", {}).setdefault("gui", {})
    cleaned = extract_coords(coords if isinstance(coords, dict) else {})
    for key in COORD_KEYS:
        if key in cleaned:
            gui[key] = list(cleaned[key])
        else:
            gui.pop(key, None)
    return out


def write_gui_coords_file(root: Path | str, coords: dict[str, Any]) -> Path:
    """同步写 config/gui_coords.yaml，避免自检说两份坐标不一致。"""
    path = Path(root) / "config" / "gui_coords.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"trade": {"gui": extract_coords(coords)}}
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
