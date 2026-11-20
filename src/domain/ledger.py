"""
账本读取与权益计算。

功能：账本 JSON 的读取、余额、峰值、权益曲线都只在这里实现；
账户、风控和界面共用同一套口径，避免各算各的。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from src.domain.accounts import book_file
from src.domain.contracts import DEFAULT_MULTIPLIER

# 同一文件正在被另一线程替换时，Windows 会暂时 Permission denied。多试几次，不要当成账丢了。
_READ_TRIES = 8
_last_busy_warn_at: dict[str, float] = {}


def _file_busy(exc: BaseException) -> bool:
    """文件正被占用：权限不足、共享冲突、锁定。"""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        win = getattr(exc, "winerror", None)
        if win in (5, 32, 33):
            return True
        if exc.errno in (11, 13, 16):
            return True
    return False


def safe_float(value: Any, default: float = 0.0) -> float:
    """把账本里的数值读成 float，坏数据用 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def read_book_file(path: Path | str) -> dict[str, Any] | None:
    """读一份账本文件。还没有文件、文件坏了或不是对象，都返回 None。"""
    target = Path(path)
    if not target.exists():
        return None
    data: Any = None
    last_exc: BaseException | None = None
    for attempt in range(_READ_TRIES):
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            last_exc = None
            break
        except json.JSONDecodeError as exc:
            last_exc = exc
            break
        except OSError as exc:
            last_exc = exc
            if _file_busy(exc) and attempt + 1 < _READ_TRIES:
                time.sleep(0.05 * (attempt + 1))
                continue
            break
    if last_exc is not None:
        key = str(target)
        now = time.monotonic()
        if now - float(_last_busy_warn_at.get(key) or 0) >= 60:
            _last_busy_warn_at[key] = now
            logger.warning("信号账户账本读不出来: {}", last_exc)
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("cashflows", [])
    data.setdefault("trades", [])
    # 账本文件可能被手工改过，或旧版本写进了非法字符串。这里一次性把所有数值字段清洗掉，
    # 避免后面 float(row.get("amount")) 这类调用散落在各处时，一处坏数据把整个界面刷新链炸断。
    data["initial_capital"] = safe_float(data.get("initial_capital"), 0.0)
    for section in ("changes", "cashflows"):
        for row in data.get(section) or []:
            if isinstance(row, dict):
                for key in ("amount", "price", "stop_price"):
                    if key in row and not isinstance(row[key], (int, float)):
                        row[key] = safe_float(row.get(key), 0.0)
    data.setdefault("initial_capital", 0.0)
    # 1.8.0 的账本还没有逐笔变动。读出来时补上，余额和原来的算法一致。
    ensure_changes(data)
    return data


def load_active_book(root: str | Path) -> dict[str, Any] | None:
    """读当前选中那一本账。还没有账就返回 None。"""
    return read_book_file(book_file(root))


def ensure_changes(book: dict[str, Any]) -> None:
    """旧账没有逐笔变动时，按当时的本金、出入金、结算和已平仓盈亏补一张表。"""
    if isinstance(book.get("changes"), list):
        return
    book["changes"] = legacy_changes(book)


def legacy_changes(book: dict[str, Any]) -> list[dict[str, Any]]:
    """
    把 1.8.0 的账补成五类变动。

    本金记一笔初始资金。入金、出金、每日结算沿用原来的手工记录。
    已经平掉且盈亏不是 0 的，补一笔交易盈亏。这样补完后的余额和旧算法相同。
    """
    changes: list[dict[str, Any]] = [
        {
            "time": str(book.get("started_at") or ""),
            "kind": "initial",
            "amount": round(float(book.get("initial_capital") or 0), 2),
            "note": "开始记账",
        }
    ]
    for row in book.get("cashflows") or []:
        kind = str(row.get("kind") or "")
        amount = round(float(row.get("amount") or 0), 2)
        if kind == "deposit":
            signed = amount
        elif kind == "withdraw":
            signed = -amount
        elif kind == "settlement":
            signed = amount
        else:
            continue
        changes.append(
            {
                "time": str(row.get("time") or ""),
                "kind": kind,
                "amount": signed,
                "note": str(row.get("note") or ""),
            }
        )
    opens: list[dict[str, Any]] = []
    for row in book.get("trades") or []:
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
            mult = float(held.get("multiplier") or DEFAULT_MULTIPLIER)
            part_sum += diff * take * mult
            held["left"] = int(held["left"]) - take
            need -= take
            matched += take
        if matched > 0 and abs(part_sum) >= 0.005:
            changes.append(
                {
                    "time": str(row.get("time") or ""),
                    "kind": "pnl",
                    "amount": round(part_sum, 2),
                    "note": f"{row.get('symbol')} {row.get('direction')} {matched}手",
                }
            )
    return changes


def equity_points(book: dict[str, Any], multipliers: dict[str, float] | None = None) -> list[tuple[str, float]]:
    """按资金变动的先后，算出每一笔之后的余额。回撤用这条线。"""
    del multipliers
    ensure_changes(book)
    equity = 0.0
    points: list[tuple[str, float]] = []
    for row in book.get("changes") or []:
        equity = round(equity + float(row.get("amount") or 0), 2)
        points.append((str(row.get("time") or ""), equity))
    return points


def balance_of(book: dict[str, Any], multipliers: dict[str, float] | None = None) -> float:
    """当前余额。等于全部资金变动相加，未平仓的浮动盈亏不算进来。"""
    ensure_changes(book)
    # multipliers 仍留给旧账补交易盈亏时使用。已经补过的账直接加变动。
    del multipliers
    return round(sum(float(row.get("amount") or 0) for row in book.get("changes") or []), 2)


def shown_equity(book: dict[str, Any] | None, multipliers: dict[str, float] | None = None) -> float:
    """
    界面和仓位计算看到的当前权益。

    华泰账已经跟过柜台时，用柜台动态权益；同花顺账仍把资金变动加总。
    """
    if not isinstance(book, dict):
        return 0.0
    if str(book.get("funding") or "") == "ctp":
        snap_raw = book.get("ctp_snapshot")
        snap: dict[str, Any] = snap_raw if isinstance(snap_raw, dict) else {}
        try:
            bal = float(snap.get("balance") or 0)
        except (TypeError, ValueError):
            bal = 0.0
        if bal > 0:
            return round(bal, 2)
        try:
            avail = float(snap.get("available") or 0)
        except (TypeError, ValueError):
            avail = 0.0
        if avail > 0:
            return round(avail, 2)
    return balance_of(book, multipliers)


def equity_and_peak(book: dict[str, Any], multipliers: dict[str, float] | None = None) -> tuple[float, float]:
    """
    当前余额，以及这条余额曲线曾经到过的最高点。

    入金会抬高峰值，出金会让余额掉下去，回撤百分比会变大。
    和账户分析页同一条线，未平仓浮动盈亏不算。
    """
    points = equity_points(book, multipliers)
    if not points:
        return 0.0, 0.0
    last = float(points[-1][1])
    peak = float(points[0][1])
    for _moment, point in points:
        peak = max(peak, float(point))
    return last, peak


def ctp_history_equity(book: dict[str, Any] | None) -> list[tuple[str, float]]:
    """柜台动态权益按查询时间排成一条线，给账户分析画图。"""
    if not isinstance(book, dict):
        return []
    points: list[tuple[str, float]] = []
    for row in book.get("ctp_history") or []:
        if not isinstance(row, dict):
            continue
        try:
            bal = float(row.get("balance") or 0)
        except (TypeError, ValueError):
            continue
        points.append((str(row.get("time") or ""), bal))
    snap = book.get("ctp_snapshot") if isinstance(book.get("ctp_snapshot"), dict) else None
    if isinstance(snap, dict):
        try:
            bal = float(snap.get("balance") or 0)
        except (TypeError, ValueError):
            bal = 0.0
        if bal > 0:
            stamp = str(snap.get("time") or "")
            if not points or points[-1][1] != bal or points[-1][0] != stamp:
                points.append((stamp, bal))
    return points
