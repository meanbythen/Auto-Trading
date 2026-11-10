"""Jesse Casper 简化策略：价值区、黄金时段、工厂接线。"""

from datetime import datetime

import pandas as pd

from src.strategy.factory import create_strategy
from src.strategy.jesse_casper import JesseCasperStrategy
from src.strategy.jesse_va import calc_va_from_rows, is_imbalance, previous_session_va, session_key
from src.strategy.types import StrategyAction


def _cfg(**box) -> dict:
    jesse = {
        "lookback_liq": 8,
        "vol_ma_len": 8,
        "vol_mult": 1.1,
        "min_rr": 3.0,
        "price_tick": 1.0,
        "use_va": False,
        "use_kill_zone": False,
        "use_daily_halt": False,
        "flatten_at_close": True,
        "trend_timeframe": "15",
    }
    jesse.update(box)
    return {
        "strategy": {
            "engine": "jesse_casper",
            "timeframe": "5",
            "symbols": ["fu2701"],
            "flatten_times": ["14:55", "22:55"],
            "jesse_casper": jesse,
        },
        "trade": {"volume": 1, "symbol": "fu2701"},
        "schedule": {"trading_sessions": [["09:00", "15:00"], ["21:00", "23:00"]]},
        "dry_run": True,
    }


def test_session_key_splits_day_and_night() -> None:
    assert session_key(datetime(2026, 11, 6, 9, 5)).endswith("_day")
    assert session_key(datetime(2026, 11, 6, 21, 5)).endswith("_night")
    assert session_key(datetime(2026, 11, 6, 16, 0)) is None


def test_calc_va_covers_busy_prices() -> None:
    rows = []
    for i in range(12):
        rows.append({"high": 100.0, "low": 99.0, "volume": 10.0})
    for i in range(12):
        rows.append({"high": 110.0, "low": 109.0, "volume": 100.0})
    result = calc_va_from_rows(rows, tick=1.0)
    assert result is not None
    vah, val, vpoc = result
    assert 108.0 <= vpoc <= 111.0
    assert val <= vpoc <= vah


def test_previous_session_va_uses_completed_session() -> None:
    idx = []
    rows = []
    # 夜盘：价格挤在 100 附近
    for i in range(20):
        idx.append(datetime(2026, 11, 5, 21, i))
        rows.append({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 50.0})
    # 日盘：价格已到 120
    for i in range(20):
        idx.append(datetime(2026, 11, 6, 9, i))
        rows.append({"open": 120.0, "high": 121.0, "low": 119.0, "close": 120.0, "volume": 50.0})
    bars = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    area = previous_session_va(bars, now=datetime(2026, 11, 6, 9, 30), tick=1.0)
    assert area is not None
    assert area.val <= 101.5
    assert not is_imbalance(100.0, area)
    assert is_imbalance(120.0, area)


def test_factory_aliases() -> None:
    for name in ("jesse_casper", "casper", "jesse"):
        cfg = _cfg()
        cfg["strategy"]["engine"] = name
        got = create_strategy(cfg)
        assert isinstance(got, JesseCasperStrategy)
        assert got.market_filter.enabled is False
        assert got.uses_trend_bars is True
        assert got.trend_timeframe == "15"


def test_kill_zone_blocks_outside_hours() -> None:
    s = create_strategy(_cfg(use_kill_zone=True, use_va=False, use_daily_halt=False))
    bars = _trend_up_bars()
    now = datetime(2026, 11, 6, 13, 0)
    decision = s.evaluate("fu2701", bars, now=now, trend_bars=bars)
    assert decision.action == StrategyAction.HOLD
    assert "黄金时段" in decision.reason


def test_daily_halt_after_two_losses() -> None:
    s = create_strategy(_cfg(use_daily_halt=True, use_kill_zone=False, use_va=False, max_losses_day=2))
    s._day = None
    now = datetime(2026, 11, 6, 9, 15)
    s._sync_day(now)
    s.positions["fu2701"].side = "long"
    s.positions["fu2701"].entry_price = 100.0
    s.positions["fu2701"].volume = 1
    s.on_filled("fu2701", StrategyAction.CLOSE, 90.0, 1)
    s.positions["fu2701"].side = "long"
    s.positions["fu2701"].entry_price = 100.0
    s.positions["fu2701"].volume = 1
    s.on_filled("fu2701", StrategyAction.CLOSE, 90.0, 1)
    bars = _trend_up_bars()
    decision = s.evaluate("fu2701", bars, now=now, trend_bars=bars)
    assert decision.action == StrategyAction.HOLD
    assert "停手" in decision.reason


def test_sequential_sweep_displacement_then_pullback() -> None:
    """扫流动性、大实体、回踩分成三根 K，才能开仓。"""
    cfg = _cfg(
        use_kill_zone=False,
        use_va=False,
        lookback_liq=8,
        min_rr=1.5,
        vol_mult=1.0,
        displacement_body_ratio=0.5,
        setup_wait_bars=8,
    )
    cfg["schedule"]["trading_sessions"] = [["00:00", "23:59"]]
    s = create_strategy(cfg)
    base = _trend_up_bars()
    last = float(base["close"].iloc[-1])
    extra_idx = pd.date_range(base.index[-1] + pd.Timedelta(minutes=5), periods=4, freq="5min")
    rows = [
        # 横盘，给出明确高低点
        {"open": last, "high": last + 2, "low": last - 1, "close": last + 1, "volume": 80.0},
        # 扫下方流动性：长下影刺破近 8 根低点，实体不大
        {"open": last, "high": last + 1, "low": last - 30, "close": last + 0.5, "volume": 90.0},
        # 位移：放量大阳，目标拉远，盈亏比才够
        {"open": last + 0.5, "high": last + 80, "low": last, "close": last + 70, "volume": 400.0},
        # 回踩入场区
        {"open": last + 16, "high": last + 17, "low": last + 0.2, "close": last + 1.0, "volume": 80.0},
    ]
    extra = pd.DataFrame(rows, index=extra_idx)
    bars = pd.concat([base, extra])
    now = bars.index[-1].to_pydatetime()
    # 先走到扫、位移，再回踩。
    for end in range(len(base) + 1, len(bars) + 1):
        piece = bars.iloc[:end]
        decision = s.evaluate("fu2701", piece, now=piece.index[-1].to_pydatetime(), trend_bars=piece)
    assert decision.action == StrategyAction.BUY_OPEN, decision.reason
    assert s._open_target > 0


def _trend_up_bars() -> pd.DataFrame:
    """足够长的 5 分钟阳线，让 15 分钟偏见偏多。"""
    idx = pd.date_range("2026-11-06 09:00", periods=90, freq="5min")
    rows = []
    price = 100.0
    for i in range(90):
        o = price
        c = price + 2.0
        rows.append(
            {
                "open": o,
                "high": c + 0.5,
                "low": o - 0.5,
                "close": c,
                "volume": 100.0,
            }
        )
        price = c
    return pd.DataFrame(rows, index=idx)
