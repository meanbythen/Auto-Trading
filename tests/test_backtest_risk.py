"""回测开仓限制：止损过宽跳过，当日亏损第二天解除。"""

from datetime import datetime, timedelta

import pandas as pd

from src.backtest.engine import BacktestLimits, _cap_open_volume, run_backtest


def _wide_stop_bars() -> pd.DataFrame:
    """
    做出一笔趋势跟随做多：前面剧烈晃动抬高 ATR，最后一根创出新高。

    原理：前 40 根在 90/110 晃，ATR 很大；收盘到 130 突破通道做多。
    止损用很大倍数 ATR，离进场很远。乘以合约乘数后风险很大，
    用来检查权益很小时会不会硬开。
    """
    closes = [90.0 if i % 2 == 0 else 110.0 for i in range(40)] + [130.0]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    idx = [datetime(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i) for i in range(len(closes))]
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [100] * len(closes)},
        index=idx,
    )


def _cfg(**risk) -> dict:
    base = {
        "dry_run": True,
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": False,
            "market_filter": {"enabled": False},
            "trend_follow": {
                "entry_bars": 20,
                "exit_bars": 8,
                "atr_period": 14,
                "atr_mult": 10.0,
                "flatten_at_close": False,
            },
        },
        "trade": {"volume": 1, "symbol": "bu"},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "risk": {
            "sizing_mode": "risk_pct",
            "capital": 14000,
            "risk_per_trade_pct": 2,
            "min_volume": 1,
            "max_daily_loss": 500,
            "min_order_interval_sec": 60,
            "max_orders_per_day": 20,
            "contracts": {"bu": {"multiplier": 10}},
        },
    }
    base["risk"].update(risk)
    return base


def test_strategy_volume_is_capped_by_risk_sized_lots() -> None:
    """回测里策略写的单元手数也不能超过资金管理允许的手数。"""
    vol, note = _cap_open_volume(1, 3, limits_apply=True)
    assert vol == 1
    assert "取小1手" in note
    # 策略要得更少时按策略。
    assert _cap_open_volume(3, 1, limits_apply=True)[0] == 1
    # 明确关掉开仓限制时才按策略手数。
    assert _cap_open_volume(1, 3, limits_apply=False)[0] == 3
    # 没写手数：完全按风控。
    assert _cap_open_volume(2, None, limits_apply=True)[0] == 2


def test_wide_stop_is_skipped_when_budget_is_tiny() -> None:
    """权益很小、止损又远时，手数为 0，不成交，并记下原因。"""
    result = run_backtest(
        _cfg(capital=100, risk_per_trade_pct=0.5),
        symbol="bu",
        bars_signal=_wide_stop_bars(),
        warmup=20,
    )
    assert result.performance.trades == 0
    skipped = result.params_snapshot["skipped_opens"]
    assert skipped.get("止损过宽，手数不够", 0) >= 1
    # 成交明细之外，每一条没开成的信号都带时间、方向、原因。
    assert result.rejected, "有信号但没开成，必须留下记录"
    first = result.rejected[0]
    assert first.stage == "风控手数"
    assert first.side == "多"
    assert first.time
    assert "止损过宽" in first.reason


def test_limits_off_keeps_one_lot_even_if_stop_is_wide() -> None:
    """开关关掉后，止损很远也按 1 手做，用来和加了限制的结果对比。"""
    result = run_backtest(
        _cfg(capital=100, risk_per_trade_pct=0.5, apply_limits=False),
        symbol="bu",
        bars_signal=_wide_stop_bars(),
        warmup=20,
    )
    assert result.params_snapshot["apply_limits"] is False
    assert result.performance.trades >= 1
    assert result.trades[0].volume == 1
    assert result.params_snapshot["skipped_opens"] == {}


def test_fixed_volume_still_opens() -> None:
    """改成固定手数后，同一段行情可以开出来。"""
    result = run_backtest(
        _cfg(sizing_mode="fixed"),
        symbol="bu",
        bars_signal=_wide_stop_bars(),
        warmup=20,
    )
    assert result.performance.trades >= 1
    assert result.trades[0].volume == 1


def test_daily_loss_halt_clears_on_next_day() -> None:
    limits = BacktestLimits(_cfg(max_daily_loss=50))
    now = datetime(2026, 10, 16, 10, 0)
    limits.on_bar(now)
    limits.on_close(-80, 1, "bu")
    ok, reason = limits.allow_open(now, 1)
    assert ok is False
    assert "亏损" in reason
    limits.on_bar(datetime(2026, 10, 17, 9, 30))
    ok2, _ = limits.allow_open(datetime(2026, 10, 17, 9, 30), 1)
    assert ok2 is True
    assert limits.guard.write_alerts is False


def test_backtest_kelly_consumes_net_closed_samples_and_blocks_negative_edge() -> None:
    """回测先用固定风险度过样本不足，净样本足够且负期望后停开。"""
    limits = BacktestLimits(
        _cfg(
            capital=100_000,
            risk_per_trade_pct=1,
            kelly={"enabled": True, "auto_from_ledger": True, "min_samples": 2, "window": 10},
        )
    )
    first, reason = limits.size_open("bu", 100, 99)
    assert first > 0
    assert "暂用固定风险1%" in reason
    limits.on_close(-100, 1, "bu")
    limits.on_close(-120, 1, "bu")
    assert [row["pnl"] for row in limits.kelly_rows] == [-100, -120]
    blocked, reason = limits.size_open("bu", 100, 99)
    assert blocked == 0
    assert "正期望" in reason


def test_night_session_does_not_reset_counters_at_midnight() -> None:
    """夜盘和次日日盘是一个交易日，跨过零点不能清掉熔断。"""
    limits = BacktestLimits(_cfg(max_daily_loss=50))
    limits.on_bar(datetime(2026, 10, 27, 21, 30))
    limits.on_close(-80, 1, "bu")
    assert limits.guard.halted is True
    # 同一个夜盘跨到凌晨 00:30，仍是周三这个交易日。
    limits.on_bar(datetime(2026, 10, 28, 0, 30))
    assert limits.guard.halted is True
    # 周三 21:00 才进入周四交易日，这时才清零。
    limits.on_bar(datetime(2026, 10, 28, 21, 0))
    ok, _ = limits.allow_open(datetime(2026, 10, 28, 21, 5), 1)
    assert ok is True


def _daily_body_bars(day: str = "2026-10-23") -> pd.DataFrame:
    """前一晚先铺一段，再接 09:00 起 5 分钟。ETH 20 分钟箱体 99~101，09:25 收盘 95 跌破。"""
    pre = pd.date_range("2026-10-22 21:05", "2026-10-22 22:55", freq="5min")
    idx = pd.date_range(f"{day} 09:00", f"{day} 11:00", freq="5min")
    pre_rows = [(100.0, 100.3, 99.7, 100.0)] * len(pre)
    rows: list[tuple[float, float, float, float]] = []
    rise = 0.0
    for stamp in idx:
        if stamp <= pd.Timestamp(f"{day} 09:20"):
            rows.append((100.0, 101.0, 99.0, 100.0))
        elif stamp == pd.Timestamp(f"{day} 09:25"):
            rows.append((100.0, 100.0, 94.8, 95.0))
        else:
            rise += 0.1
            rows.append((95.0 + rise, 95.3 + rise, 94.8 + rise, 95.1 + rise))
    all_rows = pre_rows + rows
    all_idx = pd.DatetimeIndex(list(pre) + list(idx))
    return pd.DataFrame(
        {
            "open": [r[0] for r in all_rows],
            "high": [r[1] for r in all_rows],
            "low": [r[2] for r in all_rows],
            "close": [r[3] for r in all_rows],
            "volume": [100.0] * len(all_rows),
        },
        index=all_idx,
    )


def _daily_body_cfg() -> dict:
    """日内实体：ETH 20 分钟，箱体宽 2 点，上限压到 1 跳 → 跌破信号成立也不开。"""
    return {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "flatten_at_close": False,
            "daily_body": {
                "eth_minutes": 20,
                "max_box_atr_mult": 1.0,
                "stop_extra_ticks": 1,
                "regime_filter": False,
            },
        },
        "trade": {"volume": 1, "symbol": "bu", "ctp": {"price_tick": 1}},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
        "risk": {"capital": 14000, "contracts": {"bu": {"multiplier": 10}}},
        "backtest": {"slippage_ticks": 0},
    }


def test_rejected_signal_records_strategy_filter_reason() -> None:
    """策略看到跌破信号、但被自己的规则挡回去时，也要记一条带原因的记录。"""
    result = run_backtest(
        _daily_body_cfg(), symbol="bu", bars_signal=_daily_body_bars(), warmup=5, volume=1
    )
    assert result.performance.trades == 0
    assert result.rejected, "被策略过滤掉的信号也要留下记录"
    records = result.rejected
    # 09:25 到 11:00 每根都在发同一个信号、同一条原因，同一时段只留第一条。
    assert len(records) == 1
    first = records[0]
    assert first.stage == "策略过滤"
    assert first.side == "空"
    assert first.time.startswith("2026-10-23T09:25")
    assert "箱体" in first.reason
    assert "这段不开" in first.reason
    # 保存成 JSON 后仍能看到这些记录。
    payload = result.to_dict()
    assert payload["rejected"][0]["stage"] == "策略过滤"


def test_same_reason_is_recorded_once_per_session_not_once_forever() -> None:
    """同样的原因，换一个交易时段仍要重新记一条。"""
    frames = [_daily_body_bars("2026-10-23"), _daily_body_bars("2026-10-26")]
    result = run_backtest(
        _daily_body_cfg(),
        symbol="bu",
        bars_signal=pd.concat(frames),
        warmup=5,
        volume=1,
    )
    assert [item.time[:10] for item in result.rejected] == ["2026-10-23", "2026-10-26"]
