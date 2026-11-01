"""
配置页的整页存档。

功能：把配置界面上的数字和开关存成一份，名字是「关注品种-策略名称-时间」。
之后可以一键应用某一份，避免启动时常驻用的还是旧开关。
原理：存的是当时整份配置（策略小节、风控、同花顺路径都在里面）。
密码、认证码不写入这份文件，仍只留在 secrets.local.yaml。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.backtest.param_history import default_version_name, strategy_display_name

FILE_NAME = "config_presets.json"
MAX_KEEP = 30
# 这些键是运行时填进内存的，不能写进存档。
_SECRET_KEYS = {"resolved_password", "resolved_auth_code", "password", "ctp_password", "auth_code"}


def presets_path(root: Path | str) -> Path:
    return Path(root) / "data" / FILE_NAME


def clean_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """去掉密码和只在内存里用的字段，得到可以存盘的配置。"""
    dump = {k: copy.deepcopy(v) for k, v in cfg.items() if not str(k).startswith("_")}
    account = dump.get("account")
    if isinstance(account, dict):
        dump["account"] = {k: v for k, v in account.items() if k not in _SECRET_KEYS}
    trade = dump.get("trade")
    if isinstance(trade, dict) and isinstance(trade.get("ctp"), dict):
        trade["ctp"] = {k: v for k, v in trade["ctp"].items() if k not in _SECRET_KEYS}
    return dump


def preset_name(cfg: dict[str, Any], when: datetime | None = None) -> str:
    """关注品种-策略名称-时间。多个品种时用第一个，空着就写未填品种。"""
    st = cfg.get("strategy") or {}
    symbols = [str(x).strip() for x in (st.get("symbols") or []) if str(x).strip()]
    symbol = symbols[0] if symbols else ""
    return default_version_name(symbol, when or datetime.now(), st.get("engine"))


def load_presets(root: Path | str) -> list[dict[str, Any]]:
    path = presets_path(root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取配置存档失败: {}", exc)
        return []
    if isinstance(raw, dict):
        items = raw.get("presets") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def save_presets(root: Path | str, items: list[dict[str, Any]]) -> Path:
    path = presets_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"presets": items[:MAX_KEEP]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def add_preset(root: Path | str, cfg: dict[str, Any], when: datetime | None = None) -> dict[str, Any]:
    """追加一份当前配置。同名时在后面加序号，避免下拉框里两条分不清。"""
    now = when or datetime.now()
    items = load_presets(root)
    taken = {str(item.get("name") or "") for item in items}
    name = preset_name(cfg, now)
    if name in taken:
        extra = 2
        candidate = f"{name}-{extra}"
        while candidate in taken:
            extra += 1
            candidate = f"{name}-{extra}"
        name = candidate
    vid = now.strftime("%Y%m%d_%H%M%S")
    taken_ids = {str(item.get("id") or "") for item in items}
    if vid in taken_ids:
        vid = f"{vid}_{now.strftime('%f')[:3]}"
    entry = {
        "id": vid,
        "name": name,
        "time": now.isoformat(timespec="seconds"),
        "config": clean_config(cfg),
    }
    items.insert(0, entry)
    save_presets(root, items)
    logger.info("已保存配置存档 {}", name)
    return entry


def get_preset(root: Path | str, preset_id: str) -> dict[str, Any] | None:
    for item in load_presets(root):
        if str(item.get("id") or "") == str(preset_id):
            return item
    return None


def delete_preset(root: Path | str, preset_id: str) -> str:
    """
    删掉一份配置存档。

    只从存档列表里拿掉这一份，不改正在使用的配置文件。
    返回被删掉的名字。找不到就拒绝，避免误以为已经删了。
    """
    pid = str(preset_id or "").strip()
    if not pid:
        raise ValueError("请先选一份配置存档")
    items = load_presets(root)
    kept: list[dict[str, Any]] = []
    removed = ""
    for item in items:
        if str(item.get("id") or "") == pid and not removed:
            removed = str(item.get("name") or pid)
            continue
        kept.append(item)
    if not removed:
        raise ValueError("找不到这份配置存档")
    save_presets(root, kept)
    logger.info("已删除配置存档 {}", removed)
    return removed


def apply_preset_to_config(current: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    """
    用存档盖住当前配置里对应的页面内容。

    策略、风控、软件路径、开关都会换成存档里的。
    存档里如果混进了密码字段，这里会丢掉，避免写进主配置。
    若存档里的策略引擎已经从程序删除，写盘前改成日内实体，避免界面和文件不一致。
    """
    out = copy.deepcopy(current)
    cleaned = clean_config(saved)
    for key in ("dry_run", "strategy", "trade", "risk", "app", "schedule", "runtime"):
        if key in cleaned:
            out[key] = copy.deepcopy(cleaned[key])
    account = cleaned.get("account")
    if isinstance(account, dict):
        dest = out.setdefault("account", {})
        for key, value in account.items():
            if key in _SECRET_KEYS:
                continue
            dest[key] = copy.deepcopy(value)
    # 与工厂对齐：清掉已删策略残留，缺的默认小节补上，引擎名收成正式名。
    try:
        from src.strategy.factory import sanitize_strategy_config

        sanitize_strategy_config(out)
    except Exception:  # noqa: BLE001
        pass
    # 最大持仓手数已删除；旧存档若还带着这一项，应用时丢掉，避免又写回配置。
    risk = out.get("risk")
    if isinstance(risk, dict):
        risk.pop("max_position", None)
    return out


def switch_summary(cfg: dict[str, Any]) -> str:
    """给人看的开关状态，应用前先对一眼。"""
    st = cfg.get("strategy") or {}
    symbols = "、".join(str(x) for x in (st.get("symbols") or [])) or "未填"
    rows = [
        f"品种：{symbols}",
        f"策略：{strategy_display_name(st.get('engine'))}",
        f"收盘清仓：{'开' if st.get('flatten_at_close', True) else '关'}",
        f"模拟模式：{'开' if cfg.get('dry_run', True) else '关'}",
    ]
    return "\n".join(rows)
