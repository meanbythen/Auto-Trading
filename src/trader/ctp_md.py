"""
仿真行情。

功能：连接华泰测试环境的行情前置，订阅窗口里的那一个合约，
把最新价、买一、卖一、涨停价、跌停价交给测试窗口。
原理：行情端口和交易端口是一套柜台。同花顺上的价格是另一路，不能拿来在仿真里下单。
这里不计算开仓信号，也不发报单。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from src.trader.ctp_logic import ctp_depth_row, ctp_field_symbol, ctp_instrument_id
from src.trader.ctp_td import prepare_ctp_dll_search


def market_front(td_front: str, md_front: str = "") -> str:
    """
    仿真行情地址。

    配置里写了行情前置就用配置。没写、而交易前置是华泰这份测试地址时，
    用同一台机器上的行情端口 27236。
    """
    text = str(md_front or "").strip()
    if text:
        return text
    trade = str(td_front or "").strip()
    if trade.endswith(":27226"):
        return trade[: -len("27226")] + "27236"
    return ""


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


def quote_line(row: dict[str, Any]) -> str:
    """一行说明。价格没变时调用方不要重复写日志。"""
    view = describe_quote(row)
    name = str(view.get("symbol") or "")
    return (
        f"{name} 仿真报价 最新 {view.get('last_text', '—')}"
        f" 买一 {view.get('bid_text', '—')} 卖一 {view.get('ask_text', '—')}"
        f" 涨停 {view.get('upper_text', '—')} 跌停 {view.get('lower_text', '—')}"
    )


class CtpMdClient:
    """连仿真行情前置，只订阅当前要看的那一个合约。"""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.front = market_front(
            getattr(settings, "td_front", ""),
            getattr(settings, "md_front", ""),
        )
        self.wanted = ""
        self.latest: dict[str, Any] | None = None
        self.last_tick_at = 0.0
        self.ready = False
        self._api: Any = None
        self._spi: Any = None
        self._login_event = threading.Event()
        self._error = ""
        self._request_id = 0

    def start(self) -> tuple[bool, str]:
        """连接行情前置并登录。已经登录过就直接返回。"""
        if self.ready and self._api is not None:
            return True, f"仿真行情已登录 {self.front}"
        if not self.front:
            return False, "还没有仿真行情地址"
        if not str(getattr(self.settings, "broker_id", "") or "").strip():
            return False, "还没有 BrokerID，不能登录仿真行情"
        if not str(getattr(self.settings, "user_id", "") or "").strip():
            return False, "还没有仿真账号，不能登录仿真行情"
        try:
            mdapi = _load_mdapi(getattr(self.settings, "project_root", "") or "")
        except Exception as exc:  # noqa: BLE001
            text = f"仿真行情接口没有加载成功：{exc}"
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
        logger.info("正在连接仿真行情 {}", self.front)
        timeout = float(getattr(self.settings, "login_timeout_sec", 20) or 20)
        if not self._login_event.wait(timeout):
            self.close()
            return False, "仿真行情登录超时。测试环境白天交易时段才开放"
        if not self.ready:
            reason = self._error or "仿真行情登录失败"
            self.close()
            return False, reason
        return True, f"仿真行情已登录 {self.front}"

    def subscribe(self, symbol: str) -> tuple[bool, str]:
        """只订阅这一个合约。期权代码不会拿来覆盖它。"""
        code = ctp_instrument_id(symbol)
        if not code:
            return False, "请先填写合约"
        if not self.ready or self._api is None:
            return False, "仿真行情还没登录"
        self.wanted = code
        try:
            result = self._subscribe(code)
        except Exception as exc:  # noqa: BLE001
            text = f"订阅仿真行情失败：{exc}"
            logger.warning(text)
            return False, text
        if int(result or 0) != 0:
            return False, f"订阅 {code} 没有成功，返回码 {result}"
        logger.info("已订阅仿真行情 {}", code)
        return True, f"已订阅仿真行情 {code}。报价会显示在上面，第一次收到时写进日志。"

    def _subscribe(self, code: str) -> int:
        """6.7 封装要合约列表和个数。先按字节再按字符串试一次。"""
        try:
            return int(self._api.SubscribeMarketData([code.encode("utf-8")], 1) or 0)
        except TypeError:
            return int(self._api.SubscribeMarketData([code], 1) or 0)

    def apply_tick(self, row: dict[str, Any], replace_empty: bool = True) -> bool:
        """收下和当前合约完全相同的一条。别的合约丢掉。"""
        got = str(row.get("symbol") or "").strip()
        if not got:
            return False
        if self.wanted and got.lower() != self.wanted.lower():
            return False
        self.latest = merge_quote(self.latest, row, replace_empty=replace_empty)
        self.last_tick_at = time.time()
        return True

    def stale_seconds(self) -> float:
        """距离上一笔行情过了几秒。还没收到过就很大。"""
        if self.last_tick_at <= 0:
            return 9999.0
        return time.time() - self.last_tick_at

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
        self._error = reason or "仿真行情断开"
        logger.warning(self._error)
        self._login_event.set()

    def on_login(self, ok: bool, message: str) -> None:
        if not ok:
            self.ready = False
            self._error = message or "仿真行情登录失败"
            logger.warning(self._error)
        else:
            self.ready = True
            self._error = ""
            logger.info("仿真行情登录成功 {}", self.front)
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
            logger.warning("释放仿真行情接口时出错: {}", exc)


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
    """先换上 ctp/api 里的生产版动态库，再导入行情封装。"""
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
            client.on_disconnected(f"仿真行情断开，原因码 {reason}")

        def OnRspUserLogin(self, _data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _md_ok(info)
            client.on_login(ok, message)

        def OnRspSubMarketData(self, data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _md_ok(info)
            if not ok:
                logger.warning("订阅仿真行情被拒绝: {}", message or "柜台没有说明原因")
                return
            symbol = ctp_field_symbol(data)
            if symbol:
                logger.info("仿真行情订阅已确认 {}", symbol)

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
