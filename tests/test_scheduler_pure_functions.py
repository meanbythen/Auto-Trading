"""scheduler 包里可独立调用的纯函数单元测试。

目标是 `_same_contract` / `ctp_fill_key` 这类不依赖 TradingBot、不碰 SQLite 的
独立逻辑；每个用例单独传参、直接断言返回值，方便快速命中覆盖率缺口。
"""

from __future__ import annotations

from src.scheduler.fill_reconcile import ctp_fill_key
from src.scheduler.order_state import _same_contract


# --- _same_contract --------------------------------------------------------------


def test_same_contract_exact_match() -> None:
    assert _same_contract("bu2601", "bu2601") is True
    assert _same_contract("BU2601", "bu2601") is True


def test_same_contract_rejects_different_contracts() -> None:
    assert _same_contract("bu2601", "bu2605") is False
    assert _same_contract("bu2601", "fu2601") is False


def test_same_contract_rejects_empty_or_blank() -> None:
    """任意一边为空/纯空白都返回 False，不会把缺失合约当成同一个。"""
    assert _same_contract("", "bu2601") is False
    assert _same_contract("bu2601", "") is False
    assert _same_contract("   ", "bu2601") is False
    assert _same_contract(None, "bu2601") is False
    assert _same_contract("bu2601", None) is False
    assert _same_contract("", "") is False


def test_same_contract_czce_three_and_four_digit_same() -> None:
    """郑商所白糖 'SR701' 和 'SR1701' 视为同一合约。"""
    assert _same_contract("SR701", "SR1701") is True
    assert _same_contract("SR1701", "SR701") is True
    assert _same_contract("MA601", "MA1601") is True


def test_same_contract_rejects_czce_different_digits() -> None:
    """只有产品代码+年月末 3 位同时相同才视为同一合约。"""
    assert _same_contract("SR701", "SR705") is False
    assert _same_contract("SR701", "MA1701") is False


# --- ctp_fill_key ----------------------------------------------------------------


def test_ctp_fill_key_builds_dedup_string() -> None:
    assert ctp_fill_key("o12345", 10) == "ctp:o12345:10"


def test_ctp_fill_key_handles_numeric_ref_but_zero_is_treated_as_blank() -> None:
    """order_ref 是非零数字能安全转字符串；0 因 `or ""` 变成空。"""
    assert ctp_fill_key(1001, 2) == "ctp:1001:2"
    # 0 是 falsy，str(0 or "") 变成 ""
    assert ctp_fill_key(0, 5) == ""


def test_ctp_fill_key_returns_empty_for_blank_ref() -> None:
    """空的 order_ref 返回空字符串，调用方据此跳过落账。"""
    assert ctp_fill_key("", 10) == ""
    assert ctp_fill_key("   ", 10) == ""
    assert ctp_fill_key(None, 10) == ""
