"""CTP 合约代码与交易所映射的单元测试（白盒，纯函数）。"""

from __future__ import annotations

import pytest

from src.trader.ctp_symbols import (
    ctp_exchange_id,
    ctp_instrument_id,
    product_code,
    symbol_key,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bu2612", "bu"),
        ("BU2612", "bu"),
        (" c2511 ", "c"),
        ("cu2601", "cu"),
        ("SA2701", "sa"),
        ("IF2601", "if"),
        ("lh2601", "lh"),
        ("bu", "bu"),
        ("", ""),
        ("   ", ""),
        (None, ""),
        ("2612", ""),
        ("bu2612x", "bu"),
    ],
)
def test_product_code_extracts_leading_letters(raw, expected):
    assert product_code(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BU2612", "bu2612"),
        (" bu2612 ", "bu2612"),
        ("SA2701", "sa2701"),
        ("", ""),
        (None, ""),
    ],
)
def test_symbol_key_lowercases_and_strips(raw, expected):
    assert symbol_key(raw) == expected


@pytest.mark.parametrize(
    ("symbol", "exchange"),
    [
        ("bu2612", "SHFE"),
        ("cu2601", "SHFE"),
        ("SA2701", "CZCE"),
        ("IF2601", "CFFEX"),
        ("lh2601", "DCE"),
        ("sc2601", "INE"),
        ("si2601", "GFEX"),
        ("zz2601", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_ctp_exchange_id_maps_known_products(symbol, exchange):
    assert ctp_exchange_id(symbol) == exchange


@pytest.mark.parametrize(
    ("symbol", "instrument"),
    [
        ("bu2612", "bu2612"),
        ("BU2612", "bu2612"),
        (" sa2701 ", "SA2701"),
        ("SA2701", "SA2701"),
        ("IF2601", "IF2601"),
        ("lh2601", "lh2601"),
        ("sc2601", "sc2601"),
        ("zz2601", "zz2601"),
        ("", ""),
        ("   ", ""),
        (None, ""),
    ],
)
def test_ctp_instrument_id_applies_exchange_case_rules(symbol, instrument):
    assert ctp_instrument_id(symbol) == instrument
