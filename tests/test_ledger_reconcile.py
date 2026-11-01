"""启动对账：策略持仓、风控净仓和当日盈亏都跟信号账户走。"""

from src.account.signal_book import record_program_trade
from src.scheduler.runner import TradingBot
from src.utils.state_store import default_state_path, load_state, save_state
from src.utils.trading_day import futures_trading_day


def _cfg(root) -> dict:
    """一套最小的配置。构造 bot 时会自动读状态并跟账本对一次。"""
    return {
        "dry_run": True,
        "trade": {"symbol": "bu2612", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(root),
    }


def _write_state(root, *, daily_pnl=0.0, positions=None) -> None:
    """铺一份 bot_state。交易日必须写当前交易日，否则读出来会按换日清零。"""
    save_state(
        default_state_path(root),
        {
            "trading_day": str(futures_trading_day()),
            "daily_pnl": daily_pnl,
            "positions": positions or {},
        },
    )


def _events(root) -> str:
    path = root / "data" / "events.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _record_open(root, *, fill_key: str = "seed-open") -> None:
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
        fill_key=fill_key,
    )


def _record_close(root, *, fill_key: str = "seed-close") -> None:
    record_program_trade(
        root,
        initial_capital=10000,
        symbol="bu2612",
        offset="平仓",
        direction="多",
        price=3520,
        volume=2,
        multiplier=10,
        fill_key=fill_key,
    )


def test_book_open_rebuilds_missing_strategy_position(tmp_path):
    """账本上还开着仓，磁盘状态没有时，启动要按账本把持仓补回来。"""
    _record_open(tmp_path)
    _write_state(tmp_path)

    bot = TradingBot(_cfg(tmp_path))
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "long"
    assert pos.volume == 2
    assert pos.entry_price == 3500.0
    assert pos.trail_stop == 3480.0
    assert bot.risk.positions["bu2612"] == 2
    assert "ledger_reconcile" in _events(tmp_path)


def test_book_flat_clears_strategy_position(tmp_path):
    """账本已经平掉，磁盘状态还写着有仓时，不能把已平的仓又留下。"""
    _record_open(tmp_path)
    _record_close(tmp_path)
    _write_state(
        tmp_path,
        positions={
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
    )

    bot = TradingBot(_cfg(tmp_path))
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "flat"
    assert pos.volume == 0


def test_empty_book_keeps_persisted_position(tmp_path):
    """信号账户还没有成交时，不要用空账把磁盘里的真实持仓清掉。"""
    _write_state(
        tmp_path,
        positions={
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
    )

    bot = TradingBot(_cfg(tmp_path))
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "long"
    assert pos.volume == 2
    assert "ledger_reconcile" not in _events(tmp_path)


def test_reconcile_aligns_daily_pnl_with_book(tmp_path):
    """磁盘上旧的当日盈亏要按账本重算，不能沿用上一次的数。"""
    _record_open(tmp_path)
    _record_close(tmp_path)
    _write_state(tmp_path, daily_pnl=999)

    bot = TradingBot(_cfg(tmp_path))
    # (3520-3500) × 2 手 × 乘数 10 = 400。
    assert bot.risk.daily_pnl == 400.0
    assert load_state(default_state_path(tmp_path))["daily_pnl"] == 400.0
