"""最终半年冻结、成交打乱：搜索碰不到冻结段，看过一次不能再当验收。"""

from types import SimpleNamespace

import pandas as pd

from src.backtest.frozen_oos import (
    attempt_key,
    freeze_start_from_bars,
    has_seen_freeze,
    record_freeze_attempt,
    split_searchable_frozen,
)
from src.backtest.monte_carlo import judge_monte_carlo, run_trade_monte_carlo
from src.backtest.optimizer import _gate_freeze_and_monte_carlo, _prepare_searchable_bars


def _daily_bars(n: int = 400, start: str = "2025-01-01") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1] * n,
        },
        index=pd.date_range(start, periods=n, freq="D"),
    )


def test_freeze_cuts_last_half_year_out_of_search() -> None:
    """最后 180 天不进搜索，前面留给挑参数。"""
    bars = _daily_bars(400)
    start = freeze_start_from_bars(bars, 180)
    searchable, frozen = split_searchable_frozen(bars, start)
    assert len(searchable) + len(frozen) == 400
    assert searchable.index[-1] < start
    assert frozen.index[0] >= start
    assert len(frozen) >= 170


def test_prepare_searchable_raises_when_too_short() -> None:
    """只有两天的行情，冻结半年后前面不够挑参数。"""
    bars = pd.DataFrame(
        {"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0], "close": [1.0, 1.0]},
        index=pd.date_range("2026-01-01", periods=2, freq="D"),
    )
    try:
        _prepare_searchable_bars({"backtest": {"freeze_final_oos": True, "freeze_final_days": 180}}, bars, None)
    except RuntimeError as exc:
        assert "不够用来挑参数" in str(exc)
    else:
        raise AssertionError("短行情应当直接报错")


def test_second_look_at_same_freeze_cannot_pass(tmp_path, monkeypatch) -> None:
    """同一段最终半年看过一次，第二次不再当验收。"""
    bars = _daily_bars(400)
    cfg = {
        "backtest": {
            "freeze_final_oos": True,
            "freeze_final_days": 180,
            "freeze_registry_path": str(tmp_path / "frozen.json"),
        },
        "risk": {"capital": 100000},
        "strategy": {"timeframe": "D"},
        "evaluation": {"gate_on_optimize": True, "enabled": True},
    }
    searchable, _trail, meta = _prepare_searchable_bars(cfg, bars, None)
    assert len(searchable) >= 80
    key = attempt_key(
        engine="trend_follow",
        symbol="fu2701",
        period="D",
        freeze_start=str(meta["start"]),
        freeze_end=str(meta["end"]),
    )
    record_freeze_attempt(tmp_path / "frozen.json", key)
    assert has_seen_freeze(tmp_path / "frozen.json", key)

    monkeypatch.setattr(
        "src.backtest.optimizer._holdout_perf_for_params",
        lambda *_a, **_k: ({"net_pnl": 999.0, "trades": 20, "profit_factor": 2.0}, []),
    )
    accepted, reason, freeze_box, _mc = _gate_freeze_and_monte_carlo(
        cfg,
        accepted=True,
        reason="检验过关。",
        symbol="fu2701",
        engine_name="trend_follow",
        params={"entry_bars": 36},
        bars_signal=bars,
        bars_trail=None,
        freeze_meta=meta,
        train_net=1000.0,
        oos_trades=[],
        gate=True,
    )
    assert accepted is False
    assert freeze_box["already_seen"] is True
    assert "已经看过" in reason


def test_first_freeze_look_is_recorded_even_if_fail(tmp_path, monkeypatch) -> None:
    """第一次看最终半年，就算没过关也记下来，不能再看一次改到过关。"""
    bars = _daily_bars(400)
    cfg = {
        "backtest": {
            "freeze_final_oos": True,
            "freeze_final_days": 180,
            "freeze_registry_path": str(tmp_path / "frozen.json"),
        },
        "risk": {"capital": 100000},
        "strategy": {"timeframe": "D"},
        "evaluation": {"gate_on_optimize": True, "enabled": True},
    }
    _searchable, _trail, meta = _prepare_searchable_bars(cfg, bars, None)
    monkeypatch.setattr(
        "src.backtest.optimizer._holdout_perf_for_params",
        lambda *_a, **_k: ({"net_pnl": -900.0, "trades": 8, "profit_factor": 0.4}, []),
    )
    accepted, reason, freeze_box, _mc = _gate_freeze_and_monte_carlo(
        cfg,
        accepted=True,
        reason="检验过关。",
        symbol="fu2701",
        engine_name="trend_follow",
        params={"entry_bars": 36},
        bars_signal=bars,
        bars_trail=None,
        freeze_meta=meta,
        train_net=1000.0,
        oos_trades=[],
        gate=True,
    )
    assert accepted is False
    assert "最终半年" in reason
    assert freeze_box.get("already_seen") is False
    key = attempt_key(
        engine="trend_follow",
        symbol="fu2701",
        period="D",
        freeze_start=str(meta["start"]),
        freeze_end=str(meta["end"]),
    )
    assert has_seen_freeze(tmp_path / "frozen.json", key)


def test_monte_carlo_rejects_weak_expectancy() -> None:
    """赢的少、亏的大，重抽后经常变成亏钱，应当拦住。"""
    pnls = [20.0, 20.0, -50.0] * 8
    report = run_trade_monte_carlo(pnls, capital=10_000, n_runs=400, seed=1)
    assert report["skipped"] is False
    ok, reason = judge_monte_carlo(report)
    assert ok is False
    assert "运气" in reason


def test_monte_carlo_accepts_stable_edge() -> None:
    """小赚明显多于小亏时，重抽后多数仍赚钱。"""
    pnls = [30.0, -10.0] * 20
    report = run_trade_monte_carlo(pnls, capital=10_000, n_runs=200, seed=2)
    ok, reason = judge_monte_carlo(report)
    assert ok is True
    assert "仍为正" in reason


def test_monte_carlo_skips_few_trades() -> None:
    """笔数太少不算，避免三四笔假装稳健。"""
    report = run_trade_monte_carlo([10.0, -5.0, 8.0], capital=10_000, n_runs=50)
    assert report["skipped"] is True
    ok, reason = judge_monte_carlo(report)
    assert ok is True
    assert "少于" in reason


def test_failed_search_does_not_peek_frozen(monkeypatch) -> None:
    """训练/检验没过关时，不跑最终半年，免得偷看后再调参。"""
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("不该去看最终半年")

    monkeypatch.setattr("src.backtest.optimizer._holdout_perf_for_params", boom)
    bars = _daily_bars(400)
    meta = {
        "enabled": True,
        "days": 180,
        "start": "2025-07-01",
        "end": "2025-12-31",
        "bars": 180,
        "searchable_bars": 220,
        "freeze_start_ts": pd.Timestamp("2025-07-01"),
    }
    accepted, reason, freeze_box, _mc = _gate_freeze_and_monte_carlo(
        {"risk": {"capital": 100000}, "strategy": {"timeframe": "D"}, "backtest": {"freeze_final_oos": True}},
        accepted=False,
        reason="前一段没有赚到钱。",
        symbol="fu2701",
        engine_name="trend_follow",
        params={"entry_bars": 36},
        bars_signal=bars,
        bars_trail=None,
        freeze_meta=meta,
        train_net=0.0,
        oos_trades=[SimpleNamespace(pnl=10.0) for _ in range(10)],
        gate=True,
    )
    assert called["n"] == 0
    assert accepted is False
    assert freeze_box.get("performance") == {}
    assert "前一段" in reason
