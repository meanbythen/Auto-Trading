"""收盘复盘：用假K线验证盘面、策略对照和报告结构，不访问外网。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from src.review.fundamental import product_of
from src.review.replay import coarse_reason
from src.review.service import build_review
from src.review.session import windows_for
from src.review.technical import analyze_window


def _day_bars(day: date, *, start_price: float, step: float) -> pd.DataFrame:
    """造一段 09:00–15:00 的 15 分钟K线。step>0 表示逐根上涨。"""
    times = []
    cursor = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=0)
    end = datetime.combine(day, datetime.min.time()).replace(hour=15, minute=0)
    while cursor <= end:
        times.append(cursor)
        cursor += timedelta(minutes=15)
    rows = []
    price = start_price
    for _ in times:
        o = price
        c = price + step
        rows.append(
            {
                "open": o,
                "high": max(o, c) + 2,
                "low": min(o, c) - 1,
                "close": c,
                "volume": 100,
            }
        )
        price = c
    return pd.DataFrame(rows, index=pd.DatetimeIndex(times))


def test_product_code_does_not_confuse_corn_and_starch() -> None:
    """玉米 c2511 不能被认成玉米淀粉 cs。"""
    assert product_of("bu2612")[:2] == ("bu", "沥青")
    assert product_of("c2511")[0] == "c"
    assert product_of("cs2505")[0] == "cs"


def test_night_window_crosses_midnight() -> None:
    """周三这个交易日的夜盘，是周二晚上 21:00 开到周三 02:30。"""
    windows = windows_for(date(2026, 10, 14), "夜盘")
    assert len(windows) == 1
    assert windows[0].start == datetime(2026, 10, 13, 21, 0)
    assert windows[0].end == datetime(2026, 10, 14, 2, 30)


def test_monday_night_starts_on_friday() -> None:
    """周一没有前一晚，夜盘从上周五 21:00 起。"""
    windows = windows_for(date(2026, 10, 12), "夜盘")
    assert windows[0].start == datetime(2026, 10, 9, 21, 0)
    assert windows[0].end == datetime(2026, 10, 12, 2, 30)


def test_both_scope_is_previous_night_then_day() -> None:
    """「前夜盘+日盘」先看上一交易日夜盘，再看当天日盘。"""
    windows = windows_for(date(2026, 10, 14), "前夜盘+日盘")
    assert [w.name for w in windows] == ["夜盘", "日盘"]
    assert windows[0].start == datetime(2026, 10, 13, 21, 0)
    assert windows[1].start == datetime(2026, 10, 14, 9, 0)


def test_up_day_is_called_strong() -> None:
    """收在高位的上涨，技术面应说偏强，并且不依赖任何策略。"""
    day = date(2026, 10, 14)
    bars = _day_bars(day, start_price=3000, step=4)
    window = windows_for(day, "日盘")[0]
    view = analyze_window("日盘", bars, window)
    assert view.has_bars
    assert view.bias == "up"
    assert "偏强" in view.character


def test_review_report_covers_tape_and_why_flat_or_open(tmp_path) -> None:
    """
    整篇报告必须同时有盘面和策略结论。
    用两天上涨K线喂给日内实体：要么写出开仓原因，要么写出观望原因。
    """
    day = date(2026, 10, 14)
    prev = _day_bars(date(2026, 10, 13), start_price=2800, step=1)
    today = _day_bars(day, start_price=3100, step=3)
    bars = pd.concat([prev, today])
    cfg = {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "daily_body": {
                "eth_minutes": 0,
            },
        },
        "trade": {"symbol": "bu2612", "volume": 1, "executor": "sim"},
        "risk": {"capital": 1_000_000},
        "data": {"source": "demo"},
        "schedule": {"trading_sessions": [["09:00", "15:00"]]},
    }
    text = build_review(
        cfg,
        symbol="bu2612",
        trade_date=day,
        scope="日盘",
        engine="daily_body",
        user_note="库存下降，现货询价增多",
        bars_signal=bars,
        bars_trail=bars,
        now=datetime(2026, 10, 14, 16, 0),
        fetch_remote=False,
        save=False,
        root=tmp_path,
    )
    assert "一、当天盘面怎么走" in text
    assert "二、技术面" in text
    assert "三、当日基本面" in text
    assert "库存下降，现货询价增多" in text
    assert "四、策略对照" in text
    assert "五、信号质量" in text
    assert "六、程序当天真实记录" in text
    assert "【当日成绩单】" in text
    assert "十、当日重放评估" in text
    assert "十一、最近实盘 vs 最近回测" in text
    assert "十二、综合" in text
    assert ("会开仓" in text) or ("观望" in text)
    assert "日盘已收盘" in text


def test_market_only_does_not_invent_a_strategy_order(tmp_path) -> None:
    day = date(2026, 10, 14)
    bars = _day_bars(day, start_price=2200, step=0)
    text = build_review(
        {"strategy": {"timeframe": "15", "trail_timeframe": "15", "symbols": ["c2511"]}, "trade": {}},
        symbol="c2511",
        trade_date=day,
        scope="日盘",
        engine=None,
        bars_signal=bars,
        now=datetime(2026, 10, 14, 16, 0),
        fetch_remote=False,
        save=True,
        root=tmp_path,
    )
    assert "没有套用程序里的任何策略" in text
    saved = tmp_path / "data" / "reviews" / "2026-10-14_c2511.txt"
    assert saved.exists()


def test_coarse_reason_ignores_minutes_left() -> None:
    a = coarse_reason("ETH时间 09:00-09:30，观察中，约剩 12 分钟")
    b = coarse_reason("ETH时间 09:00-09:30，观察中，约剩 3 分钟")
    assert a == b
