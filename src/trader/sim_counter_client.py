"""OpenctpClient 兼容的本机仿真柜台客户端。

只负责协议收发和事件转换；登录状态、委托状态、成交入账仍由生产 CtpGateway/CtpTrader
处理。持久 TCP 连接上的 event 在读取线程中异步回调，和真实 CTP 回调路径一致。
"""

from __future__ import annotations

import itertools
import json
import queue
import socket
import threading
from typing import Any, Callable

from loguru import logger

from src.trader.sim_counter import DEFAULT_HOST, DEFAULT_PORT, PROTOCOL_VERSION, SERVICE_NAME


class _IpcConnection:
    def __init__(
        self,
        host: str,
        port: int,
        on_event: Callable[[str, dict[str, Any]], None],
        on_closed: Callable[[str], None],
        timeout: float,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.on_event = on_event
        self.on_closed = on_closed
        self.timeout = max(0.2, float(timeout))
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue[tuple[bool, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_notified = False
        self._reader_thread: threading.Thread | None = None
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise ConnectionError(
                f"连不上本机仿真柜台 {self.host}:{self.port}。"
                "请先在「运行 → 测试工具 → 仿真柜台」启动服务"
            ) from exc
        self.sock.settimeout(None)
        self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name="sim-counter-client-rx",
        )
        self._reader_thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def request(self, action: str, payload: dict[str, Any] | None = None) -> Any:
        if self._closed:
            raise ConnectionError("仿真柜台连接已断开，请重新连接登录")
        request_id = next(self._ids)
        waiter: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        synchronous = threading.current_thread() is not self._reader_thread
        if synchronous:
            with self._pending_lock:
                self._pending[request_id] = waiter
        row = {"type": "request", "id": request_id, "action": action, "payload": payload or {}}
        try:
            with self._send_lock:
                self.sock.sendall((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self.close("仿真柜台连接写入失败")
            raise ConnectionError(f"仿真柜台请求发送失败：{exc}") from exc
        # on_connected/on_auth 等回调本身就在读取线程里；若在这里同步等应答，
        # 就没有线程能读取下一行。此类请求只确认已写出，响应由读取循环丢弃。
        if not synchronous:
            return None
        try:
            ok, data = waiter.get(timeout=self.timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"仿真柜台 {action} 请求超过 {self.timeout:.1f} 秒未应答") from exc
        if not ok:
            raise RuntimeError(str(data or f"仿真柜台 {action} 请求失败"))
        return data

    def _read_loop(self) -> None:
        reason = "仿真柜台关闭了连接"
        try:
            while not self._closed:
                line = self.reader.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    logger.warning("仿真柜台返回了无法解析的 JSON")
                    continue
                if row.get("type") == "response":
                    with self._pending_lock:
                        waiter = self._pending.pop(_as_int(row.get("id")), None)
                    if waiter is not None:
                        if row.get("ok"):
                            waiter.put((True, row.get("data")))
                        else:
                            waiter.put((False, row.get("error") or "仿真柜台请求失败"))
                    continue
                if row.get("type") == "event":
                    try:
                        self.on_event(str(row.get("event") or ""), row.get("data") or {})
                    except Exception as exc:  # noqa: BLE001 - 与真实 SPI 一样隔离回调异常
                        logger.exception("仿真柜台事件回调出错: {}", exc)
        except OSError as exc:
            if not self._closed:
                reason = f"仿真柜台连接异常：{exc}"
        finally:
            was_closed = self._closed
            self.close(reason, notify=not was_closed)

    def close(self, reason: str = "客户端已释放", *, notify: bool = False) -> None:
        if self._closed and self._close_notified:
            return
        self._closed = True
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for waiter in pending:
            try:
                waiter.put_nowait((False, reason))
            except queue.Full:
                pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.reader.close()
        except OSError:
            pass
        if notify and not self._close_notified:
            self._close_notified = True
            self.on_closed(reason)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class SimCounterClient:
    """与 OpenctpClient 相同的方法面；settings 中只使用非密钥连接字段。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = int(port)
        self.listener: Any = None
        self.settings: Any = None
        self.connection: _IpcConnection | None = None
        self._releasing = False

    def start(self, settings: Any, listener: Any) -> None:
        self.listener = listener
        self.settings = settings
        self._releasing = False
        timeout = min(max(float(getattr(settings, "login_timeout_sec", 5.0) or 5.0), 0.5), 8.0)
        self.connection = _IpcConnection(
            self.host,
            self.port,
            self._on_event,
            self._on_closed,
            timeout,
        )
        self.connection.request(
            "start",
            {
                "protocol": PROTOCOL_VERSION,
                "service": SERVICE_NAME,
                "broker_id": str(getattr(settings, "broker_id", "") or ""),
                "user_id": str(getattr(settings, "user_id", "") or ""),
                "app_id": str(getattr(settings, "app_id", "") or ""),
            },
        )

    def _request(self, action: str, payload: dict[str, Any] | None = None) -> Any:
        if self.connection is None:
            raise ConnectionError("仿真柜台客户端尚未启动")
        return self.connection.request(action, payload)

    def authenticate(self, settings: Any) -> None:
        self._request("authenticate", {"broker_id": settings.broker_id, "app_id": settings.app_id})

    def login(self, settings: Any) -> None:
        # 自测账号不需要密码；即使是误传配置，也不把密码写进 IPC。
        self._request("login", {"broker_id": settings.broker_id, "user_id": settings.user_id})

    def confirm(self, settings: Any) -> None:
        self._request("confirm", {"user_id": settings.user_id})

    def insert(self, settings: Any, order: dict[str, Any]) -> None:
        self._request("insert", dict(order))

    def cancel(self, settings: Any, order: dict[str, Any]) -> None:
        self._request("cancel", dict(order))

    def query_orders(self, settings: Any) -> None:
        self._request("query_orders")

    def query_positions(self, settings: Any) -> None:
        self._request("query_positions")

    def query_account(self, settings: Any) -> None:
        self._request("query_account")

    def query_trades(self, settings: Any) -> None:
        self._request("query_trades")

    def query_instruments(self, settings: Any, keyword: str = "") -> None:
        self._request("query_instruments", {"keyword": keyword})

    def query_margin_rate(self, settings: Any, symbol: str) -> None:
        self._request("query_margin_rate", {"symbol": symbol})

    def query_commission_rate(self, settings: Any, symbol: str) -> None:
        self._request("query_commission_rate", {"symbol": symbol})

    def query_depth(self, settings: Any, symbol: str) -> None:
        self._request("query_depth", {"symbol": symbol})

    def release(self) -> None:
        self._releasing = True
        connection = self.connection
        self.connection = None
        if connection is None:
            return
        try:
            if not connection.closed:
                connection.request("release")
        except (ConnectionError, RuntimeError, TimeoutError):
            pass
        finally:
            connection.close(notify=False)

    def _on_closed(self, reason: str) -> None:
        if self._releasing or self.listener is None:
            return
        self.listener.on_disconnected(reason)

    def _on_event(self, event: str, data: dict[str, Any]) -> None:
        listener = self.listener
        if listener is None:
            return
        if event == "connected":
            listener.on_connected()
        elif event == "auth":
            listener.on_auth(bool(data.get("ok")), str(data.get("message") or ""))
        elif event == "login":
            listener.on_login(
                bool(data.get("ok")),
                _as_int(data.get("front_id")),
                _as_int(data.get("session_id")),
                str(data.get("trading_day") or ""),
                str(data.get("message") or ""),
                str(data.get("max_order_ref") or ""),
            )
        elif event == "confirm":
            listener.on_confirm(bool(data.get("ok")), str(data.get("message") or ""))
        elif event == "reject":
            listener.on_reject(str(data.get("order_ref") or ""), str(data.get("message") or "柜台拒单"))
        elif event == "cancel_error":
            listener.on_cancel_error(str(data.get("order_ref") or ""), str(data.get("message") or "撤单失败"))
        elif event == "order_status":
            listener.on_order_status(
                str(data.get("order_ref") or ""),
                str(data.get("status_code") or ""),
                _as_int(data.get("filled")),
                str(data.get("message") or ""),
                str(data.get("exchange") or ""),
                str(data.get("order_sys_id") or ""),
                _as_int(data.get("front_id")),
                _as_int(data.get("session_id")),
                {
                    "symbol": data.get("symbol") or "",
                    "direction": data.get("direction") or "",
                    "offset": data.get("offset") or "",
                    "price": data.get("price") or 0,
                    "volume": data.get("volume") or 0,
                    "insert_time": data.get("insert_time") or "",
                    "hedge": data.get("hedge") or "",
                    "remaining": data.get("remaining") or 0,
                },
            )
        elif event == "trade":
            listener.on_trade(
                str(data.get("order_ref") or ""),
                float(data.get("price") or 0),
                _as_int(data.get("volume")),
                dict(data),
            )
        elif event == "query_order":
            row = data.get("row")
            if row:
                listener.on_query_order(
                    str(row.get("order_ref") or ""),
                    str(row.get("status_code") or ""),
                    _as_int(row.get("filled")),
                    str(row.get("symbol") or ""),
                    bool(data.get("is_last")),
                    str(row.get("exchange") or ""),
                    str(row.get("order_sys_id") or ""),
                    _as_int(row.get("front_id")),
                    _as_int(row.get("session_id")),
                    float(row.get("price") or 0),
                    _as_int(row.get("volume")),
                    str(row.get("direction") or ""),
                    str(row.get("offset") or ""),
                    str(row.get("message") or ""),
                    str(row.get("insert_time") or ""),
                    str(row.get("hedge") or ""),
                    _as_int(row.get("remaining")),
                )
            else:
                listener.on_query_order("", "", 0, "", bool(data.get("is_last", True)))
        elif event == "position":
            listener.on_position_row(data.get("row"), bool(data.get("is_last")))
        elif event == "account":
            listener.on_account(data.get("row"), bool(data.get("is_last", True)))
        elif event == "query_trade":
            listener.on_query_trade(data.get("row"), bool(data.get("is_last", True)))
        elif event == "contract":
            listener.on_contract_row(data.get("row"), bool(data.get("is_last", True)))
        elif event == "margin":
            listener.on_margin_row(data.get("row"), bool(data.get("is_last", True)))
        elif event == "commission":
            listener.on_commission_row(data.get("row"), bool(data.get("is_last", True)))
        elif event == "instrument":
            listener.on_instrument(
                data.get("row"),
                bool(data.get("is_last", True)),
                str(data.get("message") or ""),
            )
