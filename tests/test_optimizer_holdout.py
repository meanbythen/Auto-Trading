"""策略优化的分段规则。

前一段只负责打分。分数接近时留更靠近出厂参数的一组。
后一段不参加挑选，只判断这组能不能写入：
前一段要赚钱；后一段可以把赚变成小亏，但不能亏掉一半以上。
"""

import copy
from types import SimpleNamespace

import pandas as pd
import pytest

from src.backtest.engine import BacktestResult
from src.backtest.evaluation import EvaluationReport, EvaluationThresholds
from src.backtest.metrics import TradeRecord, calc_performance
from src.backtest.optimizer import (
    HOLDOUT_LOSS_RATIO,
    _apply_params,
    _evaluate_champion_full_range,
    _factory_values,
    _finest_grid_timeframe,
    _holdout_perf_for_params,
    _sample_combos,
    _trades_on_or_after,
    clamp_holdout_pct,
    describe_params,
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


def test_holdout_replays_only_the_holdout_window(monkeypatch) -> None:
    """检验段要独立重跑：只给检验区间（前面带 warmup），不再从头跑一遍。"""
    from src.backtest import optimizer as optimizer_module

    seen: dict[str, object] = {}

    class _Perf:
        def to_dict(self) -> dict:
            return {}

    class _Full:
        performance = _Perf()
        trades: list = []

    def fake_run(_cfg, **kwargs):
        seen.update(kwargs)
        return _Full()

    monkeypatch.setattr(optimizer_module, "run_backtest", fake_run)
    bars = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=[pd.Timestamp("2026-10-20 09:00")],
    )
    _holdout_perf_for_params(
        {"strategy": {"engine": "trend_follow"}, "trade": {}, "risk": {}},
        symbol="bu",
        engine_name="trend_follow",
        params={},
        bars_signal=bars,
        bars_trail=None,
        holdout_start_ts=pd.Timestamp("2026-10-15"),
        holdout_end_ts=pd.Timestamp("2026-10-20"),
    )
    assert str(seen["start_date"]).startswith("2026-10-15")
    assert str(seen["end_date"]).startswith("2026-10-20")


def test_perf_block_matches_overall_fields() -> None:
    """训练/检验成绩和整体结果一样，都写出成交、净盈亏、盈利因子、最大回撤、盈利覆盖。"""
    lines = format_perf_block(
        "训练成绩",
        {
            "trades": 10,
            "wins": 6,
            "losses": 4,
            "win_rate": 0.6,
            "avg_win": 300,
            "avg_loss": 150,
            "payoff": 2.0,
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
    assert "盈亏比    2.00" in text
    assert "收益期望  0.80" in text
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


def test_no_holdout_trade_is_not_accepted() -> None:
    """后一段没有成交就没有样本外证据，不能当成通过了。"""
    ok, reason = judge_holdout(1000, 0, 0)
    assert ok is False
    assert "没有成交" in reason


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
    """精简只搜 ETH、信号周期、止损触发方式和通用止损开关；标准再加截止钟点、时段和四种通用止损；完整覆盖配置页全部结构项。"""
    light = grid_for_engine("daily_body", "精简")
    standard = grid_for_engine("daily_body", "标准")
    full = grid_for_engine("daily_body", "完整")
    assert set(light) == {"eth_minutes", "timeframe", "stop_mode", "generic_stop.kind"}
    assert light["eth_minutes"] == [15, 20, 30]
    assert light["timeframe"] == ["5", "15"]
    assert light["stop_mode"] == ["touch", "close"]
    assert light["generic_stop.kind"] == ["off", "sar"]
    assert set(standard) == {
        "eth_minutes",
        "eth_minutes2",
        "timeframe",
        "day_cutoff",
        "night_cutoff",
        "session_scope",
        "stop_mode",
        "generic_stop.kind",
    }
    assert standard["generic_stop.kind"] == ["off", "sar", "prior_extreme", "chandelier"]
    # 10:15～10:30 是上午休盘，截止 10:15 和 10:30 会拦住同一批 K 线；日盘避开这段。
    assert standard["day_cutoff"] == ["09:45", "10:15", "10:45"]
    assert set(full) == set(standard) | {
        "stop_extra_ticks",
        "skip_gap_atr_mult",
        "max_box_atr_mult",
        "max_chase_atr_mult",
        "ema_period",
        "ema_buffer_ticks",
        "generic_stop.lookback",
        "generic_stop.sar_af_step",
        "generic_stop.atr_period",
        "generic_stop.atr_mult",
    }
    light_n = 1
    for vals in light.values():
        light_n *= len(vals)
    standard_n = 1
    for vals in standard.values():
        standard_n *= len(vals)
    full_n = 1
    for vals in full.values():
        full_n *= len(vals)
    assert light_n <= 30
    assert standard_n <= 3000
    assert full_n > 100000


def test_float_hold_grid_has_late_window_and_be() -> None:
    """浮盈飘单精简搜观察窗口、止损宽度、是否收盘清仓。"""
    from src.backtest.optimizer import default_grid_for_engine

    light = default_grid_for_engine("float_hold", "精简")
    standard = default_grid_for_engine("float_hold", "标准")
    assert 60 in light["eth_minutes"]
    assert 1.5 in light["stop_atr_mult"]
    assert False in light["flatten_at_close"]
    assert "be_atr_mult" in standard
    assert "14:45" in standard["day_cutoff"]
    assert "eth_minutes2" not in standard
    assert "be_r_mult" not in standard
    assert "stop_extra_ticks" not in standard


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
    assert _finest_grid_timeframe({"macd_timeframe": ["5", "1"]}, "5") == "1"


def test_float_hold_params_write_macd_pair_and_flatten() -> None:
    """MACD 组合拆成三根参数；均线对拆开；收盘清仓写到公共开关，并可关掉拿到隔夜。"""
    cfg = {
        "strategy": {
            "engine": "float_hold",
            "timeframe": "15",
            "flatten_at_close": True,
            "float_hold": {},
        }
    }
    out = _apply_params(
        cfg,
        {
            "macd_timeframe": "1",
            "macd_combo": "5/34/5",
            "dir_ema_pair": "20/60",
            "flatten_at_close": False,
        },
        "float_hold",
    )
    st = out["strategy"]
    hold = st["float_hold"]
    assert hold["direction"] == "macd_cycle"
    assert hold["macd_timeframe"] == "1"
    assert hold["macd_fast"] == 5
    assert hold["macd_slow"] == 34
    assert hold["macd_signal"] == 5
    assert hold["dir_ema_fast"] == 20
    assert hold["dir_ema_slow"] == 60
    assert st["timeframe"] == "1"
    assert st["flatten_at_close"] is False


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
            "daily_body": {"day_cutoff": "10:30", "night_cutoff": "22:15", "max_box_atr_mult": 4.0},
        },
        "schedule": {"session_scope": "day"},
    }
    out = _apply_params(cfg, {"eth_minutes": 30, "timeframe": "5"}, "daily_body")
    assert out["schedule"]["session_scope"] == "day"
    assert out["strategy"]["daily_body"]["max_box_atr_mult"] == 4.0
    assert out["strategy"]["daily_body"]["day_cutoff"] == "10:30"


def test_every_optimized_param_is_a_config_page_item() -> None:
    """优化网格里的每个参数都能在配置页改，写入配置时就是改配置页那一项。"""
    ui_covered = {
        # 日内实体
        "eth_minutes",
        "eth_minutes2",
        "timeframe",
        "day_cutoff",
        "night_cutoff",
        "session_scope",
        "stop_mode",
        "stop_extra_ticks",
        "skip_gap_atr_mult",
        "max_box_atr_mult",
        "max_chase_atr_mult",
        "ema_period",
        "ema_buffer_ticks",
        # 通用止损（止损设置页）
        "generic_stop.kind",
        "generic_stop.lookback",
        "generic_stop.sar_af_step",
        "generic_stop.atr_period",
        "generic_stop.atr_mult",
        # 趋势跟随 / SuperTrend
        "entry_bars",
        "atr_mult",
        "atr_period",
        "multiplier",
        # 双均线 / 布林 / SAR / 多因子（新加三套也在配置页可改）
        "short_period",
        "long_period",
        "reverse_on_cross",
        "atr_stop_mult",
        "slope_min_deg",
        "slope_lookback",
        "slope_exit",
        "no_open_before_close_min",
        "macd_combo",
        "dir_ema_pair",
        "sig_ema_triple",
        "stop_ema",
        "side_ema",
        "slope_ema",
        "swing_side_bars",
        "boll_window",
        "boll_dev",
        "ma_filter",
        "af_step",
        "threshold",
        "ma_short",
        "ma_long",
        "channel",
        # 长线趋势加减仓
        "daily_ma",
        "add_min_atr",
        "reduce_extend_atr",
        "max_adds",
        "vol_mult",
        "lookback",
        "height_atr",
        "adx_min",
        "trail_atr",
        "trail_start_h",
    }
    for engine in (
        "daily_body",
        "trend_follow",
        "supertrend",
        "dual_ma",
        "ema_swing",
        "bollinger",
        "sar_trend",
        "multi_factor",
        "swing_trend",
        "swing_box",
    ):
        for depth in ("精简", "标准", "完整"):
            assert set(grid_for_engine(engine, depth)) <= ui_covered, (engine, depth)


def test_generic_stop_params_write_under_strategy_root() -> None:
    """通用止损是公共设置：任何策略的网格都写回 strategy.generic_stop，不留在策略小节。"""
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "generic_stop": {"kind": "off", "lookback": 20, "sar_af_start": 0.02},
            "trend_follow": {"entry_bars": 36},
        }
    }
    out = _apply_params(
        cfg,
        {"generic_stop.kind": "SAR", "generic_stop.lookback": "40", "generic_stop.atr_mult": 2.5},
        "trend_follow",
    )
    st = out["strategy"]
    assert st["generic_stop"]["kind"] == "sar"
    assert isinstance(st["generic_stop"]["lookback"], int)
    assert st["generic_stop"]["lookback"] == 40
    assert isinstance(st["generic_stop"]["atr_mult"], float)
    assert st["generic_stop"]["atr_mult"] == 2.5
    assert "generic_stop.kind" not in st["trend_follow"]


def test_factory_values_cover_generic_stop() -> None:
    """出厂值要覆盖新加的通用止损项，分数接近时才能正确比较距离。"""
    for engine in ("daily_body", "trend_follow", "supertrend", "dual_ma", "ema_swing", "swing_trend"):
        factory = _factory_values(engine)
        assert factory["generic_stop.kind"] == "off"
        assert factory["generic_stop.lookback"] == 20
    body_factory = _factory_values("daily_body")
    assert body_factory["skip_gap_atr_mult"] == 2.5
    assert body_factory["max_box_atr_mult"] == 5.0
    assert body_factory["ema_buffer_ticks"] == 2


def test_describe_params_writes_chinese_labels_and_values() -> None:
    """优化结果里的参数要带中文说明，开关型参数再带上代号，方便对照配置页。"""
    text = describe_params(
        {
            "eth_minutes": 15,
            "timeframe": "5",
            "stop_mode": "close",
            "session_scope": "night",
            "generic_stop.kind": "sar",
            "generic_stop.atr_mult": 2.5,
        }
    )
    assert "第一ETH窗口（分钟）=15" in text
    assert "信号周期（分钟）=5" in text
    assert "止损触发方式=收盘价确认（close）" in text
    assert "交易时段=只做夜盘（night）" in text
    assert "通用止损类型=SAR 移动止损（sar）" in text
    assert "通用止损 ATR 倍数=2.5" in text


def test_sample_combos_covers_every_value_before_random_fill() -> None:
    """组合太多时要分层抽样：每个旋钮的每个取值都至少出现一次，再随机补足。"""
    values = [[1, 2, 3], [10, 20], [100, 200, 300, 400]]
    got = _sample_combos(["a", "b", "c"], values, 12)
    assert len(got) == 12
    assert len(set(got)) == 12
    for i, vals in enumerate(values):
        assert {combo[i] for combo in got} == set(vals)
    # 固定种子：同样网格再抽一次是同一批。
    assert _sample_combos(["a", "b", "c"], values, 12) == got


def test_sample_combos_runs_all_when_combos_fit() -> None:
    """理论组合不超过上限时全部跑，不再抽。"""
    values = [[1, 2], [10, 20]]
    got = _sample_combos(["a", "b"], values, 80)
    assert len(got) == 4


def test_sample_combos_handles_huge_grid_without_expanding_it() -> None:
    """完整网格有几百万组：只按需要取几组，不能把全部组合展开。"""
    values = [[0, 1, 2, 3, 4, 5]] * 10
    got = _sample_combos(list("abcdefghij"), values, 12)
    assert len(got) == 12
    assert len(set(got)) == 12
    for i in range(10):
        assert {combo[i] for combo in got} == set(values[i])


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


def test_param_table_defaults_match_builtin(tmp_path) -> None:
    """按内置默认生成的 Excel，读回来要和代码里的网格完全一致。"""
    from src.backtest.optimizer import default_grid_for_engine
    from src.backtest.param_table import DEPTHS, ENGINE_NAMES, build_default_table, read_param_table

    path = build_default_table(tmp_path / "优化参数.xlsx")
    table = read_param_table(path)
    assert table.labels["eth_minutes"] == "第一ETH窗口（分钟）"
    for engine in ENGINE_NAMES:
        for depth in DEPTHS:
            default_grid = default_grid_for_engine(engine, depth)
            if not default_grid:
                # 固定规则引擎本来就没有可搜参数，表里不写行，交给内置默认。
                assert table.grid(engine, depth) is None
                continue
            assert table.grid(engine, depth) == default_grid, (engine, depth)


def test_param_table_edits_change_grid_and_can_switch_off_a_knob(tmp_path) -> None:
    """改 Excel 的「取值」和「启用」后，读回来的网格跟着变；填「否」的那一项不再搜。"""
    import openpyxl

    from src.backtest.optimizer import default_grid_for_engine
    from src.backtest.param_table import build_default_table, read_param_table

    path = build_default_table(tmp_path / "优化参数.xlsx")
    book = openpyxl.load_workbook(path)
    sheet = book["参数网格"]
    for row in sheet.iter_rows(min_row=2):
        engine, depth, key = row[0].value, row[2].value, row[3].value
        if engine == "daily_body" and depth == "标准" and key == "eth_minutes":
            row[5].value = "20,30"
        if engine == "daily_body" and depth == "标准" and key == "stop_mode":
            row[6].value = "否"
    book.save(path)
    book.close()

    table = read_param_table(path)
    grid = table.grid("daily_body", "标准")
    assert grid is not None
    assert grid["eth_minutes"] == [20, 30]
    assert "stop_mode" not in grid
    # 其余参数不受影响，仍和内置默认一致。
    default_grid = default_grid_for_engine("daily_body", "标准")
    for key, values in grid.items():
        if key == "eth_minutes":
            continue
        assert values == default_grid[key]


def test_label_sheet_rows_are_split_by_strategy(tmp_path) -> None:
    """「参数名称」表按策略拆行，带策略名称列和自动筛选，可直接按策略筛参数。"""
    import openpyxl

    from src.backtest.optimizer import default_grid_for_engine
    from src.backtest.param_table import (
        DEPTHS,
        LABEL_HEADERS,
        SHEET_LABELS,
        build_default_table,
        read_param_table,
    )

    path = build_default_table(tmp_path / "优化参数.xlsx")
    book = openpyxl.load_workbook(path, data_only=True)
    sheet = book[SHEET_LABELS]
    assert [c.value for c in sheet[1]] == list(LABEL_HEADERS)
    assert sheet.auto_filter.ref == f"A1:C{sheet.max_row}"
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    book.close()

    daily_keys = set()
    for depth in DEPTHS:
        daily_keys.update(default_grid_for_engine("daily_body", depth))
    daily_rows = {key for name, key, _ in rows if name == "日内实体（主推）"}
    assert daily_rows == daily_keys
    assert ("日内实体（主推）", "eth_minutes", "第一ETH窗口（分钟）") in rows
    # 同一策略内同一个参数键只出现一次；读表仍能拿到中文名。
    assert len(rows) == len({(name, key) for name, key, _ in rows})
    assert read_param_table(path).labels["eth_minutes"] == "第一ETH窗口（分钟）"


def test_param_table_without_rows_falls_back_to_builtin(tmp_path) -> None:
    """表里某个策略 + 深度一行都没有时返回 None，让调用方回退内置默认。"""
    import openpyxl

    from src.backtest.param_table import GRID_HEADERS, read_param_table, SHEET_GRID

    path = tmp_path / "空表.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = SHEET_GRID
    sheet.append(list(GRID_HEADERS))
    book.save(path)

    table = read_param_table(path)
    assert table.grid("daily_body", "标准") is None


def test_param_table_parses_text_time_and_json_values() -> None:
    """取值支持逗号、JSON 数组；周期/钟点保持文字，数值仍写成数字。"""
    from src.backtest.param_table import parse_values

    assert parse_values("15,20,30", "eth_minutes") == [15, 20, 30]
    assert parse_values("5,15", "timeframe") == ["5", "15"]
    assert parse_values("10:00,10:15", "day_cutoff") == ["10:00", "10:15"]
    assert parse_values("true,false", "reverse_on_cross") == [True, False]
    assert parse_values("1,0", "reverse_on_cross") == [True, False]
    assert parse_values("[1.5, 2.0, 2.5]", "atr_mult") == [1.5, 2.0, 2.5]


def test_current_params_from_config_reads_daily_body_and_public_keys() -> None:
    """对照用的是配置里正在用的旋钮，不是出厂值。"""
    from src.backtest.optimizer import current_params_from_config

    cfg = {
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "stop_mode": "touch",
            "daily_body": {"eth_minutes": 30, "day_cutoff": "10:00"},
            "generic_stop": {"kind": "sar", "lookback": 8},
        },
        "schedule": {"session_scope": "day"},
    }
    got = current_params_from_config(
        cfg,
        "daily_body",
        ["eth_minutes", "timeframe", "stop_mode", "session_scope", "generic_stop.kind"],
    )
    assert got["eth_minutes"] == 30
    assert got["timeframe"] == "15"
    assert got["stop_mode"] == "touch"
    assert got["session_scope"] == "day"
    assert got["generic_stop.kind"] == "sar"


def test_judge_better_requires_holdout_not_worse() -> None:
    """检验段净赚或回撤变差时，不能一键切实盘。"""
    from src.backtest.optimizer import judge_better_than_current

    old = {"net_pnl": 1000, "max_drawdown": 200, "profit_factor": 1.5}
    worse = judge_better_than_current(
        {"net_pnl": 400, "max_drawdown": 180, "profit_factor": 1.2},
        old,
        accepted=True,
        new_params={"a": 2},
        old_params={"a": 1},
    )
    assert worse["can_apply_live"] is False

    reject = judge_better_than_current(
        {"net_pnl": 2000, "max_drawdown": 100, "profit_factor": 2.0},
        old,
        accepted=False,
        new_params={"a": 2},
        old_params={"a": 1},
    )
    assert reject["can_apply_live"] is False

    same = judge_better_than_current(
        {"net_pnl": 1200, "max_drawdown": 180, "profit_factor": 1.6},
        old,
        accepted=True,
        new_params={"a": 1},
        old_params={"a": 1},
    )
    assert same["can_apply_live"] is False

    better = judge_better_than_current(
        {"net_pnl": 1200, "max_drawdown": 180, "profit_factor": 1.6},
        old,
        accepted=True,
        new_params={"a": 2},
        old_params={"a": 1},
    )
    assert better["can_apply_live"] is True
    assert "全面优于" in better["reason"]


def _champion_eval_result(trades_count: int = 40) -> BacktestResult:
    """构造一段确定的全区间结果，避免评估口径测试依赖行情和指标计算。"""
    trades = []
    equity = [100_000.0]
    equity_times = ["2026-10-01 09:00:00"]
    for index in range(trades_count):
        pnl = 100.0 if index % 4 else -50.0
        day = 1 + index // 10
        minute = index % 10
        trade = TradeRecord(
            symbol="fu2701",
            side="long",
            entry_time=f"2026-10-{day:02d} 09:{minute:02d}:00",
            exit_time=f"2026-10-{day:02d} 10:{minute:02d}:00",
            entry_price=100.0,
            exit_price=101.0 if pnl > 0 else 99.0,
            volume=1,
            pnl=pnl,
            adx_at_entry=25.0,
        )
        trades.append(trade)
        equity.append(equity[-1] + pnl)
        equity_times.append(trade.exit_time)
    return BacktestResult(
        symbol="fu2701",
        timeframe="5",
        trail_timeframe="5",
        bars=500,
        performance=calc_performance(trades, equity, min_trades=1),
        trades=trades,
        equity=equity,
        equity_times=equity_times,
        params_snapshot={
            "capital": 100_000.0,
            "data_from": "2026-10-01",
            "data_to": "2026-10-31",
        },
    )


def test_champion_evaluation_and_stress_share_full_range_config(monkeypatch) -> None:
    """主评估、滑点和扰动都用冠军配置与完整区间，扰动基准也来自全区间 PF。"""
    cfg = {
        "strategy": {
            "engine": "trend_follow",
            "trend_follow": {"entry_bars": 36, "atr_mult": 2.0},
        },
        "backtest": {"slippage_ticks": 1.0},
    }
    original = copy.deepcopy(cfg)
    eval_result = _champion_eval_result(40)
    full_calls = []
    slip_calls = []
    pert_calls = []

    def full_runner(run_cfg, **kwargs):
        full_calls.append((copy.deepcopy(run_cfg), dict(kwargs)))
        return eval_result

    def fake_slippage(run_cfg, **kwargs):
        slip_calls.append((copy.deepcopy(run_cfg), dict(kwargs)))
        return {
            "profit_factor": 1.6,
            "base_ticks": 1.0,
            "stress_ticks": 2.0,
            "error": "",
        }

    def fake_perturbation(run_cfg, **kwargs):
        pert_calls.append((copy.deepcopy(run_cfg), dict(kwargs)))
        return {
            "ok": True,
            "cliff": False,
            "points_count": 4,
            "worst_pf": 1.5,
            "error": "",
        }

    monkeypatch.setattr("src.backtest.stress.run_slippage_stress", fake_slippage)
    monkeypatch.setattr("src.backtest.stress.run_perturbation", fake_perturbation)
    training_snapshot = {"segment": "train", "entry_bars": 48}
    report, slip, pert = _evaluate_champion_full_range(
        cfg,
        best_params={"entry_bars": 48, "atr_mult": 2.5},
        symbol="fu2701",
        engine_name="trend_follow",
        param_keys=["entry_bars", "atr_mult"],
        start_date="2026-10-01",
        end_date="2026-10-31",
        thresholds=EvaluationThresholds(stress_min_trades=30),
        runner=full_runner,
    )

    assert not report.skipped
    assert slip["profit_factor"] == 1.6
    assert pert["points_count"] == 4
    assert len(full_calls) == 1
    full_cfg, full_kwargs = full_calls[0]
    assert full_cfg["strategy"]["trend_follow"] == {"entry_bars": 48, "atr_mult": 2.5}
    assert full_kwargs["start_date"] == "2026-10-01"
    assert full_kwargs["end_date"] == "2026-10-31"
    assert "bars_signal" not in full_kwargs
    assert cfg == original
    assert training_snapshot == {"segment": "train", "entry_bars": 48}

    for stress_cfg, stress_kwargs in (slip_calls[0], pert_calls[0]):
        assert stress_cfg == full_cfg
        assert stress_kwargs["start_date"] == "2026-10-01"
        assert stress_kwargs["end_date"] == "2026-10-31"
        assert stress_kwargs["runner"] is full_runner
    assert pert_calls[0][1]["base_pf"] == eval_result.performance.profit_factor
    assert pert_calls[0][1]["param_keys"] == ["entry_bars", "atr_mult"]


def test_champion_stress_threshold_uses_full_range_trade_count() -> None:
    """全区间不足 30 笔就不跑压力，不能拿训练段笔数代替。"""
    calls = []

    def runner(_cfg, **kwargs):
        calls.append(kwargs)
        return _champion_eval_result(29)

    _report, slip, pert = _evaluate_champion_full_range(
        {"strategy": {"engine": "trend_follow", "trend_follow": {"entry_bars": 36}}},
        best_params={"entry_bars": 48},
        symbol="fu2701",
        engine_name="trend_follow",
        param_keys=["entry_bars"],
        start_date=None,
        end_date=None,
        thresholds=EvaluationThresholds(stress_min_trades=30),
        runner=runner,
    )
    assert len(calls) == 1
    assert slip == {}
    assert pert == {}


def test_champion_full_range_failure_returns_skipped_report() -> None:
    """全区间复跑失败要留下明确原因，不能退回训练段评估冒充成功。"""

    def broken_runner(_cfg, **_kwargs):
        raise RuntimeError("测试行情失败")

    report, slip, pert = _evaluate_champion_full_range(
        {"strategy": {"engine": "trend_follow", "trend_follow": {"entry_bars": 36}}},
        best_params={"entry_bars": 48},
        symbol="fu2701",
        engine_name="trend_follow",
        param_keys=["entry_bars"],
        start_date="2026-10-01",
        end_date="2026-10-31",
        thresholds=EvaluationThresholds(),
        runner=broken_runner,
    )
    assert "冠军参数全区间评估失败" in report.skipped
    assert "测试行情失败" in report.skipped
    assert slip == {}
    assert pert == {}


@pytest.mark.parametrize("gate_on_optimize", [True, False])
@pytest.mark.parametrize(("skipped", "conclusion"), [("测试评估失败", "watch"), ("", "reject")])
def test_optimization_evaluation_gate_and_training_snapshot(
    monkeypatch,
    tmp_path,
    gate_on_optimize: bool,
    skipped: str,
    conclusion: str,
) -> None:
    """评估失败或淘汰时门禁开启则拦截、关闭只提示；best_limits 始终来自训练段。"""
    from src.backtest.optimizer import optimize_strategy

    bars = pd.DataFrame(
        {
            "open": [100.0] * 100,
            "high": [101.0] * 100,
            "low": [99.0] * 100,
            "close": [100.0] * 100,
            "volume": [1] * 100,
        },
        index=pd.date_range("2026-01-01", periods=100, freq="min"),
    )

    class Provider:
        def __init__(self, _cfg):
            pass

        def fetch(self, *_args, **_kwargs):
            return bars

    training_result = _champion_eval_result(5)
    training_result.params_snapshot = {
        "segment": "train",
        "entry_bars": 48,
        "slippage_ticks": 1.0,
        "price_tick": 1.0,
    }
    eval_calls = []

    def train_runner(_cfg, **_kwargs):
        return training_result

    def fake_full_eval(_cfg, **kwargs):
        eval_calls.append(kwargs)
        return EvaluationReport(skipped=skipped, conclusion=conclusion), {}, {}

    monkeypatch.setattr("src.backtest.optimizer.KlineProvider", Provider)
    monkeypatch.setattr("src.backtest.optimizer.run_backtest", train_runner)
    monkeypatch.setattr(
        "src.backtest.optimizer._score_grid_on_slice",
        lambda *_args, **_kwargs: [
            {
                "params": {"entry_bars": 48},
                "score": 1.0,
                "performance": {"net_pnl": 1000.0, "profit_factor": 1.8, "trades": 20},
            }
        ],
    )
    monkeypatch.setattr(
        "src.backtest.optimizer._holdout_perf_for_params",
        lambda *_args, **_kwargs: ({"net_pnl": 100.0, "trades": 3}, []),
    )
    monkeypatch.setattr("src.backtest.optimizer._evaluate_champion_full_range", fake_full_eval)

    cfg = {
        "strategy": {
            "engine": "trend_follow",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["fu2701"],
            "trend_follow": {"entry_bars": 36},
        },
        "trade": {"executor": "sim"},
        "risk": {"risk_per_trade_pct": 0.01},
        "evaluation": {"enabled": True, "gate_on_optimize": gate_on_optimize},
    }
    original = copy.deepcopy(cfg)
    result = optimize_strategy(
        cfg,
        symbol="fu2701",
        grid={"entry_bars": [48]},
        start_date="2026-01-01",
        end_date="2026-01-02",
    )

    assert cfg == original
    assert result.accepted is (not gate_on_optimize)
    assert result.evaluation_ok is False
    assert result.best_limits == {
        "segment": "train",
        "entry_bars": 48,
        "slippage_ticks": 1.0,
        "price_tick": 1.0,
    }
    assert eval_calls[0]["best_params"] == {"entry_bars": 48}
    assert eval_calls[0]["start_date"] == "2026-01-01"
    assert eval_calls[0]["end_date"] == "2026-01-02"
    if gate_on_optimize:
        assert "这次不写入" in result.evaluation_reason
    else:
        assert "仅提示，不拦截写入" in result.evaluation_reason

    import json

    from src.backtest.optimizer import save_optimization_report

    path = save_optimization_report(result, tmp_path / "optimization.json")
    folder = path.parent / "experiments" / result.experiment["experiment_id"]
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "optimization"
    assert manifest["details"]["random_seed"] == 42
    assert manifest["review_gate"]["snapshot_complete"] is True
    assert manifest["promotion"]["live_approval"] == "not_approved"
    assert manifest["data"]["files"]
    assert cfg == original


def test_optimize_walk_mode_keeps_rolling_search(monkeypatch) -> None:
    """滚动前进仍按段搜索、按段验收，结果里记下实际段数和每段明细。"""
    from src.backtest.optimizer import MODE_WALK, optimize_strategy

    bars = pd.DataFrame(
        {
            "open": [100.0] * 200,
            "high": [101.0] * 200,
            "low": [99.0] * 200,
            "close": [100.0] * 200,
            "volume": [1] * 200,
        },
        index=pd.date_range("2026-01-01", periods=200, freq="min"),
    )

    class Provider:
        def __init__(self, _cfg):
            pass

        def fetch(self, *_args, **_kwargs):
            return bars

    progress_phases: list[str] = []

    monkeypatch.setattr("src.backtest.optimizer.KlineProvider", Provider)
    monkeypatch.setattr(
        "src.backtest.optimizer.run_backtest", lambda *_a, **_k: _champion_eval_result(5)
    )
    monkeypatch.setattr(
        "src.backtest.optimizer._score_grid_on_slice",
        lambda *_args, **_kwargs: [
            {
                "params": {"entry_bars": 48},
                "score": 1.0,
                "performance": {"net_pnl": 1000.0, "profit_factor": 1.8, "trades": 20},
            }
        ],
    )
    monkeypatch.setattr(
        "src.backtest.optimizer._holdout_perf_for_params",
        lambda *_args, **_kwargs: ({"net_pnl": 100.0, "trades": 3}, []),
    )

    result = optimize_strategy(
        {
            "strategy": {
                "engine": "trend_follow",
                "timeframe": "5",
                "trail_timeframe": "5",
                "symbols": ["fu2701"],
                "trend_follow": {"entry_bars": 36},
            },
            "risk": {"capital": 100_000},
        },
        symbol="fu2701",
        grid={"entry_bars": [48]},
        start_date="2026-01-01",
        end_date="2026-01-02",
        mode=MODE_WALK,
        walk_steps=3,
        progress=lambda done, total, phase: progress_phases.append(phase),
    )

    assert result.mode == MODE_WALK
    assert result.walk_steps >= 1
    assert len(result.walk_folds) == result.walk_steps
    assert result.trials[0]["params"] == {"entry_bars": 48}
    assert any(phase.startswith("第1段 冠军复跑") for phase in progress_phases)


def test_load_optimization_bars_rejects_short_history(monkeypatch) -> None:
    """K 线不足 50 根时直接报错，避免拿一小段行情挑参数。"""
    from src.backtest.optimizer import _load_optimization_bars

    class Provider:
        def __init__(self, _cfg):
            pass

        def fetch(self, *_args, **_kwargs):
            return pd.DataFrame(
                {"open": [], "high": [], "low": [], "close": [], "volume": []}
            )

    monkeypatch.setattr("src.backtest.optimizer.KlineProvider", Provider)

    with pytest.raises(RuntimeError, match="优化用K线不足"):
        _load_optimization_bars(
            {"strategy": {"timeframe": "5", "trail_timeframe": "5"}},
            symbol="fu2701",
            engine_name="trend_follow",
            grid={"entry_bars": [48]},
            start_date=None,
            end_date=None,
        )


def test_once_cut_search_without_usable_trials(monkeypatch) -> None:
    """所有参数组都失败时不给冠军，也不写回任何参数。"""
    from src.backtest.optimizer import _once_cut_search

    bars = pd.DataFrame(
        {
            "open": [100.0] * 120,
            "high": [101.0] * 120,
            "low": [99.0] * 120,
            "close": [100.0] * 120,
            "volume": [1] * 120,
        },
        index=pd.date_range("2026-01-01", periods=120, freq="min"),
    )
    monkeypatch.setattr(
        "src.backtest.optimizer._score_grid_on_slice",
        lambda *_args, **_kwargs: [
            {"params": {"entry_bars": 48}, "score": -999, "error": "回测失败"}
        ],
    )

    outcome = _once_cut_search(
        {"strategy": {"engine": "trend_follow"}, "risk": {"capital": 100_000}},
        symbol="fu2701",
        engine_name="trend_follow",
        combos=[(48,)],
        keys=["entry_bars"],
        bars_signal=bars,
        bars_trail=None,
        min_trades=3,
        train_ratio=0.7,
        holdout_pct=30.0,
        cross_n=0,
        total_steps=3,
        report=lambda *_args: None,
    )

    assert outcome.best_params == {}
    assert outcome.best_result is None
    assert "成交太少" in outcome.reason

