"""
把 CTP 资金查询写进华泰仿真 / 华泰真实这两本账。

功能：交易台刷新持仓、账户分析「从柜台刷新」、以及常驻连上柜台后，把期货公司回报原样存本地。
原理：仿真号只写华泰仿真账；真实资金只写华泰真实账。同花顺点单账完全分开，即使账号相同也不混用。
本地不改柜台数字。常驻启动前必须先选中对应那本账，且登录号和账上记下的资金账号一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from src.account.signal_book import (
    apply_ctp_equity,
    load_named_book,
    save_named_book,
    write_capital_to_config,
)
from src.account.workspace import (
    book_id_for_ctp_kind,
    display_label,
    is_ctp_book,
    load_active_id,
)
from src.version import read_version


def broker_day_pnl(data: dict[str, Any] | None) -> float | None:
    """
    用柜台资金算出当天盈亏，给日亏损熔断用。

    有昨结就用：动态权益 − 昨结 − 入金 + 出金。没有昨结时，用平仓盈亏加盯市浮盈。
    算不出就返回 None，调用方继续用原来的账本口径，不会因此停开仓。
    """
    if not isinstance(data, dict):
        return None
    try:
        pre = float(data.get("pre_balance") or 0)
        bal = float(data.get("balance") or 0)
        dep = float(data.get("deposit") or 0)
        wdr = float(data.get("withdraw_amt") or 0)
    except (TypeError, ValueError):
        return None
    if pre > 1e-9 and bal > 0:
        return round(bal - pre - dep + wdr, 2)
    try:
        close_p = data.get("close_profit")
        pos_p = data.get("position_profit")
    except (TypeError, ValueError):
        return None
    if close_p is None and pos_p is None:
        return None
    try:
        return round(float(close_p or 0) + float(pos_p or 0), 2)
    except (TypeError, ValueError):
        return None


def apply_broker_runtime(
    root: str | Path,
    *,
    cfg: dict[str, Any],
    sizer: Any,
    risk: Any | None,
    trader: Any,
) -> str:
    """
    把刚查到的柜台资金写进华泰账，并同步到算手数、日亏损、配置权益。

    不改变持仓对账结果，也不会因为资金数字变化而拦开仓。
    """
    gateway = getattr(trader, "gateway", None)
    if gateway is None:
        return ""
    # 柜台这一次没给出有效权益时，不要用 0 去改账本，沿用上一笔柜台数字。
    raw_account = dict(getattr(gateway, "account", None) or {})
    try:
        raw_balance = float(raw_account.get("balance") or 0)
    except (TypeError, ValueError):
        raw_balance = 0.0
    raw_avail = getattr(gateway, "available", None)
    if raw_balance <= 0 and (raw_avail is None or float(raw_avail or 0) <= 0):
        snap = dict(getattr(trader, "last_broker_snapshot", None) or {})
        try:
            keep = float(snap.get("balance") or getattr(trader, "last_broker_equity", 0) or 0)
        except (TypeError, ValueError):
            keep = 0.0
        if keep > 0:
            sizer.capital = keep
            cfg.setdefault("risk", {})["capital"] = keep
            avail = snap.get("available")
            if avail is not None:
                try:
                    sizer.cash_available = float(avail)
                except (TypeError, ValueError):
                    pass
        return ""
    note = push_gateway_account(root, gateway)
    snap = dict(getattr(trader, "last_broker_snapshot", None) or {})
    if not snap:
        from src.trader.ctp_display import parse_account

        snap = dict(parse_account(getattr(gateway, "account", None)))
        settings = getattr(gateway, "settings", None)
        snap["user_id"] = str(getattr(settings, "user_id", "") or "")
    try:
        equity = float(snap.get("balance") or 0)
    except (TypeError, ValueError):
        equity = 0.0
    if equity <= 0:
        try:
            equity = float(getattr(trader, "last_broker_equity", 0) or 0)
        except (TypeError, ValueError):
            equity = 0.0
    if equity > 0:
        before = float(getattr(sizer, "capital", 0) or 0)
        sizer.capital = equity
        cfg.setdefault("risk", {})["capital"] = equity
        if abs(equity - before) >= 0.01:
            logger.info("本地仓位计算已改成期货公司动态权益 {:.2f}（原 {:.2f}）", equity, before)
    avail = snap.get("available")
    if avail is not None:
        try:
            sizer.cash_available = float(avail)
        except (TypeError, ValueError):
            pass
    if risk is not None:
        day = broker_day_pnl(snap)
        if day is not None and abs(float(getattr(risk, "daily_pnl", 0) or 0) - day) >= 0.01:
            risk.set_daily_pnl(day)
    return note


def push_gateway_account(root: str | Path, gateway: Any) -> str:
    """
    把已经查到的柜台资金写入对应华泰账。

    当前下拉框如果正好是这一本，再把账户权益写进配置，给仓位计算用。
    """
    settings = getattr(gateway, "settings", None)
    if settings is None:
        return "还没有柜台连接。"
    snapshot = dict(getattr(gateway, "account", None) or {})
    available = getattr(gateway, "available", None)
    if not snapshot and available is None:
        return "柜台还没有资金数据。请先登录并点「刷新持仓」。"
    if available is not None and "available" not in snapshot:
        snapshot["available"] = float(available)
    if "balance" not in snapshot and available is not None:
        snapshot["balance"] = float(available)
    user_id = str(getattr(settings, "user_id", "") or "")
    kind = str(getattr(settings, "account_kind", "") or "sim")
    book_id = book_id_for_ctp_kind(kind)
    # 读-改-写要和成交记账共用一把锁，否则会拿旧账本把刚追加的成交行盖掉。
    from src.account.signal_book import book_lock

    with book_lock():
        old = load_named_book(root, book_id)
        target, book = apply_ctp_equity(old, snapshot, user_id=user_id, version=read_version())
        save_named_book(root, book_id, book)
    label = display_label(book_id, root)
    if load_active_id(root) == book_id:
        # 只在真正的程序目录里改配置权益，测试用的临时文件夹不会去改 config.yaml。
        from src.utils.config_loader import load_config

        project = str((load_config() or {}).get("_project_root") or "")
        if project and Path(root).resolve() == Path(project).resolve():
            write_capital_to_config(target)
    logger.info("已把柜台资金写入 {}，动态权益 {:.2f}", label, target)
    return f"已写入「{label}」动态权益 {target:.2f} 元。"


def resident_book_guard(root: str | Path, cfg: dict[str, Any] | None) -> str:
    """
    常驻用 CTP 前先核对：账户分析选中的账，必须和这次要连的柜台是同一本、同一个资金账号。

    返回空字符串表示可以启动。有字就是拦下的原因，给界面弹窗和日志用。
    """
    trade = (cfg or {}).get("trade") or {}
    if str(trade.get("executor") or "").strip().lower() != "ctp":
        return ""
    from src.trader.ctp_logic import normalize_ctp_kind, overlay_ctp_profile

    overlaid = overlay_ctp_profile(
        trade.get("ctp") if isinstance(trade.get("ctp"), dict) else {},
        account=(cfg or {}).get("account") if isinstance((cfg or {}).get("account"), dict) else {},
    )
    kind = normalize_ctp_kind(overlaid.get("account_kind"))
    needed = book_id_for_ctp_kind(kind)
    active = load_active_id(root)
    if active != needed:
        return (
            f"请先到账户分析选中「{display_label(needed, root)}」，再启动常驻。"
            f"现在选的是「{display_label(active, root)}」。"
            "本地账必须和这次 CTP 要连的账号是同一本。"
        )
    user = str(overlaid.get("user_id") or "").strip()
    if not user:
        if kind == "live":
            return "配置页还没有「资金账号」，CTP 真实通道不知道连哪一号。请先填好。"
        return "这次 CTP 仿真还没有资金账号。请先在配置里填仿真账号（例如 28077）。"
    book = load_named_book(root, needed)
    stored = ""
    if isinstance(book, dict):
        snap_raw = book.get("ctp_snapshot")
        snap: dict[str, Any] = snap_raw if isinstance(snap_raw, dict) else {}
        stored = str(snap.get("user_id") or "").strip()
    if stored and stored != user:
        return (
            f"本地「{display_label(needed, root)}」记下的资金账号是 {stored}，"
            f"这次 CTP 要连 {user}。请先对上配置页账号，再启动常驻。"
        )
    return ""


def can_pull_from_trader(trader: Any, account_id: str) -> bool:
    """当前连接的柜台，是不是正在看的那本华泰账。"""
    if trader is None or not is_ctp_book(account_id):
        return False
    settings = getattr(trader, "settings", None)
    gateway = getattr(trader, "gateway", None)
    if settings is None or gateway is None:
        return False
    if not getattr(getattr(gateway, "session", None), "ready", False):
        return False
    return book_id_for_ctp_kind(getattr(settings, "account_kind", "")) == account_id
