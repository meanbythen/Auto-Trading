"""PERF-04 / PERF-06：单品种与 10 品种一轮扫描的计算耗时。

只测策略评估与行情处理这一段，不把网络下载算进代码性能；
下载延迟由 P3.3 的长稳工具在真实进程里单独观察。
"""

from __future__ import annotations

import time

import pytest

from src.strategy import factory as strategy_factory

pytestmark = pytest.mark.perf

SYMBOLS = [
    "bu2601",
    "rb2601",
    "cu2601",
    "al2601",
    "zn2601",
    "au2601",
    "ag2601",
    "ru2601",
    "fu2601",
    "sp2601",
]
WARMUP = 20


def test_perf_single_symbol_evaluate(perf_recorder, perf_cfg, perf_bars) -> None:
    strategy = strategy_factory.create_strategy(perf_cfg)
    frame = perf_bars.iloc[-600:]
    now = frame.index[-1]
    samples: list[float] = []
    for _ in range(220):
        started = time.perf_counter()
        strategy.evaluate("bu2612", frame, now=now)
        samples.append((time.perf_counter() - started) * 1000.0)
    perf_recorder.record(
        "PERF-04",
        samples[WARMUP:],
        unit="ms",
        threshold=2000,
        relative_floor=5,
        note="单品种 600 根已收盘 K 线，evaluate 一次",
    )


def test_perf_ten_symbol_round(perf_recorder, perf_cfg, perf_bars) -> None:
    strategy = strategy_factory.create_strategy(perf_cfg)
    frame = perf_bars.iloc[-600:]
    now = frame.index[-1]
    samples: list[float] = []
    for _ in range(50):
        started = time.perf_counter()
        for symbol in SYMBOLS:
            strategy.evaluate(symbol, frame, now=now)
        samples.append((time.perf_counter() - started) * 1000.0)
    perf_recorder.record(
        "PERF-06",
        samples[WARMUP:],
        unit="ms",
        threshold=8000,
        relative_floor=5,
        note="10 品种各 evaluate 一次，算一轮扫描",
    )
