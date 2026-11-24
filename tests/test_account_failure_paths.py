"""账号模块失败/迁移分支的直选用例（测试报告 10.3 补测）。

只覆盖 registry / outbox / ctp_sync 的错误处理与迁移分支：
审计轮转、损坏文件、增删改状态流转、配置回写、账本归档、发件箱重试与调度、
柜台资金空值/坏值对账。全部使用临时目录，无网络、无 GUI。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.account import outbox
from src.account import registry as reg
from src.domain import trading_account as ta
from src.domain.trading_account import BrokerProfileTemplate, TradingAccount
from src.utils.secret_store import SecretStore


class _FakeCipher:
    def protect(self, text: str) -> str:
        return "enc:" + text

    def unprotect(self, token: str) -> str:
        return token[4:]


def _store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "config" / "secrets.json", cipher=_FakeCipher())


def _profile(profile_id: str = "conn_sim", environment: str = "sim", **extra) -> BrokerProfileTemplate:
    data = {
        "profile_id": profile_id,
        "label": profile_id,
        "environment": environment,
        "td_fronts": ["tcp://td:1"],
        "md_fronts": ["tcp://md:2"],
        **extra,
    }
    return BrokerProfileTemplate.from_mapping(data)


def _account(account_id: str = "acc_a_01", **extra) -> TradingAccount:
    data = {
        "account_id": account_id,
        "display_name": account_id,
        "channel": "ctp",
        "environment": "sim",
        "status": "enabled",
        "profile_id": "conn_sim",
        "investor_id": "1",
        "book_id": account_id,
        "password_ref": "secret://ctp/sim/password",
        **extra,
    }
    return TradingAccount.from_mapping(data)


def _registry(**extra) -> reg.AccountRegistry:
    return reg.AccountRegistry(profiles=[_profile()], accounts=[_account()], **extra)


def _self_test_account() -> TradingAccount:
    return TradingAccount(
        account_id=ta.SELF_TEST_ACCOUNT_ID,
        display_name="程序自测",
        channel=ta.CHANNEL_CTP,
        environment=ta.ENV_SIM,
        status=ta.STATUS_ENABLED,
        profile_id=ta.SELF_TEST_CONNECTION_ID,
        investor_id=ta.SELF_TEST_INVESTOR_ID,
        book_id=ta.SELF_TEST_BOOK_ID,
    )


# —— registry：审计与解析 ——


def test_audit_rotates_when_file_exceeds_limit(tmp_path: Path, monkeypatch) -> None:
    audit = tmp_path / reg.AUDIT_REL
    audit.parent.mkdir(parents=True)
    audit.write_text("x" * 500, encoding="utf-8")
    monkeypatch.setattr(reg, "AUDIT_MAX_BYTES", 10)

    reg.record_audit(tmp_path, "probe", account_id="acc_a_01")

    backup = audit.with_name(audit.name + ".1")
    assert backup.exists()
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "probe"


def test_rotate_skips_when_stat_or_replace_fails() -> None:
    class _StatBoom:
        name = "audit.jsonl"

        def exists(self) -> bool:
            return True

        def stat(self) -> None:
            raise OSError("stat")

    reg._rotate_if_needed(_StatBoom(), 1)  # 不抛

    class _ReplaceBoom:
        name = "audit.jsonl"

        def exists(self) -> bool:
            return True

        def stat(self):
            return SimpleNamespace(st_size=999)

        def with_name(self, name: str):
            return SimpleNamespace(unlink=lambda missing_ok=False: None)

        def replace(self, other) -> None:
            raise OSError("replace")

    reg._rotate_if_needed(_ReplaceBoom(), 1)  # 不抛


def test_audit_write_failure_is_swallowed(tmp_path: Path) -> None:
    (tmp_path / "data").write_text("not a directory", encoding="utf-8")
    reg.record_audit(tmp_path, "probe")  # 不抛


def test_split_fronts_accepts_none_sequence_and_separators() -> None:
    assert reg._split_fronts(None) == []
    assert reg._split_fronts(("tcp://a:1", " ")) == ["tcp://a:1"]
    assert reg._split_fronts("tcp://a:1;tcp://b:2；tcp://c:3\ntcp://d:4") == [
        "tcp://a:1",
        "tcp://b:2",
        "tcp://c:3",
        "tcp://d:4",
    ]


def test_load_returns_none_for_broken_or_non_mapping_file(tmp_path: Path) -> None:
    path = tmp_path / reg.REGISTRY_REL
    path.parent.mkdir(parents=True)
    path.write_text("a: [", encoding="utf-8")
    assert reg.AccountRegistry.load(tmp_path) is None

    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert reg.AccountRegistry.load(tmp_path) is None


def test_validate_skips_account_without_book_id() -> None:
    registry = reg.AccountRegistry(profiles=[_profile()], accounts=[_account(book_id="")])
    assert registry.validate() == []


# —— registry：增删改与状态 ——


def test_add_account_creates_new_connection() -> None:
    registry = reg.AccountRegistry()
    account = registry.add_account("仿真一号", "sim", "28077", broker="huatai")

    assert account.account_id == "acc_huatai_sim_01"
    assert account.profile_id == account.account_id
    assert account.status == ta.STATUS_ENABLED
    profile = registry.profile(account.account_id)
    assert profile is not None
    assert profile.environment == ta.ENV_SIM
    assert profile.password_env == "CTP_PASSWORD"
    assert account.password_ref == f"secret://ctp/sim/{account.account_id}/password"
    assert account.auth_code_ref == ""


def test_add_account_with_template_keeps_auth_code_env() -> None:
    registry = reg.AccountRegistry()
    template = BrokerProfileTemplate(
        profile_id="", label="", environment="sim", auth_code_env="CTP_AUTH_CODE"
    )

    account = registry.add_account("仿真二号", "sim", "", template=template)

    assert account.status == ta.STATUS_DISABLED
    assert account.auth_code_ref == f"secret://ctp/sim/{account.account_id}/auth_code"
    assert registry.profile(account.account_id).auth_code_env == "CTP_AUTH_CODE"


def test_add_gui_account_has_no_connection() -> None:
    registry = reg.AccountRegistry()
    account = registry.add_account("界面账号", "live", "20318052", channel="gui")

    assert account.channel == ta.CHANNEL_GUI
    assert account.profile_id == ""
    assert account.book_id == account.account_id


def test_duplicate_account_rejects_unknown_and_self_test() -> None:
    registry = _registry()
    with pytest.raises(KeyError):
        registry.duplicate_account("acc_missing_01")

    registry.accounts.append(_self_test_account())
    with pytest.raises(ValueError, match="只读"):
        registry.duplicate_account(ta.SELF_TEST_ACCOUNT_ID)


def test_duplicate_account_to_live_gets_its_own_connection() -> None:
    registry = _registry()

    dup = registry.duplicate_account("acc_a_01", "真实一号", "20318052", "live")

    assert dup.environment == ta.ENV_LIVE
    assert dup.account_id != "acc_a_01"
    assert dup.profile_id == dup.account_id
    assert registry.profile(dup.profile_id).environment == ta.ENV_LIVE


def test_update_account_writes_fields_and_environment() -> None:
    registry = _registry()

    updated = registry.update_account(
        "acc_a_01",
        display_name="新名",
        investor_id=" 9 ",
        notes="备注",
        max_order_volume=3,
        environment="live",
    )

    assert updated.display_name == "新名"
    assert updated.investor_id == "9"
    assert updated.notes == "备注"
    assert updated.max_order_volume == 3
    assert updated.environment == ta.ENV_LIVE
    assert registry.profile("conn_sim").environment == ta.ENV_LIVE


def test_update_account_errors_and_shared_connection() -> None:
    registry = _registry()
    registry.accounts.append(_account("acc_b_01", book_id="acc_b_01", investor_id="2"))

    registry.update_account("acc_a_01", environment="live")
    # 共用连接不跟着单个账号改环境。
    assert registry.profile("conn_sim").environment == ta.ENV_SIM

    with pytest.raises(KeyError):
        registry.update_account("acc_missing_01", notes="x")

    registry.accounts.append(_self_test_account())
    with pytest.raises(ValueError, match="只读"):
        registry.update_account(ta.SELF_TEST_ACCOUNT_ID, notes="x")


def test_set_status_and_remove_account_paths() -> None:
    registry = _registry()
    assert registry.set_status("acc_a_01", "disabled").status == ta.STATUS_DISABLED
    with pytest.raises(KeyError):
        registry.set_status("acc_missing_01", "enabled")

    assert registry.remove_account("acc_missing_01") is False
    assert registry.remove_account("acc_a_01") is True
    account = registry.account("acc_a_01")
    assert account is not None
    assert account.status == ta.STATUS_ARCHIVED
    assert account.notes.count("保留账本") == 1

    assert registry.remove_account("acc_a_01") is True
    assert account.notes.count("保留账本") == 1

    assert registry.remove_account("acc_a_01", hard=True) is True
    assert registry.account("acc_a_01") is None
    assert registry.profile("conn_sim") is None


def test_self_test_account_cannot_change_status_or_be_removed() -> None:
    registry = reg.AccountRegistry(accounts=[_self_test_account()])
    with pytest.raises(ValueError, match="只读"):
        registry.set_status(ta.SELF_TEST_ACCOUNT_ID, "disabled")
    with pytest.raises(ValueError, match="只读"):
        registry.remove_account(ta.SELF_TEST_ACCOUNT_ID)


# —— registry：物化与回写 ——


def test_materialize_tolerates_malformed_sections(tmp_path: Path) -> None:
    registry = _registry()
    store = _store(tmp_path)

    assert registry.materialize({"trade": "oops"}, store=store) == []

    cfg: dict = {"trade": {"ctp": "oops"}}
    registry.materialize(cfg, store=store)
    assert cfg["trade"]["ctp"]["ctp_profiles"]["conn_sim"]["account_id"] == "acc_a_01"

    cfg2: dict = {"trade": {"ctp": {"ctp_profiles": ["bad"]}}}
    registry.materialize(cfg2, store=store)
    assert isinstance(cfg2["trade"]["ctp"]["ctp_profiles"], dict)


def test_materialize_notes_missing_connection(tmp_path: Path) -> None:
    registry = reg.AccountRegistry(accounts=[_account(profile_id="ghost")])
    notes = registry.materialize({}, store=_store(tmp_path))
    assert "找不到连接" in notes[0]


def test_sync_from_cfg_writes_back_account_and_connection_fields() -> None:
    profile = _profile("conn_sim")
    account = _account()
    registry = reg.AccountRegistry(profiles=[profile], accounts=[account])
    cfg = {
        "trade": {
            "ctp": {
                "ctp_profiles": {
                    "conn_sim": {
                        "user_id": "28088",
                        "kind": "live",
                        "broker_id": "9999",
                        "td_fronts": ["tcp://td1:1", "tcp://td2:2"],
                        "md_front": "tcp://md:2",
                        "password_env": "CTP_LIVE_PASSWORD",
                        "auth_code_env": "CTP_AUTH_CODE",
                        "max_order_volume": "3",
                    }
                }
            }
        }
    }

    notes = registry.sync_from_cfg(cfg)

    assert any("资金号" in item for item in notes)
    assert any("环境" in item for item in notes)
    assert account.investor_id == "28088"
    assert account.environment == ta.ENV_LIVE
    assert profile.environment == ta.ENV_LIVE
    assert profile.broker_id == "9999"
    assert profile.td_fronts == ["tcp://td1:1", "tcp://td2:2"]
    assert profile.md_fronts == ["tcp://md:2"]
    assert profile.password_env == "CTP_LIVE_PASSWORD"
    assert profile.auth_code_env == "CTP_AUTH_CODE"
    assert account.max_order_volume == 3


def test_sync_from_cfg_ignores_bad_items_and_numbers() -> None:
    registry = _registry()
    registry.accounts.append(_self_test_account())

    assert registry.sync_from_cfg({"trade": {"ctp": {"ctp_profiles": {"conn_sim": "bad"}}}}) == []

    registry.sync_from_cfg(
        {"trade": {"ctp": {"ctp_profiles": {"conn_sim": {"max_order_volume": "abc"}}}}}
    )
    assert registry.account("acc_a_01").max_order_volume == 0


def test_sync_from_cfg_keeps_shared_connection_environment() -> None:
    registry = _registry()
    registry.accounts.append(_account("acc_b_01", book_id="acc_b_01", investor_id="2"))

    notes = registry.sync_from_cfg(
        {"trade": {"ctp": {"ctp_profiles": {"conn_sim": {"kind": "live"}}}}}
    )

    assert registry.account("acc_a_01").environment == ta.ENV_SIM
    assert not any("环境" in item for item in notes)


# —— registry：迁移与归档 ——


def test_repoint_active_book_handles_bad_and_legacy_payloads(tmp_path: Path) -> None:
    registry = _registry()
    reg._repoint_active_book(tmp_path, {}, registry)

    path = tmp_path / "data" / "active_account.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    reg._repoint_active_book(tmp_path, {"ctp_sim": "acc_a_01"}, registry)

    path.write_text(json.dumps({"account_id": "acc_a_01"}), encoding="utf-8")
    reg._repoint_active_book(tmp_path, {"ctp_sim": "acc_a_01"}, registry)
    assert json.loads(path.read_text(encoding="utf-8"))["account_id"] == "acc_a_01"

    reg._repoint_active_book(tmp_path, {}, registry)
    assert json.loads(path.read_text(encoding="utf-8"))["account_id"] == "acc_a_01"

    path.write_text(json.dumps({"id": "ctp_sim"}), encoding="utf-8")
    reg._repoint_active_book(tmp_path, {"ctp_sim": "acc_a_01"}, registry)
    assert json.loads(path.read_text(encoding="utf-8"))["account_id"] == "acc_a_01"

    path.write_text(json.dumps("ctp_sim"), encoding="utf-8")
    reg._repoint_active_book(tmp_path, {"ctp_sim": "acc_a_01"}, registry)
    assert json.loads(path.read_text(encoding="utf-8"))["account_id"] == "acc_a_01"


def test_repoint_active_book_swallows_write_error(tmp_path: Path) -> None:
    path = tmp_path / "data" / "active_account.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"id": "ctp_sim"}), encoding="utf-8")
    (path.parent / "active_account.json.tmp").mkdir()

    reg._repoint_active_book(tmp_path, {"ctp_sim": "acc_a_01"}, _registry())

    assert json.loads(path.read_text(encoding="utf-8"))["id"] == "ctp_sim"


def _seed_duplicate_books(root: Path) -> None:
    from src.domain.accounts import book_layout

    legacy = book_layout("ctp_sim")
    mine = book_layout("acc_a_01")
    (root / "data").mkdir(parents=True, exist_ok=True)
    for key in ("book_name", "state_name", "intent_name"):
        (root / "data" / legacy[key]).write_text("same", encoding="utf-8")
        (root / "data" / mine[key]).write_text("same", encoding="utf-8")


def test_archive_duplicate_legacy_books_moves_identical_files(tmp_path: Path) -> None:
    from src.domain.accounts import book_layout

    _seed_duplicate_books(tmp_path)
    archived = reg.archive_duplicate_legacy_books(tmp_path, _registry())

    assert len(archived) == 3
    legacy = book_layout("ctp_sim")
    assert not (tmp_path / "data" / legacy["book_name"]).exists()
    assert (tmp_path / reg.LEGACY_ARCHIVE_REL / legacy["book_name"]).exists()


def test_archive_duplicate_books_avoids_overwriting_archive(tmp_path: Path) -> None:
    from src.domain.accounts import book_layout

    _seed_duplicate_books(tmp_path)
    archive_dir = tmp_path / reg.LEGACY_ARCHIVE_REL
    archive_dir.mkdir(parents=True)
    legacy = book_layout("ctp_sim")
    (archive_dir / legacy["book_name"]).write_text("old", encoding="utf-8")

    archived = reg.archive_duplicate_legacy_books(tmp_path, _registry())

    assert archived
    names = {item.name for item in archive_dir.iterdir()}
    assert legacy["book_name"] in names
    assert any(name != legacy["book_name"] for name in names)


def test_archive_skips_unreadable_files(tmp_path: Path, monkeypatch) -> None:
    _seed_duplicate_books(tmp_path)

    def bad_read(self: Path) -> bytes:
        raise OSError("read")

    monkeypatch.setattr(Path, "read_bytes", bad_read)
    assert reg.archive_duplicate_legacy_books(tmp_path, _registry()) == []


def test_archive_reports_move_failure(tmp_path: Path, monkeypatch) -> None:
    _seed_duplicate_books(tmp_path)

    def bad_move(*args, **kwargs) -> None:
        raise OSError("move")

    monkeypatch.setattr(reg.shutil, "move", bad_move)
    assert reg.archive_duplicate_legacy_books(tmp_path, _registry()) == []


def test_migrate_legacy_books_records_copy_failures(tmp_path: Path, monkeypatch) -> None:
    registry = reg.AccountRegistry(accounts=[_account("acc_a_01", book_id="ctp_sim")])

    def bad_copy(*args, **kwargs) -> bool:
        raise OSError("copy")

    monkeypatch.setattr(reg, "_copy_ledger_path", bad_copy)
    notes = reg.migrate_legacy_books(tmp_path, registry)

    assert notes and "跳过" in notes[0]
    assert registry.account("acc_a_01").book_id == "acc_a_01"


def test_migrate_from_cfg_skips_bad_profile_entries() -> None:
    cfg = {"trade": {"ctp": {"ctp_profiles": {"": {"kind": "sim"}, "bad": "x"}}}}
    registry = reg.migrate_from_cfg(cfg)
    assert registry.accounts == []


def test_migrate_from_cfg_gives_second_sim_account_its_own_book() -> None:
    cfg = {
        "trade": {
            "ctp": {
                "ctp_profiles": {
                    "sim_a": {"kind": "sim", "user_id": "1", "td_front": "tcp://a:1"},
                    "sim_b": {"kind": "sim", "user_id": "2", "td_front": "tcp://b:1"},
                }
            }
        }
    }
    registry = reg.migrate_from_cfg(cfg)
    assert [item.book_id for item in registry.accounts] == ["ctp_sim", "book_acc_sim_b_01"]


# —— registry：注册表入口 ——


def test_backfill_gui_settings_ignores_empty_and_bad_files(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text("accounts: []", encoding="utf-8")
    registry = _registry()

    assert reg._backfill_gui_settings(path, registry, {"account": {}}) is False

    path.write_text("a: [", encoding="utf-8")
    assert reg._backfill_gui_settings(path, registry, {"account": {"user_id": "1"}}) is False

    path.write_text(yaml.safe_dump({"accounts": "not-a-list"}), encoding="utf-8")
    assert reg._backfill_gui_settings(path, registry, {"account": {"user_id": "1"}}) is False


def test_backfill_gui_settings_keeps_existing_secret_ref(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": [{"account_id": "acc_gui_01"}]}), encoding="utf-8")
    registry = reg.AccountRegistry(
        accounts=[
            _account(
                "acc_gui_01",
                channel="gui",
                profile_id="",
                book_id="live",
                password_ref="env://EXISTING",
            )
        ]
    )

    changed = reg._backfill_gui_settings(path, registry, {"account": {"user_id": "1"}})

    assert changed is True
    assert registry.account("acc_gui_01").password_ref == "env://EXISTING"


def test_ensure_registry_returns_none_for_broken_file(tmp_path: Path) -> None:
    path = tmp_path / reg.REGISTRY_REL
    path.parent.mkdir(parents=True)
    path.write_text("a: [", encoding="utf-8")
    assert reg.ensure_registry(tmp_path, {}) is None


def test_ensure_registry_keeps_going_when_autofix_save_fails(
    tmp_path: Path, monkeypatch
) -> None:
    _registry().save(tmp_path)

    def bad_save(self, root) -> None:
        raise OSError("save")

    monkeypatch.setattr(reg.AccountRegistry, "save", bad_save)
    result = reg.ensure_registry(tmp_path, {})
    assert result is not None


def test_first_migration_save_failure_returns_none(tmp_path: Path, monkeypatch) -> None:
    cfg = {
        "trade": {
            "ctp": {
                "ctp_profiles": {
                    "huatai_sim": {"kind": "sim", "user_id": "28077", "td_front": "tcp://a:1"}
                }
            }
        }
    }

    def bad_save(self, root) -> None:
        raise OSError("save")

    monkeypatch.setattr(reg.AccountRegistry, "save", bad_save)
    assert reg.ensure_registry(tmp_path, cfg) is None


def test_apply_registry_falls_back_on_unexpected_error(tmp_path: Path, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(reg, "ensure_registry", boom)
    assert reg.apply_account_registry(tmp_path, {"trade": {}}) == []


def test_sync_registry_from_cfg_persists_and_audits(tmp_path: Path) -> None:
    _registry().save(tmp_path)
    cfg = {"trade": {"ctp": {"ctp_profiles": {"conn_sim": {"user_id": "9"}}}}}

    notes = reg.sync_registry_from_cfg(tmp_path, cfg)

    assert notes
    reloaded = reg.AccountRegistry.load(tmp_path)
    assert reloaded is not None
    assert reloaded.account("acc_a_01").investor_id == "9"
    assert (tmp_path / reg.AUDIT_REL).exists()


def test_sync_registry_from_cfg_without_file_or_on_error(tmp_path: Path, monkeypatch) -> None:
    assert reg.sync_registry_from_cfg(tmp_path, {}) == []

    _registry().save(tmp_path)

    def boom(self, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(reg.AccountRegistry, "sync_from_cfg", boom)
    assert reg.sync_registry_from_cfg(tmp_path, {}) == []


def test_check_cfg_with_registry_reports_conflict(tmp_path: Path) -> None:
    registry = reg.AccountRegistry(
        profiles=[_profile()],
        accounts=[
            _account("acc_a_01", book_id="acc_a_01"),
            _account("acc_b_01", book_id="a_01"),
        ],
    )
    registry.save(tmp_path)

    problems = reg.check_cfg_with_registry(tmp_path, {})
    assert any("共用" in item for item in problems)


def test_check_cfg_with_registry_without_file_or_on_error(
    tmp_path: Path, monkeypatch
) -> None:
    assert reg.check_cfg_with_registry(tmp_path, {}) == []

    _registry().save(tmp_path)

    def boom(self, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(reg.AccountRegistry, "sync_from_cfg", boom)
    assert reg.check_cfg_with_registry(tmp_path, {}) == []


def test_validate_registry_file_reads_disk(tmp_path: Path) -> None:
    assert reg.validate_registry_file(tmp_path) == []
    _registry().save(tmp_path)
    assert reg.validate_registry_file(tmp_path) == []


# —— outbox：写入与读取失败 ——


def test_outbox_write_and_mark_failures_are_swallowed(tmp_path: Path, monkeypatch) -> None:
    def bad_connect(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(outbox, "connect", bad_connect)
    assert outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}) is False
    outbox.mark(tmp_path, "event-x", "ready")


def test_outbox_disabled_short_circuits(tmp_path: Path) -> None:
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("storage:\n  outbox_enabled: false\n", encoding="utf-8")

    assert outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}) is False
    outbox.mark(tmp_path, "event-x", "ready")
    assert outbox.pending_events(tmp_path) == []
    assert outbox.enqueue(tmp_path, "NotAnEvent", {}) is False


def test_outbox_rows_handle_missing_or_broken_connection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(outbox, "connect_readonly", lambda *args, **kwargs: None)
    assert outbox.pending_events(tmp_path) == []

    class _BadConn:
        def execute(self, *args, **kwargs):
            raise RuntimeError("broken")

        def close(self) -> None:
            pass

    monkeypatch.setattr(outbox, "connect_readonly", lambda *args, **kwargs: _BadConn())
    assert outbox.ready_events(tmp_path) == []


def test_outbox_rows_tolerate_broken_payload_json(tmp_path: Path) -> None:
    from contextlib import closing

    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {"ok": 1}, event_id="bad-payload")
    settings = outbox.storage_settings(tmp_path)
    with outbox.DB_LOCK, closing(outbox.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "UPDATE event_outbox SET payload_json = ? WHERE event_id = ?",
            ("{bad", "bad-payload"),
        )

    events = outbox.pending_events(tmp_path)

    assert len(events) == 1
    assert events[0]["payload"] == {}


def test_enqueue_order_stage_maps_sent_and_terminal_reports(tmp_path: Path) -> None:
    from src.account.signal_book import new_book, save_book

    save_book(tmp_path, new_book(100_000, version="test"))

    assert outbox.enqueue_order_stage(tmp_path, {"stage": "order_sent"}) is False
    assert outbox.enqueue_order_stage(tmp_path, {"stage": "started", "trace_id": "t1"}) is False
    assert (
        outbox.enqueue_order_stage(
            tmp_path, {"stage": "broker_report", "trace_id": "t1", "status": "filled"}
        )
        is False
    )
    assert (
        outbox.enqueue_order_stage(
            tmp_path, {"stage": "order_sent", "trace_id": "t1", "symbol": "bu2601"}
        )
        is True
    )
    assert (
        outbox.enqueue_order_stage(
            tmp_path, {"stage": "broker_report", "trace_id": "t2", "status": "rejected"}
        )
        is True
    )
    assert (
        outbox.enqueue_order_stage(
            tmp_path, {"stage": "broker_report", "trace_id": "t3", "status": "cancelled"}
        )
        is True
    )

    assert [item["type"] for item in outbox.ready_events(tmp_path)] == [
        outbox.ORDER_SUBMITTED,
        outbox.ORDER_REJECTED,
        outbox.ORDER_CANCELLED,
    ]


def test_recover_pending_keeps_event_when_handler_fails(tmp_path: Path) -> None:
    outbox.enqueue(tmp_path, outbox.ORDER_FILLED, {}, event_id="fill:1", book_id="live")

    def boom(event):
        raise RuntimeError("boom")

    assert outbox.recover_pending(tmp_path, boom) == 0
    events = outbox.pending_events(tmp_path)
    assert events[0]["attempts"] == 1
    assert events[0]["last_error"] == "boom"


def test_failed_event_reaches_terminal_state_and_reports(tmp_path: Path, monkeypatch) -> None:
    import src.ops.alerts as alerts

    def bad_alert(*args, **kwargs) -> None:
        raise RuntimeError("alert down")

    monkeypatch.setattr(alerts, "emit_alert", bad_alert)
    outbox.enqueue(
        tmp_path,
        outbox.ORDER_SUBMITTED,
        {},
        event_id="order:x",
        book_id="live",
        status="ready",
    )
    for _ in range(outbox.MAX_ATTEMPTS - 1):
        outbox.mark(tmp_path, "order:x", "ready", bump_attempts=True)

    def boom(event):
        raise RuntimeError("boom")

    assert outbox.process_ready(tmp_path, boom) == 0
    failed = outbox._rows(tmp_path, "failed", 10)
    assert len(failed) == 1
    assert outbox.report_failed(tmp_path) == 1


def test_dispatcher_processes_ready_and_stops(tmp_path: Path) -> None:
    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="e1", status="ready")
    seen: list[dict] = []
    dispatcher = outbox.OutboxDispatcher(tmp_path, seen.append, interval=0.05)

    dispatcher.start()
    dispatcher.start()  # 已在跑，直接返回
    dispatcher.wake()
    deadline = time.time() + 3
    while outbox.ready_events(tmp_path) and time.time() < deadline:
        time.sleep(0.02)
    dispatcher.stop()
    dispatcher.stop()  # 已停止再停不报错

    assert [item["event_id"] for item in seen] == ["e1"]


def test_dispatcher_survives_consumer_errors(tmp_path: Path, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(outbox, "process_ready", boom)
    dispatcher = outbox.OutboxDispatcher(tmp_path, lambda event: None, interval=0.05)
    dispatcher.start()
    time.sleep(0.15)
    dispatcher.stop()

    assert dispatcher._thread is not None
    assert not dispatcher._thread.is_alive()


def test_start_and_stop_dispatcher_registry(tmp_path: Path) -> None:
    outbox.enqueue(tmp_path, outbox.ORDER_SUBMITTED, {}, event_id="e2", status="ready")

    dispatcher = outbox.start_dispatcher(tmp_path, lambda event: None)
    try:
        assert dispatcher is not None
        assert outbox.start_dispatcher(tmp_path, lambda event: None) is dispatcher
        outbox.wake_dispatcher(tmp_path)
    finally:
        outbox.stop_dispatcher(tmp_path)

    outbox.wake_dispatcher(tmp_path)
    outbox.stop_dispatcher(tmp_path)


# —— ctp_sync：柜台资金空值与坏值 ——


def test_should_log_mtm_handles_bad_and_small_values() -> None:
    from src.account.ctp_sync import _should_log_mtm

    assert _should_log_mtm("bad", 1) is True
    assert _should_log_mtm(1000.0, 1050.0) is False
    assert _should_log_mtm(1000.0, 1400.0) is True


def test_apply_broker_runtime_without_gateway_or_account(tmp_path: Path) -> None:
    from src.account.ctp_sync import apply_broker_runtime

    sizer = SimpleNamespace(capital=1.0, cash_available=None)
    assert (
        apply_broker_runtime(
            tmp_path, cfg={}, sizer=sizer, risk=None, trader=SimpleNamespace(gateway=None)
        )
        == ""
    )


def test_apply_broker_runtime_keeps_previous_equity(tmp_path: Path) -> None:
    from src.account.ctp_sync import apply_broker_runtime

    gateway = SimpleNamespace(
        settings=SimpleNamespace(user_id="28077", account_kind="sim"),
        account={"balance": "bad"},
        available=None,
    )
    trader = SimpleNamespace(
        gateway=gateway,
        last_broker_snapshot={"balance": 50000.0, "available": "bad"},
        last_broker_equity=0,
    )
    sizer = SimpleNamespace(capital=1.0, cash_available=None)
    cfg: dict = {}

    assert apply_broker_runtime(tmp_path, cfg=cfg, sizer=sizer, risk=None, trader=trader) == ""
    assert sizer.capital == 50000.0
    assert cfg["risk"]["capital"] == 50000.0
    assert sizer.cash_available is None


def test_apply_broker_runtime_ignores_bad_snapshot_numbers(tmp_path: Path) -> None:
    from src.account.ctp_sync import apply_broker_runtime

    gateway = SimpleNamespace(
        settings=SimpleNamespace(user_id="28077", account_kind="sim"),
        account={"balance": 1000.0},
        available=None,
    )
    trader = SimpleNamespace(
        gateway=gateway,
        last_broker_snapshot={"balance": "bad", "available": None},
        last_broker_equity="bad",
    )
    sizer = SimpleNamespace(capital=1.0, cash_available=2.0)

    apply_broker_runtime(tmp_path, cfg={}, sizer=sizer, risk=None, trader=trader)

    assert sizer.capital == 1.0
    assert sizer.cash_available == 2.0


def test_push_gateway_account_without_settings_or_data(tmp_path: Path) -> None:
    from src.account.ctp_sync import push_gateway_account

    assert push_gateway_account(tmp_path, SimpleNamespace(settings=None)) == "还没有柜台连接。"
    empty = SimpleNamespace(
        settings=SimpleNamespace(user_id="28077", account_kind="sim"),
        account=None,
        available=None,
    )
    assert "柜台还没有资金数据" in push_gateway_account(tmp_path, empty)


def test_push_gateway_account_fills_available_and_balance(tmp_path: Path) -> None:
    from src.account.ctp_sync import push_gateway_account
    from src.account.signal_book import load_named_book

    gateway = SimpleNamespace(
        settings=SimpleNamespace(user_id="28077", account_kind="sim"),
        account={},
        available=30000.0,
    )

    note = push_gateway_account(tmp_path, gateway)

    assert "30000.00" in note
    book = load_named_book(tmp_path, "ctp_sim")
    assert book is not None
    assert abs(float(book["ctp_snapshot"]["balance"]) - 30000.0) < 0.01
    assert abs(float(book["ctp_snapshot"]["available"]) - 30000.0) < 0.01


def test_push_gateway_account_handles_bad_old_balance(tmp_path: Path) -> None:
    from src.account.ctp_sync import push_gateway_account
    from src.account.signal_book import save_named_book

    save_named_book(tmp_path, "ctp_sim", {"ctp_snapshot": {"balance": "bad", "available": 0}})
    gateway = SimpleNamespace(
        settings=SimpleNamespace(user_id="28077", account_kind="sim"),
        account={"balance": 40000.0},
        available=40000.0,
    )

    note = push_gateway_account(tmp_path, gateway)

    assert "40000.00" in note


def test_align_active_book_skips_non_ctp(tmp_path: Path) -> None:
    from src.account.ctp_sync import align_active_book_to_ctp

    assert align_active_book_to_ctp(tmp_path, {"trade": {"executor": "gui"}}) == ""


def test_resident_guard_requires_user_id(tmp_path: Path) -> None:
    from src.account.ctp_sync import resident_book_guard
    from src.account.workspace import set_active_id

    cfg = {
        "trade": {
            "executor": "ctp",
            "ctp": {"account_kind": "live", "ctp_profiles": {"p": {"kind": "live"}}},
        },
        "account": {},
    }
    set_active_id(tmp_path, "ctp_live")
    assert "资金账号" in resident_book_guard(tmp_path, cfg)

    cfg["trade"]["ctp"]["account_kind"] = "sim"
    set_active_id(tmp_path, "ctp_sim")
    assert "28077" in resident_book_guard(tmp_path, cfg)


def test_can_pull_from_trader_checks_book_and_session() -> None:
    from src.account.ctp_sync import can_pull_from_trader

    assert can_pull_from_trader(None, "ctp_sim") is False
    assert can_pull_from_trader(SimpleNamespace(), "live") is False

    trader = SimpleNamespace(
        settings=None,
        gateway=SimpleNamespace(session=SimpleNamespace(ready=True)),
    )
    assert can_pull_from_trader(trader, "ctp_sim") is False

    trader = SimpleNamespace(
        settings=SimpleNamespace(account_kind="sim"),
        gateway=SimpleNamespace(session=SimpleNamespace(ready=False)),
    )
    assert can_pull_from_trader(trader, "ctp_sim") is False

    trader.gateway.session.ready = True
    assert can_pull_from_trader(trader, "ctp_sim") is True
    assert can_pull_from_trader(trader, "ctp_live") is False
