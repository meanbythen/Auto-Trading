"""
天勤（TqSdk）行情源。

功能：用账号密码登录天勤，取期货分钟 K 线，供策略算信号。只读行情，不下单。
原理：TqApi 登录一次后进程内复用；tqsdk 的 datetime 是 UTC 纳秒、且是 K 线「起点时间」。
      这里先加 8 小时换成北京时间，再加一个周期换成程序其它地方用的右标签
      （5 分钟 14:55 → 15:00）。还没走完的最后一根会被剔掉。
"""

from __future__ import annotations

import atexit
import threading
import time
from datetime import date
from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import canonical_period

# 品种代码在天勤属于哪家交易所。主连用 KQ.m@交易所.品种。
_EXCHANGES = {
    "SHFE": "cu al zn pb ni sn au ag rb wr hc ss bu ru sp fu ao br ad op".split(),
    "INE": "sc lu nr bc ec".split(),
    "DCE": "a b m y p c cs jd lh l v pp j jm i eg eb pg rr fb bb bz lg".split(),
    "CZCE": "sr cf ta ma fg rm oi sa pf ur sm sf ap cj pk cy sh px pl pr".split(),
    "CFFEX": "if ih ic im t tf ts tl".split(),
    "GFEX": "si lc ps pt pd".split(),
}
EXCHANGE_BY_PRODUCT = {
    product: exchange for exchange, products in _EXCHANGES.items() for product in products
}

# 各交易所开盘时刻（分钟数）。K 线是右标签（结束时刻），正好落在开盘上的不可能存在。
COMMODITY_OPEN_MINUTES = (9 * 60, 13 * 60 + 30, 21 * 60)  # 商品：09:00 / 13:30 / 21:00
CFFEX_OPEN_MINUTES = (9 * 60 + 30, 13 * 60)  # 中金所：09:30 / 13:00，没有夜盘


def session_open_minutes(symbol: str) -> tuple[int, ...]:
    """这个合约所在交易时段的开盘时刻。中金所下午 13:30 是正常收盘时刻，不能按商品那套算。"""
    text = str(symbol or "").split("@")[0]
    product = "".join(ch for ch in text if ch.isalpha()).lower()
    if EXCHANGE_BY_PRODUCT.get(product) == "CFFEX":
        return CFFEX_OPEN_MINUTES
    return COMMODITY_OPEN_MINUTES


# 分钟周期 → 秒。日线/周线暂不提供：tqsdk 日线的时间戳口径与新浪不一致，混用会把日期错开。
_MINUTE_SECONDS = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600}

# TqApi 不是线程安全的。实盘扫描和回测可能同时来拉，排队用同一个连接。
_LOCK = threading.RLock()
_api: Any | None = None
_api_key: tuple[str, str] | None = None


def period_seconds(period: str) -> int:
    """把程序里的周期换成 tqsdk 要的秒数。日线/周线不支持。"""
    code = canonical_period(period)
    if code in _MINUTE_SECONDS:
        return _MINUTE_SECONDS[code]
    if code.isdigit() and int(code) > 0:
        return int(code) * 60
    raise ValueError(f"天勤没有 {period} 这一档，只支持分钟线（1/5/15/30/60 或自定义分钟）")


def tq_symbol(symbol: str) -> str:
    """
    把配置里的合约换成天勤代码。

    fu2701 → SHFE.fu2701；只写品种 bu → KQ.m@SHFE.bu（主连）。
    CZCE 的合约是 3 位年月，配置里常写 4 位（SR2701 → CZCE.SR701）。
    """
    text = str(symbol or "").strip()
    if not text:
        raise ValueError("合约名是空的")
    if "." in text:
        exchange, code = text.split(".", 1)
        exchange = exchange.upper()
        return f"{exchange}.{_code_for(exchange, ''.join(ch for ch in code if ch.isalpha()).lower(), ''.join(ch for ch in code if ch.isdigit()) or code)}"

    product = "".join(ch for ch in text if ch.isalpha()).lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    exchange = EXCHANGE_BY_PRODUCT.get(product)
    if not exchange:
        raise ValueError(f"还不知道 {symbol} 在天勤属于哪家交易所")
    if not digits:
        return f"KQ.m@{exchange}.{_short_code(exchange, product)}"
    return f"{exchange}.{_code_for(exchange, product, digits)}"


def _short_code(exchange: str, product: str) -> str:
    """交易所内品种代码的大小写：郑商所、中金所大写，其余小写。"""
    return product.upper() if exchange in {"CZCE", "CFFEX"} else product.lower()


def _code_for(exchange: str, product: str, digits: str) -> str:
    """拼具体合约。郑商所只保留年月后三位。"""
    number = str(digits)
    if exchange == "CZCE" and len(number) == 4:
        number = number[-3:]
    return f"{_short_code(exchange, product)}{number}"


def config_symbol_from_tq(instrument: str, *, today: date | None = None) -> str:
    """
    天勤合约写法换回配置写法：SHFE.fu2701 → fu2701，CZCE.TA701 → TA2701。

    郑商所是三位年月（7 表示 2027 年、01 表示 1 月），配置里习惯写四位。
    按今天的日期补十年位：已经过去的年份算到下一个十年。
    """
    text = str(instrument or "").strip()
    if "." not in text:
        return ""
    exchange, contract = text.split(".", 1)
    exchange = exchange.upper()
    product = "".join(ch for ch in contract if ch.isalpha()).lower()
    digits = "".join(ch for ch in contract if ch.isdigit())
    if not product or not digits:
        return ""
    if exchange == "CZCE" and len(digits) == 3:
        now = today or date.today()
        year = now.year - now.year % 10 + int(digits[0])
        if year < now.year:
            year += 10
        digits = f"{year % 100:02d}{digits[1:]}"
    return _short_code(exchange, product) + digits


def _pick_cont(found: Any, exchange: str, code: str) -> str:
    """从 query_cont_quotes 的结果里挑出这一品种的主力标的。"""
    for item in found or []:
        text = str(item or "").strip()
        if "." not in text:
            continue
        ex, contract = text.split(".", 1)
        product = "".join(ch for ch in contract if ch.isalpha()).lower()
        if ex.upper() == exchange.upper() and product == code.lower():
            return text
    return ""


def _quote_main_underlying(client: Any, exchange: str, code: str) -> str:
    """问主连行情：KQ.m@SHFE.fu 现在挂着哪个具体合约。"""
    try:
        quote = client.get_quote(f"KQ.m@{exchange}.{code}")
    except Exception:  # noqa: BLE001
        return ""
    deadline = time.time() + 5
    while not str(getattr(quote, "underlying_symbol", "") or ""):
        if time.time() >= deadline:
            break
        try:
            _wait_update(client, min(time.time() + 1, deadline))
        except Exception:  # noqa: BLE001
            break
    return str(getattr(quote, "underlying_symbol", "") or "")


def main_contract(
    symbol: str,
    *,
    user: str = "",
    password: str = "",
    api: Any | None = None,
) -> str:
    """
    该品种当前的主力合约（配置写法，例如 fu2701 / TA2701）。

    主力不是交易所指定的：天勤的主连按成交量和持仓量挑最活跃的合约，
    所以切换发生在交易日盘中/日终，不会固定在每月 1 日。这里直接问题库里的主连标的。
    """
    text = str(symbol or "").strip()
    if "." in text:
        exchange, contract = text.split(".", 1)
        exchange = exchange.upper()
        product = "".join(ch for ch in contract if ch.isalpha()).lower()
    else:
        product = "".join(ch for ch in text if ch.isalpha()).lower()
        exchange = EXCHANGE_BY_PRODUCT.get(product, "")
    if not product or not exchange:
        raise ValueError(f"还不知道 {symbol} 属于哪家交易所")

    code = _short_code(exchange, product)
    with _LOCK:
        client = api if api is not None else _get_api(user, password)
        underlying = ""
        for product_id in (code, code.lower()):
            try:
                found = client.query_cont_quotes(exchange_id=exchange, product_id=product_id)
            except Exception:  # noqa: BLE001
                continue
            underlying = _pick_cont(found, exchange, code)
            if underlying:
                break
        if not underlying:
            underlying = _quote_main_underlying(client, exchange, code)

    result = config_symbol_from_tq(underlying)
    if not result:
        raise RuntimeError(f"天勤没有给出 {symbol} 的主力合约")
    return result


def reset_api() -> None:
    """断线或换账号后丢掉旧连接，下次重新登录。"""
    global _api, _api_key
    with _LOCK:
        api, _api, _api_key = _api, None, None
    if api is not None:
        try:
            api.close()
        except Exception:  # noqa: BLE001
            logger.debug("天勤旧连接关闭时出错，已忽略")


def _get_api(user: str, password: str) -> Any:
    """登录一次，后面复用。账号或密码变了就重连。"""
    global _api, _api_key
    user = str(user or "").strip()
    password = str(password or "").strip()
    if not user or not password:
        raise RuntimeError(
            "天勤需要账号和密码：请填 data.tq.user，密码放 config/secrets.local.yaml 的 tq_password"
        )
    if _api is not None and _api_key == (user, password):
        return _api
    reset_api()
    try:
        from tqsdk import TqApi, TqAuth

        _api = TqApi(auth=TqAuth(user, password))
        _api_key = (user, password)
    except Exception as exc:  # noqa: BLE001
        _api, _api_key = None, None
        raise RuntimeError(f"天勤登录失败：{exc}") from exc
    logger.info("天勤已登录 | {}", user)
    return _api


def frame_from_klines(raw: Any, seconds: int) -> pd.DataFrame:
    """
    把 tqsdk 的 K 线表收成和程序其它地方一样的开高低收量。

    tqsdk 的 datetime 是 UTC 纳秒、且是起点时间。先加 8 小时换成北京时间，
    再加一个周期变成右标签（这一段的结束时刻）。

    最后一根可能还在走，照样留着，不在这里删：持仓时它要用来看盘中影线，
    空仓和回测只认已经收盘的那些。删不删由调用方判断，两种行情源口径一致。
    """
    if raw is None or len(raw) == 0:
        raise RuntimeError("天勤没有返回K线")
    columns = {str(name).lower(): name for name in raw.columns}
    for need in ("datetime", "open", "high", "low", "close", "volume"):
        if need not in columns:
            raise RuntimeError(f"天勤K线缺少 {need} 列")
    times = pd.to_datetime(pd.Series(raw[columns["datetime"]]).astype("int64"), unit="ns")
    frame = pd.DataFrame(
        {
            "open": pd.to_numeric(raw[columns["open"]], errors="coerce").astype(float),
            "high": pd.to_numeric(raw[columns["high"]], errors="coerce").astype(float),
            "low": pd.to_numeric(raw[columns["low"]], errors="coerce").astype(float),
            "close": pd.to_numeric(raw[columns["close"]], errors="coerce").astype(float),
            "volume": pd.to_numeric(raw[columns["volume"]], errors="coerce").astype(float),
        }
    )
    # tqsdk 的时间戳是 UTC；程序里统一用不带时区的北京时间。
    beijing = pd.DatetimeIndex(times).floor("s") + pd.Timedelta(hours=8)
    frame.index = beijing + pd.Timedelta(seconds=int(seconds))
    frame.index.name = "datetime"
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    return frame


def _filled_close_count(klines: Any) -> int | None:
    """close 列已经填了多少行。读不出结构时返回 None，调用方按老逻辑只等一次。"""
    node = klines
    for _ in range(4):
        cols = getattr(node, "columns", None)
        if cols is not None and "close" in cols:
            try:
                return int(node["close"].notna().sum())
            except Exception:  # noqa: BLE001
                return None
        try:
            node = next(iter(node.values())) if isinstance(node, dict) else node["klines"]
        except Exception:  # noqa: BLE001
            return None
    return None


def _wait_update(client: Any, deadline: float) -> None:
    """老版本 tqsdk 没有 deadline 参数。"""
    try:
        client.wait_update(deadline=deadline)
    except TypeError:
        client.wait_update()


def fetch_frame(
    symbol: str,
    period: str,
    *,
    limit: int = 200,
    user: str = "",
    password: str = "",
    api: Any | None = None,
) -> pd.DataFrame:
    """向天勤要这一段 K 线。测试可以传入假的 api，不必真的登录。"""
    seconds = period_seconds(period)
    code = tq_symbol(symbol)
    # 天勤服务端自己有上限（免费账号实测约 10000 根，要多了不报错、按上限截），
    # 所以这里不再设帽子：调用方要多少就传多少，给多少算多少。
    size = max(10, int(limit))
    with _LOCK:
        client = api if api is not None else _get_api(user, password)
        try:
            klines = client.get_kline_serial(code, seconds, data_length=size)
            # 等服务端把历史填满再转换：服务端分批下发，只等一次可能拿到半截数据。
            # 填满到请求根数就提前走；更早的历史没有了时填不满，最多等 15 秒。
            filled = _filled_close_count(klines)
            if filled is None:
                _wait_update(client, time.time() + 3)
            else:
                deadline = time.time() + 15
                while filled < size and time.time() < deadline:
                    _wait_update(client, min(time.time() + 1, deadline))
                    filled = _filled_close_count(klines) or filled
                if filled < size:
                    logger.warning(
                        "天勤K线只填满 {}/{} 根，按现有数据继续（更早的历史可能没有了）",
                        filled,
                        size,
                    )
        except Exception:
            # 连接断了就丢掉，下一轮重新登录，不要拿着死连接一直重试。
            if api is None:
                reset_api()
            raise
        frame = frame_from_klines(klines, seconds)
    logger.info("天勤K线 {} {} {} 根", code, period, len(frame))
    return frame


# 进程退出时主动关闭天勤连接，消除退出时一片 "Event loop is closed" 的 asyncio 噪音。
# reset_api 里判断过 _api 是不是 None，没登录过时这步什么都不做。
atexit.register(reset_api)
