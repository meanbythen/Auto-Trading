"""密钥托管：DPAPI 文件、解析顺序、迁移与不可用时的回退。"""

from __future__ import annotations

import base64
import copy
from pathlib import Path

import pytest

from src.utils import secret_store as ss


class _FakeCipher:
    """测试用可逆编码；密文里看不到明文。"""

    def protect(self, text: str) -> str:
        return base64.b64encode(("x" + text).encode("utf-8")).decode("ascii")

    def unprotect(self, token: str) -> str:
        return base64.b64decode(token.encode("ascii")).decode("utf-8")[1:]


def _store(tmp_path: Path) -> ss.SecretStore:
    return ss.SecretStore(tmp_path / "secrets.dpapi.json", cipher=_FakeCipher())


def test_ctypes_dpapi_roundtrip_without_pywin32() -> None:
    """打包环境没有 pywin32 时，ctypes 直接调 crypt32 也要能加解密。"""
    import os

    if os.name != "nt":
        pytest.skip("只在 Windows 上验证 DPAPI")
    backend = ss._CtypesDpapi()
    token = backend.protect("ctp-secret-123")
    assert b"ctp-secret-123" not in token
    assert backend.unprotect(token) == b"ctp-secret-123"


def test_set_and_resolve_dpapi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTP_PASSWORD", raising=False)
    store = _store(tmp_path)
    store.set("secret://ctp/sim/password", "pw-sim")

    assert store.resolve("secret://ctp/sim/password") == "pw-sim"
    text = store.path.read_text(encoding="utf-8")
    assert "pw-sim" not in text
    assert "secret://ctp/sim/password" in text


def test_resolve_order_dpapi_then_env_then_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("CTP_PASSWORD", "pw-env")
    assert store.resolve("secret://ctp/sim/password", {"ctp_password": "pw-file"}) == "pw-env"

    store.set("secret://ctp/sim/password", "pw-dpapi")
    assert store.resolve("secret://ctp/sim/password", {"ctp_password": "pw-file"}) == "pw-dpapi"

    monkeypatch.delenv("CTP_PASSWORD", raising=False)
    _other = ss.SecretStore(tmp_path / "empty.json", cipher=_FakeCipher())
    assert _other.resolve("secret://ctp/sim/password", {"ctp_password": "pw-file"}) == "pw-file"


def test_env_uri_is_read_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "abc")
    store = ss.SecretStore(Path("nonexistent.json"), cipher=_FakeCipher())
    assert store.resolve("env://MY_TOKEN") == "abc"


def test_unavailable_cipher_refuses_to_store_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args, **_kwargs) -> None:
        raise ss.SecretStoreUnavailable("no dpapi")

    monkeypatch.setattr(ss, "DpapiCipher", _boom)
    path = tmp_path / "secrets.dpapi.json"
    store = ss.SecretStore(path)

    assert store.available() is False
    with pytest.raises(ss.SecretStoreUnavailable):
        store.set("secret://ctp/live/password", "pw")
    assert not path.exists()


def test_migrate_legacy_copies_without_touching_source(tmp_path: Path) -> None:
    legacy = {
        "password": "gui-pw",
        "ctp_password": "sim-pw",
        "ctp_live_password": "live-pw",
        "ctp_auth_code": "auth-code",
    }
    before = copy.deepcopy(legacy)
    store = _store(tmp_path)

    migrated = store.migrate_legacy(legacy)

    assert set(migrated) == {
        "secret://gui/password",
        "secret://ctp/sim/password",
        "secret://ctp/live/password",
        "secret://ctp/auth_code",
    }
    assert legacy == before
    assert store.resolve("secret://ctp/live/password") == "live-pw"
    # 再迁移一次不会重复写
    assert store.migrate_legacy(legacy) == []


def test_ref_and_env_helpers() -> None:
    assert ss.ref_for_env("CTP_PASSWORD") == "secret://ctp/sim/password"
    assert ss.ref_for_env("CTP_LIVE_PASSWORD") == "secret://ctp/live/password"
    assert ss.ref_for_env("FUTURES_PASSWORD") == "secret://gui/password"
    assert ss.ref_for_env("custom_pwd") == "secret://ctp/custom_pwd"
    assert ss.env_names_for("secret://ctp/sim/password") == ("CTP_PASSWORD",)
    assert ss.legacy_keys_for("secret://ctp/live/password") == ("ctp_live_password", "password")
