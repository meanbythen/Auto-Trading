"""品种保证金 Excel：生成、读取、写回配置。"""

from __future__ import annotations

from pathlib import Path

from src.risk.margin_table import (
    apply_margin_table_to_cfg,
    ensure_margin_table,
    read_margin_table,
    summarize_contracts,
    write_margin_table,
)


def test_write_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "品种保证金.xlsx"
    contracts = {
        "bu": {"name": "沥青", "margin": 8276.0, "multiplier": 10.0},
        "fu": {"name": "燃油", "margin": 11232.0, "multiplier": 10.0},
        "c": {"name": "玉米", "margin": 0.0, "multiplier": 10.0},
    }
    write_margin_table(path, contracts)
    loaded = read_margin_table(path)
    assert loaded["bu"]["margin"] == 8276.0
    assert loaded["fu"]["multiplier"] == 10.0
    assert loaded["c"]["margin"] == 0.0
    assert "bu:8276" in summarize_contracts(loaded)
    assert "c:" not in summarize_contracts(loaded)


def test_ensure_does_not_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "m.xlsx"
    write_margin_table(path, {"bu": {"name": "沥青", "margin": 100.0, "multiplier": 10.0}})
    ensure_margin_table(path, {"fu": {"name": "燃油", "margin": 200.0, "multiplier": 10.0}})
    loaded = read_margin_table(path)
    assert "bu" in loaded
    assert "fu" not in loaded


def test_apply_to_cfg(tmp_path: Path) -> None:
    path = tmp_path / "m.xlsx"
    write_margin_table(
        path,
        {
            "jm": {"name": "焦煤", "margin": 12690.0, "multiplier": 60.0},
        },
    )
    cfg: dict = {
        "_project_root": str(tmp_path),
        "risk": {
            "margin_table": str(path),
            "contracts": {"bu": {"name": "沥青", "margin": 1.0, "multiplier": 10.0}},
        },
    }
    out = apply_margin_table_to_cfg(cfg, path=path, create_if_missing=False)
    assert "bu" not in out
    assert out["jm"]["margin"] == 12690.0
    assert out["jm"]["multiplier"] == 60.0
    assert cfg["risk"]["contracts"]["jm"]["multiplier"] == 60.0
