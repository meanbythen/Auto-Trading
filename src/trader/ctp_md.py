"""
CTP 行情。

功能：连接券商档案里配置的行情前置，订阅窗口里的那一个合约，
把最新价、买一、卖一、涨停价、跌停价交给交易台。仿真、真实同一套。
原理：行情前置必须来自配置（md_front）；交易端口和行情端口不是同一个，
程序不再按交易端口去猜行情端口。这里不计算开仓信号，也不发报单。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from src.trader.ctp_logic import (
    CTP_KIND_LIVE,
    ctp_depth_row,
    ctp_field_symbol,
    ctp_instrument_id,
    normalize_ctp_kind,
)
from src.trader.ctp_td import prepare_ctp_dll_search


def market_front(md_front: str = "") -> str:
    """行情前置只认配置。没写就留空，由界面提示去券商档案里补。"""
    return str(md_front or "").strip()


def quote_text(value: Any) -> str:
    """把行情里的价格写成窗口上的数字。空价和特别大的占位数字显示成横线。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number <= 0 or number >= 1_000_000_000:
        return "—"
    if abs(number - round(number)) < 0.0001:
        return str(int(round(number)))
    return f"{number:.2f}"


def describe_quote(row: dict[str, Any]) -> dict[str, Any]:
    """给一条行情补上窗口直接能显示的文字。"""
    view = dict(row)
    view["last_text"] = quote_text(row.get("last"))
    view["bid_text"] = quote_text(row.get("bid"))
    view["ask_text"] = quote_text(row.get("ask"))
    view["upper_text"] = quote_text(row.get("upper"))
    view["lower_text"] = quote_text(row.get("lower"))
    view["time_text"] = str(row.get("time") or "").strip() or "—"
    view["bid_vol_text"] = str(int(row.get("bid_volume") or 0))
    view["ask_vol_text"] = str(int(row.get("ask_volume") or 0))
    return view


def merge_quote(old: dict[str, Any] | None, incoming: dict[str, Any], *, replace_empty: bool) -> dict[str, Any]:
    """
    把新盘口叠到已有报价上。

    实时行情 replace_empty=True：盘口真没卖一，就显示横线。
    柜台快照 replace_empty=False：快照缺卖一，不要把已经收到的卖一擦掉。
    """
    base = dict(old or {})
    for key in (
        "symbol",
        "last",
        "bid",
        "ask",
        "bid_volume",
        "ask_volume",
        "upper",
        "lower",
        "time",
    ):
        if key not in incoming:
            continue
        val = incoming[key]
        if key == "symbol":
            if str(val or "").strip():
                base[key] = val
            continue
        if key == "time":
            if str(val or "").strip():
                base[key] = val
            continue
        if key.endswith("volume"):
            if replace_empty or val not in (None, ""):
                base[key] = val
            continue
        if replace_empty or quote_text(val) != "—":
            base[key] = val
    return describe_quote(base)


def quote_channel_word(settings: Any | None = None) -> str:
    """日志里写真实还是仿真，避免实盘还显示仿真报价。"""
    if settings is None:
        return "柜台"
    kind = normalize_ctp_kind(getattr(settings, "account_kind", "") or "")
    if kind == CTP_KIND_LIVE:
        return "真实"
    return "仿真"


def quote_book_key(row: dict[str, Any]) -> tuple[str, ...]:
    """买一卖一涨跌停。最新价来回跳时不要靠这一组去刷日志。"""
    view = describe_quote(row)
    return (
        str(view.get("symbol") or ""),
        str(view.get("bid_text") or "—"),
        str(view.get("ask_text") or "—"),
        str(view.get("upper_text") or "—"),
        str(view.get("lower_text") or "—"),
    )


def quote_line(row: dict[str, Any], *, channel: str = "") -> str:
    """一行说明。交易台盘口用数字显示，不要拿这一行去刷运行日志。"""
    view = describe_quote(row)
    name = str(view.get("symbol") or "")
    tag = f"{channel}报价" if str(channel or "").strip() else "柜台报价"
    return (
        f"{name} {tag} 最新 {view.get('last_text', '—')}"
        f" 买一 {view.get('bid_text', '—')} 卖一 {view.get('ask_text', '—')}"
        f" 涨停 {view.get('upper_text', '—')} 跌停 {view.get('lower_text', '—')}"
    )


class CtpMdClient:
    """连行情前置，订阅一个或多个合约。仿真、真实同一套。

    单合约就是列表里只有一个元素；多合约用 subscribe_many()。
    两种走同一条订阅与分发路径，不再维护两套逻辑。
    """

    def _md_name(self) -> str:
        """给窗口和日志用的名称：真实行情 / 仿真行情。"""
        return f"{quote_channel_word(self.settings)}行情"

    def __init__(self, settings: Any) -> None:
        from src.trader.ctp_fronts import collect_fronts

        self.settings = settings
        self.fronts = collect_fronts(
            getattr(settings, "md_front", ""),
            getattr(settings, "md_fronts", ()),
        )
        self.front = self.fronts[0] if self.fronts else market_front(getattr(settings, "md_front", ""))
        self.wanted = ""
        self.wanted_symbols: set[str] = set()
        self.latest: dict[str, Any] | None = None
        self.latest_by_symbol: dict[str, dict[str, Any]] = {}
        self.sub_status: dict[str, tuple[bool, str]] = {}
        self.last_tick_at = 0.0
        self.last_tick_at_by_symbol: dict[str, float] = {}
        self.ready = False
        self._api: Any = None
        self._spi: Any = None
        self._login_event = threading.Event()
        self._error = ""
        self._request_id = 0

    def start(self) -> tuple[bool, str]:
        """连接行情前置并登录。已经登录过就直接返回。"""
        if self.ready and self._api is not None:
            return True, f"{self._md_name()}已登录 {self.front}"
        if not self.front:
            return (
                False,
                "还没有行情前置。请在券商档案里填写 md_front（行情前置），"
                "程序不会按交易端口自动推断行情端口",
            )
        if not str(getattr(self.settings, "broker_id", "") or "").strip():
            return False, f"还没有 BrokerID，不能登录{self._md_name()}"
        if not str(getattr(self.settings, "user_id", "") or "").strip():
            return False, f"还没有账号，不能登录{self._md_name()}"
        try:
            mdapi = _load_mdapi(getattr(self.settings, "project_root", "") or "")
        except Exception as exc:  # noqa: BLE001
            text = f"{self._md_name()}接口没有加载成功：{exc}"
            logger.warning(text)
            return False, text
        flow = _md_flow_dir(getattr(self.settings, "flow_dir", "") or "data/ctp_flow")
        self._login_event.clear()
        self._error = ""
        self._api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(flow)
        self._spi = _build_md_spi(mdapi, self)
        self._api.RegisterSpi(self._spi)
        self._api.RegisterFront(self.front)
        self._api.Init()
        logger.info("正在连接{} {}", self._md_name(), self.front)
        timeout = float(getattr(self.settings, "login_timeout_sec", 20) or 20)
        if not self._login_event.wait(timeout):
            self.close()
            if quote_channel_word(self.settings) == "真实":
                return False, f"{self._md_name()}登录超时，请检查行情前置和网络"
            return False, f"{self._md_name()}登录超时。仿真环境白天交易时段才开放"
        if not self.ready:
            reason = self._error or f"{self._md_name()}登录失败"
            self.close()
            return False, reason
        return True, f"{self._md_name()}已登录 {self.front}"

    def subscribe(self, symbol: str) -> tuple[bool, str]:
        """订阅单个合约。等价于 subscribe_many([symbol])，保持原有调用不变。"""
        return self.subscribe_many([symbol])

    def subscribe_many(
        self, symbols: Iterable[str], *, replace: bool = True
    ) -> tuple[bool, str]:
        """订阅多个合约。单合约就是列表里只有一个元素。

        replace=True 时替换当前订阅集合；replace=False 时在原有基础上追加。
        一次调用把整个列表交给柜台，避免单合约与多合约两套代码。
        """
        codes: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            code = ctp_instrument_id(symbol)
            key = code.upper()
            if code and key not in seen:
                seen.add(key)
                codes.append(code)
        if not codes:
            return False, "请先填写合约"
        if not self.ready or self._api is None:
            return False, f"{self._md_name()}还没登录"
        if replace:
            self.wanted_symbols = set()
            self.sub_status.clear()
        self.wanted_symbols.update(codes)
        if replace:
            self.wanted = codes[0]
        elif not self.wanted:
            self.wanted = codes[0]
        try:
            result = self._subscribe(codes)
        except Exception as exc:  # noqa: BLE001
            text = f"订阅{self._md_name()}失败：{exc}"
            logger.warning(text)
            return False, text
        if int(result or 0) != 0:
            return False, f"订阅 {', '.join(codes)} 没有成功，返回码 {result}"
        if len(codes) == 1:
            logger.info("已订阅{} {}。报价看交易台数字，不再写入运行日志。", self._md_name(), codes[0])
            return True, f"已订阅{self._md_name()} {codes[0]}。报价看交易台上面的数字，不再写入运行日志。"
        logger.info("已订阅{} {} 个合约：{}", self._md_name(), len(codes), ", ".join(codes))
        return True, f"已订阅{self._md_name()} {len(codes)} 个合约：{', '.join(codes)}"

    def _subscribe(self, codes: str | Iterable[str]) -> int:
        """6.7 封装要合约列表和个数。单合约也走这里，只是列表长度为 1。"""
        items = [codes] if isinstance(codes, str) else list(codes)
        if not items:
            return 0
        try:
            payload = [code.encode("utf-8") for code in items]
            return int(self._api.SubscribeMarketData(payload, len(payload)) or 0)
        except TypeError:
            return int(self._api.SubscribeMarketData(items, len(items)) or 0)

    def apply_tick(self, row: dict[str, Any], replace_empty: bool = True) -> bool:
        """收下已订阅合约的一条行情，按合约分别保存。

        没有 wanted_symbols 时保持旧行为：只认 wanted 那一个。
        """
        got = str(row.get("symbol") or "").strip()
        if not got:
            return False
        key = got.upper()
        if self.wanted_symbols:
            if key not in {item.upper() for item in self.wanted_symbols}:
                return False
        elif self.wanted and got.lower() != self.wanted.lower():
            return False
        merged = merge_quote(self.latest_by_symbol.get(key), row, replace_empty=replace_empty)
        self.latest_by_symbol[key] = merged
        self.latest = merged
        now = time.time()
        self.last_tick_at = now
        self.last_tick_at_by_symbol[key] = now
        return True

    def latest_for(self, symbol: str) -> dict[str, Any] | None:
        """按合约取最近一条行情。非订阅合约返回 None，不拿别的合约顶替。"""
        code = ctp_instrument_id(symbol)
        if not code:
            return None
        return self.latest_by_symbol.get(code.upper())

    def stale_seconds_for(self, symbol: str) -> float:
        """某个合约距离上一笔行情过了几秒。"""
        code = ctp_instrument_id(symbol)
        if not code:
            return 9999.0
        at = self.last_tick_at_by_symbol.get(code.upper(), 0.0)
        if at <= 0:
            return 9999.0
        return time.time() - at

    def stale_seconds(self) -> float:
        """距离上一笔行情过了几秒。还没收到过就很大。"""
        if self.last_tick_at <= 0:
            return 9999.0
        return time.time() - self.last_tick_at

    def on_sub_result(self, symbol: str, ok: bool, message: str = "") -> None:
        """记录柜台对每个合约的订阅应答，供诊断工具判断配额与权限。"""
        code = ctp_instrument_id(symbol)
        if code:
            self.sub_status[code.upper()] = (bool(ok), str(message or ""))

    def on_connected(self) -> None:
        """行情前置连上之后登录。行情登录不需要交易那一步的认证码。"""
        mdapi = self._spi.mdapi
        field = mdapi.CThostFtdcReqUserLoginField()
        field.BrokerID = self.settings.broker_id
        field.UserID = self.settings.user_id
        # 密码只放进登录报文，不写进日志。
        field.Password = self.settings.password
        self._request_id += 1
        self._api.ReqUserLogin(field, self._request_id)

    def on_disconnected(self, reason: str) -> None:
        self.ready = False
        self._error = reason or f"{self._md_name()}断开"
        logger.warning(self._error)
        self._login_event.set()

    def on_login(self, ok: bool, message: str) -> None:
        if not ok:
            self.ready = False
            self._error = message or f"{self._md_name()}登录失败"
            logger.warning(self._error)
        else:
            self.ready = True
            self._error = ""
            logger.info("{}登录成功 {}", self._md_name(), self.front)
        self._login_event.set()

    def on_tick(self, row: dict[str, Any]) -> None:
        self.apply_tick(row)

    def close(self) -> None:
        """释放行情接口。窗口关掉时调用。"""
        api = self._api
        self._api = None
        self.ready = False
        if api is None:
            return
        try:
            api.RegisterSpi(None)
            api.Release()
        except Exception as exc:  # noqa: BLE001
            logger.warning("释放{}接口时出错: {}", self._md_name(), exc)


def _md_flow_dir(trade_flow: str) -> str:
    """行情流文件和交易流文件分开，避免两个接口抢同一个目录。"""
    path = Path(trade_flow)
    folder = path.parent / (path.name + "_md")
    folder.mkdir(parents=True, exist_ok=True)
    text = str(folder)
    if not text.endswith(("\\", "/")):
        text += os.sep
    return text


def _load_mdapi(project_root: str) -> Any:
    """先换上 ctp/vendor_libs（或旧目录 ctp/api）里的生产版动态库，再导入行情封装。"""
    prepare_ctp_dll_search(project_root or None)
    from openctp_ctp import mdapi  # type: ignore

    return mdapi


def _build_md_spi(mdapi: Any, client: CtpMdClient) -> Any:
    """行情回调。没有安装封装时不会执行到这里。"""

    class _Spi(mdapi.CThostFtdcMdSpi):  # type: ignore
        def __init__(self) -> None:
            super().__init__()
            self.mdapi = mdapi

        def OnFrontConnected(self) -> None:
            client.on_connected()

        def OnFrontDisconnected(self, reason: int) -> None:
            client.on_disconnected(f"{client._md_name()}断开，原因码 {reason}")

        def OnRspUserLogin(self, _data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _md_ok(info)
            client.on_login(ok, message)

        def OnRspSubMarketData(self, data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _md_ok(info)
            symbol = ctp_field_symbol(data)
            if symbol:
                client.on_sub_result(symbol, ok, message)
            if not ok:
                logger.warning(
                    "订阅{} {} 被拒绝: {}",
                    client._md_name(),
                    symbol or "某个合约",
                    message or "柜台没有说明原因",
                )
                return
            if symbol:
                logger.info("{}订阅已确认 {}", client._md_name(), symbol)

        def OnRtnDepthMarketData(self, data: Any) -> None:
            row = ctp_depth_row(data)
            if row is None:
                return
            client.on_tick(row)

    return _Spi()


def _md_ok(info: Any) -> tuple[bool, str]:
    """行情应答。没有错误信息就算成功。"""
    if info is None:
        return True, ""
    error_id = int(getattr(info, "ErrorID", 0) or 0)
    message = str(getattr(info, "ErrorMsg", "") or "")
    if error_id == 0:
        return True, message
    return False, message or f"错误码 {error_id}"
