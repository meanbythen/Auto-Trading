"""PERF-07：固定数据集回测的吞吐与可复现性。"""

from __future__ import annotations

import time

import pytest

from src.backtest.engine import run_backtest

pytestmark = pytest.mark.perf

RUNS = 3


def _trade_key(result) -> list[tuple]:
    return [
        (t.side, t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.volume)
        for t in result.trades
    ]


def test_perf_backtest_throughput(perf_recorder, perf_cfg, perf_bars, perf_workspace) -> None:
    throughput: list[float] = []
    keys: list[list[tuple]] = []
    for _ in range(RUNS):
        started = time.perf_counter()
        result = run_backtest(
            perf_cfg,
            symbol="bu2612",
            bars_signal=perf_bars,
            bars_trail=perf_bars,
            warmup=40,
        )
        elapsed = time.perf_counter() - started
        throughput.append(len(perf_bars) / elapsed)
        keys.append(_trade_key(result))

    assert all(key == keys[0] for key in keys), "同一份数据多次回测结果不一致"
    perf_recorder.record(
        "PERF-07",
        throughput,
        unit="bars/s",
        threshold=1500,
        higher_is_better=True,
        relative_floor=200,
        note=f"{len(perf_bars)} 根 15 分钟 K 线，回测 {RUNS} 次取中位数",
    )
