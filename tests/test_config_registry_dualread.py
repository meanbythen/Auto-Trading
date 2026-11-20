"""配置双读：有注册表时物化，关闭/损坏/不存在时回退旧 ctp_profiles。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.utils import config_loader


def _cfg(account_registry: object | None = None) -> dict:
    ctp: dict = {
        "app_id": "app-1",
        "ctp_profile": "huatai_live",
        "ctp_profiles": {
            "huatai_sim": {
                "label": "华泰仿真",
                "kind": "sim",
                "td_front": "tcp://sim-td:41205",
                "md_front": "tcp://sim-md:41213",
                "user_id": "28077",
                "password_env": "CTP_PASSWORD",
            },
            "huatai_live": {
                "label": "华泰真实",
                "kind": "live",
                "td_front": "tcp://live-td:41206",
                "md_front": "tcp://live-md:41214",
                "user_id": "20318052",
                "password_env": "CTP_LIVE_PASSWORD",
            },
        },
    }
    if account_registry is not None:
        ctp["account_registry"] = account_registry
    return {
        "dry_run": True,
        "account": {"broker": "华泰期货", "user_id": "20318052", "password_env": "FUTURES_PASSWORD"},
        "trade": {"ctp": ctp},
    }


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(config_loader, "project_root", lambda: tmp_path)
    monkeypatch.setattr(config_loader, "ensure_runtime_layout", lambda: tmp_path)
    monkeypatch.delenv("FUTURES_PASSWORD", raising=False)
    monkeypatch.delenv("CTP_PASSWORD", raising=False)
    monkeypatch.delenv("CTP_LIVE_PASSWORD", raising=False)
    return tmp_path


def _write(path: Path, cfg: dict) -> None:
    (path / "config" / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def test_load_config_migrates_and_materializes(sandbox: Path) -> None:
    _write(sandbox, _cfg())

    cfg = config_loader.load_config(apply_universe=False)

    assert (sandbox / "config" / "accounts.yaml").exists()
    profile = cfg["trade"]["ctp"]["ctp_profiles"]["huatai_sim"]
    assert profile["account_id"] == "acc_huatai_sim_01"
    assert profile["user_id"] == "28077"
    assert profile["password_ref"] == "secret://ctp/sim/password"


def test_saved_config_has_no_plaintext_secret(sandbox: Path) -> None:
    (sandbox / "config" / "secrets.local.yaml").write_text(
        yaml.safe_dump({"password": "gui-secret", "ctp_password": "sim-secret"}), encoding="utf-8"
    )
    _write(sandbox, _cfg())

    cfg = config_loader.load_config(apply_universe=False)
    assert cfg["account"]["resolved_password"] == "gui-secret"

    path = config_loader.save_config(cfg)
    text = path.read_text(encoding="utf-8")
    assert "gui-secret" not in text
    assert "sim-secret" not in text
    assert "resolved_password" not in text
    assert "resolved_auth_code" not in text


def test_registry_can_be_disabled(sandbox: Path) -> None:
    _write(sandbox, _cfg(account_registry=False))

    cfg = config_loader.load_config(apply_universe=False)

    assert not (sandbox / "config" / "accounts.yaml").exists()
    assert "account_id" not in cfg["trade"]["ctp"]["ctp_profiles"]["huatai_sim"]


def test_broken_registry_falls_back_to_legacy(sandbox: Path) -> None:
    _write(sandbox, _cfg())
    (sandbox / "config" / "accounts.yaml").write_text("::: broken", encoding="utf-8")

    cfg = config_loader.load_config(apply_universe=False)

    profile = cfg["trade"]["ctp"]["ctp_profiles"]["huatai_sim"]
    assert "account_id" not in profile
    assert profile["user_id"] == "28077"


def test_registry_changes_win_over_old_profile(sandbox: Path) -> None:
    _write(sandbox, _cfg())
    config_loader.load_config(apply_universe=False)

    registry_path = sandbox / "config" / "accounts.yaml"
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    for account in data["accounts"]:
        if account["account_id"] == "acc_huatai_sim_01":
            account["investor_id"] = "28088"
            account["display_name"] = "仿真二号"
    registry_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    cfg = config_loader.load_config(apply_universe=False)
    assert cfg["trade"]["ctp"]["ctp_profiles"]["huatai_sim"]["user_id"] == "28088"
