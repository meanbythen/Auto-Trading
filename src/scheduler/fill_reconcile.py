"""CTP 成交去重、成交入账与报单收尾。

方法挂在 FillReconcileMixin 上，共享状态由 TradingBot 提供；本模块不持有运行状态。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from loguru import logger

from src.ops import journal
from src.strategy import StrategyAction, StrategyDecision
from src.trader.order_timeline import elapsed_ms, now_iso


def ctp_fill_key(order_ref: object, filled_total: int) -> str:
    """CTP 成交去重键：同一报单、同一累计成交量只入一次账。回调与补记共用。"""
    ref = str(order_ref or "").strip()
    if not ref:
        return ""
    return f"ctp:{ref}:{int(filled_total)}"


class FillReconcileMixin:
    """柜台回报入账：同一笔只记一次，落账失败不改持仓。"""

    FILL_LOCK_TIMEOUT = 10.0
    _RECENT_FILL_LIMIT = 5000
    # 锁内二级去重：本次运行已经入账的成交键。惰性初始化，避免可变类属性共享。
    _recent_fill_keys: OrderedDict[str, float] | None = None

    def _apply_fill_accounting(
        self,
        symbol: str,
        decision: StrategyDecision,
        vol: int,
        entry_price: float,
        fill_price: float,
        *,
        fill_key: str = "",
        stop_price: float | None = None,
        fill_evidence: dict[str, Any] | None = None,
    ) -> bool:
        """更新策略仓位、风控、信号账户，并且只落盘一次。返回这一笔是否已经入账。"""
        note = getattr(self, "_note_own_fill", None)
        if callable(note):
            kind = "close" if decision.action == StrategyAction.CLOSE else "open"
            try:
                note(symbol, kind=kind)
            except TypeError:
                note(symbol)
        # 同一份回报再来一次时，账本会跳过。这里也直接返回，避免把仓位再改一遍。
        if fill_key and not self.cfg.get("dry_run", True):
            from src.account.signal_book import load_book

            book = load_book(self.root)
            if book is not None and any(
                str(row.get("fill_key") or "") == fill_key
                for row in book.get("trades") or []
            ):
                logger.info("同一笔成交回报已记过，跳过 | {} {}", symbol, fill_key)
                self._record_event(
                    "ledger_skip",
                    symbol=symbol,
                    side=decision.action.value,
                    volume=vol,
                    fill_key=fill_key,
                )
                return True

        journal.record_audit(
            "order_filled",
            trace_id=str((decision.meta or {}).get("trace_id") or ""),
            strategy_id=str(getattr(self.strategy, "name", "") or type(self.strategy).__name__),
            symbol=symbol,
            order_id=str((fill_evidence or {}).get("order_id") or ""),
            message=str(decision.reason or ""),
            payload={
                "volume": int(vol),
                "fill_price": float(fill_price or 0),
                "fill_key": fill_key,
                "action": decision.action.value,
            },
            root=self.cfg.get("_project_root"),
        )

        pos_before = self._position_for(symbol)
        side_before = getattr(pos_before, "side", "flat") if pos_before else "flat"
        entry_before = float(getattr(pos_before, "entry_price", 0) or 0) if pos_before else entry_price

        from src.risk.commission import fee_close, fee_open, is_close_today

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        fee = 0.0
        gross = 0.0
        if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            fee = fee_open(contracts, symbol, vol)
        elif decision.action == StrategyAction.CLOSE and entry_before > 0 and fill_price > 0:
            mult = self._contract_multiplier(symbol)
            if side_before == "long":
                gross = (fill_price - entry_before) * vol * mult
            elif side_before == "short":
                gross = (entry_before - fill_price) * vol * mult
            opened_at = str(getattr(pos_before, "opened_at", "") or "") if pos_before else ""
            today = is_close_today(opened_at, datetime.now())
            fee = fee_close(contracts, symbol, vol, close_today=today)

        # 1) 先写发件箱：成交事实先落库，账本写成功后再置 ready。
        # 这样程序在「柜台已成交、账本还没写」之间崩溃时，下次启动能补记。
        fill_event_id = ""
        book_id = ""
        if not self.cfg.get("dry_run", True):
            if decision.action == StrategyAction.CLOSE:
                direction = "多" if side_before == "long" else "空" if side_before == "short" else ""
                offset = "平仓"
            elif decision.action == StrategyAction.BUY_OPEN:
                direction, offset = "多", "开仓"
            elif decision.action == StrategyAction.SELL_OPEN:
                direction, offset = "空", "开仓"
            else:
                direction, offset = "", ""
            if direction and fill_key:
                from src.account.outbox import ORDER_FILLED, enqueue
                from src.domain.accounts import load_active_id

                book_id = str(load_active_id(self.root) or "")
                fill_event_id = f"fill:{book_id}:{fill_key}" if fill_key else ""
                payload = {
                    "symbol": symbol,
                    "offset": offset,
                    "direction": direction,
                    "price": float(fill_price),
                    "volume": int(vol),
                    "multiplier": float(self._contract_multiplier(symbol)),
                    "commission": float(fee),
                    "reason": str(decision.reason or ""),
                    "bar_id": str((decision.meta or {}).get("bar_id") or ""),
                    "fill_key": str(fill_key or ""),
                    "side_before": str(side_before or ""),
                    "account_id": book_id,
                    "book_id": book_id,
                }
                enqueue(
                    self.root,
                    ORDER_FILLED,
                    payload,
                    event_id=fill_event_id,
                    account_id=book_id,
                    book_id=book_id,
                    status="pending",
                )

        # 2) 再写信号账户。实盘以这本账为准；dry_run 只看信号，不写账。
        balance = None
        if not self.cfg.get("dry_run", True):
            balance = self._record_signal_account(
                symbol,
                decision,
                vol,
                fill_price,
                side_before,
                fee=fee,
                fill_key=fill_key,
                stop_price=stop_price,
            )
            if fill_event_id and balance is not None:
                from src.account.outbox import mark_ready

                mark_ready(self.root, fill_event_id)

        # 账本没记上时不能改策略/风控仓位：否则真实仓还在，程序却以为已平，
        # 重启后按账本恢复会得到「幽灵仓」。先停止开新仓，等人工处理。
        if not self.cfg.get("dry_run", True) and balance is None:
            self._ledger_failed = True
            logger.error(
                "账本拒记，保留策略/风控仓位不动 | {} {} vol={} price={}",
                symbol,
                decision.action.value,
                vol,
                fill_price,
            )
            self._record_event(
                "ledger_block_position_update",
                symbol=symbol,
                side=decision.action.value,
                volume=vol,
                price=fill_price,
            )
            return False

        # 2) 再同步策略持仓和风控净仓。
        ok = True
        try:
            self.strategy.on_filled(
                symbol,
                decision.action,
                fill_price,
                vol,
                initial_stop=decision.stop_price,
            )
        except Exception as exc:  # noqa: BLE001
            # 账本已写、策略没写：置暂停开仓，避免账实继续分叉。
            ok = False
            self._ledger_failed = True
            logger.exception("策略持仓同步失败，已暂停新开仓: {}", exc)
            self._record_event(
                "fill_apply_error",
                symbol=symbol,
                side=decision.action.value,
                volume=vol,
                error=str(exc),
            )
        if decision.action == StrategyAction.CLOSE and hasattr(self.strategy, "on_trade_closed"):
            try:
                self.strategy.on_trade_closed(
                    symbol,
                    float(gross) - float(fee),
                    volume=int(vol),
                    side=str(side_before or ""),
                    entry_price=float(entry_before or 0),
                    exit_price=float(fill_price or 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("{} 平仓后进化没跑完: {}", symbol, exc)
        if decision.action == StrategyAction.CLOSE and hasattr(self.strategy, "note_factor_pnl"):
            try:
                self.strategy.note_factor_pnl(
                    symbol,
                    float(gross) - float(fee),
                    volume=int(vol),
                    multiplier=float(self._contract_multiplier(symbol) or 0),
                    side=str(side_before or ""),
                    entry_time=str(getattr(pos_before, "opened_at", "") or "") if pos_before else "",
                    exit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    entry_price=float(entry_before or 0),
                    exit_price=float(fill_price or 0),
                    reason_exit=str(decision.reason or ""),
                    commission=float(fee or 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("{} 因子盈亏分摊没记下: {}", symbol, exc)
        bar_id = str((decision.meta or {}).get("bar_id", ""))
        if bar_id:
            # 通过公共接口记录，重启后才能认出「这根已经开过」。
            self.strategy.set_last_entry_bar(symbol, bar_id)

        if decision.action == StrategyAction.CLOSE:
            self._consume_signal_bar_after_flat(
                symbol, str((decision.meta or {}).get("bar_id", "") or "")
            )

        try:
            self.risk.on_order_filled(decision.action.value, vol, symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            logger.exception("风控净仓同步失败: {}", exc)

        if fill_event_id and direction:
            from src.account.outbox import POSITION_CHANGED, enqueue

            enqueue(
                self.root,
                POSITION_CHANGED,
                {
                    "symbol": symbol,
                    "direction": direction,
                    "offset": offset,
                    "volume": int(vol),
                    "fill_key": str(fill_key or ""),
                    "book_id": book_id,
                },
                event_id=f"position:{book_id}:{fill_key}:{symbol}",
                account_id=book_id,
                book_id=book_id,
                status="ready",
            )

        # 滑点记录：优先用发单时保存的理论价，避免 CTP 回报里的成交价覆盖信号价。
        # 只记录，不参与任何决策；出错不能影响记账。
        theory: float | None = None
        slip: float | None = None
        try:
            from src.account.slippage_log import record_fill
            from src.risk.universe_table import product_key

            meta = decision.meta or {}
            raw_theory = meta.get("theory_price")
            if raw_theory not in (None, ""):
                theory = float(raw_theory)
            elif decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
                theory = float(decision.entry_price or 0) or None
            elif decision.action == StrategyAction.CLOSE and decision.stop_price:
                theory = float(decision.stop_price)
            tick_box = contracts.get(product_key(symbol)) if isinstance(contracts, dict) else {}
            tick_raw = (tick_box or {}).get("price_tick")
            if tick_raw in (None, ""):
                trade_box: dict[str, Any] = self.cfg.get("trade") or {}
                raw_ctp = trade_box.get("ctp")
                ctp_box = raw_ctp if isinstance(raw_ctp, dict) else {}
                tick_raw = ctp_box.get("price_tick", trade_box.get("price_tick", 1))
            trade_cfg = self.cfg.get("trade") if isinstance(self.cfg.get("trade"), dict) else {}
            raw_ctp = trade_cfg.get("ctp")
            ctp_cfg = raw_ctp if isinstance(raw_ctp, dict) else {}
            slip = record_fill(
                self.root,
                symbol=symbol,
                action=decision.action.value,
                volume=vol,
                theory_price=theory,
                fill_price=fill_price,
                tick=float(tick_raw or 1),
                side_before=side_before,
                executor=str(trade_cfg.get("executor", "")),
                dry_run=bool(self.cfg.get("dry_run", True)),
                assumed_fill=str((fill_evidence or {}).get("fill_kind") or "") == "assumed_fill",
                account=str(ctp_cfg.get("account_kind") or ""),
            )
            if slip is not None:
                logger.info("滑点记录 | {} {} 理论 {} 成交 {} | {:+.2f} 跳", symbol, decision.action.value, theory, fill_price, slip)
        except Exception:  # noqa: BLE001
            logger.debug("滑点记录失败，已忽略", exc_info=True)

        # 3) 当日盈亏：CTP 跟柜台（下一轮扫描对账时刷新，成交回调里不再查一次，以免拖住报单）。
        # 其它通道跟账本。账本没记上时退回原算法。
        if self._uses_ctp_broker_funds():
            self._apply_broker_equity()
        elif balance is not None:
            from src.account.signal_book import load_book, today_realized_pnl

            book = load_book(self.root)
            if book is not None:
                self.risk.set_daily_pnl(today_realized_pnl(book))
        elif decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            if fee > 0:
                self.risk.update_pnl(-fee)
                self._record_event(
                    "commission",
                    symbol=symbol,
                    fee=fee,
                    offset="开仓",
                    daily_pnl=self.risk.daily_pnl,
                )
        elif decision.action == StrategyAction.CLOSE and entry_before > 0 and fill_price > 0:
            pnl = gross - fee
            self.risk.update_pnl(pnl)
            self._record_event(
                "pnl",
                symbol=symbol,
                pnl=pnl,
                fee=fee,
                daily_pnl=self.risk.daily_pnl,
            )

        # 真实盘先按原逻辑落一次状态，再补最后的「程序入账」节点。
        if not self.cfg.get("dry_run", True):
            self._persist_state()
        evidence = dict(fill_evidence or {})
        accounted_at = now_iso()
        fill_received_at = str(
            evidence.get("fill_received_at")
            or evidence.get("trade_received_at")
            or evidence.get("channel_result_at")
            or ""
        )
        self._record_order_timeline(
            "accounted",
            decision,
            volume=int(vol),
            ok=bool(ok),
            fill_kind=str(evidence.get("fill_kind") or "channel_fill"),
            price_kind=str(evidence.get("price_kind") or ""),
            broker_confirmed=bool(evidence.get("broker_confirmed", False)),
            order_id=str(evidence.get("order_id") or ""),
            order_ref=str(evidence.get("order_ref") or ""),
            order_sys_id=str(evidence.get("order_sys_id") or ""),
            trade_id=str(evidence.get("trade_id") or ""),
            trade_time=str(evidence.get("trade_time") or ""),
            fill_price=float(fill_price),
            theory_price=theory,
            slip_ticks=slip,
            fill_received_at=fill_received_at,
            accounted_at=accounted_at,
            stage_ms=elapsed_ms(fill_received_at, accounted_at),
            fill_to_account_ms=elapsed_ms(fill_received_at, accounted_at),
        )
        # 模拟测试只更新内存，不能把假成交写进正在用的账，也不能盖掉真实持仓文件。
        return True if self.cfg.get("dry_run", True) else ok

    def _record_signal_account(
        self,
        symbol: str,
        decision: StrategyDecision,
        vol: int,
        fill_price: float,
        side_before: str,
        *,
        fee: float = 0.0,
        fill_key: str = "",
        stop_price: float | None = None,
    ) -> float | None:
        """
        把这一笔写进信号账户，返回记完后的余额。

        只在程序自己已经记账之后调用。下单失败、只点了但没确认成交，都不会走到这里。
        模拟模式直接返回，避免完整模拟测试把假价格写进当前账本。
        开仓本身不改余额（手续费会扣）；平仓后余额变了，才写回配置里的账户权益。
        同一份回报用同一个 fill_key 再来一次时，账本会跳过，这里返回 None。
        """
        if decision.action == StrategyAction.CLOSE:
            if side_before == "long":
                direction = "多"
            elif side_before == "short":
                direction = "空"
            else:
                return None
            offset = "平仓"
        elif decision.action == StrategyAction.BUY_OPEN:
            direction = "多"
            offset = "开仓"
        elif decision.action == StrategyAction.SELL_OPEN:
            direction = "空"
            offset = "开仓"
        else:
            return None
        if self.cfg.get("dry_run", True):
            return None
        try:
            from src.account.signal_book import record_program_trade, write_capital_to_config

            before = float(self.sizer.capital)
            balance = record_program_trade(
                self.root,
                initial_capital=before,
                symbol=symbol,
                offset=offset,
                direction=direction,
                price=fill_price,
                volume=vol,
                multiplier=self._contract_multiplier(symbol),
                reason=str(decision.reason or ""),
                bar_id=str((decision.meta or {}).get("bar_id") or ""),
                commission=float(fee or 0),
                fill_key=fill_key,
                stop_price=stop_price,
            )
            if balance is None:
                self._record_event(
                    "ledger_skip",
                    symbol=symbol,
                    side=decision.action.value,
                    volume=vol,
                    fill_key=str(fill_key or ""),
                )
                return None
            # CTP 常驻时，手数和配置权益跟期货公司动态权益，不把程序估的成交盈亏写进去。
            # 成交记录仍记下，方便账户分析看开平仓；权益等柜台查询回来再对齐。
            if self._uses_ctp_broker_funds():
                self._ledger_failed = False
                return balance
            if abs(balance - before) > 0.009:
                self.sizer.capital = balance
            self.cfg.setdefault("risk", {})["capital"] = balance
            write_capital_to_config(balance)
            self._ledger_failed = False
            return balance
        except Exception as exc:  # noqa: BLE001
            self._ledger_failed = True
            self.last_error = "信号账户写入失败"
            logger.exception("信号账户记账失败，已暂停新开仓（仍可平仓）: {}", exc)
            self._record_event(
                "ledger_error",
                symbol=symbol,
                side=decision.action.value,
                volume=vol,
                error=str(exc),
            )
            return None


    def _on_ctp_fill(self, report: dict[str, Any]) -> bool:
        """CTP 回调线程入口：拿交易锁后入账，超时返回 False 交回补记路径。"""
        lock = getattr(self, "_trade_lock", None)
        if lock is None:
            return FillReconcileMixin._on_ctp_fill_locked(self, report)
        if not lock.acquire(timeout=self.FILL_LOCK_TIMEOUT):
            logger.error("成交入账等待交易锁超时，交回补记/对账 | {}", report.get("order_ref"))
            self._record_event("fill_lock_timeout", order_ref=str(report.get("order_ref") or ""))
            return False
        try:
            return self._on_ctp_fill_locked(report)
        finally:
            lock.release()

    def _on_ctp_fill_locked(self, report: dict[str, Any]) -> bool:
        """
        柜台确认成交后改内部持仓。

        回报可能比报单函数返回更早，所以真实成交只在这里记一次账。
        回报里的 filled 是累计值，已经记过的部分要从这次入账里扣掉。
        """
        offset = str(report.get("offset") or "")
        direction = str(report.get("direction") or "")
        if offset == "0":
            action = StrategyAction.BUY_OPEN if direction == "0" else StrategyAction.SELL_OPEN
        else:
            action = StrategyAction.CLOSE
        price = float(report.get("avg_price") or report.get("price") or 0)
        filled_total = int(report.get("filled") or report.get("volume") or 0)
        booked_volume = int(report.get("accounted_volume") or 0)
        volume = max(0, filled_total - booked_volume)
        if volume <= 0:
            # 这一部分已经由补记路径记过账，不重复入账。
            logger.debug("CTP 成交已入账，跳过 | {}", report.get("order_ref"))
            return True
        if price <= 0:
            logger.error("CTP 成交回报缺少价格，不记账")
            return False
        raw_meta = report.get("meta")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        meta["bar_id"] = str(report.get("bar_id") or meta.get("bar_id") or "")
        meta["trace_id"] = str(report.get("trace_id") or meta.get("trace_id") or "")
        decision = StrategyDecision(
            action=action,
            symbol=str(report.get("symbol") or ""),
            reason="CTP成交回报",
            stop_price=report.get("stop_price"),
            entry_price=price,
            meta=meta,
        )
        ref = str(report.get("order_ref") or "").strip()
        fill_key = ctp_fill_key(ref, filled_total)
        recent = getattr(self, "_recent_fill_keys", None)
        if fill_key and recent is not None and fill_key in recent:
            logger.info("同一笔成交回报已在本次运行入账，跳过 | {} {}", decision.symbol, fill_key)
            return True
        stop = report.get("stop_price")
        ok = self._apply_fill_accounting(
            decision.symbol,
            decision,
            volume,
            price,
            price,
            fill_key=fill_key,
            stop_price=float(stop) if stop else None,
            fill_evidence={
                **report,
                "fill_kind": "broker_fill",
                "price_kind": "broker_reported",
                "broker_confirmed": True,
                "order_id": ref,
                "fill_received_at": str(
                    report.get("trade_received_at") or report.get("last_trade_received_at") or ""
                ),
            },
        )
        if not ok:
            return False
        chased = getattr(self, "_close_chase_ticks", None)
        if isinstance(chased, dict):
            chased.pop(str(decision.symbol or "").strip().lower(), None)
        if fill_key:
            if recent is None:
                recent = OrderedDict()
                self._recent_fill_keys = recent
            recent[fill_key] = time.monotonic()
            while len(recent) > int(self._RECENT_FILL_LIMIT):
                recent.popitem(last=False)
        self._record_event(
            "order",
            symbol=decision.symbol,
            side=action.value,
            volume=volume,
            price=price,
            order_id=ref,
            reason=decision.reason,
        )
        return True

    def _book_ctp_leftover(
        self,
        decision: StrategyDecision,
        raw: dict[str, Any],
        fill_price: float,
        side: str,
    ) -> bool:
        """回报回调没记账时，按报单逐笔补记还没入账的成交手数。"""
        rows = raw.get("orders")
        if not isinstance(rows, list):
            return False
        booked = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            ref = str(row.get("order_ref") or "").strip()
            filled_row = int(row.get("filled") or 0)
            delta = filled_row - int(row.get("accounted_volume") or 0)
            price = float(row.get("avg_price") or 0) or fill_price
            if bool(row.get("foreign")) or delta <= 0:
                continue
            if price <= 0 or not ref:
                logger.error(
                    "CTP 有成交但没有成交价或报单号，不记账 | {} {} | {} 手",
                    decision.symbol,
                    side,
                    delta,
                )
                self._record_event(
                    "order_no_price", symbol=decision.symbol, side=side, volume=delta
                )
                continue
            self._apply_fill_accounting(
                decision.symbol,
                decision,
                delta,
                price,
                price,
                fill_key=ctp_fill_key(ref, filled_row),
                stop_price=decision.stop_price,
                fill_evidence={
                    **row,
                    "fill_kind": "broker_fill",
                    "price_kind": "broker_reported",
                    "broker_confirmed": True,
                    "order_id": ref,
                    "channel_result_at": str(raw.get("channel_result_at") or ""),
                    "fill_received_at": str(row.get("last_trade_received_at") or ""),
                },
            )
            self._record_event(
                "order",
                symbol=decision.symbol,
                side=side,
                volume=delta,
                price=price,
                order_id=ref,
                reason=decision.reason,
            )
            booked = True
        return booked

    def _finish_ctp_order(
        self,
        decision: StrategyDecision,
        vol: int,
        result: Any,
        side: str,
        size_note: str,
    ) -> None:
        """根据 CTP 回报决定记账还是继续等。部分成交按实际成交手数记账。"""
        if not result.ok:
            message = str(getattr(result, "message", "") or "")
            already_flat = "没有可平仓位" in message or "平仓量超过持仓量" in message
            if decision.action == StrategyAction.CLOSE and already_flat:
                if self._book_external_close(
                    decision.symbol, "柜台已无持仓（可能已在别处平掉），补记平仓"
                ):
                    logger.warning(
                        "{} 柜台拒绝平仓（已无仓），已补记程序账，不再重报",
                        decision.symbol,
                    )
                    return
                logger.info(
                    "{} 柜台确认已无仓（{}）。本地账本已是空仓，按成交回报处理，不告警",
                    decision.symbol,
                    message,
                )
                return
            if decision.action == StrategyAction.CLOSE and "未成交" in message:
                key = str(decision.symbol or "").strip().lower()
                chased = getattr(self, "_close_chase_ticks", None)
                if chased is None:
                    self._close_chase_ticks: dict[str, int] = {}
                    chased = self._close_chase_ticks
                chased[key] = min(8, int(chased.get(key, 0) or 0) + 2)
                logger.warning(
                    "{} 平仓未成交，下次对手价再多让 {} 跳",
                    decision.symbol,
                    chased[key],
                )
            logger.warning("CTP 报单没有成功 | {} {} | {}", decision.symbol, side, result.message)
            self._record_event(
                "order_rejected",
                symbol=decision.symbol,
                side=side,
                volume=vol,
                reason=result.message,
            )
            try:
                from src.ops.alerts import emit_alert

                emit_alert(
                    "ctp_reject",
                    f"{decision.symbol} {side}：{result.message}",
                    level="error",
                    root=self.root,
                )
            except Exception:  # noqa: BLE001
                pass
            return
        raw = result.raw or {}
        filled = int(raw.get("filled_volume") or 0)
        accounted = int(raw.get("accounted_volume") or 0)
        if "accounted_volume" not in raw and raw.get("accounted"):
            # 旧回报没有分项字段时，记过账就当作全部入账。
            accounted = filled
        leftover = max(0, filled - accounted)
        fill_price = float(raw.get("fill_price") or 0)
        booked = leftover <= 0
        if filled > 0:
            note = getattr(self, "_note_own_fill", None)
            if callable(note):
                kind = "close" if "close" in str(side) or decision.action == StrategyAction.CLOSE else "open"
                try:
                    note(decision.symbol, kind=kind)
                except TypeError:
                    note(decision.symbol)
        if leftover > 0:
            # 回报回调没记账（例如没挂记账函数），这里补记实际成交的部分。
            if isinstance(raw.get("orders"), list) and raw.get("orders"):
                # 按报单逐笔补，拆分平今/平昨时手数不会算错。
                booked = self._book_ctp_leftover(decision, raw, fill_price, side)
            else:
                ref = str(getattr(result, "order_id", "") or "").strip()
                if "," in ref:
                    # 拆成多笔报单却没有分笔信息：不猜，交给对账。
                    logger.warning(
                        "CTP 成交未入账，回报里没有可分笔的报单号，交给对账处理 | {} {} {} 手",
                        decision.symbol,
                        side,
                        leftover,
                    )
                    self._record_event(
                        "ctp_fill_unbooked",
                        symbol=decision.symbol,
                        side=side,
                        volume=leftover,
                    )
                elif fill_price <= 0:
                    logger.error(
                        "CTP 有成交但没有成交价，不记账 | {} {} | {} 手",
                        decision.symbol,
                        side,
                        leftover,
                    )
                    self._record_event(
                        "order_no_price", symbol=decision.symbol, side=side, volume=leftover
                    )
                else:
                    self._apply_fill_accounting(
                        decision.symbol,
                        decision,
                        leftover,
                        fill_price,
                        fill_price,
                        fill_key=ctp_fill_key(ref, filled),
                        stop_price=decision.stop_price,
                        fill_evidence={
                            **raw,
                            "fill_kind": "broker_fill",
                            "price_kind": "broker_reported",
                            "broker_confirmed": True,
                            "order_id": ref,
                            "order_ref": ref,
                            "fill_received_at": str(raw.get("last_trade_received_at") or ""),
                        },
                    )
                    self._record_event(
                        "order",
                        symbol=decision.symbol,
                        side=side,
                        volume=leftover,
                        price=fill_price,
                        order_id=ref,
                        reason=decision.reason,
                    )
                    booked = True
        if raw.get("filled"):
            if leftover <= 0:
                logger.info("CTP 成交已记账 | {} {} | {}", decision.symbol, side, result.message)
            if booked:
                logger.info(
                    "已执行 {} {} vol={} | {} | stop={} | {}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                    decision.reason,
                    decision.stop_price,
                    size_note or "平仓",
                )
            return
        if filled > 0:
            logger.warning(
                "CTP 只成交 {} / {} 手，剩余没有成交，先按成交部分记账 | {} {} | {}",
                filled,
                vol,
                decision.symbol,
                side,
                result.message,
            )
            self._record_event(
                "order_partial",
                symbol=decision.symbol,
                side=side,
                volume=filled,
                target=vol,
                message=result.message,
            )
            return
        if raw.get("working"):
            logger.warning(
                "CTP 回报还不是全部成交，先不改内部持仓 | {} {} | {}",
                decision.symbol,
                side,
                result.message,
            )
            self._record_event(
                "order_working",
                symbol=decision.symbol,
                side=side,
                volume=vol,
                order_id=result.order_id,
                reason=result.message,
            )
            return
        logger.warning("CTP 报单没有成交 | {} {} | {}", decision.symbol, side, result.message)
        self._record_event(
            "order_unfilled",
            symbol=decision.symbol,
            side=side,
            volume=vol,
            order_id=result.order_id,
            reason=result.message,
        )
