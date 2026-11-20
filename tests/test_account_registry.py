"""账号注册表：迁移、持久化、物化、回退与校验。"""

from __future__ import annotations

import copy
from pathlib import Path

from src.account import registry as reg
from src.domain.trading_account import (
    BrokerProfileTemplate,
    TradingAccount,
    new_account_id,
    validate_registry,
)
from src.utils.secret_store import SecretStore


class _FakeCipher:
    """测试用：protect 加前缀，unprotect 去前缀，不碰真实 DPAPI。"""

    def protect(self, text: str) -> str:
        return "enc:" + text

    def unprotect(self, token: str) -> str:
        return token[4:]


def _cfg() -> dict:
    return {
        "account": {
            "broker": "华泰期货",
            "user_id": "20318052",
            "password_env": "FUTURES_PASSWORD",
        },
        "trade": {
            "ctp": {
                "app_id": "app-1",
                "flow_dir": "data/ctp_flow",
                "ctp_profile": "huatai_live",
                "ctp_profiles": {
                    "huatai_sim": {
                        "label": "华泰仿真",
                        "kind": "sim",
                        "broker_id": "9999",
                        "td_front": "tcp://sim-td:41205",
                        "md_front": "tcp://sim-md:41213",
                        "user_id": "28077",
                        "password_env": "CTP_PASSWORD",
                        "auth_code_env": "",
                    },
                    "huatai_live": {
                        "label": "华泰真实",
                        "kind": "live",
                        "broker_id": "9999",
                        "td_front": "tcp://live-td:41206",
                        "md_front": "tcp://live-md:41214",
                        "user_id": "20318052",
                        "password_env": "CTP_LIVE_PASSWORD",
                        "auth_code_env": "CTP_AUTH_CODE",
                    },
                },
            }
        },
    }


def _profile(profile_id: str = "huatai_sim", environment: str = "sim", **extra) -> BrokerProfileTemplate:
    data = {
        "profile_id": profile_id,
        "label": profile_id,
        "environment": environment,
        "td_fronts": ["tcp://td:1"],
        "md_fronts": ["tcp://md:2"],
        **extra,
    }
    return BrokerProfileTemplate.from_mapping(data)


def _account(account_id: str = "acc_sim_01", **extra) -> TradingAccount:
    data = {
        "account_id": account_id,
        "display_name": account_id,
        "channel": "ctp",
        "environment": "sim",
        "status": "enabled",
        "profile_id": "huatai_sim",
        "investor_id": "28077",
        "book_id": "ctp_sim",
        **extra,
    }
    return TradingAccount.from_mapping(data)


# —— 迁移 ——


def test_migrate_from_cfg_builds_accounts_and_books(tmp_path: Path) -> None:
    registry = reg.migrate_from_cfg(_cfg())

    assert [item.account_id for item in registry.accounts] == [
        "acc_huatai_sim_01",
        "acc_huatai_live_01",
        "acc_gui_01",
    ]
    assert [item.book_id for item in registry.accounts] == ["ctp_sim", "ctp_live", "live"]
    sim = registry.account("acc_huatai_sim_01")
    live = registry.account("acc_huatai_live_01")
    gui = registry.account("acc_gui_01")
    assert sim is not None and sim.profile_id == "huatai_sim" and sim.investor_id == "28077"
    assert live is not None and live.environment == "live"
    assert gui is not None and gui.channel == "gui" and gui.profile_id == ""
    assert registry.profile("huatai_sim").password_ref == "secret://ctp/sim/password"
    assert registry.profile("huatai_live").auth_code_ref == "secret://ctp/auth_code"
    assert registry.validate() == []


def test_ensure_registry_writes_once_and_reloads(tmp_path: Path) -> None:
    cfg = _cfg()
    first = reg.ensure_registry(tmp_path, cfg)
    path = tmp_path / "config" / "accounts.yaml"
    assert first is not None and path.exists()

    second = reg.ensure_registry(tmp_path, cfg)
    assert second is not None
    assert [item.account_id for item in second.accounts] == [item.account_id for item in first.accounts]
    assert path.read_text(encoding="utf-8").count("account_id:") == 3


# —— 物化 ——


def test_materialize_writes_account_identity_and_secret(tmp_path: Path) -> None:
    cfg = _cfg()
    registry = reg.ensure_registry(tmp_path, cfg)
    assert registry is not None
    registry.accounts[0].investor_id = "28088"
    registry.accounts[0].display_name = "仿真二号"
    store = SecretStore(tmp_path / "config" / "secrets.dpapi.json", cipher=_FakeCipher())
    store.set("secret://ctp/sim/password", "pw-sim")

    registry.materialize(cfg, {"ctp_password": "legacy"}, store=store)

    item = cfg["trade"]["ctp"]["ctp_profiles"]["huatai_sim"]
    assert item["user_id"] == "28088"
    assert item["account_id"] == "acc_huatai_sim_01"
    assert item["kind"] == "sim"
    assert item["status"] == "enabled"
    assert item["password_ref"] == "secret://ctp/sim/password"
    assert item["resolved_password"] == "pw-sim"
    # 未纳管的旧档案原样保留
    assert cfg["trade"]["ctp"]["ctp_profiles"]["huatai_live"]["user_id"] == "20318052"


def test_apply_registry_falls_back_when_disabled(tmp_path: Path) -> None:
    cfg = _cfg()
    cfg["trade"]["ctp"]["account_registry"] = False
    before = copy.deepcopy(cfg)

    notes = reg.apply_account_registry(tmp_path, cfg)

    assert notes == []
    assert cfg == before
    assert not (tmp_path / "config" / "accounts.yaml").exists()


def test_apply_registry_without_legacy_profiles_does_nothing(tmp_path: Path) -> None:
    cfg = {"trade": {"ctp": {"ctp_profiles": {}}}}
    before = copy.deepcopy(cfg)
    assert reg.apply_account_registry(tmp_path, cfg) == []
    assert cfg == before
    assert not (tmp_path / "config" / "accounts.yaml").exists()


# —— 校验 ——


def test_validate_rejects_sim_front_on_live_account() -> None:
    profile = _profile("p_live", "live", td_fronts=["tcp://180.168.146.187:41205"])
    account = _account("acc_live_01", environment="live", profile_id="p_live", investor_id="20318052")
    problems = validate_registry([account], [profile])
    assert any("仿真前置" in item for item in problems)


def test_validate_rejects_real_investor_id_in_sim_account() -> None:
    profiles = [_profile("p_sim", "sim"), _profile("p_live", "live")]
    accounts = [
        _account("acc_sim_01", profile_id="p_sim", investor_id="20318052", book_id="book_a"),
        _account(
            "acc_live_01",
            environment="live",
            profile_id="p_live",
            investor_id="20318052",
            book_id="book_b",
        ),
    ]
    problems = validate_registry(accounts, profiles)
    assert any("仿真账号填了真实资金号" in item for item in problems)


def test_validate_allows_shared_connection_but_rejects_shared_book() -> None:
    profile = _profile("p_sim", "sim")
    accounts = [
        _account("acc_a_01", profile_id="p_sim", investor_id="1", book_id="book_a"),
        _account("acc_b_01", profile_id="p_sim", investor_id="2", book_id="book_a"),
    ]
    problems = validate_registry(accounts, [profile])
    # 一条连接可以被多个账号引用（资金号在账号上），只有账本不能重复。
    assert not any("共用" in item for item in problems)
    assert any("账本 book_a" in item for item in problems)


def test_validate_requires_investor_id_when_enabled() -> None:
    problems = validate_registry([_account("acc_a_01", investor_id="")], [_profile("huatai_sim")])
    assert any("必须填资金账号" in item for item in problems)


# —— 新增 / 复制 ——


def test_add_and_duplicate_account(tmp_path: Path) -> None:
    registry = reg.ensure_registry(tmp_path, _cfg())
    assert registry is not None
    source = registry.account("acc_huatai_sim_01")
    assert source is not None

    added = registry.add_account(
        "华泰仿真-2号",
        "sim",
        "28099",
        template=registry.profile(source.profile_id),
    )
    assert added.account_id.startswith("acc_")
    # 传入现有连接表示复用，不再复制一条连接。
    assert added.profile_id == source.profile_id
    assert len([item for item in registry.profiles if item.profile_id == source.profile_id]) == 1
    assert added.book_id == added.account_id

    dup = registry.duplicate_account(added.account_id, "华泰仿真-3号", "28100")
    assert dup.account_id != added.account_id
    assert dup.investor_id == "28100"
    assert registry.validate() == []


def test_duplicate_sim_to_live_requires_new_investor_id(tmp_path: Path) -> None:
    registry = reg.ensure_registry(tmp_path, _cfg())
    assert registry is not None
    try:
        registry.duplicate_account("acc_huatai_sim_01", "复制到真实", "28077", "live")
    except ValueError as exc:
        assert "真实资金账号" in str(exc)
    else:  # pragma: no cover - 这里必须抛错
        raise AssertionError("仿真复制到真实时沿用原资金号应当报错")


def test_new_sim_account_is_selectable_and_resolves(tmp_path: Path) -> None:
    """验收：新增仿真账号不改代码，物化后按档案名就能解析到它的资金号。"""
    cfg = _cfg()
    registry = reg.ensure_registry(tmp_path, cfg)
    assert registry is not None
    added = registry.add_account(
        "华泰仿真-2号",
        "sim",
        "28099",
        template=registry.profile("huatai_sim"),
    )
    registry.materialize(cfg, store=SecretStore(tmp_path / "config" / "s.json", cipher=_FakeCipher()))

    from src.trader.ctp_profile import resolve_ctp_profile

    key = registry.runtime_profile_key(added.account_id)
    assert key in cfg["trade"]["ctp"]["ctp_profiles"]
    cfg["trade"]["ctp"]["ctp_profile"] = key
    resolved = resolve_ctp_profile(cfg["trade"]["ctp"], account=cfg["account"], kind="sim")
    assert resolved.get("user_id") == "28099"
    assert resolved.get("td_front") == "tcp://sim-td:41205"


def test_new_account_id_is_stable_and_unique() -> None:
    assert new_account_id("huatai_sim", []) == "acc_huatai_sim_01"
    assert new_account_id("huatai_sim", ["acc_huatai_sim_01"]) == "acc_huatai_sim_02"
    assert new_account_id("acc_x", []) == "acc_x_01"


# —— 连接 / 账号 / 账本 v2 ——


def test_shared_connection_resolves_books_by_account(tmp_path: Path) -> None:
    registry = reg.AccountRegistry(
        profiles=[_profile("conn_sim", password_env="CTP_PASSWORD")],
        accounts=[
            _account("acc_a_01", profile_id="conn_sim", book_id="acc_a_01", investor_id="1"),
            _account("acc_b_01", profile_id="conn_sim", book_id="acc_b_01", investor_id="2"),
        ],
    )
    registry.save(tmp_path)

    from src.domain.accounts import book_file, profile_of

    assert profile_of("acc_b_01", root=tmp_path)["book_name"] == "signal_account_b_01.json"
    assert book_file(tmp_path, "acc_b_01").name == "signal_account_b_01.json"
    # 旧账号沿用旧账本 id 时仍落在旧文件上。
    registry.accounts[0].book_id = "ctp_sim"
    registry.save(tmp_path)
    assert book_file(tmp_path, "acc_a_01").name == profile_of("ctp_sim", root=tmp_path)["book_name"]


def test_set_active_account_switches_executor_profile_and_book(tmp_path: Path) -> None:
    from src.account.session import set_active_account
    from src.domain.accounts import load_active_id

    registry = reg.AccountRegistry(
        profiles=[_profile("conn_sim", password_env="CTP_PASSWORD")],
        accounts=[
            _account("acc_a_01", profile_id="conn_sim", book_id="acc_a_01", investor_id="1"),
            _account("acc_b_01", profile_id="conn_sim", book_id="acc_b_01", investor_id="2"),
        ],
    )
    registry.save(tmp_path)
    cfg = _cfg()
    cfg["trade"]["executor"] = "gui"

    session = set_active_account(tmp_path, cfg, "acc_b_01", {})

    assert session.book_id == "acc_b_01"
    assert cfg["trade"]["executor"] == "ctp"
    assert cfg["trade"]["ctp"]["account_kind"] == "sim"
    assert cfg["trade"]["ctp"]["ctp_profile"] == "conn_sim__b_01"
    assert load_active_id(tmp_path) == "acc_b_01"
    assert cfg["trade"]["ctp"]["ctp_profiles"]["conn_sim__b_01"]["user_id"] == "2"


def test_set_active_account_rejects_disabled(tmp_path: Path) -> None:
    import pytest

    from src.account.session import set_active_account

    registry = reg.AccountRegistry(
        profiles=[_profile("conn_sim", password_env="CTP_PASSWORD")],
        accounts=[
            _account("acc_a_01", profile_id="conn_sim", status="disabled", investor_id="1"),
        ],
    )
    registry.save(tmp_path)
    with pytest.raises(ValueError, match="启用"):
        set_active_account(tmp_path, _cfg(), "acc_a_01", {})


# —— GUI 账号：登录参数并入账号 ——


def test_migrate_from_cfg_gui_keeps_gui_settings() -> None:
    cfg = _cfg()
    cfg["account"].update({"captcha_mode": "skip", "skip_auto_login": True})

    registry = reg.migrate_from_cfg(cfg)
    gui = registry.account("acc_gui_01")

    assert gui is not None
    assert gui.captcha_mode == "skip"
    assert gui.skip_auto_login is True
    assert gui.password_ref == "secret://gui/password"


def test_ensure_registry_backfills_gui_settings_for_old_file(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "config" / "accounts.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "connections": [],
                "accounts": [
                    {
                        "account_id": "acc_gui_01",
                        "display_name": "GUI实盘-20318052",
                        "channel": "gui",
                        "environment": "live",
                        "status": "enabled",
                        "investor_id": "20318052",
                        "book_id": "live",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cfg = _cfg()
    cfg["account"].update({"captcha_mode": "skip", "skip_auto_login": False})

    registry = reg.ensure_registry(tmp_path, cfg)

    assert registry is not None
    gui = registry.account("acc_gui_01")
    assert gui is not None
    assert gui.captcha_mode == "skip"
    assert gui.skip_auto_login is False
    assert gui.password_ref == "secret://gui/password"
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["accounts"][0]["skip_auto_login"] is False


def test_resolve_session_hydrates_gui_account_block(
    tmp_path: Path, monkeypatch
) -> None:
    from src.account.session import set_active_account

    monkeypatch.setenv("TEST_GUI_PW", "pw-gui")
    registry = reg.AccountRegistry(
        accounts=[
            _account(
                "acc_gui_01",
                channel="gui",
                environment="live",
                profile_id="",
                book_id="live",
                investor_id="20318052",
                password_ref="env://TEST_GUI_PW",
                captcha_mode="skip",
                skip_auto_login=False,
            )
        ]
    )
    registry.save(tmp_path)
    cfg = _cfg()
    cfg["account"] = {"user_id": "old", "resolved_password": "old-pw"}

    set_active_account(tmp_path, cfg, "acc_gui_01", {})

    block = cfg["account"]
    assert cfg["trade"]["executor"] == "gui"
    assert block["user_id"] == "20318052"
    assert block["resolved_password"] == "pw-gui"
    assert block["captcha_mode"] == "skip"
    assert block["skip_auto_login"] is False
    assert block["_from_active_account"] is True


def test_switch_gui_account_clears_previous_password(
    tmp_path: Path, monkeypatch
) -> None:
    from src.account.session import set_active_account

    monkeypatch.delenv("FUTURES_PASSWORD", raising=False)
    monkeypatch.setenv("TEST_GUI_A_PW", "pw-a")
    registry = reg.AccountRegistry(
        accounts=[
            _account(
                "acc_gui_a",
                channel="gui",
                environment="live",
                profile_id="",
                book_id="book_a",
                investor_id="1001",
                password_ref="env://TEST_GUI_A_PW",
            ),
            _account(
                "acc_gui_b",
                channel="gui",
                environment="live",
                profile_id="",
                book_id="book_b",
                investor_id="1002",
            ),
        ]
    )
    registry.save(tmp_path)
    cfg = _cfg()

    set_active_account(tmp_path, cfg, "acc_gui_a", {})
    assert cfg["account"]["resolved_password"] == "pw-a"

    set_active_account(tmp_path, cfg, "acc_gui_b", {})
    assert cfg["account"]["user_id"] == "1002"
    assert cfg["account"]["resolved_password"] == ""


def test_inject_ctp_secrets_skips_registry_managed_password(monkeypatch) -> None:
    from src.utils.config_loader import inject_ctp_secrets

    monkeypatch.setenv("FUTURES_PASSWORD", "env-pw")
    cfg = {"account": {"resolved_password": "", "_from_active_account": True}}

    inject_ctp_secrets(cfg, {"password": "file-pw"})

    assert cfg["account"]["resolved_password"] == ""


def test_validate_requires_investor_for_gui() -> None:
    account = _account(
        "acc_gui_01",
        channel="gui",
        environment="live",
        profile_id="",
        investor_id="",
        book_id="live",
    )
    problems = validate_registry([account], [])
    assert any("GUI 账号必须填资金账号" in item for item in problems)
