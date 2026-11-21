"""
常驻扫描范围与手动功能

功能：验证常驻只扫当前选中的一个品种、行情过滤按配置生效。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.scheduler.runner import TradingBot


def _cfg(
    root,
    *,
    symbols: list[str],
    selected: str,
    multi: dict | None = None,
    legs: list[dict] | None = None,
) -> dict:
    cfg = {
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
        "risk": {
            "sizing_mode": "fixed",
            "capital": 10000,
            "max_daily_loss": 1000,
            "contracts": {
                "fu": {
                    "trading_sessions": "09:00-11:30,13:30-15:00,21:00-23:00",
                    "flatten_times": "14:55,22:55",
                },
                "lh": {
                    "trading_sessions": "09:00-11:30,13:30-15:00",
                    "flatten_times": "14:55",
                },
            },
        },
        "data": {},
        "_project_root": str(root),
    }
    if multi is not None:
        cfg["live"] = {"multi_symbol": multi}
    if legs is not None:
        cfg["strategy"]["legs"] = legs
    return cfg


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


def test_multi_symbol_whitelist_adds_empty_symbols(tmp_path):
    """打开多品种白名单后，空仓的第二个品种也要进扫描清单，选中品种排第一。"""
    bot = TradingBot(
        _cfg(
            tmp_path,
            symbols=["fu2701", "lh2701", "rb2701"],
            selected="lh2701",
            multi={"enabled": True, "symbols": ["fu2701", "lh2701"], "scan_interval_sec": 15},
        )
    )

    assert bot._multi_symbol_enabled() is True
    assert bot.scan_symbols == ["lh2701", "fu2701"]
    assert bot.scan_interval == 30  # schedule 30 秒比多品种 15 秒长，保持原值


def test_multi_symbol_outside_whitelist_is_skipped(tmp_path):
    """白名单里写了关注列表没有的合约，只告警一次，不拿它去拉行情。"""
    bot = TradingBot(
        _cfg(
            tmp_path,
            symbols=["fu2701"],
            selected="fu2701",
            multi={"enabled": True, "symbols": ["fu2701", "zz9999"]},
        )
    )

    assert bot.scan_symbols == ["fu2701"]


def test_multi_symbol_rejects_duplicate_legs(tmp_path):
    """同一合约两条腿会互相覆盖止损，多品种启动时必须直接报错。"""
    cfg = _cfg(
        tmp_path,
        symbols=["fu2701", "lh2701"],
        selected="fu2701",
        multi={"enabled": True, "symbols": ["fu2701", "lh2701"]},
        legs=[
            {"symbol": "fu2701", "engine": "multi_factor", "timeframe": "15"},
            {"symbol": "fu2701", "engine": "ema_swing", "timeframe": "30"},
        ],
    )

    with pytest.raises(ValueError, match="fu2701"):
        TradingBot(cfg)


def test_symbol_sessions_and_flatten_follow_each_contract(tmp_path):
    """燃油有夜盘、生猪没有：21:30 只允许燃油，22:55 只清燃油。"""
    bot = TradingBot(
        _cfg(
            tmp_path,
            symbols=["fu2701", "lh2701"],
            selected="fu2701",
            multi={"enabled": True, "symbols": ["fu2701", "lh2701"]},
        )
    )
    night = datetime(2026, 11, 2, 21, 30)

    assert bot._any_symbol_in_session(["fu2701"], night) is True
    assert bot._any_symbol_in_session(["lh2701"], night) is False
    assert bot._symbol_flatten_due("lh2701", datetime(2026, 11, 2, 22, 55)) is False
    assert bot._symbol_flatten_due("fu2701", datetime(2026, 11, 2, 22, 55)) is True
    assert bot._symbol_flatten_due("lh2701", datetime(2026, 11, 2, 14, 55)) is True


def test_leg_router_uses_each_leg_engine_and_shares_positions(tmp_path):
    """两条腿各用各的引擎/周期，但持仓和信号状态必须是同一份。"""
    bot = TradingBot(
        _cfg(
            tmp_path,
            symbols=["fu2701", "lh2701"],
            selected="fu2701",
            multi={"enabled": True, "symbols": ["fu2701", "lh2701"]},
            legs=[
                {"symbol": "fu2701", "engine": "multi_factor", "timeframe": "5"},
                {"symbol": "lh2701", "engine": "ema_swing", "timeframe": "15"},
            ],
        )
    )

    fu = bot._strategy_for("fu2701")
    lh = bot._strategy_for("lh2701")
    assert fu is not lh
    assert str(fu.timeframe) == "5"
    assert str(lh.timeframe) == "15"
    assert fu.positions is lh.positions
    assert bot.strategy.positions is fu.positions
    assert bot.strategy.cfg_for("lh2701")["strategy"]["symbols"] == ["lh2701"]


def test_leg_router_not_used_when_multi_disabled(tmp_path):
    """多品种关闭时保持原全局策略，CTP 权限不够也不用改代码。"""
    bot = TradingBot(
        _cfg(
            tmp_path,
            symbols=["fu2701", "lh2701"],
            selected="fu2701",
            multi={"enabled": False, "symbols": ["fu2701", "lh2701"]},
            legs=[
                {"symbol": "fu2701", "engine": "multi_factor", "timeframe": "5"},
                {"symbol": "lh2701", "engine": "ema_swing", "timeframe": "15"},
            ],
        )
    )

    assert not hasattr(bot.strategy, "strategy_for")
    assert bot.scan_symbols == ["fu2701"]


def test_multi_symbol_disabled_keeps_single_symbol_scan(tmp_path):
    """开关关闭时完全保持原口径，第二个空仓品种不扫。"""
    bot = TradingBot(
        _cfg(
            tmp_path,
            symbols=["fu2701", "lh2701"],
            selected="fu2701",
            multi={"enabled": False, "symbols": ["fu2701", "lh2701"]},
        )
    )

    assert bot.scan_symbols == ["fu2701"]

