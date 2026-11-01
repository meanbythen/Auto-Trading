"""
行情来源。

一次只问一家。开仓前不再额外对另一家，避免下单前多等一次网络。
新浪仍用原来的代码 akshare。东方财富用 eastmoney。天勤用 tq。
三家的 K 线分开存放，换来源时不会把旧的那一家盖在同一根时间上。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import requests

# 界面上的名字，和配置文件里的代码。
SOURCE_LABELS = ("新浪", "东方财富", "天勤")
_CODE_BY_LABEL = {"新浪": "akshare", "东方财富": "eastmoney", "天勤": "tq"}
_LABEL_BY_CODE = {
    "akshare": "新浪",
    "sina": "新浪",
    "eastmoney": "东方财富",
    "tq": "天勤",
    "tqsdk": "天勤",
}

# 东方财富期货市场编号。只用来找合约。
_MARKET = {
    "cu": "113", "al": "113", "zn": "113", "pb": "113", "ni": "113", "sn": "113",
    "au": "113", "ag": "113", "rb": "113", "wr": "113", "hc": "113", "ss": "113",
    "bu": "113", "ru": "113", "sp": "113", "fu": "113", "ao": "113", "br": "113",
    "a": "114", "b": "114", "m": "114", "y": "114", "p": "114", "c": "114",
    "cs": "114", "jd": "114", "lh": "114", "l": "114", "v": "114", "pp": "114",
    "j": "114", "jm": "114", "i": "114", "eg": "114", "eb": "114", "pg": "114",
    "rr": "114", "fb": "114", "bb": "114",
    "sr": "115", "cf": "115", "ta": "115", "ma": "115", "fg": "115", "rm": "115",
    "oi": "115", "sa": "115", "pf": "115", "ur": "115", "sm": "115", "sf": "115",
    "ap": "115", "cj": "115", "pk": "115", "cy": "115", "sh": "115", "px": "115",
    "if": "220", "ih": "220", "ic": "220", "im": "220", "t": "220", "tf": "220",
    "ts": "220", "tl": "220",
    "sc": "142", "lu": "142", "nr": "142", "bc": "142", "ec": "142",
    "si": "225", "lc": "225",
}

# 东方财富的周期编号。5 分钟就是 5，日线是 101，周线是 102。
_KLT = {"1": "1", "5": "5", "15": "15", "30": "30", "60": "60", "D": "101", "W": "102"}
_HTTP_TIMEOUT = (4, 8)
# 主地址经常把连接掐断。这几个是同一家的备用地址，连上一个就用它。
_EASTMONEY_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://48.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://7.push2his.eastmoney.com/api/qt/stock/kline/get",
)
_EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
    "Connection": "close",
}
# 记住刚才连上的地址，下一轮先问它。
_last_eastmoney_url = ""


def normalize_source(value: str | None) -> str:
    """
    把界面上的「新浪」或配置里的 akshare 收成程序用的代码。

    旧配置写 akshare，仍然表示新浪，不用改文件也能接着用。
    """
    text = str(value or "").strip()
    if text in _CODE_BY_LABEL:
        return _CODE_BY_LABEL[text]
    lowered = text.lower()
    if lowered in {"eastmoney", "east", "em"}:
        return "eastmoney"
    if lowered in {"tq", "tqsdk", "tianqin", "shinny"}:
        return "tq"
    if lowered in {"csv", "demo"}:
        return lowered
    return "akshare"


def source_label(value: str | None) -> str:
    """配置里的代码换成下拉框上的中文。"""
    code = normalize_source(value)
    return _LABEL_BY_CODE.get(code, "新浪")


def storage_symbol(symbol: str, source: str | None) -> str:
    """
    本地库存用的合约名。

    新浪仍是 bu2612，已经存下的文件不用搬家。
    东方财富写成 bu2612@eastmoney，天勤写成 fu2701@tq，各家不会叠在同一根 K 线上。
    """
    code = normalize_source(source)
    if code == "eastmoney":
        return f"{symbol}@eastmoney"
    if code == "tq":
        return f"{symbol}@tq"
    return str(symbol)


def product_code(symbol: str) -> str:
    """bu2612 → bu。只留字母，用来判断是哪家交易所。"""
    return "".join(ch for ch in str(symbol or "") if ch.isalpha()).lower()


def eastmoney_secid(symbol: str) -> str:
    """
    东方财富要的合约编号。

    沥青 2612 是 113.bu2612。113 表示上期所，后面是合约本身。
    """
    product = product_code(symbol)
    market = _MARKET.get(product)
    if not market:
        raise ValueError(f"还不知道 {symbol} 在东方财富属于哪家交易所")
    code = str(symbol or "").strip()
    if market in {"115", "220"}:
        code = code.upper()
    else:
        code = code.lower()
    return f"{market}.{code}"


def parse_eastmoney_bar(line: str) -> dict[str, Any]:
    """
    拆东方财富的一行 K 线。

    顺序是：时间、开盘、收盘、最高、最低、成交量。
    11:10 那一行是 4720,4725,4729,4720,1197，也就是开 4720、收 4725、高 4729、低 4720。
    """
    parts = str(line or "").split(",")
    if len(parts) < 6:
        raise ValueError("东方财富这一行不完整")
    return {
        "time": pd.Timestamp(parts[0]),
        "open": float(parts[1]),
        "close": float(parts[2]),
        "high": float(parts[3]),
        "low": float(parts[4]),
        "volume": float(parts[5]),
    }


def frame_from_eastmoney_lines(lines: list[str]) -> pd.DataFrame:
    """把东方财富返回的多行收成和程序其他地方一样的开高低收表。"""
    rows = [parse_eastmoney_bar(line) for line in lines]
    if not rows:
        raise RuntimeError("东方财富没有返回K线")
    frame = pd.DataFrame(rows).set_index("time").sort_index()
    frame.index.name = "datetime"
    return frame[["open", "high", "low", "close", "volume"]]


def fetch_eastmoney_frame(symbol: str, period: str, *, limit: int = 200, getter=None) -> pd.DataFrame:
    """
    向东方财富要这一档 K 线。只读行情，不下单。

    直接用它给出的 5 分钟、15 分钟，不再拿 1 分钟重拼。
    主地址经常把连接掐断，所以换几个备用地址再试。连上过的地址下一轮先用。
    测试可以传入 getter，这样就不必真的上网。
    """
    global _last_eastmoney_url

    klt = _KLT.get(str(period))
    if not klt:
        raise ValueError(f"东方财富没有 {period} 这一档")
    urls = list(_EASTMONEY_URLS)
    if _last_eastmoney_url in urls:
        urls.remove(_last_eastmoney_url)
        urls.insert(0, _last_eastmoney_url)
    # 根数太大时，对方更容易把连接掐断。先按要的根数试，不行再少要一点。
    limits = [str(max(20, int(limit)))]
    if int(limit) > 200:
        limits.append("200")
    last_error: Exception | None = None
    for url in urls:
        for size in limits:
            try:
                lines = _read_eastmoney_lines(url, eastmoney_secid(symbol), klt, size, getter)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            if not lines:
                last_error = RuntimeError("东方财富没有返回K线")
                continue
            _last_eastmoney_url = url
            return frame_from_eastmoney_lines(lines)
    raise RuntimeError("东方财富这次把连接掐断了，没有拿到K线") from last_error


def _read_eastmoney_lines(url: str, secid: str, klt: str, limit: str, getter) -> list[str]:
    """问一个地址要 K 线。每次新开连接，避免用到刚才已经被对方关掉的旧连接。"""
    params = {
        "secid": secid,
        "klt": klt,
        "fqt": "1",
        "lmt": limit,
        "end": "20500101",
        "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "forcect": "1",
    }
    if getter is not None:
        response = getter(url, params=params, headers=_EASTMONEY_HEADERS, timeout=_HTTP_TIMEOUT)
    else:
        with requests.Session() as session:
            response = session.get(url, params=params, headers=_EASTMONEY_HEADERS, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return list(((payload or {}).get("data") or {}).get("klines") or [])
