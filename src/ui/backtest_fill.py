"""
把配置页上会影响回测结果的内容，收成回测页要填的那一组。

功能：给「按配置页重新填入」用。策略、品种、周期、三个开关和资金数字都在这里算好。
原理：回测页有自己的输入框。不抄的话，点开始回测时会用回测页上的旧开关，配置页改了也不生效。
密码、自动登录、模拟模式、收盘后关机不影响回测，这里不收。
"""

from __future__ import annotations


def product_key(symbol: str) -> str:
    """bu2612 收成 bu。只留字母，和实盘算保证金时用的是同一种取法。"""
    return "".join(ch for ch in str(symbol or "").strip().lower() if "a" <= ch <= "z")


_EMPTY_SYMBOL = "（还没有）"


def choose_backtest_symbol(
    choices: list[str],
    current: str,
    *,
    prefer: str | None = None,
    force: bool = False,
) -> str:
    """
    历史回测品种下拉跟配置页用同一份合约。

    平时只换列表，回测页已经选中的合约还在这份列表里就留着。
    force 时改成配置页当前选中的那一个，用来响应「按配置页重新填入」。
    列表空了就显示「（还没有）」，不拿一个没关注的合约顶上。
    """
    cleaned = [item for item in choices if item and item != _EMPTY_SYMBOL]
    if not cleaned:
        return _EMPTY_SYMBOL
    wanted = str(prefer or "").strip()
    if force and wanted and wanted != _EMPTY_SYMBOL:
        match = next((item for item in cleaned if item.lower() == wanted.lower()), None)
        if match:
            return match
    holding = str(current or "").strip()
    match = next((item for item in cleaned if item.lower() == holding.lower()), None)
    if match:
        return match
    if wanted and wanted != _EMPTY_SYMBOL:
        match = next((item for item in cleaned if item.lower() == wanted.lower()), None)
        if match:
            return match
    return cleaned[0]


def first_symbol(symbols_text: str) -> str:
    """关注品种可能写了好几个，回测一次只跑第一个。空着就用 bu2612。"""
    text = str(symbols_text or "").replace("，", ",").replace("、", ",").replace(";", ",")
    for part in text.split(","):
        piece = part.strip()
        if piece:
            return piece
    return "bu2612"
