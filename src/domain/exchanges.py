"""品种代码 → 交易所（全项目共用的纯数据）。

功能：行情（天勤）、柜台（CTP）和期权解析都用同一份品种归属表，避免各写一份后对不上。
原理：只放静态映射，不导入任何业务模块，因此不会产生循环依赖。
"""

from __future__ import annotations

_EXCHANGE_PRODUCTS: dict[str, str] = {
    "SHFE": (
        "cu al zn pb ni sn au ag rb hc ss bu ru sp fu wr ao br ad op"
    ),
    "INE": "sc nr lu bc ec",
    "DCE": "a b c cs eb eg i j jm l m p pg pp v y lh jd fb bb rr lg bz",
    "CZCE": (
        "ap cf cj cy fg jr lr ma oi pf pk pl pm pr px ri rm rs sa sf sh sm "
        "sr ta ur wh zc"
    ),
    "CFFEX": "if ih ic im io ho mo t tf ts tl",
    "GFEX": "si lc ps pt pd",
}

EXCHANGE_BY_PRODUCT: dict[str, str] = {
    product.upper(): exchange
    for exchange, products in _EXCHANGE_PRODUCTS.items()
    for product in products.split()
}


def exchange_for_product(product: str) -> str:
    """品种代码 → 交易所；认不出返回空串。"""
    return EXCHANGE_BY_PRODUCT.get(str(product or "").strip().upper(), "")
