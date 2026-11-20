"""
CTP 执行通道。

功能：给策略用的下单接口。内部完成登录、按合约报单、区分平今和平昨、接收回报、和柜台对账。
原理：策略仍然只说买开、卖开或平仓。这一层把它翻译成柜台认的方向和开平。
上期所、能源中心、大商所先平昨再平今。郑商所、中金所用普通平仓。
和柜台净持仓不一致时，停止开新仓，平仓仍然允许。
动态权益、可用资金以期货公司柜台为准，对不上就改本地账，不因此拦开仓。
"""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Callable

from loguru import logger

from src.trader.base import DEFAULT_CAPABILITIES, Capabilities, OrderRequest, OrderResult, TraderBase
from src.trader.ctp_gateway import STATUS_TEXT, CtpGateway, CtpSettings
from src.trader.ctp_logic import (
    OFFSET_OPEN,
    CloseSlice,
    PositionView,
    ReconcileReport,
    book_differences,
    check_order_instruction,
    describe_positions,
    describe_working_orders,
    close_direction,
    is_terminal,
    needs_close_split,
    open_direction,
    plan_close,
    product_code,
    symbol_key,
    three_way_diff,
)
from src.trader.order_timeline import OrderTimeline, elapsed_ms, now_iso


class CtpTrader(TraderBase):
    """华泰 CTP 交易通道。可以注入 gateway，测试时不连接真实柜台。"""

    def __init__(self, cfg: dict[str, Any], gateway: CtpGateway | None = None) -> None:
        self.cfg = cfg
        # 测试可以塞进一个假的网关。这时用网关自己的连接参数，避免和空配置各算各的。
        self.settings = gateway.settings if gateway is not None else CtpSettings.from_cfg(cfg)
        self.gateway = gateway or CtpGateway(self.settings)
        self._timeline = OrderTimeline(self.settings.project_root or cfg.get("_project_root"))
        self.gateway.order_event_handler = self._record_gateway_event
        self._books_match = False
        self._last_query = 0.0
        self._last_note = "还没有和柜台对过账"
        # 交易台和常驻共用这一份通道。查挂单到报单之间只放一笔过去，
        # 否则两边各自查到「没有挂单」，会对同一根 K 线各报一笔。
        self._order_gate = threading.Lock()
        self.last_broker_equity: float | None = None
        # 最近一次三方对账的完整结果，给运行台和启动修复用。
        self.last_report: ReconcileReport | None = None
        # 最近一次柜台资金查询的完整数字，给常驻同步账号、可用、保证金、当日盈亏。
        self.last_broker_snapshot: dict[str, Any] | None = None
        fronts = getattr(self.settings, "td_fronts", ()) or (self.settings.td_front,)
        logger.info(
            "CTP 通道已准备。操作账={} 账号={} 前置{}台 第一台={} AppID={}，平今平昨品种={}",
            "真实" if self.settings.account_kind == "live" else "仿真",
            self.settings.user_id,
            len([item for item in fronts if item]),
            self.settings.td_front,
            self.settings.app_id,
            ",".join(self.settings.close_split),
        )

    @property
    def capabilities(self) -> Capabilities:
        return DEFAULT_CAPABILITIES["ctp"]

    def connect(self) -> tuple[bool, str]:
        """连接交易前置，并完成认证、登录、结算单确认。"""
        if self.cfg.get("dry_run", True):
            return False, "模拟开关开着，不会连接柜台"
        return self.gateway.connect()

    def shutdown(self) -> None:
        """退出时释放动态库。"""
        self.gateway.close()

    def set_fill_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        """全部成交时回调。用来把真实成交写进程序自己的账，并且只写一次。"""
        self.gateway.fill_handler = handler

    def has_working_order(self, symbol: str | None = None) -> bool:
        """这一笔还在柜台里，没有全部成交、拒单或撤单。"""
        for order in self.gateway.table.orders.values():
            if is_terminal(order.status):
                continue
            if symbol and symbol_key(order.symbol) != symbol_key(symbol):
                continue
            return True
        return False

    def blocks_new_opens(self) -> bool:
        """持仓对不上、或上一笔还没完，不允许开新仓。资金差额不拦。"""
        if self.has_working_order() or self.gateway.unknown_working:
            self._last_note = "有报单还没全部成交，先停止开新仓"
            return True
        return not self._books_match

    def mark_reconcile_unknown(self, reason: str) -> None:
        """对账过程抛异常：按「未知=不匹配」处理，停止开新仓，等下一轮再对。"""
        self._books_match = False
        self._last_note = reason

    def reconcile(self, internal: dict[str, int], ledger: dict[str, int] | None = None) -> tuple[bool, str]:
        """
        查询柜台真实持仓和资金。

        资金按期货公司数字改本地账，差额不拦开仓。
        净持仓对不上才记下失败，调用方停止开新仓。
        查询间隔没到时，用上一次的柜台结果再比一次，避免频繁查询被柜台拒绝。

        ledger 传入时按「柜台 / 信号账户 / 程序内存」三方比较；
        不传时保持旧行为，只比程序和柜台。
        """
        memory = {symbol_key(k): int(v or 0) for k, v in (internal or {}).items()}
        ledger_nets = {symbol_key(k): int(v or 0) for k, v in (ledger or {}).items()}
        at = datetime.now().isoformat(timespec="seconds")

        def _unavailable(note: str) -> tuple[bool, str]:
            """柜台查询没做成：禁止据此修复内存，只留下原因。"""
            self._books_match = False
            self._last_note = note
            self.last_report = ReconcileReport(
                at=at,
                queried=False,
                broker={},
                ledger=dict(ledger_nets) if ledger is not None else {},
                memory=dict(memory),
                funds=None,
                ok=False,
                diffs=[note],
            )
            return False, note

        if self.cfg.get("dry_run", True):
            return _unavailable("模拟开关开着，没有柜台持仓可对")
        if not self.gateway.session.ready:
            return _unavailable("还没登录成功，先停止开新仓")
        now = time.time()
        if now - self._last_query >= self.settings.query_interval_sec:
            ok = self.gateway.refresh()
            self._last_query = now
            if not ok:
                return _unavailable("查询柜台持仓或资金失败，先停止开新仓")
        self._capture_broker_equity()
        extra_text = self._sync_signal_book()
        available = self.gateway.available
        money = f"{available:.2f}" if available is not None else "未知"
        broker = {symbol_key(k): int(v.net) for k, v in self.gateway.positions.items()}

        if ledger is None:
            notes = book_differences(internal, self.gateway.positions)
            self._books_match = not notes
            report_ok = not notes
            diffs = list(notes)
            unclaimed: list[str] = []
            phantom: list[str] = []
            extra: list[str] = []
        else:
            report_ok, diffs, unclaimed, phantom, extra = three_way_diff(
                broker, ledger_nets, memory
            )
            self._books_match = not diffs

        if diffs:
            self._last_note = "；".join(diffs) + f"。可用资金 {money}。持仓对不上，停止开新仓"
            if extra_text:
                self._last_note = self._last_note + "。" + extra_text
            prev = getattr(self, "last_report", None)
            same = bool(prev is not None and list(getattr(prev, "diffs", []) or []) == list(diffs))
            if same:
                logger.debug("柜台持仓仍不一致: {}", self._last_note)
            else:
                logger.warning("柜台持仓不一致: {}", self._last_note)
        else:
            self._last_note = f"持仓一致。可用资金 {money}"
            if extra_text:
                self._last_note = self._last_note + "。" + extra_text
            logger.info("柜台对账通过: {}", self._last_note)
        self.last_report = ReconcileReport(
            at=at,
            queried=True,
            broker=broker,
            ledger={} if ledger is None else dict(ledger_nets),
            memory=dict(memory),
            funds=available,
            ok=report_ok,
            diffs=diffs,
            unclaimed=unclaimed,
            phantom=phantom,
            extra=extra,
        )
        return report_ok, self._last_note

    def _capture_broker_equity(self) -> None:
        """记下柜台刚查到的资金，给常驻同步账号、权益、可用和当日盈亏。"""
        from src.trader.ctp_display import parse_account

        raw_account = dict(getattr(self.gateway, "account", None) or {})
        available = getattr(self.gateway, "available", None)
        if available is not None and "available" not in raw_account:
            raw_account["available"] = available
        if "balance" not in raw_account and available is not None:
            raw_account["balance"] = available
        parsed = parse_account(raw_account)
        settings = getattr(self.gateway, "settings", None)
        broker_id = str(parsed.get("account_id") or getattr(settings, "user_id", "") or "")
        parsed["user_id"] = broker_id
        parsed["account_id"] = broker_id
        self.last_broker_snapshot = parsed
        try:
            value = float(parsed.get("balance") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value <= 0:
            try:
                value = float(available) if available is not None else 0.0
            except (TypeError, ValueError):
                return
        if value > 0:
            self.last_broker_equity = value

    def fetch_account_snapshot(self) -> tuple[bool, str]:
        """向柜台查一遍持仓和可用资金，给交易台对照。"""
        if not self.gateway.session.ready:
            return False, "还没登录，不能查持仓"
        ok = self.gateway.refresh()
        if not ok:
            return False, "查询持仓或资金超时，请过一两秒再点「查持仓」"
        text = describe_positions(self.gateway.positions, self.gateway.available)
        extra = self._sync_signal_book()
        if extra:
            text = text + " " + extra
        logger.info("柜台持仓查询：{}", text)
        return True, text

    def _sync_signal_book(self) -> str:
        """把柜台资金写进华泰仿真或华泰真实那本账，给账户分析看。"""
        root = str(self.settings.project_root or "") or "."
        try:
            from src.account.ctp_sync import push_gateway_account

            return push_gateway_account(root, self.gateway)
        except Exception as exc:  # noqa: BLE001
            logger.warning("柜台资金没写进账户分析: {}", exc)
            return ""

    def fetch_contracts(self, keyword: str) -> tuple[bool, str]:
        """按代码或前缀查合约列表，给交易台合约页。"""
        if not self.gateway.session.ready:
            return False, "还没登录，不能查合约"
        return self.gateway.query_contracts(keyword)

    def fetch_contract_costs(self, symbol: str) -> tuple[bool, dict]:
        """点中一行后补保证金和手续费。"""
        if not self.gateway.session.ready:
            return False, {}
        return self.gateway.query_contract_costs(symbol)

    def fetch_working_orders(self) -> tuple[bool, str]:
        """向柜台查今天的委托（含已成交）。改价前先看还有没有未完成的。"""
        if not self.gateway.session.ready:
            return False, "还没登录，不能查委托"
        ok, rows = self.gateway.query_working_orders()
        if not ok:
            return False, "查询委托超时，请过一两秒再点「查委托」"
        text = describe_working_orders(rows)
        logger.info("仿真委托查询：{}", text)
        return True, text

    def fetch_trades(self) -> tuple[bool, str]:
        """向柜台查今天的成交。"""
        if not self.gateway.session.ready:
            return False, "还没登录，不能查成交"
        ok, rows = self.gateway.query_trade_history()
        if not ok:
            return False, "查询成交超时，请过一两秒再点「刷新成交」"
        if not rows:
            text = "今天还没有成交。"
        else:
            text = f"今天成交 {len(rows)} 笔（窗口成交表会列出明细）"
        logger.info("柜台成交查询：{}", text)
        return True, text

    def cancel_ref(self, order_ref: str) -> OrderResult:
        """撤销指定编号。交易台「撤选中」和常驻超时撤单都走网关同一条撤单。"""
        ref = str(order_ref or "").strip()
        if not ref:
            return OrderResult(ok=False, message="没有委托编号，不能撤", assumed_fill=False)
        ok, message = self.gateway.cancel(ref)
        return OrderResult(ok=ok, message=message, order_id=ref, assumed_fill=False)

    def cancel_all_working(self, symbol: str = "") -> OrderResult:
        """撤销还没完结的委托。可只撤当前合约。"""
        self.fetch_working_orders()
        targets = []
        for order in self.gateway.table.orders.values():
            if is_terminal(order.status):
                continue
            if symbol and symbol_key(order.symbol) != symbol_key(symbol):
                continue
            targets.append(order)
        if not targets:
            return OrderResult(ok=True, message="没有未成交委托", assumed_fill=False)
        notes: list[str] = []
        for order in targets:
            ok, message = self.gateway.cancel(order.order_ref)
            notes.append(message)
            time.sleep(0.2)
        return OrderResult(ok=True, message="；".join(notes), assumed_fill=False)

    def _record_gateway_event(self, stage: str, payload: dict[str, Any]) -> None:
        row = dict(payload)
        trace_id = str(row.pop("trace_id", "") or "")
        try:
            self._timeline.record(stage, trace_id=trace_id, **row)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CTP 订单时间线写失败 {}: {}", stage, exc)

    def _record_preflight(
        self,
        req: OrderRequest,
        started_at: str,
        *,
        ok: bool,
        check: str,
        message: str,
    ) -> dict[str, Any]:
        finished_at = now_iso()
        try:
            return self._timeline.record(
                "preflight",
                trace_id=req.trace_id,
                symbol=req.symbol,
                side=req.side,
                volume=int(req.volume),
                theory_price=(req.meta or {}).get("theory_price"),
                latest_price=(req.meta or {}).get("latest_price"),
                latest_price_at=(req.meta or {}).get("latest_price_at") or "",
                order_price=req.price,
                bar_id=req.bar_id,
                ok=bool(ok),
                check=check,
                message=message,
                account_kind=self.settings.account_kind,
                dry_run=bool(self.cfg.get("dry_run", True)),
                broker_ready=bool(self.gateway.session.ready and not self.gateway.session.need_order_check),
                preflight_at=finished_at,
                stage_ms=elapsed_ms(started_at, finished_at),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("CTP 前置检查时间线写失败: {}", exc)
            return {"ts": finished_at}

    def _reject_order(self, req: OrderRequest, started_at: str, check: str, message: str) -> OrderResult:
        row = self._record_preflight(req, started_at, ok=False, check=check, message=message)
        finished_at = now_iso()
        try:
            self._timeline.record(
                "channel_result",
                trace_id=req.trace_id,
                symbol=req.symbol,
                side=req.side,
                volume=int(req.volume),
                order_price=req.price,
                ok=False,
                message=message,
                channel_result_at=finished_at,
                stage_ms=elapsed_ms(started_at, finished_at),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("CTP 拒绝结果时间线写失败: {}", exc)
        return OrderResult(
            ok=False,
            message=message,
            assumed_fill=False,
            raw={
                "trace_id": req.trace_id,
                "bar_id": req.bar_id,
                "meta": dict(req.meta or {}),
                "preflight_at": row.get("ts") or "",
                "channel_result_at": finished_at,
            },
        )

    def place_order(self, req: OrderRequest) -> OrderResult:
        """
        提交报单。交易台按钮和常驻策略的唯一发单入口。

        开仓一笔。平仓在沥青、玉米上按今仓昨仓拆开，可能变成两笔。
        只有柜台确认全部成交才算 filled。点击成功不算成交。
        同一时间只放一笔单过这道门，交易台和常驻不会各查一次挂单、各报一笔。
        """
        gate = getattr(self, "_order_gate", None)
        with (gate if gate is not None else nullcontext()):
            return self._place_order_locked(req)

    def _place_order_locked(self, req: OrderRequest) -> OrderResult:
        started_at = now_iso()
        if self.cfg.get("dry_run", True):
            return self._reject_order(req, started_at, "dry_run", "模拟开关开着，不会向 CTP 发单")
        missing = self.settings.missing()
        if missing:
            return self._reject_order(req, started_at, "config", "CTP 还缺：" + "、".join(missing))
        if req.price is None or float(req.price) <= 0:
            return self._reject_order(req, started_at, "price", "CTP 限价单需要价格")
        blocked = check_order_instruction(
            req.symbol,
            float(req.price),
            int(req.volume),
            price_tick=self.settings.price_tick,
            max_volume=self.settings.max_order_volume,
        )
        if blocked:
            logger.error(blocked)
            return self._reject_order(req, started_at, "instruction", blocked)
        if self.gateway.trading_paused and req.side != "close":
            text = "已暂停开新仓，本笔没有发到柜台"
            logger.warning(text)
            return self._reject_order(req, started_at, "paused", text)
        if not self.gateway.session.ready or self.gateway.session.need_order_check:
            ok, message = self.connect()
            if not ok:
                return self._reject_order(req, started_at, "connection", message)
        # 连上并查回未完成报单之后再查挂单。断线时本地委托表可能是空的，
        # 先查会把「柜台其实还挂着单」当成没有，重登后又报一笔。
        if not self.settings.desk_session and self.has_working_order(req.symbol):
            text = (
                f"{req.symbol} 还有未成交委托占着仓。"
                "请先点「查委托」确认，再点「撤最近一笔」或「全撤未成交」，然后才能改价重报。"
            )
            logger.warning(text)
            return self._reject_order(req, started_at, "working_order", text)

        volume = int(req.volume)
        if volume <= 0:
            return self._reject_order(req, started_at, "volume", "手数必须大于 0")

        if req.side == "close":
            slices, direction, error = self._close_plan(req)
            if error:
                return self._reject_order(req, started_at, "position", error)
        else:
            # 买开、卖开都是开仓，不拆今昨。
            slices = [CloseSlice(OFFSET_OPEN, volume)]
            direction = open_direction(req.side)

        preflight = self._record_preflight(
            req,
            started_at,
            ok=True,
            check="config,instruction,connection,position",
            message="CTP 前置检查通过",
        )
        orders = []
        for piece in slices:
            order = self.gateway.submit(
                symbol=req.symbol,
                direction=direction,
                offset=piece.offset,
                volume=piece.volume,
                price=float(req.price),
                stop_price=req.stop_price,
                bar_id=req.bar_id,
                trace_id=req.trace_id,
                meta=req.meta,
            )
            orders.append(order)
            logger.info(
                "CTP {} {} {} {} 手 @ {} | {}",
                req.symbol,
                _direction_text(direction),
                _offset_text(piece.offset),
                piece.volume,
                req.price,
                _status_text(order),
            )

        filled_volume = sum(int(order.filled) for order in orders)
        accounted_volume = sum(int(order.accounted_volume) for order in orders)
        all_filled = filled_volume == volume and all(order.status == "filled" for order in orders)
        texts = [_status_text(order) for order in orders]
        message = "，".join(texts)
        if not all_filled and filled_volume > 0:
            message += f"（共成交 {filled_volume} 手）"
        elif filled_volume <= 0 and any(order.status == "cancelled" for order in orders):
            message += "（未成交）"
        rejected = [order.message for order in orders if order.status == "rejected" and order.message]
        if rejected:
            message = message + "：" + "；".join(rejected)
        avg = _avg_price(orders)
        # 还在柜台排队、或者已经部分成交，都说明这一笔已经发出去过。
        working = any(order.status in {"submitted", "partial"} for order in orders)
        finished_at = now_iso()
        raw = {
            "trace_id": req.trace_id,
            "meta": dict(req.meta or {}),
            "preflight_at": preflight.get("ts") or "",
            "channel_result_at": finished_at,
            "filled": all_filled,
            "accounted": filled_volume > 0 and accounted_volume >= filled_volume,
            "fill_price": avg,
            "filled_volume": filled_volume,
            "accounted_volume": accounted_volume,
            # 每笔报单的成交情况。拆平今/平昨时会不止一笔，补记要按报单逐笔算增量。
            "orders": [
                {
                    "order_ref": order.order_ref,
                    "trace_id": order.trace_id,
                    "meta": dict(order.meta or {}),
                    "filled": int(order.filled),
                    "accounted_volume": int(order.accounted_volume),
                    "avg_price": float(order.avg_price or 0),
                    "status": order.status,
                    "foreign": bool(order.foreign),
                    "sent_at": order.sent_at,
                    "last_report_received_at": order.last_report_received_at,
                    "last_trade_received_at": order.last_trade_received_at,
                    "insert_time": order.insert_time,
                    "order_sys_id": order.order_sys_id,
                }
                for order in orders
            ],
            "statuses": texts,
            "working": working,
        }
        try:
            self._timeline.record(
                "channel_result",
                trace_id=req.trace_id,
                symbol=req.symbol,
                side=req.side,
                volume=volume,
                order_id=",".join(order.order_ref for order in orders if order.order_ref),
                order_price=req.price,
                message=message or "没有收到回报",
                ok=all_filled or working or filled_volume > 0,
                filled_volume=filled_volume,
                accounted_volume=accounted_volume,
                fill_price=avg or None,
                fill_kind="broker" if filled_volume > 0 else "none",
                channel_result_at=finished_at,
                stage_ms=elapsed_ms(started_at, finished_at),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("CTP 通道结果时间线写失败: {}", exc)
        return OrderResult(
            ok=all_filled or working or filled_volume > 0,
            message=message or "没有收到回报",
            order_id=",".join(order.order_ref for order in orders if order.order_ref),
            assumed_fill=False,
            raw=raw,
        )

    def _close_plan(self, req: OrderRequest) -> tuple[list[Any], str, str]:
        """决定平仓方向，以及要不要拆成平今、平昨。返回 (拆分, 方向, 错误)。"""
        split = needs_close_split(req.symbol) or product_code(req.symbol) in set(self.settings.close_split)
        if split and not self.gateway.positions_ready:
            if not self.gateway.refresh():
                return [], "", "还没查到今仓和昨仓，先不报平仓"
        side = str(req.position_side or "").lower()
        view = self.gateway.positions.get(symbol_key(req.symbol), PositionView())
        if side not in {"long", "short"}:
            side = "short" if view.net < 0 else "long"
        if side == "short":
            today, yesterday = view.short_today, view.short_yesterday
        else:
            today, yesterday = view.long_today, view.long_yesterday
        held = int(today) + int(yesterday)
        if not self.settings.desk_session and held <= 0:
            return [], "", "没有可平仓位，未向柜台报平仓"
        # 交易台空仓仍发平仓，好让华泰截到柜台「持仓不足」。
        slices = plan_close(
            req.symbol,
            int(req.volume),
            today=today,
            yesterday=yesterday,
            split_products=self.settings.close_split,
        )
        return slices, close_direction(side), ""

    def cancel_latest(self, symbol: str = "") -> OrderResult:
        """撤销该合约最近一笔还没结束的报单。没有编号就先不发。"""
        chosen = None
        for order in self.gateway.table.orders.values():
            if is_terminal(order.status):
                continue
            if symbol and symbol_key(order.symbol) != symbol_key(symbol):
                continue
            chosen = order
        if chosen is None:
            return OrderResult(ok=False, message="没有可以撤销的报单", assumed_fill=False)
        ok, message = self.gateway.cancel(chosen.order_ref)
        return OrderResult(ok=ok, message=message, order_id=chosen.order_ref, assumed_fill=False)


def _direction_text(direction: str) -> str:
    return "买" if direction == "0" else "卖"


def _offset_text(offset: str) -> str:
    return {
        "0": "开仓",
        "1": "平仓",
        "3": "平今",
        "4": "平昨",
    }.get(offset, offset)


def _status_text(order: Any) -> str:
    """报单状态。撤单时如果已经成交过，要把成交手数说清楚。"""
    status = str(getattr(order, "status", "") or "")
    filled = int(getattr(order, "filled", 0) or 0)
    if status == "cancelled" and filled > 0:
        return f"成交{filled}手后撤单"
    return STATUS_TEXT.get(status, status)


def _avg_price(orders: list[Any]) -> float:
    total_qty = 0
    total_money = 0.0
    for order in orders:
        qty = int(getattr(order, "filled", 0) or 0)
        if qty <= 0:
            continue
        total_qty += qty
        total_money += float(getattr(order, "avg_price", 0) or 0) * qty
    if total_qty <= 0:
        return 0.0
    return total_money / total_qty
