"""
行情来源。

一次只问一家。开仓前不再额外对另一家，避免下单前多等一次网络。

实盘只保留两家：主源天勤（tq），备源新浪（akshare）。
回测还可选同花顺图（ths，读图会抢鼠标，不能扫描下单）和 tick 合成
（本地用天勤 tick 拼出来的 K 线，不是另一家行情商）。

东方财富已经下线：旧配置里的 eastmoney /「东方财富」会改成主源天勤；
备源若写了东方财富会改成新浪。电脑里残留的 @eastmoney 库存不会自动删，
可在数据管理里按「不限」删掉。
"""

from __future__ import annotations

# 界面上的名字，和配置文件里的代码。
# 实盘下拉只有天勤、新浪。同花顺图、tick 合成只出现在历史回测/数据管理。
SOURCE_LABELS = ("天勤", "新浪")
BACKTEST_SOURCE_LABELS = ("天勤", "新浪", "同花顺图", "tick合成")
DEFAULT_SOURCE = "tq"
DEFAULT_FALLBACK = "akshare"

_CODE_BY_LABEL = {
    "新浪": "akshare",
    "天勤": "tq",
    "同花顺图": "ths",
    "tick合成": "tick",
}
_LABEL_BY_CODE = {
    "akshare": "新浪",
    "sina": "新浪",
    "tq": "天勤",
    "tqsdk": "天勤",
    "ths": "同花顺图",
    "tick": "tick合成",
}
# 已下线，不再拉网。配置迁移见 normalize_source / normalize_fallback。
_RETIRED_EASTMONEY = {"eastmoney", "east", "em", "东方财富"}


def normalize_source(value: str | None) -> str:
    """
    把界面上的「天勤」或配置里的 tq 收成程序用的代码。

    旧配置写 akshare 仍表示新浪。写 eastmoney / 东方财富的，改成主源天勤，
    避免下拉消失后还去连东财。
    """
    text = str(value or "").strip()
    if text in _CODE_BY_LABEL:
        return _CODE_BY_LABEL[text]
    lowered = text.lower()
    if text in _RETIRED_EASTMONEY or lowered in _RETIRED_EASTMONEY:
        return DEFAULT_SOURCE
    if lowered in {"tq", "tqsdk", "tianqin", "shinny"}:
        return "tq"
    if lowered in {"ths", "tonghuashun", "同花顺", "同花顺图"}:
        return "ths"
    if lowered in {"tick", "tickbars", "tick合成", "tick_bars"}:
        return "tick"
    if lowered in {"csv", "demo"}:
        return lowered
    if lowered in {"akshare", "sina", "新浪"}:
        return "akshare"
    if not text:
        return DEFAULT_SOURCE
    return "akshare"


def normalize_fallback(value: str | None) -> str:
    """
    天勤失败后的备用源。只允许新浪。

    配成天勤、同花顺图、tick 或已下线的东方财富时，一律改成新浪，避免回退又绕回主源。
    """
    text = str(value or "").strip()
    if text in _RETIRED_EASTMONEY or text.lower() in _RETIRED_EASTMONEY:
        return DEFAULT_FALLBACK
    code = normalize_source(text)
    if code != "akshare":
        return DEFAULT_FALLBACK
    return code


def source_label(value: str | None) -> str:
    """配置里的代码换成下拉框上的中文。"""
    code = normalize_source(value)
    return _LABEL_BY_CODE.get(code, "天勤")


def stored_source_label(code: str | None) -> str:
    """
    库存文件上的来源名。东方财富残留仍显示「已停用」，不要误标成天勤。
    """
    text = str(code or "").strip().lower()
    if text in {"eastmoney", "east", "em"}:
        return "东方财富(已停用)"
    return source_label(code)


def is_tq_source(value: str | None) -> bool:
    """现在是不是在用天勤拉行情。只有这时才登录天勤账号。"""
    return normalize_source(value) == "tq"


def is_ths_source(value: str | None) -> bool:
    """是不是从同花顺主图信息框读 K 线。只给历史回测。"""
    return normalize_source(value) == "ths"


def is_tick_source(value: str | None) -> bool:
    """是不是读本地 tick 合成 K 线。只读、不联网，只给历史回测。"""
    return normalize_source(value) == "tick"


def is_live_source(value: str | None) -> bool:
    """实盘扫描能不能用这一家。同花顺图和 tick 合成不行。"""
    return normalize_source(value) in {"tq", "akshare"}


def storage_symbol(symbol: str, source: str | None) -> str:
    """
    本地库存用的合约名。

    新浪仍是 bu2612，已经存下的文件不用搬家。
    天勤写成 fu2701@tq，同花顺图写成 fu2701@ths，tick 合成写成 fu2701@tick。
    各家不会叠在同一根 K 线上。
    """
    code = normalize_source(source)
    if code == "tq":
        return f"{symbol}@tq"
    if code == "ths":
        return f"{symbol}@ths"
    if code == "tick":
        return f"{symbol}@tick"
    return str(symbol)


def product_code(symbol: str) -> str:
    """bu2612 → bu。只留字母，用来判断是哪家交易所。"""
    return "".join(ch for ch in str(symbol or "") if ch.isalpha()).lower()
