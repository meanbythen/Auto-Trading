"""
CTP 动态库加载。

功能：优先使用项目里 ctp/api 目录中华泰指定的动态库，再调用 Python 封装去连接交易前置。
原理：Windows 按 PATH 顺序找 dll。把华泰给的 thosttraderapi_se.dll 放在最前面，
程序就会用这一份，而不是别的机器上碰巧装过的旧库。没有库时只返回说明，不让整个程序崩溃。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from src.trader.ctp_logic import ctp_exchange_id, ctp_instrument_id


def ctp_api_dir(project_root: str | Path | None) -> Path | None:
    """华泰动态库应该放在这个目录。没有目录就返回 None。"""
    if not project_root:
        return None
    folder = Path(project_root) / "ctp" / "api"
    if folder.is_dir():
        return folder
    return None


def _ctp_libs_dirs() -> list[Path]:
    """openctp 实际加载的动态库目录。文件名带一串校验，不能只靠 PATH。"""
    found: list[Path] = []
    seeds = list(sys.path)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        seeds.append(meipass)
    for seed in seeds:
        folder = Path(seed) / "openctp_ctp.libs"
        if folder.is_dir() and folder not in found:
            found.append(folder)
    return found


def apply_huatai_dlls(project_root: str | Path | None) -> None:
    """
    用 ctp/api 里的动态库覆盖程序即将加载的那一份。

    华泰如果发来指定版本，文件名仍是 thosttraderapi_se.dll。
    覆盖之后再导入接口，连上的就是这一份，而不是安装包里自带的旧库。
    """
    folder = ctp_api_dir(project_root)
    if folder is None:
        return
    names = ("thosttraderapi_se.dll", "thostmduserapi_se.dll")
    for name in names:
        source = folder / name
        if not source.is_file():
            continue
        prefix = source.stem
        payload = source.read_bytes()
        for libs in _ctp_libs_dirs():
            for target in libs.glob(prefix + "-*.dll"):
                try:
                    if target.is_file() and target.read_bytes() == payload:
                        continue
                    shutil.copyfile(source, target)
                    logger.info("已用 ctp/api 里的 {} 替换接口要加载的动态库", name)
                except OSError as exc:
                    logger.warning("没能替换 {}：{}", target.name, exc)


def prepare_ctp_dll_search(project_root: str | Path | None) -> Path | None:
    """
    让系统先到 ctp/api 里找动态库。

    返回实际加入搜索路径的目录；目录不存在则什么都不改。
    """
    apply_huatai_dlls(project_root)
    folder = ctp_api_dir(project_root)
    if folder is None:
        return None
    folder_text = str(folder)
    if hasattr(os, "add_dll_directory"):
        # Python 3.8 以后，Windows 不再用 PATH 加载扩展模块旁边的 dll，必须单独登记。
        os.add_dll_directory(folder_text)
    os.environ["PATH"] = folder_text + os.pathsep + os.environ.get("PATH", "")
    return folder


def load_tdapi(project_root: str | Path | None) -> Any:
    """
    导入交易接口模块。

    先把华泰动态库目录加进搜索路径，再导入 openctp 的 Python 封装。
    封装和 dll 要是同一套版本。华泰明天如果指定了别的版本，替换 ctp/api 里的 dll，
    并安装对应的 Python 封装即可，业务代码不用改。
    """
    prepare_ctp_dll_search(project_root)
    try:
        from openctp_ctp import tdapi  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "没有找到 CTP 的 Python 封装。请先安装 openctp-ctp，"
            "并把华泰指定的 thosttraderapi_se.dll 放到 ctp/api。"
        ) from exc
    return tdapi


class OpenctpClient:
    """
    真正收发 CTP 报文的一层。

    只做字段填写和回调转发。登录顺序、今昨仓、对账都在别的文件里。
    """

    def __init__(self) -> None:
        self.api: Any = None
        self.tdapi: Any = None
        self.listener: Any = None
        self._request_id = 0
        self._spi: Any = None

    def start(self, settings: Any, listener: Any) -> None:
        """创建接口、登记回调、连接交易前置。同一条连接只调用一次。"""
        self.listener = listener
        root = getattr(settings, "project_root", "") or ""
        self.tdapi = load_tdapi(root)
        flow = str(settings.flow_dir or "data/ctp_flow")
        Path(flow).mkdir(parents=True, exist_ok=True)
        # CTP 要求流文件目录以分隔符结尾，否则会把目录名当成文件前缀。
        if not flow.endswith(("\\", "/")):
            flow = flow + os.sep
        self.api = self.tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(flow)
        # 必须把真正的 Spi 子类交给柜台，不能包一层普通对象。
        self._spi = build_spi(self.tdapi, self)
        self.api.RegisterSpi(self._spi)
        resume = getattr(self.tdapi, "THOST_TERT_RESUME", 2)
        # 从上次断开的地方继续收私有流。重连后才能知道刚才那笔到底成没成。
        self.api.SubscribePrivateTopic(resume)
        self.api.SubscribePublicTopic(resume)
        self.api.RegisterFront(str(settings.td_front))
        self.api.Init()
        logger.info("CTP 正在连接交易前置 {}", settings.td_front)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def authenticate(self, settings: Any) -> None:
        """穿透式认证：BrokerID、资金账号、AppID、认证码。"""
        field = self.tdapi.CThostFtdcReqAuthenticateField()
        field.BrokerID = settings.broker_id
        field.UserID = settings.user_id
        field.AppID = settings.app_id
        field.AuthCode = settings.auth_code
        self.api.ReqAuthenticate(field, self._next_id())

    def login(self, settings: Any) -> None:
        """登录交易前置。密码只放进报文，不写日志。"""
        field = self.tdapi.CThostFtdcReqUserLoginField()
        field.BrokerID = settings.broker_id
        field.UserID = settings.user_id
        field.Password = settings.password
        self.api.ReqUserLogin(field, self._next_id())

    def confirm(self, settings: Any) -> None:
        """确认结算单。当天不确认，柜台不接受报单。"""
        field = self.tdapi.CThostFtdcSettlementInfoConfirmField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        self.api.ReqSettlementInfoConfirm(field, self._next_id())

    def insert(self, settings: Any, order: dict[str, Any]) -> None:
        """限价报单。方向、开平、手数、价格都由上层算好。"""
        td = self.tdapi
        field = td.CThostFtdcInputOrderField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        field.UserID = settings.user_id
        # 上期所、大商所要小写。这里若写成 BU2612，柜台会回「找不到合约」。
        field.InstrumentID = ctp_instrument_id(str(order["symbol"]))
        exchange = str(order.get("exchange_id") or "") or ctp_exchange_id(str(order["symbol"]))
        if exchange and hasattr(field, "ExchangeID"):
            field.ExchangeID = exchange
        field.OrderRef = str(order["order_ref"])
        field.Direction = str(order["direction"])
        field.CombOffsetFlag = str(order["offset"])
        field.CombHedgeFlag = getattr(td, "THOST_FTDC_HF_Speculation", "1")
        field.OrderPriceType = getattr(td, "THOST_FTDC_OPT_LimitPrice", "2")
        field.LimitPrice = float(order["price"])
        field.VolumeTotalOriginal = int(order["volume"])
        field.TimeCondition = getattr(td, "THOST_FTDC_TC_GFD", "3")
        field.VolumeCondition = getattr(td, "THOST_FTDC_VC_AV", "1")
        field.MinVolume = 1
        field.ContingentCondition = getattr(td, "THOST_FTDC_CC_Immediately", "1")
        field.ForceCloseReason = getattr(td, "THOST_FTDC_FCC_NotForceClose", "0")
        field.IsAutoSuspend = 0
        field.UserForceClose = 0
        self.api.ReqOrderInsert(field, self._next_id())

    def cancel(self, settings: Any, order: dict[str, Any]) -> None:
        """撤销一笔未完成报单。编号来自柜台回报，不在这里编。"""
        td = self.tdapi
        field = td.CThostFtdcInputOrderActionField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        field.UserID = settings.user_id
        field.InstrumentID = ctp_instrument_id(str(order["symbol"]))
        field.OrderRef = str(order["order_ref"])
        field.FrontID = int(order.get("front_id") or 0)
        field.SessionID = int(order.get("session_id") or 0)
        field.ExchangeID = str(order.get("exchange_id") or "")
        field.OrderSysID = str(order.get("order_sys_id") or "")
        field.ActionFlag = getattr(td, "THOST_FTDC_AF_Delete", "0")
        self.api.ReqOrderAction(field, self._next_id())

    def query_orders(self, settings: Any) -> None:
        """查询今天的报单，用来核对断线前那一笔。"""
        field = self.tdapi.CThostFtdcQryOrderField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        self.api.ReqQryOrder(field, self._next_id())

    def query_positions(self, settings: Any) -> None:
        """查询投资者持仓。"""
        field = self.tdapi.CThostFtdcQryInvestorPositionField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        self.api.ReqQryInvestorPosition(field, self._next_id())

    def query_account(self, settings: Any) -> None:
        """查询可用资金。"""
        field = self.tdapi.CThostFtdcQryTradingAccountField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        self.api.ReqQryTradingAccount(field, self._next_id())

    def query_depth(self, settings: Any, symbol: str) -> None:
        """
        向柜台查这一个合约今天的涨停价和跌停价。

        用行情查询，不用合约资料。合约资料里的涨跌停在这个仿真上是 0，
        当天涨跌停写在行情里。只填这一个合约，上期所要小写，例如 bu2612。
        """
        field = self.tdapi.CThostFtdcQryDepthMarketDataField()
        field.InstrumentID = ctp_instrument_id(symbol)
        exchange = ctp_exchange_id(symbol)
        if exchange and hasattr(field, "ExchangeID"):
            field.ExchangeID = exchange
        self.api.ReqQryDepthMarketData(field, self._next_id())

    def release(self) -> None:
        """退出时释放接口。"""
        api = self.api
        self.api = None
        if api is not None:
            try:
                api.RegisterSpi(None)
                api.Release()
            except Exception as exc:  # noqa: BLE001
                logger.warning("释放 CTP 接口时出错: {}", exc)


def build_spi(tdapi: Any, client: OpenctpClient) -> Any:
    """
    生成柜台回调对象。

    类写在函数里面，是为了没有安装 CTP 封装时，导入本文件也不会失败。
    回调里只转给 listener，不在这里做开平判断。
    """

    class _Bound(tdapi.CThostFtdcTraderSpi):  # type: ignore
        def OnFrontConnected(self) -> None:
            client.listener.on_connected()

        def OnFrontDisconnected(self, reason: int) -> None:
            client.listener.on_disconnected(f"交易前置断开，原因码 {reason}")

        def OnRspAuthenticate(self, _data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            client.listener.on_auth(ok, message)

        def OnRspUserLogin(self, data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            client.listener.on_login(
                ok,
                int(getattr(data, "FrontID", 0) or 0) if data else 0,
                int(getattr(data, "SessionID", 0) or 0) if data else 0,
                str(getattr(data, "TradingDay", "") or "") if data else "",
                message,
            )

        def OnRspSettlementInfoConfirm(self, _data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            client.listener.on_confirm(ok, message)

        def OnRspOrderInsert(self, data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            if ok or data is None:
                return
            client.listener.on_reject(str(getattr(data, "OrderRef", "") or ""), message)

        def OnErrRtnOrderInsert(self, data: Any, info: Any) -> None:
            _ok, message = _rsp_ok(info)
            if data is None:
                return
            client.listener.on_reject(str(getattr(data, "OrderRef", "") or ""), message)

        def OnRtnOrder(self, data: Any) -> None:
            if data is None:
                return
            client.listener.on_order_status(
                str(getattr(data, "OrderRef", "") or ""),
                str(getattr(data, "OrderStatus", "") or ""),
                int(getattr(data, "VolumeTraded", 0) or 0),
                str(getattr(data, "StatusMsg", "") or ""),
                str(getattr(data, "ExchangeID", "") or ""),
                str(getattr(data, "OrderSysID", "") or ""),
                int(getattr(data, "FrontID", 0) or 0),
                int(getattr(data, "SessionID", 0) or 0),
            )

        def OnRspOrderAction(self, data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            if ok or data is None:
                return
            client.listener.on_cancel_error(str(getattr(data, "OrderRef", "") or ""), message)

        def OnErrRtnOrderAction(self, data: Any, info: Any) -> None:
            _ok, message = _rsp_ok(info)
            ref = getattr(data, "OrderRef", "") if data is not None else ""
            client.listener.on_cancel_error(str(ref or ""), message)

        def OnRtnTrade(self, data: Any) -> None:
            if data is None:
                return
            client.listener.on_trade(
                str(getattr(data, "OrderRef", "") or ""),
                float(getattr(data, "Price", 0) or 0),
                int(getattr(data, "Volume", 0) or 0),
            )

        def OnRspQryOrder(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            if data is not None and ok:
                client.listener.on_query_order(
                    str(getattr(data, "OrderRef", "") or ""),
                    str(getattr(data, "OrderStatus", "") or ""),
                    int(getattr(data, "VolumeTraded", 0) or 0),
                    str(getattr(data, "InstrumentID", "") or ""),
                    bool(last),
                )
            elif last:
                client.listener.on_query_order("", "", 0, "", True)
            if not ok and message:
                logger.warning("查询报单: {}", message)

        def OnRspQryInvestorPosition(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = {
                    "symbol": getattr(data, "InstrumentID", ""),
                    "exchange": getattr(data, "ExchangeID", ""),
                    "direction": str(getattr(data, "PosiDirection", "") or ""),
                    "position": int(getattr(data, "Position", 0) or 0),
                    "today": int(getattr(data, "TodayPosition", 0) or 0),
                    "yesterday": int(getattr(data, "YdPosition", 0) or 0),
                }
            client.listener.on_position_row(row, bool(last))
            if not ok and message:
                logger.info("查询持仓: {}", message)

        def OnRspQryTradingAccount(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            available = None
            if data is not None and ok:
                available = float(getattr(data, "Available", 0) or 0)
            client.listener.on_account(available, bool(last))
            if not ok and message:
                logger.warning("查询资金: {}", message)

        def OnRspQryDepthMarketData(self, data: Any, info: Any, _req: int, last: bool) -> None:
            # 当天涨停价、跌停价在行情里。柜台按最后一条结束这次查询。
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = {
                    "symbol": str(getattr(data, "InstrumentID", "") or ""),
                    "last": float(getattr(data, "LastPrice", 0) or 0),
                    "bid": float(getattr(data, "BidPrice1", 0) or 0),
                    "ask": float(getattr(data, "AskPrice1", 0) or 0),
                    "bid_volume": int(getattr(data, "BidVolume1", 0) or 0),
                    "ask_volume": int(getattr(data, "AskVolume1", 0) or 0),
                    "upper": float(getattr(data, "UpperLimitPrice", 0) or 0),
                    "lower": float(getattr(data, "LowerLimitPrice", 0) or 0),
                    "time": str(getattr(data, "UpdateTime", "") or ""),
                }
            elif not ok:
                logger.warning("查询涨跌停失败: {}", message or "柜台没有说明原因")
            client.listener.on_instrument(row, bool(last), "" if ok else message)


def _rsp_ok(info: Any) -> tuple[bool, str]:
    """柜台应答。ErrorID 为 0 或没有应答信息，都当作成功。"""
    if info is None:
        return True, ""
    error_id = int(getattr(info, "ErrorID", 0) or 0)
    message = str(getattr(info, "ErrorMsg", "") or "")
    if error_id == 0:
        return True, message
    return False, message or f"错误码 {error_id}"
