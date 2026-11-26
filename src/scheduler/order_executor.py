"""订单编排、风控链接入、通道执行与人工补账命令。

价格、委托状态、成交对账分别在 order_pricing / order_state / fill_reconcile。
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.kernel.risk_chain import TradeIntent, run_early_gates, run_send_gates
from src.ops import journal
from src.scheduler.fill_reconcile import FillReconcileMixin, ctp_fill_key as ctp_fill_key
from src.scheduler.order_pricing import OrderPricingMixin, ctp_opponent_limit as ctp_opponent_limit
from src.scheduler.order_state import OrderIntentMixin, OrderIntentWriteError as OrderIntentWriteError
from src.strategy import StrategyAction, StrategyDecision
from src.trader.base import Capabilities, OrderRequest
from src.trader.ctp_orders import ctp_order
from src.trader.order_timeline import elapsed_ms, now_iso


def _ops_alert(host: Any, code: str, message: str, *, level: str = "warning") -> None:
    """
    除日志外，再写一条运行页「最近告警」能看到的事。

    只在常驻对象有 root 时写文件（轻量测试宿主没有 root，不落盘）；
    写失败只记 debug，绝不反过来影响风控结论。
    """
    root = getattr(host, "root", None)
    if root is None:
        return
    try:
        from src.ops.alerts import emit_alert

        emit_alert(code, message, level=level, root=root)
    except Exception as exc:  # noqa: BLE001 - 告警是同步旁路，不能影响开仓判断
        logger.debug("运行页告警写入失败: {}", exc)


def _ops_recovery(
    host: Any,
    code: str,
    message: str,
    *,
    symbol: str = "",
) -> None:
    """
    条件恢复：补一条 info 恢复行，运行页「最近告警」自动撤掉旧的缺比例/越界告警。

    没有挂着的相关告警时什么都不写；传 symbol 时只清正文提到这条腿的那条，
    避免把另一条腿还在缺比例的告警一起撤掉。
    """
    root = getattr(host, "root", None)
    if root is None:
        return
    try:
        from src.ops.alerts import active_alert, emit_recovery

        active = active_alert(
            root,
            codes=("leg_margin_missing", "leg_alloc_overflow"),
            run_id=getattr(host, "run_id", None),
        )
        if not isinstance(active, dict):
            return
        if symbol and symbol not in str(active.get("message") or ""):
            return
        emit_recovery(code, message, root=root)
    except Exception as exc:  # noqa: BLE001 - 恢复记录是同步旁路，不能影响开仓
        logger.debug("运行页告警恢复写入失败: {}", exc)


def order_volume_cap(trader: Any) -> int | None:
    """
    交易对象的单笔最大手数。算仓时就按这个数压，避免到柜台前才被拦成废单。

    柜台渠道在 ctp_trader 发单前还会再查一次，这里读的是同一个 settings 值。
    GUI 渠道没有 settings，返回 None，不影响原来的手数。
    """
    settings = getattr(trader, "settings", None)
    if settings is None:
        return None
    try:
        value = int(getattr(settings, "max_order_volume", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class OrderExecutorMixin(OrderPricingMixin, OrderIntentMixin, FillReconcileMixin):
    """订单编排与人工命令入口；共享运行状态仍保存在 TradingBot 实例上。"""

    # 点击已发出但收尾出错：本笔按已发出算，意图文件留到人工核对。
    _order_unclean = False

    @property
    def trader_capabilities(self) -> Capabilities:
        """当前通道能力。调度只看能力，不看具体是哪一家。"""
        return getattr(self.trader, "capabilities", None) or Capabilities()

    def manual_close(
        self,
        symbol: str,
        exit_price: float,
        *,
        reason: str = "按实际成交价平账",
    ) -> tuple[dict[str, Any] | None, float | None]:
        """手工平账；与后台扫描共用交易锁。"""
        lock = getattr(self, "_trade_lock", None)
        with (lock if lock is not None else nullcontext()):
            return self._manual_close_locked(symbol, exit_price, reason=reason)

    def _manual_close_locked(
        self,
        symbol: str,
        exit_price: float,
        *,
        reason: str = "按实际成交价平账",
    ) -> tuple[dict[str, Any] | None, float | None]:
        """
        按人工填写的成交价平账，并同时更新策略、风控、bot_state 和信号账户。

        返回 (被平掉的这一笔, 新余额)。账上没有仓或价格无效时返回 (None, None)。
        """
        from src.risk.commission import fee_close, is_close_today
        from src.utils.state_store import flatten_strategy_position

        if float(exit_price) <= 0:
            return None, None

        pos = self._position_for(symbol)
        if pos is None:
            return None, None
        side = str(getattr(pos, "side", "flat") or "flat")
        volume = int(getattr(pos, "volume", 0) or 0)
        if side not in ("long", "short") or volume <= 0:
            return None, None
        opened_at = str(getattr(pos, "opened_at", "") or "")

        positions = getattr(self.strategy, "positions", {}) or {}
        booked_symbol = next(
            (name for name in positions if str(name).lower() == str(symbol).lower()),
            str(symbol),
        )

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        fee = fee_close(
            contracts,
            booked_symbol,
            volume,
            close_today=is_close_today(opened_at, datetime.now()),
        )
        decision = StrategyDecision(
            StrategyAction.CLOSE,
            booked_symbol,
            reason,
            entry_price=float(exit_price),
        )
        # 先记账；账本没记上就不能改内存，否则出现「程序空仓、实际还有仓」。
        balance = self._record_signal_account(
            booked_symbol,
            decision,
            volume,
            float(exit_price),
            side,
            fee=fee,
            fill_key=f"manual-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        )
        if balance is None and not self.cfg.get("dry_run", True):
            self._ledger_failed = True
            logger.error(
                "手工平仓账本拒记，保留程序仓位不动 | {} {} 手 @{}",
                booked_symbol,
                side,
                volume,
                exit_price,
            )
            self._record_event(
                "ledger_block_position_update",
                symbol=booked_symbol,
                side=side,
                volume=volume,
                price=float(exit_price),
                source="manual_close",
            )
            return None, None

        closed = flatten_strategy_position(self.strategy, symbol)
        if closed is None:
            logger.error("手工平仓记账成功但程序仓位未清掉，请核对: {}", booked_symbol)
            return None, balance
        self.risk.on_order_filled("close", volume, symbol=booked_symbol)
        self._consume_signal_bar_after_flat(booked_symbol)

        if self._uses_ctp_broker_funds():
            self._apply_broker_equity()
        elif balance is not None:
            from src.account.signal_book import load_book, today_realized_pnl

            book = load_book(self.root)
            if book is not None:
                self.risk.set_daily_pnl(today_realized_pnl(book))

        self._persist_state()
        self._record_event(
            "manual_close",
            symbol=booked_symbol,
            side=side,
            volume=volume,
            price=float(exit_price),
            balance=balance,
        )
        return closed, balance

    def manual_open(
        self,
        symbol: str,
        side: str,
        volume: int,
        entry_price: float,
        *,
        initial_stop: float = 0.0,
        reason: str = "手工录入开仓",
    ) -> tuple[dict[str, Any] | None, float | None]:
        """手工回填开仓；与后台扫描共用交易锁。"""
        lock = getattr(self, "_trade_lock", None)
        with (lock if lock is not None else nullcontext()):
            return self._manual_open_locked(
                symbol,
                side,
                volume,
                entry_price,
                initial_stop=initial_stop,
                reason=reason,
            )

    def _manual_open_locked(
        self,
        symbol: str,
        side: str,
        volume: int,
        entry_price: float,
        *,
        initial_stop: float = 0.0,
        reason: str = "手工录入开仓",
    ) -> tuple[dict[str, Any] | None, float | None]:
        """
        人工在交易软件上开了仓后，把这笔仓回填进程序的策略内存、风控、bot_state 和信号账户。

        side: "long" / "short"。只改程序的账，不会去点交易软件。
        initial_stop: 可选的策略级止损价（填了之后策略会按规则自动止损；0 只靠反向信号和收盘清仓）。
        返回 (写进去的这一笔, 新余额)。参数无效或品种不在关注列表里时返回 (None, None)。
        """
        from src.risk.commission import fee_open
        from src.utils.state_store import strategy_position_open

        if side not in ("long", "short") or int(volume) <= 0 or float(entry_price) <= 0:
            return None, None

        # 品种必须在程序内存里已有 position state（启动时就初始化了），
        # 否则 runner 下一轮扫描可能把这笔仓当成"程序没有的"处理掉。
        positions = getattr(self.strategy, "positions", None) or {}
        key = next((name for name in positions if str(name).lower() == str(symbol).lower()), None)
        if key is None:
            return None, None

        booked_symbol = str(key)

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        fee = fee_open(contracts, booked_symbol, int(volume))

        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        decision = StrategyDecision(
            action,
            booked_symbol,
            reason,
            entry_price=float(entry_price),
            stop_price=float(initial_stop or 0),
        )
        # 先记账；账本没记上就不能改内存，否则程序会凭空多出一笔仓。
        balance = self._record_signal_account(
            booked_symbol,
            decision,
            int(volume),
            float(entry_price),
            "flat",  # 开仓前是空仓
            fee=fee,
            fill_key=f"manual-open-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        )
        if balance is None and not self.cfg.get("dry_run", True):
            self._ledger_failed = True
            logger.error(
                "手工开仓账本拒记，不写入程序仓位 | {} {} 手 @{}",
                booked_symbol,
                side,
                volume,
                entry_price,
            )
            self._record_event(
                "ledger_block_position_update",
                symbol=booked_symbol,
                side=side,
                volume=int(volume),
                price=float(entry_price),
                source="manual_open",
            )
            return None, None

        ok = strategy_position_open(
            self.strategy, booked_symbol, side, int(volume), float(entry_price),
            initial_stop=float(initial_stop or 0),
        )
        if not ok:
            logger.error("手工开仓记账成功但程序仓位未写入，请核对: {}", booked_symbol)
            return None, balance

        fill_side = "buy_open" if side == "long" else "sell_open"
        self.risk.on_order_filled(fill_side, int(volume), symbol=booked_symbol)

        self._persist_state()
        self._record_event(
            "manual_open",
            symbol=booked_symbol,
            side=side,
            volume=int(volume),
            price=float(entry_price),
            balance=balance,
        )
        refresh = getattr(self, "_refresh_scan_symbols", None)
        if callable(refresh):
            refresh()
        return (
            {
                "symbol": booked_symbol,
                "side": side,
                "volume": int(volume),
                "entry_price": float(entry_price),
            },
            balance,
        )

    def adopt_broker_position(self, symbol: str) -> tuple[bool, str]:
        """把柜台有、账本没有的持仓认领进信号账户（只补账本，不重复加内存仓位）。"""
        lock = getattr(self, "_trade_lock", None)
        with (lock if lock is not None else nullcontext()):
            pos = self._position_for(symbol)
            side = str(getattr(pos, "side", "flat") or "flat") if pos is not None else "flat"
            volume = int(getattr(pos, "volume", 0) or 0) if pos is not None else 0
            if pos is None or side not in ("long", "short") or volume <= 0:
                return False, "没有可认领的持仓"
            price = float(self.kline.latest_price(symbol) or 0)
            if price <= 0:
                price = float(getattr(pos, "entry_price", 0) or 0)
            if price <= 0:
                return False, "拿不到价格，无法认领"
            positions = getattr(self.strategy, "positions", {}) or {}
            booked = next(
                (name for name in positions if str(name).lower() == str(symbol).lower()),
                str(symbol),
            )
            decision = StrategyDecision(
                StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN,
                booked,
                "对账认领柜台持仓",
            )
            balance = self._record_signal_account(booked, decision, volume, price, "flat")
            if balance is None and not self.cfg.get("dry_run", True):
                self._ledger_failed = True
                logger.error(
                    "对账认领账本拒记，仍保持冻结 | {} {} 手 @{}", booked, side, price
                )
                self._record_event(
                    "ledger_block_position_update",
                    symbol=booked,
                    side=side,
                    volume=volume,
                    price=price,
                    source="adopt_broker_position",
                )
                return False, "账本没记上，没有认领"
            pos.origin = ""
            self._release_frozen_symbol(symbol)
            self._persist_state()
            self._record_event(
                "position_adopted",
                symbol=booked,
                side=side,
                volume=volume,
                price=price,
                balance=balance,
            )
            return True, f"已认领 {booked} {side} {volume} 手 @{price:g}"

    def close_broker_position(self, symbol: str) -> tuple[bool, str]:
        """柜台已无持仓、账本还有的幽灵仓：按最新价补记平仓。"""
        lock = getattr(self, "_trade_lock", None)
        with (lock if lock is not None else nullcontext()):
            pos = self._position_for(symbol)
            side = str(getattr(pos, "side", "flat") or "flat") if pos is not None else "flat"
            volume = int(getattr(pos, "volume", 0) or 0) if pos is not None else 0
            if pos is None or side not in ("long", "short") or volume <= 0:
                return False, "程序账上没有这个持仓"
            price = float(self.kline.latest_price(symbol) or 0)
            if price <= 0:
                price = float(getattr(pos, "entry_price", 0) or 0)
            if price <= 0:
                return False, "拿不到价格，无法平账"
            row, balance = self.manual_close(
                symbol, price, reason="对账确认柜台无持仓，人工平掉"
            )
            if row is None:
                return False, "平账没成功，请核对账本"
            pos.origin = ""
            self._release_frozen_symbol(symbol)
            self._persist_state()
            self._record_event(
                "position_broker_closed",
                symbol=str(row.get("symbol") or symbol),
                side=side,
                volume=volume,
                price=price,
                balance=balance,
            )
            return True, f"已按 {price:g} 补记平仓 {volume} 手"

    def reject_broker_lots(self, symbol: str) -> tuple[bool, str]:
        """
        拒绝仓单：以程序账为准，不跟柜台改。

        柜台有仓、账本没有：不把柜台的仓收进策略。
        柜台已空、账本还有：账本不动（空仓不是一笔要收的仓，这里也不会替你平掉），
        该品种继续暂停自动开仓，避免对着空气下单。
        要把账本改成空，应走认领仓单（按柜台补记平仓）。
        """
        lock = getattr(self, "_trade_lock", None)
        with (lock if lock is not None else nullcontext()):
            key = str(symbol or "").strip().lower()
            state = getattr(self, "_reconcile_state", None) or {}
            unclaimed = {
                str(item).strip().lower()
                for item in (state.get("unclaimed") or [])
                if str(item).strip()
            }
            self._drop_reconcile_pending(symbol)
            if key in unclaimed:
                self._release_frozen_symbol(symbol)
                consume = getattr(self, "_consume_signal_bar_after_flat", None)
                if callable(consume):
                    consume(symbol)
                self._persist_state()
                self._record_event("position_rejected", symbol=symbol, mode="local_book")
                return True, f"已拒绝把柜台 {symbol} 收进程序。以程序账为准，等下一根信号再开"
            self._persist_state()
            self._record_event("reconcile_rejected", symbol=symbol, mode="keep_local")
            return True, f"已拒绝按柜台改 {symbol}。程序账不动，该品种仍暂停自动开仓"

    def ignore_reconcile_symbol(self, symbol: str) -> tuple[bool, str]:
        """人工决定先不处理这个品种：只清掉待办提示，品种仍保持冻结不自动交易。"""
        lock = getattr(self, "_trade_lock", None)
        with (lock if lock is not None else nullcontext()):
            self._drop_reconcile_pending(symbol)
            self._persist_state()
            self._record_event("reconcile_ignored", symbol=symbol)
            return True, "已忽略提示，品种仍保持冻结"

    def _drop_reconcile_pending(self, symbol: str) -> None:
        """把品种从对账待处理列表里移走，但不解除冻结。只改内存。"""
        key = str(symbol or "").strip().lower()
        if not key:
            return
        state = getattr(self, "_reconcile_state", None)
        if isinstance(state, dict):
            for name in ("unclaimed", "phantom", "extra"):
                rows = state.get(name)
                if isinstance(rows, list):
                    state[name] = [item for item in rows if str(item).strip().lower() != key]
        rounds = getattr(self, "_phantom_rounds", None)
        if isinstance(rounds, dict):
            rounds.pop(key, None)

    def _release_frozen_symbol(self, symbol: str) -> None:
        """把品种从对账冻结和待处理列表里移走。只改内存，写盘由调用方负责。"""
        key = str(symbol or "").strip().lower()
        if not key:
            return
        frozen = getattr(self, "_frozen_symbols", None)
        if isinstance(frozen, set):
            frozen.discard(key)
        self._drop_reconcile_pending(key)


    def _contract_multiplier(self, symbol: str) -> float:
        return float(self.sizer.multiplier_for(symbol))


    def _sync_leg_quota(self, prices: dict[str, float] | None = None) -> None:
        """
        多品种挂机时，按当前权益把仓位上限预算分给各条腿。

        口径：每条腿至少保 1 手，剩余额度尽量用满。分配结果只写入
        sizer.leg_quota 作为每条腿的总手数上限；止损、风险%、单笔上限、
        柜台可用资金仍由 calc_open 继续取小。单品种模式不启用。
        """
        sizer = getattr(self, "sizer", None)
        if sizer is None:
            return
        setter = getattr(sizer, "set_leg_quota", None)
        key_of = getattr(sizer, "product_key", None)
        if not callable(setter) or not callable(key_of):
            return
        if not getattr(self, "_multi_enabled", False):
            setter(None)
            self._leg_quota_key = None
            return
        if getattr(sizer, "ignore_margin_cap", False):
            # 研究/交叉测试关掉了保证金层，这里也不做组合分配，避免两套口径打架。
            setter(None)
            self._leg_quota_key = None
            return
        symbols = [
            str(item).strip()
            for item in (getattr(self, "symbols", None) or [])
            if str(item).strip()
        ]
        wanted = [
            str(item).strip()
            for item in (getattr(self, "_multi_symbols", None) or [])
            if str(item).strip()
        ]
        legs = [item for item in symbols if not wanted or item in wanted]
        if len(legs) < 2:
            setter(None)
            self._leg_quota_key = None
            return
        need: dict[str, float] = {}
        missing: list[str] = []
        for symbol in legs:
            price = 0.0
            if prices and symbol in prices:
                price = float(prices.get(symbol) or 0)
            if price <= 0:
                try:
                    price = float(self.kline.latest_price(symbol) or 0)
                except Exception:  # noqa: BLE001 - 取不到价格按缺数据处理
                    price = 0.0
            lot_margin = sizer.margin_for(symbol, price) if price > 0 else 0.0
            if price <= 0 or lot_margin <= 0:
                missing.append(symbol)
                continue
            need[symbol] = lot_margin
        if missing:
            # 缺价格/保证金比例时不能拿不完整的数据做「占用最大化」分配：
            # 退回单信号风控，该腿开仓仍会被 calc_open 拒绝并写警告。
            setter(None)
            missing_key: tuple[Any, ...] = ("missing", tuple(sorted(missing)))
            if missing_key != getattr(self, "_leg_quota_key", None):
                self._leg_quota_key = missing_key
                message = (
                    "组合保证金分配跳过："
                    + "、".join(missing)
                    + " 缺价格或保证金比例，无法在风控内分额度；"
                    "该腿开仓会被拒绝，请先在「品种与合约」表补上比例"
                )
                logger.warning(message)
                _ops_alert(self, "leg_margin_missing", message)
            return
        if len(need) < 2:
            setter(None)
            self._leg_quota_key = None
            return
        pct = float(getattr(sizer, "margin_usage_pct", 0.0) or 0.0)
        budget = float(sizer.capital) if pct <= 0 else float(sizer.capital) * (min(pct, 100.0) / 100.0)
        from src.risk.leg_alloc import allocate

        plan = allocate(need, budget)
        if plan.used > plan.budget + 0.01:
            # 分配绝不能越过仓位上限；真出现也宁可退回单信号风控。
            setter(None)
            self._leg_quota_key = ("overflow", round(budget))
            logger.warning(
                "组合保证金分配结果越界（占用{:.0f} > 预算{:.0f}），已退回单信号风控",
                plan.used,
                plan.budget,
            )
            _ops_alert(
                self,
                "leg_alloc_overflow",
                f"组合保证金分配结果越界（占用{plan.used:.0f} > 预算{plan.budget:.0f}），已退回单信号风控",
                level="error",
            )
            return
        # sizer 按品种键（fu / lh）对齐：同一品种多合约时把额度合起来。
        quota: dict[str, int] = {}
        for symbol, lots in plan.lots.items():
            key = key_of(symbol)
            quota[key] = quota.get(key, 0) + int(lots)
        margins: dict[str, float] = {}
        for symbol, value in need.items():
            key = key_of(symbol)
            margins[key] = max(margins.get(key, 0.0), value)
        setter(quota, margins)
        plan_key: tuple[Any, ...] = (tuple(sorted(quota.items())), round(budget))
        if plan_key != getattr(self, "_leg_quota_key", None):
            self._leg_quota_key = plan_key
            logger.info("组合保证金分配：{}", plan.summary())
        # 数据补齐、分配恢复正常：撤掉运行页还挂着的缺比例/越界告警。
        _ops_recovery(self, "leg_alloc_ok", f"组合保证金分配恢复：{plan.summary()}")

    def _size_open(self, decision: StrategyDecision) -> tuple[int, str]:
        """
        开仓手数：先按资金管理算，策略指定的手数只能往下压。

        长线趋势等策略会在 meta 里写「单元手数×加仓次数」，这个数不能绕过
        每笔风险%和仓位上限，否则小权益下会开出买不起的仓。
        """
        meta = decision.meta or {}
        kelly_rows: list[dict[str, Any]] | None = None
        from src.risk.kelly import kelly_box

        kelly_cfg = kelly_box(getattr(self, "cfg", None))
        if kelly_cfg.get("enabled") and kelly_cfg.get("auto_from_ledger", True):
            from src.account.signal_book import load_book, matched_closes

            # 自动模式没有账本也按真实零样本处理，绝不能退回手工全账户统计。
            book = load_book(getattr(self, "root", Path(".")))
            kelly_rows = matched_closes(book) if book is not None else []
        # 多品种组合分配：每条腿先留好额度，避免先出信号的腿把预算占满。
        # 用 __new__ 搭的轻量测试对象没有这个方法，按单品种处理。
        sync_quota = getattr(self, "_sync_leg_quota", None)
        if callable(sync_quota):
            sync_quota({decision.symbol: float(decision.entry_price or 0)})
        sized = self.sizer.calc_open(
            decision.symbol,
            decision.entry_price,
            decision.stop_price,
            current_abs_position=self.risk.total_abs_position(),
            positions=self.risk.positions,
            max_per_order=order_volume_cap(getattr(self, "trader", None)),
            kelly_rows=kelly_rows,
        )
        if sized.volume <= 0 and "保证金比例" in sized.reason:
            # 缺保证金比例时单信号算仓会拒开；除日志外再写一条运行页告警。
            _ops_alert(
                self,
                "leg_margin_missing",
                f"缺保证金比例，{decision.symbol} 开仓被拒绝：请先在「品种与合约」表补上比例",
            )
        elif sized.volume > 0 and not getattr(self, "_multi_enabled", False):
            # 单品种模式没有组合分配那一步，数据补齐后在这里把旧告警撤掉。
            _ops_recovery(
                self,
                "leg_alloc_ok",
                f"{decision.symbol} 保证金口径已恢复，可以正常算仓",
                symbol=decision.symbol,
            )
        raw = meta.get("volume")
        if raw is None:
            vol, note = sized.volume, sized.reason
        else:
            try:
                want = max(0, int(raw))
            except (TypeError, ValueError):
                return sized.volume, f"{sized.reason}（策略手数无效，按风控）"
            if want <= 0:
                return 0, f"策略指定{want}手"
            vol = min(sized.volume, want)
            note = f"策略指定{want}手，风控允许{sized.volume}手 → 取小{vol}手（{sized.reason}）"
        scale = meta.get("size_scale")
        if scale is not None and vol > 0:
            try:
                ratio = float(scale)
            except (TypeError, ValueError):
                ratio = 1.0
            else:
                if ratio <= 0:
                    return 0, f"{note}；信号强度×{ratio:.2f}→0手"
                if ratio < 0.999:
                    scaled = max(1, int(vol * ratio))
                    vol = min(vol, scaled)
                    note = f"{note}；信号强度×{ratio:.2f}→{vol}手"
        from src.risk.dynamic import apply_dynamic_open, live_equity_and_peak_or_error

        last_bars = getattr(self, "_last_signal_bars", None) or {}
        bars = last_bars.get(decision.symbol)
        equity, peak, equity_error = live_equity_and_peak_or_error(
            getattr(self, "root", Path("."))
        )
        if equity_error:
            # 账本读不出来时不能当成「0 回撤」放行，直接压到 0 手。
            self._dynamic_note = equity_error
            logger.error("账户权益读取失败，暂停开仓: {}", equity_error)
            return 0, equity_error
        adj, dyn_note = apply_dynamic_open(
            getattr(self, "cfg", None),
            vol,
            bars,
            equity=equity,
            peak=peak,
        )
        if dyn_note:
            self._dynamic_note = dyn_note
            note = f"{note}；{dyn_note}" if note else dyn_note
        return adj, note

    def set_orders_paused(self, paused: bool) -> None:
        """运行页「暂停报单」：扫描继续，只停开新仓。止损和清仓仍发。"""
        self._orders_paused = bool(paused)
        gateway = getattr(self.trader, "gateway", None)
        if gateway is not None and hasattr(gateway, "trading_paused"):
            gateway.trading_paused = self._orders_paused
        logger.warning("已暂停开新仓" if self._orders_paused else "已恢复报单")

    @property
    def orders_paused(self) -> bool:
        """运行页只读：是否已暂停开新仓。"""
        return bool(self._orders_paused)

    def _ctp_flow_text(self) -> str:
        """今日报撤笔数。到阈值只提醒。"""
        activity = getattr(getattr(self.trader, "gateway", None), "activity", None)
        if activity is None or not hasattr(activity, "describe"):
            return ""
        return str(activity.describe() or "")

    def _trade_intent(
        self,
        decision: StrategyDecision,
        *,
        volume: int = 1,
        detail: str = "",
    ) -> TradeIntent:
        """把当前准备发的这一笔收成只读意图，交给风控链，不把整个机器人传出去。"""
        cfg = getattr(self, "cfg", None) or {}
        caps = self.trader_capabilities
        bar_key = str((decision.meta or {}).get("bar_id", "") or "")
        last_bar = ""
        get_last = getattr(self.strategy, "get_last_entry_bar", None)
        if callable(get_last):
            last_bar = str(get_last(decision.symbol) or "")
        from src.strategy.base import same_signal_bar

        # 策略没带 bar_id 时，用最新已收盘K线，让「平仓后等下一根」对所有引擎生效。
        if not bar_key:
            try:
                from src.data.periods import drop_forming_bar

                period = str(getattr(self.strategy, "timeframe", "15") or "15")
                frame = self.kline.fetch(decision.symbol, period=period)
                closed = drop_forming_bar(frame)
                if closed is not None and len(closed) > 0:
                    bar_key = str(closed.index[-1])
            except Exception:  # noqa: BLE001
                bar_key = last_bar
        bar_consumed = same_signal_bar(last_bar, bar_key)
        stage = ""
        if caps.can_query_orders:
            from src.trader.go_live import strategy_order_block_reason

            stage = str(strategy_order_block_reason(cfg) or "")
        working = False
        trader = getattr(self, "trader", None)
        if caps.can_query_orders and trader is not None:
            working = bool(getattr(trader, "has_working_order", lambda _symbol: False)(decision.symbol))
        sent = getattr(self, "_sent_bar", None) or {}
        return TradeIntent(
            symbol=decision.symbol,
            side=decision.action.value,
            volume=int(volume),
            entry_price=float(decision.entry_price or 0),
            stop_price=float(decision.stop_price or 0),
            bar_id=bar_key,
            orders_paused=bool(getattr(self, "_orders_paused", False)),
            ledger_failed=bool(getattr(self, "_ledger_failed", False)),
            already_sent_this_bar=bool(bar_key and sent.get(decision.symbol) == bar_key),
            bar_consumed=bar_consumed,
            can_query_positions=caps.can_query_positions,
            can_query_orders=caps.can_query_orders,
            confirms_fill=caps.confirms_fill,
            stage_block=stage,
            has_working_order=working,
            blocks_new_opens=bool(getattr(trader, "blocks_new_opens", lambda: False)()) if trader is not None else False,
            books_note=str(getattr(trader, "_last_note", "") or "") if trader is not None else "",
            detail=detail,
            force=bool((decision.meta or {}).get("force")),
            health_blocked=bool(getattr(self, "_health_blocked", False)),
            health_block_reason=str(getattr(self, "_health_block_reason", "") or ""),
        )

    def _reject_gate(self, decision: StrategyDecision, code: str, reason: str, bar_key: str) -> None:
        """风控链拦住之后写日志和事件，不发单。"""
        side = decision.action.value
        if code == "order_paused":
            logger.warning("已暂停报单，本笔没有发出 | {} {}", decision.symbol, side)
        elif code == "ledger_error_block":
            logger.error("{} | {}", decision.symbol, reason)
            self.last_error = reason
        elif code == "order_dedup":
            logger.info("{} 这根K线已经发过开仓单，跳过重复发单 | {}", decision.symbol, bar_key)
        elif code == "order_wait_next_bar":
            logger.info("{} {} | {}", decision.symbol, reason, bar_key)
        elif code == "close_skip_no_position":
            logger.warning("策略要求平仓，但程序账上无持仓，跳过 {} | {}", decision.symbol, decision.reason)
        elif code == "size_skip":
            logger.warning("仓位为0，跳过开仓 {}: {}", decision.symbol, reason)
        elif code == "order_blocked_stage":
            logger.warning(reason)
        elif code == "order_working":
            logger.warning("上一笔 {} 还没有最终回报，先不报新单", decision.symbol)
        elif code == "strategy_health":
            logger.warning("策略健康停开新仓 {}: {}", decision.symbol, reason)
        elif code == "risk_block":
            if "停止开新仓" in reason or "柜台" in reason or "对账" in reason:
                logger.warning("停止开新仓 {}: {}", decision.symbol, reason)
            else:
                logger.warning("风控拒绝 {}: {}", decision.symbol, reason)
        else:
            logger.warning("{} {}: {}", code, decision.symbol, reason)
        extra = {"reason": reason}
        if code in {"order_dedup", "order_wait_next_bar"}:
            extra = {"bar_id": bar_key}
        elif code == "close_skip_no_position":
            extra = {"reason": decision.reason}
        elif code == "order_working":
            extra = {"reason": "等待上一笔回报"}
        elif code in {"order_blocked_stage", "risk_block", "size_skip", "strategy_health"}:
            extra = {"reason": reason}
        self._record_event(code, symbol=decision.symbol, side=side, **extra)
        risk_at = now_iso()
        decision.meta = {**(decision.meta or {}), "risk_at": risk_at}
        try:
            volume = int((decision.meta or {}).get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0
        self._record_order_timeline(
            "risk_intent",
            decision,
            volume=volume,
            allowed=False,
            code=code,
            reason=reason,
            risk_at=risk_at,
            stage_ms=elapsed_ms((decision.meta or {}).get("decided_at"), risk_at),
        )
        # 暂停、挂单未完、上线阶段未到：这一根还应该能再试，不要把策略标记锁死。
        if (
            decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)
            and code in {"order_paused", "order_working", "order_blocked_stage", "ledger_error_block", "risk_block"}
        ):
            self._release_signal_mark(decision)

    def _execute(self, decision: StrategyDecision) -> bool:
        """把策略决策交给风控链，通过后才走交易通道。返回 True 表示这一笔已经发出（不代表成交）。"""
        lock = getattr(self, "_trade_lock", None)
        with (lock if lock is not None else nullcontext()):
            return self._execute_locked(decision)

    def _order_session_open(self, symbol: str = "", now: datetime | None = None) -> bool:
        """当前是否在该品种可下单的交易时段内。没配时段时放行。"""
        picker = getattr(self, "_symbol_sessions_for", None)
        sessions = picker(symbol) if (symbol and callable(picker)) else getattr(self, "sessions", None)
        if not sessions:
            return True
        from datetime import time as clock_time

        from src.utils.session_clock import has_commodity_tea_break, in_trading_sessions

        stamp = now or datetime.now()
        # 品种表上午常写成 09:00～11:30 一整段，但商品 10:15～10:30 休市。
        # 落在这一段时柜台会以「当前状态禁止此项操作」拒单，不能当成在盘。
        if has_commodity_tea_break(symbol) and clock_time(10, 15) <= stamp.time() < clock_time(10, 30):
            return False
        return bool(in_trading_sessions(sessions, stamp))

    def _execute_locked(self, decision: StrategyDecision) -> bool:
        """_execute 的实体；调用方已经持有交易锁。"""
        self._order_dispatch_started = False
        self._order_unclean = False
        if decision.action == StrategyAction.HOLD:
            logger.debug("{}: {}", decision.symbol, decision.reason)
            return False
        # GUI 通道只交易「当前选中」品种：扫描清单里的其它合约（旧仓）只允许平仓，不在这里开新仓。
        if (
            decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)
            and self.trader_capabilities.window_driven
            and getattr(self, "symbols", None)
            and decision.symbol != self.symbols[0]
        ):
            logger.warning(
                "GUI 通道只交易当前选中品种 {}，跳过 {} 的开仓信号",
                self.symbols[0],
                decision.symbol,
            )
            self._record_event(
                "gui_open_skip_not_selected",
                symbol=decision.symbol,
                side=decision.action.value,
                selected=self.symbols[0],
            )
            return False
        self._start_decision_timeline(decision)
        # 收盘后 / 周末不下单：挂着的老信号不能拿到已收盘的市场上去点。
        if not self._order_session_open(decision.symbol):
            logger.warning(
                "现在不在交易时段，本笔不下单 | {} {} {}",
                decision.action.value,
                decision.symbol,
                decision.reason,
            )
            self._record_event(
                "order_outside_session",
                symbol=decision.symbol,
                side=decision.action.value,
                reason=decision.reason,
            )
            risk_at = now_iso()
            decision.meta = {**(decision.meta or {}), "risk_at": risk_at}
            self._record_order_timeline(
                "risk_intent",
                decision,
                volume=0,
                allowed=False,
                code="order_outside_session",
                reason="现在不在交易时段",
                risk_at=risk_at,
                stage_ms=elapsed_ms((decision.meta or {}).get("decided_at"), risk_at),
            )
            return False
        bar_key = str((decision.meta or {}).get("bar_id", "") or "")
        # 手机/同花顺已经把仓平了：柜台是 0，程序还当自己有仓，会反复报平仓被拒。
        if (
            decision.action == StrategyAction.CLOSE
            and self.trader_capabilities.orders_via_broker
            and self._broker_net_for(decision.symbol) == 0
        ):
            booked = self._book_external_close(
                decision.symbol, "柜台已无持仓（可能已在别处平掉），补记平仓"
            )
            if booked:
                logger.warning(
                    "{} 柜台已经空仓，不再报平仓，已按现价补记程序账",
                    decision.symbol,
                )
            return booked
        if decision.action == StrategyAction.CLOSE:
            key = str(decision.symbol or "").strip().lower()
            last_try = float((getattr(self, "_close_attempt_at", {}) or {}).get(key) or 0)
            if last_try and (time.monotonic() - last_try) < 8.0:
                logger.info("{} 刚报过平仓，8 秒内不连发", decision.symbol)
                return False
        early = run_early_gates(
            self._trade_intent(decision, volume=1),
            getattr(self, "risk", None),
        )
        if not early.ok:
            self._reject_gate(decision, early.code, early.reason, bar_key)
            return False

        # 均线/SuperTrend 等策略报了「反手」时，先平掉原来的仓，再开新方向。平仓没完成就不再开。
        # 这是执行器「怎么做」，不放进风控链。
        pos_now = self._position_for(decision.symbol)
        want = None
        if decision.action == StrategyAction.BUY_OPEN:
            want = "long"
        elif decision.action == StrategyAction.SELL_OPEN:
            want = "short"
        if (
            want
            and pos_now
            and pos_now.side not in ("flat", want)
            and (decision.meta or {}).get("reverse")
        ):
            self._execute(
                StrategyDecision(
                    StrategyAction.CLOSE,
                    decision.symbol,
                    "先平掉原来的仓，再开反向",
                    entry_price=decision.entry_price,
                    stop_price=decision.stop_price,
                    meta=dict(decision.meta or {}),
                )
            )
            # 平仓递归会改这个标记；外层开仓还没点出去，必须清掉，否则异常时会误以为已下单。
            self._order_dispatch_started = False
            pos_now = self._position_for(decision.symbol)
            if pos_now and pos_now.side != "flat":
                logger.warning("反向开仓的平仓没有完成，本根不再开新仓")
                return False

        side = decision.action.value
        size_note = ""
        meta = decision.meta or {}
        if decision.action == StrategyAction.CLOSE:
            pos = self._position_for(decision.symbol)
            held = abs(int(pos.volume)) if pos and pos.volume else 0
            # 长线减仓会在 meta 里写要平几手；没有就全平。手数是否为 0 交给风控链。
            raw_cut = meta.get("close_volume", meta.get("volume"))
            if held <= 0:
                vol = 0
            elif raw_cut is not None:
                try:
                    vol = max(1, min(held, int(raw_cut)))
                    size_note = f"策略指定平{vol}手"
                except (TypeError, ValueError):
                    vol = held
            else:
                vol = held
        else:
            # 开仓/加仓：策略手数作为上限，实际手数不能超过资金管理算出的值。
            vol, size_note = self._size_open(decision)

        send = run_send_gates(
            self._trade_intent(decision, volume=int(vol or 0), detail=size_note),
            getattr(self, "risk", None),
        )
        if not send.ok:
            self._reject_gate(decision, send.code, send.reason, bar_key)
            return False

        risk_at = now_iso()
        decision.meta = {**(decision.meta or {}), "risk_at": risk_at, "volume": int(vol)}
        self._record_order_timeline(
            "risk_intent",
            decision,
            volume=int(vol),
            allowed=True,
            code="passed",
            reason=size_note,
            risk_at=risk_at,
            stage_ms=elapsed_ms((decision.meta or {}).get("decided_at"), risk_at),
            decision_to_risk_ms=elapsed_ms((decision.meta or {}).get("decided_at"), risk_at),
        )
        # 定价和发单一起放进 _dispatch：GUI 真下单时整段由委托意图上下文包住，
        # 中途出错保留意图文件给下次启动核对，正常走完（成功或明确失败）自动清掉。
        return self._dispatch(decision, vol, side, size_note, bar_key)

    def _dispatch(
        self,
        decision: StrategyDecision,
        vol: int,
        side: str,
        size_note: str,
        bar_key: str,
    ) -> bool:
        """定好价格和成交方向，再交给 _send_and_book 点单。"""
        caps = self.trader_capabilities

        # 对手价只在真正下单时看一次。下单会先把交易软件拉到最前，没有交易窗口才登录。
        # 不在这里提前读价格，免得日志刷新把本程序叫到前面，也免得价格菜单被点开两次。

        # 窗口点击时永远不把策略里的价格写进价格框。启动时已经选好对手价。
        # 柜台报单必须带限价。价格按对手价方向挂，不把止损价当成限价。
        position_side = None
        if caps.orders_via_broker:
            price = self._ctp_limit_price(decision)
            if price is None or price <= 0:
                logger.error("CTP 没有可用价格，这次不报单 | {}", decision.symbol)
                self._record_event("order_no_price", symbol=decision.symbol, side=side)
                self._release_signal_mark(decision)
                return False
            if (
                decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)
                and self._ctp_open_slip_blocked(decision, float(price))
            ):
                self._release_signal_mark(decision)
                return False
            if decision.action == StrategyAction.CLOSE:
                held = self._position_for(decision.symbol)
                position_side = getattr(held, "side", None)
        elif caps.confirms_fill and decision.action != StrategyAction.CLOSE:
            price = decision.entry_price
        else:
            price = None

        decision.meta = {
            **(decision.meta or {}),
            "order_price": price,
            "price_mode": "opponent_limit" if caps.orders_via_broker else (
                "limit" if price is not None else "opponent"
            ),
        }
        self._timeline_trace(decision)

        def place() -> bool:
            return self._send_and_book(
                decision, vol, price, position_side, side, size_note, bar_key
            )

        # 窗口点击真下单先落一笔「意图」，避免点击成功后程序崩溃、重启又开一次。
        if caps.window_driven and not bool(self.cfg.get("dry_run", True)):
            try:
                with self._order_intent(decision, vol) as state:
                    sent = place()
                    state["unclean"] = bool(getattr(self, "_order_unclean", False))
            except OrderIntentWriteError as exc:
                if getattr(self.risk, "halt_kind", "") == "pending_intent":
                    # 盘上已有未核对委托，_write_order_intent / _check 已经按那件事熔断。
                    logger.error("上一笔委托尚未核对，本笔不下单: {}", exc)
                else:
                    self.risk.set_halt("intent_write_failed", str(exc))
                    self.last_error = str(exc)
                    logger.error("委托意图写盘失败，本笔不下单: {}", exc)
                    self._record_event(
                        "intent_write_failed",
                        symbol=decision.symbol,
                        side=side,
                        volume=vol,
                        error=str(exc),
                    )
                return False
            except Exception:
                # 可能已经点出去。留意图文件，并立刻熔断，等人工核对。
                if self._intent_files():
                    self._check_pending_intent()
                raise
            if self._order_unclean:
                self._halt_on_pending_intent()
            return sent
        return place()

    def _send_and_book(
        self,
        decision: StrategyDecision,
        vol: int,
        price: float | None,
        position_side: str | None,
        side: str,
        size_note: str,
        bar_key: str,
    ) -> bool:
        """真正点单并记账。调用前风控链已经放行，价格也已经定好。"""
        # 从这里开始真正点单。下面的异常再撤销信号标记就可能重复下单，所以先置位。
        caps = self.trader_capabilities
        self._order_dispatch_started = True
        self._order_unclean = False
        if decision.action == StrategyAction.CLOSE:
            stamps = getattr(self, "_close_attempt_at", None)
            if stamps is None:
                self._close_attempt_at: dict[str, float] = {}
                stamps = self._close_attempt_at
            stamps[str(decision.symbol or "").strip().lower()] = time.monotonic()
            flatten_try = getattr(self, "_flatten_try", None)
            if isinstance(flatten_try, dict):
                flatten_try[decision.symbol] = time.monotonic()
        trace_id = self._timeline_trace(decision)
        dispatch_started_at = now_iso()
        decision.meta = {**(decision.meta or {}), "dispatch_started_at": dispatch_started_at}
        journal.record_audit(
            "order_submitted",
            trace_id=trace_id,
            strategy_id=str(getattr(self.strategy, "name", "") or type(self.strategy).__name__),
            symbol=decision.symbol,
            message=str(decision.reason or ""),
            payload={
                "volume": int(vol),
                "price": float(price or 0),
                "side": decision.action.value,
                "bar_time": str((decision.meta or {}).get("bar_id") or ""),
            },
            root=self.cfg.get("_project_root"),
        )
        if not caps.orders_via_broker:
            decision.meta = {**(decision.meta or {}), "sent_at": dispatch_started_at}
            self._record_order_timeline(
                "order_sent",
                decision,
                volume=int(vol),
                order_price=price,
                sent_at=dispatch_started_at,
                stage_ms=elapsed_ms((decision.meta or {}).get("decided_at"), dispatch_started_at),
                decision_to_send_ms=elapsed_ms((decision.meta or {}).get("decided_at"), dispatch_started_at),
                risk_to_send_ms=elapsed_ms((decision.meta or {}).get("risk_at"), dispatch_started_at),
            )
        if caps.orders_via_broker:
            # 和交易台同一份 OrderRequest、同一个 place_order。价格来自策略/最新收盘，人点时来自盘口。
            result = self.trader.place_order(
                ctp_order(
                    decision.symbol,
                    decision.action.value,  # type: ignore[arg-type]
                    vol,
                    float(price or 0),
                    position_side=position_side,
                    stop_price=decision.stop_price,
                    bar_id=str((decision.meta or {}).get("bar_id", "") or ""),
                    remark=decision.reason,
                    trace_id=trace_id,
                    meta=dict(decision.meta or {}),
                )
            )
        else:
            request = OrderRequest(
                decision.symbol,
                decision.action.value,  # type: ignore[arg-type]
                vol,
                price,
                remark=decision.reason,
                position_side=position_side,
                stop_price=decision.stop_price,
                bar_id=str((decision.meta or {}).get("bar_id") or ""),
                trace_id=trace_id,
                meta=dict(decision.meta or {}),
            )
            place_order = getattr(self.trader, "place_order", None)
            if callable(place_order):
                result = place_order(request)
            elif decision.action == StrategyAction.BUY_OPEN:
                # 旧的鸭子类型测试替身只实现便捷方法；真实通道统一走 OrderRequest。
                result = self.trader.buy_open(decision.symbol, vol, price)
            elif decision.action == StrategyAction.SELL_OPEN:
                result = self.trader.sell_open(decision.symbol, vol, price)
            else:
                result = self.trader.close_position(decision.symbol, vol, price)

        journal.record_audit(
            "order_accepted" if bool(getattr(result, "ok", False)) else "order_rejected",
            trace_id=trace_id,
            strategy_id=str(getattr(self.strategy, "name", "") or type(self.strategy).__name__),
            symbol=decision.symbol,
            order_id=str(getattr(result, "order_id", "") or ""),
            message=str(getattr(result, "message", "") or ""),
            payload={
                "assumed_fill": bool(getattr(result, "assumed_fill", False)),
                "bar_time": str((decision.meta or {}).get("bar_id") or ""),
            },
            root=self.cfg.get("_project_root"),
        )
        raw_value = getattr(result, "raw", None)
        raw = raw_value if isinstance(raw_value, dict) else {}
        channel_result_at = str(raw.get("channel_result_at") or "") or now_iso()
        raw.setdefault("trace_id", trace_id)
        raw.setdefault("bar_id", str((decision.meta or {}).get("bar_id") or ""))
        raw.setdefault("meta", dict(decision.meta or {}))
        raw.setdefault("order_price", price)
        raw.setdefault("channel_result_at", channel_result_at)
        decision.meta = {**(decision.meta or {}), "channel_result_at": channel_result_at}
        if not caps.orders_via_broker:
            self._record_order_timeline(
                "channel_result",
                decision,
                volume=int(vol),
                order_id=str(getattr(result, "order_id", "") or ""),
                ok=bool(getattr(result, "ok", False)),
                message=str(getattr(result, "message", "") or ""),
                channel_result_at=channel_result_at,
                stage_ms=elapsed_ms(dispatch_started_at, channel_result_at),
                send_to_channel_result_ms=elapsed_ms(dispatch_started_at, channel_result_at),
            )
        if result.ok and isinstance(raw, dict) and raw.get("unclean"):
            # 点击已经落下去，只是收尾出错。按已发出处理，记账照常，但留住意图文件等人工核对。
            self._order_unclean = True
            logger.error(
                "下单已发出但收尾异常，保留委托意图待核对 | {} {} vol={} | {}",
                decision.action.value,
                decision.symbol,
                vol,
                result.message,
            )
            self._record_event(
                "order_unclean",
                symbol=decision.symbol,
                side=side,
                volume=vol,
                order_id=result.order_id,
                message=result.message,
            )

        if caps.orders_via_broker:
            self._finish_ctp_order(decision, vol, result, side, size_note)
            # CTP 是异步发单：result.ok 表示已报入柜台。只有成功报入才标记 sent，
            # 没发出去（缺配置、前置没连上、暂停报单）的话下一轮应该再试。
            if result.ok and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN) and bar_key:
                self._sent_bar[decision.symbol] = bar_key
            elif decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
                self._release_signal_mark(decision)
            return bool(getattr(result, "ok", False))

        # 发单动作已经送出（result.ok=True），这根 K 线不再重复点击。
        # result.ok=False 的情况（价格被挡、窗口被盖、手数没写进框）不算"已发单"，
        # 下一轮扫描还在同一根 K 线里时应该再试一次——那才是用户真正想要的行为。
        if result.ok and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN) and bar_key:
            self._sent_bar[decision.symbol] = bar_key
        elif decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            self._release_signal_mark(decision)

        if result.ok:
            # 不确认成交的通道且关闭 assume_fill：只记「已发单」，不改内部持仓（点击≠成交）
            if (
                not caps.confirms_fill
                and getattr(result, "assumed_fill", False)
                and not self.gui_assume_fill
            ):
                logger.warning(
                    "已发单但 assume_fill=false，不更新内部持仓 | {} {} vol={} | {}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                    result.message,
                )
                self._record_event(
                    "order_sent_unconfirmed",
                    symbol=decision.symbol,
                    side=side,
                    volume=vol,
                    order_id=result.order_id,
                    reason=decision.reason,
                )
                return True

            # 记账用参考价：开仓用策略 entry；平仓尽量用最新收盘近似盈亏
            if decision.action == StrategyAction.CLOSE:
                fill_price = float(decision.entry_price or 0.0)
                df = None
                try:
                    leg = self._strategy_for(decision.symbol)
                    df = self.kline.fetch(decision.symbol, period=str(leg.timeframe))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("平仓取最新价失败，回退持仓开仓价: {}", exc)
                if df is not None and len(df) > 0:
                    try:
                        latest = float(df["close"].iloc[-1])
                    except Exception:  # noqa: BLE001
                        latest = 0.0
                    if latest > 0:
                        fill_price = latest
                if fill_price <= 0:
                    # 时钟清仓的 decision 不带 entry_price，K 线又拿不到时，
                    # 用程序账上的开仓价兜底，绝不能带着 0 价去记账。
                    pos = self._position_for(decision.symbol)
                    fill_price = float(getattr(pos, "entry_price", 0) or 0) if pos else 0.0
                if fill_price <= 0:
                    logger.error("平仓无可用参考价，保留程序仓位不动，避免账实分叉")
                    self._record_event("order_no_price", symbol=decision.symbol, side=side)
                    return False
            else:
                fill_price = float(decision.entry_price or 0.0)
                if fill_price <= 0:
                    logger.error("开仓无参考入场价，拒绝更新内部持仓，避免 entry=0")
                    self._record_event("order_no_price", symbol=decision.symbol, side=side)
                    return False

            assumed = bool(getattr(result, "assumed_fill", False))
            executor_name = str((self.cfg.get("trade") or {}).get("executor") or "").strip().lower()
            simulated = executor_name == "sim" or (bool(self.cfg.get("dry_run", True)) and not assumed)
            fill_kind = "assumed_fill" if assumed else "simulated_fill" if simulated else "channel_fill"
            if assumed:
                logger.warning(
                    "假定成交记账（请尽快核对交易软件真实持仓）| {} {} vol={}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                )
                # GUI 只能确认点击动作；这里的价格是程序记账参考价，不是柜台成交价。
                self._record_order_timeline(
                    "assumed_fill",
                    decision,
                    volume=int(vol),
                    order_id=str(getattr(result, "order_id", "") or ""),
                    assumed_price=fill_price,
                    assumed_at=channel_result_at,
                    price_kind="reference_assumed",
                    broker_confirmed=False,
                    trade_id="",
                    trade_time="",
                )
            elif simulated:
                simulated_at = str(raw.get("simulated_at") or "") or channel_result_at
                self._record_order_timeline(
                    "simulated_fill",
                    decision,
                    volume=int(vol),
                    order_id=str(getattr(result, "order_id", "") or ""),
                    fill_price=fill_price,
                    simulated_at=simulated_at,
                    price_kind="pure_simulation",
                    broker_confirmed=False,
                )

            evidence = {
                **raw,
                "fill_kind": fill_kind,
                "price_kind": "reference_assumed" if assumed else "pure_simulation" if simulated else "channel_reported",
                "broker_confirmed": bool(caps.confirms_fill and not simulated),
                "order_id": str(getattr(result, "order_id", "") or ""),
                "channel_result_at": channel_result_at,
                "fill_received_at": str(raw.get("simulated_at") or "") if simulated else "",
            }
            self._apply_fill_accounting(
                decision.symbol,
                decision,
                vol,
                fill_price,
                fill_price,
                fill_evidence=evidence,
            )
            logger.info(
                "已执行 {} {} vol={} | {} | stop={} | {}",
                decision.action.value,
                decision.symbol,
                vol,
                decision.reason,
                decision.stop_price,
                size_note or "平仓",
            )
            self._record_event(
                "order",
                symbol=decision.symbol,
                side=side,
                volume=vol,
                order_id=result.order_id,
                reason=decision.reason,
                stop=decision.stop_price,
                sizing=size_note,
                assumed_fill=bool(getattr(result, "assumed_fill", False)),
                market_health=dict((decision.meta or {}).get("market_health") or {}),
            )
            return True
        else:
            logger.error("下单失败: {}", result.message)
            self.last_error = result.message
            self._record_event("order_fail", symbol=decision.symbol, side=side, message=result.message)
            return False

    def _consume_signal_bar_after_flat(self, symbol: str, bar_id: str = "") -> None:
        """人工或策略平仓后，本根已收盘信号不再开，等下一根。"""
        bid = str(bar_id or "").strip()
        if not bid:
            try:
                from src.data.periods import drop_forming_bar

                period = str(getattr(self.strategy, "timeframe", "15") or "15")
                frame = self.kline.fetch(symbol, period=period)
                closed = drop_forming_bar(frame)
                if closed is not None and len(closed) > 0:
                    bid = str(closed.index[-1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("{} 平仓后取信号K线失败，仍标记等下一根: {}", symbol, exc)
        note = getattr(self.strategy, "note_flat_wait_next_bar", None)
        if callable(note):
            note(symbol, bid)
        elif bid:
            setter = getattr(self.strategy, "set_signaled_bar", None)
            if callable(setter):
                setter(symbol, bid)
            last = getattr(self.strategy, "set_last_entry_bar", None)
            if callable(last):
                last(symbol, bid)
        logger.info(
            "{} 已平仓，本根信号 {} 不再开。等下一根已收盘K线再评估，不会因为仓单对不上立刻补仓",
            symbol,
            bid or "（当前）",
        )

    def _release_signal_mark(self, decision: StrategyDecision) -> None:
        """
        下单没发出去时，把策略里「这根 K 线已处理」的标记撤销。

        策略在产生信号时就写标记，原本是为了同一根 K 线不重复评估；
        但下单失败也被一起锁住，信号会静默丢掉。这里只在确认没点单时撤销，
        让下一轮扫描还能在同一根 K 线上再试一次。
        刚平过仓的那一根不能撤：否则会把你刚平掉的仓立刻补回去。
        """
        bar_key = str((decision.meta or {}).get("bar_id", "") or "")
        if not bar_key:
            return
        last_fn = getattr(self.strategy, "get_last_entry_bar", None)
        last_bar = str(last_fn(decision.symbol) or "") if callable(last_fn) else ""
        too_fresh = getattr(self, "_own_fill_too_fresh", None)
        kind_of = getattr(self, "_own_fill_kind", None)
        just_closed = callable(too_fresh) and too_fresh(decision.symbol) and (
            not callable(kind_of) or str(kind_of(decision.symbol)) == "close"
        )
        if just_closed or (last_bar and last_bar == bar_key):
            logger.info(
                "{} 这一根已经平过仓（{}），不撤销已处理标记，避免立刻补仓",
                decision.symbol,
                bar_key,
            )
            return
        if self.strategy.clear_signaled_bar(decision.symbol, expected_bar_id=bar_key):
            logger.warning(
                "{} 下单没有发出去，撤销「{} 已处理」标记，下一轮再试",
                decision.symbol,
                bar_key,
            )
        pending = getattr(self.strategy, "_pending", None)
        if isinstance(pending, dict):
            item = pending.get(decision.symbol)
            if isinstance(item, dict) and str(item.get("bar_id", "")) == bar_key:
                pending.pop(decision.symbol, None)

    def _broker_net_for(self, symbol: str) -> int | None:
        """柜台净仓：多正空负。没有柜台对象时返回 None，不当成空仓。"""
        gateway = getattr(getattr(self, "trader", None), "gateway", None)
        if gateway is None:
            return None
        positions = getattr(gateway, "positions", None)
        if not isinstance(positions, dict):
            return None
        from src.trader.ctp_logic import symbol_key

        # 刚重连、持仓还没查回来时，快照是空的，但不是「真的空仓」。
        # 只有确认这份快照已经查全，才能把「查不到这个合约」当成净仓 0。
        if not getattr(gateway, "positions_ready", True):
            return None
        view = positions.get(symbol_key(symbol))
        if view is None:
            return 0
        try:
            return int(getattr(view, "net", 0) or 0)
        except (TypeError, ValueError):
            return None

    def _book_external_close(self, symbol: str, reason: str) -> bool:
        """柜台已经空了，只补记程序账，不再向柜台报平仓。"""
        pos = self._position_for(symbol)
        if pos is None or str(getattr(pos, "side", "flat") or "flat") == "flat":
            return False
        price = 0.0
        try:
            price = float(self.kline.latest_price(symbol) or 0)
        except Exception:  # noqa: BLE001
            price = 0.0
        if price <= 0:
            price = float(getattr(pos, "entry_price", 0) or 0)
        if price <= 0:
            logger.warning("柜台已空但没有平仓价，无法补记 {}", symbol)
            return False
        closed, _balance = self._manual_close_locked(symbol, price, reason=reason)
        if closed is None:
            return False
        frozen = getattr(self, "_frozen_symbols", None)
        if isinstance(frozen, set):
            frozen.discard(str(symbol or "").strip().lower())
        return True

