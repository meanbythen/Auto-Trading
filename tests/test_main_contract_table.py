"""主力合约表生成逻辑，不联网。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from src.backtest.main_table import (
    _display_product,
    _to_dates,
    dominant_periods,
    normalize_contract,
    read_manual_rows,
    write_workbook,
)


def test_normalize_contract_czce_three_digit() -> None:
    assert normalize_contract("TA701", date(2026, 10, 8), "CZCE") == ("TA2701", "2027-01")
    assert normalize_contract("SH610", date(2026, 10, 8), "CZCE") == ("SH2610", "2026-10")
    # 配置里本来就是四位
    assert normalize_contract("TA2701", date(2026, 10, 8), "CZCE") == ("TA2701", "2027-01")


def test_normalize_contract_keeps_lowercase_for_shfe_dce() -> None:
    assert normalize_contract("RB2701", date(2026, 10, 8), "SHFE") == ("rb2701", "2027-01")
    assert normalize_contract("c2701", date(2026, 10, 8), "DCE") == ("c2701", "2027-01")


def test_display_product_uses_exchange_case() -> None:
    assert _display_product("ta", "CZCE") == "TA"
    assert _display_product("rb", "SHFE") == "rb"


def test_to_dates_parses_exchange_integer_dates() -> None:
    series = pd.Series([20261008, 20261009])
    parsed = _to_dates(series)
    assert [d.date() for d in parsed] == [date(2026, 10, 8), date(2026, 10, 9)]


def _daily(rows: list[tuple[str, str, str, int, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp(day),
                "variety": variety,
                "symbol": symbol,
                "open_interest": oi,
                "volume": vol,
            }
            for day, variety, symbol, oi, vol in rows
        ]
    )


def test_dominant_periods_merges_consecutive_same_contract() -> None:
    daily = _daily(
        [
            ("2026-10-08", "rb", "RB2701", 1000, 500),
            ("2026-10-09", "rb", "RB2701", 1200, 600),
            ("2026-10-12", "rb", "RB2705", 1500, 100),
            ("2026-10-13", "rb", "RB2705", 1600, 120),
        ]
    )

    periods = dominant_periods(daily, "rb", "螺纹钢", "SHFE", min_days=0)

    assert [(p["contract"], p["start"], p["end"], p["days"]) for p in periods] == [
        ("rb2701", date(2026, 10, 8), date(2026, 10, 9), 2),
        ("rb2705", date(2026, 10, 12), date(2026, 10, 13), 2),
    ]
    assert periods[0]["avg_oi"] == 1100


def test_dominant_periods_breaks_tie_by_volume() -> None:
    daily = _daily(
        [
            ("2026-10-08", "rb", "RB2701", 1000, 300),
            ("2026-10-08", "rb", "RB2705", 1000, 900),
        ]
    )

    periods = dominant_periods(daily, "rb", "螺纹钢", "SHFE", min_days=0)

    assert periods[0]["contract"] == "rb2705"


def test_dominant_periods_empty_when_product_missing() -> None:
    daily = _daily([("2026-10-08", "rb", "RB2701", 1000, 500)])
    assert dominant_periods(daily, "bu", "沥青", "SHFE") == []


def test_dominant_periods_ignores_marginal_lead() -> None:
    """持仓量只领先一点点不算换月：要领先 5% 且连续 2 个交易日。"""
    daily = _daily(
        [
            ("2026-10-08", "fu", "FU2605", 1000, 500),
            ("2026-10-08", "fu", "FU2606", 300, 100),
            ("2026-10-09", "fu", "FU2605", 1000, 500),
            ("2026-10-09", "fu", "FU2606", 1001, 400),  # 只多 0.1%，不换
            ("2026-10-12", "fu", "FU2605", 1000, 500),
            ("2026-10-12", "fu", "FU2606", 1100, 400),  # 领先 10%，第 1 天
            ("2026-10-13", "fu", "FU2605", 1000, 500),
            ("2026-10-13", "fu", "FU2606", 1200, 400),  # 第 2 天，认换月
            ("2026-10-14", "fu", "FU2605", 1000, 500),
            ("2026-10-14", "fu", "FU2606", 1300, 400),
        ]
    )

    periods = dominant_periods(daily, "fu", "燃油", "SHFE", min_days=0)

    assert [(p["contract"], p["start"], p["end"]) for p in periods] == [
        ("fu2605", date(2026, 10, 8), date(2026, 10, 12)),
        ("fu2606", date(2026, 10, 13), date(2026, 10, 14)),
    ]


def test_dominant_periods_merges_short_blip() -> None:
    """两三天的抖动段并到下一段，不单独算一次换月。"""
    rows = []
    for day in ("10-08", "10-09", "10-12", "10-13", "10-14", "10-15", "10-16", "10-19", "10-20", "10-21", "10-22"):
        rows.append((f"2026-{day}", "fu", "FU2605", 900, 500))
        rows.append((f"2026-{day}", "fu", "FU2606", 500, 100))
    rows.extend(
        [
            ("2026-10-23", "fu", "FU2605", 900, 500),
            ("2026-10-23", "fu", "FU2606", 1500, 400),
            ("2026-10-26", "fu", "FU2606", 1600, 400),
            ("2026-10-26", "fu", "FU2609", 900, 200),
            ("2026-10-27", "fu", "FU2606", 1600, 400),
            ("2026-10-27", "fu", "FU2609", 2000, 500),
            ("2026-10-28", "fu", "FU2606", 1600, 400),
            ("2026-10-28", "fu", "FU2609", 2100, 500),
            ("2026-10-29", "fu", "FU2609", 2200, 500),
            ("2026-10-30", "fu", "FU2609", 2300, 500),
        ]
    )
    daily = _daily(rows)

    periods = dominant_periods(daily, "fu", "燃油", "SHFE", min_days=10)

    # 12 个交易日的 fu2605；fu2606 只站了 2 天（抖动），被并进下一段 fu2609
    assert [(p["contract"], p["start"], p["end"], p["days"]) for p in periods] == [
        ("fu2605", date(2026, 10, 8), date(2026, 10, 23), 12),
        ("fu2609", date(2026, 10, 26), date(2026, 10, 30), 5),
    ]


def test_write_workbook_keeps_manual_sheet(tmp_path) -> None:
    """更新表时「手动修正」页原样保留，不会被自动结果覆盖。"""
    path = tmp_path / "主力合约表.xlsx"
    products = [{"symbol": "rb2701", "name": "螺纹钢", "product": "rb", "exchange": "SHFE"}]
    periods = [
        {
            "product": "rb",
            "name": "螺纹钢",
            "exchange": "SHFE",
            "contract": "rb2701",
            "delivery": "2027-01",
            "start": date(2026, 1, 5),
            "end": date(2026, 1, 30),
            "days": 18,
            "oi_sum": 18000.0,
            "avg_oi": 1000,
        }
    ]
    manual = [["rb", "RB2705", date(2026, 3, 2), date(2026, 3, 31), "自己改的"]]
    write_workbook(path, products, periods, date(2026, 1, 5), date(2026, 1, 30), "测试", manual)
    assert [(r.product, r.symbol) for r in read_manual_rows(path)] == [("rb", "RB2705")]

    # 第二次写表不传 manual_rows，相当于界面点更新，手改内容要还在
    write_workbook(path, products, periods, date(2026, 1, 5), date(2026, 1, 30), "测试2")
    rows = read_manual_rows(path)
    assert len(rows) == 1
    assert rows[0].symbol == "RB2705"
    assert rows[0].start == date(2026, 3, 2)
    assert rows[0].note == "自己改的"
