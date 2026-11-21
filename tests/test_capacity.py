"""容量估算：按成交量算单品种最多能下多少手。"""

from __future__ import annotations

from src.backtest.capacity import estimate_capacity, format_capacity_lines


def _bars(count: int, *, volume: float = 1000.0, price: float = 1000.0) -> list[dict]:
    return [
        {"datetime": f"2024-01-{index:02d}", "close": price, "volume": volume}
        for index in range(1, count + 1)
    ]


def test_estimate_capacity_uses_average_volume_and_participation():
    estimate = estimate_capacity(
        _bars(30),
        participation=0.01,
        lookback=20,
        multiplier=10,
        margin_rate=0.1,
    )
    assert estimate["max_lots"] == 10
    assert estimate["bars_used"] == 20
    assert estimate["notional"] == 100_000.0
    assert estimate["margin"] == 10_000.0
    assert estimate["note"] == ""


def test_short_sample_is_marked():
    estimate = estimate_capacity(_bars(5), participation=0.01)
    assert "不足" in estimate["note"]


def test_missing_volume_cannot_estimate():
    estimate = estimate_capacity(_bars(30, volume=0))
    assert estimate["max_lots"] == 0
    assert "成交量缺失" in estimate["note"]
    assert "算不出" in "\n".join(format_capacity_lines("fu2701", estimate))


def test_format_line_contains_lots_and_margin():
    estimate = estimate_capacity(_bars(30), multiplier=10, margin_rate=0.1)
    text = "\n".join(format_capacity_lines("fu2701", estimate))
    assert "建议最多 10 手" in text
    assert "保证金约" in text
