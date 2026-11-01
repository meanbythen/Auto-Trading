"""
CTP 报单规则（不连柜台也能测）

功能：
  决定买卖方向和开平；沥青、玉米把平仓拆成平今、平昨。
  把柜台回报收成：已报、拒单、部分成交、全部成交、撤单。
  拿程序自己记的净持仓和柜台查回来的净持仓对比。
  记住登录走到哪一步，断线后知道要重连，并且先查未完成的报单。

原理：这些都是纯判断，不调用动态库。真正收发报文的代码只负责把柜台字段填进来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


# 柜台开平标志。和上期技术 CTP 的 CombOffsetFlag 一致。
OFFSET_OPEN = "0"
OFFSET_CLOSE = "1"
OFFSET_CLOSE_TODAY = "3"
OFFSET_CLOSE_YESTERDAY = "4"

DIRECTION_BUY = "0"
DIRECTION_SELL = "1"

# 需要单独报平今、平昨的品种。bu 是上期所沥青，c 是大商所玉米。
# 用完整字母前缀比较，避免把沪铜 cu 当成玉米 c。
DEFAULT_CLOSE_SPLIT_PRODUCTS = ("bu", "c")


def product_code(symbol: str) -> str:
    """bu2612 -> bu，c2511 -> c，cu2601 -> cu。"""
    text = str(symbol or "").strip().lower()
    end = 0
    while end < len(text) and text[end].isalpha():
        end += 1
    return text[:end]


def symbol_key(symbol: str) -> str:
    """柜台返回 BU2612，程序里记 bu2612。比对前都收成小写。"""
    return str(symbol or "").strip().lower()


# 郑商所、中金所的合约代码必须大写。上期所、能源中心、大商所、广期所必须小写。
# 把沥青写成 BU2612 时，柜台会直接回「找不到合约」。
_CTP_EXCHANGE_BY_PRODUCT: dict[str, str] = {
    "AP": "CZCE", "CF": "CZCE", "CJ": "CZCE", "CY": "CZCE", "FG": "CZCE",
    "JR": "CZCE", "LR": "CZCE", "MA": "CZCE", "OI": "CZCE", "PF": "CZCE",
    "PK": "CZCE", "PL": "CZCE", "PM": "CZCE", "PR": "CZCE", "PX": "CZCE",
    "RI": "CZCE", "RM": "CZCE", "RS": "CZCE", "SA": "CZCE", "SF": "CZCE",
    "SH": "CZCE", "SM": "CZCE", "SR": "CZCE", "TA": "CZCE", "UR": "CZCE",
    "WH": "CZCE", "ZC": "CZCE",
    "IF": "CFFEX", "IH": "CFFEX", "IC": "CFFEX", "IM": "CFFEX",
    "T": "CFFEX", "TF": "CFFEX", "TS": "CFFEX", "TL": "CFFEX",
    "CU": "SHFE", "AL": "SHFE", "ZN": "SHFE", "PB": "SHFE", "NI": "SHFE",
    "SN": "SHFE", "AU": "SHFE", "AG": "SHFE", "RB": "SHFE", "HC": "SHFE",
    "SS": "SHFE", "BU": "SHFE", "RU": "SHFE", "SP": "SHFE", "FU": "SHFE",
    "WR": "SHFE", "AO": "SHFE", "BR": "SHFE", "AD": "SHFE",
    "SC": "INE", "NR": "INE", "LU": "INE", "BC": "INE", "EC": "INE",
    "A": "DCE", "B": "DCE", "C": "DCE", "CS": "DCE", "EB": "DCE", "EG": "DCE",
    "I": "DCE", "J": "DCE", "JM": "DCE", "L": "DCE", "M": "DCE", "P": "DCE",
    "PG": "DCE", "PP": "DCE", "V": "DCE", "Y": "DCE", "LH": "DCE", "JD": "DCE",
    "FB": "DCE", "BB": "DCE", "RR": "DCE", "LG": "DCE",
    "SI": "GFEX", "LC": "GFEX", "PS": "GFEX",
}


def ctp_exchange_id(symbol: str) -> str:
    """bu2612 -> SHFE，SA2701 -> CZCE。不认识的品种留空，让柜台按合约自己判断。"""
    return _CTP_EXCHANGE_BY_PRODUCT.get(product_code(symbol).upper(), "")


def ctp_instrument_id(symbol: str) -> str:
    """
    发给柜台的合约代码。

    上期所沥青必须是 bu2612。写成 BU2612，柜台回报就是「找不到合约」。
    郑商所、中金所相反，要大写，例如 SA2701、IF2612。
    """
    text = str(symbol or "").strip()
    exchange = ctp_exchange_id(text)
    if exchange in {"CZCE", "CFFEX"}:
        return text.upper()
    return text.lower()


# 合约写成字母加数字，例如 bu2612。写错就在本机拦住，不发给柜台。
_SYMBOL_OK = re.compile(r"^[A-Za-z]{1,4}\d{3,4}$")


def check_order_instruction(
    symbol: str,
    price: float,
    volume: int,
    *,
    price_tick: float,
    max_volume: int,
) -> str:
    """
    报单送出之前先在本机检查。

    合约代码不像期货合约、价格不是最小变动价位的整数倍、手数超过单笔上限，
    都返回一段说明。返回空字符串才允许继续发给柜台。
    """
    text = str(symbol or "").strip()
    if not _SYMBOL_OK.match(text):
        shown = text or "空"
        return f"合约代码不正确：{shown}。已在本机拦住，没有报给柜台"
    lots = int(volume)
    if lots <= 0:
        return "手数必须大于 0。已在本机拦住，没有报给柜台"
    limit = int(max_volume)
    if limit > 0 and lots > limit:
        return f"委托 {lots} 手，超过单笔最大 {limit} 手。已在本机拦住，没有报给柜台"
    tick = float(price_tick)
    if tick > 0:
        steps = Decimal(str(price)) / Decimal(str(tick))
        if steps != steps.to_integral_value():
            return f"价格 {price} 不是最小变动价位 {tick} 的整数倍。已在本机拦住，没有报给柜台"
    return ""


def test_front_blocks_live_account(td_front: str, user_id: str, live_user_id: str) -> str:
    """华泰仿真前置不能拿实盘资金账号去登录。"""
    front = str(td_front or "")
    account = str(user_id or "").strip()
    live = str(live_user_id or "").strip()
    if "59.36.3.115" in front and account and live and account == live:
        return "这是华泰仿真前置，不能用实盘资金账号登录。请把 trade.ctp.user_id 改成测试指引里的仿真账号"
    return ""


@dataclass
class OrderActivity:
    """统计报单笔数和撤单笔数。笔数不是手数，一笔报单算 1。"""

    order_warn: int = 8
    cancel_warn: int = 8
    orders: int = 0
    cancels: int = 0

    def note_order(self) -> str:
        """记一笔已经发往柜台的报单，并在达到阈值时附上警示。"""
        self.orders += 1
        return self._text(self.orders, self.order_warn, "报单")

    def note_cancel(self) -> str:
        """记一笔撤单请求，并在达到阈值时附上警示。"""
        self.cancels += 1
        return self._text(self.cancels, self.cancel_warn, "撤单")

    def _text(self, count: int, warn: int, name: str) -> str:
        text = f"今日报单笔数 {self.orders}，撤单笔数 {self.cancels}"
        if warn > 0 and count >= warn:
            text += f"。{name}笔数已达到阈值 {warn}，请停止继续{name}"
        return text


@dataclass
class CloseSlice:
    """平仓拆出来的一笔。offset 用上面的柜台标志。"""

    offset: str
    volume: int


def plan_close(
    symbol: str,
    volume: int,
    *,
    today: int,
    yesterday: int,
    split_products: tuple[str, ...] = DEFAULT_CLOSE_SPLIT_PRODUCTS,
) -> list[CloseSlice]:
    """
    把要平的手数拆成柜台认的开平标志。

    沥青、玉米先平昨仓，剩下的再平今仓。
    昨仓和今仓都不够时，多出来的仍按平今报，让柜台拒单，避免悄悄少平。
    其他品种用普通平仓，由交易所自己处理今昨。
    """
    lots = max(0, int(volume))
    if lots <= 0:
        return []
    if product_code(symbol) not in {p.lower() for p in split_products}:
        return [CloseSlice(OFFSET_CLOSE, lots)]
    yd = max(0, int(yesterday))
    td = max(0, int(today))
    use_yd = min(lots, yd)
    rest = lots - use_yd
    use_td = min(rest, td)
    leftover = rest - use_td
    slices: list[CloseSlice] = []
    if use_yd:
        slices.append(CloseSlice(OFFSET_CLOSE_YESTERDAY, use_yd))
    if use_td:
        slices.append(CloseSlice(OFFSET_CLOSE_TODAY, use_td))
    if leftover:
        slices.append(CloseSlice(OFFSET_CLOSE_TODAY, leftover))
    if not slices:
        slices.append(CloseSlice(OFFSET_CLOSE_TODAY, lots))
    return slices


def close_direction(position_side: str) -> str:
    """平多要卖，平空要买。"""
    if str(position_side).lower() == "short":
        return DIRECTION_BUY
    return DIRECTION_SELL


def open_direction(side: str) -> str:
    """buy_open 买，sell_open 卖。"""
    if side == "sell_open":
        return DIRECTION_SELL
    return DIRECTION_BUY


@dataclass
class TrackedOrder:
    """一笔已经交给柜台的报单，以及目前收到的回报。"""

    order_ref: str
    symbol: str
    direction: str
    offset: str
    volume: int
    price: float
    status: str = "pending"
    filled: int = 0
    # 成交回报累计的手数。OnRtnOrder 的成交量是累计值，OnRtnTrade 是增量值；
    # 两者分开记，再取较大的那个，避免同一笔成交被加两遍。
    traded: int = 0
    avg_price: float = 0.0
    message: str = ""
    accounted: bool = False
    stop_price: float | None = None
    bar_id: str = ""
    # 撤单时柜台要这些编号。回报到达之前是空的。
    exchange_id: str = ""
    order_sys_id: str = ""
    front_id: int = 0
    session_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_ref": self.order_ref,
            "symbol": symbol_key(self.symbol),
            "direction": self.direction,
            "offset": self.offset,
            "volume": self.volume,
            "price": self.price,
            "status": self.status,
            "filled": self.filled,
            "avg_price": self.avg_price,
            "message": self.message,
            "stop_price": self.stop_price,
            "bar_id": self.bar_id,
        }


def apply_order_report(
    order: TrackedOrder,
    *,
    status_code: str,
    filled: int | None = None,
    message: str = "",
) -> str:
    """
    用柜台委托回报更新状态。

    返回这一次变成的状态：submitted / rejected / partial / filled / cancelled。
    """
    code = str(status_code or "").strip()
    done = order.filled if filled is None else max(0, int(filled))
    # 委托回报带的是累计成交量；成交回报也可能先到。只往上修正，不能把已有成交抹掉。
    order.filled = max(order.filled, min(order.volume, done))
    if message:
        order.message = message
    if code in {"rejected", "err"}:
        order.status = "rejected"
        return order.status
    if code in {"0", "filled", "AllTraded"} or order.filled >= order.volume > 0:
        order.filled = order.volume
        order.status = "filled"
        return order.status
    # 2 = 部分成交，剩余已不在队列；4 = 未成交且不在队列；5 = 已撤。
    # 这三种都不会再变，必须算终态。否则报单表一直显示「还在挂单」，
    # 后续开仓和平仓都会被 has_working_order 挡住，已成交的手数也永远没人记账。
    if code in {"2", "4", "5", "Canceled", "cancelled"}:
        order.status = "cancelled"
        return order.status
    if code in {"1", "partial"} or 0 < order.filled < order.volume:
        order.status = "partial"
        return order.status
    order.status = "submitted"
    return order.status


def apply_trade(order: TrackedOrder, price: float, volume: int) -> str:
    """
    成交回报。OnRtnTrade 的 volume 是这一次的增量，累计到 traded。

    委托回报（OnRtnOrder）已经带过累计成交量，这里再累加同一笔就会多算；
    所以 filled 取「委托累计」和「成交累计」的较大值，而不是两者相加。
    """
    lots = max(0, int(volume))
    if lots <= 0:
        return order.status
    before = order.traded
    order.traded = min(order.volume, before + lots)
    added = order.traded - before
    if added > 0:
        order.avg_price = ((order.avg_price * before) + (float(price) * added)) / order.traded
    order.filled = max(order.filled, order.traded)
    # 已经拒单/撤单的报单，迟到的成交回报不能再把它改回进行中。
    if order.status in {"rejected", "cancelled"}:
        return order.status
    if order.filled >= order.volume:
        order.status = "filled"
    else:
        order.status = "partial"
    return order.status


def is_terminal(status: str) -> bool:
    """这一笔不会再变：全部成交、拒单，或撤单后没有剩余。部分成交还可能继续。"""
    return status in {"filled", "rejected", "cancelled"}


@dataclass
class PositionView:
    """一个合约在柜台里的今仓、昨仓。多仓为正的方向分开记。"""

    long_today: int = 0
    long_yesterday: int = 0
    short_today: int = 0
    short_yesterday: int = 0

    @property
    def net(self) -> int:
        """多仓减空仓。和程序里的净持仓同一套符号。"""
        long_qty = self.long_today + self.long_yesterday
        short_qty = self.short_today + self.short_yesterday
        return long_qty - short_qty


def absorb_position_row(view: PositionView, row: dict[str, Any]) -> None:
    """
    把柜台一行持仓加进今仓/昨仓。

    上期所（沥青）同一合约会拆成两行：一行只有昨仓，一行只有今仓。
    大商所（玉米）通常一行里同时给出今仓和昨仓。
    多空用 PosiDirection：2 是多，3 是空。
    """
    position = int(row.get("position") or 0)
    today = int(row.get("today") or 0)
    yesterday = int(row.get("yesterday") or 0)
    if position == 0 and today == 0 and yesterday == 0:
        return
    direction = str(row.get("direction") or "")
    exchange = str(row.get("exchange") or "").upper()
    product = product_code(str(row.get("symbol") or ""))
    shfe_style = exchange in {"SHFE", "INE"} or product == "bu"
    if shfe_style:
        if yesterday > 0 and today == 0:
            add_yd = position or yesterday
            add_td = 0
        else:
            add_td = position or today
            add_yd = 0
    else:
        add_td = today
        add_yd = yesterday
        if add_td == 0 and add_yd == 0:
            add_yd = position
    if direction in {"3", "short", "sell"}:
        view.short_today += add_td
        view.short_yesterday += add_yd
    else:
        view.long_today += add_td
        view.long_yesterday += add_yd


def book_differences(internal: dict[str, int], remote: dict[str, PositionView]) -> list[str]:
    """
    程序净持仓和柜台净持仓对不上的说明。

    空列表表示一致。某一边没有的合约当成 0。
    """
    notes: list[str] = []
    names = {symbol_key(k) for k in internal} | {symbol_key(k) for k in remote}
    remote_net = {symbol_key(k): v.net for k, v in remote.items()}
    internal_net = {symbol_key(k): int(v) for k, v in internal.items()}
    for name in sorted(names):
        left = int(internal_net.get(name, 0))
        right = int(remote_net.get(name, 0))
        if left != right:
            notes.append(f"{name} 程序净持仓 {left} 手，柜台 {right} 手")
    return notes


@dataclass
class SessionTracker:
    """
    登录步骤。

    连接成功后才能认证，认证成功才能登录，登录成功才能确认结算单。
    断线后回到断开，重连成功要先把未完成报单查一遍。
    """

    phase: str = "idle"
    front_id: int = 0
    session_id: int = 0
    trading_day: str = ""
    disconnects: int = 0
    need_order_check: bool = False
    last_error: str = ""

    def on_connected(self) -> str:
        self.phase = "connected"
        return "authenticate"

    def on_authenticated(self, ok: bool, message: str = "") -> str:
        if not ok:
            self.phase = "connected"
            self.last_error = message or "认证失败"
            return "stop"
        self.phase = "authenticated"
        return "login"

    def on_logged_in(self, ok: bool, front_id: int = 0, session_id: int = 0, trading_day: str = "", message: str = "") -> str:
        if not ok:
            self.phase = "authenticated"
            self.last_error = message or "登录失败"
            return "stop"
        self.phase = "logged_in"
        self.front_id = int(front_id)
        self.session_id = int(session_id)
        self.trading_day = str(trading_day or "")
        return "confirm"

    def on_settlement_confirmed(self, ok: bool, message: str = "") -> str:
        if not ok:
            self.last_error = message or "结算单确认失败"
            return "stop"
        self.phase = "ready"
        # 刚连上或刚重连，先查未完成的报单，确认上一笔到底成没成。
        self.need_order_check = True
        return "query_orders"

    def on_orders_checked(self) -> None:
        self.need_order_check = False

    def on_disconnected(self, reason: str = "") -> str:
        self.phase = "disconnected"
        self.disconnects += 1
        self.last_error = reason or "交易前置断开"
        self.need_order_check = True
        return "reconnect"

    @property
    def ready(self) -> bool:
        return self.phase == "ready"


@dataclass
class OrderTable:
    """按报单编号记住每一笔，避免同一笔成交回报记两次账。"""

    orders: dict[str, TrackedOrder] = field(default_factory=dict)
    next_ref: int = 1

    def new_ref(self) -> str:
        ref = str(self.next_ref)
        self.next_ref += 1
        return ref

    def add(self, order: TrackedOrder) -> None:
        self.orders[order.order_ref] = order

    def get(self, order_ref: str) -> TrackedOrder | None:
        return self.orders.get(str(order_ref))

    def unfinished(self) -> list[TrackedOrder]:
        return [row for row in self.orders.values() if not is_terminal(row.status)]
