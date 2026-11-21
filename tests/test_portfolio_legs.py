"""策略腿：配置解析、逐腿引擎/周期透传、组合展示。"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from src.backtest.portfolio import format_portfolio_lines, parse_legs, run_portfolio_backtest


def _result(days: list[str], values: list[float]) -> SimpleNamespace:
    return SimpleNamespace(equity=list(values), equity_times=list(days), trades=[], params_snapshot={})


def test_parse_legs_reads_engine_and_timeframe_per_leg():
    cfg = {
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["lh2701", "fu2701"],
            "legs": [
                {"symbol": "lh2701", "engine": "multi_factor", "timeframe": "15"},
                {"symbol": "fu2701", "engine": "ema_swing", "timeframe": "30"},
            ],
        }
    }
    legs = parse_legs(cfg)
    assert [item["symbol"] for item in legs] == ["lh2701", "fu2701"]
    assert legs[0]["engine"] == "multi_factor"
    assert legs[1]["engine"] == "ema_swing"
    assert legs[1]["timeframe"] == "30"
    # 只写了一个周期时，止损周期跟它一致。
    assert legs[1]["trail_timeframe"] == "30"


def test_parse_legs_falls_back_to_symbols():
    cfg = {"strategy": {"engine": "ema_swing", "timeframe": "60", "trail_timeframe": "60", "symbols": ["a", "b"]}}
    legs = parse_legs(cfg)
    assert [item["symbol"] for item in legs] == ["a", "b"]
    assert all(item["engine"] == "ema_swing" and item["timeframe"] == "60" for item in legs)


def test_parse_legs_keeps_one_leg_per_symbol():
    cfg = {
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "15",
            "legs": [{"symbol": "lh2701", "timeframe": "15"}, {"symbol": "lh2701", "timeframe": "60"}],
        }
    }
    legs = parse_legs(cfg)
    assert [item["symbol"] for item in legs] == ["lh2701"]
    assert legs[0]["timeframe"] == "15"


def test_parse_legs_unknown_engine_falls_back_to_global():
    cfg = {
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "15",
            "legs": [{"symbol": "fu2701", "engine": "not_a_real_engine", "timeframe": "30"}],
        }
    }
    legs = parse_legs(cfg)
    assert legs[0]["engine"] == "multi_factor"
    assert legs[0]["timeframe"] == "30"


def test_run_portfolio_backtest_passes_engine_and_timeframe_per_leg():
    days = ["2024-01-02", "2024-01-03"]
    seen: dict[str, tuple] = {}

    def runner(cfg, *, symbol, initial_capital, engine=None, timeframe=None, trail_timeframe=None, **kwargs):
        seen[symbol] = (engine, timeframe, trail_timeframe)
        return _result(days, [initial_capital, initial_capital])

    legs = [
        {"symbol": "lh2701", "engine": "multi_factor", "timeframe": "15", "trail_timeframe": "15"},
        {"symbol": "fu2701", "engine": "ema_swing", "timeframe": "30", "trail_timeframe": "30"},
    ]
    out = run_portfolio_backtest({"risk": {"capital": 83_000.0}}, legs=legs, runner=runner)
    assert seen["lh2701"] == ("multi_factor", "15", "15")
    assert seen["fu2701"] == ("ema_swing", "30", "30")
    assert [item["symbol"] for item in out["legs"]] == ["lh2701", "fu2701"]
    assert out["weights"] == {"lh2701": 0.5, "fu2701": 0.5}
    text = "\n".join(format_portfolio_lines(out))
    assert "腿配置" in text
    assert "均线波段" in text and "30分钟" in text
    assert "多因子" in text and "15分钟" in text


def test_run_portfolio_backtest_skips_duplicate_contract():
    days = ["2024-01-02"]

    def runner(cfg, *, symbol, initial_capital, **kwargs):
        return _result(days, [initial_capital])

    legs = [{"symbol": "fu2701", "timeframe": "15"}, {"symbol": "fu2701", "timeframe": "60"}]
    out = run_portfolio_backtest({"risk": {"capital": 50_000.0}}, legs=legs, runner=runner)
    assert out["symbols"] == ["fu2701"]
    assert out["weights"] == {"fu2701": 1.0}
    assert out["skipped"][0] == {"symbol": "fu2701", "reason": "同一合约重复，只保留第一条腿"}


def test_backtest_entries_accept_timeframe_override():
    from src.backtest.engine import run_backtest
    from src.backtest.rollover import run_backtest_rollover

    assert "timeframe" in inspect.signature(run_backtest).parameters
    assert "trail_timeframe" in inspect.signature(run_backtest).parameters
    assert "timeframe" in inspect.signature(run_backtest_rollover).parameters
    assert "trail_timeframe" in inspect.signature(run_backtest_rollover).parameters
