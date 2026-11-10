"""
本地K线库。

新浪每次只返回最近一段。如果下载后直接覆盖文件，更早的K线就会丢。
这里用一个本地数据库把每次下载到的K线留下来：没有的时间补上，已经有的时间用新数据更新。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import is_session_open_stamp

# 实盘和回测可能同时写入。用一把锁保证同一时间只有一方在改数据库。
_STORE_LOCK = threading.Lock()

# 同一路径复用一条连接：每次 fetch 都新建连接会让 sqlite 反复开关文件，
# 多个线程（主扫描 / 轮巡 / 审计）还会在默认 journal 模式下互等写锁。
# 调用方必须持有 _STORE_LOCK 才能进来。
_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def merge_bars(old: pd.DataFrame | None, fresh: pd.DataFrame | None) -> pd.DataFrame:
    """
    把新下载的K线和已经存着的K线合成一份。

    同一时刻以新下载的为准，这样最后一根未收完的K线会被刷新。
    新下载里没有的旧时间全部保留。
    """
    frames = []
    for frame in (old, fresh):
        cleaned = _as_bars(frame)
        if cleaned is None:
            continue
        frames.append(cleaned)
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    merged = pd.concat(frames)
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged.sort_index()


def merge_local_and_download(
    local: pd.DataFrame | None,
    fresh: pd.DataFrame | None,
    *,
    refresh_last: bool = True,
) -> pd.DataFrame:
    """
    回测区间比网上行情更早时，把本地K线和刚下载的K线接成一条。

    规则：
    - 本地已经有的时间保留本地，不让这次下载覆盖。
    - 下载里多出来的时间补上，包括比本地更早的缺口，以及比本地更新的K线。
    - refresh_last 为真时，只刷新本地最后一根。那一根可能还没收完，要用下载到的最新价。
    """
    old = _as_bars(local)
    new = _as_bars(fresh)
    if old is None:
        if new is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return new
    if new is None:
        return old

    kept = old
    if refresh_last:
        last = old.index.max()
        if last in new.index:
            kept = old.copy()
            kept.loc[last] = new.loc[last]

    extra = new[~new.index.isin(kept.index)]
    if len(extra) == 0:
        return kept.sort_index()
    # 拼表前清掉备注，避免 pandas 拿两段「改过的时间」去比相不相等。
    try:
        kept.attrs = {}
        extra = extra.copy()
        extra.attrs = {}
    except Exception:  # noqa: BLE001
        pass
    merged = pd.concat([kept, extra])
    merged = merged[~merged.index.duplicated(keep="first")]
    return merged.sort_index()


def _bar_dict(row: Any) -> dict[str, Any] | None:
    """sqlite 一行收成给人看的字典。"""
    if not row:
        return None
    ts, open_, high, low, close, volume = row
    return {
        "ts": str(ts),
        "open": float(open_ or 0),
        "high": float(high or 0),
        "low": float(low or 0),
        "close": float(close or 0),
        "volume": float(volume or 0),
    }


def _next_day(day: str) -> str:
    stamp = pd.Timestamp(day)
    return (stamp + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def _closest_row(rows: list[Any], ts: str) -> dict[str, Any] | None:
    want = pd.Timestamp(ts)
    best = None
    best_delta = None
    for row in rows:
        bar = _bar_dict(row)
        if not bar:
            continue
        delta = abs(pd.Timestamp(bar["ts"]) - want)
        if best_delta is None or delta < best_delta:
            best = bar
            best_delta = delta
    return best


def _closer_bar(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    ts: str,
) -> dict[str, Any] | None:
    if left is None:
        return right
    if right is None:
        return left
    want = pd.Timestamp(ts)
    d_left = abs(pd.Timestamp(left["ts"]) - want)
    d_right = abs(pd.Timestamp(right["ts"]) - want)
    return left if d_left <= d_right else right


def _market_price(value: Any) -> float | None:
    """
    交易所的真实报价。

    原油可以是 520.1，国债可以是 108.365，最多到小数点后三位。
    再长的一串小数是演示数据里的随机数，不写进本地库，避免和真实行情混在一起把止损算歪。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    rounded = round(number, 3)
    if abs(number - rounded) > 1e-6:
        return None
    return rounded


def aligned_bars(
    frame: pd.DataFrame | None,
    period: str,
    *,
    session_opens: tuple[int, ...] | None = None,
) -> pd.DataFrame | None:
    """
    只留下能当成真实行情的K线，并按时间留在调用方的库里。

    演示数据按当前时间往回推，15 分钟会落在 09:05、09:20 这种格子上，
    价格还经常带一长串小数。真实行情的 15 分钟在 09:15、09:30。
    两套混在一张表里时，SAR 会在 4600 和 3700 之间来回翻，止损就失真。
    整数价和三位以内的真实报价都保留。超过三位的小数丢掉。
    K 线时间戳是右标签（结束时刻），所以正好落在开盘时刻的 K 线不可能存在，
    是早期左标签版本留下的，一并丢掉，免得被当成上一段收盘。开盘时刻按品种给
    （商品 09:00/13:30/21:00，中金所 09:30/13:00）：中金所 13:30 是正常收盘时刻，
    不能按商品那套丢掉。session_opens 不传就不做这一项。
    """
    cleaned = _as_bars(frame)
    if cleaned is None:
        return None
    step = int(period) if str(period).isdigit() else 0

    def _keep(ts: pd.Timestamp, row: pd.Series) -> bool:
        stamp = pd.Timestamp(ts)
        if step > 0:
            if stamp.second or stamp.microsecond or stamp.nanosecond:
                return False
            minutes = stamp.hour * 60 + stamp.minute
            if minutes % step != 0:
                return False
            if session_opens and is_session_open_stamp(stamp, session_opens):
                return False
        for column in ("open", "high", "low", "close"):
            if _market_price(row[column]) is None:
                return False
        return True

    mask = [_keep(ts, row) for ts, row in cleaned.iterrows()]
    kept = cleaned.loc[mask].copy()
    if len(kept) == 0:
        return None
    # 写成三位小数，避免 108.365 在文件里变成一长串。同一时间以后仍按原来的规则追加。
    for column in ("open", "high", "low", "close"):
        kept[column] = kept[column].map(lambda value: round(float(value), 3))
    return kept


def _as_bars(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """空表当成没有数据。有数据时先整理列，再去掉重复时间。"""
    if frame is None or len(frame) == 0:
        return None
    cleaned = _normalize_frame(frame)
    if len(cleaned) == 0:
        return None
    cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    return cleaned.sort_index()


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """时间收成不带时区的索引，价格收成数字，方便和数据库里的旧行对齐。"""
    out = frame.copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out.index.name = "datetime"
    for column in ("open", "high", "low", "close", "volume"):
        if column not in out.columns:
            out[column] = 0.0
    out = out[["open", "high", "low", "close", "volume"]]
    out = out.loc[:, ~out.columns.duplicated(keep="last")]
    return out.apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])


class BarStore:
    """一个 sqlite 文件，按合约和周期保存全部下载过的K线。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    PRIMARY KEY (symbol, period, ts)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        """返回按路径复用的连接；调用方需持有 _STORE_LOCK。

        WAL 让读不阻塞写，实盘扫描和轮巡/审计同时取数时不会互等到 timeout。
        """
        key = str(self.path)
        conn = _CONNECTIONS.get(key)
        if conn is None:
            conn = sqlite3.connect(key, timeout=30, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError as exc:  # noqa: BLE001
                logger.warning("K线库开启 WAL 失败，退回默认日志模式: {}", exc)
            _CONNECTIONS[key] = conn
        return conn

    def count(self, symbol: str, period: str) -> int:
        """这个合约、这个周期已经存了多少根。"""
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol = ? AND period = ?",
                (symbol, period),
            ).fetchone()
        return int(row[0] if row else 0)

    def summarize(self) -> list[dict[str, Any]]:
        """每一组（合约+周期）有多少根、从哪根到哪根。不把全部K线读进内存。"""
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, period, COUNT(*) AS n, MIN(ts) AS ts_from, MAX(ts) AS ts_to
                FROM bars
                GROUP BY symbol, period
                ORDER BY symbol, period
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for symbol, period, n, ts_from, ts_to in rows:
            out.append(
                {
                    "symbol": str(symbol),
                    "period": str(period),
                    "n": int(n or 0),
                    "ts_from": str(ts_from or ""),
                    "ts_to": str(ts_to or ""),
                }
            )
        return out

    def count_where(self, where_sql: str, params: tuple[Any, ...]) -> int:
        """按条件数有多少根。where_sql 不含 WHERE。"""
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM bars WHERE {where_sql}", params).fetchone()
        return int(row[0] if row else 0)

    def keys_where(self, where_sql: str, params: tuple[Any, ...]) -> list[tuple[str, str]]:
        """按条件列出会碰到的（库存名, 周期）。"""
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT symbol, period FROM bars WHERE {where_sql}",
                params,
            ).fetchall()
        return [(str(a), str(b)) for a, b in rows]

    def delete_where(self, where_sql: str, params: tuple[Any, ...]) -> int:
        """按条件删除清洗库里的K线。返回删掉的根数。"""
        with _STORE_LOCK, self._connect() as conn:
            cur = conn.execute(f"DELETE FROM bars WHERE {where_sql}", params)
            n = int(cur.rowcount or 0)
            conn.commit()
        return n

    def load(self, symbol: str, period: str, limit: int | None = None) -> pd.DataFrame | None:
        """读出这个合约、这个周期的K线。还没有记录时返回空。

        limit 有值时只取最近这么多根，实盘扫描不用把全部历史读进内存。
        """
        if limit is None:
            sql = """
                SELECT ts, open, high, low, close, volume
                FROM bars
                WHERE symbol = ? AND period = ?
                ORDER BY ts
                """
            params: tuple[Any, ...] = (symbol, period)
        else:
            sql = """
                SELECT ts, open, high, low, close, volume
                FROM bars
                WHERE symbol = ? AND period = ?
                ORDER BY ts DESC
                LIMIT ?
                """
            params = (symbol, period, int(limit))
        with _STORE_LOCK, self._connect() as conn:
            frame = pd.read_sql_query(sql, conn, params=params)
        if frame.empty:
            return None
        frame["ts"] = pd.to_datetime(frame["ts"])
        frame = frame.set_index("ts")
        frame.index.name = "datetime"
        if limit is not None:
            frame = frame.sort_index()
        return frame

    def replace(self, symbol: str, period: str, frame: pd.DataFrame) -> int:
        """先删掉这个合约、这个周期的全部旧行，再写入整理后的K线。用来清掉混进去的假K线。"""
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                "DELETE FROM bars WHERE symbol = ? AND period = ?",
                (symbol, period),
            )
        return self.upsert(symbol, period, frame)

    def upsert(self, symbol: str, period: str, frame: pd.DataFrame) -> int:
        """
        把一批K线写进库。

        返回这次写入的行数。同一时刻已有记录时覆盖，没有则新增。
        """
        clean = _normalize_frame(frame)
        if len(clean) == 0:
            return 0
        rows = [
            (
                symbol,
                period,
                pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume) if pd.notna(row.volume) else 0.0,
            )
            for ts, row in clean.iterrows()
        ]
        with _STORE_LOCK, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO bars (symbol, period, ts, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, period, ts) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
                """,
                rows,
            )
        return len(rows)

    def fetch_one(self, symbol: str, period: str, ts: str) -> dict[str, Any] | None:
        """按精确时间取一根清洗K线。没有就返回空。"""
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM bars WHERE symbol = ? AND period = ? AND ts = ?
                """,
                (symbol, period, ts),
            ).fetchone()
        return _bar_dict(row)

    def fetch_prev(self, symbol: str, period: str, ts: str) -> dict[str, Any] | None:
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM bars WHERE symbol = ? AND period = ? AND ts < ?
                ORDER BY ts DESC LIMIT 1
                """,
                (symbol, period, ts),
            ).fetchone()
        return _bar_dict(row)

    def fetch_next(self, symbol: str, period: str, ts: str) -> dict[str, Any] | None:
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM bars WHERE symbol = ? AND period = ? AND ts > ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol, period, ts),
            ).fetchone()
        return _bar_dict(row)

    def fetch_nearest(self, symbol: str, period: str, ts: str) -> dict[str, Any] | None:
        """先精确，再同一天里最近的一根，再退到前后各取一根里更近的。"""
        hit = self.fetch_one(symbol, period, ts)
        if hit:
            return hit
        day = str(ts)[:10]
        nxt_day = _next_day(day)
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM bars WHERE symbol = ? AND period = ? AND ts >= ? AND ts < ?
                ORDER BY ts
                """,
                (symbol, period, day, nxt_day),
            ).fetchall()
        picked = _closest_row(rows, ts)
        if picked:
            return picked
        left = self.fetch_prev(symbol, period, ts)
        right = self.fetch_next(symbol, period, ts)
        return _closer_bar(left, right, ts)


class RawBarStore:
    """
    接口刚下来、还没做对齐/去脏/重拼之前的K线。

    和清洗库分开一个文件，出了问题能对照「网上原来长什么样」。
    只整理列名和数字，不丢掉对不齐的时间戳，也不修跨休市影线。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_bars (
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    PRIMARY KEY (symbol, period, source, ts)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        key = str(self.path)
        conn = _CONNECTIONS.get(key)
        if conn is None:
            conn = sqlite3.connect(key, timeout=30, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError as exc:  # noqa: BLE001
                logger.warning("原样K线库开启 WAL 失败，退回默认日志模式: {}", exc)
            _CONNECTIONS[key] = conn
        return conn

    def upsert(self, symbol: str, period: str, source: str, frame: pd.DataFrame) -> int:
        """把这一批接口原样K线写进库。同一时刻覆盖，不删更早的。"""
        numbered = _normalize_frame(frame)
        if len(numbered) == 0:
            return 0
        numbered = numbered[~numbered.index.duplicated(keep="last")].sort_index()
        src = str(source or "akshare")
        rows = [
            (
                symbol,
                period,
                src,
                pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume) if pd.notna(row.volume) else 0.0,
            )
            for ts, row in numbered.iterrows()
        ]
        with _STORE_LOCK, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO raw_bars (symbol, period, source, ts, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, period, source, ts) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
                """,
                rows,
            )
        return len(rows)

    def load(
        self,
        symbol: str,
        period: str,
        source: str,
        limit: int | None = None,
    ) -> pd.DataFrame | None:
        """读出这一家的原样K线。还没有时返回空。"""
        src = str(source or "akshare")
        if limit is None:
            sql = """
                SELECT ts, open, high, low, close, volume
                FROM raw_bars
                WHERE symbol = ? AND period = ? AND source = ?
                ORDER BY ts
                """
            params: tuple[Any, ...] = (symbol, period, src)
        else:
            sql = """
                SELECT ts, open, high, low, close, volume
                FROM raw_bars
                WHERE symbol = ? AND period = ? AND source = ?
                ORDER BY ts DESC
                LIMIT ?
                """
            params = (symbol, period, src, int(limit))
        with _STORE_LOCK, self._connect() as conn:
            frame = pd.read_sql_query(sql, conn, params=params)
        if frame.empty:
            return None
        frame["ts"] = pd.to_datetime(frame["ts"])
        frame = frame.set_index("ts")
        frame.index.name = "datetime"
        if limit is not None:
            frame = frame.sort_index()
        return frame

    def fetch_one(
        self, symbol: str, period: str, source: str, ts: str
    ) -> dict[str, Any] | None:
        """按精确时间取一根接口原样K线。"""
        src = str(source or "akshare")
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM raw_bars
                WHERE symbol = ? AND period = ? AND source = ? AND ts = ?
                """,
                (symbol, period, src, ts),
            ).fetchone()
        return _bar_dict(row)

    def fetch_prev(
        self, symbol: str, period: str, source: str, ts: str
    ) -> dict[str, Any] | None:
        src = str(source or "akshare")
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM raw_bars
                WHERE symbol = ? AND period = ? AND source = ? AND ts < ?
                ORDER BY ts DESC LIMIT 1
                """,
                (symbol, period, src, ts),
            ).fetchone()
        return _bar_dict(row)

    def fetch_next(
        self, symbol: str, period: str, source: str, ts: str
    ) -> dict[str, Any] | None:
        src = str(source or "akshare")
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM raw_bars
                WHERE symbol = ? AND period = ? AND source = ? AND ts > ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol, period, src, ts),
            ).fetchone()
        return _bar_dict(row)

    def fetch_nearest(
        self, symbol: str, period: str, source: str, ts: str
    ) -> dict[str, Any] | None:
        hit = self.fetch_one(symbol, period, source, ts)
        if hit:
            return hit
        src = str(source or "akshare")
        day = str(ts)[:10]
        nxt_day = _next_day(day)
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, open, high, low, close, volume
                FROM raw_bars
                WHERE symbol = ? AND period = ? AND source = ? AND ts >= ? AND ts < ?
                ORDER BY ts
                """,
                (symbol, period, src, day, nxt_day),
            ).fetchall()
        picked = _closest_row(rows, ts)
        if picked:
            return picked
        left = self.fetch_prev(symbol, period, source, ts)
        right = self.fetch_next(symbol, period, source, ts)
        return _closer_bar(left, right, ts)

    def count(self, symbol: str, period: str, source: str) -> int:
        src = str(source or "akshare")
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM raw_bars WHERE symbol = ? AND period = ? AND source = ?",
                (symbol, period, src),
            ).fetchone()
        return int(row[0] if row else 0)

    def summarize(self) -> list[dict[str, Any]]:
        """每一组（合约+周期+来源）原样库有多少根、从哪根到哪根。"""
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, period, source, COUNT(*) AS n, MIN(ts) AS ts_from, MAX(ts) AS ts_to
                FROM raw_bars
                GROUP BY symbol, period, source
                ORDER BY source, symbol, period
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for symbol, period, source, n, ts_from, ts_to in rows:
            out.append(
                {
                    "symbol": str(symbol),
                    "period": str(period),
                    "source": str(source or "akshare"),
                    "n": int(n or 0),
                    "ts_from": str(ts_from or ""),
                    "ts_to": str(ts_to or ""),
                }
            )
        return out

    def count_where(self, where_sql: str, params: tuple[Any, ...]) -> int:
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM raw_bars WHERE {where_sql}", params).fetchone()
        return int(row[0] if row else 0)

    def keys_where(self, where_sql: str, params: tuple[Any, ...]) -> list[tuple[str, str]]:
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT symbol, period FROM raw_bars WHERE {where_sql}",
                params,
            ).fetchall()
        return [(str(a), str(b)) for a, b in rows]

    def delete_where(self, where_sql: str, params: tuple[Any, ...]) -> int:
        """按条件删除原样库里的K线。返回删掉的根数。"""
        with _STORE_LOCK, self._connect() as conn:
            cur = conn.execute(f"DELETE FROM raw_bars WHERE {where_sql}", params)
            n = int(cur.rowcount or 0)
            conn.commit()
        return n
