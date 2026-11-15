"""
CTP 动态库加载。

功能：优先使用项目里 ctp/vendor_libs 目录中柜台指定的动态库（旧目录 ctp/api 仍可读），
再调用 Python 封装去连接交易前置。
原理：Windows 按 PATH 顺序找 dll。把柜台给的 thosttraderapi_se.dll 放在最前面，
程序就会用这一份，而不是别的机器上碰巧装过的旧库。没有库时只返回说明，不让整个程序崩溃。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from src.trader.ctp_logic import ctp_depth_row, ctp_exchange_id, ctp_field_symbol, ctp_instrument_id, keep_order_sys_id


# 新目录优先；旧目录 ctp/api 继续可读，升级时不用先搬文件。
_VENDOR_LIB_DIRS = ("ctp/vendor_libs", "ctp/api")


def vendor_libs_dir(project_root: str | Path | None) -> Path | None:
    """柜台动态库应该放在这个目录。没有目录就返回 None。"""
    if not project_root:
        return None
    for rel in _VENDOR_LIB_DIRS:
        folder = Path(project_root) / rel
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


def apply_vendor_libs(project_root: str | Path | None) -> None:
    """
    用 ctp/vendor_libs（或旧目录 ctp/api）里的动态库覆盖程序即将加载的那一份。

    柜台如果发来指定版本，文件名仍是 thosttraderapi_se.dll。
    覆盖之后再导入接口，连上的就是这一份，而不是安装包里自带的旧库。
    """
    folder = vendor_libs_dir(project_root)
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
                    logger.info("已用 {} 里的 {} 替换接口要加载的动态库", folder, name)
                except OSError as exc:
                    logger.warning("没能替换 {}：{}", target.name, exc)


def prepare_ctp_dll_search(project_root: str | Path | None) -> Path | None:
    """
    让系统先到柜台的动态库目录里找。

    返回实际加入搜索路径的目录；目录不存在则什么都不改。
    """
    apply_vendor_libs(project_root)
    folder = vendor_libs_dir(project_root)
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

    先把柜台动态库目录加进搜索路径，再导入 openctp 的 Python 封装。
    封装和 dll 要是同一套版本。柜台换了指定版本时，替换 ctp/vendor_libs 里的 dll，
    并安装对应的 Python 封装即可，业务代码不用改。
    """
    prepare_ctp_dll_search(project_root)
    try:
        from openctp_ctp import tdapi  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "没有找到 CTP 的 Python 封装。请先安装 openctp-ctp，"
            "并把柜台指定的 thosttraderapi_se.dll 放到 ctp/vendor_libs。"
        ) from exc
    return tdapi


def _fill_contract(field: Any, symbol: str, exchange: str = "") -> None:
    """
    填写合约代码和交易所。

    6.7 起原来的 InstrumentID 改名叫 reserve1，新字段才叫 InstrumentID。
    两个都写上，仿真前置无论读哪一个都能对上合约。
    """
    code = ctp_instrument_id(symbol)
    if hasattr(field, "InstrumentID"):
        field.InstrumentID = code
    if hasattr(field, "reserve1"):
        field.reserve1 = code
    place = str(exchange or "") or ctp_exchange_id(symbol)
    if place and hasattr(field, "ExchangeID"):
        field.ExchangeID = place


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
        user = str(getattr(settings, "user_id", "") or "unknown").strip() or "unknown"
        # 每个仿真账号单独一个流文件目录。混用时私有回报对不上，买开会一直「待报」。
        flow = str(Path(flow) / user)
        Path(flow).mkdir(parents=True, exist_ok=True)
        # CTP 要求流文件目录以分隔符结尾，否则会把目录名当成文件前缀。
        if not flow.endswith(("\\", "/")):
            flow = flow + os.sep
        self.api = self.tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(flow)
        # 必须把真正的 Spi 子类交给柜台，不能包一层普通对象。
        self._spi = build_spi(self.tdapi, self)
        self.api.RegisterSpi(self._spi)
        # 登录后会主动查未完成报单。私有流用「这次登录之后」，避免沿用别人账号的流文件。
        quick = getattr(self.tdapi, "THOST_TERT_QUICK", 1)
        self.api.SubscribePrivateTopic(quick)
        self.api.SubscribePublicTopic(quick)
        self.api.RegisterFront(str(settings.td_front))
        try:
            api_ver = str(self.tdapi.CThostFtdcTraderApi.GetApiVersion() or "")
        except Exception:  # noqa: BLE001
            api_ver = "未知"
        logger.info(
            "CTP 正在连接交易前置 {}，接口版本 {}，BrokerID {}，账号 {}，AppID {}",
            settings.td_front,
            api_ver,
            settings.broker_id,
            settings.user_id,
            settings.app_id,
        )
        self.api.Init()

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
        _fill_contract(field, str(order["symbol"]), str(order.get("exchange_id") or ""))
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
        req = self._next_id()
        ret = int(self.api.ReqOrderInsert(field, req) or 0)
        if ret != 0:
            raise RuntimeError(f"报单请求没发出去，返回码 {ret}")
        logger.info(
            "已向柜台申报 {} {} 手 @ {}，本地编号 {}",
            field.InstrumentID,
            field.VolumeTotalOriginal,
            field.LimitPrice,
            field.OrderRef,
        )

    def cancel(self, settings: Any, order: dict[str, Any]) -> None:
        """撤销一笔未完成报单。编号来自柜台回报，不在这里编。"""
        td = self.tdapi
        field = td.CThostFtdcInputOrderActionField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        field.UserID = settings.user_id
        # 交易所编号空着时，由合约推出 SHFE/DCE。不要再写成空字符串把交易所冲掉。
        _fill_contract(field, str(order["symbol"]), str(order.get("exchange_id") or ""))
        field.OrderRef = str(order["order_ref"] or "").strip()
        field.FrontID = int(order.get("front_id") or 0)
        field.SessionID = int(order.get("session_id") or 0)
        sys_id = keep_order_sys_id(str(order.get("order_sys_id") or ""))
        if sys_id:
            field.OrderSysID = sys_id
        field.ActionFlag = getattr(td, "THOST_FTDC_AF_Delete", "0")
        req = self._next_id()
        ret = int(self.api.ReqOrderAction(field, req) or 0)
        if ret != 0:
            raise RuntimeError(f"撤单请求没发出去，返回码 {ret}")
        logger.info(
            "发出撤单 {} 本地编号 {} 柜台号 {} Front {} Session {}",
            getattr(field, "InstrumentID", ""),
            field.OrderRef,
            sys_id.strip() if sys_id else "无",
            field.FrontID,
            field.SessionID,
        )

    def query_orders(self, settings: Any) -> None:
        """查询今天的报单，含已成交、已撤。用来填委托表，并核对还没完结的那几笔。"""
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

    def query_trades(self, settings: Any) -> None:
        """查询今天的成交，给持仓窗口对照。"""
        field = self.tdapi.CThostFtdcQryTradeField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        self.api.ReqQryTrade(field, self._next_id())

    def query_instruments(self, settings: Any, keyword: str = "") -> None:
        """按代码或前缀查合约资料。空关键字会让柜台回很多行，上层要自己限制。"""
        field = self.tdapi.CThostFtdcQryInstrumentField()
        code = str(keyword or "").strip()
        if code:
            _fill_contract(field, code)
        self.api.ReqQryInstrument(field, self._next_id())

    def query_margin_rate(self, settings: Any, symbol: str) -> None:
        """查这一个合约的保证金率。"""
        field = self.tdapi.CThostFtdcQryInstrumentMarginRateField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        _fill_contract(field, symbol)
        hedge = getattr(self.tdapi, "THOST_FTDC_HF_Speculation", "1")
        if hasattr(field, "HedgeFlag"):
            field.HedgeFlag = hedge
        self.api.ReqQryInstrumentMarginRate(field, self._next_id())

    def query_commission_rate(self, settings: Any, symbol: str) -> None:
        """查这一个合约的手续费。仿真柜台经常是 0，空着就空着。"""
        field = self.tdapi.CThostFtdcQryInstrumentCommissionRateField()
        field.BrokerID = settings.broker_id
        field.InvestorID = settings.user_id
        _fill_contract(field, symbol)
        self.api.ReqQryInstrumentCommissionRate(field, self._next_id())

    def query_depth(self, settings: Any, symbol: str) -> None:
        """
        向柜台查这一个合约今天的涨停价和跌停价。

        用行情查询，不用合约资料。合约资料里的涨跌停在这个仿真上是 0，
        当天涨跌停写在行情里。只填这一个合约，上期所要小写，例如 bu2612。
        """
        field = self.tdapi.CThostFtdcQryDepthMarketDataField()
        _fill_contract(field, symbol)
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
    必须调用基类 __init__，否则动态库收不到 OnFrontConnected。
    方法不要写类型标注，部分 CTP 封装按参数个数匹配回调。
    """

    def _call(name: str, *args: Any) -> None:
        listener = getattr(client, "listener", None)
        fn = getattr(listener, name, None) if listener is not None else None
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("CTP 回调 {} 出错: {}", name, exc)

    class _Bound(tdapi.CThostFtdcTraderSpi):  # type: ignore
        def __init__(self):
            super().__init__()

        def OnFrontConnected(self):
            logger.info("交易前置已握手")
            _call("on_connected")

        def OnFrontDisconnected(self, reason):
            _call("on_disconnected", f"交易前置断开，原因码 {reason}")

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
                str(getattr(data, "MaxOrderRef", "") or "") if data else "",
            )

        def OnRspSettlementInfoConfirm(self, _data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            client.listener.on_confirm(ok, message)

        def OnRspError(self, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            if not ok:
                logger.warning("柜台返回错误：{}", message or "没有说明原因")

        def OnRspOrderInsert(self, data: Any, info: Any, _req: int, _last: bool) -> None:
            ok, message = _rsp_ok(info)
            ref = str(getattr(data, "OrderRef", "") or "") if data is not None else ""
            if ok:
                logger.info("前置已收下报单 {}", ref or "（无编号）")
                return
            if data is None:
                logger.warning("报单被前置拒绝：{}", message or "没有说明原因")
                return
            client.listener.on_reject(ref, message)

        def OnErrRtnOrderInsert(self, data: Any, info: Any) -> None:
            _ok, message = _rsp_ok(info)
            if data is None:
                logger.warning("报单被交易所拒绝：{}", message or "没有说明原因")
                return
            client.listener.on_reject(str(getattr(data, "OrderRef", "") or ""), message)

        def OnRtnOrder(self, data: Any) -> None:
            if data is None:
                return
            logger.info(
                "收到委托回报 编号 {} 状态 {} 柜台号 {} {}",
                str(getattr(data, "OrderRef", "") or "").strip(),
                str(getattr(data, "OrderStatus", "") or ""),
                str(getattr(data, "OrderSysID", "") or "").strip(),
                str(getattr(data, "StatusMsg", "") or ""),
            )
            client.listener.on_order_status(
                str(getattr(data, "OrderRef", "") or ""),
                str(getattr(data, "OrderStatus", "") or ""),
                int(getattr(data, "VolumeTraded", 0) or 0),
                str(getattr(data, "StatusMsg", "") or ""),
                str(getattr(data, "ExchangeID", "") or ""),
                str(getattr(data, "OrderSysID", "") or ""),
                int(getattr(data, "FrontID", 0) or 0),
                int(getattr(data, "SessionID", 0) or 0),
                {
                    "symbol": str(getattr(data, "InstrumentID", "") or ""),
                    "direction": str(getattr(data, "Direction", "") or ""),
                    "offset": str(getattr(data, "CombOffsetFlag", "") or ""),
                    "price": float(getattr(data, "LimitPrice", 0) or 0),
                    "volume": int(getattr(data, "VolumeTotalOriginal", 0) or 0),
                    "insert_time": str(getattr(data, "InsertTime", "") or ""),
                    "hedge": str(getattr(data, "CombHedgeFlag", "") or ""),
                    "remaining": int(getattr(data, "VolumeTotal", 0) or 0),
                },
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
                {
                    "symbol": str(getattr(data, "InstrumentID", "") or ""),
                    "trade_id": str(getattr(data, "TradeID", "") or "").strip(),
                    "direction": str(getattr(data, "Direction", "") or ""),
                    "offset": str(getattr(data, "OffsetFlag", "") or ""),
                    "time": str(getattr(data, "TradeTime", "") or ""),
                    "exchange": str(getattr(data, "ExchangeID", "") or ""),
                    "hedge": str(getattr(data, "HedgeFlag", "") or ""),
                    "order_sys_id": str(getattr(data, "OrderSysID", "") or "").strip(),
                    "commission": float(getattr(data, "Commission", 0) or 0),
                    "close_profit": float(getattr(data, "CloseProfit", 0) or 0),
                },
            )

        def OnRspQryTrade(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = {
                    "order_ref": str(getattr(data, "OrderRef", "") or ""),
                    "symbol": str(getattr(data, "InstrumentID", "") or ""),
                    "trade_id": str(getattr(data, "TradeID", "") or "").strip(),
                    "direction": str(getattr(data, "Direction", "") or ""),
                    "offset": str(getattr(data, "OffsetFlag", "") or ""),
                    "price": float(getattr(data, "Price", 0) or 0),
                    "volume": int(getattr(data, "Volume", 0) or 0),
                    "time": str(getattr(data, "TradeTime", "") or ""),
                    "exchange": str(getattr(data, "ExchangeID", "") or ""),
                    "hedge": str(getattr(data, "HedgeFlag", "") or ""),
                    "order_sys_id": str(getattr(data, "OrderSysID", "") or "").strip(),
                    "commission": float(getattr(data, "Commission", 0) or 0),
                    "close_profit": float(getattr(data, "CloseProfit", 0) or 0),
                }
            if not ok and message:
                logger.warning("查询成交: {}", message)
            client.listener.on_query_trade(row, bool(last))

        def OnRspQryOrder(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            if data is not None and ok:
                client.listener.on_query_order(
                    str(getattr(data, "OrderRef", "") or ""),
                    str(getattr(data, "OrderStatus", "") or ""),
                    int(getattr(data, "VolumeTraded", 0) or 0),
                    str(getattr(data, "InstrumentID", "") or ""),
                    bool(last),
                    str(getattr(data, "ExchangeID", "") or ""),
                    str(getattr(data, "OrderSysID", "") or ""),
                    int(getattr(data, "FrontID", 0) or 0),
                    int(getattr(data, "SessionID", 0) or 0),
                    float(getattr(data, "LimitPrice", 0) or 0),
                    int(getattr(data, "VolumeTotalOriginal", 0) or 0),
                    str(getattr(data, "Direction", "") or ""),
                    str(getattr(data, "CombOffsetFlag", "") or ""),
                    str(getattr(data, "StatusMsg", "") or ""),
                    str(getattr(data, "InsertTime", "") or ""),
                    str(getattr(data, "CombHedgeFlag", "") or ""),
                    int(getattr(data, "VolumeTotal", 0) or 0),
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
                    # 持仓均价用开仓成本反推；浮盈柜台已经算好。没有单独的「上一笔成交价」。
                    "open_cost": float(getattr(data, "OpenCost", 0) or 0),
                    "position_profit": float(getattr(data, "PositionProfit", 0) or 0),
                    "use_margin": float(getattr(data, "UseMargin", 0) or 0),
                    "hedge": str(getattr(data, "HedgeFlag", "") or ""),
                }
            client.listener.on_position_row(row, bool(last))
            if not ok and message:
                logger.info("查询持仓: {}", message)

        def OnRspQryTradingAccount(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = {
                    # 资金账号以柜台回报为准，不用配置里手填的号去改。
                    "account_id": str(
                        getattr(data, "AccountID", "") or getattr(data, "InvestorID", "") or ""
                    ),
                    "pre_balance": float(getattr(data, "PreBalance", 0) or 0),
                    "balance": float(getattr(data, "Balance", 0) or 0),
                    "available": float(getattr(data, "Available", 0) or 0),
                    "withdraw_quota": float(getattr(data, "WithdrawQuota", 0) or 0),
                    "currency": str(getattr(data, "CurrencyID", "") or "") or "CNY",
                    "deposit": float(getattr(data, "Deposit", 0) or 0),
                    "withdraw": float(getattr(data, "Withdraw", 0) or 0),
                    "margin": float(getattr(data, "CurrMargin", 0) or 0),
                    "frozen_margin": float(getattr(data, "FrozenMargin", 0) or 0),
                    "frozen_cash": float(getattr(data, "FrozenCash", 0) or 0),
                    "commission": float(getattr(data, "Commission", 0) or 0),
                    "close_profit": float(getattr(data, "CloseProfit", 0) or 0),
                    "position_profit": float(getattr(data, "PositionProfit", 0) or 0),
                    "mortgage_in": float(getattr(data, "FundMortgageIn", 0) or 0),
                    "mortgage_out": float(getattr(data, "FundMortgageOut", 0) or 0),
                    "option_value": float(getattr(data, "SpecProductPositionProfit", 0) or 0),
                }
            client.listener.on_account(row, bool(last))
            if not ok and message:
                logger.warning("查询资金: {}", message)

        def OnRspQryInstrument(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = {
                    "symbol": str(getattr(data, "InstrumentID", "") or ""),
                    "name": str(getattr(data, "InstrumentName", "") or ""),
                    "exchange": str(getattr(data, "ExchangeID", "") or ""),
                    "product_class": str(getattr(data, "ProductClass", "") or ""),
                    "volume_multiple": int(getattr(data, "VolumeMultiple", 0) or 0),
                    "price_tick": float(getattr(data, "PriceTick", 0) or 0),
                    "open_date": str(getattr(data, "OpenDate", "") or ""),
                    "expire_date": str(getattr(data, "ExpireDate", "") or ""),
                }
            client.listener.on_contract_row(row, bool(last))
            if not ok and message:
                logger.warning("查询合约: {}", message)

        def OnRspQryInstrumentMarginRate(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = {
                    "symbol": str(getattr(data, "InstrumentID", "") or ""),
                    "long_margin_money": float(getattr(data, "LongMarginRatioByMoney", 0) or 0),
                    "short_margin_money": float(getattr(data, "ShortMarginRatioByMoney", 0) or 0),
                    "long_margin_volume": float(getattr(data, "LongMarginRatioByVolume", 0) or 0),
                    "short_margin_volume": float(getattr(data, "ShortMarginRatioByVolume", 0) or 0),
                }
            client.listener.on_margin_row(row, bool(last))
            if not ok and message:
                logger.warning("查询保证金: {}", message)

        def OnRspQryInstrumentCommissionRate(self, data: Any, info: Any, _req: int, last: bool) -> None:
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = {
                    "symbol": str(getattr(data, "InstrumentID", "") or ""),
                    "open_money": float(getattr(data, "OpenRatioByMoney", 0) or 0),
                    "open_volume": float(getattr(data, "OpenRatioByVolume", 0) or 0),
                    "close_money": float(getattr(data, "CloseRatioByMoney", 0) or 0),
                    "close_volume": float(getattr(data, "CloseRatioByVolume", 0) or 0),
                    "close_today_money": float(getattr(data, "CloseTodayRatioByMoney", 0) or 0),
                    "close_today_volume": float(getattr(data, "CloseTodayRatioByVolume", 0) or 0),
                }
            client.listener.on_commission_row(row, bool(last))
            if not ok and message:
                logger.warning("查询手续费: {}", message)

        def OnRspQryDepthMarketData(self, data: Any, info: Any, _req: int, last: bool) -> None:
            # 当天涨停价、跌停价在行情里。柜台按最后一条结束这次查询。
            ok, message = _rsp_ok(info)
            row = None
            if data is not None and ok:
                row = ctp_depth_row(data) or {"symbol": ctp_field_symbol(data)}
            elif not ok:
                logger.warning("查询涨跌停失败: {}", message or "柜台没有说明原因")
            client.listener.on_instrument(row, bool(last), "" if ok else message)

    # 必须把实例交回去。只定义类、不 return，柜台收不到回调，就会一直等握手。
    return _Bound()


def _rsp_ok(info: Any) -> tuple[bool, str]:
    """柜台应答。ErrorID 为 0 或没有应答信息，都当作成功。"""
    if info is None:
        return True, ""
    error_id = int(getattr(info, "ErrorID", 0) or 0)
    message = str(getattr(info, "ErrorMsg", "") or "")
    if error_id == 0:
        return True, message
    return False, message or f"错误码 {error_id}"
