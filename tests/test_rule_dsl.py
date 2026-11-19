"""rule_dsl：编译校验、条件树求值、RuleStrategy 接入与等价性。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.strategy.dual_ma import DualMaStrategy
from src.strategy.rule_dsl import (
    INDICATOR_LIBRARY,
    RuleConfigError,
    RuleStrategy,
    _INDICATOR_KINDS,
    compile_rule,
    delete_rule_file,
    evaluate_condition,
    indicator_formula_text,
    indicator_name_hint,
    indicator_param_type,
    indicator_snippet,
    insert_indicator_line,
    is_builtin_rule,
    is_rule_stem,
    list_rule_files,
    load_rule_file,
    rename_rule_file,
    rule_engine_code,
    rule_engine_stem,
    save_rule_text,
    validate_expr_formula,
    validate_indicator,
)
from src.strategy.types import StrategyAction

TEMPLATE_RULE = Path(__file__).resolve().parents[1] / "config" / "rules" / "dual_ma_equiv.yaml"
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "config" / "rules"


def _bars(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> pd.DataFrame:
    index = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="15min")
    high = highs if highs is not None else [value + 1 for value in closes]
    low = lows if lows is not None else [value - 1 for value in closes]
    return pd.DataFrame(
        {
            "open": closes,
            "high": high,
            "low": low,
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=index,
    )


MA_RULE = {
    "schema": 1,
    "name": "收盘上穿3均线",
    "indicators": {"ma3": {"kind": "sma", "period": 3}},
    "entry": {"long": {"cross_above": {"left": "close", "right": "ma3"}}},
    "exit": {"long": {"cross_below": {"left": "close", "right": "ma3"}}},
    "stop": {"kind": "percent", "pct": 2},
}


# ---------------------------------------------------------------- 编译校验


def test_compile_minimal_rule_sets_required_bars() -> None:
    rule = compile_rule(MA_RULE)
    assert rule.name == "收盘上穿3均线"
    assert rule.required_bars == 5  # 周期 3 + 2
    assert set(rule.entries) == {"long"}
    assert rule.stop.kind == "percent"


def test_unknown_root_key_rejected() -> None:
    with pytest.raises(RuleConfigError) as exc:
        compile_rule({**MA_RULE, "entry2": {}})
    assert "entry2" in str(exc.value)


def test_unknown_condition_rejected() -> None:
    raw = {
        "schema": 1,
        "indicators": {"ma3": {"kind": "sma", "period": 3}},
        "entry": {"long": {"above": {"left": "close", "right": "ma3"}}},
        "stop": {"kind": "percent", "pct": 1},
    }
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "above" in str(exc.value)


def test_negative_prev_is_rejected_as_future_data() -> None:
    raw = {
        "schema": 1,
        "indicators": {"ma3": {"kind": "sma", "period": 3}},
        "entry": {"long": {"gt": {"left": {"prev": "close", "n": -1}, "right": 100}}},
        "stop": {"kind": "percent", "pct": 1},
    }
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "prev" in str(exc.value) or "n" in str(exc.value)


def test_empty_entry_rejected() -> None:
    raw = {"schema": 1, "indicators": {"ma3": {"kind": "sma", "period": 3}}, "entry": {}, "stop": {"kind": "none"}}
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "entry" in str(exc.value)


def test_unknown_indicator_reference_rejected() -> None:
    raw = {
        "schema": 1,
        "indicators": {"ma3": {"kind": "sma", "period": 3}},
        "entry": {"long": {"gt": {"left": "close", "right": "ma30"}}},
        "stop": {"kind": "none"},
    }
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "ma30" in str(exc.value)


def test_unknown_indicator_kind_rejected() -> None:
    raw = {
        "schema": 1,
        "indicators": {"x": {"kind": "kdj", "period": 9}},
        "entry": {"long": {"gt": {"left": "close", "right": 1}}},
        "stop": {"kind": "none"},
    }
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "kdj" in str(exc.value)


def test_indicator_name_cannot_shadow_bar_field() -> None:
    raw = {
        "schema": 1,
        "indicators": {"close": {"kind": "sma", "period": 3}},
        "entry": {"long": {"gt": {"left": "close", "right": 1}}},
        "stop": {"kind": "none"},
    }
    with pytest.raises(RuleConfigError):
        compile_rule(raw)


def test_atr_stop_must_reference_atr_indicator() -> None:
    raw = {
        "schema": 1,
        "indicators": {"ma3": {"kind": "sma", "period": 3}},
        "entry": {"long": {"gt": {"left": "close", "right": "ma3"}}},
        "stop": {"kind": "atr", "mult": 2, "atr_ref": "ma3"},
    }
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "atr_ref" in str(exc.value)


def test_trail_atr_must_reference_atr_indicator() -> None:
    raw = {**MA_RULE, "trail": {"kind": "atr", "mult": 1}}
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "trail.atr_ref" in str(exc.value)


def test_sizing_only_allows_inherit() -> None:
    with pytest.raises(RuleConfigError):
        compile_rule({**MA_RULE, "sizing": {"mode": "fixed"}})


def test_load_rule_file_reports_bad_yaml(tmp_path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("schema: [1\n", encoding="utf-8")
    with pytest.raises(RuleConfigError) as exc:
        load_rule_file(path)
    assert "YAML" in str(exc.value)


# ---------------------------------------------------------------- 条件求值


def test_cross_above_fires_on_last_bar() -> None:
    rule = compile_rule(MA_RULE)
    bars = _bars([10, 9, 8, 7, 9])
    assert rule.signal(bars) == "long"


def test_no_signal_when_only_previous_bar_crossed() -> None:
    rule = compile_rule(MA_RULE)
    bars = _bars([10, 9, 8, 7, 9, 9.5])  # 上一根已在上方，这一根不算新的上穿
    assert rule.signal(bars) is None


def test_exit_condition_hits_after_cross_below() -> None:
    rule = compile_rule(MA_RULE)
    bars = _bars([8, 9, 10, 11, 6])
    assert rule.exit_hit("long", bars) is True


def test_both_directions_true_returns_none() -> None:
    raw = {
        "schema": 1,
        "indicators": {"ma3": {"kind": "sma", "period": 3}},
        "entry": {
            "long": {"gt": {"left": "close", "right": 0}},
            "short": {"gt": {"left": "close", "right": 0}},
        },
        "stop": {"kind": "none"},
    }
    rule = compile_rule(raw)
    assert rule.signal(_bars([10, 10, 10, 10])) is None


def test_between_rising_and_not() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {"ma3": {"kind": "sma", "period": 3}},
            "entry": {
                "long": {
                    "all": [
                        {"between": {"value": "close", "low": 9, "high": 20}},
                        {"rising": {"ref": "ma3", "bars": 2}},
                        {"not": {"lt": {"left": "close", "right": 5}}},
                    ]
                }
            },
            "stop": {"kind": "none"},
        }
    )
    values = rule.values(_bars([10, 11, 12, 13, 14]))
    assert evaluate_condition(rule.entries["long"], values) is True


def test_missing_values_count_as_false() -> None:
    rule = compile_rule(MA_RULE)
    values = rule.values(_bars([10, 9]))  # 不足 3 根，均线是空值
    assert evaluate_condition(rule.entries["long"], values) is False


# ---------------------------------------------------------------- 未来函数


def test_indicators_are_causal_prefix_equals_full() -> None:
    """同一根之前的值，不能被后面的 K 线改变；否则就是未来函数。"""
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {
                "ma5": {"kind": "sma", "period": 5},
                "e21": {"kind": "ema", "period": 21},
                "a7": {"kind": "atr", "period": 7},
            },
            "entry": {"long": {"gt": {"left": "close", "right": "ma5"}}},
            "stop": {"kind": "none"},
        }
    )
    bars = _bars([10 + (index % 7) for index in range(60)])
    full = rule.values(bars)
    for cut in (10, 30, 59):
        prefix = rule.values(bars.iloc[: cut + 1])
        for name in ("ma5", "e21", "a7"):
            pd.testing.assert_series_equal(full[name].iloc[: cut + 1], prefix[name], check_names=False)


# ---------------------------------------------------------------- 止损


def test_percent_stop_both_sides() -> None:
    rule = compile_rule(MA_RULE)
    bars = _bars([100, 100, 100])
    assert rule.initial_stop("long", bars, 100) == pytest.approx(98)
    assert rule.initial_stop("short", bars, 100) == pytest.approx(102)


def test_atr_stop_uses_indicator_value() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {"atr3": {"kind": "atr", "period": 3}},
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "atr", "mult": 2, "atr_ref": "atr3"},
        }
    )
    bars = _bars([10, 11, 12, 13], highs=[11, 12, 13, 14], lows=[9, 10, 11, 12])
    atr = rule.values(bars)["atr3"].iloc[-1]
    assert rule.initial_stop("long", bars, 13) == pytest.approx(13 - 2 * atr)
    assert rule.initial_stop("short", bars, 13) == pytest.approx(13 + 2 * atr)


def test_swing_stop_uses_lookback_extreme() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {"ma3": {"kind": "sma", "period": 3}},
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "swing", "lookback": 3},
        }
    )
    bars = _bars([10, 11, 12], lows=[9, 8, 10])
    assert rule.initial_stop("long", bars, 12) == pytest.approx(8)


# ---------------------------------------------------------------- 策略骨架


def test_rule_strategy_trail_follows_public_stop_period() -> None:
    """自定义规则的止损周期跟公共项，不再抄成信号周期。"""
    cfg = {
        "strategy": {
            "timeframe": "30",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
            "flatten_at_close": True,
        },
        "trade": {"volume": 2, "symbol": "bu2612"},
    }
    strategy = RuleStrategy(cfg, rule_data=MA_RULE)
    assert strategy.timeframe == "30"
    assert strategy.trail_timeframe == "5"


def test_rule_strategy_loads_inline_rule() -> None:
    cfg = {
        "strategy": {"timeframe": "30", "symbols": ["bu2612"], "flatten_at_close": True},
        "trade": {"volume": 2, "symbol": "bu2612"},
    }
    strategy = RuleStrategy(cfg, rule_data=MA_RULE)
    assert strategy.required_bars == 5
    assert strategy.timeframe == "30"
    assert strategy.volume == 2
    assert list(strategy.positions) == ["bu2612"]
    assert strategy.positions["bu2612"].side == "flat"


def test_rule_strategy_requires_configured_file() -> None:
    with pytest.raises(RuleConfigError) as exc:
        RuleStrategy({"strategy": {}, "trade": {}})
    assert "rule_dsl" in str(exc.value)


def _cfg(**strategy) -> dict:
    box = {"timeframe": "15", "symbols": ["bu2612"], "flatten_at_close": False}
    box.update(strategy)
    return {"strategy": box, "trade": {"volume": 1, "symbol": "bu2612"}}


def test_shipped_template_rule_compiles() -> None:
    rule = load_rule_file(TEMPLATE_RULE)
    assert rule.source.endswith("dual_ma_equiv.yaml")
    assert set(rule.entries) == {"long", "short"}
    assert rule.stop.kind == "atr"


def test_evaluate_opens_long_and_dedupes_same_bar() -> None:
    strategy = RuleStrategy(_cfg(), rule_data=MA_RULE)
    bars = _bars([10, 9, 8, 7, 9])
    now = datetime(2026, 1, 5, 9, 30)

    decision = strategy.evaluate("bu2612", bars, now=now)
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.stop_price == pytest.approx(9 * 0.98)
    assert decision.entry_price == pytest.approx(9)

    again = strategy.evaluate("bu2612", bars, now=now)
    assert again.action == StrategyAction.HOLD
    assert "已经处理过" in again.reason


def test_evaluate_holds_when_bars_not_enough() -> None:
    strategy = RuleStrategy(_cfg(), rule_data=MA_RULE)
    decision = strategy.evaluate("bu2612", _bars([10, 9]), now=datetime(2026, 1, 5, 9, 30))
    assert decision.action == StrategyAction.HOLD
    assert "K线不足" in decision.reason


def test_evaluate_closes_on_stop_and_on_exit() -> None:
    strategy = RuleStrategy(_cfg(), rule_data=MA_RULE)
    now = datetime(2026, 1, 5, 9, 30)
    strategy.evaluate("bu2612", _bars([10, 9, 8, 7, 9]), now=now)
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 9, 1, initial_stop=8.8)

    stopped = strategy.evaluate("bu2612", _bars([10, 9, 8, 7, 8.5]), now=now)
    assert stopped.action == StrategyAction.CLOSE
    assert "止损" in stopped.reason

    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 9, 1, initial_stop=8.8)
    exited = strategy.evaluate("bu2612", _bars([8, 9, 10, 11, 6]), now=now)
    assert exited.action == StrategyAction.CLOSE


def test_dual_ma_and_rule_open_on_the_same_bar() -> None:
    """同一段 K 线，内置双均线和规则模板必须在同一根给出开仓信号。"""
    closes = [100.0] * 40 + [100 - index for index in range(20)] + [80 + 2 * index for index in range(20)]
    bars = _bars(closes)
    now = datetime(2026, 1, 5, 9, 30)

    def first_open(strategy) -> int:
        for cut in range(60, len(bars)):
            strategy.clear_signaled_bar("bu2612")
            decision = strategy.evaluate("bu2612", bars.iloc[: cut + 1], now=now)
            if decision.action == StrategyAction.BUY_OPEN:
                return cut
        return -1

    dual = first_open(DualMaStrategy(_cfg()))
    rule = first_open(RuleStrategy(_cfg(), rule_path=TEMPLATE_RULE))
    assert dual >= 0
    assert rule == dual


def test_backtest_runs_with_rule_dsl() -> None:
    """注册进回测引擎后能完整跑一轮，不报错、不静默换成别的策略。"""
    from src.backtest.engine import run_backtest
    from src.data.kline import KlineProvider

    cfg = _cfg()
    cfg.update(
        {
            "dry_run": True,
            "strategy": {
                "engine": "rule_dsl",
                "timeframe": "15",
                "trail_timeframe": "15",
                "symbols": ["bu2612"],
                "flatten_times": ["14:55", "22:55"],
                "rule_dsl": {"file": str(TEMPLATE_RULE)},
            },
            "risk": {"capital": 1_000_000, "contracts": {"bu": {"multiplier": 10}}},
            "data": {"source": "demo", "cache_dir": "data/klines", "min_bars": 60},
            "schedule": {"trading_sessions": [["09:00", "23:00"]]},
        }
    )
    provider = KlineProvider(cfg)
    bars = provider.fetch("bu2612", "15")
    result = run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)
    assert result.bars >= 60
    assert isinstance(result.performance.score, float)


def test_create_strategy_blocks_instead_of_switching(tmp_path, monkeypatch) -> None:
    """规则文件写错时不能悄悄回退到别的策略，只能停手并报出来。"""
    from src.ops import alerts
    from src.strategy.factory import create_strategy

    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "emit_alert", lambda code, msg, **kw: captured.append((code, msg)) or True)

    cfg = _cfg()
    cfg["strategy"]["engine"] = "rule_dsl"
    cfg["strategy"]["rule_dsl"] = {"file": str(tmp_path / "missing.yaml")}

    strategy = create_strategy(cfg)
    assert type(strategy).__name__ == "_BlockedStrategy"
    assert strategy.evaluate("bu2612", _bars([1, 2, 3]), now=datetime(2026, 1, 5, 9, 30)).action == (
        StrategyAction.HOLD
    )
    assert captured and captured[0][0] == "rule_dsl"
    assert "missing.yaml" in captured[0][1]


# ---------------------------------------------------------------- 算术表达式


def _ohlc(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows 是 (open, high, low, close)，方便手算档位。"""
    index = pd.date_range("2026-01-05 09:00", periods=len(rows), freq="15min")
    return pd.DataFrame(
        {
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": [100] * len(rows),
        },
        index=index,
    )


def _with_entry(node: dict) -> dict:
    return {"schema": 1, "indicators": {}, "entry": {"long": node}, "stop": {"kind": "none"}}


def test_expr_arithmetic_and_functions() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {
                "a": {"kind": "sma", "period": 2},
                "b": {"kind": "expr", "formula": "max(a - close, 1) + abs(-2) * 2"},
            },
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "none"},
        }
    )
    values = rule.values(_bars([10, 12]))
    # a 最后一根 = 11，max(11-12, 1) = 1，abs(-2)*2 = 4
    assert values["b"].iloc[-1] == pytest.approx(5)


def test_expr_rejects_unknown_name() -> None:
    raw = _with_entry({"gt": {"left": "close", "right": 0}})
    raw["indicators"] = {"x": {"kind": "expr", "formula": "missing + 1"}}
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "missing" in str(exc.value)


def test_expr_rejects_forward_reference() -> None:
    raw = _with_entry({"gt": {"left": "close", "right": 0}})
    raw["indicators"] = {
        "x": {"kind": "expr", "formula": "ma3 + 1"},
        "ma3": {"kind": "sma", "period": 3},
    }
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "ma3" in str(exc.value)


def test_expr_rejects_bad_syntax() -> None:
    raw = _with_entry({"gt": {"left": "close", "right": 0}})
    raw["indicators"] = {"x": {"kind": "expr", "formula": "close +"}}
    with pytest.raises(RuleConfigError) as exc:
        compile_rule(raw)
    assert "公式" in str(exc.value)


def test_highest_can_take_close_column() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {"hc2": {"kind": "highest", "period": 2, "field": "close"}},
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "none"},
        }
    )
    bars = _bars([10, 15, 12], highs=[100, 101, 102])
    assert rule.values(bars)["hc2"].iloc[-1] == pytest.approx(15)


def test_expr_is_causal_prefix_equals_full() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {
                "ma5": {"kind": "sma", "period": 5},
                "a7": {"kind": "atr", "period": 7},
                "band": {"kind": "expr", "formula": "ma5 + 1.5 * a7"},
            },
            "entry": {"long": {"gt": {"left": "close", "right": "band"}}},
            "stop": {"kind": "none"},
        }
    )
    bars = _bars([10 + (index % 7) for index in range(60)])
    full = rule.values(bars)
    for cut in (10, 30, 59):
        prefix = rule.values(bars.iloc[: cut + 1])
        pd.testing.assert_series_equal(full["band"].iloc[: cut + 1], prefix["band"], check_names=False)


# ---------------------------------------------------------------- 日内策略模板


def test_shipped_intraday_templates_compile() -> None:
    for name in ("dualthrust.yaml", "r_breaker.yaml", "turtle_adx.yaml", "adx_atr_break.yaml"):
        rule = load_rule_file(TEMPLATE_DIR / name)
        assert rule.name
        assert set(rule.entries) == {"long", "short"}
        assert set(rule.exits) == {"long", "short"}
        assert rule.stop.kind == "atr"


def _level_rule(indicators: dict, entry: dict) -> dict:
    return {"schema": 1, "indicators": indicators, "entry": entry, "stop": {"kind": "none"}}


def test_dual_thrust_levels_match_formula() -> None:
    rule = compile_rule(
        _level_rule(
            {
                "hh": {"kind": "highest", "period": 3},
                "ll": {"kind": "lowest", "period": 3},
                "hc": {"kind": "highest", "period": 3, "field": "close"},
                "lc": {"kind": "lowest", "period": 3, "field": "close"},
                "dt_range": {"kind": "expr", "formula": "max(hh - lc, hc - ll)"},
                "up": {"kind": "expr", "formula": "open + 0.7 * dt_range"},
                "dn": {"kind": "expr", "formula": "open - 0.7 * dt_range"},
            },
            {
                "long": {"gt": {"left": "close", "right": {"prev": "up", "n": 1}}},
                "short": {"lt": {"left": "close", "right": {"prev": "dn", "n": 1}}},
            },
        )
    )
    bars = _ohlc(
        [
            (10, 11, 9, 10),
            (10, 12, 9, 11),
            (11, 13, 10, 12),
            (12, 13, 11, 12),
            (12, 15, 12, 15),
        ]
    )
    values = rule.values(bars)
    # 最后一根：hh=15 ll=10 hc=15 lc=12 → range=5 → up=12+3.5 dn=12-3.5
    assert values["up"].iloc[-1] == pytest.approx(15.5)
    assert values["dn"].iloc[-1] == pytest.approx(8.5)
    # 上一根 up=12+0.7*max(13-10, 12-9)=14.1，收盘 15 突破 → 做多
    assert rule.signal(bars) == "long"


def test_r_breaker_levels_match_formula() -> None:
    rule = compile_rule(
        _level_rule(
            {
                "hh": {"kind": "highest", "period": 3},
                "ll": {"kind": "lowest", "period": 3},
                "width": {"kind": "expr", "formula": "hh - ll"},
                "r3": {"kind": "expr", "formula": "hh + 0.35 * (close - ll)"},
                "r1": {"kind": "expr", "formula": "close + 0.25 * width"},
                "s1": {"kind": "expr", "formula": "close - 0.25 * width"},
                "s3": {"kind": "expr", "formula": "ll - 0.35 * (close - ll)"},
            },
            {
                "long": {"gt": {"left": "close", "right": {"prev": "r3", "n": 1}}},
                "short": {"lt": {"left": "close", "right": {"prev": "s3", "n": 1}}},
            },
        )
    )
    bars = _ohlc(
        [
            (10, 11, 9, 10),
            (10, 12, 9, 11),
            (11, 13, 10, 12),
            (12, 13, 11, 12),
            (12, 15, 12, 15),
        ]
    )
    values = rule.values(bars)
    # 最后一根：h=15 l=10 c=15 → r3=15+0.35*5=16.75，s3=10-0.35*5=8.25
    assert values["r3"].iloc[-1] == pytest.approx(16.75)
    assert values["s3"].iloc[-1] == pytest.approx(8.25)
    assert values["r1"].iloc[-1] == pytest.approx(16.25)
    assert values["s1"].iloc[-1] == pytest.approx(13.75)
    # 上一根 r3=13+0.35*(12-9)=14.05，收盘 15 站上 → 做多
    assert rule.signal(bars) == "long"


# ---------------------------------------------------------------- 配置页的规则编辑器


def test_rule_engine_code_roundtrip() -> None:
    assert rule_engine_code("DualThrust") == "rule_dsl:dualthrust"
    assert rule_engine_stem(" rule_dsl:turtle_adx ") == "turtle_adx"
    assert rule_engine_stem("dual_ma") is None
    assert rule_engine_stem("rule_dsl:") is None
    assert is_rule_stem("my-rule_2") is True
    assert is_rule_stem("Bad Name") is False


def test_list_rule_files_reads_titles() -> None:
    titles = {item["stem"]: item["name"] for item in list_rule_files(TEMPLATE_DIR)}
    assert {"dualthrust", "r_breaker", "turtle_adx", "adx_atr_break", "dual_ma_equiv"} <= set(titles)
    assert titles["dualthrust"] == "DualThrust 日内突破"


def test_save_rule_text_writes_only_valid(tmp_path) -> None:
    path = tmp_path / "mine.yaml"
    text = (
        'schema: 1\nname: "我的规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n"
    )
    rule = save_rule_text(path, text)
    assert rule.name == "我的规则"
    assert path.exists()

    with pytest.raises(RuleConfigError):
        save_rule_text(path, "schema: 1\nentry: {}\nstop: {kind: none}\n")
    # 校验失败时原文件一个字节都不能动
    assert "我的规则" in path.read_text(encoding="utf-8")


def test_register_rule_engines_adds_codes(tmp_path) -> None:
    """保存后的规则立刻出现在策略下拉框名单里，重复注册不会加两遍。"""
    from src.ui.app_common import ENGINE_CHOICES, ENGINE_LABELS, register_rule_engines

    (tmp_path / "demo_rule.yaml").write_text(
        'schema: 1\nname: "演示规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n",
        encoding="utf-8",
    )
    code = "rule_dsl:demo_rule"
    try:
        assert code in register_rule_engines(tmp_path)
        assert code in ENGINE_CHOICES
        assert ENGINE_LABELS[code] == "自定义：演示规则"
        assert register_rule_engines(tmp_path) == []
    finally:
        if code in ENGINE_CHOICES:
            ENGINE_CHOICES.remove(code)
        ENGINE_LABELS.pop(code, None)


# ---------------------------------------------------------------- 内置/用户规则边界


def _write_user_rule(path: Path, name: str = "用户规则") -> str:
    text = (
        f'schema: 1\nname: "{name}"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n"
    )
    path.write_text(text, encoding="utf-8")
    return text


def test_builtin_rules_are_marked_in_template_dir() -> None:
    """出厂模板都带 builtin: true，界面上据此禁止删改。"""
    stems = {item["stem"]: item["builtin"] for item in list_rule_files(TEMPLATE_DIR)}
    assert all(stems.values()), f"以下内置规则缺少 builtin: true：{[k for k, v in stems.items() if not v]}"
    assert is_builtin_rule("dualthrust", TEMPLATE_DIR) is True


def test_builtin_rule_cannot_be_deleted(tmp_path) -> None:
    path = tmp_path / "builtin_x.yaml"
    path.write_text("schema: 1\nbuiltin: true\nname: 内置\nindicators: {}\nentry: {}\nstop: {kind: none}\n", encoding="utf-8")
    with pytest.raises(RuleConfigError):
        delete_rule_file("builtin_x", tmp_path)
    assert path.exists()


def test_builtin_rule_cannot_be_overwritten(tmp_path) -> None:
    path = tmp_path / "builtin_y.yaml"
    original = _write_user_rule(path, "我的规则").replace("schema: 1\n", "schema: 1\nbuiltin: true\n")
    path.write_text(original, encoding="utf-8")
    modified = original.replace("我的规则", "内置规则-改过")
    assert modified != original
    with pytest.raises(RuleConfigError):
        save_rule_text(path, modified)
    # 原文件一字未改
    assert path.read_text(encoding="utf-8") == original
    # 显式 allow_overwrite_builtin 才能覆盖（仅给内部迁移用，UI 不走这条）
    save_rule_text(path, modified, allow_overwrite_builtin=True)
    assert path.read_text(encoding="utf-8") != original


def test_user_rule_can_be_deleted_and_renamed(tmp_path) -> None:
    path = tmp_path / "mine.yaml"
    _write_user_rule(path, "我的规则")
    # 改名
    assert rename_rule_file("mine", "yours", tmp_path) == "yours"
    assert not (tmp_path / "mine.yaml").exists()
    assert (tmp_path / "yours.yaml").exists()
    # 目标已存在时拒绝
    (tmp_path / "third.yaml").write_text("schema: 1\nname: x\nindicators: {}\nentry: {}\nstop: {kind: none}\n", encoding="utf-8")
    with pytest.raises(RuleConfigError):
        rename_rule_file("yours", "third", tmp_path)
    # 删除
    delete_rule_file("yours", tmp_path)
    assert not (tmp_path / "yours.yaml").exists()


def test_user_rule_missing_cannot_be_deleted(tmp_path) -> None:
    with pytest.raises(RuleConfigError):
        delete_rule_file("nope", tmp_path)


def test_indicator_library_kinds_are_supported_and_examples_compile() -> None:
    """指标库里每个 kind 都受支持，example 是合法的 indicators 片段；
    对每个 kind 用最简参数编译一条规则，确认实现没有坏。"""
    import yaml as _yaml

    for ind in INDICATOR_LIBRARY:
        assert ind["kind"] in _INDICATOR_KINDS, f"指标库出现未支持的 kind: {ind['kind']}"
        # example 必须能被 yaml 解析成字典（即合法的 indicators 条目）。
        parsed = _yaml.safe_load(f"indicators:\n{ind['example']}")
        assert isinstance(parsed, dict) and isinstance(parsed.get("indicators"), dict)

    # 用每个 kind 的最简参数拼一条规则，确认每种指标都能编译。
    minimal = {
        "ema": {"kind": "ema", "period": 3},
        "sma": {"kind": "sma", "period": 3},
        "atr": {"kind": "atr", "period": 3},
        "adx": {"kind": "adx", "period": 3},
        "highest": {"kind": "highest", "period": 3},
        "lowest": {"kind": "lowest", "period": 3},
        "rsi": {"kind": "rsi", "period": 3},
        "macd_hist": {"kind": "macd_hist", "fast": 3, "slow": 5, "signal": 2},
        "boll_upper": {"kind": "boll_upper", "period": 3, "std": 2.0},
        "boll_lower": {"kind": "boll_lower", "period": 3, "std": 2.0},
        "expr": {"kind": "expr", "formula": "close + 1"},
    }
    indicators = {f"i_{name}": spec for name, spec in minimal.items()}
    rule = {
        "schema": 1,
        "name": "指标库自检",
        "indicators": indicators,
        "entry": {"long": {"gt": {"left": "close", "right": "i_sma"}}},
        "stop": {"kind": "none"},
    }
    compile_rule(rule, source="indicator-lib-check")


def test_indicator_snippet_and_formula_preview() -> None:
    """面板拼出来的 YAML 行与公式说明要跟参数一致。"""
    assert indicator_snippet("ma20", "ema", {"period": 20}) == "  ma20: { kind: ema, period: 20 }"
    # 空值参数自动省略
    assert indicator_snippet("atr14", "atr", {"period": 14, "field": ""}) == "  atr14: { kind: atr, period: 14 }"
    # 文本参数加引号，避免 YAML 解析歧义
    assert indicator_snippet("dt", "expr", {"formula": "open + 1"}) == '  dt: { kind: expr, formula: "open + 1" }'
    ema_text = indicator_formula_text("ema", {"period": 20})
    assert "EMA(20)" in ema_text and "0.0952" in ema_text
    assert "EMA(12)" in indicator_formula_text("macd_hist", {"fast": 12, "slow": 26, "signal": 9})
    assert "2 ×" in indicator_formula_text("boll_upper", {"period": 20, "std": 2.0})
    assert indicator_name_hint("highest") == "hh"
    assert indicator_param_type("std") == "float"
    assert indicator_param_type("period") == "int"


def test_indicator_snippet_compiles_inside_rule() -> None:
    """面板生成的片段插进规则后必须能直接编译。"""
    import yaml as _yaml

    snippet = indicator_snippet("bo", "boll_upper", {"period": 20, "std": 2.0})
    text = (
        "schema: 1\nname: 面板自检\nindicators:\n"
        f"{snippet}\n"
        "entry:\n  long: { gt: { left: close, right: bo } }\n"
        "stop: { kind: none }\n"
    )
    compile_rule(_yaml.safe_load(text), source="panel-check")


def test_insert_indicator_line_into_existing_block() -> None:
    text = (
        'schema: 1\nname: "t"\nindicators:\n  ma20: { kind: ema, period: 20 }\n\n'
        "entry:\n  long: { gt: { left: close, right: ma20 } }\nstop: { kind: none }\n"
    )
    new_text = insert_indicator_line(text, indicator_snippet("atr14", "atr", {"period": 14}))
    lines = new_text.splitlines()
    assert lines[3] == "  ma20: { kind: ema, period: 20 }"
    assert lines[4] == "  atr14: { kind: atr, period: 14 }"
    assert new_text.endswith("\n")
    # 插完还是合法规则
    import yaml as _yaml

    compile_rule(_yaml.safe_load(new_text), source="insert-check")


def test_insert_indicator_line_creates_missing_block() -> None:
    text = 'schema: 1\nname: "t"\nentry:\n  long: { gt: { left: close, right: 1 } }\nstop: { kind: none }\n'
    new_text = insert_indicator_line(text, indicator_snippet("ma3", "sma", {"period": 3}))
    assert "indicators:\n  ma3: { kind: sma, period: 3 }" in new_text
    # 新段插在 entry 之前，不会破坏原有顶层键
    assert new_text.index("indicators:") < new_text.index("entry:")
    import yaml as _yaml

    compile_rule(_yaml.safe_load(new_text), source="insert-new-block")


def test_validate_indicator_reports_bad_params() -> None:
    validate_indicator("ma20", "ema", {"period": 20})
    with pytest.raises(RuleConfigError):
        validate_indicator("ma20", "ema", {"period": 1})
    with pytest.raises(RuleConfigError):
        validate_indicator("close", "ema", {"period": 20})  # 和行情字段重名
    with pytest.raises(RuleConfigError):
        validate_indicator("x", "nope", {})


def test_validate_expr_formula_uses_defined_names() -> None:
    validate_expr_formula("close / 2 + ma20", ["ma20"])
    with pytest.raises(RuleConfigError):
        validate_expr_formula("close / 2 + nope", ["ma20"])
    with pytest.raises(RuleConfigError):
        validate_expr_formula("close +", [])


def test_register_rule_engines_prunes_deleted(tmp_path) -> None:
    """删除 yaml 后再注册，对应引擎会从下拉列表里消失。"""
    from src.ui.app_common import ENGINE_CHOICES, ENGINE_LABELS, register_rule_engines

    (tmp_path / "demo_rule.yaml").write_text(
        'schema: 1\nname: "演示规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n",
        encoding="utf-8",
    )
    code = "rule_dsl:demo_rule"
    try:
        register_rule_engines(tmp_path)
        assert code in ENGINE_CHOICES
        (tmp_path / "demo_rule.yaml").unlink()
        register_rule_engines(tmp_path)
        assert code not in ENGINE_CHOICES
        assert code not in ENGINE_LABELS
    finally:
        if code in ENGINE_CHOICES:
            ENGINE_CHOICES.remove(code)
        ENGINE_LABELS.pop(code, None)


# ---------------------------------------------------------------- 指标库扩编


def test_every_library_kind_compiles_with_its_defaults() -> None:
    """指标库里的每个 kind，按面板默认值拼出来的行都能直接编译；且和解析器支持的种类一一对应。"""
    import yaml as _yaml

    from src.strategy.rule_dsl import indicator_defaults

    assert {str(entry["kind"]) for entry in INDICATOR_LIBRARY} == set(_INDICATOR_KINDS)
    for entry in INDICATOR_LIBRARY:
        if entry["kind"] == "expr":
            continue
        params = indicator_defaults(entry["kind"])
        snippet = indicator_snippet(f"i_{entry['kind']}", entry["kind"], params)
        text = (
            'schema: 1\nname: "库自检"\nindicators:\n'
            f"{snippet}\n"
            "entry:\n  long: { gt: { left: close, right: low } }\n"
            "stop: { kind: none }\n"
        )
        compile_rule(_yaml.safe_load(text), source=f"lib-{entry['kind']}")


def test_new_indicator_math() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {
                "sd3": {"kind": "stddev", "period": 3},
                "bm": {"kind": "boll_mid", "period": 3},
                "bw": {"kind": "boll_width", "period": 3, "std": 2.0},
                "dm": {"kind": "donchian_mid", "period": 3},
                "r3": {"kind": "roc", "period": 3},
                "v3": {"kind": "vol_sma", "period": 3},
                "obv": {"kind": "obv"},
                "k3": {"kind": "stoch_k", "period": 3, "smooth": 1, "d": 2},
                "wr3": {"kind": "williams_r", "period": 3},
                "cci3": {"kind": "cci", "period": 3},
                "ap3": {"kind": "atr_pct", "period": 3},
                "kc": {"kind": "keltner_lower", "period": 3, "atr_period": 3, "mult": 2.0},
                "st": {"kind": "supertrend", "period": 3, "mult": 2.0},
                "sar": {"kind": "psar"},
                "dif": {"kind": "macd_dif", "fast": 2, "slow": 4, "signal": 2},
                "dea": {"kind": "macd_dea", "fast": 2, "slow": 4, "signal": 2},
                "pdi": {"kind": "plus_di", "period": 3},
                "mdi": {"kind": "minus_di", "period": 3},
            },
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "none"},
        }
    )
    bars = _ohlc(
        [
            (10, 11, 9, 10),
            (10, 12, 9, 11),
            (11, 13, 10, 12),
            (12, 14, 11, 13),
            (13, 15, 12, 14),
            (14, 16, 13, 15),
        ]
    )
    values = rule.values(bars)
    # 常数序列下标准差为 0；这里用后 3 根 close 13,14,15 验算
    assert values["sd3"].iloc[-1] == pytest.approx(pd.Series([13, 14, 15]).std(ddof=0))
    assert values["bm"].iloc[-1] == pytest.approx(14.0)
    assert values["bw"].iloc[-1] == pytest.approx(2 * 2 * values["sd3"].iloc[-1] / 14.0 * 100)
    # 最后 3 根 high=14,15,16 low=11,12,13
    assert values["dm"].iloc[-1] == pytest.approx((16 + 11) / 2)
    # ROC 用 3 根前的收盘 12 对比 15
    assert values["r3"].iloc[-1] == pytest.approx((15 - 12) / 12 * 100)
    assert values["v3"].iloc[-1] == pytest.approx(100.0)
    assert values["obv"].iloc[-1] == pytest.approx(500.0)  # 每根成交量 100，五根上涨
    assert 0 <= values["k3"].iloc[-1] <= 100
    assert -100 <= values["wr3"].iloc[-1] <= 0
    assert values["ap3"].iloc[-1] > 0
    assert pd.notna(values["kc"].iloc[-1])
    assert pd.notna(values["dif"].iloc[-1]) and pd.notna(values["dea"].iloc[-1])
    assert pd.notna(values["pdi"].iloc[-1]) and pd.notna(values["mdi"].iloc[-1])
    assert pd.notna(values["cci3"].iloc[-1])
    assert pd.notna(values["sar"].iloc[-1])


def _session_bars() -> pd.DataFrame:
    index = pd.to_datetime(
        [
            "2026-01-05 09:00",
            "2026-01-05 09:15",
            "2026-01-05 09:30",
            "2026-01-06 09:00",
            "2026-01-06 09:15",
            "2026-01-06 09:30",
        ]
    )
    return pd.DataFrame(
        {
            "open": [10, 11, 12, 20, 21, 22],
            "high": [11, 12, 13, 21, 22, 23],
            "low": [9, 10, 11, 19, 20, 21],
            "close": [10.5, 11.5, 12.5, 20.5, 21.5, 22.5],
            "volume": [100, 200, 300, 100, 200, 300],
        },
        index=index,
    )


def test_session_indicators_reset_each_trading_day() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {
                "d_open": {"kind": "day_open"},
                "pdh": {"kind": "prev_day_high"},
                "pdl": {"kind": "prev_day_low"},
                "pdc": {"kind": "prev_day_close"},
                "vwap": {"kind": "vwap"},
                "orh": {"kind": "opening_range_high", "period": 2},
                "orl": {"kind": "opening_range_low", "period": 2},
            },
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "none"},
        }
    )
    values = rule.values(_session_bars())

    assert list(values["d_open"]) == [10, 10, 10, 20, 20, 20]
    assert pd.isna(values["pdh"].iloc[0])
    assert list(values["pdh"].iloc[3:]) == [13, 13, 13]
    assert list(values["pdl"].iloc[3:]) == [9, 9, 9]
    assert list(values["pdc"].iloc[3:]) == [12.5, 12.5, 12.5]
    # 当日 VWAP：前三天典型价 10.1667/11.1667/12.1667，量 100/200/300
    assert values["vwap"].iloc[2] == pytest.approx(11.5)
    # 开盘区间取前 2 根，之后固定
    assert list(values["orh"].iloc[:3]) == [11, 12, 12]
    assert list(values["orl"].iloc[:3]) == [9, 9, 9]


def test_session_indicator_requires_time_index() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {"d_open": {"kind": "day_open"}},
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "none"},
        }
    )
    bars = _bars([10, 11, 12]).reset_index(drop=True)
    with pytest.raises(RuleConfigError) as exc:
        rule.values(bars)
    assert "时间" in str(exc.value)


def test_expr_rolling_aggregates_and_math_functions() -> None:
    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {
                "avg3": {"kind": "expr", "formula": "avg(close, 3)"},
                "std3": {"kind": "expr", "formula": "std(close, 3)"},
                "hi3": {"kind": "expr", "formula": "hhv(close, 3)"},
                "lo3": {"kind": "expr", "formula": "llv(close, 3)"},
                "tot3": {"kind": "expr", "formula": "sum(close, 3)"},
                "root": {"kind": "expr", "formula": "sqrt(close)"},
                "sign": {"kind": "expr", "formula": "sign(close - 10)"},
                "clip": {"kind": "expr", "formula": "clip(close, 10, 12)"},
            },
            "entry": {"long": {"gt": {"left": "close", "right": 0}}},
            "stop": {"kind": "none"},
        }
    )
    values = rule.values(_bars([10, 12, 14]))
    assert values["avg3"].iloc[-1] == pytest.approx(12.0)
    assert values["std3"].iloc[-1] == pytest.approx(pd.Series([10, 12, 14]).std(ddof=0))
    assert values["hi3"].iloc[-1] == pytest.approx(14)
    assert values["lo3"].iloc[-1] == pytest.approx(10)
    assert values["tot3"].iloc[-1] == pytest.approx(36)
    assert values["root"].iloc[-1] == pytest.approx(14**0.5)
    assert values["sign"].iloc[0] == pytest.approx(0)
    assert values["sign"].iloc[-1] == pytest.approx(1)
    assert values["clip"].iloc[-1] == pytest.approx(12)


def test_expr_rolling_window_must_be_constant_integer() -> None:
    raw = _with_entry({"gt": {"left": "close", "right": 0}})
    raw["indicators"] = {"x": {"kind": "expr", "formula": "avg(close, period)"}}
    with pytest.raises(RuleConfigError):
        compile_rule(raw)
    raw["indicators"] = {"x": {"kind": "expr", "formula": "avg(close, 1)"}}
    with pytest.raises(RuleConfigError):
        compile_rule(raw)


def test_break_high_and_break_low_conditions() -> None:
    rule = compile_rule(
        _level_rule(
            {"hh3": {"kind": "highest", "period": 3}},
            {"long": {"break_high": {"period": 3}}, "short": {"break_low": {"period": 3}}},
        )
    )
    rising = _ohlc(
        [(10, 11, 9, 10), (10, 12, 9, 11), (11, 13, 10, 12), (12, 14, 11, 13), (13, 15, 12, 14)]
    )
    assert rule.signal(rising) == "long"
    falling = _ohlc(
        [(10, 11, 9, 10), (10, 11, 9, 10), (10, 12, 9, 11), (11, 13, 10, 12), (12, 13, 8, 9)]
    )
    assert rule.signal(falling) == "short"

    ref_rule = compile_rule(
        _level_rule(
            {"hh3": {"kind": "highest", "period": 3}},
            {"long": {"break_high": {"ref": {"prev": "hh3", "n": 1}}}},
        )
    )
    assert ref_rule.signal(rising) == "long"


def test_sustained_and_bars_since_conditions() -> None:
    sustained = compile_rule(
        _with_entry(
            {
                "sustained": {
                    "cond": {"gt": {"left": "close", "right": 10}},
                    "bars": 3,
                }
            }
        )
    )
    assert sustained.signal(_bars([11, 12, 13])) == "long"
    assert sustained.signal(_bars([11, 12, 9])) is None

    since = compile_rule(
        _with_entry(
            {
                "bars_since": {
                    "cond": {"gt": {"left": "close", "right": 14}},
                    "within": 3,
                }
            }
        )
    )
    assert since.signal(_bars([11, 15, 9, 8])) == "long"
    assert since.signal(_bars([9, 10, 11, 12])) is None


def test_time_condition_limits_entry_window() -> None:
    def frame(times: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": [10] * len(times),
                "high": [11] * len(times),
                "low": [9] * len(times),
                "close": [10] * len(times),
                "volume": [1] * len(times),
            },
            index=pd.to_datetime(times),
        )

    rule = compile_rule(
        {
            "schema": 1,
            "indicators": {},
            "entry": {
                "long": {
                    "all": [
                        {"gt": {"left": "close", "right": 0}},
                        {"time": {"from": "09:05", "to": "09:20"}},
                    ]
                }
            },
            "stop": {"kind": "none"},
        }
    )
    assert rule.signal(frame(["2026-01-05 09:00", "2026-01-05 09:05", "2026-01-05 09:15"])) == "long"
    assert rule.signal(frame(["2026-01-05 09:00", "2026-01-05 09:15", "2026-01-05 09:30"])) is None

    overnight = compile_rule(
        {
            "schema": 1,
            "indicators": {},
            "entry": {
                "long": {
                    "all": [
                        {"gt": {"left": "close", "right": 0}},
                        {"time": {"from": "21:00", "to": "02:30"}},
                    ]
                }
            },
            "stop": {"kind": "none"},
        }
    )
    # 跨零点夜盘：21:15 → 23:00 → 次日 01:30 都算在窗口内
    assert overnight.signal(frame(["2026-01-05 21:15", "2026-01-05 23:00", "2026-01-06 01:30"])) == "long"


TRAIL_RULE = {
    "schema": 1,
    "name": "移动止损",
    "indicators": {"atr3": {"kind": "atr", "period": 3}},
    "entry": {"long": {"gt": {"left": "close", "right": 1000}}},
    "stop": {"kind": "percent", "pct": 2},
    "trail": {"kind": "percent", "pct": 2},
    "breakeven": {"atr": 1.0},
}


def test_trail_and_breakeven_only_tighten_stop() -> None:
    strategy = RuleStrategy(_cfg(), rule_data=TRAIL_RULE)
    now = datetime(2026, 1, 5, 9, 30)
    flat = _ohlc([(10, 10.1, 9.9, 10)] * 5)
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 10, 1, initial_stop=9.8)

    jumped = pd.concat([flat, _ohlc([(11.9, 12.1, 11.8, 12.0)])])
    decision = strategy.evaluate("bu2612", jumped, now=now)
    assert decision.action == StrategyAction.HOLD
    assert decision.stop_price == pytest.approx(12.0 * 0.98)

    slipped = _ohlc([(10, 10.1, 9.9, 10)] * 5 + [(11.9, 12.1, 11.8, 12.0), (11.9, 11.95, 11.85, 11.9)])
    decision = strategy.evaluate("bu2612", slipped, now=now)
    assert decision.stop_price == pytest.approx(12.0 * 0.98)
    # 保本止损不会低于开仓价
    assert strategy.positions["bu2612"].trail_stop >= 10.0


def test_breakeven_lifts_stop_to_entry() -> None:
    rule = dict(TRAIL_RULE)
    rule.pop("trail")
    strategy = RuleStrategy(_cfg(), rule_data=rule)
    now = datetime(2026, 1, 5, 9, 30)
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 10, 1, initial_stop=9.8)
    bars = _ohlc([(10, 10.1, 9.9, 10)] * 5 + [(11.9, 12.1, 11.8, 12.0)])
    decision = strategy.evaluate("bu2612", bars, now=now)
    assert decision.stop_price == pytest.approx(10.0)


def test_max_hold_bars_closes_position() -> None:
    rule = {
        "schema": 1,
        "name": "限持仓",
        "indicators": {"ma3": {"kind": "sma", "period": 3}},
        "entry": {"long": {"gt": {"left": "close", "right": 0}}},
        "stop": {"kind": "percent", "pct": 5},
        "max_hold_bars": 2,
    }
    strategy = RuleStrategy(_cfg(), rule_data=rule)
    bars = _ohlc([(10, 10.1, 9.9, 10)] * 8)
    now = datetime(2026, 1, 5, 9, 30)

    assert strategy.evaluate("bu2612", bars.iloc[:5], now=now).action == StrategyAction.BUY_OPEN
    strategy.on_filled("bu2612", StrategyAction.BUY_OPEN, 10, 1, initial_stop=9.0)
    assert strategy.evaluate("bu2612", bars.iloc[:6], now=now).action == StrategyAction.HOLD
    closed = strategy.evaluate("bu2612", bars.iloc[:7], now=now)
    assert closed.action == StrategyAction.CLOSE
    assert "持仓已满 2 根" in closed.reason


def test_htf_filter_blocks_entries() -> None:
    rule = {
        "schema": 1,
        "name": "日线过滤",
        "indicators": {"ma3": {"kind": "sma", "period": 3}},
        "entry": {"long": {"gt": {"left": "close", "right": 0}}},
        "stop": {"kind": "percent", "pct": 5},
        "filters_htf": {"gt": {"left": "close", "right": "ma3"}},
    }
    signal_bars = _bars([10, 10, 10, 10, 10])
    now = datetime(2026, 1, 5, 9, 30)

    blocked = RuleStrategy(_cfg(), rule_data=rule)
    decision = blocked.evaluate("bu2612", signal_bars, now=now, trail_bars=_bars([6, 5, 4, 3, 2, 1]))
    assert decision.action == StrategyAction.HOLD
    assert "高周期过滤" in decision.reason

    allowed = RuleStrategy(_cfg(), rule_data=rule)
    assert allowed.evaluate("bu2612", signal_bars, now=now, trail_bars=_bars([1, 2, 3, 4, 5, 6])).action == (
        StrategyAction.BUY_OPEN
    )

    missing = RuleStrategy(_cfg(), rule_data=rule)
    decision = missing.evaluate("bu2612", signal_bars, now=now, trail_bars=None)
    assert decision.action == StrategyAction.HOLD
    assert "高周期" in decision.reason

