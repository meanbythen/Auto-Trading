"""
天勤（TqSdk）行情源。

功能：用账号密码登录天勤，取期货分钟 / 小时 / 日线，供策略算信号。只读行情，不下单。
原理：TqApi 登录一次后进程内复用；tqsdk 的 datetime 是 UTC 纳秒、且是 K 线「起点时间」。
      分钟、小时：先加 8 小时换成北京时间，再加一个周期换成右标签（5 分钟 14:55 → 15:00）。
      日线：官方就是交易日 00:00，只换北京时间，不再加 86400 秒，否则会错成第二天。
      周线：免费接口不能直接要大于 1 天的周期，用日线按周五收合成周线。
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from datetime import date, datetime
from typing import Any

import pandas as pd
from loguru import logger

from src.data.periods import canonical_period, trading_minutes_between
from src.domain.exchanges import EXCHANGE_BY_PRODUCT as _EXCHANGE_BY_PRODUCT

# 品种代码在天勤属于哪家交易所。主连用 KQ.m@交易所.品种。
# 归属表与柜台、期权解析共用一份，这里只把小写键留给原有调用点。
EXCHANGE_BY_PRODUCT = {
    product.lower(): exchange for product, exchange in _EXCHANGE_BY_PRODUCT.items()
}

# 各交易所开盘时刻（分钟数）。K 线是右标签（结束时刻），正好落在开盘上的不可能存在。
# 商品开盘（右标签不可能落在这里）：早盘、上午休息后、午盘、夜盘。
COMMODITY_OPEN_MINUTES = (9 * 60, 10 * 60 + 30, 13 * 60 + 30, 21 * 60)
CFFEX_OPEN_MINUTES = (9 * 60 + 30, 13 * 60)  # 中金所：09:30 / 13:00，没有夜盘


def session_open_minutes(symbol: str) -> tuple[int, ...]:
    """这个合约所在交易时段的开盘时刻。中金所下午 13:30 是正常收盘时刻，不能按商品那套算。"""
    from src.domain.options import option_product

    text = str(symbol or "").split("@")[0]
    product = option_product(text)
    if EXCHANGE_BY_PRODUCT.get(product) == "CFFEX":
        return CFFEX_OPEN_MINUTES
    return COMMODITY_OPEN_MINUTES


# 能直接问天勤的周期（秒）。日线是 86400。周线不在这里：免费接口上限一天，要自己用日线合成。
_MINUTE_SECONDS = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600}
_DAY_SECONDS = 86400

# TqApi 不是线程安全的。实盘扫描和回测可能同时来拉，排队用同一个连接。
_LOCK = threading.RLock()
_api: Any | None = None
_api_key: tuple[str, str] | None = None

# 天勤免费账号单档 K 线上限；超过会被服务端静默截断且 while filled<size 恒成立等满 15 秒。
# 夹取到上限就好，要更多请自己日线合成。
_TQ_MAX_SIZE = 10_000
# 分批下发时 close 连续几次没增加，就当作更早的历史没有了，不必空等到 15 秒。
_HISTORY_STALL_WAITS = 3
# 每轮取 K 线都必须推进一次 tqsdk 事件循环：K 线序列只有被推进才会刷新。
# 行情活跃时 wait_update 立即返回；完全没消息时才等满这个时间（秒）。
_PUMP_SECONDS = 0.5
# (天勤合约, 周期秒数) → 已经确认填不满时的根数。下一轮不要再空等、也不要再刷同一条告警。
_HISTORY_SHORT: dict[tuple[str, int], int] = {}
_HISTORY_SHORT_WARNED: set[tuple[str, int, int, int]] = set()

# 序列活性：(天勤合约, 周期秒) → (最后一根K线时间, 第一次看到这个时间点的时刻)。
# 盘内超过两个周期没前进就重建连接，否则旧序列会一直僵在同一个时间上。
_LIVENESS: dict[tuple[str, int], tuple[datetime, datetime]] = {}
_LIVENESS_WARNED: set[tuple[str, int]] = set()


def period_seconds(period: str) -> int:
    """
    把程序里的周期换成 tqsdk 要的秒数。

    日线 = 86400。周线请走 fetch_frame，不要直接要 604800（免费版上限一天）。
    自定义分钟只要不超过一天都可以问。
    """
    code = canonical_period(period)
    if code == "D":
        return _DAY_SECONDS
    if code == "W":
        raise ValueError("天勤没有原生周线，请用日线合成（fetch_frame 会自动做）")
    if code in _MINUTE_SECONDS:
        return _MINUTE_SECONDS[code]
    if code.isdigit() and int(code) > 0:
        seconds = int(code) * 60
        if seconds > _DAY_SECONDS:
            raise ValueError(f"天勤免费接口周期最多 1 天，不能直接要 {period}")
        return seconds
    raise ValueError(f"天勤没有 {period} 这一档，支持分钟/小时/日线；周线用日线合成")


def tq_symbol(symbol: str) -> str:
    """
    把配置里的合约换成天勤代码。

    fu2701 → SHFE.fu2701；只写品种 bu → KQ.m@SHFE.bu（主连）。
    CZCE 的合约是 3 位年月，配置里常写 4 位（SR2701 → CZCE.SR701）。
    期权按交易所原生写法直出：rb2601C3500 → SHFE.rb2601C3500。
    """
    text = str(symbol or "").strip()
    if not text:
        raise ValueError("合约名是空的")
    from src.domain.options import is_option, normalize_option_code, option_exchange

    if is_option(text):
        exchange = option_exchange(text)
        if not exchange:
            raise ValueError(f"还不知道期权 {symbol} 属于哪家交易所")
        return f"{exchange}.{normalize_option_code(text)}"
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


def _finite_number(value: Any) -> float:
    """行情里空值常是 NaN，当成 0。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num:  # NaN
        return 0.0
    return num


def instrument_specs(
    symbol: str,
    *,
    user: str = "",
    password: str = "",
    api: Any | None = None,
    timeout: float = 5.0,
) -> dict[str, float]:
    """
    问天勤这一张合约的每手数量、每跳、最新价。

    天勤行情没有期货公司的保证金比例，比例请在品种表手填，或从 CTP 合约页抄。
    每手数量就是 VolumeMultiple，和盈亏乘数是同一个数。
    """
    code = tq_symbol(symbol)
    with _LOCK:
        client = api if api is not None else _get_api(user, password)
        try:
            quote = client.get_quote(code)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"天勤没有 {symbol} 的合约资料：{exc}") from exc
        deadline = time.time() + max(1.0, float(timeout))
        volume_multiple = 0
        while time.time() < deadline:
            volume_multiple = int(_finite_number(getattr(quote, "volume_multiple", 0)) or 0)
            if volume_multiple > 0:
                break
            try:
                _wait_update(client, min(time.time() + 1.0, deadline))
            except Exception:  # noqa: BLE001
                break
        return {
            "volume_multiple": float(volume_multiple),
            "price_tick": _finite_number(getattr(quote, "price_tick", 0)),
            "last_price": _finite_number(getattr(quote, "last_price", 0)),
        }


def _clear_history_short() -> None:
    """清掉「历史不够 200 根」的记忆。断线重连或测试开始时用。"""
    _HISTORY_SHORT.clear()
    _HISTORY_SHORT_WARNED.clear()


def _clear_liveness() -> None:
    """清掉序列活性的观测基准。测试开始时用，运行中不要清。"""
    _LIVENESS.clear()
    _LIVENESS_WARNED.clear()


def _liveness_frozen(
    symbol: str,
    code: str,
    seconds: int,
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """
    盘内最后一根K线超过两个周期没前进，返回这根的时间；否则返回 None。

    只在交易时段累计：午休、收盘后、周末的等待不算，避免把正常休市误判成僵死。
    第一次看到某根K线只记基准；之后它一直不动、且盘内已经走过两个周期，才判死。
    """
    if frame is None or len(frame) == 0:
        return None
    try:
        last = pd.Timestamp(frame.index[-1]).to_pydatetime()
    except Exception:  # noqa: BLE001
        return None
    if getattr(last, "tzinfo", None) is not None:
        last = last.replace(tzinfo=None)
    current = now or datetime.now()
    key = (code, int(seconds))
    seen = _LIVENESS.get(key)
    if seen is None or last > seen[0]:
        _LIVENESS[key] = (last, current)
        _LIVENESS_WARNED.discard(key)
        return None
    if trading_minutes_between(seen[1], current, symbol) < 2.0 * (int(seconds) / 60.0):
        return None
    return last


def _login_disabled() -> bool:
    """测试/自检进程禁止登录真实天勤账号：同账号并发登录会顶掉实盘行情连接。"""
    return os.environ.get("AUTO_TRADING_NO_TQ_LOGIN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def reset_api() -> None:
    """断线或换账号后丢掉旧连接，下次重新登录。"""
    global _api, _api_key
    with _LOCK:
        api, _api, _api_key = _api, None, None
        _clear_history_short()
    if api is None:
        return

    def _close() -> None:
        try:
            api.close()
        except BaseException:  # noqa: BLE001
            # 退出阶段可能被 Ctrl-C / 事件循环销毁打断，任何异常都吞掉，
            # 不让它在 atexit 回调里喷 traceback。
            logger.debug("天勤旧连接关闭时出错，已忽略")

    # api.close() 内部要跑事件循环，可能因为网络/事件循环状态卡住。
    # 放到后台线程，给 3 秒，到点进程继续退出，不阻塞关闭流程。
    worker = threading.Thread(target=_close, name="TqApiClose", daemon=True)
    worker.start()
    worker.join(timeout=3.0)
    if worker.is_alive():
        logger.debug("天勤连接关闭超时，进程退出时丢弃")


def _get_api(user: str, password: str) -> Any:
    """登录一次，后面复用。账号或密码变了就重连。"""
    global _api, _api_key
    user = str(user or "").strip()
    password = str(password or "").strip()
    if not user or not password:
        raise RuntimeError(
            "天勤需要账号和密码：请填 data.tq.user，密码放 config/secrets.local.yaml 的 tq_password"
        )
    if _login_disabled():
        raise RuntimeError(
            "AUTO_TRADING_NO_TQ_LOGIN 已开启：测试/自检不登录天勤，避免顶掉实盘行情连接"
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

    tqsdk 的 datetime 是 UTC 纳秒、且是起点时间。先加 8 小时换成北京时间。

    分钟、小时再加一个周期变成右标签（这一段的结束时刻）。
    日线官方就是交易日 00:00，不能再加 86400 秒，否则整段会错成第二天，
    多因子用日线定方向时会对不上分钟线所属的交易日。

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
    # 天勤 K 线自带持仓量，字段叫 close_oi（这根结束时的持仓）。因子挖掘要用它，
    # 这里一并收进 oi 列；没有这个字段的行情源仍按原来的价量处理。
    oi_column = columns.get("close_oi") or columns.get("open_oi") or columns.get("open_interest")
    if oi_column:
        frame["oi"] = pd.to_numeric(raw[oi_column], errors="coerce").astype(float)
    # tqsdk 的时间戳是 UTC；程序里统一用不带时区的北京时间。
    # 显式指定 unit="s"，避免 numpy 把「整数 + Timedelta」当成 generic unit（未来会变错误）。
    beijing = pd.DatetimeIndex(times).floor("s") + pd.Timedelta(hours=8)
    if int(seconds) >= _DAY_SECONDS:
        frame.index = beijing.normalize()
    else:
        frame.index = beijing + pd.Timedelta(seconds=int(seconds))
    frame.index.name = "datetime"
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    from src.data.periods import canonicalize_ohlc

    # 和新浪、同花顺图走同一套：无时区北京时间、秒级、列名统一。
    period = "D" if int(seconds) >= _DAY_SECONDS else None
    out = canonicalize_ohlc(frame, period=period)
    return out if out is not None else frame


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


def _pump_once(client: Any) -> None:
    """
    每轮至少推进一次事件循环。

    tqsdk 的 K 线序列是被动的：只有 wait_update 处理过消息，序列才会刷新。
    这件事必须由取数入口每轮无条件做，不能挂在「历史还没填满」这种条件上：
    历史一填满就不会再刷新，最后一根 K 线会一直停住，只能靠僵死检测重建来治标。
    """
    _wait_update(client, time.time() + _PUMP_SECONDS)


def _client_serials(client: Any) -> dict[tuple[str, int], tuple[int, Any]]:
    """每个天勤连接自己的 K 线序列缓存。重连换了对象，旧序列自然丢掉。"""
    try:
        cache = getattr(client, "_at_kline_serials", None)
        if cache is None:
            cache = {}
            setattr(client, "_at_kline_serials", cache)
        return cache
    except Exception:  # noqa: BLE001
        # 极少数封装不允许挂属性，退化成每次都新建，不影响结果。
        return {}


def _fill_history(
    client: Any,
    codes: dict[str, str],
    serials: dict[str, Any],
    seconds: int,
    size: int,
) -> None:
    """等这些序列的历史填满。多个合约共用同一次 wait_update。

    调用方（`fetch_frames`）每轮已经先推进过一次事件循环；这里只管历史没满时多等。
    """
    filled: dict[str, int] = {}
    unknown = False
    for symbol in codes:
        count = _filled_close_count(serials[symbol])
        if count is None:
            unknown = True
            filled[symbol] = 0
        else:
            filled[symbol] = int(count)
    if unknown:
        _wait_update(client, time.time() + 3)
        for symbol in codes:
            count = _filled_close_count(serials[symbol])
            if count is not None:
                filled[symbol] = int(count)
    skip: set[str] = set()
    for symbol, code in codes.items():
        known_short = _HISTORY_SHORT.get((code, seconds))
        if known_short is not None and filled[symbol] >= known_short and filled[symbol] < size:
            skip.add(symbol)
    stalled = {symbol: 0 for symbol in codes}
    deadline = time.time() + 15
    while time.time() < deadline:
        active = [symbol for symbol in codes if symbol not in skip and filled[symbol] < size]
        if not active:
            break
        previous = {symbol: filled[symbol] for symbol in active}
        _wait_update(client, min(time.time() + 1, deadline))
        for symbol in active:
            count = _filled_close_count(serials[symbol])
            if count is None:
                count = previous[symbol]
            if int(count) <= previous[symbol]:
                stalled[symbol] += 1
                filled[symbol] = int(count)
                if stalled[symbol] >= _HISTORY_STALL_WAITS:
                    skip.add(symbol)
            else:
                stalled[symbol] = 0
                filled[symbol] = int(count)
    for symbol, code in codes.items():
        hist_key = (code, seconds)
        count = filled[symbol]
        if count < size:
            _HISTORY_SHORT[hist_key] = count
            warn_key = (code, seconds, count, size)
            if warn_key not in _HISTORY_SHORT_WARNED:
                _HISTORY_SHORT_WARNED.add(warn_key)
                logger.warning(
                    "天勤K线 {} 只填满 {}/{} 根，按现有数据继续（更早的历史可能没有了）",
                    code,
                    count,
                    size,
                )
        else:
            _HISTORY_SHORT.pop(hist_key, None)


def fetch_frames(
    symbols: Any,
    period: str,
    *,
    limit: int = 200,
    user: str = "",
    password: str = "",
    api: Any | None = None,
) -> dict[str, pd.DataFrame]:
    """向天勤要多个合约同一周期的 K 线，单合约就是列表里只有一个元素。

    多个合约共用一次 wait_update；序列按 (天勤代码, 周期) 复用，
    不在每一轮重新创建，避免扫描多品种时序列数量不断累积。
    """
    wanted: list[str] = []
    for item in symbols:
        text = str(item or "").strip()
        if text and text not in wanted:
            wanted.append(text)
    if not wanted:
        return {}
    code_period = canonical_period(period)
    if code_period == "W":
        # 免费接口不能直接要一周秒数。先拿日线，再收到周五，和新浪周线同一套收法。
        daily = fetch_frames(
            wanted,
            "D",
            limit=max(int(limit), 80),
            user=user,
            password=password,
            api=api,
        )
        from src.data.periods import resample_ohlc

        weeks: dict[str, pd.DataFrame] = {}
        for symbol in wanted:
            frame = daily.get(symbol)
            if frame is None:
                continue
            week = resample_ohlc(frame, "W")
            if week is None or len(week) == 0:
                if len(wanted) == 1:
                    raise RuntimeError("天勤日线合成周线后没有K线")
                logger.warning("天勤周线合成失败，跳过 {}", symbol)
                continue
            logger.info("天勤周线 {} 由日线合成 {} 根（日线 {} 根）", symbol, len(week), len(frame))
            weeks[symbol] = week
        return weeks
    seconds = period_seconds(period)
    # 天勤服务端自己有上限（免费账号实测约 10000 根），超过会被静默截断；
    # 夹取到上限，否则 while filled < size 会恒成立、等满 15 秒超时。
    size = max(10, min(int(limit), _TQ_MAX_SIZE))
    codes = {symbol: tq_symbol(symbol) for symbol in wanted}
    # 登录不持 _LOCK：_get_api 要联网握手，回测首次登录时不能把实盘扫描挡在锁外。
    client = api if api is not None else _get_api(user, password)
    with _LOCK:
        try:
            cache = _client_serials(client)
            serials: dict[str, Any] = {}
            for symbol, code in codes.items():
                key = (code, seconds)
                cached = cache.get(key)
                if cached is None or cached[0] < size:
                    klines = client.get_kline_serial(code, seconds, data_length=size)
                    cache[key] = (size, klines)
                else:
                    klines = cached[1]
                serials[symbol] = klines
            # 每轮先推进一次事件循环：K 线序列只有被推进才会刷新，
            # 这件事和「历史是否已经填满」无关，必须无条件做。
            _pump_once(client)
            # 历史没满（服务端分批下发）时再多等几轮。
            _fill_history(client, codes, serials, seconds, size)
        except Exception:
            # 连接断了就丢掉，下一轮重新登录，不要拿着死连接一直重试。
            if api is None:
                reset_api()
            raise
        frames: dict[str, pd.DataFrame] = {}
        for symbol in wanted:
            try:
                frames[symbol] = frame_from_klines(serials[symbol], seconds)
            except Exception:
                if len(wanted) == 1:
                    raise
                logger.warning("天勤K线 {} 没有可用数据，跳过", codes[symbol])
        for symbol, frame in frames.items():
            logger.info("天勤K线 {} {} {} 根", codes[symbol], period, len(frame))
        # 盘内最后一根连续两个周期没前进，说明这条连接里的序列已经僵死，重建后下轮重新取。
        frozen: list[tuple[tuple[str, int], datetime]] = []
        for symbol, frame in frames.items():
            stopped = _liveness_frozen(symbol, codes[symbol], seconds, frame)
            if stopped is None:
                continue
            key = (codes[symbol], seconds)
            if key not in _LIVENESS_WARNED:
                _LIVENESS_WARNED.add(key)
                logger.warning(
                    "天勤K线 {} {} 最后一根停在 {}，盘内已超过两个周期没有前进，重建行情序列",
                    codes[symbol],
                    period,
                    stopped,
                )
            frozen.append((key, stopped))
    if frozen and api is None:
        reset_api()
        # 重建后重新计时，避免下一轮立刻又判死；告警标记保留到序列真的前进。
        resumed = datetime.now()
        for key, stopped in frozen:
            _LIVENESS[key] = (stopped, resumed)
    return frames


def fetch_frame(
    symbol: str,
    period: str,
    *,
    limit: int = 200,
    user: str = "",
    password: str = "",
    api: Any | None = None,
) -> pd.DataFrame:
    """向天勤要这一段 K 线。单合约走 fetch_frames，只是列表里只有一个。"""
    frames = fetch_frames(
        [symbol],
        period,
        limit=limit,
        user=user,
        password=password,
        api=api,
    )
    if symbol in frames:
        return frames[symbol]
    raise RuntimeError(f"天勤没有返回 {symbol} 的K线")


# 进程退出时主动关闭天勤连接，消除退出时一片 "Event loop is closed" 的 asyncio 噪音。
# reset_api 里判断过 _api 是不是 None，没登录过时这步什么都不做。
def _reset_api_at_exit() -> None:
    """退出阶段可能被 Ctrl-C 打断；atexit 里吞掉异常，不再往控制台喷 traceback。"""
    try:
        reset_api()
    except BaseException:  # noqa: BLE001
        logger.debug("退出时关闭天勤连接被打断，已忽略")


atexit.register(_reset_api_at_exit)
