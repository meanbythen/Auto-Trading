"""参数曲面只读优化 trials，不重跑回测。"""

from __future__ import annotations

from pathlib import Path

from src.backtest.surface import build_surface, numeric_param_keys, save_surface_csv
from src.backtest.stress import joint_perturb_patches


def _trials() -> list[dict[str, object]]:
    return [
        {"params": {"entry_bars": 20, "atr_mult": 2.0}, "score": 0.5},
        {"params": {"entry_bars": 20, "atr_mult": 3.0}, "score": 0.7},
        {"params": {"entry_bars": 40, "atr_mult": 2.0}, "score": 0.9},
        {"params": {"entry_bars": 40, "atr_mult": 3.0}, "score": 0.1},
        {"params": {"entry_bars": 40, "atr_mult": 3.0}, "score": 0.8},
    ]


def test_numeric_param_keys_keep_first_seen_order() -> None:
    keys = numeric_param_keys(_trials())
    assert keys == ["entry_bars", "atr_mult"]


def test_build_surface_takes_best_score_per_cell() -> None:
    surface = build_surface(_trials(), "entry_bars", "atr_mult")
    assert surface["ok"] is True
    assert surface["x_values"] == [20.0, 40.0]
    assert surface["y_values"] == [2.0, 3.0]
    assert len(surface["cells"]) == 4
    assert surface["best"]["score"] == 0.9
    lookup = {(cell["x"], cell["y"]): cell["score"] for cell in surface["cells"]}
    assert lookup[(40.0, 3.0)] == 0.8


def test_build_surface_rejects_missing_axis() -> None:
    surface = build_surface(_trials(), "entry_bars", "atr_mult_x")
    assert surface["ok"] is False
    assert "不足两档" in surface["error"]


def test_save_surface_csv_keeps_grid_shape(tmp_path: Path) -> None:
    surface = build_surface(_trials(), "entry_bars", "atr_mult")
    path = save_surface_csv(surface, tmp_path / "surface.csv")
    rows = path.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("atr_mult\\entry_bars")
    assert len(rows) == 3


def test_joint_perturb_patches_builds_four_corners() -> None:
    cfg = {
        "strategy": {
            "engine": "trend_follow",
            "trend_follow": {"entry_bars": 20, "exit_bars": 10, "atr_mult": 2.0},
        }
    }
    patches = joint_perturb_patches(cfg, ["entry_bars", "atr_mult"])
    assert len(patches) == 4
    assert all(set(patch) == {"entry_bars", "atr_mult"} for patch in patches)
    assert {patch["entry_bars"] for patch in patches} == {18, 22}
    assert {patch["atr_mult"] for patch in patches} == {1.8, 2.2}


def test_joint_perturb_patches_need_two_params() -> None:
    cfg = {"strategy": {"engine": "dual_ma", "dual_ma": {"short_period": 20}}}
    assert joint_perturb_patches(cfg, ["short_period"]) == []
