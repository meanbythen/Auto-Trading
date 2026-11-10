"""自适应权重：慢、有上下限、笔数不够不动。不测开仓公式。"""

from __future__ import annotations

from src.strategy.adaptive_weights import update_weights
from src.strategy.factory import create_strategy
from src.strategy.multi_factor import MultiFactorStrategy
from src.strategy.multi_factor_adapt import MultiFactorAdaptStrategy


def _base() -> dict[str, float]:
    return {"ma": 0.30, "turtle": 0.25, "boll": 0.25, "sar": 0.20}


def test_weights_stay_until_lookback_filled() -> None:
    """不到 20 笔，权重必须原样，避免前几笔把系统拧歪。"""
    rows = [{"ma": 10.0, "turtle": 0.0, "boll": 0.0, "sar": 0.0}] * 5
    new_w, _pnl, dead = update_weights(_base(), _base(), rows, lookback=20)
    assert abs(new_w["ma"] - 0.30) < 1e-9
    assert dead == set()


def test_losing_factor_cannot_take_all_weight() -> None:
    """均线窗口大亏时权重会降，但不能低于下限，也不能一步跳光。"""
    rows = [{"ma": -10.0, "turtle": 2.0, "boll": 2.0, "sar": 2.0}] * 20
    new_w, pnl, dead = update_weights(_base(), _base(), rows, lookback=20, max_step=0.03)
    assert "ma" in dead
    assert pnl["ma"] < 0
    assert new_w["ma"] >= 0.05 - 1e-9
    assert new_w["ma"] <= 0.30 + 1e-9
    assert 0.30 - new_w["ma"] <= 0.03 + 1e-9
    assert abs(sum(new_w.values()) - 1.0) < 1e-9


def test_winning_factor_hits_cap_slowly() -> None:
    """均线窗口大赚时也不能一口吃到超过上限；单次最多挪步长。"""
    rows = [{"ma": 20.0, "turtle": -1.0, "boll": -1.0, "sar": -1.0}] * 20
    new_w, _pnl, _dead = update_weights(
        _base(), _base(), rows, lookback=20, max_weight=0.40, max_step=0.03
    )
    assert new_w["ma"] <= 0.40 + 1e-9
    assert new_w["ma"] <= 0.30 + 0.03 + 1e-9


def test_adapt_engine_is_separate_from_fixed() -> None:
    """新引擎能建出来；原加权打分仍是原来的类。"""
    cfg = {
        "strategy": {
            "engine": "multi_factor_adapt",
            "timeframe": "5",
            "symbols": ["fu2701"],
            "multi_factor_adapt": {"lookback_trades": 20, "adapt_enabled": True},
        },
        "trade": {"executor": "sim", "volume": 1, "symbol": "fu2701"},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "dry_run": True,
    }
    adapt = create_strategy(cfg)
    assert isinstance(adapt, MultiFactorAdaptStrategy)
    cfg["strategy"]["engine"] = "multi_factor"
    fixed = create_strategy(cfg)
    assert isinstance(fixed, MultiFactorStrategy)
    assert not isinstance(fixed, MultiFactorAdaptStrategy)


def test_on_trade_closed_moves_weight_only_after_lookback() -> None:
    """模拟 20 笔均线主导的亏损，权重才下降。"""
    cfg = {
        "strategy": {
            "engine": "multi_factor_adapt",
            "timeframe": "5",
            "symbols": ["fu2701"],
            "multi_factor_adapt": {"lookback_trades": 20, "max_step": 0.03},
        },
        "trade": {"executor": "sim", "volume": 1, "symbol": "fu2701"},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "dry_run": True,
    }
    strat = create_strategy(cfg)
    mixed = {"ma": -10.0, "turtle": 2.0, "boll": 2.0, "sar": 2.0}
    for _ in range(19):
        strat._pnl_rows.append(mixed)
    before = strat.params.w_ma
    strat._last_closed_snap["fu2701"] = {
        "score_ma": 1.0,
        "score_turtle": 0.0,
        "score_boll": 0.0,
        "score_sar": 0.0,
    }
    # 第 20 笔：均线再亏，其它因子窗口仍是正的，权重才会离开底仓。
    strat.on_trade_closed("fu2701", -80.0)
    assert strat.params.w_ma < before


def test_adapt_does_not_move_weight_without_closes() -> None:
    """没有平仓回调时，权重就是出厂值。"""
    cfg = {
        "strategy": {
            "engine": "multi_factor_adapt",
            "timeframe": "5",
            "symbols": ["fu2701"],
        },
        "trade": {"executor": "sim", "volume": 1, "symbol": "fu2701"},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "dry_run": True,
    }
    strat = create_strategy(cfg)
    assert abs(strat.params.w_ma - 0.30) < 1e-6
    assert abs(strat.params.w_sar - 0.20) < 1e-6
