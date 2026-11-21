"""
组合风控：跨品种、跨账号的敞口快照与观察式检查。

功能：把各账号账本里的持仓汇总成组合快照，再按配置的敞口上限给出结论。
原理：快照从 SQLite 投影读取，检查是纯函数；默认 observe，只记录「本应拦截」，
      不改变报单结果，配置改成 enforce 后才真正拦单。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from loguru import logger

from src.account.trading_db import storage_settings
from src.domain.trading_account import SELF_TEST_BOOK_ID

_OPEN_SIDES = {"buy_open", "sell_open"}
_MODES = {"off", "observe", "enforce"}


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class PortfolioLimits:
    """组合敞口上限。0 表示该维度不限。"""

    mode: str = "observe"
    max_symbol_lots: int = 0
    max_gross_lots: int = 0

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "PortfolioLimits":
        raw = ((cfg or {}).get("risk") or {}).get("portfolio")
        if not isinstance(raw, dict):
            return cls()
        mode = str(raw.get("mode") or "observe").strip().lower()
        if mode not in _MODES:
            mode = "observe"
        return cls(
            mode=mode,
            max_symbol_lots=_int(raw.get("max_symbol_lots")),
            max_gross_lots=_int(raw.get("max_gross_lots")),
        )


@dataclass(frozen=True)
class PortfolioSnapshot:
    """account_id → symbol → 有符号手数（多正空负）。"""

    books: Mapping[str, Mapping[str, int]]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "PortfolioSnapshot":
        books: dict[str, dict[str, int]] = {}
        for row in rows:
            book = str(row.get("book_id") or row.get("account_id") or "")
            symbol = str(row.get("symbol") or "").strip().lower()
            if not book or not symbol:
                continue
            volume = _int(row.get("volume"))
            signed = volume if str(row.get("side")) == "long" else -volume
            bucket = books.setdefault(book, {})
            bucket[symbol] = bucket.get(symbol, 0) + signed
        return cls(books=books)

    def net_lots(self, symbol: str) -> int:
        key = str(symbol or "").strip().lower()
        return sum(int(book.get(key, 0)) for book in self.books.values())

    def gross_lots(self) -> int:
        return sum(abs(int(volume)) for book in self.books.values() for volume in book.values())

    def digest(self) -> str:
        text = ";".join(
            f"{book}:{symbol}:{volume}"
            for book in sorted(self.books)
            for symbol, volume in sorted(self.books[book].items())
        )
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class PortfolioDecision:
    """组合检查结论。blocked 表示规则命中；enforced 表示当前模式会真正拦单。"""

    blocked: bool
    enforced: bool
    code: str
    reason: str
    symbol_net: int = 0
    gross: int = 0
    digest: str = ""


def check(
    snapshot: PortfolioSnapshot,
    limits: PortfolioLimits,
    *,
    symbol: str,
    side: str,
    volume: int,
) -> PortfolioDecision:
    """只检查开仓；平仓只减少敞口，交给单账号闸门。"""
    digest = snapshot.digest()
    if limits.mode == "off":
        return PortfolioDecision(False, False, "portfolio_off", "", 0, 0, digest)
    if str(side) not in _OPEN_SIDES:
        return PortfolioDecision(False, False, "portfolio_skip", "", 0, 0, digest)
    delta = int(volume) if side == "buy_open" else -int(volume)
    symbol_net = snapshot.net_lots(symbol) + delta
    gross = snapshot.gross_lots() + max(0, int(volume))
    enforced = limits.mode == "enforce"
    if limits.max_symbol_lots > 0 and abs(symbol_net) > limits.max_symbol_lots:
        return PortfolioDecision(
            True,
            enforced,
            "portfolio_symbol_limit",
            f"{symbol} 组合净持仓 {symbol_net} 手超过上限 {limits.max_symbol_lots} 手",
            symbol_net,
            gross,
            digest,
        )
    if limits.max_gross_lots > 0 and gross > limits.max_gross_lots:
        return PortfolioDecision(
            True,
            enforced,
            "portfolio_gross_limit",
            f"组合总持仓 {gross} 手超过上限 {limits.max_gross_lots} 手",
            symbol_net,
            gross,
            digest,
        )
    return PortfolioDecision(False, enforced, "portfolio_pass", "", symbol_net, gross, digest)


class PortfolioRisk:
    """组合检查端口：从 trading.db 的持仓投影刷新快照，读失败就退化为空快照。"""

    def __init__(
        self,
        limits: PortfolioLimits,
        *,
        root: str | Path | None = None,
        snapshot: PortfolioSnapshot | None = None,
    ) -> None:
        self.limits = limits
        self.root = Path(root) if root is not None else None
        self._fixed = snapshot

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any] | None,
        *,
        root: str | Path,
    ) -> "PortfolioRisk":
        return cls(PortfolioLimits.from_config(cfg), root=root)

    def snapshot(self) -> PortfolioSnapshot:
        if self._fixed is not None:
            return self._fixed
        if self.root is None:
            return PortfolioSnapshot(books={})
        return self._read(self.root)

    def check(self, *, symbol: str, side: str, volume: int) -> PortfolioDecision:
        # 关闭或平仓时不必读库：只有开仓才可能把敞口推过上限。
        if self.limits.mode == "off":
            return PortfolioDecision(False, False, "portfolio_off", "")
        if str(side) not in _OPEN_SIDES:
            return PortfolioDecision(False, False, "portfolio_skip", "")
        if self.limits.max_symbol_lots <= 0 and self.limits.max_gross_lots <= 0:
            # 没配任何上限时不读库，保持默认路径零开销。
            return PortfolioDecision(False, self.limits.mode == "enforce", "portfolio_pass", "")
        return check(self.snapshot(), self.limits, symbol=symbol, side=side, volume=volume)

    @staticmethod
    def _read(root: Path) -> PortfolioSnapshot:
        path = storage_settings(root).db_path
        if not path.exists():
            return PortfolioSnapshot(books={})
        try:
            conn = sqlite3.connect(str(path), timeout=2.0)
        except sqlite3.Error as exc:
            logger.warning("组合风控读取持仓失败，按空快照处理: {}", exc)
            return PortfolioSnapshot(books={})
        try:
            rows = conn.execute("SELECT book_id, symbol, side, volume FROM positions").fetchall()
        except sqlite3.Error as exc:
            logger.warning("组合风控读取持仓失败，按空快照处理: {}", exc)
            return PortfolioSnapshot(books={})
        finally:
            conn.close()
        return PortfolioSnapshot.from_rows(
            {"book_id": row[0], "symbol": row[1], "side": row[2], "volume": row[3]}
            for row in rows
            # 程序自测账本是仿真数据，不能占真实组合敞口。
            if str(row[0]) != SELF_TEST_BOOK_ID
        )
