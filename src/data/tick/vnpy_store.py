"""
天勤 tick 本地库。

入库走 vnpy 官方 save_tick_data()，不裸写 SQL：字段、时区、去重都由 vnpy 负责。
数据库文件放项目内（默认 data/ticks/vnpy_data.db），不写 ~/.vnpy。

时区：天勤 tick 的 datetime 已经是北京时间，但没带时区。这里只附加 Asia/Shanghai，
不改时间数值；vnpy 入库时会自己转成北京时间存成 naive 文本，读出来再转回 aware。
"""

from __future__ import annotations

import importlib
import threading
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from loguru import logger

TZ = ZoneInfo("Asia/Shanghai")

# vnpy_sqlite 在 import 时就读 SETTINGS["database.database"]，先设路径再导入。
# 同一进程换库（测试、改配置）要重新加载模块，否则还在写第一个文件。
_LOCK = threading.RLock()
_DB: Any = None
_DB_PATH = ""

_INSTALL_HINT = "tick 入库需要 vnpy 与 vnpy_sqlite，请执行 pip install vnpy vnpy_sqlite"

# 天勤只给一档盘口，也不给成交额、涨跌停。缺的字段填 0，不能少列。
_TICK_COLUMNS = (
    "last_price",
    "volume",
    "open_interest",
    "bid_price_1",
    "ask_price_1",
    "bid_volume_1",
    "ask_volume_1",
    "high_price",
    "low_price",
)


class VnpyUnavailable(RuntimeError):
    """vnpy / vnpy_sqlite 没装好。"""


def _database(path: str | Path) -> Any:
    """打开（或复用）vnpy sqlite 库。"""
    global _DB, _DB_PATH
    resolved = str(Path(path).expanduser().resolve())
    with _LOCK:
        if _DB is not None and _DB_PATH == resolved:
            return _DB
        try:
            from vnpy.trader.setting import SETTINGS
        except ImportError as exc:
            raise VnpyUnavailable(_INSTALL_HINT) from exc
        if _DB is not None:
            try:
                _DB.db.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭旧的 tick 库连接失败，已忽略")
            _DB = None
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        SETTINGS["database.database"] = resolved
        try:
            module = importlib.reload(importlib.import_module("vnpy_sqlite.sqlite_database"))
            _DB = module.SqliteDatabase()
        except ImportError as exc:
            raise VnpyUnavailable(_INSTALL_HINT) from exc
        _DB_PATH = resolved
        logger.info("tick 库就绪：{}", resolved)
        return _DB


def reset_database() -> None:
    """关掉并丢掉当前连接。测试换库、进程退出时用。"""
    global _DB, _DB_PATH
    with _LOCK:
        if _DB is not None:
            try:
                _DB.db.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 tick 库连接失败，已忽略")
        _DB = None
        _DB_PATH = ""


def split_instrument(instrument: str) -> tuple[str, Any]:
    """天勤合约 SHFE.fu2701 → (fu2701, Exchange.SHFE)。"""
    text = str(instrument or "").strip()
    if "." not in text:
        raise ValueError(f"天勤合约要写成 交易所.合约，例如 SHFE.fu2701：{instrument}")
    exchange_text, symbol = text.split(".", 1)
    try:
        from vnpy.trader.constant import Exchange
    except ImportError as exc:
        raise VnpyUnavailable(_INSTALL_HINT) from exc
    try:
        exchange = Exchange(exchange_text.strip().upper())
    except ValueError as exc:
        raise ValueError(f"vnpy 里没有这个交易所：{exchange_text}") from exc
    if not symbol.strip():
        raise ValueError(f"合约名是空的：{instrument}")
    return symbol.strip(), exchange


def _shanghai(moment: Any) -> datetime:
    """只给北京时间打 Asia/Shanghai 标签，不改数值。"""
    if isinstance(moment, str):
        moment = pd.Timestamp(moment)
    if isinstance(moment, pd.Timestamp):
        moment = moment.to_pydatetime()
    if not isinstance(moment, datetime):
        raise TypeError(f"tick 时间看不懂：{moment!r}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=TZ)
    return moment.astimezone(TZ)


def _beijing_naive(moment: Any) -> datetime:
    """vnpy 库存的是北京时间文本，查询参数也统一成 naive 北京时间。"""
    value = _shanghai(moment)
    return value.replace(tzinfo=None)


def _number(value: Any) -> float:
    """空值、NaN、坏字符串一律当 0。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return number


def to_tick_data(row: Any, symbol: str, exchange: Any) -> Any:
    """一行天勤 tick（Series / dict）转成 vnpy TickData。"""
    try:
        from vnpy.trader.object import TickData
    except ImportError as exc:
        raise VnpyUnavailable(_INSTALL_HINT) from exc
    moment = _shanghai(row.get("datetime"))
    return TickData(
        symbol=symbol,
        exchange=exchange,
        datetime=moment,
        name=str(row.get("name") or symbol),
        volume=_number(row.get("volume")),
        turnover=0.0,
        open_interest=_number(row.get("open_interest")),
        last_price=_number(row.get("last_price")),
        last_volume=0.0,
        limit_up=0.0,
        limit_down=0.0,
        open_price=0.0,
        high_price=_number(row.get("highest")),
        low_price=_number(row.get("lowest")),
        pre_close=0.0,
        bid_price_1=_number(row.get("bid_price1", row.get("bid_price_1"))),
        bid_volume_1=_number(row.get("bid_volume1", row.get("bid_volume_1"))),
        ask_price_1=_number(row.get("ask_price1", row.get("ask_price_1"))),
        ask_volume_1=_number(row.get("ask_volume1", row.get("ask_volume_1"))),
        localtime=moment,
        gateway_name="TQ",
    )


def ticks_from_frame(
    frame: pd.DataFrame | None,
    *,
    symbol: str = "",
    exchange: Any = None,
    instrument: str = "",
) -> list[Any]:
    """
    天勤 tick 表转 TickData 列表。

    instrument 写法是 SHFE.fu2701；也可以直接给 symbol + exchange。
    时间为空的行跳过；结果按时间升序排好，方便入库和后续差分。
    """
    if frame is None or len(frame) == 0:
        return []
    if instrument:
        symbol, exchange = split_instrument(instrument)
    if not symbol or exchange is None:
        raise ValueError("要么给 instrument，要么同时给 symbol 和 exchange")
    ticks: list[Any] = []
    for _, row in frame.iterrows():
        moment = row.get("datetime")
        if moment is None or pd.isna(moment):
            continue
        ticks.append(to_tick_data(row, symbol, exchange))
    ticks.sort(key=lambda item: item.datetime)
    return ticks


def save_ticks(ticks: list[Any] | None, *, database: str | Path, batch_size: int = 2000) -> int:
    """
    按批调用 vnpy save_tick_data()。

    vnpy 按 (symbol, exchange, datetime) upsert，同一时间重复写不会多出一行。
    空列表直接返回，不去打开数据库。
    """
    if not ticks:
        return 0
    db = _database(database)
    size = max(1, int(batch_size))
    saved = 0
    for start in range(0, len(ticks), size):
        # vnpy 入库时会就地改 tick 的 exchange / datetime（转字符串、转时区），
        # 传副本，避免调用方手里的对象被改坏。
        batch = [copy(item) for item in ticks[start : start + size]]
        db.save_tick_data(batch)
        saved += len(batch)
    return saved


def load_ticks(
    symbol: str,
    exchange: Any,
    start: Any = None,
    end: Any = None,
    *,
    database: str | Path,
) -> list[Any]:
    """读一段 tick。start/end 可给北京时间 naive，也可给带时区时间；返回时间为上海时区。"""
    db = _database(database)
    begin = _beijing_naive(start) if start is not None else datetime(1970, 1, 1)
    finish = _beijing_naive(end) if end is not None else datetime(2100, 1, 1)
    ticks = db.load_tick_data(symbol=symbol, exchange=exchange, start=begin, end=finish)
    for tick in ticks:
        tick.datetime = _shanghai(tick.datetime)
    return ticks


def load_tick_frame(
    symbol: str,
    exchange: Any,
    start: Any = None,
    end: Any = None,
    *,
    database: str | Path,
) -> pd.DataFrame:
    """读一段 tick，收成合成 K 线要用的表；索引是不带时区的北京时间。"""
    ticks = load_ticks(symbol, exchange, start, end, database=database)
    if not ticks:
        return pd.DataFrame(columns=list(_TICK_COLUMNS))
    rows = [
        {
            "datetime": pd.Timestamp(tick.datetime).tz_convert(TZ).tz_localize(None),
            "last_price": tick.last_price,
            "volume": tick.volume,
            "open_interest": tick.open_interest,
            "bid_price_1": tick.bid_price_1,
            "ask_price_1": tick.ask_price_1,
            "bid_volume_1": tick.bid_volume_1,
            "ask_volume_1": tick.ask_volume_1,
            "high_price": tick.high_price,
            "low_price": tick.low_price,
        }
        for tick in ticks
    ]
    frame = pd.DataFrame(rows).set_index("datetime").sort_index()
    return frame[list(_TICK_COLUMNS)]


def tick_overview(*, database: str | Path) -> list[dict[str, Any]]:
    """库里每个合约有多少条 tick、从哪到哪。"""
    db = _database(database)
    out: list[dict[str, Any]] = []
    for item in db.get_tick_overview():
        out.append(
            {
                "symbol": str(item.symbol),
                "exchange": str(getattr(item.exchange, "value", item.exchange)),
                "count": int(item.count or 0),
                "start": str(item.start or ""),
                "end": str(item.end or ""),
            }
        )
    return out
