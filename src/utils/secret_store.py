"""
密钥托管：Windows DPAPI 加密文件，环境变量与旧密钥文件只做回退。

功能：密码/认证码只保存成引用（secret://...），真实值用当前 Windows 用户的 DPAPI 加密，
      落在 config/secrets.dpapi.json。解不开密文就按"环境变量 → secrets.local.yaml"回退。
原理：优先用 pywin32 的 win32crypt；没装时直接用 ctypes 调系统 crypt32.dll，
      同样走 Windows DPAPI，所以打包环境不需要额外依赖。非 Windows 明确报错，
      不静默降级成明文写盘。secrets.local.yaml 保持只读备份，既不删也不改。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
from typing import Any, Iterable, Protocol

from loguru import logger

from src.utils.paths import project_root

SECRET_SCHEME = "secret://"
ENV_SCHEME = "env://"

# 引用 → 可推导的环境变量名。界面里仍按环境变量名填写，旧链路不用改。
REF_ENV_NAMES: dict[str, tuple[str, ...]] = {
    "secret://gui/password": ("FUTURES_PASSWORD",),
    "secret://ctp/sim/password": ("CTP_PASSWORD",),
    "secret://ctp/live/password": ("CTP_LIVE_PASSWORD",),
    "secret://ctp/auth_code": ("CTP_AUTH_CODE",),
    "secret://tq/password": ("TQ_PASSWORD",),
}

# 引用 → secrets.local.yaml 里的旧键（只读回退）。
REF_LEGACY_KEYS: dict[str, tuple[str, ...]] = {
    "secret://gui/password": ("password",),
    "secret://ctp/sim/password": ("ctp_password",),
    "secret://ctp/live/password": ("ctp_live_password", "password"),
    "secret://ctp/auth_code": ("ctp_auth_code",),
    "secret://tq/password": ("tq_password",),
}


class SecretStoreUnavailable(RuntimeError):
    """本机无法使用 DPAPI 加密；不允许把密钥降级成明文保存。"""


class SecretCipher(Protocol):
    """DPAPI 的最小接口，单测可以注入假实现。"""

    def protect(self, text: str) -> str: ...

    def unprotect(self, token: str) -> str: ...


class _DataBlob(ctypes.Structure):
    """crypt32 的 DATA_BLOB。"""

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class _CtypesDpapi:
    """不依赖 pywin32 的 DPAPI：直接调用 Windows crypt32.dll。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStoreUnavailable("当前系统不是 Windows，无法使用 Windows DPAPI")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:  # pragma: no cover - 只在异常系统
            raise SecretStoreUnavailable(f"无法加载 Windows DPAPI: {exc}") from exc
        blob_ptr = ctypes.POINTER(_DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_ptr,
            ctypes.c_wchar_p,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob_ptr,
        ]
        self._crypt32.CryptProtectData.restype = ctypes.c_int
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_ptr,
            ctypes.POINTER(ctypes.c_wchar_p),
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob_ptr,
        ]
        self._crypt32.CryptUnprotectData.restype = ctypes.c_int
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

        # 先试一次，确认系统真的能加密（某些精简系统缺 crypt32 依赖）。
        self.protect("selftest")

    @staticmethod
    def _blob(data: bytes) -> tuple[ctypes.Array[Any], _DataBlob]:
        buffer = ctypes.create_string_buffer(data, max(len(data), 1))
        return buffer, _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    def _free(self, blob: _DataBlob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def protect(self, text: str) -> bytes:
        data = text.encode("utf-8")
        _buffer, in_blob = self._blob(data)
        out_blob = _DataBlob()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "AutoTrading",
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
        return self._free(out_blob)

    def unprotect(self, raw: bytes) -> bytes:
        _buffer, in_blob = self._blob(raw)
        out_blob = _DataBlob()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
        return self._free(out_blob)


class DpapiCipher:
    """Windows DPAPI：只有同一台机器的同一用户能解开。"""

    def __init__(self) -> None:
        self._crypt: Any = None
        self._ctypes: _CtypesDpapi | None = None
        try:
            import win32crypt  # type: ignore
        except ImportError:
            # 打包后没有 pywin32 也能用，直接走系统 crypt32。
            self._ctypes = _CtypesDpapi()
        else:
            self._crypt = win32crypt

    def protect(self, text: str) -> str:
        if self._ctypes is not None:
            return base64.b64encode(self._ctypes.protect(text)).decode("ascii")
        assert self._crypt is not None
        blob = self._crypt.CryptProtectData(
            text.encode("utf-8"), "AutoTrading", None, None, None, 0
        )
        return base64.b64encode(blob).decode("ascii")

    def unprotect(self, token: str) -> str:
        raw = base64.b64decode(token.encode("ascii"))
        if self._ctypes is not None:
            return self._ctypes.unprotect(raw).decode("utf-8")
        assert self._crypt is not None
        data, _desc = self._crypt.CryptUnprotectData(raw, None, None, None, 0)
        return bytes(data).decode("utf-8")


def normalize_ref(ref: Any) -> str:
    """统一引用写法：scheme 和路径小写；env:// 的名称保持原样。"""
    text = str(ref or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith(ENV_SCHEME):
        return ENV_SCHEME + text[len(ENV_SCHEME):].strip()
    if lower.startswith(SECRET_SCHEME):
        return SECRET_SCHEME + text[len(SECRET_SCHEME):].strip().lower()
    return text


def ref_for_env(env_name: Any) -> str:
    """环境变量名 → 标准引用；未知名称统一收进 secret://ctp/<小写名>。"""
    name = str(env_name or "").strip()
    if not name:
        return ""
    upper = name.upper()
    for ref, names in REF_ENV_NAMES.items():
        if upper in names:
            return ref
    return f"secret://ctp/{upper.lower()}"


def env_names_for(ref: Any) -> tuple[str, ...]:
    """引用能对应的环境变量名，按优先级返回。"""
    key = normalize_ref(ref)
    if key.startswith(ENV_SCHEME):
        return (key[len(ENV_SCHEME):],)
    names = REF_ENV_NAMES.get(key)
    if names:
        return names
    tail = key.rsplit("/", 1)[-1] if key else ""
    return (tail.upper(),) if tail else ()


def legacy_keys_for(ref: Any) -> tuple[str, ...]:
    """引用能对应的 secrets.local.yaml 旧键。"""
    key = normalize_ref(ref)
    keys = REF_LEGACY_KEYS.get(key)
    if keys:
        return keys
    tail = key.rsplit("/", 1)[-1] if key else ""
    return (tail.lower(),) if tail else ()


def secrets_dpapi_path() -> Path:
    """加密密钥文件路径（与 config.yaml 同目录，不进 git）。"""
    return project_root() / "config" / "secrets.dpapi.json"


class SecretStore:
    """引用式密钥仓库。文件里只有密文，不保存明文。"""

    def __init__(self, path: Path | None = None, cipher: SecretCipher | None = None) -> None:
        self.path = Path(path) if path is not None else secrets_dpapi_path()
        self._cipher = cipher
        self._cipher_error = ""

    # —— 内部 ——

    def _cipher_or_raise(self) -> SecretCipher:
        if self._cipher is not None:
            return self._cipher
        try:
            self._cipher = DpapiCipher()
        except SecretStoreUnavailable as exc:
            self._cipher_error = str(exc)
            raise
        return self._cipher

    def _items(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("密钥库读不出来，按空处理: {}", exc)
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in (items or {}).items()} if isinstance(items, dict) else {}

    def _write_items(self, items: dict[str, str]) -> Path:
        payload = {"version": 1, "items": items}
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    # —— 对外 ——

    def available(self) -> bool:
        """本机能否写密文（能加密才能保存新密钥）。"""
        try:
            self._cipher_or_raise()
        except SecretStoreUnavailable:
            return False
        return True

    def list_refs(self) -> list[str]:
        return sorted(self._items())

    def set(self, ref: Any, value: str) -> None:
        """保存一条密钥；DPAPI 不可用时抛 SecretStoreUnavailable，不写明文。"""
        key = normalize_ref(ref)
        if not key:
            raise ValueError("密钥引用不能为空")
        token = self._cipher_or_raise().protect(str(value))
        items = self._items()
        items[key] = token
        self._write_items(items)

    def delete(self, ref: Any) -> bool:
        key = normalize_ref(ref)
        items = self._items()
        if key not in items:
            return False
        items.pop(key, None)
        self._write_items(items)
        return True

    def resolve(self, ref: Any, legacy: dict[str, Any] | None = None) -> str:
        """
        解引用：密文 → 环境变量 → secrets.local.yaml 旧键。

        找不到返回空串，由调用方决定是否报「缺少密码」。
        """
        key = normalize_ref(ref)
        if not key:
            return ""
        if key.startswith(ENV_SCHEME):
            return os.environ.get(key[len(ENV_SCHEME):], "")
        token = self._items().get(key)
        if token:
            try:
                return self._cipher_or_raise().unprotect(token)
            except (SecretStoreUnavailable, ValueError, OSError) as exc:
                logger.warning("密钥 {} 解不开，回退环境变量: {}", key, exc)
        for name in env_names_for(key):
            value = os.environ.get(name, "")
            if value:
                return value
        box = legacy or {}
        for name in legacy_keys_for(key):
            value = str(box.get(name, "") or "")
            if value:
                return value
        return ""

    def migrate_legacy(
        self,
        legacy: dict[str, Any],
        refs: Iterable[str] | None = None,
    ) -> list[str]:
        """把 secrets.local.yaml 的明文搬到 DPAPI；原文件不改不删。"""
        items = self._items()
        migrated: list[str] = []
        for ref in refs or tuple(REF_ENV_NAMES):
            key = normalize_ref(ref)
            if not key or key in items:
                continue
            value = ""
            for name in legacy_keys_for(key):
                value = str((legacy or {}).get(name, "") or "")
                if value:
                    break
            if not value:
                continue
            try:
                self.set(key, value)
            except SecretStoreUnavailable as exc:
                logger.warning("本机不能加密，密钥迁移跳过: {}", exc)
                break
            migrated.append(key)
        if migrated:
            logger.info("已迁移 {} 条密钥到 Windows DPAPI: {}", len(migrated), ", ".join(migrated))
        return migrated


_DEFAULT: SecretStore | None = None


def default_store() -> SecretStore:
    """进程内共用一个仓库，避免反复读文件。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SecretStore()
    return _DEFAULT
