"""多进程交叉测试的编排测试。

不真正起进程：进程池的行为交给一次真实冒烟测试，这里盯住任务展开、
取消信号和单进程回退，避免改坏顺序或口径。
"""

from __future__ import annotations

import queue
import types

import pytest

import src.screen.picker as picker


class _Perf:
    trades = 4
    win_rate = 50.0
    profit_factor = 1.6
    payoff = 1.4
    avg_win = 120.0
    avg_loss = -80.0
    losses = 2
    expectancy = 20.0
    max_drawdown = 300.0
    net_pnl = 80.0
    score = 1.2
    margin_used = 10000.0


class _Result:
    performance = _Perf()
    params_snapshot = {"margin_used": 10000, "data_from": "2026-08-01", "data_to": "2026-10-31"}
    bars = 200
    evaluation = None


@pytest.fixture(autouse=True)
def _restore_worker_state():
    old = (
        picker._WORKER_CFG,
        picker._WORKER_SPEC,
        picker._WORKER_RUNNER,
        picker._WORKER_CANCEL,
        picker._WORKER_PROGRESS,
    )
    yield
    (
        picker._WORKER_CFG,
        picker._WORKER_SPEC,
        picker._WORKER_RUNNER,
        picker._WORKER_CANCEL,
        picker._WORKER_PROGRESS,
    ) = old


def test_cross_plan_orders_period_symbol_engine():
    settings = {"fixed_volume": 2, "timeframe": "60"}
    universe = [("lh2701", "生猪", 16, False), ("rb2701", "螺纹钢", 10, False)]
    engines = [("dual_ma", "双均线"), ("bollinger", "布林带")]

    _spec, strategies, periods, skipped, chunks = picker._cross_plan(
        settings, universe, engines, ["60", "15"]
    )

    assert strategies == engines
    assert periods == ["60", "15"]
    assert skipped == 0
    # 一块 = 一个品种 × 一个周期，块内是本轮要跑的策略。
    assert [(chunk[1], chunk[3], [entry[0] for entry in chunk[4]]) for chunk in chunks] == [
        ("lh2701", "60", [0, 1]),
        ("rb2701", "60", [2, 3]),
        ("lh2701", "15", [4, 5]),
        ("rb2701", "15", [6, 7]),
    ]
    flattened = [(chunk[1], entry[1], chunk[3]) for chunk in chunks for entry in chunk[4]]
    assert flattened[:4] == [
        ("lh2701", "dual_ma", "60"),
        ("lh2701", "bollinger", "60"),
        ("rb2701", "dual_ma", "60"),
        ("rb2701", "bollinger", "60"),
    ]
    assert [entry[0] for chunk in chunks for entry in chunk[4]] == list(range(8))


def test_cross_plan_drops_thin_contracts():
    settings = {"fixed_volume": 1}
    universe = [("lh2701", "生猪", 16, False), ("jd2701", "鸡蛋", 10, True)]

    _spec, _strategies, _periods, skipped, chunks = picker._cross_plan(
        settings, universe, [("dual_ma", "双均线")], ["60"]
    )

    assert skipped == 1
    assert [chunk[1] for chunk in chunks] == ["lh2701"]


def test_worker_builds_rows_for_whole_chunk(monkeypatch):
    picker._WORKER_CFG = {"backtest": {}}
    picker._WORKER_SPEC = {"fixed_volume": 1, "timeframe": "60"}
    picker._WORKER_RUNNER = lambda *_args, **_kwargs: None
    picker._WORKER_CANCEL = None
    picker._WORKER_PROGRESS = None
    monkeypatch.setattr(
        picker,
        "_run_cross_backtest",
        lambda *_args, **_kwargs: (_Result(), 3),
    )

    chunk = (5, "lh2701", "生猪", "60", [(12, "dual_ma", "双均线"), (13, "bollinger", "布林带")])
    chunk_index, rows, volume = picker._cross_worker_run(chunk)

    assert chunk_index == 5
    assert volume == 3
    assert [index for index, _row in rows] == [12, 13]
    row = rows[0][1]
    assert row["symbol"] == "lh2701"
    assert row["strategy"] == "双均线"
    assert row["trades"] == 4
    assert row["net_pnl"] == 80.0
    assert row["frequency"]
    assert row["market_score"] is None
    assert row["picked"] == ""


def test_worker_reports_progress_per_strategy(monkeypatch):
    picker._WORKER_CFG = {"backtest": {}}
    picker._WORKER_SPEC = {"fixed_volume": 1, "timeframe": "60"}
    picker._WORKER_RUNNER = lambda *_args, **_kwargs: None
    picker._WORKER_CANCEL = None
    progress = queue.Queue()
    picker._WORKER_PROGRESS = progress
    monkeypatch.setattr(
        picker,
        "_run_cross_backtest",
        lambda *_args, **_kwargs: (_Result(), 1),
    )

    chunk = (0, "lh2701", "生猪", "60", [(0, "dual_ma", "双均线"), (1, "bollinger", "布林带")])
    picker._cross_worker_run(chunk)

    assert [progress.get_nowait()[0] for _ in range(2)] == [0, 1]


def test_worker_skips_when_cancelled():
    picker._WORKER_CFG = {"backtest": {}}
    picker._WORKER_CANCEL = types.SimpleNamespace(is_set=lambda: True)

    chunk = (2, "lh2701", "生猪", "60", [(7, "dual_ma", "双均线")])
    assert picker._cross_worker_run(chunk) == (2, [], 1)


def test_small_matrix_falls_back_to_sequential(monkeypatch):
    called = {}

    def fake_cross(*_args, **_kwargs):
        called["yes"] = True
        return {"rows": [], "workers": 9}

    monkeypatch.setattr(picker, "screen_cross", fake_cross)
    result = picker.screen_cross_parallel(
        {},
        {"fixed_volume": 1},
        universe=[("lh2701", "生猪", 16, False)],
        engines=[("dual_ma", "双均线")],
        periods=["60"],
        workers=4,
    )

    assert called.get("yes") is True
    assert result["workers"] == 1


def test_worker_count_env_override(monkeypatch):
    monkeypatch.setenv("AUTO_TRADING_CROSS_WORKERS", "2")
    assert picker.cross_worker_count({"backtest": {"cross_workers": 8}}) == 2
    monkeypatch.setenv("AUTO_TRADING_CROSS_WORKERS", "坏值")
    assert picker.cross_worker_count({"backtest": {"cross_workers": 3}}) == 3
