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


def direction_cn(code: str) -> str:
    """柜台买卖方向：0 买，1 卖。"""
    flag = str(code or "").strip()[:1]
    return {"0": "买", "1": "卖"}.get(flag, str(code or ""))


def offset_cn(code: str) -> str:
    """柜台开平：0 开仓，1 平仓，3 平今，4 平昨。"""
    flag = str(code or "").strip()[:1]
    return {"0": "开仓", "1": "平仓", "3": "平今", "4": "平昨"}.get(flag, str(code or ""))

# 需要单独报平今、平昨的品种。配置里可再追加；上期所、能源中心、大商所即使没写也会拆。
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


def needs_close_split(symbol: str) -> bool:
    """
    上期所、能源中心、大商所不能报普通平仓，必须拆成平今或平昨。

    燃油 fu 和沥青 bu 一样在上期所。报「平仓」柜台会回「平仓位不足」。
    """
    return ctp_exchange_id(symbol) in {"SHFE", "INE", "DCE"}


def keep_order_sys_id(sys_id: str) -> str:
    """
    撤单用的柜台编号。

    上期所编号常带前导空格。存盘时若已经带空格就原样保留；
    若只剩数字，右对齐补到 12 位，柜台才能找到那一笔。
    """
    text = str(sys_id or "")
    body = text.strip()
    if not body:
        return ""
    if text != body:
        return text
    return body.rjust(12)


def describe_working_orders(orders: list[Any]) -> str:
    """把还没完结的委托写成一句话，方便对照无限易。"""
    live = [row for row in orders if not is_terminal(str(getattr(row, "status", "") or ""))]
    if not live:
        return "柜台没有未成交委托。"
    parts: list[str] = []
    for row in live:
        ref = str(getattr(row, "order_ref", "") or "").strip()
        name = symbol_key(str(getattr(row, "symbol", "") or ""))
        price = float(getattr(row, "price", 0) or 0)
        volume = int(getattr(row, "volume", 0) or 0)
        note = str(getattr(row, "message", "") or "") or str(getattr(row, "status", "") or "")
        piece = f"编号 {ref} {name} {volume} 手"
        if price > 0:
            piece += f" @{price:g}"
        if note:
            piece += f" {note}"
        parts.append(piece)
    return f"未成交 {len(live)} 笔：" + "；".join(parts)


def ctp_field_symbol(data: Any) -> str:
    """
    从柜台一条行情/报单里取出合约代码。

    6.7 起旧字段改名叫 reserve1，新字段才叫 InstrumentID。
    华泰仿真行情经常只填 reserve1，只读新字段会把整条跳动丢掉。
    """
    if data is None:
        return ""
    for name in ("InstrumentID", "reserve1"):
        text = str(getattr(data, name, "") or "").strip()
        if text:
            return text
    return ""


def ctp_quote_clock(data: Any) -> str:
    """行情时间。带上毫秒，同一秒内的跳动也能看出来。"""
    if data is None:
        return ""
    clock = str(getattr(data, "UpdateTime", "") or "").strip()
    try:
        ms = int(getattr(data, "UpdateMillisec", 0) or 0)
    except (TypeError, ValueError):
        ms = 0
    if clock and ms:
        return f"{clock}.{ms:03d}"
    return clock


def ctp_depth_row(data: Any) -> dict[str, Any] | None:
    """把柜台盘口结构收成窗口用的一行。没有数据就返回空。"""
    if data is None:
        return None

    def _num(name: str) -> float:
        try:
            return float(getattr(data, name, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _lots(name: str) -> int:
        try:
            return int(getattr(data, name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "symbol": ctp_field_symbol(data),
        "last": _num("LastPrice"),
        "bid": _num("BidPrice1"),
        "ask": _num("AskPrice1"),
        "bid_volume": _lots("BidVolume1"),
        "ask_volume": _lots("AskVolume1"),
        "upper": _num("UpperLimitPrice"),
        "lower": _num("LowerLimitPrice"),
        "time": ctp_quote_clock(data),
    }


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


CTP_KIND_SIM = "sim"
CTP_KIND_LIVE = "live"
CTP_KIND_LABELS = {CTP_KIND_SIM: "仿真账号", CTP_KIND_LIVE: "真实账号"}


def normalize_ctp_kind(value: Any) -> str:
    """把界面上的「仿真账号 / 真实账号」收成 sim 或 live。"""
    text = str(value or "").strip().lower()
    if text in {"live", "real", "prod", "实盘", "真实", "真实账号"}:
        return CTP_KIND_LIVE
    return CTP_KIND_SIM


def ctp_kind_label(value: Any) -> str:
    """给界面显示用的中文名。"""
    return CTP_KIND_LABELS[normalize_ctp_kind(value)]


def overlay_ctp_profile(
    ctp: dict[str, Any] | None,
    kind: Any = None,
    account: dict[str, Any] | None = None,
    account_user_id: str = "",
    account_password: str = "",
) -> dict[str, Any]:
    """
    把仿真或实盘账号叠到共用参数上。

    窗口和策略走同一套字段。
    仿真：只用 sim 小节（默认 28077 + 仿真前置），不会去用资金账号。
    真实：资金账号就是配置页「资金账号」（同花顺登录的那一个，例如 20318052）；
    交易/行情前置只用 live 小节，不会把仿真前置抄过去。
    """
    raw = dict(ctp or {})
    acc = account if isinstance(account, dict) else {}
    # 资金账号只在配置页填一次；CTP 真实通道跟这一号走。
    fund_id = str(account_user_id or acc.get("user_id") or "").strip()
    fund_pw = str(account_password or acc.get("resolved_password") or "").strip()
    chosen = normalize_ctp_kind(kind if kind is not None else raw.get("account_kind"))
    nested = raw.get(chosen)
    nested = dict(nested) if isinstance(nested, dict) else {}
    merged = dict(raw)
    keys = ("td_front", "md_front", "user_id", "broker_id")
    if chosen == CTP_KIND_LIVE:
        # 前置必须是华泰给你的实盘地址，缺了就空着，让登录提示去填。
        merged["td_front"] = str(nested.get("td_front") or "").strip()
        merged["md_front"] = str(nested.get("md_front") or "").strip()
        merged["broker_id"] = str(
            nested.get("broker_id") or raw.get("broker_id") or ""
        ).strip()
        # 真实资金账号 = 上面的资金账号。live.user_id 只是保存时的副本，不再单独填。
        merged["user_id"] = fund_id or str(nested.get("user_id") or "").strip()
        # 实盘密码优先用单独写的 ctp_live_password；没写就用同一份交易密码。
        merged["resolved_password"] = str(
            raw.get("resolved_live_password")
            or nested.get("resolved_password")
            or fund_pw
            or ""
        ).strip()
    else:
        for key in keys:
            overlay = str(nested.get(key) or "").strip()
            if overlay:
                merged[key] = overlay
        merged["resolved_password"] = str(
            raw.get("resolved_sim_password")
            or raw.get("resolved_password")
            or nested.get("resolved_password")
            or ""
        ).strip()
    merged["account_kind"] = chosen
    return merged


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
        """记一笔已经发往柜台的报单，并在达到阈值时附上警示。只提醒，不拦住下一笔。"""
        self.orders += 1
        return self.describe(kind="报单")

    def note_cancel(self) -> str:
        """记一笔撤单请求，并在达到阈值时附上警示。只提醒，不拦住下一笔。"""
        self.cancels += 1
        return self.describe(kind="撤单")

    def describe(self, kind: str | None = None) -> str:
        """窗口和日志共用的笔数说明。到了阈值只写「请停止」，不会禁止再报。"""
        text = f"今日报单笔数 {self.orders}，撤单笔数 {self.cancels}"
        show_order = kind in {None, "", "报单"}
        show_cancel = kind in {None, "", "撤单"}
        if show_order and self.order_warn > 0 and self.orders >= self.order_warn:
            text += f"。报单笔数已达到阈值 {self.order_warn}，请停止继续报单"
        if show_cancel and self.cancel_warn > 0 and self.cancels >= self.cancel_warn:
            text += f"。撤单笔数已达到阈值 {self.cancel_warn}，请停止继续撤单"
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

    上期所、能源中心、大商所先平昨仓，剩下的再平今仓。
    昨仓和今仓都不够时，多出来的仍按平今报，让柜台拒单，避免悄悄少平。
    郑商所、中金所用普通平仓，由交易所自己处理今昨。
    """
    lots = max(0, int(volume))
    if lots <= 0:
        return []
    listed = {p.lower() for p in split_products}
    if product_code(symbol) not in listed and not needs_close_split(symbol):
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
    # 已经记入本程序账本的累计手数。部分成交分几次记账时，下一次只记增量。
    accounted_volume: int = 0
    # 不是这次登录由本程序发出的单（手工单、别人的单、重启前遗留）。只显示，不入账。
    foreign: bool = False
    stop_price: float | None = None
    bar_id: str = ""
    # 撤单时柜台要这些编号。回报到达之前是空的。
    exchange_id: str = ""
    order_sys_id: str = ""
    front_id: int = 0
    session_id: int = 0
    insert_time: str = ""
    hedge: str = "1"
    remaining: int = 0

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
            "accounted_volume": self.accounted_volume,
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
    # 柜台持仓查询自带：开仓成本（用来算均价）、持仓盈亏（浮盈）。
    long_open_cost: float = 0.0
    short_open_cost: float = 0.0
    long_profit: float = 0.0
    short_profit: float = 0.0
    long_margin: float = 0.0
    short_margin: float = 0.0
    hedge: str = "1"

    @property
    def long_lots(self) -> int:
        """这一边买开还剩几手（今仓加昨仓）。"""
        return int(self.long_today) + int(self.long_yesterday)

    @property
    def short_lots(self) -> int:
        """这一边卖开还剩几手（今仓加昨仓）。"""
        return int(self.short_today) + int(self.short_yesterday)

    @property
    def net(self) -> int:
        """多仓减空仓。和程序里的净持仓同一套符号。"""
        return self.long_lots - self.short_lots


def close_sides_for_view(view: PositionView | None) -> list[str]:
    """
    交易台「平仓」要平哪几边。

    锁仓时净持仓是 0，但仍有买开和卖开，必须两边都发平仓，不能当成空仓。
    返回值给 place_order 的 position_side：long=平多，short=平空。
    """
    if view is None:
        return []
    sides: list[str] = []
    if view.long_lots > 0:
        sides.append("long")
    if view.short_lots > 0:
        sides.append("short")
    return sides


def reverse_open_side(position_side: str) -> str:
    """
    策略反手：先平旧仓，再开反向。

    手里是多仓就卖开；手里是空仓就买开。锁仓时不要调用，应先平掉一边。
    """
    return "sell_open" if str(position_side) == "long" else "buy_open"


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
    # 开仓成本和浮盈跟这一行的方向走。上期所今昨拆成两行时，两边各自加一次。
    cost = float(row.get("open_cost") or 0)
    profit = float(row.get("position_profit") or 0)
    margin = float(row.get("use_margin") or 0)
    hedge = str(row.get("hedge") or "").strip()
    if hedge:
        view.hedge = hedge
    if direction in {"3", "short", "sell"}:
        view.short_today += add_td
        view.short_yesterday += add_yd
        view.short_open_cost += cost
        view.short_profit += profit
        view.short_margin += margin
    else:
        view.long_today += add_td
        view.long_yesterday += add_yd
        view.long_open_cost += cost
        view.long_profit += profit
        view.long_margin += margin


def describe_positions(positions: dict[str, PositionView], available: float | None = None) -> str:
    """把柜台持仓写成人能看懂的一句话，方便对照无限易。"""
    money = f"{available:.2f}" if available is not None else "未知"
    parts: list[str] = []
    for name in sorted(positions):
        view = positions[name]
        long_qty = int(view.long_today) + int(view.long_yesterday)
        short_qty = int(view.short_today) + int(view.short_yesterday)
        if long_qty <= 0 and short_qty <= 0:
            continue
        bits: list[str] = []
        if long_qty > 0:
            bits.append(f"买 {long_qty} 手")
        if short_qty > 0:
            bits.append(f"卖 {short_qty} 手")
        parts.append(f"{name} " + "，".join(bits))
    if not parts:
        return f"柜台现在没有持仓。可用资金 {money}"
    return "柜台持仓：" + "；".join(parts) + f"。可用资金 {money}"


def position_avg_price(open_cost: float, lots: int, multiplier: float) -> float | None:
    """
    用柜台开仓成本反推持仓均价。

    持仓查询没有「上一笔成交价」。OpenCost 一般是 均价 × 手数 × 合约乘数。
    成本是 0、手数或乘数不对时，算不出来，返回空。
    """
    if lots <= 0 or multiplier <= 0 or abs(float(open_cost)) < 1e-9:
        return None
    return float(open_cost) / (float(lots) * float(multiplier))


def format_position_marks(view: PositionView, multiplier: float) -> tuple[str, str]:
    """
    给持仓表用的均价、浮盈两列文字。

    均价来自开仓成本，不是成交页那一笔的成交价。
    浮盈来自柜台 PositionProfit，刷新持仓时更新，不是我们用买一卖一现算的。
    """
    long_qty = int(view.long_today) + int(view.long_yesterday)
    short_qty = int(view.short_today) + int(view.short_yesterday)
    both = long_qty > 0 and short_qty > 0
    bits: list[str] = []
    long_px = position_avg_price(view.long_open_cost, long_qty, multiplier)
    short_px = position_avg_price(view.short_open_cost, short_qty, multiplier)
    if long_qty > 0 and long_px is not None:
        bits.append(f"买 {long_px:g}" if both else f"{long_px:g}")
    if short_qty > 0 and short_px is not None:
        bits.append(f"卖 {short_px:g}" if both else f"{short_px:g}")
    avg_text = " / ".join(bits) if bits else "—"
    profit = float(view.long_profit) + float(view.short_profit)
    pnl_text = f"{profit:+.2f}"
    return avg_text, pnl_text


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


@dataclass(frozen=True)
class ReconcileReport:
    """一次三方对账的结果：柜台、信号账户账本、程序内存。"""

    at: str
    queried: bool          # 柜台查询是否成功；False 时禁止据此修复内存
    broker: dict[str, int]
    ledger: dict[str, int]
    memory: dict[str, int]
    funds: float | None
    ok: bool
    diffs: list[str] = field(default_factory=list)
    unclaimed: list[str] = field(default_factory=list)   # 柜台有、账本无
    phantom: list[str] = field(default_factory=list)     # 账本有、柜台无
    extra: list[str] = field(default_factory=list)       # 内存有、柜台与账本都无

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at, "queried": self.queried, "broker": dict(self.broker),
                "ledger": dict(self.ledger), "memory": dict(self.memory), "funds": self.funds,
                "ok": self.ok, "diffs": list(self.diffs), "unclaimed": list(self.unclaimed),
                "phantom": list(self.phantom), "extra": list(self.extra)}


def three_way_diff(
    broker: dict[str, int] | None = None,
    ledger: dict[str, int] | None = None,
    memory: dict[str, int] | None = None,
) -> tuple[bool, list[str], list[str], list[str], list[str]]:
    """统一小写键后比较；返回 (ok, diffs, unclaimed, phantom, extra)。"""
    left = {symbol_key(k): int(v or 0) for k, v in (broker or {}).items()}
    book = {symbol_key(k): int(v or 0) for k, v in (ledger or {}).items()}
    hold = {symbol_key(k): int(v or 0) for k, v in (memory or {}).items()}
    names = sorted(set(left) | set(book) | set(hold))
    diffs: list[str] = []
    unclaimed: list[str] = []
    phantom: list[str] = []
    extra: list[str] = []
    ok = True
    for name in names:
        at_broker = int(left.get(name, 0))
        at_ledger = int(book.get(name, 0))
        at_memory = int(hold.get(name, 0))
        if at_broker != at_ledger:
            diffs.append(f"{name} 柜台 {at_broker} 手，账本 {at_ledger} 手")
            ok = False
            if at_broker != 0 and at_ledger == 0:
                unclaimed.append(name)
            elif at_broker == 0 and at_ledger != 0:
                phantom.append(name)
        if at_broker != at_memory or at_ledger != at_memory:
            ok = False
        if at_memory != 0 and at_broker == 0 and at_ledger == 0:
            extra.append(name)
    return ok, diffs, unclaimed, phantom, extra


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


def _canon_order_ref(order_ref: str) -> str:
    """把柜台回报里的报单编号收成程序能对上的样子。"""
    text = str(order_ref or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    return text


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
        """
        按报单编号找回这一笔。

        柜台回报常把编号补空格或前导零，例如发出去是 1，回来是 '           1'。
        对不上就会一直显示「待报」，撤单也找不到。
        """
        raw = str(order_ref or "")
        found = self.orders.get(raw)
        if found is not None:
            return found
        key = _canon_order_ref(raw)
        if not key:
            return None
        found = self.orders.get(key)
        if found is not None:
            return found
        for stored, order in self.orders.items():
            if _canon_order_ref(stored) == key:
                return order
        return None

    def unfinished(self) -> list[TrackedOrder]:
        return [row for row in self.orders.values() if not is_terminal(row.status)]
