"""换日清零只做一次，并且写回文件。界面反复读取时不能把同一句日志刷出来。"""

import json
from datetime import timedelta

from loguru import logger

from src.utils.state_store import load_state
from src.utils.trading_day import futures_trading_day


def _capture_info():
    """把这段时间里的 INFO 日志收进列表，测完就拆掉。"""
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    return lines, sink


def test_missing_trading_day_is_stamped_without_wiping_or_repeating(tmp_path) -> None:
    path = tmp_path / "bot_state.json"
    path.write_text(
        json.dumps(
            {
                "daily_pnl": 12.5,
                "orders_today": 2,
                "positions": {"bu2612": {"side": "short", "volume": 1}},
            }
        ),
        encoding="utf-8",
    )
    lines, sink = _capture_info()
    try:
        first = load_state(path)
        second = load_state(path)
    finally:
        logger.remove(sink)

    assert first["trading_day"] == str(futures_trading_day())
    assert first["daily_pnl"] == 12.5
    assert first["orders_today"] == 2
    assert first["positions"]["bu2612"]["side"] == "short"
    assert second["daily_pnl"] == 12.5
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["trading_day"] == str(futures_trading_day())
    assert not any("新交易日" in line for line in lines)


def test_real_day_change_resets_once_and_keeps_position(tmp_path) -> None:
    path = tmp_path / "bot_state.json"
    path.write_text(
        json.dumps(
            {
                "trading_day": "2020-01-01",
                "daily_pnl": 88,
                "orders_today": 4,
                "open_orders_today": 3,
                "halted": True,
                "halt_reason": "旧熔断",
                "positions": {"bu2612": {"side": "long", "volume": 1, "entry_price": 4600}},
            }
        ),
        encoding="utf-8",
    )
    lines, sink = _capture_info()
    try:
        first = load_state(path)
        second = load_state(path)
    finally:
        logger.remove(sink)

    assert first["daily_pnl"] == 0.0
    assert first["orders_today"] == 0
    assert first["open_orders_today"] == 0
    assert first["halted"] is False
    assert first["positions"]["bu2612"]["entry_price"] == 4600
    assert second["trading_day"] == str(futures_trading_day())
    assert sum("新交易日" in line for line in lines) == 1


def test_future_trading_day_is_not_rolled_back(tmp_path) -> None:
    """文件里的交易日比现在还新（系统时间被往回拨）时，不能反过来把计数清掉。"""
    path = tmp_path / "bot_state.json"
    future = (futures_trading_day() + timedelta(days=7)).isoformat()
    path.write_text(
        json.dumps(
            {
                "trading_day": future,
                "daily_pnl": 66,
                "orders_today": 2,
                "halted": True,
                "positions": {},
            }
        ),
        encoding="utf-8",
    )
    state = load_state(path)
    assert state["trading_day"] == future
    assert state["daily_pnl"] == 66
    assert state["orders_today"] == 2
    assert state["halted"] is True


def test_mark_position_closed_clears_the_recorded_lot() -> None:
    """按实际价格平账时，程序记下的这一笔仓要变成空仓。"""
    from src.utils.state_store import flatten_strategy_position, format_position_text, mark_position_closed

    positions = {
        "bu2612": {
            "side": "long",
            "volume": 1,
            "entry_price": 4832.0,
            "trail_stop": 4815.0,
            "extreme": 4900.0,
        }
    }
    closed = mark_position_closed(positions, "BU2612")
    assert closed is not None
    assert closed["side"] == "long"
    assert closed["entry_price"] == 4832.0
    assert format_position_text(positions) == "空仓"
    assert mark_position_closed(positions, "bu2612") is None

    class Pos:
        side = "short"
        volume = 2
        entry_price = 100.0
        initial_stop = 110.0
        trail_stop = 110.0
        trail_armed = True
        opened_at = "刚才"

    class Strategy:
        positions = {"ag2612": Pos()}
        _extreme = {"ag2612": 90.0}

    held = flatten_strategy_position(Strategy(), "ag2612")
    assert held is not None
    assert held["volume"] == 2
    assert Strategy.positions["ag2612"].side == "flat"


def test_manual_close_without_bot_aligns_daily_pnl_with_book(tmp_path) -> None:
    """常驻没跑时按成交价平账：状态文件里的空仓和当日盈亏都跟账本一致。"""
    from src.account.signal_book import load_book, record_program_trade, today_realized_pnl
    from src.utils.state_store import default_state_path, manual_close_without_bot, save_state

    root = tmp_path
    record_program_trade(
        root,
        initial_capital=10000,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=2,
        multiplier=10,
        stop_price=3480,
        fill_key="seed-open",
    )
    save_state(
        default_state_path(root),
        {
            "trading_day": str(futures_trading_day()),
            "daily_pnl": 0,
            "positions": {
                "bu2612": {
                    "side": "long",
                    "volume": 2,
                    "entry_price": 3500.0,
                    "initial_stop": 3480.0,
                    "trail_stop": 3480.0,
                    "trail_armed": True,
                    "opened_at": "",
                }
            },
        },
    )
    closed, balance = manual_close_without_bot(
        root,
        symbol="bu2612",
        exit_price=3520,
        multiplier=10,
        initial_capital=10000,
    )
    assert closed is not None
    assert closed["volume"] == 2
    assert isinstance(balance, float)

    state = load_state(default_state_path(root))
    assert state["positions"]["bu2612"]["side"] == "flat"

    book = load_book(root)
    assert state["daily_pnl"] == today_realized_pnl(book)
    assert any(
        str(row.get("fill_key") or "").startswith("manual-")
        for row in book["trades"]
        if row.get("offset") == "平仓"
    )
