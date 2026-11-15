"""
几本互不影响的账：GUI 实盘、GUI 模拟、CTP 仿真、CTP 真实。

功能：当前选中哪一本，写在 data/active_account.json。账本、持仓、意图的文件名
按这本账来。没写过这个文件时，仍用原来的实盘文件名，旧用户不用搬家。

本模块只放文件布局，不含界面文字和配置读取。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

# 实盘账：原来那一套文件，GUI 点真实资金时用这一本。
LIVE = "live"
# 模拟账：交易软件里打开「模拟交易」时用这一本，不改实盘文件。
PAPER = "paper"
# CTP 仿真：资金账号例如 28077，余额来自 CTP。
CTP_SIM = "ctp_sim"
# CTP 真实资金：账号就是配置页「资金账号」（GUI 登录那一号）。
CTP_LIVE = "ctp_live"
ORDER = (LIVE, PAPER, CTP_SIM, CTP_LIVE)

# 界面下拉框上的名字，和文件内部记号分开，方便以后改字不改文件。
PROFILES: dict[str, dict[str, str]] = {
    LIVE: {
        "id": LIVE,
        "label": "GUI实盘账",
        "short": "GUI实盘",
        "book_name": "signal_account.json",
        "periods_dir": "signal_periods",
        "state_name": "bot_state.json",
        "intent_name": "order_intent.json",
        "hint": "这一本记 GUI 点单的真实资金，仍是程序开平仓价加手工结算。CTP 柜台权益请换到「CTP真实」。",
        "banner": "记账：GUI实盘",
        "funding": "manual",
    },
    PAPER: {
        "id": PAPER,
        "label": "GUI模拟账",
        "short": "GUI模拟",
        "book_name": "signal_account_paper.json",
        "periods_dir": "signal_periods_paper",
        "state_name": "bot_state_paper.json",
        "intent_name": "order_intent_paper.json",
        "hint": "这一本配合交易软件的模拟交易。点单只进这本账，不会改实盘账和实盘持仓记录。余额仍要手工记入金、出金或每日结算。",
        "banner": "记账：GUI模拟（请打开交易软件的模拟交易）",
        "funding": "manual",
    },
    CTP_SIM: {
        "id": CTP_SIM,
        "label": "CTP仿真",
        "short": "CTP仿真",
        "book_name": "signal_account_ctp_sim.json",
        "periods_dir": "signal_periods_ctp_sim",
        "state_name": "bot_state_ctp_sim.json",
        "intent_name": "order_intent_ctp_sim.json",
        "hint": "这一本是 CTP 仿真的柜台资金账，和 GUI 点单账分开。资金账号、权益、可用都以期货公司回报为准，原样保存，不能手改。请先在交易台登录后点「刷新持仓」，或在本页点「从柜台刷新资金」。",
        "banner": "记账：CTP仿真（柜台资金）",
        "funding": "ctp",
        "ctp_kind": "sim",
    },
    CTP_LIVE: {
        "id": CTP_LIVE,
        "label": "CTP真实",
        "short": "CTP真实",
        "book_name": "signal_account_ctp_live.json",
        "periods_dir": "signal_periods_ctp_live",
        "state_name": "bot_state_ctp_live.json",
        "intent_name": "order_intent_ctp_live.json",
        "hint": "这一本是 CTP 真实资金的柜台账，和 GUI 点单账分开，即使账号相同也不混记。资金页数字来自期货公司回报。用 CTP 开常驻前请先选中这一本。",
        "banner": "记账：CTP真实（柜台资金）",
        "funding": "ctp",
        "ctp_kind": "live",
    },
}


def is_ctp_book(account_id: Any) -> bool:
    """这本账的余额是否以柜台资金查询为准。"""
    aid = normalize_id(account_id)
    return str((PROFILES.get(aid) or {}).get("funding") or "") == "ctp"


def book_id_for_ctp_kind(kind: Any) -> str:
    """仿真柜台对 CTP 仿真账，真实柜台对 CTP 真实账。"""
    text = str(kind or "").strip().lower()
    if text in {"live", "real", "prod", "真实", "真实账号"}:
        return CTP_LIVE
    return CTP_SIM


def normalize_id(raw: Any) -> str:
    """把界面文字收成内部记号。认不出来就当 GUI 实盘，避免误进模拟账。"""
    text = str(raw or "").strip().lower()
    shown = str(raw or "")
    if text in {CTP_SIM, "huatai_sim"} or "华泰仿真" in shown or "ctp仿真" in text:
        return CTP_SIM
    if text in {CTP_LIVE, "huatai_live"} or "华泰真实" in shown or "ctp真实" in text:
        return CTP_LIVE
    if text in (
        PAPER,
        "ths_sim",
        "paper",
        "模拟",
        "模拟账",
        "同花顺模拟账",
        PROFILES[PAPER]["label"].lower(),
    ):
        return PAPER
    if "同花顺模拟" in shown or "gui模拟" in text or shown.strip() == "模拟账":
        return PAPER
    return LIVE


def id_from_label(label: Any) -> str:
    """下拉框选中的中文，转成内部记号。"""
    return normalize_id(label)


def profile_of(account_id: Any = None, *, root: str | Path | None = None) -> dict[str, str]:
    """一本账的名字和文件名。没指定就用当前选中的那本。"""
    if account_id is None:
        aid = load_active_id(root or ".")
    else:
        aid = normalize_id(account_id)
    return dict(PROFILES.get(aid) or PROFILES[LIVE])


def active_file(root: str | Path) -> Path:
    """当前选中哪一本，记在这个小文件里。不写进配置，避免改你正在用的策略数字。"""
    return Path(root) / "data" / "active_account.json"


def load_active_id(root: str | Path) -> str:
    """读当前账。文件没有或坏了，就是 GUI 实盘账。"""
    path = active_file(root)
    if not path.exists():
        return LIVE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("当前账记号读不出来，按实盘账: {}", exc)
        return LIVE
    if isinstance(data, dict):
        return normalize_id(data.get("id"))
    return normalize_id(data)


def book_file(root: str | Path, account_id: Any = None) -> Path:
    """当前（或指定）账的账本路径。"""
    name = profile_of(account_id, root=root)["book_name"]
    return Path(root) / "data" / name


def periods_folder(root: str | Path, account_id: Any = None) -> Path:
    """当前（或指定）账的旧账期文件夹。"""
    name = profile_of(account_id, root=root)["periods_dir"]
    return Path(root) / "data" / name


def state_file(root: str | Path, account_id: Any = None) -> Path:
    """当前（或指定）账的程序持仓文件。"""
    name = profile_of(account_id, root=root)["state_name"]
    return Path(root) / "data" / name


def intent_file(root: str | Path, account_id: Any = None) -> Path:
    """
    旧版「一本账一个」的点单意图文件。

    只为兼容升级前崩溃遗留的记录（启动时仍会读、会清）；新委托写在 intent_dir 里，一笔一个文件。
    """
    name = profile_of(account_id, root=root)["intent_name"]
    return Path(root) / "data" / name


def intent_dir(root: str | Path, account_id: Any = None) -> Path:
    """当前（或指定）账按笔存放委托意图的目录：一笔一个 JSON，互不覆盖。"""
    aid = normalize_id(account_id) if account_id is not None else load_active_id(root)
    return Path(root) / "data" / "intents" / aid
