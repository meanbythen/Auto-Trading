# -*- coding: utf-8 -*-
"""滑点灵敏度的纯逻辑：分水岭、利润边缘、输出文本。"""
from src.backtest.sensitivity import breakeven_slip, edge_ticks_per_trade, format_slip_scan

_ROWS = [
    {"slip": 0.0, "trades": 100, "win_rate": 0.46, "net_pnl": 2000.0, "profit_factor": 1.15, "max_drawdown": 5000},
    {"slip": 0.5, "trades": 100, "win_rate": 0.44, "net_pnl": 1000.0, "profit_factor": 1.07, "max_drawdown": 6000},
    {"slip": 1.0, "trades": 100, "win_rate": 0.43, "net_pnl": -10.0, "profit_factor": 1.00, "max_drawdown": 7000},
    {"slip": 2.0, "trades": 100, "win_rate": 0.41, "net_pnl": -2000.0, "profit_factor": 0.87, "max_drawdown": 9000},
]


def test_breakeven_interpolates():
    # 0.5 跳时 +1000，1 跳时 -10：分水岭插在两者之间（-10 很小，插值点贴近 1 跳，四舍五入到 1.0 也算对）
    turn = breakeven_slip(_ROWS)
    assert turn is not None
    assert 0.5 < turn <= 1.0


def test_breakeven_none_when_always_losing():
    rows = [dict(r, net_pnl=-100.0) for r in _ROWS]
    assert breakeven_slip(rows) is None


def test_edge_ticks():
    # 滑点0：100 笔共 +2000 元 → 每笔 20 元；每跳10元 → 来回2跳 → 单边1跳
    assert edge_ticks_per_trade(_ROWS, tick=1.0, mult=10.0) == 1.0
    # 滑点0就亏 → 没有边缘可言
    rows = [dict(r, net_pnl=-50.0) for r in _ROWS]
    assert edge_ticks_per_trade(rows, tick=1.0, mult=10.0) is None


def test_format_marks_current_and_verdict():
    scan = {"rows": _ROWS, "bars": 10000, "data_from": "2026-03-25 11:15", "data_to": "2026-10-30 23:00"}
    text = format_slip_scan(scan, symbol="fu2701", current_slip=1.0, tick=1.0, mult=10.0)
    assert "滑点灵敏度  fu2701" in text
    assert "← 当前" in text
    assert "分水岭" in text
    assert "利润边缘" in text
