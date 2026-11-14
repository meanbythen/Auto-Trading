"""配置保存：先写临时文件再原子替换；缺 config.yaml 时不覆盖示例模板。"""

from __future__ import annotations

import pytest
import yaml

from src.utils import config_loader


@pytest.fixture()
def _no_side_tables(monkeypatch):
    """保存只测写盘，不读品种表、不补策略默认值。"""
    monkeypatch.setattr(
        "src.risk.universe_table.apply_universe_table_to_cfg",
        lambda cfg, **kwargs: cfg,
    )
    monkeypatch.setattr("src.strategy.factory.sanitize_strategy_config", lambda cfg: None)


def test_save_config_writes_config_yaml_instead_of_the_example_template(tmp_path, monkeypatch, _no_side_tables):
    """升级前缺 config.yaml 时读到的是示例模板，保存必须落到正式配置，不能改模板。"""
    monkeypatch.setattr(config_loader, "_project_root", lambda: tmp_path)
    example = tmp_path / "config" / "config.example.yaml"
    example.parent.mkdir(parents=True)
    example.write_text("strategy:\n  engine: daily_body\n", encoding="utf-8")

    saved = config_loader.save_config(
        {"strategy": {"engine": "daily_body"}, "_config_path": str(example)}
    )

    assert saved == tmp_path / "config" / "config.yaml"
    assert yaml.safe_load(saved.read_text(encoding="utf-8"))["strategy"]["engine"] == "daily_body"
    assert example.read_text(encoding="utf-8") == "strategy:\n  engine: daily_body\n"
    assert list((tmp_path / "config").glob("*.tmp")) == []


def test_save_config_keeps_the_old_file_when_replace_fails(tmp_path, monkeypatch, _no_side_tables):
    """替换失败时原配置保持完整，临时文件不留在磁盘上。"""
    monkeypatch.setattr(config_loader, "_project_root", lambda: tmp_path)
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("old: 1\n", encoding="utf-8")

    def _boom(src, dst):
        raise OSError("磁盘忙")

    monkeypatch.setattr(config_loader.os, "replace", _boom)
    with pytest.raises(OSError):
        config_loader.save_config({"strategy": {"engine": "daily_body"}})

    assert path.read_text(encoding="utf-8") == "old: 1\n"
    assert list((tmp_path / "config").glob("*.tmp")) == []
