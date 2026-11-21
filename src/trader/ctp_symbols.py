"""CTP 合约代码与交易所映射。纯函数，供生产链路和独立仿真柜台共用。"""

from __future__ import annotations


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
    """bu2612 -> SHFE，SA2701 -> CZCE。不认识的品种留空。"""
    return _CTP_EXCHANGE_BY_PRODUCT.get(product_code(symbol).upper(), "")


def ctp_instrument_id(symbol: str) -> str:
    """按交易所规范合约代码大小写，避免柜台因代码格式拒单。"""
    text = str(symbol or "").strip()
    exchange = ctp_exchange_id(text)
    if exchange in {"CZCE", "CFFEX"}:
        return text.upper()
    return text.lower()
