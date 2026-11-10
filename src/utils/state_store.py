"""
运行状态持久化
功能：保存/恢复持仓、当日盈亏、心跳时间，避免程序重启后「失忆」。
原理：写入当前账的持仓文件（实盘 data/bot_state.json，模拟账另一份）；启动时读入，成交/平仓后写回。
主流自动交易程序几乎都有 State / Position Store，属于必备模块。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.utils.trading_day import futures_trading_day


def default_state_path(project_root: Path | str) -> Path:
    """当前选中那本账的持仓文件。换账之后读的是另一份，不会把实盘仓和模拟仓混在一起。"""
    from src.account.workspace import state_file

    return state_file(project_root)


def load_state(path: Path | str) -> dict[str, Any]:
    """读取状态；文件不存在或损坏则返回空骨架。"""
    path = Path(path)
    empty = {
        "saved_at": "",
        "trading_day": str(futures_trading_day()),
        "daily_pnl": 0.0,
        "orders_today": 0,
        "open_orders_today": 0,  # 与 orders_today 一并换日清零，避免误触开仓上限
        "halted": False,
        "halt_reason": "",
        "positions": {},
        "last_entry_bars": {},
        "heartbeat_at": "",
        "last_scan_at": "",
        "last_error": "",
    }
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        # 先看文件里原来写的交易日。补默认值之后再看，会把「没写」误当成「已经是今天」。
        stored_day = str(data.get("trading_day") or "")
        for k, v in empty.items():
            data.setdefault(k, v)
        today = str(futures_trading_day())
        if stored_day == today:
            return data
        if stored_day:
            # 文件里的交易日比现在还新（比如系统时间被往回拨），不能反过来清掉。
            try:
                stored_date = date.fromisoformat(stored_day[:10])
            except ValueError:
                stored_date = None
            if stored_date is not None and stored_date > date.fromisoformat(today):
                return data
            # 只有文件里明确写着另一个交易日，才清当天的盈亏、报单次数和熔断。仓留下。
            # 夜盘 21:00 就已经算新交易日了，不是等零点。
            logger.info("新交易日，清零当日盈亏与下单计数（持仓状态保留）")
            data["daily_pnl"] = 0.0
            data["orders_today"] = 0
            data["open_orders_today"] = 0  # 以前漏清的话，会误报「今日开仓已达上限」
            data["halted"] = False
            data["halt_reason"] = ""
        data["trading_day"] = today
        # 立刻写回。界面大约每秒读一次这个文件，不写回的话每次都以为还是旧日期，日志会刷个不停。
        try:
            save_state(path, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("换日状态写回失败，下次读取还会再处理一次: {}", e)
        return data
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 bot_state 失败，使用空状态: {}", e)
        return empty


def save_state(path: Path | str, state: dict[str, Any]) -> None:
    """原子一点地写状态文件。临时文件名带进程号，避免两个进程互相覆盖。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["saved_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def snapshot_positions(strategy: Any) -> dict[str, dict[str, Any]]:
    """从策略对象导出可 JSON 的持仓字典。"""
    out: dict[str, dict[str, Any]] = {}
    positions = getattr(strategy, "positions", {}) or {}
    for sym, pos in positions.items():
        item = {
            "side": getattr(pos, "side", "flat"),
            "volume": int(getattr(pos, "volume", 0) or 0),
            "entry_price": float(getattr(pos, "entry_price", 0) or 0),
            "initial_stop": float(getattr(pos, "initial_stop", 0) or 0),
            "trail_stop": float(getattr(pos, "trail_stop", 0) or 0),
            "trail_armed": bool(getattr(pos, "trail_armed", False)),
            "opened_at": str(getattr(pos, "opened_at", "") or ""),
        }
        # 趋势跟随用持仓以来的最远价格收紧止损。重启后要带上，不然隔夜之后会忘掉已经走过的距离。
        extremes = getattr(strategy, "_extreme", None) or {}
        if sym in extremes and extremes[sym] is not None:
            item["extreme"] = float(extremes[sym])
        out[str(sym)] = item
    return out


def format_position_text(positions: dict[str, Any] | None) -> str:
    """
    把程序自己记下的仓写成一行，给顶部状态卡用。

    空仓写「空仓」。有仓写「品种 多/空手数 开仓价 止损」。
    这是程序账上的仓，不是同花顺里手工下的单。
    """
    parts: list[str] = []
    for sym, raw in (positions or {}).items():
        if not isinstance(raw, dict):
            continue
        side = str(raw.get("side") or "flat")
        volume = int(raw.get("volume") or 0)
        if side not in {"long", "short"} or volume <= 0:
            continue
        word = "多" if side == "long" else "空"
        text = f"{sym} {word}{volume}手"
        entry = float(raw.get("entry_price") or 0)
        stop = float(raw.get("trail_stop") or raw.get("initial_stop") or 0)
        if entry:
            text += f" @{entry:g}"
        if stop:
            text += f" 止损{stop:g}"
        parts.append(text)
    return " · ".join(parts) if parts else "空仓"


def open_position_rows(positions: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    列出程序账上还开着的仓。

    每一项有品种、方向、手数、开仓价。空仓和手数是 0 的不放进来。
    """
    rows: list[dict[str, Any]] = []
    for sym, raw in (positions or {}).items():
        if not isinstance(raw, dict):
            continue
        side = str(raw.get("side") or "flat")
        volume = int(raw.get("volume") or 0)
        if side not in {"long", "short"} or volume <= 0:
            continue
        rows.append(
            {
                "symbol": str(sym),
                "side": side,
                "volume": volume,
                "entry_price": float(raw.get("entry_price") or 0),
            }
        )
    return rows


def mark_position_closed(positions: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """
    把这一个品种在程序账上标成空仓。

    返回平掉前的那一笔。账上没有这个仓时返回 None。
    这里只改字典，不发单，也不写文件。
    """
    key = next((name for name in positions if str(name).lower() == str(symbol).lower()), None)
    if key is None or not isinstance(positions.get(key), dict):
        return None
    raw = positions[key]
    side = str(raw.get("side") or "flat")
    volume = int(raw.get("volume") or 0)
    if side not in {"long", "short"} or volume <= 0:
        return None
    closed = {
        "symbol": str(key),
        "side": side,
        "volume": volume,
        "entry_price": float(raw.get("entry_price") or 0),
    }
    # 方向和手数清掉。止损价也清掉，避免下次打开还显示旧止损。
    positions[key] = {
        **raw,
        "side": "flat",
        "volume": 0,
        "entry_price": 0.0,
        "initial_stop": 0.0,
        "trail_stop": 0.0,
        "trail_armed": False,
        "opened_at": "",
    }
    positions[key].pop("extreme", None)
    return closed


def flatten_strategy_position(strategy: Any, symbol: str) -> dict[str, Any] | None:
    """
    把正在运行的策略内存里的这一笔仓清成空仓。

    常驻还开着时必须改内存。只改磁盘的话，下一轮扫描会把旧仓又写回去。
    """
    positions = getattr(strategy, "positions", None) or {}
    key = next((name for name in positions if str(name).lower() == str(symbol).lower()), None)
    if key is None:
        return None
    pos = positions[key]
    side = str(getattr(pos, "side", "flat") or "flat")
    volume = int(getattr(pos, "volume", 0) or 0)
    if side not in {"long", "short"} or volume <= 0:
        return None
    closed = {
        "symbol": str(key),
        "side": side,
        "volume": volume,
        "entry_price": float(getattr(pos, "entry_price", 0) or 0),
    }
    pos.side = "flat"
    pos.volume = 0
    pos.entry_price = 0.0
    pos.initial_stop = 0.0
    pos.trail_stop = 0.0
    pos.trail_armed = False
    pos.opened_at = ""
    extremes = getattr(strategy, "_extreme", None)
    if isinstance(extremes, dict):
        extremes.pop(key, None)
    return closed


def strategy_position_open(
    strategy: Any,
    symbol: str,
    side: str,
    volume: int,
    entry_price: float,
    *,
    initial_stop: float = 0.0,
) -> bool:
    """
    给正在运行的策略内存里加一笔仓。人工在同花顺上开了仓后回填程序内存用。

    side: "long" / "short"。只改内存，不发单。
    initial_stop: 可选的策略级止损价。填了之后 trail_stop/trail_armed 一起生效。
    """
    positions = getattr(strategy, "positions", None)
    if positions is None:
        return False
    key = next((name for name in positions if str(name).lower() == str(symbol).lower()), None)
    if key is None:
        return False
    pos = positions[key]
    pos.side = side
    pos.volume = int(volume)
    pos.entry_price = float(entry_price)
    stop = float(initial_stop or 0)
    pos.initial_stop = stop
    pos.trail_stop = stop
    pos.trail_armed = stop > 0
    pos.opened_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return True


def mark_position_open(
    positions: dict[str, Any],
    symbol: str,
    side: str,
    volume: int,
    entry_price: float,
    *,
    initial_stop: float = 0.0,
) -> dict[str, Any] | None:
    """
    在磁盘 state 的持仓字典里写一笔仓。只改字典，不发单，也不写文件。

    返回写进去的那一笔（含 symbol/side/volume/entry_price/initial_stop）。
    """
    if side not in {"long", "short"} or int(volume) <= 0 or float(entry_price) <= 0:
        return None
    stop = float(initial_stop or 0)
    key = symbol
    positions[key] = {
        "side": side,
        "volume": int(volume),
        "entry_price": float(entry_price),
        "initial_stop": stop,
        "trail_stop": stop,
        "trail_armed": stop > 0,
        "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {
        "symbol": str(key),
        "side": side,
        "volume": int(volume),
        "entry_price": float(entry_price),
        "initial_stop": stop,
    }


def manual_close_without_bot(
    root: Path | str,
    *,
    symbol: str,
    exit_price: float,
    multiplier: float,
    initial_capital: float,
    reason: str = "按实际成交价平账",
) -> tuple[dict[str, Any] | None, float | None]:
    """
    常驻没在跑时，按人工填写的成交价平账。

    改磁盘上的持仓状态，把这一笔写进信号账户，再把当日盈亏对齐到账本。
    返回 (被平掉的这一笔, 新余额)。账上没有这个仓时返回 (None, None)。
    """
    path = default_state_path(root)
    state = load_state(path)
    closed = mark_position_closed(state.setdefault("positions", {}), symbol)
    if closed is None:
        return None, None

    direction = "多" if closed["side"] == "long" else "空"
    moment = datetime.now().strftime("%Y%m%d%H%M%S%f")
    # 放在函数内导入，避免和 state_store 启动时的导入顺序打结。
    from src.account.signal_book import load_book, record_program_trade, today_realized_pnl

    balance = record_program_trade(
        root,
        initial_capital=float(initial_capital or 0),
        symbol=str(closed["symbol"]),
        offset="平仓",
        direction=direction,
        price=float(exit_price),
        volume=int(closed["volume"]),
        multiplier=float(multiplier or 10),
        reason=reason,
        bar_id=f"manual-{moment}",
        fill_key=f"manual-{moment}",
    )
    book = load_book(root)
    if book is not None:
        state["daily_pnl"] = today_realized_pnl(book)
    save_state(path, state)
    return closed, balance


def manual_open_without_bot(
    root: Path | str,
    *,
    symbol: str,
    side: str,
    volume: int,
    entry_price: float,
    multiplier: float,
    initial_capital: float,
    initial_stop: float = 0.0,
    reason: str = "手工录入开仓",
) -> tuple[dict[str, Any] | None, float | None]:
    """
    常驻没在跑时，手工录入一笔开仓。

    改磁盘上的持仓状态，把这一笔写进信号账户。
    返回 (写进去的这一笔, 新余额)。参数无效时返回 (None, None)。
    initial_stop: 可选的策略级止损价。
    """
    if side not in ("long", "short") or int(volume) <= 0 or float(entry_price) <= 0:
        return None, None

    path = default_state_path(root)
    state = load_state(path)
    opened = mark_position_open(
        state.setdefault("positions", {}), symbol, side, volume, entry_price,
        initial_stop=float(initial_stop or 0),
    )
    if opened is None:
        return None, None

    direction = "多" if side == "long" else "空"
    moment = datetime.now().strftime("%Y%m%d%H%M%S%f")
    from src.account.signal_book import record_program_trade

    balance = record_program_trade(
        root,
        initial_capital=float(initial_capital or 0),
        symbol=str(symbol),
        offset="开",
        direction=direction,
        price=float(entry_price),
        volume=int(volume),
        multiplier=float(multiplier or 10),
        reason=reason,
        bar_id=f"manual-open-{moment}",
        fill_key=f"manual-open-{moment}",
    )
    save_state(path, state)
    return opened, balance


def restore_positions(strategy: Any, raw: dict[str, Any]) -> None:
    """把磁盘持仓灌回策略（同结构 PositionState）。"""
    from src.strategy.types import PositionState

    if not raw:
        return
    positions = getattr(strategy, "positions", None)
    if positions is None:
        return
    for sym, d in raw.items():
        if not isinstance(d, dict):
            continue
        side = d.get("side", "flat")
        if side not in ("flat", "long", "short"):
            side = "flat"
        positions[str(sym)] = PositionState(
            side=side,
            volume=int(d.get("volume", 0) or 0),
            entry_price=float(d.get("entry_price", 0) or 0),
            initial_stop=float(d.get("initial_stop", 0) or 0),
            trail_stop=float(d.get("trail_stop", 0) or 0),
            trail_armed=bool(d.get("trail_armed", False)),
            symbol=str(sym),
            opened_at=str(d.get("opened_at", "") or ""),
        )
        extremes = getattr(strategy, "_extreme", None)
        if isinstance(extremes, dict) and d.get("extreme") is not None and side != "flat":
            extremes[str(sym)] = float(d["extreme"])
    logger.info("已从磁盘恢复持仓: {}", list(raw.keys()))
