"""命令层与只读快照：配置页改参数、界面读持仓都只走 TradingBot 的公开接口。"""

from __future__ import annotations

from types import SimpleNamespace

from src.scheduler.runner import TradingBot


class _RecorderLock:
    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> "_RecorderLock":
        self.entered += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _bot() -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {}
    bot.scan_interval = 30
    bot.strategy = SimpleNamespace(positions={})
    bot._trade_lock = _RecorderLock()
    return bot


def test_apply_runtime_config_updates_running_parameters() -> None:
    bot = _bot()
    calls: list[object] = []
    bot._refresh_sessions = lambda: calls.append("refresh")  # type: ignore[method-assign]
    bot.sync_risk_limits = lambda value: calls.append(("limit", value))  # type: ignore[method-assign]

    bot.apply_runtime_config(
        {
            "schedule": {"scan_interval_sec": 12, "trading_sessions": ["09:00-10:15"]},
            "risk": {"contracts": {"fu": {"multiplier": 10}}, "max_daily_loss": 800},
        }
    )

    assert bot.scan_interval == 12
    assert bot.cfg["schedule"]["trading_sessions"] == ["09:00-10:15"]
    assert bot.cfg["risk"]["contracts"] == {"fu": {"multiplier": 10}}
    assert calls == ["refresh", ("limit", 800.0)]
    assert bot._trade_lock.entered == 1  # type: ignore[attr-defined]


def test_apply_runtime_config_falls_back_to_defaults() -> None:
    bot = _bot()
    bot._refresh_sessions = lambda: None  # type: ignore[method-assign]
    bot.sync_risk_limits = lambda value: None  # type: ignore[method-assign]

    bot.apply_runtime_config({})

    assert bot.scan_interval == 30
    assert bot.cfg["schedule"]["trading_sessions"] == []


def test_positions_snapshot_is_a_copy() -> None:
    bot = _bot()
    position = SimpleNamespace(side="long", volume=2, entry_price=3000.0, opened_at="2026-11-20T09:00")
    bot.strategy.positions = {"fu2701": position}

    snapshot = bot.positions_snapshot()

    assert snapshot == {
        "fu2701": {
            "side": "long",
            "volume": 2,
            "entry_price": 3000.0,
            "opened_at": "2026-11-20T09:00",
        }
    }
    snapshot["fu2701"]["volume"] = 99
    assert bot.strategy.positions["fu2701"].volume == 2
