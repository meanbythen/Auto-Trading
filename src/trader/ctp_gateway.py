"""
CTP 交易网关。

功能：
  连接交易前置，按顺序做认证、登录、确认结算单。
  报单后等回报：已报、拒单、部分成交、全部成交、撤单。
  断线后由柜台自动重连；重连成功先查未完成报单，再允许发新单。
  定时查询真实持仓和可用资金，交给上层和程序自己的账比较。

原理：这里不判断买卖点。它只记住柜台走到哪一步，以及每一笔报单现在是什么状态。
测试时可以换一个假的客户端，不需要真的动态库和网络。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from loguru import logger

from src.trader.ctp_logic import (
    OrderTable,
    PositionView,
    SessionTracker,
    TrackedOrder,
    absorb_position_row,
    OrderActivity,
    apply_order_report,
    apply_trade,
    ctp_instrument_id,
    is_terminal,
    symbol_key,
    test_front_blocks_live_account,
)


STATUS_TEXT = {
    "pending": "待报",
    "submitted": "已报",
    "rejected": "拒单",
    "partial": "部分成交",
    "filled": "全部成交",
    "cancelled": "撤单",
}


def _flag(value: Any, default: bool = True) -> bool:
    """配置里的开关可能是 true/false，也可能是字符串。认不出来就用默认值。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def probe_front_tcp(td_front: str, timeout_sec: float = 3.0) -> bool | None:
    """
    只测交易前置的 IP 端口能不能连上。

    返回 True/False；地址写得不像 tcp://主机:端口 时返回 None。
    端口通不等于柜台握手完成，但能区分「网络拦了」和「柜台没回应」。
    """
    text = str(td_front or "").strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"tcp://{text}")
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout_sec)):
            return True
    except OSError:
        return False


@dataclass
class CtpSettings:
    """连接柜台需要的参数。密码和认证码只留在内存里。"""

    td_front: str
    broker_id: str
    user_id: str
    password: str
    app_id: str
    auth_code: str
    flow_dir: str
    project_root: str
    order_wait_sec: float = 8.0
    query_interval_sec: float = 30.0
    login_timeout_sec: float = 20.0
    close_split: tuple[str, ...] = ("bu", "c")
    price_tick: float = 1.0
    max_order_volume: int = 20
    order_count_warn: int = 8
    cancel_count_warn: int = 8
    # 等待回报超时、单子还挂在柜台时，先撤掉再返回，避免过时委托继续成交。
    auto_cancel_on_timeout: bool = True
    cancel_wait_sec: float = 3.0
    account_user_id: str = ""
    # 仿真报价走行情端口。空着时，华泰测试交易端口 27226 会配对到 27236。
    md_front: str = ""

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "CtpSettings":
        trade = cfg.get("trade") or {}
        ctp = trade.get("ctp") or {}
        account = cfg.get("account") or {}
        root = str(cfg.get("_project_root") or "")
        flow = str(ctp.get("flow_dir") or "data/ctp_flow")
        flow_path = Path(flow)
        if root and not flow_path.is_absolute():
            flow_path = Path(root) / flow_path
        products = ctp.get("close_split_products") or ["bu", "c"]
        return cls(
            td_front=str(ctp.get("td_front") or "").strip(),
            broker_id=str(ctp.get("broker_id") or "").strip(),
            user_id=str(ctp.get("user_id") or account.get("user_id") or "").strip(),
            password=str(ctp.get("resolved_password") or ""),
            app_id=str(ctp.get("app_id") or "client_AutoTrading_1.0").strip(),
            auth_code=str(ctp.get("resolved_auth_code") or ""),
            flow_dir=str(flow_path),
            project_root=root,
            order_wait_sec=float(ctp.get("order_wait_sec") or 8),
            query_interval_sec=float(ctp.get("query_interval_sec") or 30),
            login_timeout_sec=float(ctp.get("login_timeout_sec") or 20),
            close_split=tuple(str(item).lower() for item in products),
            price_tick=float(ctp.get("price_tick") or 1),
            max_order_volume=int(ctp.get("max_order_volume") or 20),
            order_count_warn=int(ctp.get("order_count_warn") or 8),
            cancel_count_warn=int(ctp.get("cancel_count_warn") or 8),
            auto_cancel_on_timeout=_flag(ctp.get("auto_cancel_on_timeout"), True),
            cancel_wait_sec=float(ctp.get("cancel_wait_sec") or 3),
            account_user_id=str(account.get("user_id") or "").strip(),
            md_front=str(ctp.get("md_front") or "").strip(),
        )

    def missing(self) -> list[str]:
        """还没填的必填项。不把密码和认证码本身算进说明文字。"""
        names: list[str] = []
        if not self.td_front:
            names.append("交易前置")
        if not self.broker_id:
            names.append("BrokerID")
        if not self.user_id:
            names.append("资金账号")
        if not self.password:
            names.append("交易密码")
        if not self.app_id:
            names.append("AppID")
        if not self.auth_code:
            names.append("认证码")
        return names


class CtpGateway:
    """登录状态机 + 报单表。客户端负责真的发报文。"""

    def __init__(self, settings: CtpSettings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client
        self.session = SessionTracker()
        self.table = OrderTable()
        self.positions: dict[str, PositionView] = {}
        self.positions_ready = False
        # 重连后查到、但程序自己没有记住的未完成报单。有这种单就先不开新仓。
        self.unknown_working: list[str] = []
        self.available: float | None = None
        self.fill_handler: Callable[[dict[str, Any]], None] | None = None
        self._lock = threading.RLock()
        self._ready_event = threading.Event()
        self._pos_event = threading.Event()
        self._account_event = threading.Event()
        # 查涨跌停时等柜台把合约资料回完。和报单等待分开，避免两件事互相叫醒。
        self._instrument_event = threading.Event()
        self._instrument: dict[str, Any] | None = None
        self._instrument_error = ""
        self._waiters: dict[str, threading.Event] = {}
        self._pending_positions: dict[str, PositionView] = {}
        self._started = False
        self._querying_positions = False
        self.activity = OrderActivity(
            order_warn=self.settings.order_count_warn,
            cancel_warn=self.settings.cancel_count_warn,
        )
        # 测试时可以手动暂停。暂停后新报单留在本机，不发到柜台。
        self.trading_paused = False
        # 仿真公用账号会把别人的成交一并推过来。那些单不是我们发的，只记次数，不逐条当错误。
        self.foreign_reports = 0

    def connect(self) -> tuple[bool, str]:
        """
        连上并完成认证、登录、结算单确认，然后先查一遍报单。

        已经就绪就直接返回。缺参数或动态库时返回失败原因，不抛给界面。
        """
        blocked = test_front_blocks_live_account(
            self.settings.td_front,
            self.settings.user_id,
            self.settings.account_user_id,
        )
        if blocked:
            self.session.last_error = blocked
            logger.error(blocked)
            return False, blocked
        missing = self.settings.missing()
        if missing:
            text = "还不能连接柜台，缺少：" + "、".join(missing)
            self.session.last_error = text
            return False, text
        if self.session.ready and not self.session.need_order_check:
            return True, "交易前置已登录，结算单已确认"
        if self.client is None:
            try:
                from src.trader.ctp_td import OpenctpClient

                self.client = OpenctpClient()
            except Exception as exc:  # noqa: BLE001
                text = f"CTP 动态库没有加载成功：{exc}"
                self.session.last_error = text
                logger.error(text)
                return False, text
        self._ready_event.clear()
        if not self._started:
            try:
                self.client.start(self.settings, self)
                self._started = True
            except Exception as exc:  # noqa: BLE001
                text = f"连接交易前置失败：{exc}"
                self.session.last_error = text
                logger.exception(text)
                self._abandon_connect()
                return False, text
        if not self._ready_event.wait(self.settings.login_timeout_sec):
            text = self.session.last_error or self._timeout_hint()
            self.session.last_error = text
            # 超时后丢掉旧连接，否则再点「连接登录」只会干等同一条死连接。
            self._abandon_connect()
            return False, text
        if not self.session.ready or self.session.need_order_check:
            text = self.session.last_error or "认证、登录或结算单确认没有完成"
            self._abandon_connect()
            return False, text
        return True, "认证、登录、结算单确认完成，并已查询未完成报单"

    def _abandon_connect(self) -> None:
        """连接、认证或登录没走完时释放接口，下次可以重新 RegisterFront。"""
        client = self.client
        self._started = False
        self.client = None
        if self.session.phase != "ready":
            self.session.phase = "idle"
        if client is not None and hasattr(client, "release"):
            try:
                client.release()
            except Exception as exc:  # noqa: BLE001
                logger.warning("放弃连接时释放 CTP 接口出错: {}", exc)

    def _timeout_hint(self) -> str:
        """根据端口能不能通，写出用户能看懂的超时原因。"""
        front = self.settings.td_front
        wait = int(self.settings.login_timeout_sec)
        tcp_ok = probe_front_tcp(front)
        if tcp_ok is False:
            return (
                f"连不上交易前置 {front}（网络或防火墙拦截）。"
                "请检查网络，或向华泰确认最新仿真前置地址。"
            )
        if tcp_ok is True:
            return (
                f"交易前置 {front} 端口能通，但 {wait} 秒内柜台没有完成握手。"
                "这通常是华泰仿真前置暂时无响应，不是账号或密码错误。"
                "请过几分钟再点「连接登录」，或联系华泰确认仿真环境是否可用。"
            )
        return f"连接交易前置超时（已等 {wait} 秒）"

    def submit(
        self,
        *,
        symbol: str,
        direction: str,
        offset: str,
        volume: int,
        price: float,
        stop_price: float | None = None,
        bar_id: str = "",
    ) -> TrackedOrder:
        """发一笔限价单，等到全部成交、拒单、撤单，或等待超时。"""
        with self._lock:
            if not self.session.ready or self.session.need_order_check:
                order = TrackedOrder(
                    order_ref="",
                    symbol=symbol,
                    direction=direction,
                    offset=offset,
                    volume=volume,
                    price=price,
                    status="rejected",
                    message=self.session.last_error or "还没完成登录和报单核对",
                )
                return order
            ref = self.table.new_ref()
            order = TrackedOrder(
                order_ref=ref,
                symbol=symbol,
                direction=direction,
                offset=offset,
                volume=int(volume),
                price=float(price),
                stop_price=stop_price,
                bar_id=bar_id,
            )
            self.table.add(order)
            waiter = threading.Event()
            self._waiters[ref] = waiter
        try:
            self.client.insert(
                self.settings,
                {
                    "symbol": symbol,
                    "order_ref": ref,
                    "direction": direction,
                    "offset": offset,
                    "volume": int(volume),
                    "price": float(price),
                },
            )
        except Exception as exc:  # noqa: BLE001
            order.status = "rejected"
            order.message = f"报单发送失败：{exc}"
            logger.exception(order.message)
            return order
        note = self.activity.note_order()
        if "阈值" in note:
            logger.warning(note)
        else:
            logger.info(note)
        waiter.wait(self.settings.order_wait_sec)
        # 等不到终态还挂着：撤掉，再等撤单回报落地。过时委托留在柜台上，
        # 行情反向时照样会成交，而策略这根 K 线的判断早就作废了。
        if not is_terminal(order.status) and self.settings.auto_cancel_on_timeout:
            self._cancel_stale(order)
        return order

    def _cancel_stale(self, order: TrackedOrder) -> None:
        """撤掉等待超时还没结束的报单，并等柜台把终态回报送回来。"""
        if not order.order_sys_id:
            # 没有柜台编号，撤单报文柜台不认。至少不要把它当成成交。
            logger.warning(
                "报单 {} 等不到柜台编号，暂时无法撤单；后续开仓会先被这一笔挡住",
                order.order_ref,
            )
            return
        ok, message = self.cancel(order.order_ref)
        if not ok:
            # 撤单前刚巧成交或撤掉时，这里会报「已经结束」，不用当成故障。
            if not is_terminal(order.status):
                logger.warning("超时没有撤掉报单 {}：{}", order.order_ref, message)
            return
        deadline = time.time() + max(0.0, float(self.settings.cancel_wait_sec))
        while not is_terminal(order.status) and time.time() < deadline:
            time.sleep(0.05)
        if is_terminal(order.status):
            logger.warning(
                "报单 {} 超时撤单后结束：{}，成交 {} 手",
                order.order_ref,
                STATUS_TEXT.get(order.status, order.status),
                order.filled,
            )
        else:
            logger.warning(
                "报单 {} 已发撤单，但 {:.1f} 秒内没有等到撤单回报",
                order.order_ref,
                self.settings.cancel_wait_sec,
            )

    def cancel(self, order_ref: str) -> tuple[bool, str]:
        """撤销还没完结的一笔。没有柜台编号时不发送。"""
        with self._lock:
            order = self.table.get(order_ref)
            if order is None:
                return False, "没有这一笔报单"
            if is_terminal(order.status):
                return False, f"报单 {order_ref} 已经结束，不用撤"
            if not order.order_sys_id:
                return False, "柜台编号还没回来，暂时不能撤"
            payload = {
                "symbol": order.symbol,
                "order_ref": order.order_ref,
                "front_id": order.front_id or self.session.front_id,
                "session_id": order.session_id or self.session.session_id,
                "exchange_id": order.exchange_id,
                "order_sys_id": order.order_sys_id,
            }
        try:
            self.client.cancel(self.settings, payload)
        except Exception as exc:  # noqa: BLE001
            text = f"撤单发送失败：{exc}"
            logger.exception(text)
            return False, text
        note = self.activity.note_cancel()
        if "阈值" in note:
            logger.warning("已发送撤单 {}。{}", order_ref, note)
        else:
            logger.info("已发送撤单 {}。{}", order_ref, note)
        return True, f"已发送撤单 {order_ref}"

    def refresh(self) -> bool:
        """向柜台查一次持仓和可用资金。查持仓完成后再查资金，避免两次查询挤在一起被拒绝。"""
        if not self.session.ready or self.client is None:
            return False
        self._pos_event.clear()
        self._begin_position_query()
        try:
            self.client.query_positions(self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询持仓失败: {}", exc)
            return False
        if not self._pos_event.wait(self.settings.login_timeout_sec):
            return False
        self._account_event.clear()
        try:
            self.client.query_account(self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询资金失败: {}", exc)
            return False
        return self._account_event.wait(self.settings.login_timeout_sec)

    def close(self) -> None:
        """程序退出时释放接口。"""
        client = self.client
        if client is not None and hasattr(client, "release"):
            logger.info(
                "CTP 登出。账号 {} AppID {}",
                self.settings.user_id,
                self.settings.app_id,
            )
            client.release()

    def on_connected(self) -> None:
        """前置连上了。无论是第一次还是断线重连，都重新认证。"""
        again = self.session.disconnects > 0
        action = self.session.on_connected()
        who = f"账号 {self.settings.user_id} AppID {self.settings.app_id}"
        if again:
            logger.warning("交易前置重连成功。{}。下一步：{}", who, action)
        else:
            logger.info("交易前置连接成功。{}。下一步：{}", who, action)
        self._ready_event.clear()
        try:
            self.client.authenticate(self.settings)
        except Exception as exc:  # noqa: BLE001
            self.session.last_error = f"认证请求失败：{exc}"
            logger.exception(self.session.last_error)
            self._ready_event.set()

    def on_disconnected(self, reason: str) -> None:
        """断线。接口会自己再连，连上后 on_connected 会重新登录并先查报单。"""
        action = self.session.on_disconnected(reason)
        self.positions_ready = False
        logger.warning("交易前置连接断开：{}。下一步：{}", reason, action)
        self._ready_event.clear()

    def on_auth(self, ok: bool, message: str) -> None:
        step = self.session.on_authenticated(ok, message)
        if step != "login":
            logger.error("CTP 认证失败: {}", message)
            self._ready_event.set()
            return
        logger.info(
            "CTP 认证成功。账号 {} AppID {}",
            self.settings.user_id,
            self.settings.app_id,
        )
        try:
            self.client.login(self.settings)
        except Exception as exc:  # noqa: BLE001
            self.session.last_error = f"登录请求失败：{exc}"
            logger.exception(self.session.last_error)
            self._ready_event.set()

    def on_login(self, ok: bool, front_id: int, session_id: int, trading_day: str, message: str) -> None:
        step = self.session.on_logged_in(ok, front_id, session_id, trading_day, message)
        if step != "confirm":
            logger.error("CTP 登录失败: {}", message)
            self._ready_event.set()
            return
        logger.info(
            "CTP 登录成功。账号 {} AppID {} 交易日 {}",
            self.settings.user_id,
            self.settings.app_id,
            trading_day,
        )
        try:
            self.client.confirm(self.settings)
        except Exception as exc:  # noqa: BLE001
            self.session.last_error = f"结算单确认请求失败：{exc}"
            logger.exception(self.session.last_error)
            self._ready_event.set()

    def on_confirm(self, ok: bool, message: str) -> None:
        step = self.session.on_settlement_confirmed(ok, message)
        if step != "query_orders":
            logger.error("结算单确认失败: {}", message)
            self._ready_event.set()
            return
        logger.info("结算单已确认，先查询未完成报单")
        self.unknown_working = []
        try:
            self.client.query_orders(self.settings)
        except Exception as exc:  # noqa: BLE001
            self.session.last_error = f"查询报单失败：{exc}"
            logger.exception(self.session.last_error)
            self._ready_event.set()

    def on_reject(self, order_ref: str, message: str) -> None:
        with self._lock:
            order = self.table.get(order_ref)
            if order is None:
                self._note_foreign_report()
                return
            apply_order_report(order, status_code="rejected", message=message or "拒单")
            logger.error("柜台返回错误，报单 {}：{}", order_ref, order.message)
            self._finish_locked(order)

    def on_order_status(
        self,
        order_ref: str,
        status_code: str,
        filled: int,
        message: str,
        exchange_id: str = "",
        order_sys_id: str = "",
        front_id: int = 0,
        session_id: int = 0,
    ) -> None:
        with self._lock:
            order = self.table.get(order_ref)
            if order is None:
                self._note_foreign_report()
                return
            if exchange_id:
                order.exchange_id = exchange_id
            if order_sys_id:
                order.order_sys_id = order_sys_id.strip()
            if front_id:
                order.front_id = int(front_id)
            if session_id:
                order.session_id = int(session_id)
            apply_order_report(order, status_code=status_code, filled=filled, message=message)
            logger.info(
                "报单 {} {} {}",
                order_ref,
                STATUS_TEXT.get(order.status, order.status),
                message,
            )
            self._finish_locked(order)

    def on_cancel_error(self, order_ref: str, message: str) -> None:
        """撤单被柜台拒绝。不是我们发出的那笔，不当成这次登录的错误。"""
        with self._lock:
            order = self.table.get(str(order_ref or ""))
            if order is None:
                self._note_foreign_report()
                return
            logger.error("柜台返回撤单错误，报单 {}：{}", order_ref, message)

    def _note_foreign_report(self) -> None:
        """
        公用仿真账号上别人的回报。

        登录后柜台会把今天这个账号上所有人的成交、拒单一起推过来。
        那些单不是我们发的，第一笔说明一次，后面不再逐条写成错误。
        """
        self.foreign_reports += 1
        if self.foreign_reports == 1:
            logger.info("仿真公用账号上有别人的历史回报。那些单不是这次登录发出的，后面不再逐条显示。")

    def on_trade(self, order_ref: str, price: float, volume: int) -> None:
        with self._lock:
            order = self.table.get(order_ref)
            if order is None:
                self._note_foreign_report()
                return
            apply_trade(order, price, volume)
            logger.info(
                "报单 {} {} 价 {} 手数 {}",
                order_ref,
                STATUS_TEXT.get(order.status, order.status),
                price,
                volume,
            )
            self._finish_locked(order)

    def on_query_order(self, order_ref: str, status_code: str, filled: int, symbol: str, is_last: bool) -> None:
        """重连后的报单查询。认识的报单更新状态；不认识的只记下来，避免编号重复。"""
        with self._lock:
            if order_ref:
                self._note_ref(order_ref)
                order = self.table.get(order_ref)
                if order is None:
                    stranger = TrackedOrder(order_ref, symbol or "", "", "", max(int(filled or 0), 1), 0)
                    apply_order_report(stranger, status_code=status_code, filled=filled)
                    if not is_terminal(stranger.status):
                        self.unknown_working.append(order_ref)
                        logger.warning(
                            "柜台还有未完成报单 {} {}，程序没记住它。先停止开新仓。",
                            order_ref,
                            symbol,
                        )
                else:
                    apply_order_report(order, status_code=status_code, filled=filled)
                    self._finish_locked(order)
            if is_last:
                self.session.on_orders_checked()
                logger.info("未完成报单已核对")
                self._ready_event.set()

    def on_position_row(self, row: dict[str, Any] | None, is_last: bool) -> None:
        if row:
            name = symbol_key(str(row.get("symbol") or ""))
            if name:
                view = self._pending_positions.setdefault(name, PositionView())
                absorb_position_row(view, row)
        if is_last:
            self.positions = self._pending_positions
            self._pending_positions = {}
            self.positions_ready = True
            self._querying_positions = False
            self._pos_event.set()

    def query_price_band(self, symbol: str) -> tuple[bool, str]:
        """
        向已经登录的柜台查这个合约的涨停价和跌停价。

        仿真撮合用的是柜台自己的涨跌停，不是同花顺上的实盘价。
        还没登录、或柜台没回这个合约，就返回失败原因，不发报单。
        """
        if not self.session.ready or self.client is None:
            return False, "还没登录，不能查涨跌停"
        if not hasattr(self.client, "query_depth"):
            return False, "这条连接不能查涨跌停"
        self._instrument_event.clear()
        self._instrument = None
        self._instrument_error = ""
        # 行情查询会把同名前缀的期权也带回。只认和窗口里完全一样的合约。
        self._instrument_symbol = ctp_instrument_id(symbol)
        try:
            self.client.query_depth(self.settings, symbol)
        except Exception as exc:  # noqa: BLE001
            text = f"查询涨跌停失败：{exc}"
            logger.warning(text)
            return False, text
        if not self._instrument_event.wait(self.settings.login_timeout_sec):
            return False, "查询涨跌停超时，柜台没有返回"
        band = self._instrument
        if not band:
            return False, self._instrument_error or "柜台没有这个合约的涨跌停"
        from src.trader.ctp_md import describe_quote, quote_line

        view = describe_quote(band)
        band.update(view)
        name = str(band.get("symbol") or symbol)
        text = quote_line(band)
        logger.info("柜台快照 {}", text)
        if band.get("upper_text") == "—" and band.get("lower_text") == "—":
            return True, f"{name} 柜台这次没有给出涨停价和跌停价"
        return True, text

    def on_instrument(self, row: dict[str, Any] | None, is_last: bool, message: str = "") -> None:
        """记下合约资料里的涨停价和跌停价。最后一条到达后叫醒等待的查询。"""
        # 只收和要查的合约完全相同的一条。期权代码更长，不能拿来覆盖期货本身。
        wanted = str(getattr(self, "_instrument_symbol", "") or "")
        got = str((row or {}).get("symbol") or "").strip()
        if row and got and (not wanted or got.lower() == wanted.lower()):
            self._instrument = row
        if message:
            self._instrument_error = message
            logger.warning("查询涨跌停: {}", message)
        if is_last:
            self._instrument_event.set()

    def on_account(self, available: float | None, is_last: bool) -> None:
        if available is not None:
            self.available = available
            logger.info("柜台可用资金 {:.2f}", available)
        if is_last:
            self._account_event.set()

    def _begin_position_query(self) -> None:
        self._pending_positions = {}
        self._querying_positions = True

    def _note_ref(self, order_ref: str) -> None:
        try:
            number = int(str(order_ref).strip() or "0")
        except ValueError:
            return
        if number >= self.table.next_ref:
            self.table.next_ref = number + 1

    def _finish_locked(self, order: TrackedOrder) -> None:
        """已经持有锁。终态时叫醒等待的报单，并只记一次成交账。"""
        if not is_terminal(order.status):
            return
        waiter = self._waiters.get(order.order_ref)
        # 撤单或部分成交后剩余作废时，已经成交的手数同样要入账；
        # 否则内部持仓会一直比柜台少一截，对账永远不通过。
        if order.filled > 0 and not order.accounted and self.fill_handler is not None:
            try:
                # 返回 False 表示没记上账，调用方还可以再记一次。
                done = self.fill_handler(order.to_dict())
                if done is not False:
                    order.accounted = True
            except Exception:  # noqa: BLE001
                logger.exception("成交记账失败，报单 {}", order.order_ref)
        if waiter is not None:
            waiter.set()
