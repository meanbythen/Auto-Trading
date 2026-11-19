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
    _ctp_snap_changed,
    apply_ctp_equity,
    load_named_book,
    save_named_book,
)
from src.account.workspace import (
    book_id_for_ctp_kind,
    display_label,
    is_ctp_book,
    load_active_id,
    set_active_id,
)
from src.version import read_version

# 持仓时动态权益每跳都变。内存里每轮跟上，供算手数和日亏熔断。
# 日志不要跟着每跳刷：第一次，或一下子变超过约 200 元，才说一声。
_MTM_LOG_YUAN = 200.0


def _should_log_mtm(before: float, after: float) -> bool:
    """盯市几十元的跳动返回 False。"""
    try:
        before_n = float(before or 0)
        after_n = float(after or 0)
    except (TypeError, ValueError):
        return True
    return before_n <= 0 or abs(after_n - before_n) + 1e-9 >= _MTM_LOG_YUAN


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
        if data.get("withdraw_amt") is not None:
            wdr = float(data.get("withdraw_amt") or 0)
        elif data.get("withdraw_quota") is not None:
            wdr = float(data.get("withdraw") or 0)
        else:
            # 已翻译快照里 withdraw 是可取资金，不能当出金。
            wdr = 0.0
    except (TypeError, ValueError):
        return None
    # 可取资金几乎等于权益时，那是额度不是出金。
    if wdr > 1 and pre > 1 and abs(wdr - pre) < 0.05:
        wdr = 0.0
    if wdr > 1 and bal > 1 and abs(wdr - bal) < 0.05:
        wdr = 0.0
    equity_pnl = round(bal - pre - dep + wdr, 2) if pre > 1e-9 and bal > 0 else None
    try:
        close_p = float(data.get("close_profit") or 0)
        pos_p = float(data.get("position_profit") or 0)
        fee = float(data.get("commission") or 0)
        trade_pnl = round(close_p + pos_p - fee, 2)
        has_trade = any(
            data.get(key) is not None
            for key in ("close_profit", "position_profit", "commission")
        )
    except (TypeError, ValueError):
        trade_pnl = None
        has_trade = False
    if equity_pnl is not None:
        if has_trade and abs(equity_pnl) < 0.005 and abs(trade_pnl or 0) >= 0.01:
            return trade_pnl
        return equity_pnl
    if has_trade:
        return trade_pnl
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
        sizer.capital = equity
        cfg.setdefault("risk", {})["capital"] = equity
    avail = snap.get("available")
    if avail is not None:
        try:
            sizer.cash_available = float(avail)
        except (TypeError, ValueError):
            pass
    if risk is not None:
        day = broker_day_pnl(snap)
        if day is not None and abs(float(getattr(risk, "daily_pnl", 0) or 0) - day) >= 0.01:
            # 盯市浮盈每轮都要对齐熔断数字，但不要每跳打「当日盈亏按账本校准」。
            risk.set_daily_pnl(day, log=False)
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
    from src.account.workspace import book_file as named_book_path

    book_path = named_book_path(root, book_id)
    with book_lock():
        old = load_named_book(root, book_id)
        # 文件在却读失败时，绝不能当成「还没有账」新建一本盖上去，成交记录会丢。
        if old is None and book_path.exists():
            logger.warning("华泰账本暂时读不到，本轮不改文件（持仓和成交记录不动）")
            return "账本正被占用，本轮不改文件。"
        # 必须先拷贝旧快照。apply_ctp_equity 会改同一本字典，事后再比等于自己和自己比，永远以为没变。
        raw_old_snap = (old or {}).get("ctp_snapshot") if isinstance(old, dict) else None
        old_snap = dict(raw_old_snap) if isinstance(raw_old_snap, dict) else None
        target, book = apply_ctp_equity(old, snapshot, user_id=user_id, version=read_version())
        new_snap = book.get("ctp_snapshot") if isinstance(book, dict) else None
        # 动态权益、可用、保证金没变时不必重写账本和配置，否则每轮扫描都会刷盘。
        unchanged = isinstance(old_snap, dict) and isinstance(new_snap, dict) and not _ctp_snap_changed(
            old_snap, new_snap
        )
        if not unchanged:
            save_named_book(root, book_id, book)
    if unchanged:
        return ""
    label = display_label(book_id, root)
    old_balance = 0.0
    if isinstance(old_snap, dict):
        try:
            old_balance = float(old_snap.get("balance") or 0)
        except (TypeError, ValueError):
            old_balance = 0.0
    # 手数用内存里的柜台权益。盯市跳动不要每轮改 config.yaml，否则会一直刷「配置已保存」。
    # 成交后、账户分析手动刷新仍会写配置。
    if not _should_log_mtm(old_balance, target):
        return ""
    logger.info("已把柜台资金写入 {}，动态权益 {:.2f}", label, target)
    return f"已写入「{label}」动态权益 {target:.2f} 元。"


def align_active_book_to_ctp(
    root: str | Path,
    cfg: dict[str, Any] | None,
    *,
    running: bool = False,
) -> str:
    """
    把账户分析换成和这次 CTP 账号同一本。

    程序基座只有一套：仿真连 28077 就看 CTP 仿真账，真实连资金账号就看 CTP 真实账。
    开平仓规则不变。常驻还在跑时不换，避免内存持仓和账本对不上。
    返回空字符串表示本来就对上了，或现在不是 CTP 通道。
    """
    trade = (cfg or {}).get("trade") or {}
    if str(trade.get("executor") or "").strip().lower() != "ctp":
        return ""
    from src.trader.ctp_logic import normalize_ctp_kind, overlay_ctp_profile

    overlaid = overlay_ctp_profile(
        trade.get("ctp") if isinstance(trade.get("ctp"), dict) else {},
        account=(cfg or {}).get("account") if isinstance((cfg or {}).get("account"), dict) else {},
    )
    needed = book_id_for_ctp_kind(normalize_ctp_kind(overlaid.get("account_kind")))
    active = load_active_id(root)
    if active == needed:
        return ""
    wanted = display_label(needed, root)
    now = display_label(active, root)
    if running:
        return (
            f"常驻还在跑，账户分析仍是「{now}」。"
            f"请先停止常驻，再切到「{wanted}」。"
        )
    set_active_id(root, needed)
    logger.info("账户分析已换成 {}，和这次 CTP 账号同一本", wanted)
    return f"账户分析已换成「{wanted}」，和这次 CTP 账号同一本。"


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
