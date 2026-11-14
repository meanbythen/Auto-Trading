"""通用止损：独立于策略自身止损的公共止损。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from src.strategy.generic_stop import (
    GENERIC_STOP_BREAKEVEN,
    GENERIC_STOP_CHANDELIER,
    GENERIC_STOP_OFF,
    GENERIC_STOP_PRIOR_EXTREME,
    GENERIC_STOP_SAR,
    GenericStopOverlay,
    generic_stop_from_cfg,
    generic_stop_from_label,
    generic_stop_label,
    normalize_generic_stop,
)


def _frame(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start: str = "2026-10-16 09:00",
) -> pd.DataFrame:
    idx = [datetime.fromisoformat(start) + timedelta(minutes=5 * i) for i in range(len(closes))]
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs if highs is not None else [c + 1 for c in closes],
            "low": lows if lows is not None else [c - 1 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=idx,
    )


def _pos(side: str, entry: float, opened_at: object, volume: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        side=side,
        entry_price=entry,
        opened_at=opened_at,
        volume=volume,
    )


def test_normalize_and_labels() -> None:
    assert normalize_generic_stop("off") == GENERIC_STOP_OFF
    assert normalize_generic_stop("关闭") == GENERIC_STOP_OFF
    assert normalize_generic_stop("sar") == GENERIC_STOP_SAR
    assert normalize_generic_stop("前高低点") == GENERIC_STOP_PRIOR_EXTREME
    assert normalize_generic_stop("吊灯") == GENERIC_STOP_CHANDELIER
    assert normalize_generic_stop("Chandelier 吊灯跟踪") == GENERIC_STOP_CHANDELIER
    assert normalize_generic_stop("盈利1ATR后保本") == GENERIC_STOP_BREAKEVEN
    assert normalize_generic_stop("breakeven") == GENERIC_STOP_BREAKEVEN
    assert normalize_generic_stop("看不懂") == GENERIC_STOP_OFF
    assert generic_stop_from_label("SAR 移动止损") == GENERIC_STOP_SAR
    assert generic_stop_from_label("盈利1ATR后保本") == GENERIC_STOP_BREAKEVEN
    assert generic_stop_label("prior_extreme") == "前高低点止损"


def test_config_defaults_are_off() -> None:
    assert generic_stop_from_cfg({}).kind == GENERIC_STOP_OFF
    cfg = {"strategy": {"generic_stop": {"kind": "sar", "lookback": 999}}}
    got = generic_stop_from_cfg(cfg)
    assert got.kind == GENERIC_STOP_SAR
    assert got.lookback == 200


def test_prior_extreme_long_touch_and_close_modes() -> None:
    closes = [105.0] * 21
    bars = _frame(closes, lows=[104.0] * 20 + [105.0])
    idx = bars.index
    pos = _pos("long", 105.0, idx[0].isoformat())
    overlay = GenericStopOverlay(
        {"strategy": {"generic_stop": {"kind": "prior_extreme", "lookback": 20}}}
    )

    # 前 20 根最低 104，当前这根低点 105 还没破。
    assert overlay.check("bu", pos, bars) is None

    # 影线刺到 104（触及即止损）就平；close 模式要等收盘价也跌破。
    bars.loc[idx[-1], "low"] = 103.0
    assert overlay.check("bu", pos, bars).stop == 104.0

    close_bars = bars.copy()
    close_bars.loc[idx[-1], "close"] = 105.0
    assert overlay.check("bu", pos, close_bars, mode="close") is None
    close_bars.loc[idx[-1], "close"] = 103.0
    assert overlay.check("bu", pos, close_bars, mode="close") is not None


def test_prior_extreme_short_uses_recent_high() -> None:
    closes = [105.0] * 21
    bars = _frame(closes, highs=[106.0] * 20 + [105.0])
    idx = bars.index
    pos = _pos("short", 105.0, idx[0].isoformat())
    overlay = GenericStopOverlay(
        {"strategy": {"generic_stop": {"kind": "prior_extreme", "lookback": 20}}}
    )
    assert overlay.check("bu", pos, bars) is None
    bars.loc[idx[-1], "high"] = 107.0
    hit = overlay.check("bu", pos, bars)
    assert hit is not None and hit.stop == 106.0


def test_entry_bar_itself_is_not_used() -> None:
    """开仓那根的影线可能出现在开仓之前，不能拿来误触通用止损。"""
    closes = [105.0] * 21
    bars = _frame(closes)
    idx = bars.index
    pos = _pos("long", 105.0, idx[-1].isoformat())
    overlay = GenericStopOverlay(
        {"strategy": {"generic_stop": {"kind": "prior_extreme", "lookback": 20}}}
    )
    bars.loc[idx[-1], "low"] = 90.0
    assert overlay.check("bu", pos, bars) is None

    # 下一根才开始判断：这一根直接跌穿前 20 根最低，触发。
    nxt = _frame(closes + [104.0], lows=[c - 1 for c in closes] + [90.0])
    hit = overlay.check("bu", pos, nxt)
    assert hit is not None and hit.stop == 104.0


def test_sar_waits_until_it_flips_to_the_same_side() -> None:
    """SAR 刚开仓时可能还在持仓反方向，先不武装，等它转到同向再用。"""
    closes = [100 - i for i in range(12)] + [89 + i for i in range(1, 11)]
    bars = _frame(closes)
    idx = bars.index
    pos = _pos("long", 100.0, idx[0].isoformat())
    overlay = GenericStopOverlay({"strategy": {"generic_stop": {"kind": "sar"}}})

    # 下跌段：SAR 在价格上方，做多还没武装，不能触发。
    assert overlay.check("bu", pos, bars.iloc[:12]) is None

    # 上涨段：SAR 翻到价格下方，武装但不触及。
    assert overlay.check("bu", pos, bars) is None

    # 之后一根盘中跌到 SAR 下方 → 通用止损触发。
    last_low = float(bars["low"].iloc[-1])
    crash = _frame(
        closes + [float(bars["close"].iloc[-1])],
        lows=[c - 1 for c in closes] + [last_low - 5],
    )
    hit = overlay.check("bu", pos, crash)
    assert hit is not None and hit.kind == GENERIC_STOP_SAR


def test_sar_next_level_is_the_point_where_the_next_bar_flips() -> None:
    """SAR 止损线要按「下一根会翻向的价位」算，不是最后一根自己的 SAR 值。"""
    from src.strategy.indicators import parabolic_sar, parabolic_sar_next_level

    closes = [100.0 - i for i in range(10)]
    bars = _frame(closes)
    level = parabolic_sar_next_level(bars["high"], bars["low"])
    assert level is not None
    last_sar = float(parabolic_sar(bars["high"], bars["low"]).iloc[-1])
    # 空头趋势里 SAR 逐根往价格靠，下一根的线比最后一根的 SAR 值更低，不能直接用最后一根。
    assert level < last_sar

    # 差一点点没碰到：不翻向。
    calm = _frame(
        closes + [float(bars["close"].iloc[-1])],
        highs=[c + 1 for c in closes] + [level - 0.5],
    )
    assert float(parabolic_sar(calm["high"], calm["low"]).iloc[-1]) > float(calm["close"].iloc[-1])

    # 碰到这根线：翻向。
    flipped = _frame(
        closes + [float(bars["close"].iloc[-1])],
        highs=[c + 1 for c in closes] + [level + 0.5],
    )
    assert float(parabolic_sar(flipped["high"], flipped["low"]).iloc[-1]) < float(
        flipped["close"].iloc[-1]
    )


def test_sar_short_stops_at_the_flip_level_not_the_last_sar_value() -> None:
    """空单遇到 SAR 翻向那根：按翻向的价位平，不能漏掉这一跳等到下一根。"""
    closes = [100.0 - i for i in range(10)]
    bars = _frame(closes)
    pos = _pos("short", 100.0, bars.index[0].isoformat())
    overlay = GenericStopOverlay({"strategy": {"generic_stop": {"kind": "sar"}}})

    # 下跌段 SAR 在价格上方，空单武装。
    assert overlay.check("bu", pos, bars) is None

    from src.strategy.indicators import parabolic_sar_next_level

    level = parabolic_sar_next_level(bars["high"], bars["low"])
    assert level is not None

    # 这根最高价只比翻向线高一点，但还没到最后一根 SAR 值上方：仍然要按翻向线平。
    touch = _frame(
        closes + [float(bars["close"].iloc[-1])],
        highs=[c + 1 for c in closes] + [level + 0.5],
        lows=[c - 1 for c in closes] + [float(bars["close"].iloc[-1]) - 1],
    )
    hit = overlay.check("bu", pos, touch)
    assert hit is not None
    assert hit.kind == GENERIC_STOP_SAR
    assert abs(hit.stop - level) < 1e-6
    assert "触及" in hit.reason


def test_chandelier_long_and_short() -> None:
    closes = [100.0] * 30
    bars = _frame(closes, highs=[101.0] * 30, lows=[99.0] * 30)
    idx = bars.index
    overlay = GenericStopOverlay(
        {"strategy": {"generic_stop": {"kind": "chandelier", "lookback": 20, "atr_mult": 3.0}}}
    )
    long_pos = _pos("long", 100.0, idx[0].isoformat())
    short_pos = _pos("short", 100.0, idx[0].isoformat())

    # 全平盘：ATR≈2，多单止损=101-6=95，空单止损=99+6=105，都不触发。
    assert overlay.check("bu", long_pos, bars) is None
    assert overlay.check("bu", short_pos, bars) is None

    long_crash = _frame(closes, highs=[101.0] * 30, lows=[99.0] * 29 + [90.0])
    assert overlay.check("bu", long_pos, long_crash) is not None

    short_spike = _frame(closes, highs=[101.0] * 29 + [110.0], lows=[99.0] * 30)
    assert overlay.check("bu", short_pos, short_spike) is not None


def test_breakeven_waits_for_profit_then_protects_entry() -> None:
    """浮盈没到 1×ATR 不设保本；到了以后回踩开仓价就走。"""
    bars = _frame([100.0] * 30, highs=[101.0] * 30, lows=[99.0] * 30)
    pos = _pos("long", 100.0, bars.index[0].isoformat())
    overlay = GenericStopOverlay(
        {"strategy": {"generic_stop": {"kind": "breakeven", "atr_period": 14, "be_atr_mult": 1.0}}}
    )
    # 全平盘：ATR=2，最大浮盈只有 1，保本线还没生效。
    assert overlay.check("bu", pos, bars) is None

    # 第 20 根冲高到 103（浮盈 3 ≥ 2），最后收盘在开仓价上方、但影线回踩到 99.5。
    highs = [101.0] * 30
    highs[20] = 103.0
    lows = [99.0] * 30
    lows[-1] = 99.5
    closes = [100.0] * 30
    closes[-1] = 100.2
    bars2 = _frame(closes, highs=highs, lows=lows)
    hit = overlay.check("bu", pos, bars2)
    assert hit is not None
    assert abs(hit.stop - 100.0) < 1e-9
    assert "保本" in hit.reason
    # close 模式：收盘 100.2 还在保本线上方，不触发。
    assert overlay.check("bu", pos, bars2, mode="close") is None
    # 收盘真跌到 99.8 才平。
    closes_bad = list(closes)
    closes_bad[-1] = 99.8
    bars3 = _frame(closes_bad, highs=highs, lows=lows)
    assert overlay.check("bu", pos, bars3, mode="close") is not None


def test_breakeven_short_mirror() -> None:
    bars = _frame([100.0] * 30, highs=[101.0] * 30, lows=[99.0] * 30)
    pos = _pos("short", 100.0, bars.index[0].isoformat())
    overlay = GenericStopOverlay(
        {"strategy": {"generic_stop": {"kind": "breakeven", "be_atr_mult": 1.0}}}
    )
    assert overlay.check("bu", pos, bars) is None
    lows = [99.0] * 30
    lows[20] = 97.0
    highs = [101.0] * 30
    highs[-1] = 100.5
    closes = [100.0] * 30
    closes[-1] = 99.8
    bars2 = _frame(closes, highs=highs, lows=lows)
    hit = overlay.check("bu", pos, bars2)
    assert hit is not None
    assert abs(hit.stop - 100.0) < 1e-9


def test_off_never_triggers() -> None:
    bars = _frame([100.0] * 30, highs=[120.0] * 30, lows=[80.0] * 30)
    pos = _pos("long", 100.0, bars.index[0].isoformat())
    overlay = GenericStopOverlay({"strategy": {"generic_stop": {"kind": "off"}}})
    assert overlay.enabled is False
    assert overlay.check("bu", pos, bars) is None


def test_cooldown_suppresses_repeated_close_alerts() -> None:
    """实盘平仓单还没成交时，同一持仓不要每轮都重复报通用止损。"""
    closes = [105.0] * 21
    bars = _frame(closes, lows=[104.0] * 20 + [103.0])
    pos = _pos("long", 105.0, bars.index[0].isoformat())
    overlay = GenericStopOverlay(
        {"strategy": {"generic_stop": {"kind": "prior_extreme", "lookback": 20}}}
    )
    first = overlay.check("bu", pos, bars, cooldown_sec=120.0)
    assert first is not None
    assert overlay.check("bu", pos, bars, cooldown_sec=120.0) is None


def test_backtest_rewrites_wall_clock_entry_time_so_generic_stop_can_fire() -> None:
    """
    日内实体开仓时用真实时钟记 opened_at；回测里要改成这根K线的模拟时间。

    否则通用止损会把这些历史K线全当成「开仓之前」，整段回测都不会生效。
    """
    from src.backtest.engine import run_backtest

    idx = pd.date_range("2026-10-23 09:00", "2026-10-23 11:00", freq="5min")
    rows = []
    rise = 0.0
    for stamp in idx:
        if stamp <= pd.Timestamp("2026-10-23 09:20"):
            rows.append((100.0, 101.0, 99.0, 100.0))
        elif stamp == pd.Timestamp("2026-10-23 09:25"):
            rows.append((100.0, 96.2, 94.8, 95.0))
        else:
            rise += 0.1
            rows.append((95.0 + rise, 95.3 + rise, 94.8 + rise, 95.1 + rise))
    bars = pd.DataFrame(
        {"open": [r[0] for r in rows], "high": [r[1] for r in rows],
         "low": [r[2] for r in rows], "close": [r[3] for r in rows],
         "volume": [100.0] * len(rows)},
        index=idx,
    )
    cfg = {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": False,
            "market_filter": {"enabled": False},
            "generic_stop": {"kind": "prior_extreme", "lookback": 2},
            "daily_body": {
                "eth_minutes": 20,
                "eth_minutes2": 0,
                "stop_extra_ticks": 1,
                "ema_period": 20,
                "ema_buffer_ticks": 2,
                "filter_enabled": False,
                "regime_filter": False,
            },
        },
        "trade": {"volume": 1, "symbol": "bu", "ctp": {"price_tick": 1}},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "risk": {"capital": 14000, "contracts": {"bu": {"multiplier": 10}}},
        "backtest": {"slippage_ticks": 0},
    }
    result = run_backtest(cfg, symbol="bu", bars_signal=bars, warmup=10, volume=1)
    assert result.trades, "至少要开一次仓"
    assert any("通用止损" in str(t.reason_exit) for t in result.trades)
    # 策略自己的箱沿止损在 102，通用止损应该先把它打掉。
    assert not any("开盘箱体上沿" in str(t.reason_exit) for t in result.trades)


def test_backtest_applies_generic_stop_before_strategy_exit() -> None:
    """回测里策略没让平仓时，公共止损先触发就按公共止损平。"""
    from src.backtest.engine import run_backtest

    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)]
    closes += [130.0, 131.0, 120.0, 115.0, 110.0, 105.0]
    idx = [datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(len(closes))]
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=idx,
    )
    cfg = {
        "dry_run": True,
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": False,
            # 公共止损比策略自己的 8 根低点更早触发。
            "market_filter": {"enabled": False},
            "generic_stop": {"kind": "prior_extreme", "lookback": 5},
            "trend_follow": {
                "entry_bars": 20,
                "exit_bars": 8,
                "atr_period": 14,
                "atr_mult": 10.0,
                "flatten_at_close": False,
            },
        },
        "trade": {"volume": 1, "symbol": "bu", "ctp": {"price_tick": 1}},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "risk": {"capital": 14000, "contracts": {"bu": {"multiplier": 10}}},
        "backtest": {"slippage_ticks": 0},
    }
    result = run_backtest(cfg, symbol="bu", bars_signal=bars, warmup=20, volume=1)
    assert result.trades, "至少要开一次仓"
    assert any("通用止损" in str(t.reason_exit) for t in result.trades)
    assert result.params_snapshot["generic_stop"]["kind"] == "prior_extreme"
