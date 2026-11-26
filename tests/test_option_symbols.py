"""期权合约代码解析：四家交易所写法、天勤代码、CTP 代码。"""

from src.data.quote_check import product_code as quote_product
from src.data.tq_source import tq_symbol
from src.domain.options import (
    is_option,
    normalize_option_code,
    option_kind,
    option_product,
    option_strike,
    parse_option_symbol,
)
from src.trader.ctp_symbols import ctp_instrument_id


def test_parse_four_exchange_styles() -> None:
    """上期所/大商所/郑商所/中金所四种写法都要拆对。"""
    cases = {
        "rb2601C3500": ("rb", "2601", "C", 3500.0, "SHFE"),
        "m2601-C-3000": ("m", "2601", "C", 3000.0, "DCE"),
        "SR605C6000": ("sr", "2605", "C", 6000.0, "CZCE"),
        "IO2512-C-4000": ("io", "2512", "C", 4000.0, "CFFEX"),
    }
    for text, expected in cases.items():
        parsed = parse_option_symbol(text)
        assert parsed is not None, text
        assert (parsed.product, parsed.month, parsed.kind, parsed.strike, parsed.exchange) == expected


def test_put_and_lowercase() -> None:
    """看跌、大小写和小写 c/p 都要认。"""
    parsed = parse_option_symbol("ag2612p5000")
    assert parsed is not None
    assert parsed.kind == "P"
    assert parsed.strike == 5000.0
    assert option_kind("ag2612P5000") == "P"
    assert option_strike("ag2612P5000") == 5000.0
    assert option_product("ag2612P5000") == "ag"


def test_futures_are_not_options() -> None:
    """期货不能被误判成期权。"""
    for text in ("rb2601", "SR605", "IO2512", "fu2701"):
        assert not is_option(text)
    assert quote_product("rb2601C3500") == "rb"


def test_normalize_option_code_by_exchange() -> None:
    """CTP 写法：上期所无连字符，大商所/中金所带连字符，郑商所三位年月。"""
    assert normalize_option_code("rb2601C3500") == "rb2601C3500"
    assert normalize_option_code("m2601-C-3000") == "m2601-C-3000"
    assert normalize_option_code("SR2605C6000") == "SR605C6000"
    assert normalize_option_code("IO2512-C-4000") == "IO2512-C-4000"
    assert ctp_instrument_id("rb2601C3500") == "rb2601C3500"
    assert ctp_instrument_id("SR605C6000") == "SR605C6000"
    assert ctp_instrument_id("m2601-C-3000") == "m2601-C-3000"


def test_tq_symbol_option() -> None:
    """天勤代码要带交易所前缀，且保留原生写法。"""
    assert tq_symbol("rb2601C3500") == "SHFE.rb2601C3500"
    assert tq_symbol("m2601-C-3000") == "DCE.m2601-C-3000"
    assert tq_symbol("SR605C6000") == "CZCE.SR605C6000"
    assert tq_symbol("IO2512-C-4000") == "CFFEX.IO2512-C-4000"
