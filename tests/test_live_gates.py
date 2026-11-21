"""回测实盘镜像闸门：默认关时成绩不变；打开后能标出追价放弃。"""

from __future__ import annotations

from datetime import datetime

from src.backtest.engine import run_backtest
from src.backtest.fill import FillModel
from src.backtest.live_gates import format_mirror_gate_lines, inspect_live_open_gate
from tests.test_backtest_fill import _cfg, _trend_bars


def test_short_chase_like_this_morning_is_blocked() -> None:
    """信号 9495、现价 9450 的空单追跌 45 点，超过 10 点上限，应放弃。"""
    hit = inspect_live_open_gate(
        enabled=True,
        is_long=False,
        signal_price=9495.0,
        fill_price=9450.0,
        signal_time=datetime(2026, 11, 19, 9, 15),
        fill_time=datetime(2026, 11, 19, 9, 30),
        atr=80.0,
        period="15",
        max_stale_minutes=90,
        limit_atr=0.5,
        limit_points=10.0,
    )
    assert hit is not None
    assert hit["kind"] == "slip_block"
    assert hit["chase"] == 45.0
    assert hit["limit"] == 10.0


def test_short_better_fill_is_not_chase() -> None:
    """空单现价更高是卖得更贵，不算追价。"""
    hit = inspect_live_open_gate(
        enabled=True,
        is_long=False,
        signal_price=9495.0,
        fill_price=9505.0,
        signal_time=datetime(2026, 11, 19, 9, 15),
        fill_time=datetime(2026, 11, 19, 9, 30),
        atr=80.0,
        period="15",
        max_stale_minutes=90,
        limit_atr=0.5,
        limit_points=10.0,
    )
    assert hit is None


def test_overnight_fill_is_stale_before_slip() -> None:
    """昨收盘对今早开盘，先算过期，不再用追价解释。"""
    hit = inspect_live_open_gate(
        enabled=True,
        is_long=False,
        signal_price=9495.0,
        fill_price=9450.0,
        signal_time=datetime(2026, 11, 18, 15, 0),
        fill_time=datetime(2026, 11, 19, 9, 15),
        atr=80.0,
        period="15",
        max_stale_minutes=90,
        limit_atr=0.5,
        limit_points=10.0,
    )
    assert hit is not None
    assert hit["kind"] == "stale_block"


def test_gate_off_never_blocks() -> None:
    hit = inspect_live_open_gate(
        enabled=False,
        is_long=False,
        signal_price=9495.0,
        fill_price=9450.0,
        signal_time=datetime(2026, 11, 19, 9, 15),
        fill_time=datetime(2026, 11, 19, 9, 30),
        atr=80.0,
        period="15",
        max_stale_minutes=90,
        limit_atr=0.5,
        limit_points=10.0,
    )
    assert hit is None


def test_fill_model_default_does_not_mirror() -> None:
    fill = FillModel.from_cfg({"backtest": {}, "trade": {}})
    assert fill.mirror_live_gates is False
    text = fill.describe()
    assert "追价闸" not in text
    assert "已套实盘" not in text


def test_fill_model_reads_mirror_flag() -> None:
    """界面打开实盘镜像后，成交模型必须带着这个开关。"""
    fill = FillModel.from_cfg({"backtest": {"mirror_live_gates": True}, "trade": {}})
    assert fill.mirror_live_gates is True
    assert "追价闸" in fill.describe()


def test_default_backtest_has_no_mirror_hits() -> None:
    """默认关闸门：趋势回测仍有成交，快照里没有镜像放弃。"""
    result = run_backtest(_cfg(), symbol="bu", bars_signal=_trend_bars(), warmup=20, volume=1)
    assert result.performance.trades >= 1
    assert result.params_snapshot.get("mirror_live_gates") is False
    assert result.params_snapshot.get("mirror_gate_hits") == []


def test_mirror_on_can_skip_opens(monkeypatch) -> None:
    """打开镜像且闸门命中时，这笔开仓记放弃，不再成交。"""

    def always_block(**_kwargs):
        return {
            "kind": "slip_block",
            "label": "开仓滑点过大",
            "signal_price": 130.0,
            "live_price": 180.0,
            "chase": 50.0,
            "limit": 10.0,
        }

    monkeypatch.setattr("src.backtest.live_gates.inspect_live_open_gate", always_block)
    result = run_backtest(
        _cfg(mirror_live_gates=True, slippage_ticks=0),
        symbol="bu",
        bars_signal=_trend_bars(),
        warmup=20,
        volume=1,
    )
    assert result.params_snapshot.get("mirror_live_gates") is True
    assert result.performance.trades == 0
    assert any(row.stage == "实盘镜像" for row in result.rejected)
    lines = format_mirror_gate_lines(result.params_snapshot.get("mirror_gate_hits") or [], enabled=True)
    assert lines and "放弃" in lines[0]
