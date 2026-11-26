"""CTP 合约代码与交易所映射。纯函数，供生产链路和独立仿真柜台共用。"""

from __future__ import annotations

# 品种归属表和行情、期权解析共用一份，避免三处各写一份后对不上。
from src.domain.exchanges import EXCHANGE_BY_PRODUCT as _CTP_EXCHANGE_BY_PRODUCT


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


def ctp_exchange_id(symbol: str) -> str:
    """bu2612 -> SHFE，SA2701 -> CZCE。不认识的品种留空。"""
    return _CTP_EXCHANGE_BY_PRODUCT.get(product_code(symbol).upper(), "")


def ctp_instrument_id(symbol: str) -> str:
    """按交易所规范合约代码大小写，避免柜台因代码格式拒单。

    期权不能整串大写或小写：上期所/大商所的品种小写、C/P 大写，
    郑商所/中金所整串大写，大商所/广期所/中金所还带连字符。
    """
    text = str(symbol or "").strip()
    from src.domain.options import is_option, normalize_option_code

    if is_option(text):
        return normalize_option_code(text)
    exchange = ctp_exchange_id(text)
    if exchange in {"CZCE", "CFFEX"}:
        return text.upper()
    return text.lower()
