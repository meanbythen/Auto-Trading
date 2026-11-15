"""订单意图、风控链接入、通道执行、成交记账和手工补账。"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.kernel.risk_chain import TradeIntent, run_early_gates, run_send_gates
from src.strategy import StrategyAction, StrategyDecision
from src.trader.base import Capabilities
from src.trader.ctp_orders import ctp_order


def _same_contract(left: object, right: object) -> bool:
    """两个写法是不是同一个合约。郑商所三位和四位年月写法算同一个。"""
    a = str(left or "").strip().lower()
    b = str(right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    product_a = "".join(ch for ch in a if ch.isalpha())
    product_b = "".join(ch for ch in b if ch.isalpha())
    return product_a == product_b and a[-3:] == b[-3:]


class OrderIntentWriteError(RuntimeError):
    """委托意图写盘失败：本笔必须拒绝下单，避免崩溃后无迹可查。"""


def ctp_fill_key(order_ref: object, filled_total: int) -> str:
    """CTP 成交去重键：同一报单、同一累计成交量只入一次账。回调与补记共用。"""
    ref = str(order_ref or "").strip()
    if not ref:
        return ""
    return f"ctp:{ref}:{int(filled_total)}"


class OrderExecutorMixin:
    """订单执行方法；共享运行状态仍保存在 TradingBot 实例上。"""

    _INTENT_HALT_PREFIX = "发现未完成的下单记录"
    _pending_intent: dict[str, Any] | None = None
    # 盘上所有未核对委托（按笔一个文件）和对应路径；只在对账确认时一起清掉。
    _pending_intents: list[dict[str, Any]] | None = None
    _pending_intent_files: list[Path] | None = None
    # 点击已发出但收尾出错：本笔按已发出算，意图文件留到人工核对。
    _order_unclean = False
    # 成交回调等交易锁的上限。超时就交回补记/对账，不把 CTP 回调线程卡死。
    FILL_LOCK_TIMEOUT = 10.0
    _RECENT_FILL_LIMIT = 5000
    # 锁内二级去重：本次运行已经入账的成交键。惰性初始化，避免可变类属性共享。
    _recent_fill_keys: OrderedDict[str, float] | None = None

    @property
    def trader_capabilities(self) -> Capabilities:
        """当前通道能力。调度只看能力，不看具体是哪一家。"""
        return getattr(self.trader, "capabilities", None) or Capabilities()

    def _intent_files(self) -> list[Path]:
        """盘上所有还没清理的委托意图：按笔目录里的每一笔，加上升级前的单文件。"""
        files: list[Path] = []
        folder = getattr(self, "intent_dir", None)
        if folder is not None and folder.exists():
            files.extend(sorted(path for path in folder.glob("*.json") if path.is_file()))
        legacy = getattr(self, "intent_path", None)
        if legacy is not None and legacy.exists():
            files.append(legacy)
        return files

    def _write_order_intent(self, decision: StrategyDecision, vol: int) -> None:
        """点击前先落盘这一笔委托，一笔一个文件。成功后由 _clear_order_intent 删除。"""
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "intent_id": f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{int(vol)}",
            "symbol": decision.symbol,
            "side": decision.action.value,
            "volume": int(vol),
            "bar_id": str((decision.meta or {}).get("bar_id", "") or ""),
            "reason": decision.reason,
            "executor": "gui",
        }
        existing = self._intent_files()
        if existing:
            # 盘上还有没核对的点击记录：读回内容，禁住本笔，绝不允许覆盖。
            self._check_pending_intent()
            raise OrderIntentWriteError(
                f"上一笔委托意图尚未清理（{existing[0].name}），拒绝覆盖"
            )
        folder = getattr(self, "intent_dir", None)
        target = (
            folder / f"{payload['intent_id']}.json"
            if folder is not None
            else self.intent_path
        )
        tmp = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except Exception as exc:  # noqa: BLE001
            logger.error("写委托意图失败，拒绝本笔下单: {}", exc)
            raise OrderIntentWriteError(f"写委托意图失败: {exc}") from exc
        self._pending_intent = payload
        self._pending_intents = [payload]
        self._pending_intent_files = [target]

    def _clear_order_intent(self) -> None:
        """删掉本进程写的那一笔；升级前遗留的单文件也一并清掉。"""
        for path in list(getattr(self, "_pending_intent_files", []) or []):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("清理委托意图失败: {}", exc)
        legacy = getattr(self, "intent_path", None)
        if legacy is not None:
            try:
                legacy.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("清理委托意图失败: {}", exc)
        self._pending_intent = None
        self._pending_intents = []
        self._pending_intent_files = []

    @contextmanager
    def _order_intent(
        self, decision: StrategyDecision, vol: int
    ) -> Iterator[dict[str, Any]]:
        """
        GUI 真下单前后管理「委托意图」文件。

        正常走完（下单成功，或明确失败）就清掉。中途抛异常时不清：
        异常可能发生在点击之后，留着文件让运行期和下次启动都按「未完成委托」熔断，
        先去交易软件核对持仓，宁可多核对一次，也不重复下单。

        yield 出去的字典是给调用方的回执：置 ``unclean=True`` 表示点击已发出但收尾
        出错，这时同样不删文件，等人工核对。
        """
        state: dict[str, Any] = {"unclean": False}
        self._write_order_intent(decision, vol)
        # 下面这行在抛异常时不会执行（生成器从 yield 里直接带出异常），文件自然留在盘上。
        yield state
        if state.get("unclean"):
            pending = getattr(self, "_pending_intent_files", None) or []
            name = pending[-1].name if pending else "委托意图"
            logger.error(
                "点击已发出但收尾未完成，保留委托意图待人工核对: {}",
                name,
            )
            return
        self._clear_order_intent()

    def _halt_on_pending_intent(self) -> None:
        """盘上还有没核对的点击记录：立即停手，等人工核对后再开仓。"""
        intents = [item for item in (getattr(self, "_pending_intents", None) or []) if item]
        intent = max(
            intents,
            key=lambda item: str(item.get("ts") or ""),
            default=dict(self._pending_intent or {}),
        )
        volume = intent.get("volume")
        message = (
            f"{self._INTENT_HALT_PREFIX}：{intent.get('symbol') or '未知合约'} "
            f"{intent.get('side') or '未知方向'} "
            f"{'?' if volume is None else volume} 手 "
            f"（{intent.get('ts') or '时间未知'}）。程序可能在点击后崩溃。"
            "请先在交易软件核对持仓与委托，再到界面点「对账确认」解除熔断。"
        )
        if len(intents) > 1:
            message += f"（盘上共 {len(intents)} 笔未核对委托）"
        self.risk.set_halt("pending_intent", message)
        self.last_error = message
        logger.error(message)
        self._record_event("pending_intent_found", **intent)
        try:
            from src.ops.alerts import emit_alert

            emit_alert("pending_intent", message, level="error")
        except Exception:  # noqa: BLE001
            pass

    def _check_pending_intent(self) -> None:
        """检查上次是否崩在「点击后、记账前」。GUI 真下单时熔断等人工核对。"""
        self._pending_intent = None
        intents: list[dict[str, Any]] = []
        files_on_disk = self._intent_files()
        if not files_on_disk:
            self._pending_intents = intents
            self._pending_intent_files = []
            return
        for path in files_on_disk:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取委托意图失败，按有未完成委托处理: {}", exc)
                data = {}
            intents.append(dict(data) if isinstance(data, dict) else {})
        self._pending_intents = intents
        self._pending_intent_files = list(files_on_disk)
        # 对话/界面只展示最新那一笔；对账确认会把盘上所有未核对记录一起清掉。
        self._pending_intent = max(
            intents,
            key=lambda item: str(item.get("ts") or ""),
            default=None,
        )
        trade = self.cfg.get("trade") or {}
        if str(trade.get("executor", "sim")) != "gui" or bool(self.cfg.get("dry_run", True)):
            # 非 GUI 真下单通道由柜台回报/对账兜底，这些文件只是上一次残留。
            self._clear_order_intent()
            return
        # 文件在盘上就代表「点过、但没确认记账完成」。内容读不出来时更不能当没事。
        self._halt_on_pending_intent()

    def pending_intent_matches(
        self, symbol: str, side: str, volume: int
    ) -> dict[str, Any] | None:
        """
        手工录入的这一笔，如果和遗留委托是同一笔，返回那份委托内容。

        开仓比方向+手数；平仓的手数由程序持仓决定，只比方向。
        """
        intents = getattr(self, "_pending_intents", None)
        if intents is None:
            intents = [self._pending_intent] if self._pending_intent else []
        for intent in intents:
            if not isinstance(intent, dict) or not intent:
                continue
            if not _same_contract(symbol, str(intent.get("symbol") or "")):
                continue
            if str(intent.get("side") or "") != str(side or ""):
                continue
            if str(side) in {"buy_open", "sell_open"}:
                try:
                    want = int(intent.get("volume") or 0)
                except (TypeError, ValueError):
                    continue
                if want != int(volume):
                    continue
            return dict(intent)
        return None

    def confirm_positions_reviewed(self) -> tuple[bool, str]:
        """人工核对交易软件持仓后，清掉遗留委托意图并解除对应熔断。"""
        if self._pending_intent is None:
            return False, "没有待核对的委托记录。"
        intent = dict(self._pending_intent)
        self._clear_order_intent()
        if self.risk.halted and (
            self.risk.halt_kind == "pending_intent"
            or str(self.risk.halt_reason).startswith(self._INTENT_HALT_PREFIX)
        ):
            self.risk.clear_halt()
        self._persist_state()
        self._record_event("position_review_confirmed", **intent)
        return True, "已解除熔断，可以继续扫描。"

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
    ) -> bool:
        """更新策略仓位、风控、信号账户，并且只落盘一次。返回这一笔是否已经入账。"""
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

        # 1) 先写信号账户。实盘以这本账为准；dry_run 只看信号，不写账。
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

        try:
            self.risk.on_order_filled(decision.action.value, vol, symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            logger.exception("风控净仓同步失败: {}", exc)

        # 滑点记录：理论价（开仓=信号入场价，止损平仓=止损价）对比实际成交价，追加到 data/slippage_log.csv。
        # 只记录，不参与任何决策；出错不能影响记账。
        try:
            from src.account.slippage_log import record_fill
            from src.risk.universe_table import product_key

            theory = None
            if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
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
            slip = record_fill(
                self.root,
                symbol=symbol,
                action=decision.action.value,
                volume=vol,
                theory_price=theory,
                fill_price=fill_price,
                tick=float(tick_raw or 1),
                side_before=side_before,
                executor=str((self.cfg.get("trade") or {}).get("executor", "")),
                dry_run=bool(self.cfg.get("dry_run", True)),
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

        # 模拟测试也会走到这里，用来看内存里的仓对不对。不能把假成交写进正在用的账，也不能盖掉真实持仓文件。
        if self.cfg.get("dry_run", True):
            return True
        self._persist_state()
        return ok

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

    def _size_open(self, decision: StrategyDecision) -> tuple[int, str]:
        """
        开仓手数：先按资金管理算，策略指定的手数只能往下压。

        长线趋势等策略会在 meta 里写「单元手数×加仓次数」，这个数不能绕过
        每笔风险%和仓位上限，否则小权益下会开出买不起的仓。
        """
        meta = decision.meta or {}
        sized = self.sizer.calc_open(
            decision.symbol,
            decision.entry_price,
            decision.stop_price,
            current_abs_position=self.risk.total_abs_position(),
            positions=self.risk.positions,
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
            bar_id=bar_key,
            orders_paused=bool(getattr(self, "_orders_paused", False)),
            ledger_failed=bool(getattr(self, "_ledger_failed", False)),
            already_sent_this_bar=bool(bar_key and sent.get(decision.symbol) == bar_key),
            can_query_positions=caps.can_query_positions,
            can_query_orders=caps.can_query_orders,
            confirms_fill=caps.confirms_fill,
            stage_block=stage,
            has_working_order=working,
            blocks_new_opens=bool(getattr(trader, "blocks_new_opens", lambda: False)()) if trader is not None else False,
            books_note=str(getattr(trader, "_last_note", "") or "") if trader is not None else "",
            detail=detail,
            force=bool((decision.meta or {}).get("force")),
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
        elif code == "close_skip_no_position":
            logger.warning("策略要求平仓，但程序账上无持仓，跳过 {} | {}", decision.symbol, decision.reason)
        elif code == "size_skip":
            logger.warning("仓位为0，跳过开仓 {}: {}", decision.symbol, reason)
        elif code == "order_blocked_stage":
            logger.warning(reason)
        elif code == "order_working":
            logger.warning("上一笔 {} 还没有最终回报，先不报新单", decision.symbol)
        elif code == "risk_block":
            if "停止开新仓" in reason or "柜台" in reason or "对账" in reason:
                logger.warning("停止开新仓 {}: {}", decision.symbol, reason)
            else:
                logger.warning("风控拒绝 {}: {}", decision.symbol, reason)
        else:
            logger.warning("{} {}: {}", code, decision.symbol, reason)
        extra = {"reason": reason}
        if code == "order_dedup":
            extra = {"bar_id": bar_key}
        elif code == "close_skip_no_position":
            extra = {"reason": decision.reason}
        elif code == "order_working":
            extra = {"reason": "等待上一笔回报"}
        elif code in {"order_blocked_stage", "risk_block", "size_skip"}:
            extra = {"reason": reason}
        self._record_event(code, symbol=decision.symbol, side=side, **extra)
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

    def _order_session_open(self) -> bool:
        """当前是否在可下单的交易时段内。没配时段时放行。"""
        sessions = getattr(self, "sessions", None)
        if not sessions:
            return True
        from src.utils.session_clock import in_trading_sessions

        return bool(in_trading_sessions(sessions))

    def _execute_locked(self, decision: StrategyDecision) -> bool:
        """_execute 的实体；调用方已经持有交易锁。"""
        self._order_dispatch_started = False
        self._order_unclean = False
        if decision.action == StrategyAction.HOLD:
            logger.debug("{}: {}", decision.symbol, decision.reason)
            return False
        # 收盘后 / 周末不下单：挂着的老信号不能拿到已收盘的市场上去点。
        if not self._order_session_open():
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
            return False
        bar_key = str((decision.meta or {}).get("bar_id", "") or "")
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
        # 柜台报单必须带限价。平仓策略通常不给价格，就用这根 K 线的收盘价。
        position_side = None
        if caps.orders_via_broker:
            price = self._ctp_limit_price(decision)
            if price is None or price <= 0:
                logger.error("CTP 没有可用价格，这次不报单 | {}", decision.symbol)
                self._record_event("order_no_price", symbol=decision.symbol, side=side)
                self._release_signal_mark(decision)
                return False
            if decision.action == StrategyAction.CLOSE:
                held = self._position_for(decision.symbol)
                position_side = getattr(held, "side", None)
        elif caps.confirms_fill and decision.action != StrategyAction.CLOSE:
            price = decision.entry_price
        else:
            price = None

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
                )
            )
        elif decision.action == StrategyAction.BUY_OPEN:
            result = self.trader.buy_open(decision.symbol, vol, price)
        elif decision.action == StrategyAction.SELL_OPEN:
            result = self.trader.sell_open(decision.symbol, vol, price)
        else:
            result = self.trader.close_position(decision.symbol, vol, price)

        raw = getattr(result, "raw", None)
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
                    df = self.kline.fetch(decision.symbol, period=str(self.strategy.timeframe))
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

            if getattr(result, "assumed_fill", False):
                logger.warning(
                    "假定成交记账（请尽快核对交易软件真实持仓）| {} {} vol={}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                )

            self._apply_fill_accounting(decision.symbol, decision, vol, fill_price, fill_price)
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
            )
            return True
        else:
            logger.error("下单失败: {}", result.message)
            self.last_error = result.message
            self._record_event("order_fail", symbol=decision.symbol, side=side, message=result.message)
            return False

    def _release_signal_mark(self, decision: StrategyDecision) -> None:
        """
        下单没发出去时，把策略里「这根 K 线已处理」的标记撤销。

        策略在产生信号时就写标记，原本是为了同一根 K 线不重复评估；
        但下单失败也被一起锁住，信号会静默丢掉。这里只在确认没点单时撤销，
        让下一轮扫描还能在同一根 K 线上再试一次。
        """
        bar_key = str((decision.meta or {}).get("bar_id", "") or "")
        if not bar_key:
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

    def _price_tick_for(self, symbol: str) -> float:
        """每跳价格：先看品种表，再看 CTP/交易配置，最后退回 1。"""
        from src.risk.universe_table import product_key

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        box = contracts.get(product_key(symbol)) if isinstance(contracts, dict) else {}
        raw = (box or {}).get("price_tick")
        if raw in (None, ""):
            trade = self.cfg.get("trade") or {}
            raw_ctp = trade.get("ctp")
            ctp_box = raw_ctp if isinstance(raw_ctp, dict) else {}
            raw = ctp_box.get("price_tick", trade.get("price_tick", 1))
        try:
            tick = float(raw or 1)
        except (TypeError, ValueError):
            tick = 1.0
        return tick if tick > 0 else 1.0

    def _close_protect_ticks(self) -> int:
        """平仓保护滑点跳数。行情快时用收盘价原样挂限价常常挂不上。"""
        trade = self.cfg.get("trade") or {}
        try:
            return max(0, int(trade.get("ctp_close_protect_ticks", 2)))
        except (TypeError, ValueError):
            return 2

    def _ctp_limit_price(self, decision: StrategyDecision) -> float | None:
        """
        CTP 限价。开仓用策略价格；平仓用最新价加保护性滑点，挂成对手价方向。

        平仓用「已收线那根的收盘价」在快行情里挂不上，submit 超时撤单后要等下一轮，
        止损会被拖慢一整根K线。
        """
        if decision.action != StrategyAction.CLOSE and decision.entry_price and float(decision.entry_price) > 0:
            return float(decision.entry_price)
        try:
            df = self.kline.fetch(decision.symbol, period=str(self.strategy.timeframe))
            if df is None or len(df) == 0:
                return None
            price = float(df["close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 {} 最新价失败: {}", decision.symbol, exc)
            return None
        if price <= 0:
            return None
        # 平多是卖出、平空是买入：往对手价方向让几跳，确保能成交。
        protect = self._price_tick_for(decision.symbol) * self._close_protect_ticks()
        pos = self._position_for(decision.symbol)
        side = str(getattr(pos, "side", "") or "")
        if side == "long":
            price -= protect
        elif side == "short":
            price += protect
        return price if price > 0 else None

    def _on_ctp_fill(self, report: dict[str, Any]) -> bool:
        """CTP 回调线程入口：拿交易锁后入账，超时返回 False 交回补记路径。"""
        lock = getattr(self, "_trade_lock", None)
        if lock is None:
            return OrderExecutorMixin._on_ctp_fill_locked(self, report)
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
        decision = StrategyDecision(
            action=action,
            symbol=str(report.get("symbol") or ""),
            reason="CTP成交回报",
            stop_price=report.get("stop_price"),
            entry_price=price,
            meta={"bar_id": str(report.get("bar_id") or "")},
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
        )
        if not ok:
            return False
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
