"""
组合风控：跨品种、跨账号的风险热度快照与观察式检查。

功能：把各账号账本里的持仓汇总成组合快照，按配置的风险热度和品种手数上限给出结论。
原理：热度 = 各持仓「到止损的最大亏损」之和；开仓通过时把这一笔的风险记成在途预留，
      同轮其它品种也会被这笔尚未成交的风险挡住。默认 observe 只记录「本应拦截」，
      配置改成 enforce 后才真正拦单；读取失败按开仓失败关闭，平仓始终放行。
"""

from __future__ import annotations

import hashlib
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from loguru import logger

from src.account.trading_db import connect_readonly, storage_settings
from src.domain.trading_account import SELF_TEST_BOOK_ID

_OPEN_SIDES = {"buy_open", "sell_open"}
_MODES = {"off", "observe", "enforce"}


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class PortfolioLimits:
    """组合上限。0 表示该维度不限。"""

    mode: str = "observe"
    max_symbol_lots: int = 0
    # 组合风险热度上限（%权益）。0 = 不限。
    max_heat_pct: float = 0.0
    # 开仓通过后，这笔风险在内存里预留多久（秒），到期自动释放。
    pending_ttl_sec: float = 60.0

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
            max_heat_pct=_float(raw.get("max_heat_pct")),
            pending_ttl_sec=_float(raw.get("pending_ttl_sec")) or 60.0,
        )


@dataclass(frozen=True)
class PositionRow:
    """组合快照里的一行持仓。entry/stop 用于算到止损的风险。"""

    book: str
    symbol: str
    side: str
    volume: int
    entry_price: float = 0.0
    stop_price: float = 0.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    """account_id → symbol → 有符号手数（多正空负），并保留原始持仓行。"""

    books: Mapping[str, Mapping[str, int]]
    rows: tuple[PositionRow, ...] = ()

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "PortfolioSnapshot":
        books: dict[str, dict[str, int]] = {}
        positions: list[PositionRow] = []
        for row in rows:
            book = str(row.get("book_id") or row.get("account_id") or "")
            symbol = str(row.get("symbol") or "").strip().lower()
            if not book or not symbol:
                continue
            volume = _int(row.get("volume"))
            side = str(row.get("side") or "")
            signed = volume if side == "long" else -volume
            bucket = books.setdefault(book, {})
            bucket[symbol] = bucket.get(symbol, 0) + signed
            positions.append(
                PositionRow(
                    book=book,
                    symbol=symbol,
                    side=side,
                    volume=volume,
                    entry_price=_float(row.get("entry_price")),
                    stop_price=_float(row.get("stop_price")),
                )
            )
        return cls(books=books, rows=tuple(positions))

    def net_lots(self, symbol: str) -> int:
        key = str(symbol or "").strip().lower()
        return sum(int(book.get(key, 0)) for book in self.books.values())

    def gross_lots(self) -> int:
        return sum(abs(int(volume)) for book in self.books.values() for volume in book.values())

    def heat_cash(
        self,
        multiplier_of: Callable[[str], float] | None = None,
        fallback_per_position: float = 0.0,
    ) -> float:
        """
        组合风险热度：每个持仓到止损的最大亏损之和（元）。

        有入场价和止损价时按距离×乘数×手数算；缺失时按每笔风险预算兜底，
        避免把「止损未知」当成零风险。
        """
        get_mult = multiplier_of or (lambda _symbol: 1.0)
        total = 0.0
        for row in self.rows:
            if row.entry_price > 0 and row.stop_price > 0 and row.volume > 0:
                distance = abs(row.entry_price - row.stop_price)
                risk = distance * max(0.0, float(get_mult(row.symbol))) * row.volume
            else:
                risk = 0.0
            total += risk if risk > 0 else max(0.0, float(fallback_per_position))
        return total

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
    heat: float = 0.0
    heat_budget: float = 0.0
    risk_cash: float = 0.0


def check(
    snapshot: PortfolioSnapshot,
    limits: PortfolioLimits,
    *,
    symbol: str,
    side: str,
    volume: int,
    entry_price: float = 0.0,
    stop_price: float = 0.0,
    multiplier: float = 1.0,
    equity: float = 0.0,
    pending_heat: float = 0.0,
    fallback_risk: float = 0.0,
    multiplier_of: Callable[[str], float] | None = None,
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

    entry = _float(entry_price)
    stop = _float(stop_price)
    risk_cash = 0.0
    if entry > 0 and stop > 0 and volume > 0:
        risk_cash = abs(entry - stop) * max(0.0, float(multiplier)) * int(volume)
    if risk_cash <= 0:
        risk_cash = max(0.0, float(fallback_risk))

    heat_budget = max(0.0, float(equity)) * max(0.0, limits.max_heat_pct) / 100.0
    heat = snapshot.heat_cash(multiplier_of, fallback_per_position=fallback_risk)
    heat += max(0.0, float(pending_heat))
    if limits.max_heat_pct > 0 and heat + risk_cash > heat_budget + 1e-9:
        return PortfolioDecision(
            True,
            enforced,
            "portfolio_heat_limit",
            (
                f"组合风险热度 {heat + risk_cash:.0f} 元超过上限 {heat_budget:.0f} 元"
                f"（已占用 {heat:.0f}，本笔 {risk_cash:.0f}）"
            ),
            symbol_net,
            gross,
            digest,
            heat + risk_cash,
            heat_budget,
            risk_cash,
        )
    return PortfolioDecision(
        False, enforced, "portfolio_pass", "", symbol_net, gross, digest, heat, heat_budget, risk_cash
    )


class PortfolioRisk:
    """组合检查端口：从 trading.db 的持仓投影刷新快照，读失败按开仓失败关闭。"""

    def __init__(
        self,
        limits: PortfolioLimits,
        *,
        root: str | Path | None = None,
        snapshot: PortfolioSnapshot | None = None,
        cfg: dict[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limits = limits
        self.root = Path(root) if root is not None else None
        self._fixed = snapshot
        # 保留配置引用：实盘会按柜台资金改写 risk.capital，热度预算要跟着最新值走。
        self._cfg = cfg or {}
        self._clock = clock or time.monotonic
        # 在途预留：(symbol, side, 风险现金, 到期时刻)
        self._pending: list[tuple[str, str, float, float]] = []
        self._lock = threading.Lock()
        if (
            limits.mode != "off"
            and limits.max_symbol_lots <= 0
            and limits.max_heat_pct <= 0
        ):
            # 0 表示该维度不限。两个维度都没配时，开着的组合风控实际不拦任何单。
            logger.warning(
                "组合风控已开启（{}）但未配置任何上限，等同于不拦截；"
                "请设置 risk.portfolio.max_symbol_lots 或 max_heat_pct",
                limits.mode,
            )

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any] | None,
        *,
        root: str | Path,
    ) -> "PortfolioRisk":
        return cls(PortfolioLimits.from_config(cfg), root=root, cfg=cfg)

    def _equity(self) -> float:
        risk = self._cfg.get("risk") or {}
        equity = _float(risk.get("capital"))
        return equity

    def _risk_pct(self) -> float:
        risk = self._cfg.get("risk") or {}
        try:
            return max(0.0, float(risk.get("risk_per_trade_pct", 0.5)))
        except (TypeError, ValueError):
            return 0.5

    def _multiplier(self, symbol: str) -> float:
        from src.risk.sizing import PositionSizer

        return PositionSizer(self._cfg).multiplier_for(symbol)

    def _prune_locked(self, now: float) -> None:
        self._pending = [item for item in self._pending if item[3] > now]

    def release(self, symbol: str, side: str = "") -> None:
        """成交或明确失败后释放预留；side 为空时释放该品种全部方向。"""
        key = str(symbol or "").strip().lower()
        with self._lock:
            self._pending = [
                item
                for item in self._pending
                if not (item[0] == key and (not side or item[1] == side))
            ]

    def snapshot(self) -> PortfolioSnapshot | None:
        if self._fixed is not None:
            return self._fixed
        if self.root is None:
            return None
        return self._read(self.root)

    def check(
        self,
        *,
        symbol: str,
        side: str,
        volume: int,
        entry_price: float = 0.0,
        stop_price: float = 0.0,
    ) -> PortfolioDecision:
        # 关闭或平仓时不必读库：只有开仓才可能把敞口推过上限。
        if self.limits.mode == "off":
            return PortfolioDecision(False, False, "portfolio_off", "")
        if str(side) not in _OPEN_SIDES:
            return PortfolioDecision(False, False, "portfolio_skip", "")
        if self.limits.max_symbol_lots <= 0 and self.limits.max_heat_pct <= 0:
            # 没配任何上限时不读库，保持默认路径零开销。
            return PortfolioDecision(False, self.limits.mode == "enforce", "portfolio_pass", "")
        snapshot = self.snapshot()
        if snapshot is None:
            return PortfolioDecision(
                True,
                True,
                "portfolio_unavailable",
                "组合风控读取持仓失败，按开仓失败关闭；平仓不受影响",
            )
        equity = self._equity()
        if self.limits.max_heat_pct > 0 and equity <= 0:
            return PortfolioDecision(
                True,
                True,
                "portfolio_equity_unknown",
                "账户权益未知，组合风险热度无法计算，按开仓失败关闭；平仓不受影响",
            )
        risk_pct = self._risk_pct()
        fallback = equity * risk_pct / 100.0
        multiplier = self._multiplier(symbol)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            pending_heat = sum(item[2] for item in self._pending)
            decision = check(
                snapshot,
                self.limits,
                symbol=symbol,
                side=side,
                volume=volume,
                entry_price=entry_price,
                stop_price=stop_price,
                multiplier=multiplier,
                equity=equity,
                pending_heat=pending_heat,
                fallback_risk=fallback,
                multiplier_of=self._multiplier,
            )
            if not decision.blocked and self.limits.max_heat_pct > 0:
                self._pending.append(
                    (
                        str(symbol or "").strip().lower(),
                        str(side),
                        decision.risk_cash,
                        now + self.limits.pending_ttl_sec,
                    )
                )
        return decision

    @staticmethod
    def _read(root: Path) -> PortfolioSnapshot | None:
        path = storage_settings(root).db_path
        conn = connect_readonly(path)
        if conn is None:
            return None
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT book_id, symbol, side, volume, entry_price, stop_price FROM positions"
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("组合风控读取持仓失败，按开仓失败关闭: {}", exc)
            return None
        return PortfolioSnapshot.from_rows(
            {
                "book_id": row[0],
                "symbol": row[1],
                "side": row[2],
                "volume": row[3],
                "entry_price": row[4],
                "stop_price": row[5],
            }
            for row in rows
            # 程序自测账本是仿真数据，不能占真实组合敞口。
            if str(row[0]) != SELF_TEST_BOOK_ID
        )
