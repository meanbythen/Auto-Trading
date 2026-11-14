"""常驻还在跑时，不能再起一套试跑。"""

from src.ui.bot_worker import BotWorker


def test_run_once_refuses_when_resident_is_alive() -> None:
    worker = BotWorker()
    worker._alive = True
    assert worker.run_once({"trade": {}}) is False


def test_run_once_starts_when_idle(monkeypatch) -> None:
    worker = BotWorker()
    started: list[str] = []

    class _FakeThread:
        def __init__(self, target, name, daemon):
            self._target = target
            started.append(name)

        def start(self) -> None:
            started.append("start")

    monkeypatch.setattr("src.ui.bot_worker.threading.Thread", _FakeThread)
    assert worker.run_once({"trade": {}}) is True
    assert "TradingBotOnce" in started
    assert "start" in started


def test_run_once_marks_busy_while_running_and_clears_after(monkeypatch) -> None:
    """试跑期间与常驻共用 busy 标志：运行中不能再开第二套，结束后自动释放。"""
    worker = BotWorker()
    seen: list[tuple[str, bool]] = []

    class _FakeBot:
        def __init__(self, cfg):
            pass

        def do_login(self) -> None:
            seen.append(("login", worker.running))

        def scan_once(self, force: bool = False) -> None:
            seen.append(("scan", worker.running))

    class _SyncThread:
        def __init__(self, target, name, daemon):
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr("src.ui.bot_worker.TradingBot", _FakeBot)
    monkeypatch.setattr("src.ui.bot_worker.threading.Thread", _SyncThread)

    assert worker.run_once({"trade": {}}) is True
    assert seen == [("login", True), ("scan", True)]
    assert worker.running is False


def test_single_instance_acquire_is_idempotent_and_releasable() -> None:
    from src.utils.single_instance import acquire, release

    name = "AutoTrading_Test_SingleInstance"
    release()
    try:
        assert acquire(name) is True
        assert acquire(name) is True
    finally:
        release()
    assert acquire(name) is True
    release()
