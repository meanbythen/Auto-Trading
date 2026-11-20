"""实盘风控应用服务：同步评估、状态恢复和决策审计。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from src.domain.accounts import load_active_id
from src.risk.guardian import RiskGuardian
from src.risk.state_store import DualRiskStateStore, RiskStateStore
from src.utils.trading_day import futures_trading_day

_DELEGATED_FIELDS = {
    "max_daily_loss",
    "min_order_interval_sec",
    "max_orders_per_day",
    "current_position",
    "positions",
    "daily_pnl",
    "orders_today",
    "open_orders_today",
    "last_order_ts",
    "last_open_ts",
    "halted",
    "halt_reason",
    "halt_kind",
    "halt_alerted",
    "extra",
    "write_alerts",
    "clock",
}


class RiskService:
    """保持 RiskGuardian 的调用契约，同时把状态和决策交给端口。"""

    def __init__(
        self,
        guardian: RiskGuardian,
        store: RiskStateStore,
        *,
        account_id: str,
        trading_day: str = "",
        decision_store: DualRiskStateStore | None = None,
    ) -> None:
        object.__setattr__(self, "guardian", guardian)
        object.__setattr__(self, "store", store)
        object.__setattr__(self, "decision_store", decision_store)
        object.__setattr__(self, "account_id", str(account_id))
        object.__setattr__(self, "trading_day", str(trading_day or futures_trading_day()))

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        *,
        root: str | Path,
        account_id: str | None = None,
    ) -> "RiskService":
        store = DualRiskStateStore(root, cfg)
        return cls(
            RiskGuardian.from_config(cfg),
            store,
            account_id=str(account_id or load_active_id(root)),
            decision_store=store,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.guardian, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _DELEGATED_FIELDS:
            setattr(self.guardian, name, value)
            return
        object.__setattr__(self, name, value)

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "account_id": self.account_id,
            "trading_day": self.trading_day,
            "daily_pnl": float(self.guardian.daily_pnl),
            "orders_today": int(self.guardian.orders_today),
            "open_orders_today": int(self.guardian.open_orders_today),
            "last_order_ts": float(self.guardian.last_order_ts),
            "last_open_ts": float(self.guardian.last_open_ts),
            "current_position": int(self.guardian.current_position),
            "positions": dict(self.guardian.positions),
            "halted": bool(self.guardian.halted),
            "halt_reason": str(self.guardian.halt_reason),
            "halt_kind": str(self.guardian.halt_kind),
            "halt_alerted": bool(self.guardian.halt_alerted),
        }

    def save_state(self, trading_day: str | None = None) -> None:
        if trading_day is not None:
            self.trading_day = str(trading_day)
        snapshot = {**self.snapshot(), "updated_at": datetime.now().astimezone().isoformat(timespec="milliseconds")}
        self.store.save(self.account_id, snapshot)

    def restore(self, fallback: dict[str, Any] | None = None) -> str:
        """优先读专用风控状态；旧版本只有 bot_state 时仍按旧字段恢复。"""
        old = dict(fallback or {})
        saved = self.store.load(self.account_id)
        source = {**old, **(saved or {})}
        self.trading_day = str(source.get("trading_day") or self.trading_day or futures_trading_day())
        self.guardian.daily_pnl = float(source.get("daily_pnl", 0) or 0)
        self.guardian.orders_today = int(source.get("orders_today", 0) or 0)
        self.guardian.open_orders_today = int(
            source.get("open_orders_today", self.guardian.orders_today) or 0
        )
        self.guardian.last_order_ts = float(source.get("last_order_ts", 0) or 0)
        self.guardian.last_open_ts = float(source.get("last_open_ts", 0) or 0)
        self.guardian.halted = bool(source.get("halted", False))
        self.guardian.halt_reason = str(source.get("halt_reason", "") or "")
        self.guardian.halt_kind = str(source.get("halt_kind", "") or "")
        self.guardian.halt_alerted = bool(source.get("halt_alerted", saved is not None))
        if saved is not None:
            self.guardian.current_position = int(saved.get("current_position", 0) or 0)
            raw_positions = saved.get("positions")
            if isinstance(raw_positions, dict):
                self.guardian.positions = {
                    str(symbol): int(volume or 0) for symbol, volume in raw_positions.items()
                }
        return self.trading_day

    def evaluate(self, intent: Any, *, now: float | None = None) -> tuple[bool, str]:
        """risk_chain 的同步端口；这里不重复写整条链的审计。"""
        return self.guardian.allow(
            str(intent.side),
            int(intent.volume),
            symbol=str(intent.symbol or "") or None,
            force=bool(getattr(intent, "force", False)),
            now=now,
        )

    def allow(
        self,
        side: str,
        volume: int,
        *,
        symbol: str | None = None,
        force: bool = False,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """单独调用 Guardian 时也留一条审计；risk_chain 走 evaluate 避免重复。"""
        ok, reason = self.guardian.allow(side, volume, symbol=symbol, force=force, now=now)
        self.record_decision(
            "guardian",
            {"symbol": symbol or "", "side": side, "volume": volume, "force": force},
            {"ok": ok, "code": "pass" if ok else "risk_block", "reason": reason},
        )
        return ok, reason

    def record_decision(self, gate: str, intent: Any, result: Any) -> None:
        if self.decision_store is None:
            return
        if is_dataclass(intent) and not isinstance(intent, type):
            payload = asdict(intent)
        elif isinstance(intent, dict):
            payload = dict(intent)
        else:
            payload = dict(vars(intent)) if hasattr(intent, "__dict__") else {}
        if isinstance(result, dict):
            ok = bool(result.get("ok"))
            code = str(result.get("code") or "")
            reason = str(result.get("reason") or "")
        else:
            ok = bool(getattr(result, "ok", False))
            code = str(getattr(result, "code", "") or "")
            reason = str(getattr(result, "reason", "") or "")
        record = {
            "decision_id": uuid4().hex,
            "account_id": self.account_id,
            "trading_day": self.trading_day,
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "gate": str(gate),
            "symbol": str(payload.get("symbol") or ""),
            "side": str(payload.get("side") or ""),
            "volume": int(payload.get("volume") or 0),
            "allowed": ok,
            "code": code,
            "reason": reason,
            "intent": payload,
            "state": self.snapshot(),
        }
        try:
            self.decision_store.append_decision(record)
        except Exception as exc:  # noqa: BLE001
            # 审计是旁路，存储异常不能改变已经作出的放行/拒绝决定。
            logger.warning("风控决策保存失败: {}", exc)

    def decisions(self, *, trading_day: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if self.decision_store is None:
            return []
        return self.decision_store.list_decisions(
            self.account_id,
            trading_day=trading_day,
            limit=limit,
        )

    def on_order_filled(
        self,
        side: str,
        volume: int,
        symbol: str | None = None,
        *,
        now: float | None = None,
    ) -> None:
        self.guardian.on_order_filled(side, volume, symbol, now=now)
        self.save_state()

    def update_pnl(self, pnl_delta: float) -> None:
        self.guardian.update_pnl(pnl_delta)
        self.save_state()

    def set_daily_pnl(self, value: float, *, log: bool = True) -> None:
        self.guardian.set_daily_pnl(value, log=log)
        self.save_state()

    def set_max_daily_loss(self, value: float) -> None:
        self.guardian.set_max_daily_loss(value)
        self.save_state()

    def set_halt(self, kind: str, reason: str) -> None:
        self.guardian.set_halt(kind, reason)
        self.save_state()

    def clear_halt(self, kind: str | None = None) -> bool:
        cleared = self.guardian.clear_halt(kind)
        self.save_state()
        return cleared

    def reset_daily(self, trading_day: str) -> None:
        self.trading_day = str(trading_day)
        self.guardian.daily_pnl = 0.0
        self.guardian.orders_today = 0
        self.guardian.open_orders_today = 0
        self.guardian.last_order_ts = 0.0
        self.guardian.last_open_ts = 0.0
        self.guardian.clear_halt("daily_loss")
        self.save_state()
