"""第二批 P1（账本/投影/一致性）回归：connect 只读拆分、账号漂移比对、
positions.book_id 语义与自测过滤、risk_state 旧键迁移、outbox 按事件账本消费
与失败告警、发单异常/风控同步失败熔断、夜盘名额键改用期货交易日。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.account import outbox
from src.account.signal_book import (
    apply_outbox_event,
    load_book,
    load_named_book,
    new_book,
    save_book,
    save_named_book,
)
from src.account.sqlite_ledger import _book_id
from src.account.sqlite_migrate import SCHEMA_VERSION, migrate
from src.account.trading_db import (
    connect,
    connect_readonly,
    diff_accounts,
)
from src.account.registry import AccountRegistry
from src.domain.trading_account import (
    BrokerProfileTemplate,
    SELF_TEST_ACCOUNT_ID,
    SELF_TEST_BOOK_ID,
    TradingAccount,
)
from src.risk.portfolio import PortfolioRisk
from src.risk.state_store import JsonRiskStateStore, migrate_legacy_keys
from src.scheduler.order_state import OrderIntentMixin
from src.scheduler.runner import TradingBot
from src.strategy.base import StrategyBase
from src.strategy.types import StrategyAction, StrategyDecision


# --------------------------------------------------------------------------- #
# 共享构造器
# --------------------------------------------------------------------------- #
def _profile(profile_id: str = "huatai_sim") -> BrokerProfileTemplate:
    return BrokerProfileTemplate(
        profile_id=profile_id,
        label="华泰仿真",
        broker="华泰期货",
        environment="sim",
        broker_id="8080",
        td_fronts=["tcp://sim:27226"],
        md_fronts=["tcp://sim:27236"],
        password_env="CTP_PASSWORD",
        auth_code_env="CTP_AUTH_CODE",
        password_ref="secret://ctp/sim/password",
        auth_code_ref="secret://ctp/auth",
        auto_cancel_on_timeout=False,
    )


def _account(account_id: str = "acc_huatai_sim_01", book_id: str = "ctp_sim") -> TradingAccount:
    return TradingAccount(
        account_id=account_id,
        display_name="华泰仿真-28077",
        broker="华泰期货",
        channel="ctp",
        environment="sim",
        status="enabled",
        profile_id="huatai_sim",
        investor_id="28077",
        book_id=book_id,
        created_at="2026-11-20T10:00:00",
        updated_at="2026-11-20T10:00:00",
    )


def _rows(db: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def _user_version(db: Path) -> int:
    return int(_rows(db, "PRAGMA user_version")[0][0])


# =========================================================================== #
# s1/s2：connect 只读拆分 + schema 版本迁移 + 账号投影漂移比对
# =========================================================================== #
def test_connect_sets_schema_version_once_and_repeated_opens_are_noop(tmp_path: Path) -> None:
    """写连接只在 user_version 不足时跑迁移；重复打开不重复 DDL、不报错。"""
    db = tmp_path / "data" / "trading.db"

    with closing(connect(db)) as conn:
        pass
    assert _user_version(db) == SCHEMA_VERSION
    assert SCHEMA_VERSION == 1

    # 再开很多次：版本不变，也不抛（init_schema 命中 >= 直接返回）。
    for _ in range(100):
        with closing(connect(db)) as conn:
            conn.execute("SELECT 1")
    assert _user_version(db) == 1


def test_connect_readonly_returns_none_when_missing_and_cannot_write(tmp_path: Path) -> None:
    """只读连接不建库、不写 WAL；文件缺失返回 None，已有库不能写。"""
    missing = tmp_path / "data" / "ghost.db"
    assert connect_readonly(missing) is None
    assert not missing.exists()  # 没被偷偷建出来

    db = tmp_path / "data" / "trading.db"
    with closing(connect(db)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    ro = connect_readonly(db)
    assert ro is not None
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
    ro.close()


def test_diff_accounts_is_clean_after_migrate(tmp_path: Path) -> None:
    """迁移后 YAML 与 SQLite 投影一致，启动漂移比对返回空。"""
    AccountRegistry(accounts=[_account()], profiles=[_profile()]).save(tmp_path)
    db = tmp_path / "data" / "trading.db"
    migrate(tmp_path, db)

    assert diff_accounts(tmp_path) == []


def test_diff_accounts_detects_missing_mismatched_and_absent_db(tmp_path: Path) -> None:
    AccountRegistry(accounts=[_account()], profiles=[_profile()]).save(tmp_path)
    db = tmp_path / "data" / "trading.db"
    migrate(tmp_path, db)

    # 库压根没有 → 提示需要迁移。
    absent = tmp_path / "no_db"
    assert any("不存在" in d for d in diff_accounts(absent))

    # 改掉库里的 book_id → 漂移。
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE accounts SET book_id = ? WHERE account_id = ?", ("wrong", "acc_huatai_sim_01"))
    diffs = diff_accounts(tmp_path)
    assert any("book_id" in d and "不一致" in d for d in diffs)

    # 删掉账号行 → 缺失。
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM accounts WHERE account_id = ?", ("acc_huatai_sim_01",))
    diffs = diff_accounts(tmp_path)
    assert any("缺失" in d and "acc_huatai_sim_01" in d for d in diffs)


# =========================================================================== #
# s3：positions.book_id 走注册表；组合风控过滤自测账本
# =========================================================================== #
def _write_accounts_yaml(root: Path, accounts: list[dict]) -> None:
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "accounts.yaml").write_text(
        yaml.safe_dump({"accounts": accounts}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_book_id_resolves_selftest_account_to_self_test_book(tmp_path: Path) -> None:
    """自测账 account_id=acc_program_selftest 必须解析成 book_id=self_test，
    否则组合风控按 SELF_TEST_BOOK_ID 过滤会漏掉自测成交。"""
    _write_accounts_yaml(
        tmp_path,
        [
            {
                "account_id": SELF_TEST_ACCOUNT_ID,
                "book_id": SELF_TEST_BOOK_ID,
                "display_name": "程序自测",
                "channel": "sim",
                "environment": "sim",
            },
            {
                "account_id": "acc_huatai_live_01",
                "book_id": "acc_huatai_live_01",
                "display_name": "华泰真实",
                "channel": "ctp",
                "environment": "live",
            },
        ],
    )

    # 没有显式 book_id 时走注册表解析。
    assert _book_id(tmp_path, {"account_id": SELF_TEST_ACCOUNT_ID}) == SELF_TEST_BOOK_ID
    # 显式 book_id 优先（兼容老账本自带字段）。
    assert _book_id(tmp_path, {"account_id": SELF_TEST_ACCOUNT_ID, "book_id": "explicit"}) == "explicit"


def test_portfolio_excludes_self_test_positions(tmp_path: Path) -> None:
    db = tmp_path / "data" / "trading.db"
    with closing(connect(db)) as conn, conn:
        conn.executemany(
            "INSERT INTO positions (book_id, symbol, side, volume, entry_price, opened_at, stop_price, updated_at)"
            " VALUES (?, ?, ?, ?, 0, '', 0, ?)",
            [
                (SELF_TEST_BOOK_ID, "fu2701", "long", 5, "t"),
                ("acc_huatai_live_01", "fu2701", "short", 2, "t"),
                ("acc_huatai_live_01", "bu2612", "long", 1, "t"),
            ],
        )

    snap = PortfolioRisk._read(tmp_path)
    books = snap.books
    assert SELF_TEST_BOOK_ID not in books
    assert "acc_huatai_live_01" in books
    assert books["acc_huatai_live_01"]["fu2701"] == -2
    assert books["acc_huatai_live_01"]["bu2612"] == 1
    assert snap.net_lots("fu2701") == -2  # 自测的 5 手不计入组合敞口


# =========================================================================== #
# s4：risk_state 旧键迁移与清理
# =========================================================================== #
def _write_audit(root: Path, events: list[dict]) -> None:
    path = root / "data" / "accounts_audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )


def _risk_file(root: Path, key: str) -> Path:
    return root / "data" / f"risk_state_{key}.json"


def test_migrate_legacy_keys_copies_when_new_missing(tmp_path: Path) -> None:
    _write_audit(tmp_path, [{"action": "book_migrate", "old_book": "ctp_live", "account_id": "acc_live"}])
    JsonRiskStateStore(tmp_path).save("ctp_live", {"account_id": "ctp_live", "daily_pnl": 12, "positions": {}})

    result = migrate_legacy_keys(tmp_path)
    assert result["migrated"] == ["ctp_live"]
    assert not _risk_file(tmp_path, "ctp_live").exists()
    new = JsonRiskStateStore(tmp_path).load("acc_live")
    assert new is not None
    assert new["account_id"] == "acc_live"
    assert new["daily_pnl"] == 12


def test_migrate_legacy_keys_deletes_stale_duplicate(tmp_path: Path) -> None:
    _write_audit(tmp_path, [{"action": "book_migrate", "old_book": "ctp_live", "account_id": "acc_live"}])
    # 新旧内容除 account_id/updated_at 外一致 → 旧键是陈旧副本，应清理。
    JsonRiskStateStore(tmp_path).save("ctp_live", {"account_id": "ctp_live", "daily_pnl": 5})
    JsonRiskStateStore(tmp_path).save("acc_live", {"account_id": "acc_live", "daily_pnl": 5})

    result = migrate_legacy_keys(tmp_path)
    assert "ctp_live" in result["deleted"]
    assert not _risk_file(tmp_path, "ctp_live").exists()
    assert JsonRiskStateStore(tmp_path).load("acc_live") is not None


def test_migrate_legacy_keys_conflict_halts_new_key_and_leaves_old(tmp_path: Path) -> None:
    _write_audit(tmp_path, [{"action": "book_migrate", "old_book": "ctp_live", "account_id": "acc_live"}])
    JsonRiskStateStore(tmp_path).save("ctp_live", {"account_id": "ctp_live", "daily_pnl": 5})
    JsonRiskStateStore(tmp_path).save("acc_live", {"account_id": "acc_live", "daily_pnl": 999})

    with patch("src.ops.alerts.emit_alert") as alert:
        result = migrate_legacy_keys(tmp_path)

    assert result["conflicts"] == ["ctp_live→acc_live"]
    # 冲突不猜合并：旧键保留，新键写熔断等人工核对。
    assert _risk_file(tmp_path, "ctp_live").exists()
    forced = JsonRiskStateStore(tmp_path).load("acc_live")
    assert forced is not None
    assert forced["halted"] is True
    assert forced["halt_kind"] == "risk_state_migration_conflict"
    assert any(c.args[0] == "risk_state_migration_conflict" for c in alert.call_args_list)


def test_migrate_legacy_keys_is_idempotent(tmp_path: Path) -> None:
    _write_audit(tmp_path, [{"action": "book_migrate", "old_book": "ctp_live", "account_id": "acc_live"}])
    JsonRiskStateStore(tmp_path).save("ctp_live", {"account_id": "ctp_live", "daily_pnl": 1})

    migrate_legacy_keys(tmp_path)
    again = migrate_legacy_keys(tmp_path)
    assert again == {"migrated": [], "deleted": [], "conflicts": [], "orphans": []}


# =========================================================================== #
# s5/Q3：outbox 按事件账本消费 + 失败告警
# =========================================================================== #
def _set_active(root: Path, account_id: str) -> None:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "active_account.json").write_text(
        json.dumps({"account_id": account_id}), encoding="utf-8"
    )


def test_apply_outbox_event_writes_to_event_book_not_active(tmp_path: Path) -> None:
    """事件自带 book_id 指向另一本账时，补记到那本，不写当前选中账。"""
    _write_accounts_yaml(
        tmp_path,
        [
            {"account_id": "acc_a", "book_id": "book_a", "display_name": "A", "channel": "gui", "environment": "live"},
            {"account_id": "acc_b", "book_id": "book_b", "display_name": "B", "channel": "gui", "environment": "live"},
        ],
    )
    _set_active(tmp_path, "acc_a")
    save_book(tmp_path, new_book(10_000, version="test"))          # 当前账 book_a
    save_named_book(tmp_path, "book_b", new_book(10_000, version="test"))

    payload = {
        "symbol": "bu2601", "offset": "开仓", "direction": "多",
        "price": 3000.0, "volume": 2, "multiplier": 10.0, "commission": 5.0,
        "fill_key": "cross:1", "book_id": "book_b",
    }
    apply_outbox_event(tmp_path, {"type": outbox.ORDER_FILLED, "book_id": "book_b", "payload": payload})

    target = load_named_book(tmp_path, "book_b")
    active = load_book(tmp_path)
    assert target is not None and [r["fill_key"] for r in target["trades"]] == ["cross:1"]
    assert active is not None and active["trades"] == []


def test_apply_outbox_event_maps_legacy_book_via_audit(tmp_path: Path) -> None:
    """迁移前落下的旧事件记的是旧账本名，按审计日志翻到当前账号再补记。"""
    _write_accounts_yaml(
        tmp_path,
        [{"account_id": "acc_x", "book_id": "acc_x", "display_name": "X", "channel": "ctp", "environment": "live"}],
    )
    _write_audit(tmp_path, [{"action": "book_migrate", "old_book": "ctp_live", "account_id": "acc_x"}])
    save_named_book(tmp_path, "acc_x", new_book(10_000, version="test"))

    apply_outbox_event(
        tmp_path,
        {
            "type": outbox.ORDER_FILLED,
            "book_id": "ctp_live",
            "payload": {
                "symbol": "fu2701", "offset": "平仓", "direction": "空",
                "price": 3890.0, "volume": 1, "multiplier": 10.0, "commission": 2.0,
                "fill_key": "legacy:1", "book_id": "ctp_live",
            },
        },
    )
    target = load_named_book(tmp_path, "acc_x")
    assert target is not None
    assert [r["fill_key"] for r in target["trades"]] == ["legacy:1"]


def test_failed_outbox_events_are_reported_on_startup(tmp_path: Path) -> None:
    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="order:x:1", status="ready")

    def _boom(_event: dict) -> None:
        raise RuntimeError("投影失败")

    for _ in range(outbox.MAX_ATTEMPTS):
        outbox.process_ready(tmp_path, _boom)
    assert outbox.ready_events(tmp_path) == []
    assert len(outbox._rows(tmp_path, "failed", 10)) == 1

    with patch("src.ops.alerts.emit_alert") as alert:
        count = outbox.report_failed(tmp_path)
    assert count == 1
    assert any(c.args[0] == "outbox_failed_backlog" for c in alert.call_args_list)


# =========================================================================== #
# Q4：发单阶段异常 → 熔断停开新仓 + 人工确认 + 防重复报单
# =========================================================================== #
def test_halt_on_unknown_dispatch_pauses_and_alerts() -> None:
    """委托结果未知时停开新仓、记熔断、告警；保护性平仓不受影响（set_orders_paused 语义）。"""
    bot = TradingBot.__new__(TradingBot)

    class _Risk:
        halted = False
        halt_kind = ""
        halt_reason = ""

        def set_halt(self, kind: str, reason: str) -> None:
            self.halted = True
            self.halt_kind = kind
            self.halt_reason = reason

        def save_state(self) -> None:
            pass

    bot.risk = _Risk()
    paused: list[bool] = []
    bot.set_orders_paused = lambda v: paused.append(bool(v))

    with patch("src.ops.alerts.emit_alert") as alert:
        bot._halt_on_unknown_dispatch(RuntimeError("柜台连接在发单后断开"))

    assert bot.risk.halted is True
    assert bot.risk.halt_kind == "order_dispatch_unknown"
    assert paused == [True]
    assert any(c.args[0] == "order_dispatch_unknown" for c in alert.call_args_list)


class _Strategy(StrategyBase):
    def __init__(self) -> None:
        super().__init__()
        self.positions: dict[str, object] = {}


def _scan_cfg(root: Path) -> dict:
    return {
        "dry_run": True,
        "trade": {"symbol": "fu2701", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["fu2701"],
            "catch_up_on_start": {"enabled": False},
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000},
        "data": {},
        "_project_root": str(root),
    }


def test_scan_symbol_keeps_signal_mark_when_dispatch_started_raises(tmp_path: Path) -> None:
    """已经点出去后异常：信号标记不能放，否则下一轮重复报单；并触发熔断。"""
    from datetime import timedelta

    import pandas as pd

    bot = TradingBot(_scan_cfg(tmp_path))
    end = datetime.now() - timedelta(minutes=2)
    index = pd.DatetimeIndex([end - timedelta(minutes=5 * (39 - i)) for i in range(40)])
    close = pd.Series(100.0, index=index)
    bars = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10})
    bar_id = str(bars.index[-1])
    bot.strategy.set_signaled_bar("fu2701", bar_id)
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN, "fu2701", "假开仓",
        entry_price=100.0, stop_price=95.0, meta={"bar_id": bar_id},
    )
    bot._order_dispatch_started = True

    with patch.object(bot.kline, "fetch", return_value=bars), \
            patch.object(bot.kline, "latest_price", return_value=0.0), \
            patch.object(bot, "_evaluate_symbol", return_value=decision), \
            patch.object(bot, "_execute", side_effect=RuntimeError("发单后断线")), \
            patch("src.ops.alerts.emit_alert"):
        with pytest.raises(RuntimeError):
            bot.scan_symbol("fu2701")

    # 标记保留：这根不能再重试，避免重复开仓。
    assert bot.strategy.get_signaled_bar("fu2701") == bar_id
    assert bot.risk.halted is True
    assert bot.risk.halt_kind == "order_dispatch_unknown"


def test_scan_symbol_releases_mark_when_pre_dispatch_raises(tmp_path: Path) -> None:
    """还没点出去就异常（拼参数错等）：放开信号标记，下一轮还能重试，不熔断。"""
    from datetime import timedelta

    import pandas as pd

    bot = TradingBot(_scan_cfg(tmp_path))
    end = datetime.now() - timedelta(minutes=2)
    index = pd.DatetimeIndex([end - timedelta(minutes=5 * (39 - i)) for i in range(40)])
    close = pd.Series(100.0, index=index)
    bars = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10})
    bar_id = str(bars.index[-1])
    bot.strategy.set_signaled_bar("fu2701", bar_id)
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN, "fu2701", "假开仓",
        entry_price=100.0, stop_price=95.0, meta={"bar_id": bar_id},
    )
    bot._order_dispatch_started = False

    with patch.object(bot.kline, "fetch", return_value=bars), \
            patch.object(bot.kline, "latest_price", return_value=0.0), \
            patch.object(bot, "_evaluate_symbol", return_value=decision), \
            patch.object(bot, "_execute", side_effect=RuntimeError("下单前拼参失败")):
        with pytest.raises(RuntimeError):
            bot.scan_symbol("fu2701")

    assert bot.strategy.get_signaled_bar("fu2701") == ""
    assert bot.risk.halted is False


def test_confirm_positions_reviewed_clears_dispatch_and_sync_halt() -> None:
    """人工在持仓核对页确认后，发单未知/风控同步失败熔断可解除；非此类熔断不动。"""

    class _Risk:
        def __init__(self) -> None:
            self.halted = True
            self.halt_kind = "order_dispatch_unknown"
            self.halt_reason = "x"

        def clear_halt(self) -> None:
            self.halted = False
            self.halt_kind = ""

    class _Stub(OrderIntentMixin):
        pass

    stub = _Stub()
    stub._pending_intent = None
    stub.risk = _Risk()
    stub._persist_state = lambda: None
    stub._record_event = lambda *a, **k: None
    paused: list[bool] = []
    stub.set_orders_paused = lambda v: paused.append(bool(v))

    ok, msg = stub.confirm_positions_reviewed()
    assert ok is True
    assert stub.risk.halted is False
    assert paused == [False]

    # 非人工可解除的熔断（如日亏）不能从这里误解除。
    stub.risk.halted = True
    stub.risk.halt_kind = "daily_loss"
    ok2, _ = stub.confirm_positions_reviewed()
    assert ok2 is False
    assert stub.risk.halted is True


# =========================================================================== #
# Q5：风控净仓同步失败 → 停开新仓 + 告警
# =========================================================================== #
def test_risk_sync_failure_halts_and_blocks_new_opens(tmp_path: Path) -> None:
    """risk.on_order_filled 抛异常后停开新仓、记熔断、告警；成交事实仍落库不回滚。"""
    bot = TradingBot.__new__(TradingBot)
    bot.cfg = {"dry_run": False, "risk": {}, "_project_root": str(tmp_path)}
    bot.root = tmp_path
    bot._ledger_failed = False
    bot._position_for = lambda symbol: None
    bot._record_signal_account = lambda *a, **k: 10_000.0
    bot._contract_multiplier = lambda symbol: 10
    bot._record_event = lambda *a, **k: None
    bot._persist_state = lambda: None
    bot._uses_ctp_broker_funds = lambda: False
    bot.strategy = _Strategy()
    bot.strategy.on_filled = lambda *a, **k: None

    class _Risk:
        def __init__(self) -> None:
            self.halted = False
            self.halt_kind = ""

        def set_halt(self, kind: str, reason: str) -> None:
            self.halted = True
            self.halt_kind = kind

        def save_state(self) -> None:
            pass

        def on_order_filled(self, *a, **k) -> None:
            raise RuntimeError("风控服务挂了")

    class _Trader:
        def __init__(self) -> None:
            self.marked = False

        def mark_reconcile_unknown(self, reason: str) -> None:
            self.marked = True

        def blocks_new_opens(self) -> bool:
            return self.marked

    bot.risk = _Risk()
    bot.trader = _Trader()

    with patch("src.ops.alerts.emit_alert") as alert:
        bot._apply_fill_accounting(
            "fu2701",
            StrategyDecision(StrategyAction.BUY_OPEN, "fu2701", "测试", entry_price=100),
            1, 100, 100,
        )

    assert bot.risk.halted is True
    assert bot.risk.halt_kind == "risk_sync_failed"
    assert bot.trader.marked is True
    assert bot.trader.blocks_new_opens() is True
    assert any(c.args[0] == "risk_sync_failed" for c in alert.call_args_list)


# =========================================================================== #
# C2：夜盘名额键改用期货交易日
# =========================================================================== #
def test_session_key_night_shares_across_midnight_and_differs_from_day() -> None:
    """21:00 与次日 00:30 同属一个夜盘名额键（都用 futures_trading_day 锚 21:00）；
    09:00 日盘是另一个键。用周一/周二避免周末顺延干扰。"""
    bot = TradingBot.__new__(TradingBot)
    # 2026-01-05 是周一；21:00 后归入周二交易日，与周二 00:30 同键。
    night_mon = bot._session_key_from_timestamp("2026-01-05 21:00:00")
    night_tue = bot._session_key_from_timestamp("2026-01-06 00:30:00")
    day_tue = bot._session_key_from_timestamp("2026-01-06 09:00:00")

    assert night_mon == night_tue
    assert night_mon.endswith(" 21:00")
    assert day_tue.endswith(" 09:00")
    assert day_tue != night_mon
    # 日期部分都应是周二（futures_trading_day），不是开仓的自然日周一。
    assert night_mon.startswith("2026-01-06 ")


def test_session_key_handles_empty_and_invalid() -> None:
    bot = TradingBot.__new__(TradingBot)
    assert bot._session_key_from_timestamp("") == ""
    assert bot._session_key_from_timestamp("not-a-time") == ""
