"""
多因子 CTA 完整版：加权打分。
市场过滤关掉，日线用单独的趋势 K 线传入，避免测试去拉真实行情。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from src.strategy.cta_factors import FactorParams, compute_factors, position_ratio
from src.strategy.multi_factor import (
    MultiFactorStrategy,
)
from src.strategy.types import PositionState, StrategyAction


def _cfg(engine: str, **box) -> dict:
    shared = {
        "ma_short": 20,
        "ma_long": 60,
        "channel": 20,
        "threshold": 0.6,
        "use_sar": True,
        "disagree_max": 0.9,
        "flatten_at_close": False,
    }
    shared.update(box)
    shared.setdefault(
        "cta_filter",
        {
            "enabled": False,
            "use_adx": True,
            "use_atr": True,
            "use_disagree": True,
        },
    )
    return {
        "strategy": {
            "engine": engine,
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "market_filter": {"enabled": False},
            "multi_factor": dict(shared),
            engine: dict(shared),
        },
        "trade": {"symbol": "bu", "volume": 2},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
    }


def _frame(closes: list[float], spread: float = 1.0, start: str = "2026-01-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq="5min")
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": [100.0] * len(closes),
        },
        index=index,
    )


def _daily(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2025-08-01", periods=len(closes), freq="B")
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [1000.0] * len(closes),
        },
        index=index,
    )


def _up_signal() -> pd.DataFrame:
    return _frame([100 + i * 0.12 for i in range(80)] + [118.0], 0.4)


def _down_signal() -> pd.DataFrame:
    return _frame([118 - i * 0.12 for i in range(80)] + [90.0], 0.4)


def _daily_up() -> pd.DataFrame:
    return _daily([90 + i * 0.5 for i in range(80)])


def _daily_down() -> pd.DataFrame:
    return _daily([130 - i * 0.5 for i in range(80)])


def test_weighted_opens_long_when_factors_agree_with_daily_up() -> None:
    decision = MultiFactorStrategy(_cfg("multi_factor")).evaluate(
        "bu",
        _up_signal(),
        now=datetime(2026, 1, 1, 10, 0),
        trend_bars=_daily_up(),
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert "多因子做多" in decision.reason
    assert decision.meta.get("score", 0) >= 0.59


def test_weighted_blocks_open_without_daily_bars() -> None:
    decision = MultiFactorStrategy(_cfg("multi_factor")).evaluate(
        "bu", _up_signal(), now=datetime(2026, 1, 1, 10, 0)
    )
    assert decision.action == StrategyAction.HOLD
    assert "日线" in decision.reason


def test_weighted_exits_when_daily_and_score_flip() -> None:
    strategy = MultiFactorStrategy(_cfg("multi_factor"))
    strategy.positions["bu"] = PositionState(
        side="long", volume=1, entry_price=110.0, initial_stop=100.0, trail_stop=100.0
    )
    decision = strategy.evaluate(
        "bu",
        _down_signal(),
        now=datetime(2026, 1, 1, 10, 0),
        trend_bars=_daily_down(),
    )
    assert decision.action == StrategyAction.CLOSE
    assert "反向" in decision.reason or "止损" in decision.reason


def test_sar_off_still_opens_with_remaining_factors() -> None:
    decision = MultiFactorStrategy(_cfg("multi_factor", use_sar=False)).evaluate(
        "bu",
        _up_signal(),
        now=datetime(2026, 1, 1, 10, 0),
        trend_bars=_daily_up(),
    )
    assert decision.action == StrategyAction.BUY_OPEN
    assert decision.meta.get("score_sar", 1) == 0


def test_position_ratio_maps_threshold_to_min() -> None:
    assert abs(position_ratio(0.6, 0.6, 0.5, 1.0) - 0.5) < 1e-9
    assert abs(position_ratio(1.0, 0.6, 0.5, 1.0) - 1.0) < 1e-9


def test_compute_factors_needs_enough_bars() -> None:
    bars = _frame([100.0] * 10)
    assert compute_factors(bars, FactorParams()) is None


def test_allocate_pnl_by_abs_score() -> None:
    from src.strategy.cta_factors import allocate_factor_pnl, dominant_factor_label

    scores = {"ma": 0.5, "turtle": 1.0, "boll": 0.5, "sar": 0.0}
    parts = allocate_factor_pnl(scores, 100.0)
    assert abs(parts["turtle"] - 50.0) < 1e-9
    assert abs(parts["ma"] - 25.0) < 1e-9
    assert abs(parts["boll"] - 25.0) < 1e-9
    assert abs(parts["sar"] - 0.0) < 1e-9
    assert dominant_factor_label(scores) == "唐奇安突破"


def test_template_path_finds_config_copy() -> None:
    """程序用的模板在 config，和品种表放一起。"""
    from src.export.cta_decay import template_path
    from src.utils.paths import project_root

    path = template_path(project_root())
    assert path.is_file()
    assert path.parts[-2] == "config"


def test_decay_sheet_writes_dominant_factor(tmp_path) -> None:
    from src.export.cta_decay import write_decay_from_backtest

    payload = {
        "trades": [
            {
                "symbol": "fu2701",
                "side": "long",
                "entry_time": "2026-09-15 10:05:00",
                "exit_time": "2026-09-15 14:50:00",
                "entry_price": 3280,
                "exit_price": 3312,
                "volume": 1,
                "pnl": 320.0,
                "commission": 8,
                "score_ma": 0.5,
                "score_turtle": 1.0,
                "score_boll": 0.5,
                "score_sar": 0.0,
                "score_total": 0.8,
                "atr": 42,
                "dominant_factor": "唐奇安突破",
                "resonance": "F1/F3",
                "multiplier": 10,
                "reason_exit": "反向穿阈值",
            }
        ]
    }
    payload["engine"] = "multi_factor"
    payload["ran_at"] = "2026-11-08 00:52:00"
    dest = write_decay_from_backtest(tmp_path, payload)
    assert dest is not None
    assert dest.name.startswith("多因子衰减监控-加权打分-2026-11-08_00-52-00")
    from openpyxl import load_workbook

    book = load_workbook(dest)
    sheet = book["交易明细"]
    assert sheet.cell(4, 9).value == "唐奇安突破"
    assert sheet.cell(4, 3).value == "fu2701"
    assert "唐奇安突破" in str(sheet.cell(4, 24).value)
    book.close()
    again = write_decay_from_backtest(tmp_path, payload)
    assert again is not None
    assert again != dest
    assert dest.exists() and again.exists()


def test_cta_gate_blocks_low_adx_and_low_atr() -> None:
    from src.strategy.cta_filter import CtaFilterParams, gate_from_values

    p = CtaFilterParams()
    dead = gate_from_values(12.0, 0.5, p)
    assert not dead.ok
    tight = gate_from_values(30.0, 0.1, p)
    assert not tight.ok


def test_cta_gate_gray_tightens_size() -> None:
    from src.strategy.cta_filter import CtaFilterParams, gate_from_values

    gray = gate_from_values(22.0, 0.4, CtaFilterParams())
    assert gray.ok and gray.gray
    assert abs(gray.size_scale - 0.5) < 1e-9
    strong = gate_from_values(32.0, 0.4, CtaFilterParams())
    assert strong.ok and not strong.gray
    assert strong.size_scale == 1.0
    off = gate_from_values(8.0, 0.05, CtaFilterParams(enabled=False))
    assert off.ok and not off.gray


def test_weighted_gray_needs_higher_score(monkeypatch) -> None:
    from src.strategy.cta_filter import CtaGate

    monkeypatch.setattr(
        "src.strategy.multi_factor.evaluate_cta_filter",
        lambda _bars, _params: CtaGate(
            True, "弱趋势", adx=22.0, atr_rank=0.4, gray=True, size_scale=0.5
        ),
    )
    cfg = _cfg("multi_factor", cta_filter={"enabled": True, "weak_score": 0.75})
    decision = MultiFactorStrategy(cfg).evaluate(
        "bu",
        _up_signal(),
        now=datetime(2026, 1, 1, 10, 0),
        trend_bars=_daily_up(),
    )
    if decision.action == StrategyAction.BUY_OPEN:
        assert float(decision.meta.get("score") or 0) >= 0.75
        assert float(decision.meta.get("size_scale") or 1) <= 0.5
    else:
        assert decision.action == StrategyAction.HOLD
        assert "弱" in decision.reason or "空仓" in decision.reason


def test_sanitize_fills_cta_filter() -> None:
    from src.strategy.factory import sanitize_strategy_config

    cfg = {"strategy": {"engine": "multi_factor", "multi_factor": {}}}
    sanitize_strategy_config(cfg)
    filt = cfg["strategy"]["multi_factor"]["cta_filter"]
    assert filt["enabled"] is True
    assert filt["use_adx"] is True
    assert filt["atr_low_pct"] == 0.2
    assert filt["weak_score"] == 0.75
    assert "multi_vote" not in cfg["strategy"]


def test_daily_bias_uses_ema_and_can_flip_short_after_a_crash() -> None:
    """大跌后指数均线比简单均线更快翻空。"""
    from src.strategy.cta_factors import daily_bias

    # 先长期低位，再冲高，最后一天急跌：收盘仍高于 60 日简单均线，但已低于 60 日 EMA。
    closes = [3000.0] * 60 + [5400.0] * 19 + [4112.0]
    bars = _daily(closes)
    last = float(bars["close"].iloc[-1])
    sma = float(bars["close"].rolling(60).mean().iloc[-1])
    ema = float(bars["close"].ewm(span=60, adjust=False).mean().iloc[-1])
    assert last > sma
    assert last < ema
    assert daily_bias(bars, 60) == -1


def test_allocate_all_zero_scores_is_zero() -> None:
    from src.strategy.cta_factors import allocate_factor_pnl, dominant_factor_label

    parts = allocate_factor_pnl({"ma": 0, "turtle": 0, "boll": 0, "sar": 0}, 80.0)
    assert parts == {"ma": 0.0, "turtle": 0.0, "boll": 0.0, "sar": 0.0}
    assert dominant_factor_label({"ma": 0, "turtle": 0, "boll": 0, "sar": 0}) == ""
