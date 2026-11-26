"""三方对账：柜台、信号账户账本、程序内存。"""

from types import SimpleNamespace

import src.account.signal_book as signal_book
from src.account.signal_book import load_book, record_program_trade
from src.scheduler.runner import TradingBot
from src.trader.ctp_logic import PositionView, ReconcileReport, three_way_diff
from src.trader.ctp_trader import CtpTrader
from src.utils.state_store import default_state_path, load_state, save_state
from src.utils.trading_day import futures_trading_day


def _cfg(root, *, dry_run: bool = True) -> dict:
    """一套最小的配置。构造 bot 时会自动读状态并跟账本对一次。"""
    return {
        "dry_run": dry_run,
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


def _position(side: str = "long", volume: int = 1, entry: float = 3500.0) -> dict:
    return {
        "side": side,
        "volume": volume,
        "entry_price": entry,
        "initial_stop": entry - 20,
        "trail_stop": entry - 20,
        "trail_armed": True,
        "opened_at": "",
    }


def _write_state(root, *, positions=None, daily_pnl: float = 0.0) -> None:
    """铺一份 bot_state。交易日必须写当前交易日，否则读出来会按换日清零。"""
    save_state(
        default_state_path(root),
        {
            "trading_day": str(futures_trading_day()),
            "daily_pnl": daily_pnl,
            "positions": positions or {},
        },
    )


def _record_open(root, *, volume: int = 1, price: float = 3500.0, fill_key: str = "seed-open") -> None:
    record_program_trade(
        root,
        initial_capital=10000,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=price,
        volume=volume,
        multiplier=10,
        stop_price=price - 20,
        fill_key=fill_key,
    )


def _close_rows(root) -> list[dict]:
    book = load_book(root) or {}
    return [row for row in book.get("trades") or [] if str(row.get("offset")) == "平仓"]


def _report(
    *,
    broker: dict | None = None,
    ledger: dict | None = None,
    memory: dict | None = None,
    diffs: list[str] | None = None,
    unclaimed: list[str] | None = None,
    phantom: list[str] | None = None,
    extra: list[str] | None = None,
    queried: bool = True,
) -> ReconcileReport:
    rows = list(diffs or [])
    return ReconcileReport(
        at="2026-11-15T09:30:00",
        queried=queried,
        broker=dict(broker or {}),
        ledger=dict(ledger or {}),
        memory=dict(memory or {}),
        funds=10000.0,
        ok=queried and not rows,
        diffs=rows,
        unclaimed=list(unclaimed or []),
        phantom=list(phantom or []),
        extra=list(extra or []),
    )


def test_three_way_diff_consistent() -> None:
    """三边一样时没有任何差异。"""
    ok, diffs, unclaimed, phantom, extra = three_way_diff(
        {"BU2612": 2}, {"bu2612": 2}, {"bu2612": 2}
    )
    assert ok is True
    assert diffs == []
    assert unclaimed == []
    assert phantom == []
    assert extra == []


def test_memory_only_difference_aligns_to_ledger(tmp_path, monkeypatch) -> None:
    """柜台和账本一致、只有内存不同时，不拦开仓，内存按账本重建。"""
    _write_state(tmp_path, positions={"bu2612": _position(volume=1)})
    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)
    _record_open(tmp_path, volume=2)

    ok, diffs, unclaimed, phantom, extra = three_way_diff(
        {"bu2612": 2}, {"bu2612": 2}, {"bu2612": 1}
    )
    assert ok is False
    assert diffs == []
    assert extra == []
    assert unclaimed == [] and phantom == []

    bot._apply_reconcile(
        _report(broker={"bu2612": 2}, ledger={"bu2612": 2}, memory={"bu2612": 1}),
        phase="runtime",
    )
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "long"
    assert pos.volume == 2
    assert pos.origin == "ledger"


def test_broker_ledger_mismatch_blocks_and_aligns_memory(tmp_path, monkeypatch) -> None:
    """柜台和账本各说各话时拦开仓，内存对齐柜台但不冻结。"""
    settings = SimpleNamespace(
        query_interval_sec=0.0,
        project_root=str(tmp_path),
        user_id="sim",
        td_front="",
        app_id="",
        account_kind="sim",
        close_split=(),
    )
    gateway = SimpleNamespace(
        settings=settings,
        session=SimpleNamespace(ready=True),
        positions={"BU2612": PositionView(short_today=2, short_open_cost=70000.0)},
        available=5000.0,
        account={},
        refresh=lambda: True,
        table=SimpleNamespace(orders={}),
        unknown_working=False,
    )
    trader = CtpTrader({"dry_run": False}, gateway=gateway)
    trader._sync_signal_book = lambda: ""

    ok, _note = trader.reconcile({"bu2612": 1}, {"bu2612": 1})
    assert ok is False
    report = trader.last_report
    assert report is not None and report.queried is True
    assert report.broker == {"bu2612": -2}
    assert report.diffs
    assert trader.blocks_new_opens() is True

    # 只传 internal 时保持旧行为：报告里的账本为空。
    ok, _note = trader.reconcile({"BU2612": -2})
    assert ok is True
    assert trader.last_report is not None
    assert trader.last_report.ledger == {}

    _write_state(tmp_path, positions={"bu2612": _position(volume=1)})
    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)
    bot._apply_reconcile(
        _report(
            broker={"bu2612": -2},
            ledger={"bu2612": 1},
            memory={"bu2612": 1},
            diffs=["bu2612 柜台 -2 手，账本 1 手"],
        ),
        phase="runtime",
    )
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "short"
    assert pos.volume == 2
    assert pos.origin == "broker"
    assert "bu2612" not in bot._frozen_symbols


def test_unclaimed_broker_position_installed_and_frozen(tmp_path, monkeypatch) -> None:
    """柜台有、账本没有：补进内存并冻结，扫描直接跳过。"""
    bot = TradingBot(_cfg(tmp_path))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)

    def _boom(*_args, **_kwargs):
        raise AssertionError("冻结品种不应拉K线")

    monkeypatch.setattr(bot.kline, "fetch", _boom)

    bot._apply_reconcile(
        _report(
            broker={"bu2612": 1},
            ledger={},
            memory={},
            diffs=["bu2612 柜台 1 手，账本 0 手"],
            unclaimed=["bu2612"],
        ),
        phase="startup",
    )
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "long"
    assert pos.volume == 1
    assert pos.origin == "broker"
    assert "bu2612" in bot._frozen_symbols

    bot.scan_symbol("bu2612")
    assert "bu2612" in bot._frozen_noted


def test_reject_unclaimed_follows_local_book(tmp_path, monkeypatch) -> None:
    """拒绝仓单：柜台有、账本没有时不收进来，解除冻结，等下一根信号。"""
    bot = TradingBot(_cfg(tmp_path, dry_run=False))
    bot._frozen_symbols.add("lh2701")
    bot._reconcile_state = {"unclaimed": ["lh2701"], "phantom": [], "extra": [], "ok": False}
    monkeypatch.setattr(bot.kline, "fetch", lambda *_args, **_kwargs: None)

    ok, msg = bot.reject_broker_lots("lh2701")

    assert ok is True
    assert "lh2701" not in bot._frozen_symbols
    assert bot._reconcile_state["unclaimed"] == []
    assert "程序账" in msg


def test_ignore_keeps_symbol_frozen(tmp_path) -> None:
    """「忽略」只清待办提示，不解除冻结。"""
    bot = TradingBot(_cfg(tmp_path))
    bot._frozen_symbols.add("bu2612")
    bot._reconcile_state = {"unclaimed": ["bu2612"], "phantom": [], "extra": [], "ok": False}

    ok, _msg = bot.ignore_reconcile_symbol("bu2612")

    assert ok is True
    assert "bu2612" in bot._frozen_symbols
    assert bot._reconcile_state["unclaimed"] == []


def test_phantom_repair_needs_two_runtime_rounds(tmp_path, monkeypatch) -> None:
    """账本有、柜台没有：runtime 第一次只记轮数，第二次才补记平仓。"""
    _write_state(tmp_path, positions={"bu2612": _position(volume=1)})
    bot = TradingBot(_cfg(tmp_path, dry_run=False))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)
    monkeypatch.setattr(signal_book, "write_capital_to_config", lambda balance: None)
    _record_open(tmp_path, volume=1)

    report = _report(
        broker={},
        ledger={"bu2612": 1},
        memory={"bu2612": 1},
        diffs=["bu2612 柜台 0 手，账本 1 手"],
        phantom=["bu2612"],
    )
    bot._apply_reconcile(report, phase="runtime")
    assert _close_rows(tmp_path) == []
    assert bot.strategy.positions["bu2612"].volume == 1

    bot._apply_reconcile(report, phase="runtime")
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "flat"
    assert pos.volume == 0
    assert len(_close_rows(tmp_path)) == 1


def test_phantom_repaired_immediately_at_startup(tmp_path, monkeypatch) -> None:
    """startup 阶段不用等两轮，直接补记平仓。"""
    _write_state(tmp_path, positions={"bu2612": _position(volume=1)})
    bot = TradingBot(_cfg(tmp_path, dry_run=False))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)
    monkeypatch.setattr(signal_book, "write_capital_to_config", lambda balance: None)
    _record_open(tmp_path, volume=1)

    bot._apply_reconcile(
        _report(
            broker={},
            ledger={"bu2612": 1},
            memory={"bu2612": 1},
            diffs=["bu2612 柜台 0 手，账本 1 手"],
            phantom=["bu2612"],
        ),
        phase="startup",
    )
    assert bot.strategy.positions["bu2612"].side == "flat"
    assert len(_close_rows(tmp_path)) == 1


def test_unqueried_report_never_repairs(tmp_path) -> None:
    """柜台没查成时不改内存、不冻结。"""
    _write_state(tmp_path, positions={"bu2612": _position(volume=1)})
    bot = TradingBot(_cfg(tmp_path))

    bot._apply_reconcile(
        _report(queried=False, memory={"bu2612": 1}, diffs=["查询柜台持仓或资金失败"]),
        phase="runtime",
    )
    pos = bot.strategy.positions["bu2612"]
    assert pos.side == "long"
    assert pos.volume == 1
    assert not bot._frozen_symbols
    assert bot._reconcile_state.get("queried") is False


def test_persist_and_status_expose_reconcile(tmp_path) -> None:
    """状态文件和运行台快照都带上对账结果与冻结清单。"""
    bot = TradingBot(_cfg(tmp_path))
    bot._apply_reconcile(
        _report(queried=False, memory={}, diffs=["查询柜台持仓或资金失败"]),
        phase="runtime",
    )
    bot._persist_state()

    st = load_state(default_state_path(tmp_path))
    assert st["reconcile"]["phase"] == "runtime"
    assert st["frozen_symbols"] == []

    snap = bot.status_snapshot()
    assert snap["reconcile"]["queried"] is False
    assert snap["frozen_symbols"] == []


def test_unclaimed_after_own_close_does_not_reopen(tmp_path, monkeypatch) -> None:
    """刚收盘清仓成交后，柜台查询还显示空单：不能冻结，也不能把仓再开回来。"""
    bot = TradingBot(_cfg(tmp_path, dry_run=False))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 9405.0)
    monkeypatch.setattr(signal_book, "write_capital_to_config", lambda _balance: None)
    bot._note_own_fill("lh2701", kind="close")
    bot._apply_reconcile(
        _report(
            broker={"lh2701": -1},
            ledger={},
            memory={},
            diffs=["lh2701 柜台 -1 手，账本 0 手"],
            unclaimed=["lh2701"],
        ),
        phase="runtime",
    )
    pos = bot.strategy.positions.get("lh2701")
    held = int(getattr(pos, "volume", 0) or 0) if pos is not None else 0
    assert held == 0
    assert "lh2701" not in bot._frozen_symbols
    book = load_book(tmp_path) or {}
    opens = [row for row in book.get("trades") or [] if str(row.get("offset")) == "开仓"]
    assert opens == []
    rec = bot._reconcile_state or {}
    assert rec.get("unclaimed") == []
    assert rec.get("ok") is True


def test_unclaimed_recent_own_fill_is_auto_adopted(tmp_path, monkeypatch) -> None:
    """本程序刚成交、柜台已有仓、账本还是 0：自动认领，不再每轮冻结刷屏。"""
    bot = TradingBot(_cfg(tmp_path, dry_run=False))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)
    monkeypatch.setattr(signal_book, "write_capital_to_config", lambda _balance: None)
    bot._note_own_fill("bu2612")
    bot._apply_reconcile(
        _report(
            broker={"bu2612": -1},
            ledger={},
            memory={},
            diffs=["bu2612 柜台 -1 手，账本 0 手"],
            unclaimed=["bu2612"],
        ),
        phase="runtime",
    )
    book = load_book(tmp_path) or {}
    opens = [row for row in book.get("trades") or [] if str(row.get("offset")) == "开仓"]
    assert opens
    assert "bu2612" not in bot._frozen_symbols


def test_install_position_does_not_log_same_lot_twice(tmp_path) -> None:
    bot = TradingBot(_cfg(tmp_path))
    bot._install_position("bu2612", -1, 3892.0, "broker")
    first = bot.strategy.positions.get("bu2612")
    bot._install_position("bu2612", -1, 3892.0, "broker")
    second = bot.strategy.positions.get("bu2612")
    assert first is second


def test_phantom_skips_while_own_fill_is_fresh(tmp_path, monkeypatch) -> None:
    """刚成交时柜台查询可能还是 0 手，不能两轮就把程序账平掉。"""
    _write_state(tmp_path, positions={"bu2612": _position(volume=1)})
    bot = TradingBot(_cfg(tmp_path, dry_run=False))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)
    monkeypatch.setattr(signal_book, "write_capital_to_config", lambda _balance: None)
    _record_open(tmp_path, volume=1)
    bot._note_own_fill("bu2612")
    report = _report(
        broker={},
        ledger={"bu2612": 1},
        memory={"bu2612": 1},
        diffs=["bu2612 柜台 0 手，账本 1 手"],
        phantom=["bu2612"],
    )
    bot._apply_reconcile(report, phase="runtime")
    bot._apply_reconcile(report, phase="runtime")
    assert bot.strategy.positions["bu2612"].volume == 1
    assert _close_rows(tmp_path) == []


def test_reconcile_still_flattens_if_own_fill_helper_missing(tmp_path, monkeypatch) -> None:
    """旧进程没有 _own_fill_too_fresh 时，对账也不能整轮崩掉。"""
    _write_state(tmp_path, positions={"bu2612": _position(volume=1)})
    bot = TradingBot(_cfg(tmp_path, dry_run=False))
    monkeypatch.setattr(bot.kline, "latest_price", lambda symbol: 3500.0)
    monkeypatch.setattr(signal_book, "write_capital_to_config", lambda _balance: None)
    _record_open(tmp_path, volume=1)
    bot._own_fill_too_fresh = None  # type: ignore[assignment]
    report = _report(
        broker={},
        ledger={"bu2612": 1},
        memory={"bu2612": 1},
        diffs=["bu2612 柜台 0 手，账本 1 手"],
        phantom=["bu2612"],
    )
    bot._apply_reconcile(report, phase="runtime")
    bot._apply_reconcile(report, phase="runtime")
    assert bot.strategy.positions["bu2612"].side == "flat"
    assert len(_close_rows(tmp_path)) == 1


def test_startup_reconcile_unavailable_stays_silent(tmp_path, monkeypatch) -> None:
    """重启时柜台还没查完不写告警；已连上却查不到才报。"""
    bot = TradingBot(_cfg(tmp_path))
    alerts: list[tuple] = []
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: alerts.append((a, k)))

    bot._apply_reconcile(_report(queried=False), phase="startup")
    assert alerts == []
    assert bot._reconcile_alerted is False

    bot.logged_in = True
    bot._apply_reconcile(_report(queried=False), phase="runtime")
    assert [item[0][0] for item in alerts] == ["reconcile_unavailable"]
    assert bot._reconcile_alerted is True


def test_reconcile_ok_writes_recovery_once(tmp_path, monkeypatch) -> None:
    """对账异常恢复一致后补一条恢复记录，之后不再重复写。"""
    bot = TradingBot(_cfg(tmp_path))
    bot.logged_in = True
    alerts: list[tuple] = []
    recoveries: list[tuple] = []
    monkeypatch.setattr("src.ops.alerts.emit_alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(
        "src.ops.alerts.emit_recovery", lambda *a, **k: recoveries.append(a)
    )
    monkeypatch.setattr(bot, "_reconcile_repair_enabled", lambda: False)

    bot._apply_reconcile(
        _report(broker={"bu2612": 1}, diffs=["bu2612 柜台 1 手，账本 0 手"]),
        phase="runtime",
    )
    assert [item[0] for item in alerts] == ["reconcile_mismatch"]

    ok_report = _report(broker={"bu2612": 1}, ledger={"bu2612": 1}, memory={"bu2612": 1})
    bot._apply_reconcile(ok_report, phase="runtime")
    assert [item[0] for item in recoveries] == ["reconcile_ok"]
    bot._apply_reconcile(ok_report, phase="runtime")
    assert len(recoveries) == 1
