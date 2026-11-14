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
