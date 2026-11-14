"""
常驻扫描范围与手动功能

功能：验证常驻只扫当前选中的一个品种、行情过滤按配置生效。
"""

from __future__ import annotations

from src.scheduler.runner import TradingBot


def _cfg(root, *, symbols: list[str], selected: str) -> dict:
    return {
        "dry_run": True,
        "trade": {"symbol": selected, "executor": "sim", "volume": 1, "gui": {}},
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": symbols,
            "market_filter": {"enabled": True, "mode": "trend"},
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(root),
    }


def test_scan_only_selected_symbol(tmp_path):
    """关注列表保留多个，常驻只扫当前选中的那一个。"""
    symbols = ["fu2701", "bu2701", "rb2701"]
    bot = TradingBot(_cfg(tmp_path, symbols=symbols, selected="bu2701"))

    assert bot.symbols == symbols
    assert bot.scan_symbols == ["bu2701"]


def test_scan_falls_back_to_first_when_selected_missing(tmp_path):
    """选中的合约不在关注列表里时，退回列表第一个，不至于空转。"""
    bot = TradingBot(_cfg(tmp_path, symbols=["fu2701", "bu2701"], selected="zz9999"))

    assert bot.scan_symbols == ["fu2701"]


def test_scan_includes_nonflat_positions(tmp_path):
    """当前品种之外的非空持仓也要扫，否则没人给它止损/收盘清仓。"""
    bot = TradingBot(_cfg(tmp_path, symbols=["fu2701", "bu2701"], selected="bu2701"))
    pos = bot.strategy.positions["fu2701"]
    pos.side = "long"
    pos.volume = 1

    bot._refresh_scan_symbols()

    assert bot.scan_symbols == ["bu2701", "fu2701"]


def test_live_market_filter_follows_config(tmp_path):
    """多因子听自己的行情过滤，不跟公共那张表。"""
    on = TradingBot(_cfg(tmp_path, symbols=["fu2701"], selected="fu2701"))
    assert on.strategy.cta_filter.enabled is True
    assert on.strategy.market_filter.enabled is False

    cfg = _cfg(tmp_path, symbols=["fu2701"], selected="fu2701")
    cfg["strategy"]["multi_factor"] = {"cta_filter": {"enabled": False}}
    off = TradingBot(cfg)
    assert off.strategy.cta_filter.enabled is False
    assert off.strategy.market_filter.enabled is False

