"""
运行状态持久化
功能：保存/恢复持仓、当日盈亏、心跳时间，避免程序重启后「失忆」。
原理：写入当前账的持仓文件（实盘 data/bot_state.json，模拟账另一份）；启动时读入，成交/平仓后写回。
主流自动交易程序几乎都有 State / Position Store，属于必备模块。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from src.domain.position import PositionState
from src.utils.trading_day import futures_trading_day

# 状态文件的格式版本。旧文件没有这个字段，读出来按当前版本补上。
SCHEMA_VERSION = 2
# 一个进程内所有状态文件读改写都走同一把锁：界面线程和扫描线程不会互相覆盖。
_STATE_LOCK = threading.RLock()


def _backup_corrupt(path: Path) -> None:
    """解析失败时按修改时间留一份备份，避免后续写入把原文件覆盖后无法人工核对。"""
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        return
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    if backup.exists():
        return
    try:
        backup.write_bytes(path.read_bytes())
    except OSError as e:
        logger.error("状态文件损坏，备份到 {} 失败: {}", backup, e)
        return
    logger.error("状态文件损坏，原文件已备份到 {}，请人工核对后再恢复交易", backup)


def default_state_path(project_root: Path | str) -> Path:
    """当前选中那本账的持仓文件。换账之后读的是另一份，不会把实盘仓和模拟仓混在一起。"""
    from src.account.workspace import state_file

    return state_file(project_root)


def load_state(path: Path | str) -> dict[str, Any]:
    """读取状态；文件不存在或损坏则返回空骨架。同一进程内与写互斥。"""
    with _STATE_LOCK:
        return _load_locked(Path(path))


def _load_locked(path: Path) -> dict[str, Any]:
    """已持有状态锁：真正读文件、换日清零并写回。"""
    empty = {
        "schema_version": SCHEMA_VERSION,
        "saved_at": "",
        "trading_day": str(futures_trading_day()),
        "daily_pnl": 0.0,
        "orders_today": 0,
        "open_orders_today": 0,  # 与 orders_today 一并换日清零，避免误触开仓上限
        "halted": False,
        "halt_reason": "",
        "halt_kind": "",
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
        # 先看文件自己写的版本号，再补默认值，否则旧文件会被当成当前版本。
        try:
            stored_version = int(data.get("schema_version") or 0)
        except (TypeError, ValueError):
            stored_version = 0
        if stored_version > SCHEMA_VERSION:
            logger.warning(
                "状态文件版本 {} 比程序支持的 {} 新，按兼容方式读取，不要用旧程序覆盖",
                stored_version,
                SCHEMA_VERSION,
            )
        # 先看文件里原来写的交易日。补默认值之后再看，会把「没写」误当成「已经是今天」。
        stored_day = str(data.get("trading_day") or "")
        for k, v in empty.items():
            data.setdefault(k, v)
        data["schema_version"] = SCHEMA_VERSION
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
            # 只有文件里明确写着另一个交易日，才清当天的盈亏、报单次数和「日亏」熔断。仓留下。
            # 夜盘 21:00 就已经算新交易日了，不是等零点。
            logger.info("新交易日，清零当日盈亏与下单计数（持仓状态保留）")
            data["daily_pnl"] = 0.0
            data["orders_today"] = 0
            data["open_orders_today"] = 0  # 以前漏清的话，会误报「今日开仓已达上限」
            # 崩溃、遗留委托、对账不符必须人工核对，跨交易日也不能自动解除。
            # 旧文件没有 halt_kind 时按「未知原因」保留，交人工处理。
            if str(data.get("halt_kind") or "") == "daily_loss":
                data["halted"] = False
                data["halt_kind"] = ""
                data["halt_reason"] = ""
        data["trading_day"] = today
        # 立刻写回。界面大约每秒读一次这个文件，不写回的话每次都以为还是旧日期，日志会刷个不停。
        try:
            save_state(path, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("换日状态写回失败，下次读取还会再处理一次: {}", e)
        return data
    except Exception as e:  # noqa: BLE001
        # 解析失败不能当成「空仓、未熔断」：那会让风控按 0 持仓继续开新仓。
        # 按 state_corrupt 熔断，只允许人工核对后再解除。
        logger.error("读取状态文件失败，按熔断处理，禁止自动开仓: {}", e)
        _backup_corrupt(path)
        corrupt = dict(empty)
        corrupt["halted"] = True
        corrupt["halt_kind"] = "state_corrupt"
        corrupt["halt_reason"] = f"状态文件损坏或无法读取：{e}"
        return corrupt


def save_state(path: Path | str, state: dict[str, Any]) -> None:
    """原子写状态文件：进程内串行，临时文件 fsync 之后再替换，断电也不会留半截。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["schema_version"] = SCHEMA_VERSION
    payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
    with _STATE_LOCK:
        # 临时名带线程 id 和随机串：同进程多线程、或多开一个进程写同一份状态时不会互相踩。
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def update_state(
    path: Path | str, mutator: Any
) -> tuple[dict[str, Any], Any]:
    """
    读-改-写同一把锁。

    多个入口（界面手工补账、常驻扫描、账户刷新）改同一份状态时，
    各自先读后写会互相覆盖；统一走这里就不会丢更新。
    """
    path = Path(path)
    with _STATE_LOCK:
        state = _load_locked(path)
        result = mutator(state)
        save_state(path, state)
        return state, result


def snapshot_positions(strategy: Any) -> dict[str, dict[str, Any]]:
    """从策略对象导出可 JSON 的持仓字典。"""
    out: dict[str, dict[str, Any]] = {}
    positions = getattr(strategy, "positions", {}) or {}
    for sym, pos in positions.items():
        item = pos.to_dict()
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
    moment = datetime.now().strftime("%Y%m%d%H%M%S%f")
    # 放在函数内导入，避免和 state_store 启动时的导入顺序打结。
    from src.account.signal_book import load_book, record_program_trade, today_realized_pnl

    def _apply(state: dict[str, Any]) -> tuple[dict[str, Any] | None, float | None]:
        closed = mark_position_closed(state.setdefault("positions", {}), symbol)
        if closed is None:
            return None, None
        direction = "多" if closed["side"] == "long" else "空"
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
        return closed, balance

    _state, result = update_state(path, _apply)
    return result


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
    moment = datetime.now().strftime("%Y%m%d%H%M%S%f")
    from src.account.signal_book import record_program_trade

    def _apply(state: dict[str, Any]) -> tuple[dict[str, Any] | None, float | None]:
        opened = mark_position_open(
            state.setdefault("positions", {}),
            symbol,
            side,
            volume,
            entry_price,
            initial_stop=float(initial_stop or 0),
        )
        if opened is None:
            return None, None
        direction = "多" if side == "long" else "空"
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
        return opened, balance

    _state, result = update_state(path, _apply)
    return result


def restore_positions(strategy: Any, raw: dict[str, Any]) -> None:
    """把磁盘持仓灌回策略（同结构 PositionState）。"""
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
        positions[str(sym)] = PositionState.from_dict(str(sym), d)
        extremes = getattr(strategy, "_extreme", None)
        if isinstance(extremes, dict) and d.get("extreme") is not None and side != "flat":
            extremes[str(sym)] = float(d["extreme"])
    logger.info("已从磁盘恢复持仓: {}", list(raw.keys()))
