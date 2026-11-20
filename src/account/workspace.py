"""
几本互不影响的账：同花顺实盘、同花顺模拟、华泰仿真、华泰真实。

功能：账户分析可以换账。换过去之后，开平仓、出入金、持仓记录、点单意图
都只写这一本。另一本文件原样留着。

原理：当前选中哪一本，写在 data/active_account.json。账本、持仓、意图的文件名
按这本账来。没写过这个文件时，仍用原来的实盘文件名，旧用户不用搬家。
华泰仿真/真实的余额以 CTP 柜台资金查询为准；同花顺模拟账仍要手工改。

文件布局在 src.domain.accounts；这里保留界面文字、配置读取和写盘。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.domain.accounts import (
    CTP_LIVE,
    CTP_SIM,
    LIVE,
    ORDER,
    PAPER,
    PROFILES,
    active_file,
    book_file,
    book_id_for_ctp_kind,
    id_from_label,
    intent_dir,
    intent_file,
    is_ctp_book,
    load_active_id,
    normalize_id,
    periods_folder,
    profile_of,
    state_file,
)

__all__ = [
    "CTP_LIVE",
    "CTP_SIM",
    "LIVE",
    "ORDER",
    "PAPER",
    "PROFILES",
    "active_file",
    "book_file",
    "book_id_for_ctp_kind",
    "ctp_kind_of_book",
    "ctp_user_id",
    "display_label",
    "id_from_label",
    "intent_dir",
    "intent_file",
    "is_ctp_book",
    "labels",
    "load_active_id",
    "normalize_id",
    "periods_folder",
    "profile_of",
    "resolve_ctp_book",
    "set_active_id",
    "state_file",
]

# 同一份「当前账」可能被界面和常驻同时读，写的时候排队。
_LOCK = threading.Lock()


def _kind_env(kind: Any) -> str:
    text = str(kind or "").strip().lower()
    return "live" if text in ("live", "ctp_live", "真实", "real") else "sim"


def resolve_ctp_book(root: str | Path, kind: Any) -> str:
    """这次要连的柜台，对应哪个账本记号。

    当前账号正好是这个环境就沿用当前账号（active_account.json v2）；
    只有一个可用账号时用它；否则退回旧四本账，保持旧安装的行为。
    """
    env = _kind_env(kind)
    try:
        from src.account.registry import load_registry

        registry = load_registry(root)
    except Exception:  # noqa: BLE001 - 读不到就按旧账本
        registry = None
    if registry is None or not registry.accounts:
        return book_id_for_ctp_kind(env)
    active = load_active_id(root)
    candidates = [
        item
        for item in registry.accounts
        if item.channel == "ctp"
        and item.environment == env
        and item.status == "enabled"
    ]
    for item in candidates:
        if active in (item.account_id, item.book_id):
            return item.account_id
    if len(candidates) == 1:
        return candidates[0].account_id
    return book_id_for_ctp_kind(env)


def ctp_kind_of_book(book_id: str, root: str | Path | None = None) -> str:
    """账本记号对应的 CTP 环境；不是 CTP 账返回空。"""
    aid = normalize_id(book_id)
    if aid == CTP_SIM:
        return "sim"
    if aid == CTP_LIVE:
        return "live"
    try:
        from src.account.registry import load_registry

        registry = load_registry(Path(root) if root is not None else _default_root())
    except Exception:  # noqa: BLE001
        return ""
    if registry is None:
        return ""
    for item in registry.accounts:
        if aid in (item.account_id, item.book_id) and item.channel == "ctp":
            return item.environment
    return ""


def _default_root() -> Path:
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def ctp_user_id(kind: str, root: str | Path | None = None) -> str:
    """仿真或真实资金账号。仿真默认 28077；真实用配置页「资金账号」。"""
    try:
        from src.utils.config_loader import load_config
        from src.trader.ctp_logic import overlay_ctp_profile

        cfg = load_config()
        ctp = overlay_ctp_profile(
            (cfg.get("trade") or {}).get("ctp") or {},
            kind,
            account=cfg.get("account"),
        )
        return str(ctp.get("user_id") or "").strip()
    except Exception:  # noqa: BLE001
        del root
        return ""


def display_label(account_id: Any, root: str | Path | None = None) -> str:
    """下拉框上的名字。华泰账带上资金账号，方便认出 28077；新账号用显示名。"""
    aid = str(account_id or LIVE)
    info = PROFILES.get(aid)
    if info is None:
        name = _registry_account_label(aid, root)
        if name:
            return name
        info = PROFILES[LIVE]
    if aid == CTP_SIM:
        user = ctp_user_id("sim", root) or "未填"
        return f"{info['label']} {user}"
    if aid == CTP_LIVE:
        user = ctp_user_id("live", root) or "未填资金账号"
        return f"{info['label']} {user}"
    return info["label"]


def _registry_account_label(account_id: str, root: str | Path | None = None) -> str:
    """从 accounts.yaml 取账号显示名；读不到返回空。"""
    try:
        from src.account.registry import load_registry

        registry = load_registry(Path(root) if root is not None else Path("."))
        if registry is None:
            return ""
        account = registry.account(account_id)
        if account is None:
            return ""
        env = "仿真" if account.environment == "sim" else "真实"
        return f"{account.display_name} · {env}"
    except Exception:  # noqa: BLE001 - 只是显示名，不能影响记账
        return ""


def labels(root: str | Path | None = None) -> list[str]:
    """下拉框从左到右：同花顺实盘、同花顺模拟、华泰仿真、华泰真实。"""
    return [display_label(key, root) for key in ORDER]


def set_active_id(root: str | Path, account_id: Any) -> str:
    """
    换成另一本账。只改记号文件，不搬成交、不改另一本的余额。

    返回真正写进去的记号。
    """
    wanted = normalize_id(account_id)
    path = active_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if wanted in PROFILES:
        payload: dict[str, Any] = {"id": wanted, "label": display_label(wanted, root)}
        label = PROFILES[wanted]["label"]
    else:
        # 新账号账本：active_account.json 以 account_id 为准（v2）。
        payload = {
            "version": 2,
            "account_id": wanted,
            "label": display_label(wanted, root),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        label = payload["label"]
    tmp = path.with_suffix(".json.tmp")
    with _LOCK:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    logger.info("当前记账换成 {}", label)
    return wanted
