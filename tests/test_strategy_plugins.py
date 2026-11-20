"""P3 策略插件：清单、自动发现、坏插件回退、版本留痕。"""

from __future__ import annotations

from pathlib import Path

from src.strategy import factory, plugins

_PLUGIN = """
from src.strategy.factory import REGISTRY

SPEC = {
    "code": "my_break",
    "label": "我的突破",
    "short_label": "我突破",
    "factory": REGISTRY["daily_body"],
    "version": "9.9.9",
    "params": {"lookback": {"type": "int", "default": 20}},
}
"""


def test_builtin_specs_cover_primary_engines() -> None:
    specs = plugins.discover_specs()
    for code in factory.PRIMARY_ENGINES:
        spec = specs[code]
        assert spec.label
        assert spec.version == plugins.BUILTIN_VERSION
        if factory.ENGINE_SECTION_DEFAULTS.get(code):
            assert plugins.param_schema(code), code


def test_plugin_discovery_factory_and_sanitize(tmp_path: Path, monkeypatch) -> None:
    plugin_file = tmp_path / "my_break.py"
    plugin_file.write_text(_PLUGIN, encoding="utf-8")

    specs = plugins.discover_specs(tmp_path)
    assert specs["my_break"].version == "9.9.9"
    assert specs["my_break"].label == "我的突破"

    monkeypatch.setattr(plugins, "PLUGIN_DIR", tmp_path)
    assert plugins.param_schema("my_break")[0]["key"] == "lookback"
    cfg = {"strategy": {"engine": "my_break"}}

    assert factory.normalize_engine("my_break") == "my_break"
    assert factory.sanitize_strategy_config(cfg)["strategy"]["engine"] == "my_break"

    strategy = factory.create_strategy(cfg)
    assert strategy.__class__ is factory.REGISTRY["daily_body"]


def test_broken_plugin_falls_back_to_builtin(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "no_factory.py").write_text('SPEC = {"code": "x", "version": "1"}\n', encoding="utf-8")

    specs = plugins.discover_specs(tmp_path)
    assert "daily_body" in specs
    assert "x" not in specs

    monkeypatch.setattr(plugins, "PLUGIN_DIR", tmp_path)
    strategy = factory.create_strategy({"strategy": {"engine": "my_break"}})
    assert strategy.__class__ is factory.REGISTRY[factory.DEFAULT_ENGINE]


def test_plugins_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "my_break.py").write_text(_PLUGIN, encoding="utf-8")
    monkeypatch.setattr(plugins, "PLUGIN_DIR", tmp_path)

    assert "my_break" not in plugins.discover_specs(enabled=False)
    cfg = {"strategy": {"engine": "my_break", "plugins_enabled": False}}
    strategy = factory.create_strategy(cfg)
    assert strategy.__class__ is factory.REGISTRY[factory.DEFAULT_ENGINE]


def test_strategy_identity_has_version() -> None:
    ident = factory.strategy_identity({"strategy": {"engine": "daily_body"}})
    assert ident["code"] == "daily_body"
    assert ident["label"] == factory.ENGINE_LABELS["daily_body"]
    assert ident["version"] == plugins.BUILTIN_VERSION
