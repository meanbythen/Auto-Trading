"""动态减仓：账户回撤和波动放大只改手数，1 手不强平。不管连亏笔数。"""

from src.risk.dynamic import DynamicExposure, atr_ratio, describe_status, drawdown_frac

import numpy as np
import pandas as pd


def test_drawdown_frac_from_peak() -> None:
    assert drawdown_frac(95, 100) == 0.05
    assert drawdown_frac(100, 100) == 0.0
    assert drawdown_frac(110, 100) == 0.0
    assert drawdown_frac(None, 100) is None


def test_half_keeps_one_lot() -> None:
    dyn = DynamicExposure(enabled=True, dd_half_pct=5, dd_pause_pct=10)
    vol, note = dyn.adjust_open(1, ratio=None, dd=0.06)
    assert vol == 1
    assert "没法再减" in note


def test_two_lots_halve_to_one() -> None:
    dyn = DynamicExposure(enabled=True, dd_half_pct=5, dd_pause_pct=10)
    vol, note = dyn.adjust_open(4, ratio=None, dd=0.06)
    assert vol == 2
    assert "减半" in note


def test_pause_skips_new_open() -> None:
    dyn = DynamicExposure(enabled=True, dd_half_pct=5, dd_pause_pct=10)
    vol, note = dyn.adjust_open(2, ratio=None, dd=0.12)
    assert vol == 0
    assert "停手" in note


def test_drawdown_recovers_without_a_win() -> None:
    """回撤从 12% 收到 4%，低于减半线，手数自己恢复，不必再赚一笔。"""
    dyn = DynamicExposure(enabled=True, dd_half_pct=5, dd_pause_pct=10)
    paused, _ = dyn.adjust_open(4, ratio=None, dd=0.12)
    assert paused == 0
    recovered, note = dyn.adjust_open(4, ratio=None, dd=0.04)
    assert recovered == 4
    assert note == ""


def test_consecutive_losses_do_not_cut_size() -> None:
    """连亏很多笔但账户几乎没回撤，不减仓。"""
    dyn = DynamicExposure(enabled=True, dd_half_pct=5, dd_pause_pct=10)
    vol, note = dyn.adjust_open(3, ratio=None, dd=0.01)
    assert vol == 3
    assert note == ""


def test_atr_pause_skips_open() -> None:
    dyn = DynamicExposure(enabled=True, atr_half_mult=1.8, atr_pause_mult=2.5)
    vol, note = dyn.adjust_open(2, ratio=3.0, dd=0.0)
    assert vol == 0
    assert "波动" in note


def test_disabled_does_nothing() -> None:
    dyn = DynamicExposure(enabled=False)
    vol, note = dyn.adjust_open(3, ratio=9.0, dd=0.5)
    assert vol == 3
    assert note == ""
    assert dyn.held_target(4, 0.5) is None


def test_held_reduce_keeps_at_least_one() -> None:
    dyn = DynamicExposure(enabled=True, dd_half_pct=5, dd_pause_pct=10, reduce_held=True)
    assert dyn.held_target(1, 0.12) is None
    assert dyn.held_target(4, 0.06) == 2
    assert dyn.held_target(3, 0.12) == 1
    assert dyn.held_target(4, 0.01) is None


def test_missing_config_defaults_on() -> None:
    dyn = DynamicExposure.from_config({"risk": {}})
    assert dyn.enabled is True
    assert dyn.dd_half_pct == 5
    assert dyn.dd_pause_pct == 10
    off = DynamicExposure.from_config({"risk": {"dynamic": {"enabled": False}}})
    assert off.enabled is False


def test_old_loss_keys_are_ignored() -> None:
    dyn = DynamicExposure.from_config(
        {"risk": {"dynamic": {"enabled": True, "loss_pause_at": 4, "loss_pause_days": 3}}}
    )
    vol, _ = dyn.adjust_open(2, ratio=None, dd=0.01)
    assert vol == 2


def test_atr_ratio_uses_only_past_median() -> None:
    n = 80
    close = pd.Series(np.linspace(100, 110, n))
    high = close + 0.2
    low = close - 0.2
    high.iloc[-3:] = close.iloc[-3:] + 8
    low.iloc[-3:] = close.iloc[-3:] - 8
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close})
    ratio = atr_ratio(df, period=5, lookback=20)
    assert ratio is not None and ratio > 1.5
    quiet = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
        }
    )
    quiet_ratio = atr_ratio(quiet, period=5, lookback=20)
    assert quiet_ratio is not None
    assert quiet_ratio < 1.3


def test_describe_mentions_off_switch() -> None:
    text = describe_status({"risk": {"dynamic": {"enabled": False}}})
    assert "已关" in text
