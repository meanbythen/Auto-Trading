"""品种优选：同样手数、不看保证金，至少两项指标超过基准才进备选。"""

from pathlib import Path

from src.backtest.metrics import TradeRecord, peak_margin
from src.screen.picker import (
    MAIN_UNIVERSE,
    actual_span,
    contracts_for_cross,
    engines_for_cross,
    reference_universe_text,
    bar_count_text,
    baseline_from_report,
    better_items,
    cached_bar_count,
    choice_labels,
    clear_saved_picks,
    code_from_label,
    is_candidate,
    liquidity_text,
    list_rows,
    board_page,
    prepare_screen_cfg,
    listed_cross_engines,
    screen_cross,
    screen_strategies,
    screen_symbols,
    trade_frequency,
)


def _row(**kwargs):
    base = {
        "trades": 10,
        "win_rate": 0.4,
        "profit_factor": 1.0,
        "max_drawdown": 1000,
        "error": "",
    }
    base.update(kwargs)
    return base


def test_two_metrics_better_is_a_candidate() -> None:
    baseline = _row(net_pnl=100, profit_factor=1.0, max_drawdown=2000)
    row = _row(net_pnl=300, profit_factor=1.4, max_drawdown=2000, win_rate=0.3)
    assert better_items(row, baseline) == ["净盈亏", "盈利因子"]
    assert is_candidate(row, baseline)


def test_one_metric_or_too_few_trades_is_not_a_candidate() -> None:
    baseline = _row()
    only_win = _row(win_rate=0.9)
    assert better_items(only_win, baseline) == []
    assert not is_candidate(only_win, baseline)
    tiny = _row(trades=2, win_rate=0.9, profit_factor=3, max_drawdown=1)
    assert not is_candidate(tiny, baseline)


def test_smaller_drawdown_counts_and_a_tie_does_not() -> None:
    baseline = _row(max_drawdown=800)
    assert "最大回撤" in better_items(_row(max_drawdown=799), baseline)
    assert "最大回撤" not in better_items(_row(max_drawdown=800), baseline)


def test_prepare_screen_cfg_fixes_lots_and_clears_margin_without_touching_original() -> None:
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "symbols": ["bu2612"],
            "daily_body": {"eth_minutes": 20},
        },
        "trade": {"volume": 2},
        "risk": {
            "margin_per_lot": 5000,
            "contracts": {"bu": {"multiplier": 10, "margin": 7794}},
        },
    }
    baseline = {
        "engine": "daily_body",
        "timeframe": "5",
        "trail_timeframe": "15",
        "fixed_volume": 1,
        "flatten_at_close": True,
    }
    out, volume = prepare_screen_cfg(cfg, baseline)
    assert volume == 1
    assert out["trade"]["volume"] == 1
    assert out["risk"]["margin_per_lot"] == 0
    assert out["risk"]["contracts"]["bu"]["margin"] == 0
    assert out["risk"]["contracts"]["bu"]["multiplier"] == 10
    assert out["risk"]["contracts"]["i"]["multiplier"] == 100
    assert out["strategy"]["engine"] == "daily_body"
    assert out["strategy"]["flatten_at_close"] is True
    assert cfg["risk"]["margin_per_lot"] == 5000
    assert cfg["risk"]["contracts"]["bu"]["margin"] == 7794


def test_aligned_backtest_runner_follows_rollover_switch(monkeypatch) -> None:
    """历史回测勾了主力换月，交叉测试默认也要分段，不能只测当前合约。"""
    from src.screen.picker import aligned_backtest_runner

    seen: dict[str, str] = {}

    def fake_roll(cfg, **kwargs):
        seen["path"] = "roll"
        return "rolled"

    def fake_plain(cfg, **kwargs):
        seen["path"] = "plain"
        return "plain"

    monkeypatch.setattr("src.backtest.rollover.run_backtest_rollover", fake_roll)
    monkeypatch.setattr("src.backtest.engine.run_backtest", fake_plain)
    runner = aligned_backtest_runner()
    assert runner({"backtest": {"rollover_main": True}}, symbol="fu2701") == "rolled"
    assert seen["path"] == "roll"
    assert runner({"backtest": {"rollover_main": False}}, symbol="fu2701") == "plain"
    assert seen["path"] == "plain"


def test_prepare_screen_cfg_keeps_account_capital_for_evaluation() -> None:
    """交叉测试固定手数，但评估本金必须仍是账户权益，不能换成十亿把回撤冲没。"""
    cfg = {
        "strategy": {"engine": "daily_body", "symbols": ["bu2612"]},
        "trade": {"volume": 2},
        "risk": {"capital": 93100, "margin_per_lot": 5000},
    }
    out, volume = prepare_screen_cfg(
        cfg,
        {
            "fixed_volume": 1,
            "fair_compare": True,
            "ignore_equity": True,
            "timeframe": "5",
        },
    )
    assert volume == 1
    assert out["risk"]["capital"] == 93100
    assert out["risk"]["margin_per_lot"] == 0


def test_prepare_screen_cfg_uses_page_capital_when_given() -> None:
    """交叉测试页填了测试本金时，回撤评估用这个数，不用配置页余额。"""
    cfg = {
        "strategy": {"engine": "daily_body", "symbols": ["bu2612"]},
        "trade": {"volume": 2},
        "risk": {"capital": 32790, "margin_per_lot": 5000},
    }
    out, volume = prepare_screen_cfg(
        cfg,
        settings={
            "fixed_volume": 1,
            "fair_compare": True,
            "ignore_equity": True,
            "capital": 100000,
            "timeframe": "5",
        },
    )
    assert volume == 1
    assert out["risk"]["capital"] == 100000
    assert cfg["risk"]["capital"] == 32790


def test_screen_keeps_only_symbols_that_beat_baseline_on_two_metrics() -> None:
    class Perf:
        def __init__(self, trades, win_rate, profit_factor, max_drawdown, net_pnl=0):
            self.trades = trades
            self.win_rate = win_rate
            self.profit_factor = profit_factor
            self.max_drawdown = max_drawdown
            self.net_pnl = net_pnl

    class Result:
        def __init__(self, symbol, perf):
            self.symbol = symbol
            self.performance = perf

    table = {
        "rb0": Perf(8, 0.6, 1.8, 500),
        "c0": Perf(8, 0.2, 0.4, 3000),
        "bu0": Perf(6, 0.7, 2.0, 400),
    }

    def runner(cfg, *, symbol, volume, start_date, end_date, engine):
        assert volume == 1
        assert cfg["risk"]["margin_per_lot"] == 0
        if symbol == "c0":
            raise RuntimeError("没有K线")
        return Result(symbol, table[symbol])

    # c0 会失败。再补一个只赢一项的，确认它不进备选。
    table["m0"] = Perf(8, 0.9, 0.5, 2000)
    universe = [
        ("rb0", "螺纹", 10),
        ("c0", "玉米", 10),
        ("bu0", "沥青", 10),
        ("m0", "豆粕", 10),
        ("bu2612", "沥青具体月", 10),
    ]
    result = screen_symbols(
        {"strategy": {}, "trade": {}, "risk": {}},
        {
            "symbol": "bu2612",
            "fixed_volume": 1,
            "win_rate": 0.4,
            "profit_factor": 1.0,
            "max_drawdown": 1000,
            "engine": "daily_body",
        },
        universe=universe,
        runner=runner,
    )
    codes = [row["symbol"] for row in result["candidates"]]
    assert codes == ["bu0", "rb0"]
    assert any(row["symbol"] == "c0" and row["error"] for row in result["rows"])
    assert all(row["symbol"] != "bu2612" for row in result["rows"])
    assert choice_labels(result["candidates"]) == ["bu0 沥青", "rb0 螺纹"]
    assert code_from_label("rb0 螺纹") == "rb0"
    assert code_from_label("还没有备选") == ""
    shown = list_rows(result)
    assert [row["picked"] for row in shown] == ["基准", "入选", "入选", "未入选", "没测成"]
    assert shown[0]["symbol"] == "bu2612"
    assert shown[1]["symbol"] == "沥青 bu0"
    assert shown[3]["frequency"] == "8笔"
    assert shown[3]["picked"] == "未入选"
    assert shown[1]["win_rate"].endswith("%")
    assert shown[1]["max_drawdown"].endswith("元")


def test_baseline_from_report_reads_saved_backtest() -> None:
    base = baseline_from_report(
        {
            "symbol": "bu2612",
            "performance": {
                "trades": 12,
                "win_rate": 0.42,
                "profit_factor": 1.3,
                "max_drawdown": 1800,
                "net_pnl": 900,
            },
            "params_snapshot": {
                "fixed_volume": 1,
                "engine": "daily_body",
                "timeframe": "5",
                "trail_timeframe": "15",
                "start_date": "2026-01-01",
                "end_date": "2026-10-20",
                "sizing_mode": "fixed",
            },
        }
    )
    assert base["symbol"] == "bu2612"
    assert base["fixed_volume"] == 1
    assert base["win_rate"] == 0.42
    assert base["start_date"] == "2026-01-01"
    assert base["data_source"] == ""


def test_baseline_remembers_the_quote_source() -> None:
    """回测结果里写了天勤时，基准也带着这个来源，方便品种优选下拉框跟着变。"""
    base = baseline_from_report(
        {
            "symbol": "bu0",
            "performance": {"trades": 4, "win_rate": 0.5, "profit_factor": 1.1, "max_drawdown": 100},
            "params_snapshot": {"fixed_volume": 1, "data_source": "tq"},
        }
    )
    assert base["data_source"] == "tq"


def test_prepare_screen_cfg_keeps_the_quote_source() -> None:
    """品种优选改手数和保证金时，不把已经选好的行情来源换掉。"""
    cfg = {
        "data": {"source": "tq"},
        "strategy": {"engine": "daily_body", "daily_body": {"eth_minutes": 20}},
        "trade": {"volume": 1},
        "risk": {"margin_per_lot": 0, "contracts": {}},
    }
    out, _volume = prepare_screen_cfg(cfg, {"fixed_volume": 1, "engine": "daily_body"})
    assert out["data"]["source"] == "tq"
    assert cfg["data"]["source"] == "tq"


def test_return_rate_uses_the_largest_margin_of_one_position() -> None:
    """收益率用净盈亏除以占用最多的一笔保证金。两笔先后持仓，不把保证金加总。"""
    trades = [
        TradeRecord("rb0", "long", "", "", 4000, 4010, 1, 100),
        TradeRecord("rb0", "long", "", "", 3000, 3020, 1, 200),
    ]
    # 4000×10×1×10% = 4000，比 3000 那笔占用更多。
    assert peak_margin(trades, 10, 0.10) == 4000
    row = {
        "symbol": "rb0",
        "name": "螺纹",
        "trades": 2,
        "win_rate": 1,
        "profit_factor": 2,
        "max_drawdown": 100,
        "net_pnl": 300,
        "return_rate": 300 / 4000,
        "liquidity": "正常",
        "picked": "第1名",
        "rank": 1,
    }
    shown = list_rows({"mode": "rank", "rows": [row], "settings": {"timeframe": "5"}})
    assert shown[0]["return_rate"] == "7.5%"


def test_actual_span_and_bar_count_show_what_was_tested() -> None:
    """实际时段和 K 线根数只展示，不改变排名。旧记录没记根数时，按本地行情补数。"""
    assert actual_span("2026-09-22 22:20:00", "2026-10-22 11:30:00") == "2026-09-22 ~ 2026-10-22"
    assert actual_span("", "") == "—"
    assert bar_count_text(0) == "—"
    assert bar_count_text(1024) == "1024"

    folder = Path(__file__).resolve().parent / "_kline_count_tmp"
    folder.mkdir(exist_ok=True)
    sample = folder / "pr0_5m.csv"
    sample.write_text(
        "datetime,close\n"
        "2026-09-22 22:20:00,1\n"
        "2026-09-22 22:25:00,1\n"
        "2026-10-22 11:30:00,1\n"
        "2026-10-22 11:35:00,1\n",
        encoding="utf-8",
    )
    try:
        assert cached_bar_count(
            "pr0",
            "5",
            "2026-09-22 22:20:00",
            "2026-10-22 11:30:00",
            cache_dir=folder,
        ) == 3
    finally:
        sample.unlink(missing_ok=True)
        folder.rmdir()

    class Perf:
        def __init__(self):
            self.trades = 6
            self.win_rate = 0.5
            self.profit_factor = 1.2
            self.max_drawdown = 100
            self.net_pnl = 10
            self.score = 0.4

    class Result:
        def __init__(self):
            self.performance = Perf()
            self.bars = 88
            self.params_snapshot = {
                "data_from": "2026-09-22 22:20:00",
                "data_to": "2026-10-22 11:30:00",
            }

    result = screen_symbols(
        {"strategy": {}, "trade": {}, "risk": {}},
        None,
        compare_baseline=False,
        settings={
            "engine": "daily_body",
            "timeframe": "5",
            "fixed_volume": 1,
            "fair_compare": False,
            "apply_limits": True,
            "sizing_mode": "fixed",
            "capital": 100000,
            "risk_per_trade_pct": 0.5,
            "min_volume": 1,
            "max_daily_loss": 5000,
            "min_order_interval_sec": 0,
            "max_orders_per_day": 20,
            "margin_per_lot": 0,
            "has_flatten": True,
            "flatten_at_close": True,
        },
        universe=[("rb0", "螺纹", 10, False)],
        runner=lambda cfg, *, symbol, volume, start_date, end_date, engine: Result(),
    )
    assert result["rows"][0]["bars"] == 88
    shown = list_rows(result)[0]
    assert shown["span"] == "2026-09-22 ~ 2026-10-22"
    assert shown["bars"] == "88"
    assert shown["net_pnl"] == "10元"
    assert shown["profit_cover"] == "-90元"
    assert shown["trade_count"] == str(int(result["rows"][0]["trades"]))
    assert shown["span_start"] == "2026-09-22"
    assert shown["span_end"] == "2026-10-22"
    assert shown["span_days"] == "30"
    assert shown["return_rate"] == "—"
    assert shown["picked"] == "第1名"


def test_span_cover_matches_excel_formulas() -> None:
    """盈利覆盖、每笔、日均跟导出表里手加的公式一致。"""
    from src.screen.picker import span_cover_fields

    raw = span_cover_fields(13860, 3480, 11, "2026-09-28", "2026-10-27")
    assert raw["profit_cover"] == 10380
    assert raw["trade_count"] == 11
    assert abs(raw["pnl_per_trade"] - 943.636363636364) < 1e-9
    assert raw["span_start"] == "2026-09-28"
    assert raw["span_end"] == "2026-10-27"
    assert raw["span_days"] == 29
    assert abs(raw["daily_avg"] - 357.931034482759) < 1e-9


def test_span_cover_accepts_walk_forward_days_override() -> None:
    """滚动前进的检验段是多段拼起来的，天数按各段之和，不按首尾差。"""
    from src.screen.picker import span_cover_fields

    raw = span_cover_fields(
        900,
        100,
        9,
        "2026-01-01",
        "2026-12-31",
        span_days=90,
    )
    assert raw["span_days"] == 90
    assert abs(raw["daily_avg"] - 800 / 90) < 1e-9


def test_trade_frequency_is_only_a_label() -> None:
    """几天一笔只看这段实际日期，不拿来决定入选。"""
    assert trade_frequency(0, "2026-10-01", "2026-10-22") == "没有成交"
    assert trade_frequency(6, "2026-10-01 21:05:00", "2026-10-22 15:00:00") == "6笔，约3.7天1笔"
    assert trade_frequency(10, "2026-10-01", "2026-10-02") == "10笔，约1天5.0笔"
    busy = _row(trades=20, win_rate=0.9, profit_factor=0.4, max_drawdown=5000)
    busy["frequency"] = trade_frequency(20, "2026-10-01", "2026-10-05")
    assert not is_candidate(busy, _row())


def test_rank_mode_orders_by_score_and_ignores_frequency() -> None:
    """关掉基准后，按得分排名。成交太少的不占名次，交易频率再高也不往前排。"""

    class Perf:
        def __init__(self, trades, win_rate, profit_factor, max_drawdown, score):
            self.trades = trades
            self.win_rate = win_rate
            self.profit_factor = profit_factor
            self.max_drawdown = max_drawdown
            self.score = score

    class Result:
        def __init__(self, symbol, perf):
            self.symbol = symbol
            self.performance = perf
            self.params_snapshot = {}

    scores = {"rb0": 0.2, "bu0": 0.9, "c0": 0.4}

    def runner(cfg, *, symbol, volume, start_date, end_date, engine):
        assert volume == 2
        assert cfg["risk"]["margin_per_lot"] == 0
        assert engine == "trend_follow"
        if symbol == "m0":
            # 笔数很多，但样本仍少于 3 笔的门槛时不能靠频率往前排。这里用 2 笔。
            return Result(symbol, Perf(2, 1.0, 9.0, 1, 3.0))
        if symbol == "ag0":
            raise RuntimeError("没有K线")
        return Result(symbol, Perf(8, 0.5, 1.1, 100, scores[symbol]))

    result = screen_symbols(
        {"strategy": {"daily_body": {"eth_minutes": 20}}, "trade": {}, "risk": {}},
        None,
        compare_baseline=False,
        settings={
            "engine": "trend_follow",
            "timeframe": "D",
            "trail_timeframe": "D",
            "start_date": "2026-01-01",
            "end_date": "2026-10-01",
            "fixed_volume": 2,
            "fair_compare": False,
            "apply_limits": True,
            "sizing_mode": "fixed",
            "capital": 100000,
            "risk_per_trade_pct": 0.5,
            "min_volume": 1,
            "max_daily_loss": 5000,
            "min_order_interval_sec": 0,
            "max_orders_per_day": 20,
            "margin_per_lot": 0,
            "flatten_at_close": False,
            "has_flatten": True,
        },
        universe=[
            ("rb0", "螺纹", 10),
            ("c0", "玉米", 10),
            ("bu0", "沥青", 10),
            ("m0", "豆粕", 10),
            ("ag0", "白银", 15),
        ],
        runner=runner,
    )
    assert [row["symbol"] for row in result["candidates"]] == ["bu0", "c0", "rb0"]
    shown = list_rows(result)
    assert [row["picked"] for row in shown] == ["第1名", "第2名", "第3名", "未入选", "没测成"]
    assert shown[0]["symbol"] == "沥青 bu0"
    assert shown[0]["score"] == "0.90"
    assert all(row["picked"] != "基准" for row in shown)


def test_thin_liquidity_is_labeled_and_does_not_block_selection() -> None:
    """流动性差的品种标成偏少，并且整段不进入优选回测。"""
    assert len(MAIN_UNIVERSE) >= 80
    assert liquidity_text("wr0") == "偏少"
    assert liquidity_text("wr2610") == "偏少"
    assert liquidity_text("bu2612") == "正常"
    thin = _row(win_rate=0.8, profit_factor=2.0, max_drawdown=100)
    assert is_candidate(thin, _row(win_rate=0.4, profit_factor=1.0, max_drawdown=1000))

    class Perf:
        def __init__(self):
            self.trades = 6
            self.win_rate = 0.5
            self.profit_factor = 1.2
            self.max_drawdown = 100
            self.net_pnl = 10
            self.score = 0.4

    class Result:
        def __init__(self, symbol):
            self.symbol = symbol
            self.performance = Perf()
            self.params_snapshot = {}

    result = screen_symbols(
        {"strategy": {}, "trade": {}, "risk": {}},
        None,
        compare_baseline=False,
        settings={
            "engine": "daily_body",
            "fixed_volume": 1,
            "fair_compare": False,
            "apply_limits": True,
            "sizing_mode": "fixed",
            "capital": 100000,
            "risk_per_trade_pct": 0.5,
            "min_volume": 1,
            "max_daily_loss": 5000,
            "min_order_interval_sec": 0,
            "max_orders_per_day": 20,
            "margin_per_lot": 0,
            "has_flatten": True,
            "flatten_at_close": True,
        },
        universe=[("wr0", "线材", 10, True), ("rb0", "螺纹", 10, False)],
        runner=lambda cfg, *, symbol, volume, start_date, end_date, engine: Result(symbol),
    )
    assert result["skipped_thin"] == 1
    assert [row["symbol"] for row in result["rows"]] == ["rb0"]
    shown = {row["symbol"]: row for row in list_rows(result)}
    assert "线材 wr0" not in shown
    assert shown["螺纹 rb0"]["liquidity"] == "正常"
    assert shown["螺纹 rb0"]["picked"] == "第1名"


def test_strategy_compare_keeps_one_symbol_and_ranks_each_engine() -> None:
    """固定一个合约时，每套策略各跑一遍。同一套不重复，得分高的排前面。"""

    class Perf:
        def __init__(self, score):
            self.trades = 8
            self.win_rate = 0.5
            self.profit_factor = 1.2
            self.max_drawdown = 100
            self.net_pnl = score * 1000
            self.score = score

    class Result:
        def __init__(self, score):
            self.performance = Perf(score)
            self.params_snapshot = {}
            self.bars = 10

    seen: list[tuple[str, str]] = []

    def runner(cfg, *, symbol, volume, start_date, end_date, engine):
        seen.append((symbol, engine))
        assert volume == 1
        assert start_date == "2026-01-01"
        score = 0.9 if engine == "daily_body" else 0.2
        if engine == "dual_ma":
            raise RuntimeError("没有K线")
        return Result(score)

    result = screen_strategies(
        {"strategy": {}, "trade": {}, "risk": {}},
        "rb2701",
        {
            "timeframe": "5m",
            "start_date": "2026-01-01",
            "end_date": "",
            "apply_limits": True,
            "sizing_mode": "fixed",
            "fixed_volume": 1,
            "fair_compare": False,
            "has_flatten": True,
            "flatten_at_close": True,
        },
        runner=runner,
    )
    engines = [item[0] for item in listed_cross_engines()]
    assert "custom" not in engines
    assert "macd_divergence" not in engines
    assert "liquidity_exhaustion" not in engines
    assert "mean_reversion" not in engines
    assert "sar_flip" not in engines
    assert "morning_gap_c" not in engines
    assert "morning_box_a" not in engines
    # 含长线趋势加减仓和布林/SAR/多因子；名单跟当前程序策略走。
    assert "swing_trend" in engines
    assert "ema_swing" in engines
    assert "bollinger" in engines
    assert "sar_trend" in engines
    assert "multi_factor" in engines
    assert "multi_vote" not in engines
    assert "multi_hybrid" not in engines
    assert "jesse_casper" not in engines
    assert "daily_body_expand" not in engines
    assert "swing_box" in engines
    assert "float_hold" not in engines
    from src.strategy.factory import CROSS_TEST_ENGINE_ORDER

    builtins = [code for code in engines if not str(code).startswith("rule_dsl:")]
    assert builtins[: len(CROSS_TEST_ENGINE_ORDER)] == list(CROSS_TEST_ENGINE_ORDER)
    assert len(engines) == len(set(engines))
    assert [item[0] for item in seen] == ["rb2701"] * len(engines)
    assert [item[1] for item in seen] == engines
    shown = list_rows(result)
    assert shown[0]["symbol"] == "日内实体"
    assert shown[0]["picked"] == "第1名"
    failed = next(row for row in shown if row["symbol"] == "双均线趋势")
    assert failed["picked"] == "没测成"


def test_cross_row_keeps_full_evaluation_dimensions(monkeypatch) -> None:
    """交叉测试每个组合沿用回测自带的评估，收益期望、卡尔玛、连亏和未通过项都要带出来。"""
    from src.backtest.evaluation import EvaluationReport

    monkeypatch.setattr("src.screen.picker._market_snapshot", lambda *_a, **_k: {})

    class Perf:
        trades = 40
        win_rate = 0.45
        profit_factor = 1.1
        max_drawdown = 3000
        net_pnl = 800
        score = 0.2
        payoff = 1.2
        avg_win = 300
        avg_loss = -200
        losses = 22

    class Result:
        performance = Perf()
        params_snapshot = {
            "data_from": "2026-01-01",
            "data_to": "2026-02-01",
            "margin_used": 20000,
        }
        bars = 100
        evaluation = EvaluationReport(
            metrics={
                "r_expectancy": 0.12,
                "calmar": 0.8,
                "consecutive_losses": 6,
            },
            conclusion="reject",
        )

    result = screen_cross(
        {
            "strategy": {"daily_body": {"eth_minutes": 20}},
            "trade": {},
            "risk": {"capital": 50000, "contracts": {"rb": {"margin": 5000, "multiplier": 10}}},
        },
        {
            "timeframe": "5",
            "start_date": "2026-01-01",
            "fixed_volume": 1,
            "sizing_mode": "fixed",
            "has_flatten": True,
            "flatten_at_close": True,
        },
        universe=[("rb0", "螺纹", 10, False)],
        engines=[("daily_body", "日内实体")],
        periods=["5"],
        runner=lambda _cfg, **_kwargs: Result(),
    )

    row = result["rows"][0]
    assert row["r_expectancy"] == 0.12
    assert row["calmar"] == 0.8
    assert row["consecutive_losses"] == 6
    assert row["evaluation"] == "淘汰"

    shown = list_rows(result)[0]
    assert "r_expectancy" not in shown
    assert shown["expectancy"] not in ("", None)
    assert shown["calmar"] == "0.80"
    assert shown["loss_streak"] == "6笔"
    assert shown["evaluation"] == "淘汰"


def test_cross_keeps_only_the_top_quarter_of_profitable_combos() -> None:
    """品种和策略交叉测完后，只把净赚且得分靠前的那一档标成较好。"""

    class Perf:
        def __init__(self, trades, profit_factor, net_pnl, score):
            self.trades = trades
            self.win_rate = 0.5
            self.profit_factor = profit_factor
            self.max_drawdown = 100
            self.net_pnl = net_pnl
            self.score = score

    class Result:
        def __init__(self, perf):
            self.performance = perf
            self.params_snapshot = {}
            self.bars = 20

    book = {
        ("rb0", "daily_body"): Perf(10, 2.0, 5000, 0.9),
        ("bu0", "daily_body"): Perf(10, 1.4, 800, 0.4),
        ("c0", "daily_body"): Perf(10, 1.2, 300, 0.3),
        ("ag0", "daily_body"): Perf(10, 1.1, 100, 0.2),
        ("m0", "daily_body"): Perf(10, 0.4, -500, 0.1),
        ("j0", "daily_body"): Perf(2, 9.0, 9000, 0.99),
    }

    def runner(cfg, *, symbol, volume, start_date, end_date, engine):
        assert engine == "daily_body"
        assert volume == 2
        assert cfg["risk"]["sizing_mode"] == "fixed"
        assert cfg["risk"]["margin_per_lot"] == 0
        assert cfg["risk"]["contracts"]["rb"]["margin"] == 0
        assert cfg["risk"]["capital"] == 20148
        assert cfg["risk"]["max_daily_loss"] > 10_000_000
        assert cfg["risk"]["risk_per_trade_pct"] == 20
        assert cfg["risk"]["min_order_interval_sec"] == 60
        assert cfg["risk"]["max_orders_per_day"] == 5
        assert cfg["strategy"]["daily_body"]["eth_minutes"] == 20
        if symbol == "fu0":
            raise RuntimeError("没有K线")
        return Result(book[(symbol, engine)])

    source = {
        "strategy": {
            "daily_body": {"eth_minutes": 20},
        },
        "trade": {},
        "risk": {
            "capital": 20148,
            "margin_per_lot": 8000,
            "max_daily_loss": 2000,
            "contracts": {"rb": {"margin": 50000, "multiplier": 10}},
        },
    }
    result = screen_cross(
        source,
        {
            "timeframe": "5m",
            "start_date": "2026-01-01",
            "end_date": "",
            "sizing_mode": "risk_pct",
            "capital": 20148,
            "fixed_volume": 2,
            "risk_per_trade_pct": 20,
            "min_order_interval_sec": 60,
            "max_orders_per_day": 5,
            "margin_per_lot": 8000,
            "has_flatten": True,
            "flatten_at_close": True,
            "engine": "should_be_ignored",
        },
        universe=[
            ("rb0", "螺纹", 10, False),
            ("bu0", "沥青", 10, False),
            ("c0", "玉米", 10, False),
            ("ag0", "白银", 15, False),
            ("m0", "豆粕", 10, False),
            ("j0", "焦炭", 100, False),
            ("fu0", "燃油", 10, False),
            ("wr0", "线材", 10, True),
        ],
        engines=[("daily_body", "日内实体")],
        runner=runner,
    )
    assert result["mode"] == "cross"
    assert result["skipped_thin"] == 1
    assert result["volume"] == 2
    assert "engine" not in result["settings"]
    assert source["risk"]["capital"] == 20148
    assert source["risk"]["contracts"]["rb"]["margin"] == 50000
    by_symbol = {row["symbol"]: row for row in result["rows"]}
    assert by_symbol["rb0"]["picked"] == "较好"
    assert by_symbol["bu0"]["picked"] == "一般"
    assert by_symbol["m0"]["picked"] == "一般"
    assert by_symbol["j0"]["picked"] == "未入选"
    assert by_symbol["fu0"]["picked"] == "没测成"
    assert "wr0" not in by_symbol
    shown = list_rows(result)
    assert shown[0]["symbol"] == "螺纹 rb0"
    assert shown[0]["strategy"] == "日内实体"
    assert shown[0]["picked"] == "较好"
    assert sum(1 for row in result["rows"] if row["picked"] == "较好") == 1


def test_clear_saved_picks_removes_the_file_the_window_would_redraw(tmp_path: Path) -> None:
    """导出后删掉界面要重画的那份结果，文件没有时也不报错。"""
    target = tmp_path / "symbol_picks.json"
    target.write_text('{"rows": [1, 2]}', encoding="utf-8")
    clear_saved_picks(target)
    assert not target.exists()
    clear_saved_picks(target)


def test_market_snapshot_scores_by_engine_and_skips_unfiltered_ones() -> None:
    """交叉测试的「当下行情」：接了过滤的引擎给分，没接的明确标未接入。"""
    import pandas as pd

    from src.screen.picker import _market_snapshot

    idx = pd.date_range("2026-11-01 09:00", periods=140, freq="5min")
    closes = [4000 + i + (6 if (i // 5) % 2 else 0) for i in range(140)]
    bars = pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100] * 140,
        },
        index=idx,
    )
    # 开关关着也照算：分数只作参考，和回测配置无关。
    cfg = {"strategy": {"market_filter": {"enabled": False, "mode": "trend"}}}

    row = _market_snapshot(cfg, lambda symbol, tf, n: bars, "fu2701", "5", "multi_factor")
    assert row["market_score"] is not None
    assert 0 <= float(row["market_score"]) <= 100
    assert isinstance(row["market_ok"], bool)
    assert row["market_note"]

    box = _market_snapshot(cfg, lambda symbol, tf, n: bars, "fu2701", "5", "swing_box")
    assert box == {"market_score": None, "market_ok": None, "market_note": "未接入行情过滤"}

    missing = _market_snapshot(cfg, lambda symbol, tf, n: None, "fu2701", "5", "multi_factor")
    assert missing["market_score"] is None
    assert missing["market_note"] == "K线不足"


def test_board_page_draws_a_hundred_rows_at_a_time() -> None:
    """结果太多时先画一百行，点「显示更多」再往下加，避免一打开就退出。"""
    rows = [{"picked": "较好" if index < 3 else "一般"} for index in range(1036)]
    page, shown, note = board_page(rows)
    assert len(page) == 100
    assert page[0]["picked"] == "较好"
    assert shown == 100
    assert "1036" in note and "还有 936 行" in note
    second, shown2, note2 = board_page(rows, shown)
    assert len(second) == 100
    assert shown2 == 200
    assert "还有 836 行" in note2
    last, shown3, note3 = board_page(rows, 1000)
    assert len(last) == 36
    assert shown3 == 1036
    assert "已全部显示" in note3
    short, shown4, note4 = board_page(rows[:10], 0)
    assert len(short) == 10
    assert shown4 == 10
    assert note4 == ""


def test_prepared_cross_cfg_reuses_one_template_per_engine_and_period() -> None:
    """并行进程里同一套策略、同一个周期只准备一次配置；换了策略或周期才重做。"""
    from src.screen import picker

    cfg = {"strategy": {}, "trade": {}, "risk": {}, "backtest": {}}
    spec = {"fixed_volume": 2, "fair_compare": True, "ignore_equity": True}
    picker._WORKER_PREPARED.clear()
    try:
        first = picker._prepared_cross_cfg(cfg, spec, "daily_body", "15")
        again = picker._prepared_cross_cfg(cfg, spec, "daily_body", "15")
        other_period = picker._prepared_cross_cfg(cfg, spec, "daily_body", "30")
        other_engine = picker._prepared_cross_cfg(cfg, spec, "supertrend", "15")
    finally:
        picker._WORKER_PREPARED.clear()
    assert first is again
    assert other_period is not first
    assert other_engine is not first
    # 模板里已经写进这一组的策略和周期，原配置始终保持原样。
    assert first[0]["strategy"]["engine"] == "daily_body"
    assert first[0]["strategy"]["timeframe"] == "15"
    assert first[0]["strategy"]["trail_timeframe"] == "15"
    assert "engine" not in cfg["strategy"]
    assert "timeframe" not in cfg["strategy"]


def test_cross_uses_config_contracts_and_strategies() -> None:
    """实际测试用配置里的月份合约和点名的策略，不改成主力连续，也不跑全品种。"""
    seen: list[tuple[str, str]] = []

    class Perf:
        trades = 0
        win_rate = 0.0
        profit_factor = 0.0
        max_drawdown = 0.0
        net_pnl = 0.0
        score = 0.0

    class Result:
        performance = Perf()
        params_snapshot = {}
        bars = 0

    def runner(cfg, *, symbol, volume, start_date, end_date, engine):
        seen.append((symbol, engine))
        return Result()

    contracts = contracts_for_cross(["rb2701", "SA2701", "（还没有）", "rb2701", "wr2601"])
    assert [item[0] for item in contracts] == ["rb2701", "SA2701", "wr2601"]
    assert contracts[0][1] == "螺纹"
    assert contracts[2][3] is False
    engines = engines_for_cross(
        {"strategy": {"cross_engines": ["daily_body", "supertrend", "custom", "nope", "daily_body"]}}
    )
    # 配置里点名的就测这些；已删的名字忽略。没写名单时才用全部策略。
    codes = [code for code, _label in engines]
    assert codes == ["daily_body", "supertrend"]
    assert "custom" not in codes
    assert "sar_flip" not in codes
    assert len(engines_for_cross({})) == len(listed_cross_engines())

    # 交叉测试本体只跑这两套，用来核对合约名单，不把补全后的整份名单再测一遍。
    engines = [("daily_body", "日内实体"), ("supertrend", "SuperTrend趋势")]
    result = screen_cross(
        {"strategy": {}, "trade": {}, "risk": {}},
        {"fixed_volume": 1},
        universe=contracts,
        engines=engines,
        runner=runner,
    )
    assert seen == [
        ("rb2701", "daily_body"),
        ("rb2701", "supertrend"),
        ("SA2701", "daily_body"),
        ("SA2701", "supertrend"),
        ("wr2601", "daily_body"),
        ("wr2601", "supertrend"),
    ]
    assert {row["timeframe"] for row in result["rows"]} == {"5"}
    assert "rb0" not in {row["symbol"] for row in result["rows"]}
    by_symbol = {row["symbol"]: row for row in result["rows"]}
    assert by_symbol["wr2601"]["liquidity"] == "偏少"
    note = reference_universe_text()
    assert "rb0" in note
    assert "不跑这一份" in note
    assert "rb2701" not in note


def test_cross_matches_strategy_symbol_and_period() -> None:
    """勾选多档周期时，策略×合约×周期都跑；信号和止损是同一个周期。"""

    class Perf:
        trades = 0
        win_rate = 0.0
        profit_factor = 0.0
        max_drawdown = 0.0
        net_pnl = 0.0
        score = 0.0

    class Result:
        performance = Perf()
        params_snapshot = {}
        bars = 0

    seen: list[tuple[str, str, str, str]] = []

    def runner(cfg, *, symbol, volume, start_date, end_date, engine):
        st = cfg.get("strategy") or {}
        seen.append((symbol, engine, str(st.get("timeframe")), str(st.get("trail_timeframe"))))
        return Result()

    result = screen_cross(
        {"strategy": {}, "trade": {}, "risk": {}},
        {"fixed_volume": 1},
        universe=contracts_for_cross(["rb2701", "SA2701"]),
        engines=[("daily_body", "日内实体"), ("swing_box", "长线箱体突破")],
        periods=["15", "60"],
        runner=runner,
    )
    assert seen == [
        ("rb2701", "daily_body", "15", "15"),
        ("rb2701", "swing_box", "15", "15"),
        ("SA2701", "daily_body", "15", "15"),
        ("SA2701", "swing_box", "15", "15"),
        ("rb2701", "daily_body", "60", "60"),
        ("rb2701", "swing_box", "60", "60"),
        ("SA2701", "daily_body", "60", "60"),
        ("SA2701", "swing_box", "60", "60"),
    ]
    assert len(result["rows"]) == 8
    shown = list_rows(result)
    assert {row["period"] for row in shown} == {"15分钟", "60分钟"}


def test_cross_list_rows_show_payoff() -> None:
    """交叉测试结果表写出盈亏比。没有亏损笔时显示横杠，不是盈利因子。"""
    row = {
        "symbol": "rb0",
        "product": "螺纹",
        "strategy": "日内实体",
        "trades": 8,
        "win_rate": 0.5,
        "profit_factor": 6.0,
        "payoff": 3.0,
        "max_drawdown": 100,
        "net_pnl": 500,
        "picked": "较好",
        "rank": 1,
        "liquidity": "正常",
        "frequency": "8笔",
    }
    shown = list_rows({"mode": "cross", "rows": [row], "settings": {"timeframe": "5"}})
    assert shown[0]["payoff"] == "3.00"
    assert shown[0]["profit_factor"] == "6.00"
    none = dict(row)
    none["payoff"] = 0
    blank = list_rows({"mode": "cross", "rows": [none], "settings": {"timeframe": "5"}})
    assert blank[0]["payoff"] == "—"


def test_cross_list_rows_show_expectancy() -> None:
    """交叉测试写出无单位收益期望：胜率×盈亏比 − 败率。没有亏损笔时横杠。"""
    row = {
        "symbol": "rb0",
        "product": "螺纹",
        "strategy": "日内实体",
        "trades": 8,
        "win_rate": 0.5,
        "profit_factor": 6.0,
        "payoff": 3.0,
        "max_drawdown": 100,
        "net_pnl": 500,
        "picked": "较好",
        "rank": 1,
        "liquidity": "正常",
        "frequency": "8笔",
    }
    shown = list_rows({"mode": "cross", "rows": [row], "settings": {"timeframe": "5"}})
    # 胜率 0.5、盈亏比 3 → 0.5×3 − 0.5 = 1；每笔收益仍是盈利覆盖÷笔数。
    assert shown[0]["expectancy"] == "1.00"
    assert shown[0]["pnl_per_trade"] == "50.00元"
    empty = dict(row)
    empty["trades"] = 0
    empty["net_pnl"] = 0
    empty["payoff"] = 0
    blank = list_rows({"mode": "cross", "rows": [empty], "settings": {"timeframe": "5"}})
    assert blank[0]["expectancy"] == "—"


def test_rows_keep_raw_code_and_timeframe_for_chart() -> None:
    """双击看K线要用原始代码。显示列里已拼成「螺纹 rb0」「5分钟」，行里另留 code / timeframe。"""
    row = {
        "symbol": "rb0",
        "product": "螺纹",
        "strategy": "日内实体",
        "timeframe": "15",
        "trades": 8,
        "win_rate": 0.5,
        "profit_factor": 1.2,
        "max_drawdown": 100,
        "net_pnl": 500,
        "picked": "较好",
        "liquidity": "正常",
    }
    shown = list_rows({"mode": "cross", "rows": [row], "settings": {"timeframe": "5"}})
    assert shown[0]["symbol"] == "螺纹 rb0"
    assert shown[0]["code"] == "rb0"
    assert shown[0]["timeframe"] == "15"

    base = list_rows(
        {
            "mode": "baseline",
            "baseline": {"symbol": "bu0", "timeframe": "5", "trades": 3},
            "rows": [],
            "settings": {"timeframe": "5"},
        }
    )[0]
    assert base["symbol"] == "bu0"
    assert base["code"] == "bu0"
    assert base["timeframe"] == "5"
