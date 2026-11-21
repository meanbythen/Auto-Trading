"""独立的程序自测仿真柜台。

通过 127.0.0.1 回环 TCP、换行 JSON 接收交易请求。柜台在单独进程中维护资金、
持仓、委托、成交和行情；本模块既不加载 CTP SDK，也不调用程序内的订单状态机。

启动：python -m src.trader.sim_counter --root <项目目录>
管理端和交易端使用同一协议：request/response 用于确认请求已接收，event 用于异步柜台回报。
"""

from __future__ import annotations

import argparse
import copy
import json
import socket
import socketserver
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.trader.ctp_symbols import ctp_exchange_id, ctp_instrument_id, product_code

SERVICE_NAME = "auto-trading-program-self-test-counter"
PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47653
SCENARIOS = ("immediate", "working", "partial", "reject", "silent", "disconnect")
SCENARIO_LABELS = {
    "immediate": "立即全部成交",
    "working": "挂单不成交",
    "partial": "部分成交后继续挂单",
    "reject": "柜台拒单",
    "silent": "无回报（请求已接收）",
    "disconnect": "接收后断线",
}
TERMINAL_CODES = {"0", "5", "rejected"}


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _symbol(value: Any) -> str:
    return ctp_instrument_id(str(value or "").strip())


def _exchange(symbol: str) -> str:
    return ctp_exchange_id(symbol)


def _product(symbol: str) -> str:
    return product_code(symbol)


def _instrument(symbol: str, last: float = 0.0) -> dict[str, Any]:
    """生成一份可查询的合约与盘口。自测面板可改价或加入新的合约。"""
    product = _product(symbol)
    multipliers = {"cu": 5, "ad": 5, "lh": 16, "sa": 20, "if": 300, "ih": 300, "ic": 200}
    ticks = {"cu": 10.0, "ad": 5.0, "lh": 5.0, "sa": 1.0, "if": 0.2, "ih": 0.2, "ic": 0.2}
    defaults = {
        "fu": 2500.0,
        "bu": 4000.0,
        "c": 2200.0,
        "cu": 70000.0,
        "ad": 20000.0,
        "lh": 14000.0,
        "sa": 1200.0,
        "if": 4000.0,
    }
    price = float(last or defaults.get(product, 3000.0))
    tick = ticks.get(product, 1.0)
    upper = price * 1.1
    lower = price * 0.9
    if product in {"if", "ih", "ic"}:
        upper, lower = price * 1.1, price * 0.9
    return {
        "symbol": symbol,
        "name": f"自测{symbol}",
        "exchange": _exchange(symbol),
        "product_class": "1",
        "volume_multiple": multipliers.get(product, 10),
        "price_tick": tick,
        "open_date": "20260101",
        "expire_date": "20271231",
        "margin_rate": 0.12,
        "last": price,
        "bid": price - tick,
        "ask": price + tick,
        "bid_volume": 100,
        "ask_volume": 100,
        "upper": upper,
        "lower": lower,
        "time": _clock(),
    }


def _default_instruments() -> dict[str, dict[str, Any]]:
    rows = ("fu2701", "bu2612", "c2701", "c2511", "cu2601", "ad2701", "lh2701", "SA2701", "IF2612")
    return {_symbol(name): _instrument(_symbol(name)) for name in rows}


class CounterState:
    """柜台数据与校验。所有改动都在锁内完成并原子落盘。"""

    def __init__(self, state_file: str | Path) -> None:
        self.state_file = Path(state_file)
        self.lock = threading.RLock()
        self.instruments = _default_instruments()
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.positions: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.scenario = "immediate"
        self.pre_balance = 1_000_000.0
        self.close_profit = 0.0
        self.commission = 0.0
        self.next_trade_id = 1
        self.next_order_sys_id = 1
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("service") != SERVICE_NAME:
            return
        for key in (
            "instruments",
            "orders",
            "trades",
            "positions",
            "events",
            "scenario",
            "pre_balance",
            "close_profit",
            "commission",
            "next_trade_id",
            "next_order_sys_id",
        ):
            if key in data:
                setattr(self, key, data[key])
        if self.scenario not in SCENARIOS:
            self.scenario = "immediate"

    def save_locked(self) -> None:
        data = {
            "service": SERVICE_NAME,
            "instruments": self.instruments,
            "orders": self.orders,
            "trades": self.trades,
            "positions": self.positions,
            "events": self.events[-300:],
            "scenario": self.scenario,
            "pre_balance": self.pre_balance,
            "close_profit": self.close_profit,
            "commission": self.commission,
            "next_trade_id": self.next_trade_id,
            "next_order_sys_id": self.next_order_sys_id,
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def record_locked(self, kind: str, message: str, **data: Any) -> None:
        self.events.append({"ts": _now(), "kind": kind, "message": message, "data": data})
        self.events = self.events[-300:]

    def max_order_ref(self) -> str:
        numbers = [_integer(str(ref).strip()) for ref in self.orders]
        return str(max(numbers, default=0))

    def account_locked(self) -> dict[str, Any]:
        margin = 0.0
        position_profit = 0.0
        for key, row in self.positions.items():
            symbol, side = key.rsplit("|", 1)
            quantity = _integer(row.get("today")) + _integer(row.get("yesterday"))
            if quantity <= 0:
                continue
            contract = self.instruments.get(symbol) or _instrument(symbol)
            multiplier = _number(contract.get("volume_multiple"), 10.0)
            average = _number(row.get("open_cost")) / max(quantity * multiplier, 1.0)
            last = _number(contract.get("last"), average)
            sign = 1.0 if side == "long" else -1.0
            position_profit += (last - average) * multiplier * quantity * sign
            margin += _number(row.get("margin"))
        frozen = 0.0
        for order in self.orders.values():
            if order.get("status_code") in TERMINAL_CODES or order.get("offset") != "0":
                continue
            remaining = max(0, _integer(order.get("volume")) - _integer(order.get("filled")))
            contract = self.instruments.get(str(order.get("symbol") or ""), {})
            frozen += (
                remaining
                * _number(order.get("price"))
                * _number(contract.get("volume_multiple"), 10.0)
                * _number(contract.get("margin_rate"), 0.12)
            )
        balance = self.pre_balance + self.close_profit - self.commission
        return {
            "account_id": "SELFTEST",
            "pre_balance": self.pre_balance,
            "balance": balance,
            "available": balance - margin - frozen,
            "withdraw_quota": balance - margin - frozen,
            "currency": "CNY",
            "deposit": 0.0,
            "withdraw": 0.0,
            "margin": margin,
            "frozen_margin": frozen,
            "frozen_cash": 0.0,
            "commission": self.commission,
            "close_profit": self.close_profit,
            "position_profit": position_profit,
            "mortgage_in": 0.0,
            "mortgage_out": 0.0,
            "option_value": 0.0,
        }

    def position_rows_locked(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, position in self.positions.items():
            symbol, side = key.rsplit("|", 1)
            today = _integer(position.get("today"))
            yesterday = _integer(position.get("yesterday"))
            quantity = today + yesterday
            if quantity <= 0:
                continue
            contract = self.instruments.get(symbol) or _instrument(symbol)
            base = {
                "symbol": symbol,
                "exchange": _exchange(symbol),
                "direction": "2" if side == "long" else "3",
                "open_cost": _number(position.get("open_cost")),
                "position_profit": 0.0,
                "use_margin": _number(position.get("margin")),
                "hedge": "1",
            }
            split = _exchange(symbol) in {"SHFE", "INE"} or _product(symbol) == "bu"
            pieces = ((today, 0), (0, yesterday)) if split and today and yesterday else ((today, yesterday),)
            for td, yd in pieces:
                lots = td + yd
                if lots <= 0:
                    continue
                ratio = lots / quantity
                row = dict(base)
                row.update(
                    {
                        "position": lots,
                        "today": td,
                        "yesterday": yd,
                        "open_cost": base["open_cost"] * ratio,
                        "use_margin": base["use_margin"] * ratio,
                    }
                )
                average = _number(position.get("open_cost")) / max(quantity * _number(contract.get("volume_multiple"), 10), 1)
                sign = 1.0 if side == "long" else -1.0
                row["position_profit"] = (
                    (_number(contract.get("last")) - average)
                    * _number(contract.get("volume_multiple"), 10)
                    * lots
                    * sign
                )
                rows.append(row)
        return sorted(rows, key=lambda row: (str(row["symbol"]), str(row["direction"]), _integer(row["today"])))

    def snapshot(self, host: str = "", port: int = 0, sessions: int = 0) -> dict[str, Any]:
        with self.lock:
            return {
                "service": SERVICE_NAME,
                "protocol": PROTOCOL_VERSION,
                "running": True,
                "host": host,
                "port": port,
                "connections": sessions,
                "scenario": self.scenario,
                "scenario_label": SCENARIO_LABELS[self.scenario],
                "account": self.account_locked(),
                "orders": [copy.deepcopy(row) for row in self.orders.values()],
                "trades": copy.deepcopy(self.trades),
                "positions": self.position_rows_locked(),
                "events": copy.deepcopy(self.events[-100:]),
                "instruments": copy.deepcopy(list(self.instruments.values())),
            }

    def set_scenario(self, value: Any) -> dict[str, Any]:
        scenario = str(value or "").strip().lower()
        if scenario not in SCENARIOS:
            raise ValueError(f"未知场景 {scenario}；可选 {', '.join(SCENARIOS)}")
        with self.lock:
            self.scenario = scenario
            self.record_locked("scenario", f"后续报单场景切换为：{SCENARIO_LABELS[scenario]}")
            self.save_locked()
        return {"scenario": scenario, "label": SCENARIO_LABELS[scenario]}

    def set_quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = _symbol(payload.get("symbol"))
        if not symbol:
            raise ValueError("合约代码不能为空")
        with self.lock:
            current = self.instruments.get(symbol) or _instrument(symbol, _number(payload.get("last")))
            for key in ("last", "bid", "ask", "upper", "lower", "price_tick", "margin_rate"):
                if key in payload and str(payload.get(key) or "").strip():
                    value = _number(payload.get(key), -1.0)
                    if value <= 0:
                        raise ValueError(f"{key} 必须大于 0")
                    current[key] = value
            if "volume_multiple" in payload and str(payload.get("volume_multiple") or "").strip():
                current["volume_multiple"] = max(1, _integer(payload.get("volume_multiple"), 10))
            if current["bid"] <= 0:
                current["bid"] = current["last"] - current["price_tick"]
            if current["ask"] <= 0:
                current["ask"] = current["last"] + current["price_tick"]
            current["time"] = _clock()
            self.instruments[symbol] = current
            self.record_locked("quote", f"{symbol} 最新价 {current['last']}", symbol=symbol)
            self.save_locked()
            return copy.deepcopy(current)

    def _order_row(self, payload: dict[str, Any], session_id: int) -> dict[str, Any]:
        ref = str(payload.get("order_ref") or "").strip()
        if not ref:
            raise ValueError("报单缺少 order_ref")
        if ref in self.orders:
            raise ValueError(f"报单编号 {ref} 已存在")
        symbol = _symbol(payload.get("symbol"))
        sys_id = str(self.next_order_sys_id).rjust(12)
        self.next_order_sys_id += 1
        return {
            "order_ref": ref,
            "symbol": symbol,
            "direction": str(payload.get("direction") or ""),
            "offset": str(payload.get("offset") or ""),
            "volume": _integer(payload.get("volume")),
            "price": _number(payload.get("price")),
            "filled": 0,
            "status_code": "3",
            "message": "已报",
            "exchange": _exchange(symbol),
            "order_sys_id": sys_id,
            "front_id": 1,
            "session_id": session_id,
            "insert_time": _clock(),
            "hedge": "1",
        }

    def insert(self, payload: dict[str, Any], session_id: int) -> tuple[list[tuple[str, Any]], bool]:
        events: list[tuple[str, Any]] = []
        with self.lock:
            order = self._order_row(payload, session_id)
            self.orders[order["order_ref"]] = order
            scenario = self.scenario
            error = self._validate_order_locked(order)
            if error:
                order["status_code"] = "rejected"
                order["message"] = error
                self.record_locked("reject", f"{order['order_ref']} {error}", order_ref=order["order_ref"])
                events.append(("reject", {"order_ref": order["order_ref"], "message": error}))
            else:
                self.record_locked(
                    "order",
                    f"收到 {order['symbol']} {order['direction']}/{order['offset']} "
                    f"{order['volume']} 手 @ {order['price']}，编号 {order['order_ref']}",
                    order_ref=order["order_ref"],
                )
                if scenario != "reject":
                    events.append(("order_status", self.order_event_locked(order)))
                if scenario == "reject":
                    order["status_code"] = "rejected"
                    order["message"] = "自测场景：柜台拒单"
                    events = [("reject", {"order_ref": order["order_ref"], "message": order["message"]})]
                    self.record_locked("reject", order["message"], order_ref=order["order_ref"])
                elif scenario == "immediate":
                    events.extend(self._fill_locked(order, order["volume"]))
                elif scenario == "partial":
                    lots = order["volume"] // 2 if order["volume"] > 1 else 1
                    events.extend(self._fill_locked(order, lots))
                elif scenario in {"silent", "disconnect"}:
                    events = []
            self.save_locked()
        return events, scenario == "disconnect" and not error

    def _validate_order_locked(self, order: dict[str, Any]) -> str:
        symbol = order["symbol"]
        contract = self.instruments.get(symbol)
        if contract is None:
            return f"柜台找不到合约 {symbol or '（空）'}；请先在仿真柜台面板设置该合约行情"
        if order["direction"] not in {"0", "1"}:
            return f"非法买卖方向 {order['direction']}"
        if order["offset"] not in {"0", "1", "3", "4"}:
            return f"非法开平标志 {order['offset']}"
        if order["volume"] <= 0:
            return "报单手数必须大于 0"
        if order["price"] <= 0:
            return "报单价格必须大于 0"
        if order["price"] > _number(contract.get("upper")) or order["price"] < _number(contract.get("lower")):
            return f"价格超出涨跌停范围 {contract.get('lower')} ~ {contract.get('upper')}"
        if order["offset"] == "0":
            required = (
                order["price"]
                * order["volume"]
                * _number(contract.get("volume_multiple"), 10)
                * _number(contract.get("margin_rate"), 0.12)
            )
            if required > self.account_locked()["available"] + 0.001:
                return f"可用资金不足：开仓约需 {required:.2f}，当前可用 {self.account_locked()['available']:.2f}"
            return ""
        side = "long" if order["direction"] == "1" else "short"
        position = self.positions.get(f"{symbol}|{side}")
        if not position:
            return "可平持仓不足：当前方向没有持仓"
        today = _integer(position.get("today"))
        yesterday = _integer(position.get("yesterday"))
        if order["offset"] == "3" and order["volume"] > today:
            return f"平今持仓不足：可平今 {today}，请求 {order['volume']}"
        if order["offset"] == "4" and order["volume"] > yesterday:
            return f"平昨持仓不足：可平昨 {yesterday}，请求 {order['volume']}"
        if order["offset"] == "1" and order["volume"] > today + yesterday:
            return f"可平持仓不足：共 {today + yesterday}，请求 {order['volume']}"
        return ""

    def order_event_locked(self, order: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(order)
        row["remaining"] = max(0, _integer(row.get("volume")) - _integer(row.get("filled")))
        return row

    def _fill_locked(self, order: dict[str, Any], lots: int) -> list[tuple[str, Any]]:
        lots = min(max(0, lots), order["volume"] - order["filled"])
        if lots <= 0:
            return []
        close_profit = self._apply_position_locked(order, lots)
        order["filled"] += lots
        order["status_code"] = "0" if order["filled"] >= order["volume"] else "1"
        order["message"] = "全部成交" if order["status_code"] == "0" else "部分成交"
        trade = {
            "order_ref": order["order_ref"],
            "trade_id": f"ST{self.next_trade_id:08d}",
            "symbol": order["symbol"],
            "direction": order["direction"],
            "offset": order["offset"],
            "price": order["price"],
            "volume": lots,
            "time": _clock(),
            "exchange": order["exchange"],
            "hedge": "1",
            "order_sys_id": order["order_sys_id"].strip(),
            "commission": 0.0,
            "close_profit": close_profit,
        }
        self.next_trade_id += 1
        self.trades.append(trade)
        self.record_locked(
            "trade",
            f"成交 {order['order_ref']}：{lots} 手 @ {order['price']}",
            order_ref=order["order_ref"],
            trade_id=trade["trade_id"],
        )
        return [("trade", copy.deepcopy(trade)), ("order_status", self.order_event_locked(order))]

    def _apply_position_locked(self, order: dict[str, Any], lots: int) -> float:
        symbol = order["symbol"]
        contract = self.instruments[symbol]
        multiplier = _number(contract.get("volume_multiple"), 10)
        margin_rate = _number(contract.get("margin_rate"), 0.12)
        if order["offset"] == "0":
            side = "long" if order["direction"] == "0" else "short"
            key = f"{symbol}|{side}"
            row = self.positions.setdefault(
                key, {"today": 0, "yesterday": 0, "open_cost": 0.0, "margin": 0.0}
            )
            row["today"] = _integer(row.get("today")) + lots
            row["open_cost"] = _number(row.get("open_cost")) + order["price"] * multiplier * lots
            row["margin"] = _number(row.get("margin")) + order["price"] * multiplier * lots * margin_rate
            return 0.0
        side = "long" if order["direction"] == "1" else "short"
        key = f"{symbol}|{side}"
        row = self.positions[key]
        today = _integer(row.get("today"))
        yesterday = _integer(row.get("yesterday"))
        if order["offset"] == "3":
            take_today = lots
        elif order["offset"] == "4":
            take_today = 0
        else:
            take_today = min(today, max(0, lots - yesterday))
        take_yesterday = lots - take_today
        old_quantity = today + yesterday
        average = _number(row.get("open_cost")) / max(old_quantity * multiplier, 1)
        sign = 1.0 if side == "long" else -1.0
        profit = (order["price"] - average) * multiplier * lots * sign
        old_margin = _number(row.get("margin"))
        row["today"] = today - take_today
        row["yesterday"] = yesterday - take_yesterday
        row["open_cost"] = max(0.0, _number(row.get("open_cost")) - average * multiplier * lots)
        row["margin"] = max(0.0, old_margin - old_margin * lots / max(old_quantity, 1))
        self.close_profit += profit
        return profit

    def cancel(self, order_ref: Any) -> list[tuple[str, Any]]:
        ref = str(order_ref or "").strip()
        with self.lock:
            order = self.orders.get(ref)
            if order is None:
                return [("cancel_error", {"order_ref": ref, "message": "找不到报单"})]
            if order.get("status_code") in TERMINAL_CODES or _integer(order.get("filled")) >= _integer(order.get("volume")):
                return [("cancel_error", {"order_ref": ref, "message": "报单已完结，不能撤单"})]
            order["status_code"] = "5"
            order["message"] = "已撤单"
            self.record_locked("cancel", f"撤单 {ref}", order_ref=ref)
            events = [("order_status", self.order_event_locked(order))]
            self.save_locked()
            return events


class CounterHandler(socketserver.StreamRequestHandler):
    """一个 TCP 连接对应一个交易会话或一次管理请求。"""

    def setup(self) -> None:
        super().setup()
        self.send_lock = threading.Lock()
        self.session_active = False
        self.session_id = id(self) % 1000000
        self.confirmed = False

    @property
    def counter(self) -> "SimCounterServer":
        return self.server  # type: ignore[return-value]

    def send_json(self, row: dict[str, Any]) -> None:
        raw = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self.send_lock:
            self.wfile.write(raw)
            self.wfile.flush()

    def response(self, request_id: Any, data: Any = None, error: str = "") -> None:
        row = {"type": "response", "id": request_id, "ok": not error}
        if error:
            row["error"] = error
        else:
            row["data"] = data if data is not None else {}
        self.send_json(row)

    def emit(self, event: str, data: Any) -> None:
        self.send_json({"type": "event", "event": event, "data": data if data is not None else {}})

    def handle(self) -> None:
        while True:
            raw = self.rfile.readline(2 * 1024 * 1024)
            if not raw:
                break
            try:
                request = json.loads(raw.decode("utf-8"))
                request_id = request.get("id")
                action = str(request.get("action") or "").strip()
                payload = request.get("payload") or {}
                disconnect = self.dispatch(request_id, action, payload)
                if disconnect or action in {"stop", "release"}:
                    break
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.response(locals().get("request_id", ""), error=str(exc))
            except (ConnectionError, OSError):
                break
            except Exception as exc:  # 柜台错误返回给调用方，不拖垮整个进程。
                self.counter.state.record_locked("error", f"处理请求异常：{exc}")
                self.response(locals().get("request_id", ""), error=f"柜台内部错误：{exc}")

    def dispatch(self, request_id: Any, action: str, payload: dict[str, Any]) -> bool:
        state = self.counter.state
        if action == "ping":
            self.response(request_id, {"service": SERVICE_NAME, "protocol": PROTOCOL_VERSION})
            return False
        if action == "status":
            self.response(request_id, state.snapshot(*self.counter.address, len(self.counter.sessions)))
            return False
        if action == "set_scenario":
            self.response(request_id, state.set_scenario(payload.get("scenario")))
            return False
        if action == "set_quote":
            self.response(request_id, state.set_quote(payload))
            return False
        if action == "stop":
            self.response(request_id, {"stopping": True})
            self.counter.initiate_stop()
            return True
        if action == "start":
            self.session_active = True
            self.counter.sessions.add(self)
            self.response(request_id, {"connected": True})
            self.emit("connected", {})
            return False
        if not self.session_active:
            raise ValueError("请先建立交易会话")
        if action == "authenticate":
            self.response(request_id, {})
            self.emit("auth", {"ok": True, "message": "本机自测认证通过"})
            return False
        if action == "login":
            with state.lock:
                maximum = state.max_order_ref()
            self.response(request_id, {})
            self.emit(
                "login",
                {
                    "ok": True,
                    "front_id": 1,
                    "session_id": self.session_id,
                    "trading_day": datetime.now().strftime("%Y%m%d"),
                    "message": "",
                    "max_order_ref": maximum,
                },
            )
            return False
        if action == "confirm":
            self.confirmed = True
            self.response(request_id, {})
            self.emit("confirm", {"ok": True, "message": "结算单已确认"})
            return False
        if action == "release":
            self.response(request_id, {})
            return False
        if not self.confirmed:
            raise ValueError("尚未确认结算单，柜台不接受该请求")
        if action == "insert":
            events, disconnect = state.insert(payload, self.session_id)
            self.response(request_id, {"accepted": True})
            for event, data in events:
                self.emit(event, data)
            return disconnect
        if action == "cancel":
            events = state.cancel(payload.get("order_ref"))
            self.response(request_id, {"accepted": True})
            for event, data in events:
                self.emit(event, data)
            return False
        events = self._query(action, payload)
        self.response(request_id, {"accepted": True})
        for event, data in events:
            self.emit(event, data)
        return False

    def _query(self, action: str, payload: dict[str, Any]) -> list[tuple[str, Any]]:
        state = self.counter.state
        with state.lock:
            if action == "query_orders":
                rows = [state.order_event_locked(row) for row in state.orders.values()]
                return self._rows_event("query_order", rows)
            if action == "query_positions":
                return self._rows_event("position", state.position_rows_locked())
            if action == "query_account":
                return [("account", {"row": state.account_locked(), "is_last": True})]
            if action == "query_trades":
                return self._rows_event("query_trade", copy.deepcopy(state.trades))
            if action == "query_instruments":
                keyword = _symbol(payload.get("keyword"))
                rows = [
                    copy.deepcopy(row)
                    for key, row in state.instruments.items()
                    if not keyword or keyword.lower() in key.lower()
                ][:250]
                public = [{key: value for key, value in row.items() if key in {
                    "symbol", "name", "exchange", "product_class", "volume_multiple", "price_tick", "open_date", "expire_date"
                }} for row in rows]
                return self._rows_event("contract", public)
            if action in {"query_margin_rate", "query_commission_rate", "query_depth"}:
                symbol = _symbol(payload.get("symbol") or payload.get("keyword"))
                row = state.instruments.get(symbol)
                if row is None:
                    if action == "query_depth":
                        return [("instrument", {"row": None, "is_last": True, "message": f"柜台找不到合约 {symbol}"})]
                    return self._rows_event("margin" if action == "query_margin_rate" else "commission", [])
                if action == "query_depth":
                    quote = {key: copy.deepcopy(value) for key, value in row.items() if key in {
                        "symbol", "last", "bid", "ask", "bid_volume", "ask_volume", "upper", "lower", "time"
                    }}
                    quote["time"] = _clock()
                    return [("instrument", {"row": quote, "is_last": True, "message": ""})]
                if action == "query_margin_rate":
                    margin = {
                        "symbol": symbol,
                        "long_margin_money": _number(row.get("margin_rate"), 0.12),
                        "short_margin_money": _number(row.get("margin_rate"), 0.12),
                        "long_margin_volume": 0.0,
                        "short_margin_volume": 0.0,
                    }
                    return [("margin", {"row": margin, "is_last": True})]
                commission = {
                    "symbol": symbol,
                    "open_money": 0.0,
                    "open_volume": 0.0,
                    "close_money": 0.0,
                    "close_volume": 0.0,
                    "close_today_money": 0.0,
                    "close_today_volume": 0.0,
                }
                return [("commission", {"row": commission, "is_last": True})]
        raise ValueError(f"未知柜台请求 {action}")

    @staticmethod
    def _rows_event(event: str, rows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
        if not rows:
            return [(event, {"row": None, "is_last": True})]
        return [
            (event, {"row": row, "is_last": index == len(rows) - 1})
            for index, row in enumerate(rows)
        ]

    def finish(self) -> None:
        self.counter.sessions.discard(self)
        super().finish()


class SimCounterServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, state_file: str | Path) -> None:
        self.state = CounterState(state_file)
        self.sessions: set[CounterHandler] = set()
        self.address = (host, int(port))
        self._stopping = False
        super().__init__((host, int(port)), CounterHandler)
        self.address = (str(self.server_address[0]), int(self.server_address[1]))

    def initiate_stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        threading.Thread(target=self.shutdown, daemon=True, name="sim-counter-stop").start()


def request_once(
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 2.0,
) -> Any:
    """管理端单次请求。交易客户端使用持久连接，不走这个 helper。"""
    with socket.create_connection((host, int(port)), timeout=float(timeout)) as sock:
        sock.settimeout(float(timeout))
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        request = {"type": "request", "id": 1, "action": action, "payload": payload or {}}
        sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        while True:
            line = reader.readline()
            if not line:
                raise ConnectionError("仿真柜台关闭了连接")
            row = json.loads(line)
            if row.get("type") != "response" or row.get("id") != 1:
                continue
            if not row.get("ok"):
                raise RuntimeError(str(row.get("error") or "仿真柜台请求失败"))
            return row.get("data")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Trading 本机程序自测仿真柜台")
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-file", default="")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("仿真柜台只允许绑定回环地址")
    root = Path(args.root).resolve()
    state_file = Path(args.state_file) if args.state_file else root / "data" / "self_test_counter.json"
    try:
        server = SimCounterServer(args.host, args.port, state_file)
    except OSError as exc:
        print(f"SIM_COUNTER_ERROR {exc}", flush=True)
        return 2
    print(
        "SIM_COUNTER_READY "
        + json.dumps({"host": server.address[0], "port": server.address[1], "pid": __import__("os").getpid()}),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
