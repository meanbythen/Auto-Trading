"""主力合约换月回测：读表、裁时段、分段汇总。不依赖外网。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from src.backtest import rollover
from src.backtest.engine import BacktestResult
from src.backtest.metrics import TradeRecord, calc_performance


def _write_table(path, rows) -> None:
    """按真实表格的样子写：前两行标题/说明，第三行才是表头。"""
    frame = pd.DataFrame(
        rows,
        columns=["品种", "中文名", "交易所", "主力合约", "交割月份", "开始日期", "结束日期", "交易日数", "平均持仓量（手）"],
    )
    frame.to_excel(path, sheet_name="主力时段", index=False, startrow=2)


def _add_manual_sheet(path, rows) -> None:
    """在已有表里补一页「手动修正」，格式和界面写出来的一样。"""
    frame = pd.DataFrame(rows, columns=["品种", "主力合约", "开始日期", "结束日期", "备注"])
    with pd.ExcelWriter(path, mode="a") as writer:
        frame.to_excel(writer, sheet_name="手动修正", index=False, startrow=2)


def _periods() -> list[list[object]]:
    return [
        ["rb", "螺纹", "SHFE", "rb2605", "2026-03", "2026-01-05", "2026-04-01", 59, 500000],
        ["rb", "螺纹", "SHFE", "rb2610", "2026-10", "2026-04-02", "2026-08-27", 100, 480000],
        ["rb", "螺纹", "SHFE", "rb2701", "2027-01", "2026-08-28", "2026-10-30", 44, 520000],
    ]


def test_resolve_main_table_prefers_config(tmp_path) -> None:
    (tmp_path / "config").mkdir()
    table = tmp_path / "config" / "主力合约表.xlsx"
    table.write_bytes(b"placeholder")

    found = rollover.resolve_main_table({"_project_root": str(tmp_path)})

    assert found == table


def test_load_main_periods_reads_title_rows_and_dates(tmp_path) -> None:
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())

    table = rollover.load_main_periods(path)

    assert list(table) == ["rb"]
    first = table["rb"][0]
    assert first.symbol == "rb2605"
    assert isinstance(first.start, date) and isinstance(first.end, date)
    assert first.start == date(2026, 1, 5)
    assert first.end == date(2026, 4, 1)
    assert [p.symbol for p in table["rb"]] == ["rb2605", "rb2610", "rb2701"]


def test_load_main_periods_matches_product_case(tmp_path) -> None:
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, [["TA", "PTA", "CZCE", "TA2605", "2026-05", "2026-01-05", "2026-04-10", 63, 1]])

    table = rollover.load_main_periods(path)

    assert "ta" in table
    assert table["ta"][0].symbol == "TA2605"


def test_load_main_periods_manual_overrides_auto(tmp_path) -> None:
    """「手动修正」页写的段覆盖同区间的自动段，自动段只保留没被盖住的部分。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    _add_manual_sheet(path, [["rb", "rb2610", "2026-03-01", "2026-05-31", "自己改的"]])

    table = rollover.load_main_periods(path)

    got = [(p.symbol, p.start, p.end, p.manual) for p in table["rb"]]
    assert got == [
        ("rb2605", date(2026, 1, 5), date(2026, 2, 28), False),
        ("rb2610", date(2026, 3, 1), date(2026, 5, 31), True),
        ("rb2610", date(2026, 6, 1), date(2026, 8, 27), False),
        ("rb2701", date(2026, 8, 28), date(2026, 10, 30), False),
    ]


def test_rollover_day_stays_on_old_contract() -> None:
    """换月日当天仍按旧合约，下一交易日起才切到新合约。"""
    periods = [
        rollover.MainPeriod("rb", "rb2605", date(2026, 1, 5), date(2026, 4, 1)),
        rollover.MainPeriod("rb", "rb2610", date(2026, 4, 2), date(2026, 8, 27)),
        rollover.MainPeriod("rb", "rb2701", date(2026, 8, 28), date(2026, 10, 30)),
    ]

    got = rollover.keep_old_contract_on_rollover_day(periods)

    assert [(p.symbol, p.start, p.end) for p in got] == [
        ("rb2605", date(2026, 1, 5), date(2026, 4, 2)),
        ("rb2610", date(2026, 4, 3), date(2026, 8, 28)),
        ("rb2701", date(2026, 8, 29), date(2026, 10, 30)),
    ]


def test_rollover_day_handles_weekend_gap() -> None:
    """旧段周五结束、新段周一开始：中间隔着周末，仍算同一个换月边界。"""
    periods = [
        rollover.MainPeriod("fu", "fu2611", date(2026, 8, 6), date(2026, 10, 9)),
        rollover.MainPeriod("fu", "fu2701", date(2026, 10, 12), date(2026, 10, 30)),
    ]

    got = rollover.keep_old_contract_on_rollover_day(periods)

    assert [(p.symbol, p.start, p.end) for p in got] == [
        ("fu2611", date(2026, 8, 6), date(2026, 10, 12)),
        ("fu2701", date(2026, 10, 13), date(2026, 10, 30)),
    ]


def test_clip_periods_cuts_to_requested_range() -> None:
    periods = [
        rollover.MainPeriod("rb", "rb2605", date(2026, 1, 5), date(2026, 4, 1)),
        rollover.MainPeriod("rb", "rb2610", date(2026, 4, 2), date(2026, 8, 27)),
    ]

    cut = rollover.clip_periods(periods, "2026-02-01", "2026-08-01")

    assert [(p.symbol, s, e) for p, s, e in cut] == [
        ("rb2605", "2026-02-01", "2026-04-01"),
        ("rb2610", "2026-04-02", "2026-08-01"),
    ]
    # 区间完全落在表外时不再分段
    assert rollover.clip_periods(periods, "2025-01-01", "2025-12-31") == []


def _fake_backtest(symbol: str, *, initial_capital: float, **kwargs) -> BacktestResult:
    trade = TradeRecord(
        symbol=symbol,
        side="long",
        entry_time="2026-01-05 09:00:00",
        exit_time="2026-01-05 10:00:00",
        entry_price=100.0,
        exit_price=101.0,
        volume=1,
        pnl=100.0,
    )
    equity = [float(initial_capital), float(initial_capital) + 100.0]
    perf = calc_performance([trade], equity, min_trades=1)
    return BacktestResult(
        symbol=symbol,
        timeframe="15",
        trail_timeframe="15",
        bars=5,
        performance=perf,
        trades=[trade],
        equity=equity,
        params_snapshot={
            "start_date": kwargs.get("start_date") or "",
            "end_date": kwargs.get("end_date") or "",
            "data_from": "2026-01-05 09:00:00",
            "data_to": "2026-01-05 10:00:00",
            "available_from": "2026-01-05 09:00:00",
            "available_to": "2026-01-05 10:00:00",
            "skipped_opens": {"止损过宽，手数不够": 1},
        },
    )


def test_rollover_fixed_capital_and_merges_trades(tmp_path, monkeypatch) -> None:
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    cfg = {"risk": {"capital": 1000, "contracts": {"rb": {"multiplier": 10}}}}
    calls: list[float] = []

    def fake(cfg, symbol: str, *, initial_capital: float, **kwargs) -> BacktestResult:
        calls.append(float(initial_capital))
        return _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        cfg,
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
    )

    assert result.symbol == "rb主力连续"
    assert len(result.trades) == 3
    assert [t.symbol for t in result.trades] == ["rb2605", "rb2610", "rb2701"]
    # 每段固定用同一份起始资金，段内手数才能直接和单合约回测对照
    assert calls == [1000.0, 1000.0, 1000.0]
    # 净值曲线把各段盈亏累计平移：总盈亏 = 各段之和，衔接处不重复记一笔
    assert result.equity == [1000.0, 1100.0, 1200.0, 1300.0]
    assert result.params_snapshot["rollover_chain"] is False
    assert result.experiment["kind"] == "backtest_rollover"
    assert len(result.experiment["components"]) == 3
    assert len(result.experiment["details"]["main_contract_table"]["sha256"]) == 64


def test_rollover_chain_capital_links_equity(tmp_path, monkeypatch) -> None:
    """真实账户口径：上一段结束的权益接到下一段，段内手数随连续盈亏变化。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    cfg = {"risk": {"capital": 1000, "contracts": {"rb": {"multiplier": 10}}}}
    calls: list[float] = []

    def fake(cfg, symbol: str, *, initial_capital: float, **kwargs) -> BacktestResult:
        calls.append(float(initial_capital))
        return _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        cfg,
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
        chain_capital=True,
    )

    assert calls == [1000.0, 1100.0, 1200.0]
    assert result.equity == [1000.0, 1100.0, 1200.0, 1300.0]
    assert result.params_snapshot["rollover_chain"] is True
    assert "资金连续" in rollover.format_rollover_lines(result.params_snapshot)
    assert result.bars == 15
    snap = result.params_snapshot
    assert snap["rollover"] is True
    assert [row["symbol"] for row in snap["rollover_segments"]] == ["rb2605", "rb2610", "rb2701"]
    assert snap["skipped_opens"] == {"止损过宽，手数不够": 3}


def test_rollover_marks_manual_segments(tmp_path, monkeypatch) -> None:
    """手动修正的段照样参与回测，并在结果里标出来。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    _add_manual_sheet(path, [["rb", "rb2609", "2026-03-01", "2026-05-31", ""]])

    def fake(cfg, symbol: str, *, initial_capital: float, **kwargs) -> BacktestResult:
        return _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
    )

    segs = result.params_snapshot["rollover_segments"]
    assert [row["symbol"] for row in segs] == ["rb2605", "rb2609", "rb2610", "rb2701"]
    assert [row["manual"] for row in segs] == [False, True, False, False]
    assert result.params_snapshot["rollover_manual"] == 1
    assert "手动修正" in result.params_snapshot["rollover_note"]
    assert "（手动）" in rollover.format_rollover_lines(result.params_snapshot)


def test_rollover_extends_last_segment_to_requested_end(tmp_path, monkeypatch) -> None:
    """主力表通常只到上一个交易日：结束日之后的几天要按最后的主力顺延，不能漏测。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    seen: list[tuple[str, str, str]] = []

    def fake(
        cfg, symbol: str, *, start_date=None, end_date=None, initial_capital=0.0, **kwargs
    ) -> BacktestResult:
        seen.append((symbol, str(start_date), str(end_date)))
        return _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-11-03",
        table_path=path,
    )

    # 换月日归旧合约：rb2605 多做 4-02，rb2610 从 4-03 起；最后一段只到 10-30，
    # 结束日 11-03 必须顺延到这一段里。
    assert seen[0] == ("rb2605", "2026-01-05", "2026-04-02")
    assert seen[1] == ("rb2610", "2026-04-03", "2026-08-28")
    assert seen[-1] == ("rb2701", "2026-08-29", "2026-11-03")
    assert result.params_snapshot["rollover_segments"][-1]["end"] == "2026-11-03"
    assert "顺延" in result.params_snapshot["rollover_note"]


def test_rollover_empty_end_extends_to_today(tmp_path, monkeypatch) -> None:
    """结束日空着=到最新。主力表只到 10-30 时，仍要按最后主力顺延到今天。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    seen: list[tuple[str, str, str]] = []

    def fake(
        cfg, symbol: str, *, start_date=None, end_date=None, initial_capital=0.0, **kwargs
    ) -> BacktestResult:
        seen.append((symbol, str(start_date), str(end_date)))
        return _fake_backtest(
            symbol, initial_capital=initial_capital, start_date=start_date, end_date=end_date
        )

    monkeypatch.setattr(rollover, "run_backtest", fake)
    monkeypatch.setattr(rollover, "_today", lambda: date(2026, 11, 14))

    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date=None,
        table_path=path,
    )

    assert seen[-1] == ("rb2701", "2026-08-29", "2026-11-14")
    snap = result.params_snapshot
    assert snap["start_date"] == "2026-01-05"
    assert snap["end_date"] in ("", None)
    assert "结束日空着" in snap["rollover_note"]
    assert snap["rollover_segments"][-1]["end"] == "2026-11-14"


def test_rollover_snapshot_keeps_user_range_not_last_segment(tmp_path, monkeypatch) -> None:
    """结果里的「你选的区间」必须是整段回测的起止，不能写成最后一段主力自己的日期。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())

    def fake(cfg, symbol: str, *, initial_capital=0.0, **kwargs) -> BacktestResult:
        return _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
    )

    snap = result.params_snapshot
    assert snap["start_date"] == "2026-01-05"
    assert snap["end_date"] == "2026-10-30"


def test_rollover_falls_back_when_product_missing(tmp_path, monkeypatch) -> None:
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    calls: list[str] = []

    def fake(cfg, symbol: str, **kwargs) -> BacktestResult:
        calls.append(symbol)
        return _fake_backtest(symbol, initial_capital=1000.0)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000}}, symbol="bu2612", table_path=path
    )

    assert calls == ["bu2612"]
    assert result.params_snapshot["rollover"] is False
    assert "没有 bu 的时段" in result.params_snapshot["rollover_note"]


def test_rollover_skips_segment_that_has_no_bars(tmp_path, monkeypatch) -> None:
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())

    def fake(cfg, symbol: str, *, initial_capital: float, **kwargs) -> BacktestResult:
        if symbol == "rb2605":
            raise RuntimeError("K线不足，无法回测")
        return _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
    )

    assert [row["symbol"] for row in result.params_snapshot["rollover_segments"]] == ["rb2610", "rb2701"]
    skipped = result.params_snapshot["rollover_skipped"]
    assert skipped[0]["symbol"] == "rb2605"
    assert "1 段没跑成" in result.params_snapshot["rollover_note"]


def test_rollover_falls_back_when_every_segment_fails(tmp_path, monkeypatch) -> None:
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    calls: list[str] = []

    def fake(cfg, symbol: str, **kwargs) -> BacktestResult:
        calls.append(symbol)
        # 前三次是三个主力分段，都拿不到行情；第四次是退回单合约。
        if len(calls) <= 3:
            raise RuntimeError("K线不足，无法回测")
        return _fake_backtest(symbol, initial_capital=1000.0)

    monkeypatch.setattr(rollover, "run_backtest", fake)

    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
    )

    assert calls[-1] == "rb2610"
    assert result.params_snapshot["rollover"] is False
    assert "按原合约" in result.params_snapshot["rollover_note"]


def test_format_rollover_lines_lists_segments() -> None:
    snap = {
        "rollover": True,
        "rollover_segments": [
            {"symbol": "rb2605", "start": "2026-01-05", "end": "2026-04-01", "bars": 10, "trades": 1, "pnl": 100},
            {"symbol": "rb2610", "start": "2026-04-02", "end": "2026-08-27", "bars": 8, "trades": 2, "pnl": -50},
        ],
        "rollover_note": "",
    }

    text = rollover.format_rollover_lines(snap)

    assert "共 2 段" in text
    assert "移到新主力" in text
    assert "rb2605" in text and "rb2610" in text
    assert "+100元" in text and "-50元" in text


def test_carry_from_end_flatten_only() -> None:
    held = TradeRecord(
        symbol="fu2611",
        side="long",
        entry_time="2026-09-30 11:00:00",
        exit_time="2026-10-12 15:00:00",
        entry_price=4400.0,
        exit_price=5100.0,
        volume=1,
        pnl=7000.0,
        reason_exit="回测结束强平",
    )
    stopped = replace_exit(held, "碰到 EMA120 止损")
    assert rollover._carry_from_result(_result_with(held)) == {"side": "long", "volume": 1}
    assert rollover._carry_from_result(_result_with(stopped)) is None
    rolled = replace_exit(held, "fu2701 已到交割月（2027年1月），交割月不再开仓，有仓必须平掉并换到下一手")
    assert rollover._carry_from_result(_result_with(rolled)) == {"side": "long", "volume": 1}


def replace_exit(trade: TradeRecord, reason: str) -> TradeRecord:
    return TradeRecord(
        symbol=trade.symbol,
        side=trade.side,
        entry_time=trade.entry_time,
        exit_time=trade.exit_time,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        volume=trade.volume,
        pnl=trade.pnl,
        reason_exit=reason,
    )


def _result_with(trade: TradeRecord) -> BacktestResult:
    equity = [1000.0, 1000.0 + float(trade.pnl)]
    return BacktestResult(
        symbol=trade.symbol,
        timeframe="30",
        trail_timeframe="30",
        bars=10,
        performance=calc_performance([trade], equity, min_trades=1),
        trades=[trade],
        equity=equity,
    )


def test_rollover_passes_carry_into_next_segment(tmp_path, monkeypatch) -> None:
    """上一段结束强平还持仓时，下一段 run_backtest 要收到同方向移仓。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())
    seen: list[object] = []

    def fake(cfg, symbol: str, *, initial_capital: float, inherit_position=None, **kwargs) -> BacktestResult:
        seen.append(inherit_position)
        if symbol == "rb2605":
            trade = TradeRecord(
                symbol=symbol,
                side="long",
                entry_time="2026-01-05 09:00:00",
                exit_time="2026-04-02 15:00:00",
                entry_price=100.0,
                exit_price=101.0,
                volume=1,
                pnl=100.0,
                reason_exit="回测结束强平",
            )
            equity = [float(initial_capital), float(initial_capital) + 100.0]
            perf = calc_performance([trade], equity, min_trades=1)
            return BacktestResult(
                symbol=symbol,
                timeframe="15",
                trail_timeframe="15",
                bars=5,
                performance=perf,
                trades=[trade],
                equity=equity,
                params_snapshot={"skipped_opens": {}},
            )
        return _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)

    monkeypatch.setattr(rollover, "run_backtest", fake)
    rollover.run_backtest_rollover(
        {"risk": {"capital": 1000, "contracts": {"rb": {"multiplier": 10}}}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
    )
    assert seen[0] is None
    assert seen[1] == {"side": "long", "volume": 1}


def test_rollover_merges_execution_ambiguities(tmp_path, monkeypatch) -> None:
    """换月总报告不能只留下最后一段的歧义数量和影响。"""
    path = tmp_path / "2026年主力合约表.xlsx"
    _write_table(path, _periods())

    def fake(cfg, symbol: str, *, initial_capital: float, **kwargs) -> BacktestResult:
        result = _fake_backtest(symbol, initial_capital=initial_capital, **kwargs)
        result.ambiguities = [{"symbol": symbol, "pnl_impact_vs_optimistic": -5.0}]
        return result

    monkeypatch.setattr(rollover, "run_backtest", fake)
    result = rollover.run_backtest_rollover(
        {"risk": {"capital": 1000, "contracts": {"rb": {"multiplier": 10}}}},
        symbol="rb2610",
        start_date="2026-01-05",
        end_date="2026-10-30",
        table_path=path,
    )

    assert len(result.ambiguities) == 3
    assert result.params_snapshot["ambiguity_count"] == 3
    assert result.params_snapshot["ambiguity_pnl_impact"] == -15.0
    assert result.experiment["details"]["execution_ambiguity"] == {
        "count": 3,
        "pnl_impact_vs_optimistic": -15.0,
    }


def test_format_rollover_lines_explains_fallback() -> None:
    snap = {"rollover": False, "rollover_note": "没找到主力合约表"}
    assert rollover.format_rollover_lines(snap) == "没找到主力合约表"
    assert rollover.format_rollover_lines({}) == ""
