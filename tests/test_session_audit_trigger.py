"""盘前行情审计触发与线程保护的单元测试（白盒，Mixin + Fake）。"""

from __future__ import annotations

from types import SimpleNamespace

from src.scheduler import session_audit
from src.scheduler.session_audit import SessionAuditMixin


class _Thread:
    instances = []

    def __init__(self, target, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.alive = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.alive


class _Bot(SessionAuditMixin):
    def __init__(self):
        self.cfg = {"strategy": {"period": "5"}}
        self.kline = object()
        self._last_audit_at = None
        self._audit_thread = None


def test_run_skips_when_previous_thread_is_alive(monkeypatch):
    bot = _Bot()
    bot._audit_thread = SimpleNamespace(is_alive=lambda: True)

    def boom(*args, **kwargs):
        raise AssertionError("must not create a second thread")

    monkeypatch.setattr(session_audit, "threading", SimpleNamespace(Thread=boom))
    bot._run_session_audit("test")

    assert bot._last_audit_at is None
    assert bot._audit_thread.is_alive() is True


def test_run_sets_timestamp_and_starts_daemon_thread(monkeypatch):
    _Thread.instances.clear()
    bot = _Bot()
    monkeypatch.setattr(session_audit, "threading", SimpleNamespace(Thread=_Thread))

    bot._run_session_audit("test")

    assert bot._last_audit_at is not None
    created = _Thread.instances[-1]
    assert created.name == "SessionAudit"
    assert created.daemon is True
    assert created.started is True
    assert bot._audit_thread is created


def test_thread_body_swallows_audit_exceptions(monkeypatch):
    _Thread.instances.clear()
    bot = _Bot()
    monkeypatch.setattr(session_audit, "threading", SimpleNamespace(Thread=_Thread))

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("src.strategy.session_audit.run_pre_session_audit", boom)
    bot._run_session_audit("test")

    _Thread.instances[-1].target()  # 不应抛出


def test_thread_body_runs_audit_and_formats_report(monkeypatch):
    _Thread.instances.clear()
    bot = _Bot()
    monkeypatch.setattr(session_audit, "threading", SimpleNamespace(Thread=_Thread))
    called = {}

    def run(cfg, bars_provider=None):
        called["cfg"] = cfg
        called["provider"] = bars_provider
        return [{"symbol": "bu2612"}]

    monkeypatch.setattr("src.strategy.session_audit.run_pre_session_audit", run)
    monkeypatch.setattr(
        "src.strategy.session_audit.format_audit_report",
        lambda rows: [f"row:{rows[0]['symbol']}"],
    )
    bot._audit_bars_provider = lambda: "provider"
    bot._run_session_audit("test")
    _Thread.instances[-1].target()

    assert called["cfg"] == bot.cfg
    assert called["provider"] == "provider"


def test_maybe_run_respects_trigger_schedule(monkeypatch):
    bot = _Bot()
    calls = []
    monkeypatch.setattr(
        "src.strategy.session_audit.should_run_session_audit", lambda now, last: False
    )
    bot._run_session_audit = lambda trigger="": calls.append(trigger)

    bot._maybe_run_session_audit()
    assert calls == []

    monkeypatch.setattr(
        "src.strategy.session_audit.should_run_session_audit", lambda now, last: True
    )
    bot._maybe_run_session_audit()
    assert calls and calls[0].startswith("盘前定时")


def test_audit_bars_provider_reuses_kline(monkeypatch):
    bot = _Bot()
    captured = {}

    def make(cfg, provider=None):
        captured["cfg"] = cfg
        captured["provider"] = provider
        return "made"

    monkeypatch.setattr("src.strategy.session_audit.make_bars_provider", make)
    assert bot._audit_bars_provider() == "made"
    assert captured["cfg"] == bot.cfg
    assert captured["provider"] is bot.kline
