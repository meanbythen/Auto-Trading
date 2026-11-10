"""
几本互不影响的账：同花顺实盘、同花顺模拟、华泰仿真、华泰真实。

功能：账户分析可以换账。换过去之后，开平仓、出入金、持仓记录、点单意图
都只写这一本。另一本文件原样留着。

原理：当前选中哪一本，写在 data/active_account.json。账本、持仓、意图的文件名
按这本账来。没写过这个文件时，仍用原来的实盘文件名，旧用户不用搬家。
华泰仿真/真实的余额以 CTP 柜台资金查询为准；同花顺模拟账仍要手工改。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from loguru import logger

# 同一份「当前账」可能被界面和常驻同时读，写的时候排队。
_LOCK = threading.Lock()

# 实盘账：原来那一套文件，点同花顺真实资金时用这一本。
LIVE = "live"
# 模拟账：期货通里打开「模拟交易」时用这一本，不改实盘文件。
PAPER = "paper"
# 华泰仿真：资金账号例如 28077，余额来自 CTP。
CTP_SIM = "ctp_sim"
# 华泰真实资金：账号就是配置页「资金账号」（同花顺登录那一号）。
CTP_LIVE = "ctp_live"
ORDER = (LIVE, PAPER, CTP_SIM, CTP_LIVE)

# 界面下拉框上的名字，和文件内部记号分开，方便以后改字不改文件。
PROFILES: dict[str, dict[str, str]] = {
    LIVE: {
        "id": LIVE,
        "label": "同花顺实盘账",
        "short": "实盘账",
        "book_name": "signal_account.json",
        "periods_dir": "signal_periods",
        "state_name": "bot_state.json",
        "intent_name": "order_intent.json",
        "hint": "这一本记同花顺点单的真实资金，仍是程序开平仓价加手工结算。华泰柜台权益请换到「华泰真实账户」。",
        "banner": "记账：实盘账",
        "funding": "manual",
    },
    PAPER: {
        "id": PAPER,
        "label": "同花顺模拟账",
        "short": "模拟账",
        "book_name": "signal_account_paper.json",
        "periods_dir": "signal_periods_paper",
        "state_name": "bot_state_paper.json",
        "intent_name": "order_intent_paper.json",
        "hint": "这一本配合期货通左上角的「模拟交易」。点单只进这本账，不会改实盘账和实盘持仓记录。余额仍要手工记入金、出金或每日结算。",
        "banner": "记账：模拟账（请打开期货通「模拟交易」）",
        "funding": "manual",
    },
    CTP_SIM: {
        "id": CTP_SIM,
        "label": "华泰仿真",
        "short": "华泰仿真",
        "book_name": "signal_account_ctp_sim.json",
        "periods_dir": "signal_periods_ctp_sim",
        "state_name": "bot_state_ctp_sim.json",
        "intent_name": "order_intent_ctp_sim.json",
        "hint": "这一本是华泰仿真的柜台资金账，和同花顺点单账分开。资金账号、权益、可用都以期货公司回报为准，原样保存，不能手改。请先在交易台登录后点「刷新持仓」，或在本页点「从柜台刷新资金」。",
        "banner": "记账：华泰仿真（柜台资金）",
        "funding": "ctp",
        "ctp_kind": "sim",
    },
    CTP_LIVE: {
        "id": CTP_LIVE,
        "label": "华泰真实账户",
        "short": "华泰真实",
        "book_name": "signal_account_ctp_live.json",
        "periods_dir": "signal_periods_ctp_live",
        "state_name": "bot_state_ctp_live.json",
        "intent_name": "order_intent_ctp_live.json",
        "hint": "这一本是华泰真实资金的柜台账，和同花顺点单账分开，即使账号相同也不混记。资金页数字来自期货公司回报。用 CTP 开常驻前请先选中这一本。",
        "banner": "记账：华泰真实（柜台资金）",
        "funding": "ctp",
        "ctp_kind": "live",
    },
}


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
    """下拉框上的名字。华泰账带上资金账号，方便认出 28077。"""
    aid = str(account_id or LIVE)
    info = PROFILES.get(aid) or PROFILES[LIVE]
    if aid == CTP_SIM:
        user = ctp_user_id("sim", root) or "未填"
        return f"{info['label']} {user}"
    if aid == CTP_LIVE:
        user = ctp_user_id("live", root) or "未填资金账号"
        return f"{info['label']} {user}"
    return info["label"]


def labels(root: str | Path | None = None) -> list[str]:
    """下拉框从左到右：同花顺实盘、同花顺模拟、华泰仿真、华泰真实。"""
    return [display_label(key, root) for key in ORDER]


def is_ctp_book(account_id: Any) -> bool:
    """这本账的余额是否以柜台资金查询为准。"""
    aid = normalize_id(account_id)
    return str((PROFILES.get(aid) or {}).get("funding") or "") == "ctp"


def book_id_for_ctp_kind(kind: Any) -> str:
    """仿真柜台对华泰仿真账，真实柜台对华泰真实账。"""
    text = str(kind or "").strip().lower()
    if text in {"live", "real", "prod", "真实", "真实账号"}:
        return CTP_LIVE
    return CTP_SIM


def normalize_id(raw: Any) -> str:
    """把界面文字收成内部记号。认不出来就当同花顺实盘，避免误进模拟账。"""
    text = str(raw or "").strip().lower()
    shown = str(raw or "")
    if text in {CTP_SIM, "ctp_sim", "huatai_sim"} or "华泰仿真" in shown:
        return CTP_SIM
    if text in {CTP_LIVE, "ctp_live", "huatai_live"} or "华泰真实" in shown:
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
    if "同花顺模拟" in shown or shown.strip() == "模拟账":
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
    """读当前账。文件没有或坏了，就是实盘账。"""
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


def set_active_id(root: str | Path, account_id: Any) -> str:
    """
    换成另一本账。只改记号文件，不搬成交、不改另一本的余额。

    返回真正写进去的记号。
    """
    wanted = normalize_id(account_id)
    path = active_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": wanted, "label": display_label(wanted, root)}
    tmp = path.with_suffix(".json.tmp")
    with _LOCK:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    logger.info("当前记账换成 {}", PROFILES[wanted]["label"])
    return wanted


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
    """当前（或指定）账的点单意图文件。崩溃核对时只看这一本。"""
    name = profile_of(account_id, root=root)["intent_name"]
    return Path(root) / "data" / name
