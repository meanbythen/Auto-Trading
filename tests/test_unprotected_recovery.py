"""退出保护：上次退出还有持仓时，重启先对账再恢复报单；有仓退出要留标记。"""

import json

from src.domain.position import PositionState
from src.scheduler.runner import TradingBot


def _cfg(root) -> dict:
    """一套最小的配置，构造 bot 时会自动读状态并跟账本对一次。"""
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


def _flag_path(root):
    return root / "data" / "unprotected_positions.json"


def _write_marker(root, *, reason: str = "clean_exit") -> None:
    """铺一份「上次退出还有仓」的标记。"""
    path = _flag_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"reason": reason, "positions": {"bu2612": {"side": "long", "volume": 1}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _ready_bot(tmp_path, monkeypatch, *, queried: bool) -> TradingBot:
    """构造 bot 并把扫描里会碰网络/行情的部分换掉。"""
    bot = TradingBot(_cfg(tmp_path))
    bot.logged_in = True
    bot._running = True
    monkeypatch.setattr(bot, "_reconcile_ctp", lambda: bot.__setattr__("_reconcile_state", {"queried": queried}))
    monkeypatch.setattr(bot, "scan_symbol", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_maybe_check_health", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_maybe_run_session_audit", lambda *a, **k: None)
    return bot


def test_marker_pauses_orders_at_startup(tmp_path) -> None:
    """有标记时启动就暂停报单，等对账完成再恢复。"""
    _write_marker(tmp_path)
    bot = TradingBot(_cfg(tmp_path))

    assert bot._unprotected_resume is True
    assert bot._orders_paused is True


def test_scan_once_recovers_after_reconcile(tmp_path, monkeypatch) -> None:
    """柜台对账成功的一轮扫描结束后恢复报单并清掉标记。"""
    _write_marker(tmp_path)
    bot = _ready_bot(tmp_path, monkeypatch, queried=True)

    bot.scan_once(force=True)

    assert bot._unprotected_resume is False
    assert bot._orders_paused is False
    assert _flag_path(tmp_path).exists() is False


def test_scan_once_keeps_paused_when_reconcile_fails(tmp_path, monkeypatch) -> None:
    """柜台对账没成功就继续暂停，标记留到下一轮。"""
    _write_marker(tmp_path)
    bot = _ready_bot(tmp_path, monkeypatch, queried=False)

    bot.scan_once(force=True)

    assert bot._unprotected_resume is True
    assert bot._orders_paused is True
    assert _flag_path(tmp_path).exists() is True


def test_exit_flag_written_only_with_open_risk(tmp_path) -> None:
    """有仓退出写标记；清仓后再写就把标记删掉。"""
    bot = TradingBot(_cfg(tmp_path))
    bot.strategy.positions["bu2612"] = PositionState(side="long", volume=1, symbol="bu2612")

    bot._write_unprotected_flag("clean_exit")

    path = _flag_path(tmp_path)
    assert path.exists() is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "bu2612" in saved["positions"]
    assert saved["reason"] == "clean_exit"

    bot.strategy.positions["bu2612"] = PositionState(symbol="bu2612")
    bot._write_unprotected_flag("clean_exit")

    assert path.exists() is False
    assert bot._has_open_risk() is False
