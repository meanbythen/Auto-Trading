"""委托意图落盘、未核对委托熔断与订单时间线。

方法挂在 OrderIntentMixin 上，共享状态由 TradingBot 提供；本模块不持有运行状态。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.strategy import StrategyAction, StrategyDecision
from src.trader.order_timeline import elapsed_ms, new_trace_id, now_iso


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


class OrderIntentMixin:
    """委托意图与时间线：点击前落盘、异常保留、人工核对后清除。"""

    _INTENT_HALT_PREFIX = "发现未完成的下单记录"
    _pending_intent: dict[str, Any] | None = None
    # 盘上所有未核对委托（按笔一个文件）和对应路径；只在对账确认时一起清掉。
    _pending_intents: list[dict[str, Any]] | None = None
    _pending_intent_files: list[Path] | None = None

    def _timeline_trace(self, decision: StrategyDecision) -> str:
        """给决策补上本地关联号和价格证据；柜台编号仍由 CTP 单独返回。"""
        meta = dict(decision.meta or {})
        trace_id = str(meta.get("trace_id") or "") or new_trace_id()
        meta["trace_id"] = trace_id
        meta.setdefault("decided_at", now_iso())
        if "theory_price" not in meta:
            if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
                meta["theory_price"] = decision.entry_price
            elif decision.action == StrategyAction.CLOSE and decision.stop_price:
                meta["theory_price"] = decision.stop_price
        decision.meta = meta
        return trace_id

    def _record_order_timeline(
        self,
        stage: str,
        decision: StrategyDecision,
        **fields: Any,
    ) -> None:
        """记录器不存在时安静跳过，兼容 __new__ 构造的测试替身。"""
        timeline = getattr(self, "_order_timeline", None)
        if timeline is None:
            return
        trace_id = self._timeline_trace(decision)
        meta = decision.meta or {}
        row = {
            "symbol": decision.symbol,
            "side": decision.action.value,
            "bar_id": str(meta.get("bar_id") or ""),
            "theory_price": meta.get("theory_price"),
            "latest_price": meta.get("latest_price"),
            "latest_price_at": meta.get("latest_price_at") or "",
            "order_price": meta.get("order_price"),
        }
        row.update(fields)
        try:
            timeline.record(stage, trace_id=trace_id, **row)
        except Exception as exc:  # noqa: BLE001
            # 时间线是旁路证据，尤其不能因为写盘异常阻断止损和平仓。
            logger.warning("订单时间线记录失败 {}: {}", stage, exc)

    def _start_decision_timeline(self, decision: StrategyDecision) -> None:
        """串起行情、已完成信号 K 线和策略决策；只有可执行动作才建订单链。"""
        self._timeline_trace(decision)
        meta = decision.meta or {}
        health = dict(meta.get("market_health") or {})
        try:
            volume = int(meta.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0
        self._record_order_timeline(
            "market_snapshot",
            decision,
            volume=volume,
            market_time=health.get("latest_bar_time") or health.get("signal_bar_time") or "",
            source=health.get("source") or "",
            market_health=health,
        )
        self._record_order_timeline(
            "bar_closed",
            decision,
            volume=volume,
            signal_bar_time=health.get("signal_bar_time") or meta.get("bar_id") or "",
            timeframe=health.get("period") or health.get("timeframe") or "",
        )
        decided_at = str(meta.get("decided_at") or "")
        self._record_order_timeline(
            "strategy_decision",
            decision,
            volume=volume,
            reason=decision.reason,
            signal_price=decision.entry_price,
            stop_price=decision.stop_price,
            decided_at=decided_at,
            stage_ms=elapsed_ms(health.get("evaluated_at"), decided_at),
        )


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
            "trace_id": str((decision.meta or {}).get("trace_id", "") or ""),
            "theory_price": (decision.meta or {}).get("theory_price"),
            "latest_price": (decision.meta or {}).get("latest_price"),
            "market_health": dict((decision.meta or {}).get("market_health") or {}),
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
