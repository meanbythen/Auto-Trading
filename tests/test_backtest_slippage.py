# -*- coding: utf-8 -*-
"""回测三档滑点：基线 / 压力 / 实测。"""
from src.account.slippage_log import record_fill
from src.backtest.slippage import build_ladder, ladder_lines


def _cfg(base: float = 1.0, extra: float = 1.0) -> dict:
    return {
        "backtest": {
            "slippage_ticks": base,
            "evaluation": {"slippage_extra_ticks": extra},
        }
    }


def _fill(root, symbol: str, slip: int) -> None:
    record_fill(root, symbol=symbol, action="BUY_OPEN", volume=1,
                theory_price=3000, fill_price=3000 + slip, tick=1,
                executor="ctp", dry_run=False)


def test_ladder_without_log_has_no_actual(tmp_path):
    ladder = build_ladder(_cfg(), symbol="fu2701", root=tmp_path)
    assert ladder.baseline == 1.0
    assert ladder.extra == 1.0
    assert ladder.stress == 2.0
    assert ladder.actual is None
    assert ladder.has_log is False
    assert "还没有滑点记录" in ladder.actual_label


def test_ladder_uses_symbol_real_p70(tmp_path):
    for slip in (1, 2, 3):
        _fill(tmp_path, "fu2701", slip)
    ladder = build_ladder(_cfg(base=1.0, extra=1.0), symbol="fu2705", root=tmp_path)
    assert ladder.actual == 2.5
    assert ladder.actual_n == 3
    assert ladder.symbol == "fu"
    assert "fu 真实 3 笔" in ladder.actual_label


def test_ladder_other_symbol_falls_back_to_none(tmp_path):
    for slip in (1, 2, 3):
        _fill(tmp_path, "fu2701", slip)
    ladder = build_ladder(_cfg(), symbol="lh2701", root=tmp_path)
    assert ladder.actual is None
    assert ladder.has_log is True
    assert "不足 3 笔" in ladder.actual_label


def test_ladder_ignores_gui_assumed_fills(tmp_path):
    for _ in range(3):
        record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                    theory_price=3000, fill_price=3000, tick=1,
                    executor="gui", dry_run=False)
    ladder = build_ladder(_cfg(), symbol="fu2701", root=tmp_path)
    assert ladder.actual is None


def test_ladder_requires_three_samples(tmp_path):
    for slip in (1, 2):
        _fill(tmp_path, "fu2701", slip)
    assert build_ladder(_cfg(), symbol="fu2701", root=tmp_path).actual is None


def test_ladder_default_extra_when_cfg_missing(tmp_path):
    ladder = build_ladder({"backtest": {"slippage_ticks": 2}}, symbol="", root=tmp_path)
    assert ladder.extra == 1.0
    assert ladder.stress == 3.0
    assert build_ladder(None).baseline == 0.0


def test_ladder_lines_show_three_tiers_and_used(tmp_path):
    for slip in (1, 2, 3):
        _fill(tmp_path, "fu2701", slip)
    ladder = build_ladder(_cfg(base=1.0), symbol="fu2701", root=tmp_path)
    lines = ladder_lines(ladder, used=1.0)
    assert "滑点三档" in lines[0]
    assert "基线 1 跳" in lines[0]
    assert "压力 2 跳" in lines[0]
    assert "实测 2.5 跳" in lines[0]
    assert "本次滑点  1 跳" in lines[1]
    assert "不自动写配置" in lines[1]
