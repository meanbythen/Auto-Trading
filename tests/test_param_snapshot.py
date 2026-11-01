"""参数回溯要能带回各策略自己的参数，不能只留某一套。"""

from src.backtest.optimizer import apply_params_to_config, apply_run_switches
from src.backtest.param_history import (
    FACTORY_DEFAULT_SNAPSHOT,
    apply_engine_snapshot,
    apply_snapshot_to_config,
    extract_strategy_snapshot,
)


def test_kept_engines_survive_snapshot() -> None:
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
            "daily_body": {"eth_minutes": 20, "initial_atr_mult": 1.0, "reverse_atr_mult": 3.0},
            "trend_follow": {"entry_bars": 48, "atr_mult": 2.5},
        },
        "trade": {"symbol": "bu2612"},
    }
    snap = extract_strategy_snapshot(cfg)
    restored = apply_snapshot_to_config({"strategy": {}, "trade": {}}, snap)
    st = restored["strategy"]
    assert st["daily_body"]["eth_minutes"] == 20
    assert st["daily_body"]["initial_atr_mult"] == 1.0
    assert restored["data"]["source"] == "akshare"
    assert st["trend_follow"]["entry_bars"] == 48


def test_kept_engine_aliases_keep_their_name() -> None:
    """还在的策略再回溯时，仍是原来的名字，不会被改成别的策略。"""
    for old_engine, section, key, value in (
        ("supertrend", "supertrend", "multiplier", 3.5),
        ("single_ma", "single_ma", "ma_period", 60),
        ("dual_ma", "dual_ma", "short_period", 10),
        ("morning_box_a", "morning_box_a", "be_box_mult", 0.8),
    ):
        restored = apply_snapshot_to_config(
            {"strategy": {}, "trade": {}},
            {"engine": old_engine, "timeframe": "60", section: {key: value}},
        )
        st = restored["strategy"]
        assert st["engine"] == old_engine
        assert st[section][key] == value


def test_daily_body_snapshot_comes_back() -> None:
    """日内实体回溯时仍是这套。"""
    restored = apply_snapshot_to_config(
        {"strategy": {}, "trade": {}},
        {"engine": "daily_body", "timeframe": "15", "daily_body": {"initial_atr_mult": 0.7}},
    )
    st = restored["strategy"]
    assert st["engine"] == "daily_body"
    assert st["daily_body"]["initial_atr_mult"] == 0.7


def test_factory_reset_only_touches_the_selected_strategy() -> None:
    """出厂默认只恢复选中的那一套，不改正在用的策略、品种、周期，也不改另外几套。"""
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "D",
            "trail_timeframe": "D",
            "symbols": ["SA2701"],
            "daily_body": {"eth_minutes": 20, "initial_atr_mult": 1.0},
            "trend_follow": {"entry_bars": 48, "atr_mult": 2.5},
        },
        "trade": {"symbol": "SA2701"},
    }
    restored = apply_engine_snapshot(cfg, FACTORY_DEFAULT_SNAPSHOT, "trend_follow")
    st = restored["strategy"]
    assert st["engine"] == "daily_body"
    assert st["timeframe"] == "D"
    assert st["symbols"] == ["SA2701"]
    assert restored["trade"]["symbol"] == "SA2701"
    assert st["trend_follow"]["entry_bars"] == 36
    assert st["trend_follow"]["atr_mult"] == 2.0
    assert st["daily_body"]["eth_minutes"] == 20
    assert st["daily_body"]["initial_atr_mult"] == 1.0


def test_optimize_write_stays_inside_one_strategy() -> None:
    """把优化结果写入时，只改选中策略的参数，不把正在用的策略换掉。"""
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "symbols": ["bu2612"],
            "trend_follow": {"entry_bars": 36, "atr_mult": 2.0},
            "daily_body": {"eth_minutes": 20},
        },
        "trade": {"symbol": "bu2612"},
    }
    updated = apply_params_to_config(cfg, {"entry_bars": 48, "atr_mult": 2.5}, engine="trend_follow")
    st = updated["strategy"]
    assert st["engine"] == "daily_body"
    assert st["timeframe"] == "5"
    assert st["symbols"] == ["bu2612"]
    assert st["trend_follow"]["entry_bars"] == 48
    assert st["daily_body"]["eth_minutes"] == 20


def test_writing_daily_body_keeps_the_flatten_switch_used_to_score() -> None:
    """日内实体的优化结果要连同打分时的收盘清仓开关一起留下。不改策略名。"""
    cfg = {
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "D",
            "symbols": ["SA2701"],
            "flatten_at_close": False,
            "daily_body": {"eth_minutes": 20, "initial_atr_mult": 1.0},
        }
    }
    updated = apply_run_switches(
        cfg,
        "daily_body",
        flatten_at_close=True,
    )
    st = updated["strategy"]
    assert st["engine"] == "trend_follow"
    assert st["symbols"] == ["SA2701"]
    assert st["flatten_at_close"] is True
    assert st["daily_body"]["eth_minutes"] == 20
    assert st["daily_body"]["initial_atr_mult"] == 1.0


def test_writing_trend_does_not_touch_the_shared_flatten_switch() -> None:
    """趋势跟随打分时不听总开关，写入时也不能把总开关改掉。"""
    cfg = {"strategy": {"engine": "daily_body", "flatten_at_close": True, "trend_follow": {"entry_bars": 36}}}
    updated = apply_run_switches(
        cfg,
        "trend_follow",
        flatten_at_close=False,
    )
    st = updated["strategy"]
    assert st["flatten_at_close"] is True
    assert st["engine"] == "daily_body"


def test_restore_daily_body_brings_the_flatten_switch_back() -> None:
    """回溯日内实体时，把当时的收盘清仓一并带回。策略名不动。"""
    cfg = {"strategy": {"engine": "trend_follow", "flatten_at_close": False, "daily_body": {}}}
    snap = {"flatten_at_close": True, "daily_body": {"eth_minutes": 30}}
    out = apply_engine_snapshot(cfg, snap, "daily_body")
    assert out["strategy"]["engine"] == "trend_follow"
    assert out["strategy"]["flatten_at_close"] is True
    assert out["strategy"]["daily_body"]["eth_minutes"] == 30


def test_restore_trend_does_not_change_flatten() -> None:
    cfg = {"strategy": {"flatten_at_close": False, "trend_follow": {"entry_bars": 36}}}
    snap = {"flatten_at_close": True, "trend_follow": {"entry_bars": 48}}
    out = apply_engine_snapshot(cfg, snap, "trend_follow")
    assert out["strategy"]["flatten_at_close"] is False
    assert out["strategy"]["trend_follow"]["entry_bars"] == 48
