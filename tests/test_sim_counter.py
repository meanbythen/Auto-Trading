from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.domain import trading_account as ta
from src.trader.ctp_gateway import CtpGateway, CtpSettings
from src.trader.ctp_orders import ctp_order
from src.trader.ctp_trader import CtpTrader
from src.ui.sim_counter import SimCounterController
from src.trader.sim_counter import SERVICE_NAME, request_once
from src.trader.sim_counter_client import SimCounterClient

ROOT = Path(__file__).resolve().parents[1]


class RunningCounter:
    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.state_file = root / "data" / "counter.json"
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "src.trader.sim_counter",
                "--root",
                str(self.root),
                "--port",
                str(self.port),
                "--state-file",
                str(self.state_file),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        deadline = time.monotonic() + 8
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(f"仿真柜台启动失败：{output}")
            try:
                data = self.admin("status")
                if data["service"] == SERVICE_NAME:
                    return
            except (OSError, RuntimeError, ValueError) as exc:
                last_error = exc
                time.sleep(0.03)
        raise RuntimeError(f"仿真柜台启动超时：{last_error}")

    def admin(self, action: str, payload: dict | None = None):
        return request_once(action, payload, port=self.port, timeout=2)

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.admin("stop")
            self.process.wait(timeout=4)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            self.process.terminate()
            self.process.wait(timeout=4)

    def restart(self) -> None:
        self.stop()
        self.start()


@pytest.fixture
def counter(tmp_path: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    running = RunningCounter(tmp_path, port)
    running.start()
    try:
        yield running
    finally:
        running.stop()


def settings() -> CtpSettings:
    return CtpSettings(
        td_front=ta.SELF_TEST_FRONT,
        td_fronts=(ta.SELF_TEST_FRONT,),
        md_front=ta.SELF_TEST_FRONT,
        broker_id="9999",
        user_id=ta.SELF_TEST_INVESTOR_ID,
        password="",
        app_id="self-test",
        auth_code="",
        auth_code_env="",
        flow_dir="unused",
        project_root=str(ROOT),
        account_id=ta.SELF_TEST_ACCOUNT_ID,
        account_kind="sim",
        desk_session=True,
        login_timeout_sec=2,
        order_wait_sec=0.25,
        cancel_wait_sec=0.25,
    )


def gateway(counter: RunningCounter) -> CtpGateway:
    return CtpGateway(settings(), SimCounterClient(port=counter.port))


def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_real_process_login_open_close_and_accounting(counter: RunningCounter) -> None:
    gate = gateway(counter)
    try:
        ok, message = gate.connect()
        assert ok, message
        assert gate.session.ready
        opened = gate.submit(symbol="fu2701", direction="0", offset="0", volume=2, price=2500)
        assert opened.status == "filled"
        assert gate.refresh()
        view = gate.positions["fu2701"]
        assert view.long_today == 2
        assert gate.available < 1_000_000

        closed = gate.submit(symbol="fu2701", direction="1", offset="3", volume=1, price=2510)
        assert closed.status == "filled"
        assert gate.refresh()
        assert gate.positions["fu2701"].long_today == 1
        assert gate.account["close_profit"] == pytest.approx(100.0)
        assert gate.account["balance"] == pytest.approx(1_000_100.0)
        assert len(gate.trades) == 2
    finally:
        gate.close()


def test_working_partial_cancel_and_restart_recovery(counter: RunningCounter) -> None:
    counter.admin("set_scenario", {"scenario": "working"})
    gate = gateway(counter)
    assert gate.connect()[0]
    working = gate.submit(symbol="fu2701", direction="0", offset="0", volume=2, price=2500)
    assert working.status == "submitted"
    assert gate.cancel(working.order_ref)[0]
    wait_for(lambda: gate.table.get(working.order_ref).status == "cancelled")
    gate.close()

    counter.admin("set_scenario", {"scenario": "partial"})
    gate = gateway(counter)
    assert gate.connect()[0]
    partial = gate.submit(symbol="fu2701", direction="0", offset="0", volume=4, price=2500)
    wait_for(lambda: partial.status == "partial" and partial.filled == 2)
    assert gate.cancel(partial.order_ref)[0]
    wait_for(lambda: partial.status == "cancelled")
    gate.close()

    # 新客户端重连后先查今日委托，柜台持仓与成交仍可恢复。
    restored = gateway(counter)
    try:
        assert restored.connect()[0]
        assert restored.table.get(partial.order_ref).status == "cancelled"
        assert restored.refresh()
        assert restored.positions["fu2701"].long_today == 2
        assert restored.query_trade_history()[0]
        assert len(restored.trades) == 1
    finally:
        restored.close()


def test_reject_silent_unknown_and_insufficient_position(counter: RunningCounter) -> None:
    gate = gateway(counter)
    try:
        assert gate.connect()[0]
        counter.admin("set_scenario", {"scenario": "reject"})
        rejected = gate.submit(symbol="fu2701", direction="0", offset="0", volume=1, price=2500)
        assert rejected.status == "rejected"
        assert "柜台拒单" in rejected.message

        counter.admin("set_scenario", {"scenario": "silent"})
        silent = gate.submit(symbol="fu2701", direction="0", offset="0", volume=1, price=2500)
        assert silent.status == "pending"
        assert silent.filled == 0
        assert gate.query_working_orders()[0]
        wait_for(lambda: gate.table.get(silent.order_ref).status == "submitted")
        assert gate.cancel(silent.order_ref)[0]

        counter.admin("set_scenario", {"scenario": "immediate"})
        unknown = gate.submit(symbol="xx9999", direction="0", offset="0", volume=1, price=100)
        assert unknown.status == "rejected"
        assert "找不到合约" in unknown.message
        no_position = gate.submit(symbol="fu2701", direction="1", offset="3", volume=1, price=2500)
        assert no_position.status == "rejected"
        assert "持仓不足" in no_position.message
    finally:
        gate.close()


def test_counter_process_restart_keeps_state(counter: RunningCounter) -> None:
    gate = gateway(counter)
    assert gate.connect()[0]
    order = gate.submit(symbol="fu2701", direction="0", offset="0", volume=1, price=2500)
    assert order.status == "filled"
    gate.close()

    counter.restart()
    restored = gateway(counter)
    try:
        assert restored.connect()[0]
        assert restored.table.get(order.order_ref).status == "filled"
        assert restored.refresh()
        assert restored.positions["fu2701"].long_today == 1
    finally:
        restored.close()


def test_local_front_cannot_be_used_by_normal_or_live_account() -> None:
    normal = settings()
    normal.account_id = "acc_other_sim"
    gate = CtpGateway(normal)
    ok, message = gate.connect()
    assert not ok
    assert "只能用于内置" in message
    assert gate.client is None

    live = settings()
    live.account_kind = "live"
    gate = CtpGateway(live)
    ok, message = gate.connect()
    assert not ok
    assert "只能用于内置" in message
    assert gate.client is None


def test_production_trader_open_stop_close_and_local_preflight(counter: RunningCounter) -> None:
    """按钮/止损共用的 CtpTrader 原样走到独立柜台，本机拦截则不发到柜台。"""
    gate = gateway(counter)
    trader = CtpTrader({"dry_run": False, "_project_root": str(ROOT)}, gate)
    try:
        assert trader.connect()[0]
        opened = trader.place_order(ctp_order("fu2701", "buy_open", 1, 2500, remark="面板买开"))
        assert opened.ok, opened.message

        gate.trading_paused = True
        before = len(counter.admin("status")["orders"])
        paused = trader.place_order(ctp_order("fu2701", "buy_open", 1, 2500))
        assert not paused.ok
        assert "暂停" in paused.message
        assert len(counter.admin("status")["orders"]) == before
        gate.trading_paused = False

        stopped = trader.place_order(
            ctp_order(
                "fu2701",
                "close",
                1,
                2490,
                position_side="long",
                stop_price=2490,
                remark="止损",
            )
        )
        assert stopped.ok, stopped.message
        assert gate.refresh()
        remaining = gate.positions.get("fu2701")
        assert remaining is None or remaining.long_lots == 0
    finally:
        trader.shutdown()


def test_production_trader_blocks_oversized_order_before_counter(
    counter: RunningCounter,
) -> None:
    """配置页单笔 2 手、下 3 手：在本机拦住，柜台委托数不增加。"""
    limited = settings()
    limited.max_order_volume = 2
    gate = CtpGateway(limited, SimCounterClient(port=counter.port))
    trader = CtpTrader({"dry_run": False, "_project_root": str(ROOT)}, gate)
    try:
        assert trader.connect()[0]
        before = len(counter.admin("status")["orders"])
        blocked = trader.place_order(ctp_order("fu2701", "buy_open", 3, 2500))
        assert not blocked.ok
        assert "超过单笔最大 2 手" in blocked.message
        assert len(counter.admin("status")["orders"]) == before

        # 交易台登录后改小上限：传进来的 max_volume 只降不升。
        trader.settings.max_order_volume = 20
        reduced = trader.place_order(ctp_order("fu2701", "buy_open", 3, 2500), max_volume=2)
        assert not reduced.ok
        assert "超过单笔最大 2 手" in reduced.message
        assert len(counter.admin("status")["orders"]) == before

        # 传一个更大的数不能绕过登录时的本机上限。
        trader.settings.max_order_volume = 2
        raised = trader.place_order(ctp_order("fu2701", "buy_open", 3, 2500), max_volume=100)
        assert not raised.ok
        assert "超过单笔最大 2 手" in raised.message
        assert len(counter.admin("status")["orders"]) == before
    finally:
        trader.shutdown()


def test_disconnect_scenario_is_visible_and_orders_recover(counter: RunningCounter) -> None:
    gate = gateway(counter)
    try:
        assert gate.connect()[0]
        counter.admin("set_scenario", {"scenario": "disconnect"})
        disconnected = gate.submit(symbol="fu2701", direction="0", offset="0", volume=1, price=2500)
        assert disconnected.status == "pending"
        wait_for(lambda: not gate.session.ready)
        assert gate.session.phase == "disconnected"
        assert "关闭" in gate.session.last_error
        ref = disconnected.order_ref
    finally:
        gate.close()

    restored = gateway(counter)
    try:
        assert restored.connect()[0]
        assert restored.table.get(ref).status == "submitted"
        assert restored.cancel(ref)[0]
        wait_for(lambda: restored.table.get(ref).status == "cancelled")
    finally:
        restored.close()


def test_ui_controller_reuses_existing_and_only_stops_owned_process(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    owner = SimCounterController(tmp_path, port=port)
    observer = SimCounterController(tmp_path, port=port)
    try:
        assert owner.start()["service"] == SERVICE_NAME
        process = owner.process
        assert process is not None and process.poll() is None

        assert observer.start()["running"] is True
        assert observer.process is None
        observer.close_owned()
        assert process.poll() is None

        owner.close_owned()
        assert process.wait(timeout=4) == 0
        assert owner.process is None
    finally:
        observer.close_owned()
        owner.close_owned()
