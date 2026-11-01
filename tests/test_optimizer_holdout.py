"""策略优化的分段规则。

前一段只负责打分。分数接近时留更靠近出厂参数的一组。
后一段不参加挑选，只判断这组能不能写入：
前一段要赚钱；后一段可以把赚变成小亏，但不能亏掉一半以上。
"""

from types import SimpleNamespace

import pytest

from src.backtest.optimizer import (
    HOLDOUT_LOSS_RATIO,
    _apply_params,
    _finest_grid_timeframe,
    _trades_on_or_after,
    clamp_holdout_pct,
    describe_run_switches,
    format_perf_block,
    grid_for_engine,
    judge_cross_symbols,
    judge_holdout,
    judge_walk_forward,
    once_cut_efficiency,
    pick_train_winner,
    split_train_holdout,
    train_ratio_from_holdout_pct,
    walk_forward_folds,
    walk_forward_metrics,
)


def test_split_uses_about_seventy_percent() -> None:
    """1000 根里，前 700 根挑参数，后 300 根只验收。"""
    assert split_train_holdout(1000) == 700


def test_holdout_pct_can_be_hand_tuned() -> None:
    """检验比例 40% 时，训练大约剩 60%。乱填会收进 10～50。"""
    assert clamp_holdout_pct(40) == 40
    assert train_ratio_from_holdout_pct(40) == pytest.approx(0.60)
    assert clamp_holdout_pct(3) == 10
    assert clamp_holdout_pct(90) == 50
    assert split_train_holdout(1000, train_ratio_from_holdout_pct(40)) == 600


def test_walk_forward_rolls_step_by_step() -> None:
    """滚动前进：三段检验挨在末尾，每段训练只看到检验开始之前。"""
    folds = walk_forward_folds(1000, 30, 3)
    assert len(folds) == 3
    assert folds[0] == (700, 800)
    assert folds[1] == (800, 900)
    assert folds[2] == (900, 1000)


def test_cross_symbols_reject_when_most_blow_up() -> None:
    """其它关注品种大半都大亏时，不准写入。"""
    rows = [
        {"symbol": "a", "net_pnl": -600},
        {"symbol": "b", "net_pnl": -700},
        {"symbol": "c", "net_pnl": 50},
    ]
    ok, reason = judge_cross_symbols(1000, rows)
    assert ok is False
    assert "不写入" in reason


def test_walk_forward_rejects_when_too_few_oos_windows_win() -> None:
    """公开渠道常见门槛：滚动检验里赚钱的段不到一半，不准写入。"""
    folds = [
        {"holdout_performance": {"net_pnl": 100}, "params": {"a": 1}},
        {"holdout_performance": {"net_pnl": -50}, "params": {"a": 1}},
        {"holdout_performance": {"net_pnl": -20}, "params": {"a": 2}},
    ]
    metrics = walk_forward_metrics(folds, train_net=200, hold_net=30)
    assert metrics["oos_win_rate"] < 0.5
    ok, reason = judge_walk_forward(metrics)
    assert ok is False
    assert "不到一半" in reason


def test_walk_efficiency_and_once_cut_ratio() -> None:
    """检验效率 = 检验净盈亏 ÷ 训练净盈亏。"""
    assert once_cut_efficiency(1000, 500) == 0.5
    folds = [
        {"holdout_performance": {"net_pnl": 100}, "params": {"a": 1}},
        {"holdout_performance": {"net_pnl": 80}, "params": {"a": 1}},
    ]
    metrics = walk_forward_metrics(folds, train_net=200, hold_net=180)
    ok, reason = judge_walk_forward(metrics)
    assert ok is True
    assert "检验效率" in reason


def test_perf_block_matches_overall_fields() -> None:
    """训练/检验成绩和整体结果一样，都写出成交、净盈亏、盈利因子、最大回撤、盈利覆盖。"""
    lines = format_perf_block(
        "训练成绩",
        {
            "trades": 10,
            "wins": 6,
            "losses": 4,
            "win_rate": 0.6,
            "net_pnl": 1200,
            "profit_factor": 1.8,
            "max_drawdown": 300,
            "max_drawdown_pct": 0.05,
            "score": 0.72,
        },
        bars=700,
        data_from="2026-01-01",
        data_to="2026-01-31",
    )
    text = "\n".join(lines)
    assert "训练成绩" in text
    assert "净盈亏" in text
    assert "盈利因子" in text
    assert "最大回撤" in text
    assert "盈利覆盖" in text
    assert "每笔收益" in text
    assert "日均" in text


def test_split_keeps_both_sides_long_enough() -> None:
    """刚好 80 根时，前一段仍不少于 50 根，后一段仍留下 30 根。"""
    cut = split_train_holdout(80)
    assert cut >= 50
    assert 80 - cut >= 30


def test_short_history_is_refused() -> None:
    """行情太短就说明原因，不再把整段拿去打分。"""
    with pytest.raises(RuntimeError, match="分不出"):
        split_train_holdout(79)


def test_big_holdout_loss_cannot_be_saved() -> None:
    """前一段赚 1000，后一段亏超过一半，不准写入。"""
    ok, reason = judge_holdout(1000, -1000 * HOLDOUT_LOSS_RATIO - 1, 4)
    assert ok is False
    assert "不写入" in reason


def test_small_holdout_loss_can_be_saved() -> None:
    """亏掉刚好一半，还不算由赚变成大亏，可以写入。"""
    ok, reason = judge_holdout(1000, -500, 4)
    assert ok is True
    assert "小亏" in reason


def test_holdout_profit_can_be_saved() -> None:
    ok, reason = judge_holdout(1000, 200, 3)
    assert ok is True
    assert "也是赚的" in reason


def test_no_holdout_trade_is_not_a_big_loss() -> None:
    """后一段没开仓，不能当成还会赚，但也不算大亏。"""
    ok, reason = judge_holdout(1000, 0, 0)
    assert ok is True
    assert "没有开仓" in reason


def test_train_loss_cannot_be_saved() -> None:
    ok, _reason = judge_holdout(-100, 500, 2)
    assert ok is False


def test_close_scores_keep_the_factory_like_set() -> None:
    """两组成绩差不多时，留更靠近出厂的。不看后一段。"""
    trials = [
        {"params": {"entry_bars": 48, "atr_mult": 2.5}, "score": 0.80},
        {"params": {"entry_bars": 36, "atr_mult": 2.0}, "score": 0.79},
    ]
    winner = pick_train_winner(trials, "trend_follow")
    assert winner is not None
    assert winner["params"]["entry_bars"] == 36


def test_clearly_better_score_is_kept() -> None:
    """分数差得比较多时，仍留前一段更高的那组。"""
    trials = [
        {"params": {"entry_bars": 48, "atr_mult": 2.5}, "score": 0.80},
        {"params": {"entry_bars": 36, "atr_mult": 2.0}, "score": 0.70},
    ]
    winner = pick_train_winner(trials, "trend_follow")
    assert winner is not None
    assert winner["params"]["entry_bars"] == 48


def test_holdout_only_counts_new_entries() -> None:
    """切断之前开的仓不拿来验收后一段。"""
    trades = [
        SimpleNamespace(entry_time="2026-10-01 09:00:00", pnl=100),
        SimpleNamespace(entry_time="2026-10-20 09:00:00", pnl=-800),
    ]
    kept = _trades_on_or_after(trades, "2026-10-20 09:00:00")
    assert len(kept) == 1
    assert kept[0].pnl == -800


def test_daily_body_grid_stays_small() -> None:
    """精简只搜 ETH 和信号周期；标准再多加截止钟点和交易时段，靠抽样控制次数。"""
    light = grid_for_engine("daily_body", "精简")
    full = grid_for_engine("daily_body", "标准")
    assert set(light) == {"eth_minutes", "timeframe"}
    assert light["eth_minutes"] == [20, 30]
    assert light["timeframe"] == ["5", "15"]
    assert set(full) == {
        "eth_minutes",
        "timeframe",
        "day_cutoff",
        "night_cutoff",
        "session_scope",
    }
    light_n = 1
    for vals in light.values():
        light_n *= len(vals)
    full_n = 1
    for vals in full.values():
        full_n *= len(vals)
    assert light_n <= 20
    assert full_n <= 200


def test_daily_body_params_write_eth_and_timeframe() -> None:
    """ETH 写到共用项，信号周期写到策略根。"""
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "daily_body": {"eth_minutes": 20},
        }
    }
    out = _apply_params(
        cfg,
        {
            "eth_minutes": 30,
            "timeframe": "15",
        },
        "daily_body",
    )
    st = out["strategy"]
    assert st["daily_body"]["eth_minutes"] == 30
    assert st["timeframe"] == "15"
    assert st["trail_timeframe"] == "15"


def test_finest_grid_timeframe_picks_five() -> None:
    """网格里有 5 和 15 时，拉 K 线用 5 分钟。"""
    assert _finest_grid_timeframe({"timeframe": ["15", "5"]}, "15") == "5"


def test_cutoff_and_scope_write_where_the_strategy_reads() -> None:
    """截止钟点写在 daily_body；交易时段写在 schedule，和配置页同一处。"""
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "daily_body": {},
        },
        "schedule": {"session_scope": "all"},
    }
    out = _apply_params(
        cfg,
        {"day_cutoff": "09:75", "night_cutoff": "22:00", "session_scope": "night"},
        "daily_body",
    )
    # 09:75 不是合法钟点，回到出厂的 10:30。
    assert out["strategy"]["daily_body"]["day_cutoff"] == "10:30"
    assert out["strategy"]["daily_body"]["night_cutoff"] == "22:00"
    assert out["schedule"]["session_scope"] == "night"


def test_settings_outside_the_grid_stay_as_the_config_page() -> None:
    """网格没搜到的设置（例如时段、箱体宽度）在优化试参数时保持配置页的值。"""
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "daily_body": {"day_cutoff": "10:30", "night_cutoff": "22:15", "max_box_ticks": 80},
        },
        "schedule": {"session_scope": "day"},
    }
    out = _apply_params(cfg, {"eth_minutes": 30, "timeframe": "5"}, "daily_body")
    assert out["schedule"]["session_scope"] == "day"
    assert out["strategy"]["daily_body"]["max_box_ticks"] == 80
    assert out["strategy"]["daily_body"]["day_cutoff"] == "10:30"


def test_every_optimized_param_is_a_config_page_item() -> None:
    """优化网格里的每个参数都能在配置页改，写入配置时就是改配置页那一项。"""
    ui_covered = {
        # 日内实体
        "eth_minutes",
        "timeframe",
        "day_cutoff",
        "night_cutoff",
        "session_scope",
        # 趋势跟随 / SuperTrend
        "entry_bars",
        "atr_mult",
        "atr_period",
        "multiplier",
        # 单均线 / 双均线
        "ma_period",
        "short_period",
        "long_period",
        "reverse_on_cross",
        # 长线趋势加减仓
        "daily_ma",
        "add_min_atr",
        "reduce_extend_atr",
        "max_adds",
    }
    for engine in ("daily_body", "trend_follow", "supertrend", "single_ma", "dual_ma", "swing_trend"):
        for depth in ("精简", "标准"):
            assert set(grid_for_engine(engine, depth)) <= ui_covered, (engine, depth)


def test_daily_body_switch_text_matches_current_rules() -> None:
    """优化页说明要提到箱体突破和极值止损，不再写死旧的纯 ATR 开仓说法。"""
    text = describe_run_switches(
        "daily_body",
        flatten_at_close=True,
    )
    assert "开盘箱体" in text or "箱体" in text
    assert "ETH" in text
    assert "信号周期" in text
    assert "冻结" in text or "极值" in text or "本段" in text
