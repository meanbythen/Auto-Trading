"""策略插件清单：内置注册表自动生成，插件目录按文件自动发现。

约定：
1. 内置策略的清单从 factory.REGISTRY 现算，不另抄一份；
2. 新策略只需在本目录放一个 .py，模块里导出 SPEC（dict 或 StrategySpec）；
3. 任何插件加载失败都只记 warning 并跳过，调用方继续用内置 REGISTRY；
4. 参数 schema 默认取 params.ENGINE_SECTION_DEFAULTS，插件可在 SPEC.params 里补充。

本文件以下划线开头，扫描插件时会跳过自己。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from ..factory import (
    ENGINE_ALIASES,
    ENGINE_AUDIT_MODES,
    ENGINE_LABELS,
    ENGINE_SHORT_LABELS,
    PRIMARY_ENGINES,
    REGISTRY,
    normalize_engine,
)
from ..params import DEFAULT_ENGINE, ENGINE_SECTION_DEFAULTS
from ..rule_dsl import rule_engine_stem

PLUGIN_DIR = Path(__file__).resolve().parent
BUILTIN_VERSION = "builtin-1"

PluginFactory = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class StrategySpec:
    """一套策略的清单项：名字、工厂、参数 schema 和版本。"""

    code: str
    label: str
    short_label: str
    factory: PluginFactory | None
    section: str
    audit_mode: str | None
    version: str
    source: str
    params: dict[str, Any] = field(default_factory=dict)

    def schema(self) -> list[dict[str, Any]]:
        """参数表：先取该策略小节的出厂默认，再用 SPEC.params 覆盖/追加。"""
        merged: dict[str, Any] = dict(ENGINE_SECTION_DEFAULTS.get(self.section) or {})
        merged.update(self.params)
        return [_schema_row(key, value) for key, value in merged.items()]


def _type_name(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "str"


def _schema_row(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and ("type" in value or "default" in value):
        return {
            "key": str(key),
            "type": str(value.get("type") or _type_name(value.get("default"))),
            "default": value.get("default"),
            "required": bool(value.get("required", False)),
        }
    return {
        "key": str(key),
        "type": _type_name(value),
        "default": value,
        "required": False,
    }


def plugins_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """配置里 strategy.plugins_enabled 默认 true；写 false 时只用内置注册表。"""
    strategy = (cfg or {}).get("strategy")
    if not isinstance(strategy, dict) or "plugins_enabled" not in strategy:
        return True
    raw = strategy.get("plugins_enabled")
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", "关", "关闭"}
    return bool(raw)


def _builtin_specs() -> dict[str, StrategySpec]:
    specs: dict[str, StrategySpec] = {}
    for code in PRIMARY_ENGINES:
        label = ENGINE_LABELS.get(code, code)
        specs[code] = StrategySpec(
            code=code,
            label=label,
            short_label=ENGINE_SHORT_LABELS.get(code, label),
            factory=REGISTRY.get(code),
            section=code,
            audit_mode=ENGINE_AUDIT_MODES.get(code),
            version=BUILTIN_VERSION,
            source="builtin",
        )
    return specs


def _plugin_files(directory: str | Path | None) -> list[Path]:
    root = Path(directory) if directory is not None else PLUGIN_DIR
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.py") if not path.name.startswith("_"))


def _coerce_spec(raw: Any, path: Path) -> StrategySpec:
    if isinstance(raw, StrategySpec):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("插件缺少 SPEC")
    code = str(raw.get("code") or path.stem).strip().lower()
    if not code:
        raise ValueError("插件 SPEC.code 为空")
    factory = raw.get("factory")
    if not callable(factory):
        raise ValueError(f"插件 {code} 缺少可调用的 factory")
    version = str(raw.get("version") or "").strip()
    if not version:
        raise ValueError(f"插件 {code} 缺少 version")
    label = str(raw.get("label") or code).strip() or code
    section = str(raw.get("section") or code).strip() or code
    params = raw.get("params")
    return StrategySpec(
        code=code,
        label=label,
        short_label=str(raw.get("short_label") or label).strip() or label,
        factory=factory,
        section=section,
        audit_mode=raw.get("audit_mode"),
        version=version,
        source="plugin",
        params=dict(params) if isinstance(params, dict) else {},
    )


def _load_plugin(path: Path) -> StrategySpec:
    module_name = f"src.strategy.plugins._user_{path.stem}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ValueError(f"无法加载插件文件 {path.name}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return _coerce_spec(getattr(module, "SPEC", None), path)


def discover_specs(
    directory: str | Path | None = None,
    *,
    enabled: bool = True,
) -> dict[str, StrategySpec]:
    """内置策略 + 插件目录，返回 code → StrategySpec；永不抛出。"""
    specs = _builtin_specs()
    if not enabled:
        return specs
    for path in _plugin_files(directory):
        try:
            spec = _load_plugin(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("策略插件加载失败，已跳过 {}: {}", path.name, exc)
            continue
        if spec.code in specs:
            logger.warning("策略插件 {} 与已登记策略重名，已跳过：{}", path.name, spec.code)
            continue
        if spec.code in ENGINE_ALIASES:
            logger.warning("策略插件 {} 使用了别名，已跳过：{}", path.name, spec.code)
            continue
        specs[spec.code] = spec
    return specs


def spec_for(code: str | None, *, enabled: bool = True) -> StrategySpec | None:
    key = str(code or "").strip().lower()
    if not key:
        return None
    key = ENGINE_ALIASES.get(key, key)
    return discover_specs(enabled=enabled).get(key)


def plugin_factory(code: str, cfg: dict[str, Any] | None = None) -> PluginFactory | None:
    """只返回插件来源的工厂；内置策略仍走 REGISTRY，避免重复。"""
    spec = spec_for(code, enabled=plugins_enabled(cfg))
    if spec is None or spec.source != "plugin":
        return None
    return spec.factory


def param_schema(code: str) -> list[dict[str, Any]]:
    spec = spec_for(code)
    return spec.schema() if spec else []


def _matches(value: Any, expected: str) -> bool:
    """宽松类型检查：数字字符串也当数字，避免旧配置被误报。"""
    if expected == "bool":
        return isinstance(value, bool)
    if expected == "int":
        if isinstance(value, bool):
            return False
        try:
            int(str(value).strip())
        except (TypeError, ValueError):
            return False
        return True
    if expected == "float":
        if isinstance(value, bool):
            return False
        try:
            float(str(value).strip())
        except (TypeError, ValueError):
            return False
        return True
    if expected == "str":
        return isinstance(value, (str, int, float, bool))
    if expected == "object":
        return isinstance(value, dict)
    return True


def validate_params(code: str, params: dict[str, Any] | None) -> list[str]:
    """只检查已知参数的类型；未知键不报错，不阻断策略创建。"""
    if not isinstance(params, dict):
        return []
    messages: list[str] = []
    for row in param_schema(code):
        key = row["key"]
        if key not in params:
            continue
        value = params[key]
        if value is None or value == "":
            continue
        if not _matches(value, str(row["type"])):
            messages.append(f"{code}.{key} 应为 {row['type']}，实际是 {type(value).__name__}")
    return messages


def strategy_identity(cfg: dict[str, Any] | None) -> dict[str, str]:
    """回测/实验用：策略代号、中文名、版本。认不出时版本留空。"""
    raw = str(((cfg or {}).get("strategy") or {}).get("engine") or DEFAULT_ENGINE).strip().lower()
    stem = rule_engine_stem(raw)
    if stem:
        return {
            "code": raw,
            "label": ENGINE_LABELS.get("rule_dsl", "自定义规则"),
            "version": "rule-file",
        }
    code = normalize_engine(raw) or raw
    spec = spec_for(code)
    if spec is None:
        return {"code": code, "label": ENGINE_LABELS.get(code, code), "version": ""}
    return {"code": spec.code, "label": spec.label, "version": spec.version}


__all__ = [
    "BUILTIN_VERSION",
    "PLUGIN_DIR",
    "StrategySpec",
    "discover_specs",
    "param_schema",
    "plugin_factory",
    "plugins_enabled",
    "spec_for",
    "strategy_identity",
    "validate_params",
]
