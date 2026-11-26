"""期权合约代码解析（全项目唯一入口）。

功能：把国内四家交易所的期权写法统一拆成
      标的品种、年月、看涨/看跌、行权价、交易所。
原理：期货代码是「字母 + 数字」，现有代码习惯把字母全留下当品种；
      期权代码里还有 C/P 和行权价，必须先按期权规则解析，不能沿用期货写法。

支持写法：
    rb2601C3500      上期所
    m2601-C-3000     大商所
    SR605C6000       郑商所（3 位年月）
    IO2512-C-4000    中金所
也接受带交易所前缀的写法：SHFE.rb2601C3500。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 期权：可选「交易所.」+ 品种字母 + 3/4 位年月 + 可选连字符 + C/P + 可选连字符 + 行权价
_OPTION_PATTERN = re.compile(
    r"^(?:(?P<exchange>[A-Za-z]+)\.)?"
    r"(?P<product>[A-Za-z]+)"
    r"(?P<month>\d{3,4})-?"
    r"(?P<kind>[CP])-?"
    r"(?P<strike>\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)

# 交易所对期权代码的书写格式在 normalize_option_code() 里按交易所分支处理。


@dataclass(frozen=True)
class OptionSymbol:
    """一个期权合约拆出来的静态信息。"""

    raw: str
    product: str  # 标的品种代码，小写；rb2601C3500 → rb
    month: str  # 四位年月；SR605 → 2605
    kind: str  # C = 看涨，P = 看跌
    strike: float
    exchange: str  # SHFE / DCE / CZCE / CFFEX / GFEX / INE，认不出时为空


def _normalize_month(month: str) -> str:
    """郑商所 3 位年月补成 4 位；2605 原样返回。"""
    text = str(month or "")
    if len(text) == 3:
        return "2" + text
    return text


def _exchange_for(symbol: str, product: str, explicit: str = "") -> str:
    """先看写法里带的交易所，再按品种前缀查共用归属表。"""
    if explicit:
        return explicit.upper()
    from src.domain.exchanges import exchange_for_product

    return exchange_for_product(product)


def parse_option_symbol(symbol: str) -> OptionSymbol | None:
    """解析期权代码；不是期权（或格式不认识）返回 None。"""
    text = str(symbol or "").strip()
    if not text:
        return None
    match = _OPTION_PATTERN.match(text)
    if not match:
        return None
    product = match.group("product").lower()
    month = _normalize_month(match.group("month"))
    kind = match.group("kind").upper()
    try:
        strike = float(match.group("strike"))
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None
    return OptionSymbol(
        raw=text,
        product=product,
        month=month,
        kind=kind,
        strike=strike,
        exchange=_exchange_for(text, product, match.group("exchange") or ""),
    )


def is_option(symbol: str) -> bool:
    """是不是期权合约代码。"""
    return parse_option_symbol(symbol) is not None


def option_product(symbol: str) -> str:
    """标的品种代码；不是期权时退回「只留字母」的旧写法。"""
    parsed = parse_option_symbol(symbol)
    if parsed is not None:
        return parsed.product
    return "".join(ch for ch in str(symbol or "").strip().lower() if "a" <= ch <= "z")


def option_kind(symbol: str) -> str:
    """C / P；不是期权返回空串。"""
    parsed = parse_option_symbol(symbol)
    return parsed.kind if parsed is not None else ""


def option_strike(symbol: str) -> float:
    """行权价；不是期权返回 0。"""
    parsed = parse_option_symbol(symbol)
    return parsed.strike if parsed is not None else 0.0


def option_expiry(symbol: str) -> str:
    """合约年月（四位 YYMM）；不是期权返回空串。

    注意：这只是「哪个月到期」，不是最后交易日。
    最后交易日必须来自品种与合约表的「到期日」列。
    """
    parsed = parse_option_symbol(symbol)
    return parsed.month if parsed is not None else ""


def option_exchange(symbol: str) -> str:
    """交易所代码；认不出返回空串。"""
    parsed = parse_option_symbol(symbol)
    return parsed.exchange if parsed is not None else ""


def _strike_text(value: float) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


def normalize_option_code(symbol: str) -> str:
    """按交易所规范重写期权代码（CTP / 天勤共用）。

    上期所、能源中心：rb2601C3500（品种小写，C/P 大写，无连字符）
    大商所、广期所：  m2601-C-3000
    郑商所：          SR605C6000
    中金所：          IO2512-C-4000
    认不出交易所时原样返回（去掉首尾空格）。
    """
    parsed = parse_option_symbol(symbol)
    if parsed is None:
        return str(symbol or "").strip()
    strike_text = _strike_text(parsed.strike)
    month_text = parsed.month
    if parsed.exchange == "CZCE":
        # 郑商所期权年月是 3 位：SR605C6000。
        if len(month_text) == 4 and month_text.startswith("2"):
            month_text = month_text[1:]
        return f"{parsed.product.upper()}{month_text}{parsed.kind}{strike_text}"
    if parsed.exchange == "CFFEX":
        return f"{parsed.product.upper()}{month_text}-{parsed.kind}-{strike_text}"
    if parsed.exchange in {"DCE", "GFEX"}:
        return f"{parsed.product.lower()}{month_text}-{parsed.kind}-{strike_text}"
    if parsed.exchange in {"SHFE", "INE"}:
        return f"{parsed.product.lower()}{month_text}{parsed.kind}{strike_text}"
    # 认不出交易所：保持原写法，交给上层报错。
    return str(symbol or "").strip()
