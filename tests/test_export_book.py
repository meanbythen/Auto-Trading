"""统一 Excel：不同功能写不同表，后写的功能不能把先写的表冲掉。"""

from __future__ import annotations

import pandas as pd

from src.account.signal_book import append_trade, new_book, save_book
from src.backtest.param_excel import export_append_excel
from src.export.book import (
    SHEET_CHANGES,
    SHEET_CROSS,
    SHEET_FILLS,
    SHEET_INDEX,
    SHEET_OPTIMIZE,
    SHEET_PARAMS,
    SHEET_REVIEWS,
)
from src.export.bundle import export_all, rebuild_workbook


def _sample_book(root) -> None:
    """写一本当前账户分析账，供导出测试使用。"""
    book = new_book(1000, version="test", started_at="2026-10-01 09:00:00")
    append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=1,
        multiplier=10,
        reason="测试开仓",
        when="2026-10-01 09:30:00",
    )
    save_book(root, book)


def test_param_sheet_and_fill_sheet_share_one_file(tmp_path) -> None:
    _sample_book(tmp_path)
    path = tmp_path / "Auto-Trading导出.xlsx"
    export_all(tmp_path, path)
    export_append_excel(tmp_path, path)

    book = pd.ExcelFile(path, engine="openpyxl")
    assert SHEET_FILLS in book.sheet_names
    assert SHEET_CHANGES in book.sheet_names
    assert SHEET_PARAMS in book.sheet_names
    assert SHEET_INDEX in book.sheet_names
    fills = pd.read_excel(book, sheet_name=SHEET_FILLS)
    assert list(fills["品种"]) == ["bu2612"]
    assert list(fills["开平"]) == ["开仓"]
    changes = pd.read_excel(book, sheet_name=SHEET_CHANGES)
    assert "初始资金" in set(changes["方式"].astype(str))
    params = pd.read_excel(book, sheet_name=SHEET_PARAMS)
    assert "factory_default" in set(params["版本ID"].astype(str))

    # 再导出一次参数，成交表还在，版本不重复
    _path, added, total = export_append_excel(tmp_path, path)
    assert added == 0
    assert total == len(params)
    fills_again = pd.read_excel(path, sheet_name=SHEET_FILLS, engine="openpyxl")
    assert list(fills_again["品种"]) == ["bu2612"]


def test_export_backtest_keeps_other_sheets(tmp_path) -> None:
    """回测导出只更新两张回测表，已经写过的账户成交还在。"""
    import json

    from src.export.book import SHEET_BACKTEST, SHEET_BACKTEST_TRADES
    from src.export.bundle import export_backtest

    path = tmp_path / "Auto-Trading导出.xlsx"
    _sample_book(tmp_path)
    export_all(tmp_path, path)
    folder = tmp_path / "data"
    folder.mkdir(exist_ok=True)
    (folder / "backtest_last.json").write_text(
        json.dumps(
            {
                "symbol": "bu2612",
                "timeframe": "15",
                "trail_timeframe": "30",
                "bars": 100,
                "ran_at": "2026-10-19 14:00:00",
                "performance": {"trades": 1, "win_rate": 1, "net_pnl": 200, "profit_factor": 2, "max_drawdown": 10},
                "params_snapshot": {
                    "engine": "daily_body",
                    "data_from": "2026-09-01",
                    "data_to": "2026-10-01",
                },
                "trades": [
                    {
                        "symbol": "bu2612",
                        "side": "long",
                        "entry_time": "2026-10-01 09:30",
                        "exit_time": "2026-10-01 10:30",
                        "entry_price": 3500,
                        "exit_price": 3520,
                        "volume": 1,
                        "pnl": 200,
                        "reason_entry": "测试开仓",
                        "reason_exit": "测试平仓",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    export_backtest(tmp_path, path)
    book = pd.ExcelFile(path, engine="openpyxl")
    assert SHEET_BACKTEST in book.sheet_names
    assert SHEET_BACKTEST_TRADES in book.sheet_names
    assert SHEET_FILLS in book.sheet_names
    summary = pd.read_excel(book, sheet_name=SHEET_BACKTEST)
    trades = pd.read_excel(book, sheet_name=SHEET_BACKTEST_TRADES)
    assert list(summary.columns)[0] == "回测时间"
    assert str(summary.iloc[0]["回测时间"]).startswith("2026-10-19 14:00")
    assert str(trades.iloc[0]["回测时间"]).startswith("2026-10-19 14:00")
    assert summary.iloc[0]["净盈亏"] == 200
    assert float(summary.iloc[0]["盈利覆盖"]) == 190
    assert float(summary.iloc[0]["每笔收益"]) == 190
    assert int(summary.iloc[0]["天数"]) == 30
    assert "日内实体" in str(summary.iloc[0]["策略"])
    assert "夜盘分钟" in list(summary.columns)
    assert "测试开仓" in str(trades.iloc[0]["开仓原因"])
    assert trades.iloc[0]["方向"] == "多"
    fills = pd.read_excel(path, sheet_name=SHEET_FILLS, engine="openpyxl")
    assert list(fills["品种"]) == ["bu2612"]


def test_export_backtest_appends_instead_of_replacing(tmp_path) -> None:
    """第二次回测导出接在第一次后面，第一次的成交还在；再导同一份不会重复。"""
    import json

    from src.export.book import SHEET_BACKTEST_TRADES
    from src.export.bundle import export_backtest

    folder = tmp_path / "data"
    folder.mkdir()
    report = folder / "backtest_last.json"
    path = tmp_path / "Auto-Trading导出.xlsx"

    def _write(entry: str, exit_: str, pnl: int, ran_at: str) -> None:
        report.write_text(
            json.dumps(
                {
                    "symbol": "bu2612",
                    "timeframe": "5",
                    "trail_timeframe": "5",
                    "bars": 80,
                    "ran_at": ran_at,
                    "performance": {"trades": 1, "win_rate": 1, "net_pnl": pnl, "profit_factor": 2, "max_drawdown": 10},
                    "params_snapshot": {"engine": "daily_body"},
                    "trades": [
                        {
                            "symbol": "bu2612",
                            "side": "short",
                            "entry_time": entry,
                            "exit_time": exit_,
                            "entry_price": 4500,
                            "exit_price": 4400,
                            "volume": 2,
                            "pnl": pnl,
                            "reason_entry": "测试开仓",
                            "reason_exit": "测试平仓",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    _write("2026-09-18 10:15", "2026-09-18 14:55", 200, "2026-10-19 14:00:00")
    _path, summary_added, trade_added, _summary_total, trade_total = export_backtest(tmp_path, path)
    assert summary_added == 1 and trade_added == 1 and trade_total == 1

    _write("2026-09-21 09:05", "2026-09-21 14:55", 240, "2026-10-19 15:00:00")
    _path, summary_added, trade_added, summary_total, trade_total = export_backtest(tmp_path, path)
    assert summary_added == 1 and trade_added == 1
    assert summary_total == 2 and trade_total == 2
    trades = pd.read_excel(path, sheet_name=SHEET_BACKTEST_TRADES, engine="openpyxl")
    assert list(trades["开仓时间"].astype(str)) == ["2026-09-18 10:15", "2026-09-21 09:05"]
    assert list(trades["回测时间"].astype(str)) == ["2026-10-19 14:00:00", "2026-10-19 15:00:00"]

    _path, summary_added, trade_added, summary_total, trade_total = export_backtest(tmp_path, path)
    assert summary_added == 0 and trade_added == 0
    assert summary_total == 2 and trade_total == 2


def test_same_trade_from_two_backtests_stays_two_rows(tmp_path) -> None:
    """成交内容一样，但回测时间不同，必须留成两行，才能分清是哪一次。"""
    import json

    from src.export.book import SHEET_BACKTEST_TRADES
    from src.export.bundle import export_backtest

    folder = tmp_path / "data"
    folder.mkdir()
    report = folder / "backtest_last.json"
    path = tmp_path / "Auto-Trading导出.xlsx"

    def _write(ran_at: str) -> None:
        report.write_text(
            json.dumps(
                {
                    "symbol": "bu2612",
                    "timeframe": "5",
                    "trail_timeframe": "15",
                    "bars": 10,
                    "ran_at": ran_at,
                    "performance": {"trades": 1, "win_rate": 1, "net_pnl": 100, "profit_factor": 2, "max_drawdown": 0},
                    "params_snapshot": {"engine": "daily_body"},
                    "trades": [
                        {
                            "symbol": "bu2612",
                            "side": "short",
                            "entry_time": "2026-10-19 10:00",
                            "exit_time": "2026-10-19 14:55",
                            "entry_price": 4629,
                            "exit_price": 4520,
                            "volume": 2,
                            "pnl": 2180,
                            "reason_entry": "60分钟",
                            "reason_exit": "回测结束强平",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    _write("2026-10-19 13:55:00")
    export_backtest(tmp_path, path)
    _write("2026-10-19 14:55:00")
    _path, _summary_added, trade_added, _summary_total, trade_total = export_backtest(tmp_path, path)
    assert trade_added == 1 and trade_total == 2
    trades = pd.read_excel(path, sheet_name=SHEET_BACKTEST_TRADES, engine="openpyxl")
    assert list(trades["回测时间"].astype(str)) == ["2026-10-19 13:55:00", "2026-10-19 14:55:00"]


def test_old_export_without_timestamp_is_filled_not_duplicated(tmp_path) -> None:
    """以前的表没有回测时间。再导出同一笔时补上时间，不把这一笔再抄一遍。"""
    import json

    from src.export.book import SHEET_BACKTEST_TRADES, write_sheet
    from src.export.bundle import export_backtest

    path = tmp_path / "Auto-Trading导出.xlsx"
    write_sheet(
        path,
        SHEET_BACKTEST_TRADES,
        pd.DataFrame(
            [
                {
                    "品种": "bu2612",
                    "方向": "多",
                    "开仓时间": "2026-10-01 09:30",
                    "平仓时间": "2026-10-01 10:30",
                    "开仓价": 3500,
                    "平仓价": 3520,
                    "手数": 1,
                    "盈亏": 200,
                    "开仓原因": "测试开仓",
                    "平仓原因": "测试平仓",
                }
            ]
        ),
    )
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "backtest_last.json").write_text(
        json.dumps(
            {
                "symbol": "bu2612",
                "timeframe": "15",
                "trail_timeframe": "30",
                "bars": 100,
                "ran_at": "2026-10-19 16:00:00",
                "performance": {"trades": 1, "win_rate": 1, "net_pnl": 200, "profit_factor": 2, "max_drawdown": 10},
                "params_snapshot": {"engine": "daily_body"},
                "trades": [
                    {
                        "symbol": "bu2612",
                        "side": "long",
                        "entry_time": "2026-10-01 09:30",
                        "exit_time": "2026-10-01 10:30",
                        "entry_price": 3500,
                        "exit_price": 3520,
                        "volume": 1,
                        "pnl": 200,
                        "reason_entry": "测试开仓",
                        "reason_exit": "测试平仓",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _path, _summary_added, trade_added, _summary_total, trade_total = export_backtest(tmp_path, path)
    assert trade_added == 0 and trade_total == 1
    trades = pd.read_excel(path, sheet_name=SHEET_BACKTEST_TRADES, engine="openpyxl")
    assert list(trades["回测时间"].astype(str)) == ["2026-10-19 16:00:00"]


def test_cell_text_treats_point_eight_as_same_number() -> None:
    """得分 0.80 和 Excel 读回的 0.8 必须当成同一格，否则同一轮再导出会重复。"""
    from src.export.book import _cell_text

    assert _cell_text("0.80") == _cell_text(0.8)
    assert _cell_text("2.00") == _cell_text(2.0) == "2"


def test_append_drops_old_rows_when_schema_upgraded() -> None:
    """表头缺了新列时，旧行丢掉，只留按新列写的内容。"""
    from src.export.book import append_frames

    old = pd.DataFrame([{"品种": "旧行", "净盈亏": 1}])
    fresh = pd.DataFrame([{"品种": "新行", "净盈亏": 2, "盈利覆盖": 3}])
    columns = ["品种", "净盈亏", "盈利覆盖"]
    merged, added = append_frames(old, fresh, columns)
    assert added == 1
    assert list(merged.columns) == columns
    assert list(merged["品种"]) == ["新行"]
    assert float(merged.iloc[0]["盈利覆盖"]) == 3


def test_export_all_rebuilds_when_workbook_schema_outdated(tmp_path) -> None:
    """旧导出缺盈利覆盖等列时，导出全部会整本按新表重建。"""
    import json

    from src.export.book import SHEET_BACKTEST, SHEET_CROSS
    from src.export.bundle import BACKTEST_SUMMARY_COLUMNS, CROSS_COLUMNS, export_all

    data = tmp_path / "data"
    data.mkdir()
    path = data / "Auto-Trading导出.xlsx"
    # 故意写成旧表头，缺盈利覆盖等新列。
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([{"测试时间": "2026-01-01", "品种": "旧交叉", "净盈亏": 1}]).to_excel(
            writer, sheet_name=SHEET_CROSS, index=False
        )
        pd.DataFrame([{"回测时间": "2026-01-01", "品种": "旧回测", "净盈亏": 2}]).to_excel(
            writer, sheet_name=SHEET_BACKTEST, index=False
        )
    (data / "backtest_last.json").write_text(
        json.dumps(
            {
                "symbol": "bu2612",
                "timeframe": "5",
                "trail_timeframe": "5",
                "bars": 10,
                "ran_at": "2026-10-28 00:00:00",
                "performance": {
                    "trades": 1,
                    "win_rate": 1,
                    "net_pnl": 100,
                    "profit_factor": 2,
                    "max_drawdown": 20,
                },
                "params_snapshot": {
                    "engine": "daily_body",
                    "data_from": "2026-09-01",
                    "data_to": "2026-10-01",
                },
                "trades": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out, counts = export_all(tmp_path, path)
    cross = pd.read_excel(out, sheet_name=SHEET_CROSS, engine="openpyxl")
    summary = pd.read_excel(out, sheet_name=SHEET_BACKTEST, engine="openpyxl")
    assert list(cross.columns) == CROSS_COLUMNS
    assert list(summary.columns) == BACKTEST_SUMMARY_COLUMNS
    assert "盈利覆盖" in list(summary.columns)
    assert float(summary.iloc[0]["盈利覆盖"]) == 80
    assert "旧交叉" not in set(cross["品种"].astype(str))
    assert counts[SHEET_BACKTEST] == 1


def test_export_cross_appends_symbol_and_strategy(tmp_path) -> None:
    """交叉测试导出的是品种和策略的组合，同一轮再导出不重复。"""
    from src.export.book import SHEET_CROSS
    from src.export.bundle import export_cross

    data = tmp_path / "data"
    data.mkdir()
    payload = {
        "mode": "cross",
        "ran_at": "2026-10-24 00:20:00",
        "volume": 1,
        "settings": {
            "fixed_volume": 1,
            "risk_per_trade_pct": 20,
            "min_order_interval_sec": 60,
            "max_orders_per_day": 5,
            "timeframe": "5m",
            "start_date": "2026-01-01",
            "end_date": "",
        },
        "rows": [
            {
                "symbol": "rb0",
                "product": "螺纹",
                "strategy": "日内实体",
                "trades": 8,
                "win_rate": 0.5,
                "profit_factor": 2.0,
                "max_drawdown": 100,
                "net_pnl": 500,
                "score": 0.8,
                "picked": "较好",
                "rank": 1,
                "liquidity": "正常",
                "frequency": "8笔",
                "data_from": "2026-01-01",
                "data_to": "2026-10-01",
                "bars": 100,
            }
        ],
    }
    (data / "symbol_picks.json").write_text(
        __import__("json").dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    path = data / "Auto-Trading导出.xlsx"
    out, added, total = export_cross(tmp_path, path)
    frame = pd.read_excel(out, sheet_name=SHEET_CROSS, engine="openpyxl")
    assert added == 1
    assert total == 1
    assert frame.iloc[0]["品种"] == "螺纹 rb0"
    assert frame.iloc[0]["策略"] == "日内实体"
    assert frame.iloc[0]["结果"] == "较好"
    assert int(frame.iloc[0]["持仓手数"]) == 1
    assert "止损周期" in list(frame.columns)
    assert "行情来源" in list(frame.columns)
    # 盈利覆盖 = 净盈亏 − 最大回撤；每笔/日均用覆盖去除，天数用结束减开始。
    assert float(frame.iloc[0]["盈利覆盖"]) == 400
    assert int(frame.iloc[0]["笔数"]) == 8
    assert abs(float(frame.iloc[0]["每笔收益"]) - 50.0) < 1e-6
    assert str(frame.iloc[0]["开始时间"])[:10] == "2026-01-01"
    assert str(frame.iloc[0]["结束时间"])[:10] == "2026-10-01"
    assert int(frame.iloc[0]["天数"]) == 273
    assert float(frame.iloc[0]["日均"]) == round(400 / 273, 2)
    again, added_again, total_again = export_cross(tmp_path, path)
    assert added_again == 0
    assert total_again == 1
    book = pd.ExcelFile(again, engine="openpyxl")
    assert SHEET_CROSS in book.sheet_names


def test_export_all_includes_review_text(tmp_path) -> None:
    reviews = tmp_path / "data" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "2026-10-14_bu2612.txt").write_text("今天震荡。", encoding="utf-8")
    path, counts = export_all(tmp_path, tmp_path / "data" / "Auto-Trading导出.xlsx")
    frame = pd.read_excel(path, sheet_name=SHEET_REVIEWS, engine="openpyxl")
    assert "今天震荡。" in str(frame["报告"].iloc[0])
    assert counts[SHEET_REVIEWS] == 1
    assert SHEET_PARAMS in counts
    # 说明页必须列出交叉测试；工作簿里九张表（含说明）都在。
    index = pd.read_excel(path, sheet_name=SHEET_INDEX, engine="openpyxl")
    assert SHEET_CROSS in set(index["工作表"].astype(str))
    assert SHEET_OPTIMIZE in set(index["工作表"].astype(str))


def test_rebuild_workbook_uses_current_schema(tmp_path) -> None:
    """重建会丢掉旧文件，按现在的表头重写；优化结果带检验字段。"""
    import json

    data = tmp_path / "data"
    data.mkdir()
    path = data / "Auto-Trading导出.xlsx"
    # 先写一本旧表头文件，确认重建后会被替换。
    pd.DataFrame([{"旧列": 1}]).to_excel(path, sheet_name="旧表", index=False, engine="openpyxl")
    (data / "optimize_last.json").write_text(
        json.dumps(
            {
                "symbol": "bu2612",
                "engine": "daily_body",
                "start_date": "2026-01-01",
                "end_date": "2026-06-01",
                "mode": "一次切断",
                "holdout_pct": 30,
                "depth": "标准",
                "accepted": True,
                "accept_reason": "可以写入",
                "best_score": 1.2,
                "best_params": {"eth_minutes": 20},
                "train_end": "2026-04-01",
                "holdout_start": "2026-04-02",
                "best_performance": {
                    "trades": 5,
                    "win_rate": 0.6,
                    "net_pnl": 100,
                    "profit_factor": 2,
                    "max_drawdown": 20,
                },
                "train_performance": {"trades": 5, "net_pnl": 100, "max_drawdown": 20},
                "holdout_performance": {"trades": 2, "net_pnl": 40, "max_drawdown": 10},
                "walk_efficiency": 0.4,
                "oos_win_rate": 0.5,
                "trials": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out, counts = rebuild_workbook(tmp_path, path)
    book = pd.ExcelFile(out, engine="openpyxl")
    assert "旧表" not in book.sheet_names
    assert SHEET_CROSS in book.sheet_names
    assert SHEET_OPTIMIZE in book.sheet_names
    opt = pd.read_excel(out, sheet_name=SHEET_OPTIMIZE, engine="openpyxl")
    assert "是否通过" in list(opt.columns)
    assert "检验效率" in list(opt.columns)
    assert "盈利覆盖" in list(opt.columns)
    assert "训练盈利覆盖" in list(opt.columns)
    assert float(opt.iloc[0]["盈利覆盖"]) == 80
    assert float(opt.iloc[0]["训练盈利覆盖"]) == 80
    assert float(opt.iloc[0]["检验盈利覆盖"]) == 30
    assert opt.iloc[0]["是否通过"] == "通过"
    assert "日内实体" in str(opt.iloc[0]["策略"])
    assert counts[SHEET_OPTIMIZE] == 1
