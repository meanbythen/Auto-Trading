"""交易台还登着时不能再开常驻；没有交易所止损单时要写清。"""

from types import SimpleNamespace

from src.trader.protective import overnight_unprotected_notice
from src.ui.ctp_probe import (
    desk_login_block_reason,
    probe_login_block_reason,
    resident_ctp_trader,
    shutdown_probe_window,
)


def test_desk_idle_does_not_block_resident() -> None:
    trader = SimpleNamespace(
        settings=SimpleNamespace(user_id="20318052"),
        gateway=SimpleNamespace(session=SimpleNamespace(phase="idle", ready=False)),
    )
    assert desk_login_block_reason(trader) == ""
    assert desk_login_block_reason(None) == ""


def test_desk_logged_in_blocks_resident() -> None:
    trader = SimpleNamespace(
        settings=SimpleNamespace(user_id="20318052"),
        gateway=SimpleNamespace(session=SimpleNamespace(phase="ready", ready=True)),
    )
    text = desk_login_block_reason(trader)
    assert "20318052" in text
    assert "踢" in text


def test_desk_handshaking_blocks_resident() -> None:
    trader = SimpleNamespace(
        settings=SimpleNamespace(user_id="1"),
        gateway=SimpleNamespace(session=SimpleNamespace(phase="authenticated", ready=False)),
    )
    assert desk_login_block_reason(trader)


def test_shutdown_busy_probe_keeps_window() -> None:
    closed = {"n": 0}

    class FakeWin:
        def winfo_exists(self) -> bool:
            return True

        def _close(self) -> None:
            closed["n"] += 1

    app = SimpleNamespace(_ctp_probe=FakeWin())
    app._ctp_probe._busy = True
    assert "等" in shutdown_probe_window(app)
    assert closed["n"] == 0
    app._ctp_probe._busy = False
    assert shutdown_probe_window(app) == ""
    assert closed["n"] == 1


def test_resident_trader_only_when_ctp_running() -> None:
    """常驻开着且走柜台时，交易台应拿到同一条连接；没开或点同花顺时不要硬借。"""
    trader = object()
    app = SimpleNamespace(
        worker=SimpleNamespace(running=True, bot=SimpleNamespace(trader=trader)),
    )
    # 不是 CtpTrader 就不能借，避免把同花顺通道误当成柜台。
    assert resident_ctp_trader(app) is None
    app.worker.running = False
    assert resident_ctp_trader(app) is None


def test_attached_desk_does_not_block_resident() -> None:
    """窗口只是在看常驻连接时，不算第二条登录。"""
    class FakeWin:
        _attached_resident = True
        trader = SimpleNamespace(
            settings=SimpleNamespace(user_id="20318052"),
            gateway=SimpleNamespace(session=SimpleNamespace(phase="ready", ready=True)),
        )

        def winfo_exists(self) -> bool:
            return True

    app = SimpleNamespace(_ctp_probe=FakeWin())
    assert probe_login_block_reason(app) == ""


def test_overnight_notice_only_when_live() -> None:
    assert overnight_unprotected_notice(live=False, dry_run=False) == ""
    dry = overnight_unprotected_notice(live=True, dry_run=True)
    assert "没有交易所止损单" in dry
    assert "模拟还开着" in dry
    text = overnight_unprotected_notice(live=True, dry_run=False)
    assert "没有交易所止损单" in text
    assert "过夜" in text
    assert "模拟还开着" not in text
