"""
信号账户：按程序自己的开仓、平仓点位记账。

功能：从这一版开始，记下程序真正按信号做成的开仓和平仓。
用这些价格算已平仓盈亏，再加减你手工填的初始资金、入金、出金和每日结算。
手续费按「品种与合约」表扣：开仓、平仓各记一笔支出。滑点仍不单独模拟。
和实盘对不上时，用每日结算把余额改成你要的数。

原理：每一笔让余额变化的钱，都写进 changes，并标明方式。
方式：初始资金、入金、出金、每日结算差额、交易盈亏、手续费。
余额就是这些金额相加。开仓本身不改余额（只记手续费）；没平掉的浮动盈亏不记。
平仓按同一品种、同一方向、先开先平配对，盈亏 = 价差 × 合约乘数 × 手数。
"""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.domain.contracts import DEFAULT_MULTIPLIER, contract_multipliers
from src.domain.ledger import (
    balance_of,
    ctp_history_equity as ctp_history_equity,
    ensure_changes,
    equity_and_peak as equity_and_peak,
    equity_points,
    read_book_file,
    shown_equity as shown_equity,
)
from src.utils.trading_day import futures_trading_day

# 同一份账本可能被常驻线程和界面同时改，先排队再读写。
_LOCK = threading.RLock()

# 余额只允许被这五种方式改动。键是账本里的记号，值是界面上的中文。
_KIND_NAME = {
    "initial": "初始资金",
    "deposit": "入金",
    "withdraw": "出金",
    "settlement": "每日结算差额",
    "pnl": "交易盈亏",
    "commission": "手续费",
    "ctp_equity": "柜台动态权益",
}


def book_path(root: str | Path) -> Path:
    """当前选中的那本账。实盘仍是 data/signal_account.json，模拟账是另一份文件。"""
    from src.account.workspace import book_file

    return book_file(root)


def multiplier_map(cfg: dict[str, Any] | None) -> dict[str, float]:
    """
    从配置读出各品种一手等于多少吨。没写的品种按 10。

    账户页算说明文字时要用。配置页不再单独记一本手工流水。
    """
    if not cfg:
        return {}
    return contract_multipliers(cfg)


def change_kind_name(kind: Any) -> str:
    """资金变动方式的中文。界面和导出用同一套五种名字。"""
    return _kind_name(kind)


def periods_dir(root: str | Path) -> Path:
    """当前账的旧账期文件夹。模拟账另放，不会盖掉实盘已经存档的账期。"""
    from src.account.workspace import periods_folder

    return periods_folder(root)


def new_book(initial_capital: float, *, version: str, started_at: str | None = None) -> dict[str, Any]:
    """
    建一本空账。

    不把旧日志里的成交补进来。开始时间就是现在，之后的信号才往里加。
    """
    started = started_at or _now()
    capital = round(float(initial_capital), 2)
    return {
        "version_started": str(version or ""),
        "started_at": started,
        "initial_capital": capital,
        "cashflows": [],
        "trades": [],
        # 第一笔就是本金。以后每次余额变化都往这张表追加，不改旧行。
        "changes": [
            {"time": started, "kind": "initial", "amount": capital, "note": "开始记账"},
        ],
    }


def load_named_book(root: str | Path, account_id: Any) -> dict[str, Any] | None:
    """读指定那一本账，不看当前下拉框。"""
    from src.account.workspace import book_file

    path = book_file(root, account_id)
    with _LOCK:
        return read_book_file(path)


def load_book(root: str | Path) -> dict[str, Any] | None:
    """读账本。还没有文件、或文件坏了，就返回空，调用方不要当成已经开始记账。"""
    path = book_path(root)
    with _LOCK:
        return read_book_file(path)


def save_named_book(root: str | Path, account_id: Any, book: dict[str, Any]) -> Path:
    """把账本写到指定那一本的文件，不改当前下拉框选中的账。"""
    from src.account.workspace import book_file

    path = book_file(root, account_id)
    book["account_id"] = str(account_id)
    _write_json(path, book)
    _mirror_sqlite(root, book)
    return path


_CTP_HISTORY_MAX = 400
_CTP_SNAP_KEYS = (
    "pre_balance",
    "balance",
    "available",
    "withdraw",
    "withdraw_quota",
    "deposit",
    "withdraw_amt",
    "margin",
    "frozen",
    "frozen_margin",
    "frozen_cash",
    "commission",
    "close_profit",
    "position_profit",
    "usage",
    "mortgage_in",
    "mortgage_out",
    "option_value",
)


def apply_ctp_equity(
    book: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    *,
    user_id: str,
    version: str,
) -> tuple[float, dict[str, Any]]:
    """
    把期货公司资金查询原样写入华泰账。

    不把柜台数字改成「本地差额」。账号优先用回报里的资金账号。
    程序自己估的成交盈亏不会改这本账的余额。
    """
    from src.trader.ctp_display import parse_account

    raw = dict(snapshot or {})
    data = parse_account(raw)
    broker_id = str(data.get("account_id") or raw.get("account_id") or user_id or "").strip()
    target = round(float(data["balance"]), 2)
    if book is None:
        book = new_book(target, version=version)
        book["changes"] = [
            {
                "time": _now(),
                "kind": "initial",
                "amount": target,
                "note": f"CTP 柜台账户 {broker_id}，余额以期货公司回报为准",
            }
        ]
    book["funding"] = "ctp"
    book["ctp_user_id"] = broker_id
    packed: dict[str, Any] = {
        "user_id": broker_id,
        "account_id": broker_id,
        "time": _now(),
        "currency": str(data.get("currency") or ""),
        "raw": raw,
    }
    for key in _CTP_SNAP_KEYS:
        try:
            packed[key] = float(data.get(key) or 0)
        except (TypeError, ValueError):
            packed[key] = 0.0
    packed["balance"] = target
    book["ctp_snapshot"] = packed
    history = list(book.get("ctp_history") or [])
    last = history[-1] if history else None
    if last is None or _ctp_snap_changed(last, packed):
        slim = {key: packed[key] for key in ("user_id", "account_id", "time", "currency", *_CTP_SNAP_KEYS)}
        history.append(slim)
        book["ctp_history"] = history[-_CTP_HISTORY_MAX:]
    # 先写入历史，再按「白天有效快照」更新日历；结算后空壳 0 不能盖掉已记盈亏。
    try:
        _store_ctp_day_pnl(book, packed)
        repair_ctp_daily(book)
    except Exception:  # noqa: BLE001
        pass
    return target, book


def broker_snapshot_day_pnl_usable(snap: dict[str, Any] | None) -> bool:
    """
    这笔柜台快照能不能用来更新「当日盈亏」日历。

    日盘收完、夜盘还没开，或结算确认之后，柜台常把平仓盈亏/手续费清零，
    并把昨结改成今天收盘权益。这时算出来是 0，不能盖掉白天已经记下的真实盈亏。
    """
    if not isinstance(snap, dict):
        return False
    try:
        close_p = abs(float(snap.get("close_profit") or 0))
        pos_p = abs(float(snap.get("position_profit") or 0))
        fee = abs(float(snap.get("commission") or 0))
        bal = float(snap.get("balance") or 0)
        pre = float(snap.get("pre_balance") or 0)
    except (TypeError, ValueError):
        return True
    if close_p < 0.005 and pos_p < 0.005 and fee < 0.005 and pre > 1 and abs(bal - pre) < 0.05:
        return False
    return True


def day_pnl_from_ctp_history(
    history: list[Any] | None,
    day: date | str,
) -> float | None:
    """从柜台历史里，取某个交易日最后一笔「还没被结算清零」的当日盈亏。"""
    from src.account.broker_pnl import broker_day_pnl

    want = str(day)[:10]
    best: float | None = None
    for row in history or []:
        if not isinstance(row, dict):
            continue
        try:
            key = str(futures_trading_day(row.get("time")))
        except Exception:  # noqa: BLE001
            continue
        if key != want:
            continue
        if not broker_snapshot_day_pnl_usable(row):
            continue
        pnl = broker_day_pnl(row)
        if pnl is None:
            continue
        best = round(float(pnl), 2)
    return best


def _store_ctp_day_pnl(book: dict[str, Any], snap: dict[str, Any]) -> None:
    """把这一笔快照写进 ctp_daily；结算空壳且已有非零记录时不动。"""
    from src.account.broker_pnl import broker_day_pnl

    day_key = str(futures_trading_day(snap.get("time")))
    days = dict(book.get("ctp_daily") or {})
    prev = days.get(day_key)
    try:
        prev_n = float(prev) if prev is not None else None
    except (TypeError, ValueError):
        prev_n = None
    if not broker_snapshot_day_pnl_usable(snap):
        if prev_n is None:
            rebuilt = day_pnl_from_ctp_history(book.get("ctp_history"), day_key)
            if rebuilt is not None:
                days[day_key] = rebuilt
                book["ctp_daily"] = days
        return
    pnl = broker_day_pnl(snap)
    if pnl is None:
        return
    days[day_key] = round(float(pnl), 2)
    book["ctp_daily"] = days


def repair_ctp_daily(book: dict[str, Any] | None) -> bool:
    """
    用柜台历史把日历盈亏补全或纠正。

    返回是否改过账本。已有非零、历史也推不出更好结果时不改。
    """
    if not isinstance(book, dict) or str(book.get("funding") or "") != "ctp":
        return False
    history = list(book.get("ctp_history") or [])
    if not history:
        return False
    days = dict(book.get("ctp_daily") or {})
    day_keys: set[str] = set(str(k)[:10] for k in days.keys())
    for row in history:
        if not isinstance(row, dict):
            continue
        try:
            day_keys.add(str(futures_trading_day(row.get("time"))))
        except Exception:  # noqa: BLE001
            continue
    changed = False
    for day_key in sorted(day_keys):
        rebuilt = day_pnl_from_ctp_history(history, day_key)
        if rebuilt is None:
            continue
        try:
            old = float(days[day_key]) if day_key in days else None
        except (TypeError, ValueError):
            old = None
        # 被结算空壳盖成 0，或这一天还没记过：用历史里白天的数。
        if old is None or (abs(old) < 0.005 and abs(rebuilt) >= 0.01):
            days[day_key] = rebuilt
            changed = True
    if changed:
        book["ctp_daily"] = days
    return changed


def _ctp_snap_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """数字没变就不必再存一条历史，避免每轮扫描把文件写爆。"""
    for key in _CTP_SNAP_KEYS:
        try:
            if abs(float(old.get(key) or 0) - float(new.get(key) or 0)) >= 0.005:
                return True
        except (TypeError, ValueError):
            return True
    return str(old.get("account_id") or old.get("user_id") or "") != str(
        new.get("account_id") or new.get("user_id") or ""
    )


def save_book(root: str | Path, book: dict[str, Any]) -> Path:
    """
    先写临时文件再替换，避免写到一半程序退出，账本变成半截。

    当前账期如果有自己的存档名，同一份内容也写进 data/signal_periods。
    那里是这一期的存档。上一期的文件不会在这里被盖掉。
    """
    from src.account.workspace import profile_of

    path = book_path(root)
    # 账本里记下这是哪一本，以后打开存档时能分清实盘和模拟。
    book["account_id"] = profile_of(root=root)["id"]
    _write_json(path, book)
    _mirror_period(root, book)
    _mirror_sqlite(root, book)
    return path


def _mirror_sqlite(root: str | Path, book: dict[str, Any]) -> None:
    """JSON 成功后再同步投影；SQLite 故障不得把已入账的成交改成失败。"""
    try:
        from src.account.sqlite_ledger import mirror_book_to_sqlite

        mirror_book_to_sqlite(root, book, position_rows=open_position_rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("账本 SQLite 投影失败（JSON 已保存）: {}", exc)


def begin_period(initial_capital: float, *, version: str, when: str | None = None) -> dict[str, Any]:
    """
    开始一个新账期。

    这不是在旧账上改差额。交易记录、入金、出金、每日结算和交易盈亏都不再留在这本账里。
    返回的新账只剩一笔初始资金，时间就是这个账期的起点。之后的资金变动按时间接着记。
    """
    started = when or _now()
    book = new_book(initial_capital, version=version, started_at=started)
    book["period_started_at"] = started
    book["period_file"] = _period_filename(started)
    book["changes"][0]["note"] = "新账期开始"
    return book


def start_new_period(
    root: str | Path,
    initial_capital: float,
    *,
    version: str,
    when: str | None = None,
) -> tuple[dict[str, Any], Path | None]:
    """
    先把现在这本账整份存档，再开始新账期。

    存档没写成功时，当前账本保持原样，不会被清掉。
    还没有账时，只建立新账期自己的存档。
    返回 (新账, 上一本账的存档路径)。没有旧账时，路径是 None。
    """
    with _LOCK:
        moment = when or _now()
        old = load_book(root)
        archived: Path | None = None
        if old is not None:
            snapshot = deepcopy(old)
            snapshot["closed_at"] = moment
            archived = _write_closed_period(root, snapshot, moment)
        fresh = begin_period(initial_capital, version=version, when=moment)
        if archived is not None:
            fresh["previous_archive"] = archived.name
        save_book(root, fresh)
        return fresh, archived


def set_initial_capital(book: dict[str, Any], amount: float) -> float:
    """
    把本金改成新的数。

    余额不会整本重写。只追加一笔「初始资金」，金额是新旧本金的差额。
    没变化时不记。返回这次差额。
    """
    ensure_changes(book)
    new_amount = round(float(amount), 2)
    delta = round(new_amount - _sum_kind(book, "initial"), 2)
    book["initial_capital"] = new_amount
    if abs(delta) < 0.005:
        return 0.0
    _append_change(book, "initial", delta, note=f"调成 {_money(new_amount)}")
    return delta


def add_cash(book: dict[str, Any], kind: str, amount: float, *, note: str = "") -> dict[str, Any]:
    """
    记一笔入金或出金。

    金额用正数。入金让余额变多，出金让余额变少。
    """
    if kind not in {"deposit", "withdraw"}:
        raise ValueError("只能记入金或出金")
    ensure_changes(book)
    value = round(float(amount), 2)
    if value <= 0:
        raise ValueError("金额要大于 0")
    row = {"time": _now(), "kind": kind, "amount": value, "note": note}
    book.setdefault("cashflows", []).append(row)
    # 出金记成负数，这样余额直接把每笔变动加起来。
    signed = value if kind == "deposit" else -value
    _append_change(book, kind, signed, note=note)
    return row


def settle_to(book: dict[str, Any], target: float, multipliers: dict[str, float] | None = None) -> float:
    """
    每日结算：把余额改成你填的实际账户金额。

    差额单独记一笔，不改已经记过的开平仓价格。
    返回这次差额。已经一样时返回 0，不重复记。
    """
    ensure_changes(book)
    current = balance_of(book, multipliers)
    delta = round(float(target) - current, 2)
    if abs(delta) < 0.005:
        return 0.0
    book.setdefault("cashflows", []).append(
        {
            "time": _now(),
            "kind": "settlement",
            "amount": delta,
            "note": "每日结算",
        }
    )
    _append_change(book, "settlement", delta, note="每日结算")
    return delta


def append_trade(
    book: dict[str, Any],
    *,
    symbol: str,
    offset: str,
    direction: str,
    price: float,
    volume: int,
    multiplier: float,
    reason: str = "",
    bar_id: str = "",
    when: str | None = None,
    commission: float = 0.0,
    fill_key: str = "",
    stop_price: float | None = None,
) -> bool:
    """
    追加一条程序信号。

    有 fill_key 时按报单号加累计成交量去重：同一份回报来两次只记一次，
    同一根 K 线上的两笔部分成交因为累计量不同，仍会各记一笔。
    没有 fill_key 时保持旧的 bar_id 去重。
    commission 是这一侧（开或平）的手续费，记成负的资金变动。
    返回 True 表示新记上了。
    """
    if offset not in {"开仓", "平仓"} or direction not in {"多", "空"}:
        return False
    px = round(float(price), 4)
    vol = int(volume)
    if px <= 0 or vol <= 0:
        return False
    mark = str(fill_key or "").strip()
    if mark:
        if any(str(row.get("fill_key") or "") == mark for row in book.get("trades") or []):
            logger.info("信号账户已有同一笔回报，跳过 | {} {}", symbol, mark)
            return False
    else:
        key = _trade_key(symbol, offset, direction, bar_id)
        if key and any(_trade_key(row.get("symbol"), row.get("offset"), row.get("direction"), row.get("bar_id")) == key for row in book.get("trades") or []):
            logger.info("信号账户已有同一笔，跳过 | {} {} {}", symbol, offset, bar_id)
            return False
    ensure_changes(book)
    fee = round(max(0.0, float(commission or 0)), 2)
    stop = round(float(stop_price or 0), 4)
    book.setdefault("trades", []).append(
        {
            "time": when or _now(),
            "symbol": str(symbol),
            "offset": offset,
            "direction": direction,
            "price": px,
            "volume": vol,
            "multiplier": float(multiplier or DEFAULT_MULTIPLIER),
            "reason": str(reason or ""),
            "bar_id": str(bar_id or ""),
            "commission": fee,
            "fill_key": mark,
            "stop_price": stop,
        }
    )
    moment = when or _now()
    # 华泰账的余额只认柜台回报，程序成交只记交易记录，不改资金。
    if str(book.get("funding") or "") == "ctp":
        return True
    if fee > 0:
        _append_change(
            book,
            "commission",
            -fee,
            note=f"{symbol} {offset} {vol}手",
            when=moment,
        )
    # 开仓只占着手数，余额只因手续费变化。平仓对上了，而且价差不是 0，才记一笔交易盈亏。
    # 盈亏变动的时间跟这笔平仓同一时刻，不能写成「现在」，否则按日期筛权益线会漏掉。
    if offset == "平仓":
        pnl, matched_lots = _pnl_of_last_close(book)
        if matched_lots > 0 and abs(pnl) >= 0.005:
            _append_change(
                book,
                "pnl",
                round(pnl, 2),
                note=f"{symbol} {direction} {matched_lots}手",
                when=moment,
            )
    return True


def describe(book: dict[str, Any], multipliers: dict[str, float] | None = None) -> str:
    """给界面右边看的整段说明。用大白话，不写内部函数名。"""
    ensure_changes(book)
    _trade_pnl, opens, closed, unmatched = _pair(book, multipliers)
    cash_in = _sum_kind(book, "deposit")
    cash_out = -_sum_kind(book, "withdraw")
    settle = _sum_kind(book, "settlement")
    pnl = _sum_kind(book, "pnl")
    commission = _sum_kind(book, "commission")
    bal = balance_of(book)
    wins = sum(1 for item in closed if item > 0)
    losses = sum(1 for item in closed if item < 0)
    decided = wins + losses
    win_text = "还没有平仓" if decided == 0 else f"{wins / decided * 100:.1f}%"
    # 回撤从当时已经到达过的最高余额往下量。不能用最终余额当起点，否则会把后来才赚到的钱算成已经回撤。
    points = equity_points(book, multipliers)
    peak = points[0][1] if points else 0.0
    max_dd = 0.0
    for _moment, point in points:
        peak = max(peak, point)
        max_dd = max(max_dd, peak - point)

    period_at = str(book.get("period_started_at") or book.get("started_at") or "")
    lines = [
        f"本账期从 {period_at} 开始。从这一刻起按时间统计资金。",
        f"记账版本 {book.get('version_started') or '当前版本'}。",
    ]
    if book.get("period_file"):
        lines.append(f"本账期存档    data/signal_periods/{book.get('period_file')}")
    if book.get("previous_archive"):
        lines.append(f"上一账期已存档  data/signal_periods/{book.get('previous_archive')}")
    lines.extend(
        [
            "余额变动方式：初始资金、入金、出金、每日结算差额、交易盈亏、手续费、柜台动态权益。",
            "开仓本身不改余额；手续费按品种表单扣。滑点可并进每日结算差额。",
            f"初始资金    {_money(_sum_kind(book, 'initial'))} 元",
            f"入金        {_money(cash_in)} 元",
            f"出金        {_money(cash_out)} 元",
            f"每日结算差额  {_money(settle)} 元",
            f"交易盈亏    {_money(pnl)} 元",
            f"手续费合计  {_money(commission)} 元",
            f"柜台动态权益  {_money(_sum_kind(book, 'ctp_equity'))} 元",
            f"当前余额    {_money(bal)} 元",
            f"已平仓      {len(closed)} 笔    胜率 {win_text}",
            f"最大回撤    {_money(max_dd)} 元（只看已进余额的钱，不含浮动盈亏）",
            "",
            "还没平的仓（浮动盈亏不改余额）：",
        ]
    )
    if not opens:
        lines.append("  没有")
    for row in opens:
        lines.append(
            f"  {row['time']}  {row['symbol']}  {row['direction']}  {row['left']} 手  开仓价 {_money(row['price'])}"
        )
    if unmatched:
        lines.append("")
        lines.append("这些平仓没有找到这一版之后的开仓，没有算进盈亏：")
        for row in unmatched:
            lines.append(f"  {row.get('time')}  {row.get('symbol')}  {row.get('direction')}  {row.get('volume')} 手")
    lines.append("")
    lines.append("资金变动（每一笔都写了方式）：")
    running = 0.0
    change_rows = list(book.get("changes") or [])
    if not change_rows:
        lines.append("  还没有")
    for row in change_rows:
        running = round(running + float(row.get("amount") or 0), 2)
        extra = f"  {row.get('note')}" if row.get("note") else ""
        lines.append(
            f"  {row.get('time')}  {_kind_name(row.get('kind'))}  {_signed(row.get('amount'))}  "
            f"余额 {_money(running)}{extra}"
        )
    lines.append("")
    lines.append("交易记录：")
    trades = list(book.get("trades") or [])
    if not trades:
        lines.append("  还没有。程序按信号开仓或平仓后，这里会自动出现。")
    for row in trades:
        lines.append(
            f"  {row.get('time')}  {row.get('offset')}  {row.get('direction')}  {row.get('symbol')}  "
            f"价格 {_money(row.get('price'))}  {row.get('volume')} 手"
        )
    return "\n".join(lines)


def describe_ctp_funds(book: dict[str, Any] | None) -> str:
    """华泰账右边说明：柜台资金 + 日历盈亏 + 程序成交。资金数字不按程序估的盈亏改。"""
    from src.trader.ctp_display import account_headline, account_pairs

    if not isinstance(book, dict):
        return (
            "还没有期货公司资金回报。请先打开交易台，用这一本账对应的账号登录，"
            "点「刷新持仓」或本页「从柜台刷新资金」。查到的数字会原样存到本地，不能手改。"
        )
    snap = book.get("ctp_snapshot") if isinstance(book.get("ctp_snapshot"), dict) else {}
    if not snap or (
        abs(float(snap.get("balance") or 0)) < 1e-9 and abs(float(snap.get("available") or 0)) < 1e-9
    ):
        return (
            "还没有期货公司资金回报。请先打开交易台，用这一本账对应的账号登录，"
            "点「刷新持仓」或本页「从柜台刷新资金」。查到的数字会原样存到本地，不能手改。"
        )
    repair_ctp_daily(book)
    lines = [
        account_headline(snap),
        f"资金账号 {snap.get('account_id') or snap.get('user_id') or '—'}",
        f"更新于 {snap.get('time') or '—'}",
        "",
        "账户日历用的「期货公司当日盈亏」（不会被收盘后清零的柜台快照盖掉）：",
    ]
    daily = book.get("ctp_daily") if isinstance(book.get("ctp_daily"), dict) else {}
    if not daily:
        lines.append("  还没有。白天交易时点「从柜台刷新资金」会自动记。")
    else:
        for day_key in sorted(str(k) for k in daily.keys())[-14:]:
            try:
                value = float(daily.get(day_key) or 0)
            except (TypeError, ValueError):
                continue
            lines.append(f"  {day_key}    {_signed(value)} 元")
    trades = list(book.get("trades") or [])
    lines.append("")
    lines.append(f"程序成交记录（共 {len(trades)} 笔，只作策略对照，不改柜台资金）：")
    if not trades:
        lines.append("  还没有。常驻用 CTP 成交后会出现在这里。")
    else:
        for row in trades[-40:]:
            lines.append(
                f"  {row.get('time')}  {row.get('offset')}  {row.get('direction')}  {row.get('symbol')}  "
                f"价格 {_money(row.get('price'))}  {row.get('volume')} 手"
                + (f"  {row.get('reason')}" if row.get("reason") else "")
            )
    lines.append("")
    lines.append("下面和交易台「资金」页是同一份柜台查询，原样保存：")
    for title, text in account_pairs(snap):
        lines.append(f"{title}    {text}")
    return "\n".join(lines)


def record_program_trade(
    root: str | Path,
    *,
    initial_capital: float,
    symbol: str,
    offset: str,
    direction: str,
    price: float,
    volume: int,
    multiplier: float,
    reason: str = "",
    bar_id: str = "",
    commission: float = 0.0,
    fill_key: str = "",
    stop_price: float | None = None,
) -> float | None:
    """
    程序内部记账成功后调用。

    账本还不存在就用当时的账户权益当初始资金新建，不补旧成交。
    返回记完后的余额。这一笔被跳过时返回 None。
    """
    from src.version import read_version

    path = book_path(root)
    with _LOCK:
        book = load_book(root)
        if book is None:
            # 文件还在但读不出来时，绝不能当成「还没有账」新建一本盖上去，成交记录会丢。
            if path.exists():
                logger.warning("信号账本存在但读不出来，这一笔不记账，也不新建账本：{}", path)
                return None
            book = new_book(initial_capital, version=read_version())
            logger.info(
                "信号账户从版本 {} 开始记，初始资金 {} 元",
                book["version_started"],
                _money(initial_capital),
            )
        wrote = append_trade(
            book,
            symbol=symbol,
            offset=offset,
            direction=direction,
            price=price,
            volume=volume,
            multiplier=multiplier,
            reason=reason,
            bar_id=bar_id,
            commission=commission,
            fill_key=fill_key,
            stop_price=stop_price,
        )
        # 价格、手数不对，或同一根 K 线已经记过，就不新建空账，也不改余额。
        if not wrote:
            return None
        save_book(root, book)
        bal = balance_of(book)
        from src.account.workspace import profile_of

        logger.info(
            "信号账户[{}] {} {} {} 价格 {} {} 手，余额 {} 元",
            profile_of(root=root)["short"],
            symbol,
            offset,
            direction,
            _money(price),
            volume,
            _money(bal),
        )
        return bal


def _resolve_event_book(root: str | Path, recorded_book: str) -> str:
    """把事件携带的账本归属解析成当前可写的账本名。

    迁移前的旧事件可能记的是旧账本名（ctp_live 等），用审计日志的 book_migrate
    映射翻到当前账号键；book_file 再据此定位账本文件。
    """
    target = str(recorded_book or "").strip()
    if not target:
        return ""
    try:
        from src.risk.state_store import _legacy_key_map

        mapping = _legacy_key_map(root)
    except Exception:  # noqa: BLE001
        return target
    return mapping.get(target, target)


def apply_outbox_event(root: str | Path, event: dict[str, Any]) -> None:
    """发件箱消费者：把 pending/ready 的成交补进它归属的账本，其余事件只留日志。

    按事件自带的 book_id（必要时映射到当前账号）定位账本，不要求等于当前选中账，
    这样切账号后旧账的 pending 也能补记。同一 fill_key 已存在则直接返回，崩溃重投不重复入账。
    """
    if event.get("type") != "OrderFilled":
        logger.debug("发件箱事件 {} {} {}", event.get("seq"), event.get("type"), event.get("event_id"))
        return
    payload = event.get("payload") or {}
    recorded_book = str(event.get("book_id") or payload.get("book_id") or "")
    target_book = _resolve_event_book(root, recorded_book)
    if not target_book:
        from src.account.workspace import load_active_id

        target_book = str(load_active_id(root) or "")
    fill_key = str(payload.get("fill_key") or "").strip()
    with _LOCK:
        book = load_named_book(root, target_book) if target_book else load_book(root)
        if book is None:
            raise RuntimeError(
                f"账本 {target_book} 不存在，保留发件箱记录待下次启动补记"
            )
        if fill_key and any(
            str(row.get("fill_key") or "") == fill_key for row in book.get("trades") or []
        ):
            return
        wrote = append_trade(
            book,
            symbol=str(payload.get("symbol") or ""),
            offset=str(payload.get("offset") or ""),
            direction=str(payload.get("direction") or ""),
            price=float(payload.get("price") or 0),
            volume=int(payload.get("volume") or 0),
            multiplier=float(payload.get("multiplier") or 1),
            reason=str(payload.get("reason") or ""),
            bar_id=str(payload.get("bar_id") or ""),
            commission=float(payload.get("commission") or 0),
            fill_key=fill_key,
            stop_price=payload.get("stop_price"),
        )
        if not wrote:
            return
        if target_book:
            save_named_book(root, target_book, book)
        else:
            save_book(root, book)
    logger.info(
        "发件箱补记成交到账本[{}] {} fill_key={}",
        target_book,
        payload.get("symbol"),
        fill_key,
    )


def save_manual(root: str | Path, book: dict[str, Any]) -> float:
    """界面上改初始资金、出入金、结算后保存，并返回新余额。"""
    with _LOCK:
        save_book(root, book)
        return balance_of(book)


def write_capital_to_config(balance: float) -> None:
    """
    把余额写进配置文件的账户权益。

    先读磁盘上的整份配置，只改这一项再保存，避免把界面上还没保存的别的设置盖掉。
    """
    from src.utils.config_loader import load_config, save_config

    disk = load_config()
    disk.setdefault("risk", {})["capital"] = round(float(balance), 2)
    save_config(disk)


def _stamp(text: str) -> str:
    """把时间收成可以当文件名的一段。冒号在 Windows 文件名里不能用。"""
    raw = str(text or "").strip() or "未知时间"
    cleaned = raw.replace(":", "").replace(" ", "_").replace("/", "-").replace("\\", "-")
    return "".join(ch for ch in cleaned if ch not in '<>:"|?*')


def _period_filename(started_at: str) -> str:
    """新账期的存档名。同一秒重复开始时，后面再加序号。"""
    return f"{_stamp(started_at)}.json"


def _closed_filename(started_at: str, closed_at: str) -> str:
    """旧账期封存后的文件名。起点和结束时间都写在名字里。"""
    return f"{_stamp(started_at)}_至_{_stamp(closed_at)}.json"


def book_lock() -> threading.RLock:
    """
    账本的进程级读写锁。

    需要「读账本→改→写回」的调用方（例如按柜台权益刷新余额）要自己包一层，
    否则会和成交记账线程互相覆盖。
    """
    return _LOCK


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件并落盘，再换上，避免存档只写了一半；同一进程内串行写。"""
    from src.domain.ledger import _file_busy

    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            last_exc: OSError | None = None
            for attempt in range(8):
                try:
                    os.replace(temp, path)
                    last_exc = None
                    break
                except OSError as exc:
                    last_exc = exc
                    if _file_busy(exc) and attempt < 7:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass


def _unique_period_path(folder: Path, filename: str) -> Path:
    """文件已经在时换一个不冲突的名字，不盖掉以前的存档。"""
    path = folder / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    number = 2
    while True:
        candidate = folder / f"{stem}_{number}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def _write_closed_period(root: str | Path, snapshot: dict[str, Any], closed_at: str) -> Path:
    """把上一本账原样写进存档目录。调用前当前账本还不能被换掉。"""
    folder = periods_dir(root)
    name = _closed_filename(str(snapshot.get("period_started_at") or snapshot.get("started_at") or ""), closed_at)
    path = _unique_period_path(folder, name)
    _write_json(path, snapshot)
    logger.info("上一账期已存档 {}", path)
    return path


def _mirror_period(root: str | Path, book: dict[str, Any]) -> None:
    """当前账期另存一份。没有存档名时不动目录里的旧文件。"""
    name = str(book.get("period_file") or "")
    if not name or Path(name).name != name or not name.endswith(".json"):
        return
    try:
        _write_json(periods_dir(root) / name, book)
    except OSError as exc:
        logger.warning("本账期存档没有写上: {}", exc)


def _pair(
    book: dict[str, Any],
    multipliers: dict[str, float] | None = None,
) -> tuple[float, list[dict[str, Any]], list[float], list[dict[str, Any]]]:
    """
    先开先平。

    平仓只和同品种、同方向的开仓配对。多单平仓价高于开仓价赚钱，空单相反。
    找不到开仓的平仓放进 unmatched，不编造盈亏。
    """
    _pnl, still, closed, unmatched, _rows = _walk_matches(book, multipliers)
    return _pnl, still, closed, unmatched


def matched_closes(
    book: dict[str, Any],
    multipliers: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    已经配对上的每一段平仓。

    每段带上品种、平仓时间、盈亏、手数和开仓价，方便按品种、按日期统计。
    对不上开仓的平仓不会出现在这里。
    """
    return _walk_matches(book, multipliers)[4]


def open_position_rows(
    book: dict[str, Any],
    multipliers: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    账本里还没平掉的仓，按品种合成一行。

    先按品种和方向合并。同一品种多空同时存在时取净仓，并写一条警告，
    提醒人工核对，不把两个方向都当成持仓。
    """
    still = _walk_matches(book, multipliers)[1]
    sides: dict[tuple[str, str], dict[str, Any]] = {}
    for row in still:
        left = int(row.get("left") or 0)
        if left <= 0:
            continue
        sym = str(row.get("symbol") or "")
        side = "long" if str(row.get("direction") or "") == "多" else "short"
        slot = sides.get((sym, side))
        if slot is None:
            slot = {
                "symbol": sym,
                "side": side,
                "volume": 0,
                "cost": 0.0,
                "opened_at": "",
                "stop_price": 0.0,
            }
            sides[(sym, side)] = slot
        slot["volume"] += left
        slot["cost"] += float(row.get("price") or 0) * left
        opened = str(row.get("time") or "")
        if opened and (not slot["opened_at"] or opened < slot["opened_at"]):
            slot["opened_at"] = opened
        stop = float(row.get("stop_price") or 0)
        if stop > 0 and slot["stop_price"] <= 0:
            slot["stop_price"] = stop

    out: list[dict[str, Any]] = []
    for sym in sorted({key[0] for key in sides}):
        long_slot = sides.get((sym, "long"))
        short_slot = sides.get((sym, "short"))
        long_vol = int(long_slot["volume"]) if long_slot else 0
        short_vol = int(short_slot["volume"]) if short_slot else 0
        net = long_vol - short_vol
        if long_vol > 0 and short_vol > 0:
            logger.warning(
                "信号账户同一品种同时有多空：{} 多{}手 空{}手，按净仓 {} 手校准",
                sym,
                long_vol,
                short_vol,
                net,
            )
        if net == 0:
            continue
        chosen = long_slot if net > 0 else short_slot
        if chosen is None:
            continue
        volume = abs(net)
        out.append(
            {
                "symbol": sym,
                "side": str(chosen["side"]),
                "volume": volume,
                "entry_price": round(float(chosen["cost"]) / int(chosen["volume"]), 4),
                "opened_at": str(chosen["opened_at"] or ""),
                "stop_price": float(chosen["stop_price"] or 0),
            }
        )
    return out


def today_realized_pnl(book: dict[str, Any], day: date | str | None = None) -> float:
    """
    某个交易日已经进余额的净盈亏。

    只加「交易盈亏」和「手续费」两类资金变动，入金、出金、每日结算不算。
    不传 day 时按期货交易日算：夜盘 21:00 起算下一个交易日。
    传了 day 就按那个自然日逐笔筛，给复盘和手工核对用。
    """
    target = _as_day(day)
    by_futures_day = target is None
    if target is None:
        target = futures_trading_day()
    total = 0.0
    for row in book.get("changes") or []:
        if str(row.get("kind") or "") not in {"pnl", "commission"}:
            continue
        moment = _change_moment(row.get("time"))
        if moment is None:
            continue
        if by_futures_day:
            if futures_trading_day(moment) != target:
                continue
        elif moment.date() != target:
            continue
        total += float(row.get("amount") or 0)
    return round(total, 2)


def _as_day(value: date | str | None) -> date | None:
    """把日期或账本里的时间文字收成日期。"""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return _change_day(value)


def _change_moment(value: Any) -> datetime | None:
    """账本里的时间是「年-月-日 时:分:秒」。读不出时刻就跳过。"""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _change_day(value: Any) -> date | None:
    """账本里的时间只取日期。"""
    moment = _change_moment(value)
    return moment.date() if moment is not None else None


def _walk_matches(
    book: dict[str, Any],
    multipliers: dict[str, float] | None = None,
) -> tuple[float, list[dict[str, Any]], list[float], list[dict[str, Any]], list[dict[str, Any]]]:
    """先开先平，并留下每一段配对的明细。"""
    opens: list[dict[str, Any]] = []
    closed: list[float] = []
    unmatched: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    pnl = 0.0
    table = multipliers or {}
    for row in book.get("trades") or []:
        if row.get("offset") == "开仓":
            opens.append(
                {
                    **row,
                    "left": int(row.get("volume") or 0),
                    "fee_left": max(0.0, float(row.get("commission") or 0)),
                }
            )
            continue
        if row.get("offset") != "平仓":
            continue
        need = int(row.get("volume") or 0)
        close_fee_left = max(0.0, float(row.get("commission") or 0))
        price = float(row.get("price") or 0)
        for held in opens:
            if need <= 0:
                break
            if held.get("symbol") != row.get("symbol") or held.get("direction") != row.get("direction"):
                continue
            if int(held.get("left") or 0) <= 0:
                continue
            held_left = int(held["left"])
            take = min(held_left, need)
            diff = price - float(held.get("price") or 0)
            if row.get("direction") == "空":
                diff = -diff
            mult = float(held.get("multiplier") or _lookup_multiplier(str(row.get("symbol") or ""), table))
            part = diff * take * mult
            # 开、平手续费都按本次配对手数分摊；最后一段收走尾差，重复部分成交不漏费。
            open_fee_left = max(0.0, float(held.get("fee_left") or 0))
            open_fee = open_fee_left if take == held_left else open_fee_left * take / held_left
            close_fee = close_fee_left if take == need else close_fee_left * take / need
            commission = open_fee + close_fee
            pnl += part
            closed.append(part)
            details.append(
                {
                    "symbol": str(row.get("symbol") or ""),
                    "direction": str(row.get("direction") or ""),
                    "exit_time": str(row.get("time") or ""),
                    "entry_time": str(held.get("time") or ""),
                    "entry_price": float(held.get("price") or 0),
                    "exit_price": price,
                    # 账户统计原口径仍是毛 pnl；动态凯利单独使用扣费后的 net_pnl。
                    "pnl": round(part, 2),
                    "commission": round(commission, 2),
                    "net_pnl": round(part - commission, 2),
                    "volume": take,
                    "multiplier": mult,
                }
            )
            held["left"] = held_left - take
            held["fee_left"] = max(0.0, open_fee_left - open_fee)
            close_fee_left = max(0.0, close_fee_left - close_fee)
            need -= take
        if need > 0:
            unmatched.append({**row, "volume": need})
    still = [row for row in opens if int(row.get("left") or 0) > 0]
    return pnl, still, closed, unmatched, details


def _append_change(
    book: dict[str, Any],
    kind: str,
    amount: float,
    *,
    note: str = "",
    when: str | None = None,
) -> None:
    """
    追加一笔余额变动。kind 必须是五种之一。

    when 有值时按那一时刻记账（平仓盈亏要跟成交时间一致）。
    入金、出金、结算不传 when，就用当前时间。
    """
    if kind not in _KIND_NAME:
        raise ValueError("未知的资金变动方式")
    book.setdefault("changes", []).append(
        {
            "time": str(when).strip() if when else _now(),
            "kind": kind,
            "amount": round(float(amount), 2),
            "note": note,
        }
    )


def _sum_kind(book: dict[str, Any], kind: str) -> float:
    """把同一种变动的金额加在一起。出金在账里是负数。"""
    ensure_changes(book)
    return round(
        sum(float(row.get("amount") or 0) for row in book.get("changes") or [] if row.get("kind") == kind),
        2,
    )


def _pnl_of_last_close(book: dict[str, Any]) -> tuple[float, int]:
    """
    只算刚刚追加的那笔平仓。

    返回配对上的盈亏和手数。对不上开仓时手数是 0，调用方不记交易盈亏。
    """
    trades = list(book.get("trades") or [])
    if not trades or trades[-1].get("offset") != "平仓":
        return 0.0, 0
    opens: list[dict[str, Any]] = []
    last_pnl = 0.0
    last_lots = 0
    for row in trades:
        if row.get("offset") == "开仓":
            opens.append({**row, "left": int(row.get("volume") or 0)})
            continue
        if row.get("offset") != "平仓":
            continue
        need = int(row.get("volume") or 0)
        price = float(row.get("price") or 0)
        part_sum = 0.0
        matched = 0
        for held in opens:
            if need <= 0:
                break
            if held.get("symbol") != row.get("symbol") or held.get("direction") != row.get("direction"):
                continue
            if int(held.get("left") or 0) <= 0:
                continue
            take = min(int(held["left"]), need)
            diff = price - float(held.get("price") or 0)
            if row.get("direction") == "空":
                diff = -diff
            mult = float(held.get("multiplier") or _lookup_multiplier(str(row.get("symbol") or ""), {}))
            part_sum += diff * take * mult
            held["left"] = int(held["left"]) - take
            need -= take
            matched += take
        last_pnl = part_sum
        last_lots = matched
    return last_pnl, last_lots


def _kind_name(kind: Any) -> str:
    """界面上只出现这五种中文。认不出的旧记号也原样带上，避免把钱藏起来。"""
    return _KIND_NAME.get(str(kind), str(kind or "未标明"))


def _signed(value: Any) -> str:
    """变动金额带上正负号，一眼能看出余额是加还是减。"""
    number = round(float(value or 0), 2)
    text = _money(abs(number)) if number < 0 else _money(number)
    if number > 0:
        return f"+{text}"
    if number < 0:
        return f"-{text}"
    return "0"


def _lookup_multiplier(symbol: str, multipliers: dict[str, float]) -> float:
    """旧记录没写乘数时，用品种字母去配置表里找。找不到按 10。"""
    letters = "".join(ch for ch in str(symbol) if ch.isalpha()).lower()
    if letters in multipliers:
        return float(multipliers[letters])
    return DEFAULT_MULTIPLIER


def _trade_key(symbol: Any, offset: Any, direction: Any, bar_id: Any) -> str:
    """有 K 线编号才用来去重。没有编号的单笔不去重，避免误伤两笔真成交。"""
    text = str(bar_id or "").strip()
    if not text:
        return ""
    return f"{symbol}|{offset}|{direction}|{text}"


def _money(value: Any) -> str:
    """金额最多两位小数。整数不带 .00，方便和配置里的权益对照。"""
    number = round(float(value or 0), 2)
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}"


def _now() -> str:
    """本地时间，精确到秒，和日志里的下单时间同一套格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
