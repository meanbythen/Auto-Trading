"""批次3 Task 2：实验索引带上假设编号与失败原因，端到端验证治理模块关联/失败标记。

覆盖 TR：索引含 hypothesis_id / failure_reason 新字段；旧 manifest 缺字段回退空串、
不阻塞 rebuild_index；governance.link/mark 写回 manifest 并刷新索引后两端一致；
list_failed_experiments 能经由索引行返回失败原因。全部用 tmp_path 造 data/，不碰真实数据。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.backtest import experiment as exp_mod
from src.research import governance as gov


def _data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _experiments_dir(tmp_path: Path) -> Path:
    return _data_root(tmp_path) / "experiments"


def _make_manifest(tmp_path: Path, *, details: dict | None = None) -> tuple[str, dict]:
    """用 new_experiment 造一个 manifest 并落盘，返回 (experiment_id, manifest)。"""
    manifest = exp_mod.new_experiment("backtest", {"symbol": "rb"}, details=details)
    exp_id = manifest["experiment_id"]
    exp_dir = _experiments_dir(tmp_path) / exp_id
    exp_mod._atomic_json(exp_dir / "manifest.json", manifest)
    return exp_id, manifest


def _read_index(tmp_path: Path) -> dict:
    return json.loads((_experiments_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))


def _index_row(tmp_path: Path, exp_id: str) -> dict:
    for row in _read_index(tmp_path).get("experiments", []):
        if row.get("experiment_id") == exp_id:
            return row
    raise AssertionError(f"index 中找不到实验 {exp_id}")


def _read_manifest(tmp_path: Path, exp_id: str) -> dict:
    return json.loads((_experiments_dir(tmp_path) / exp_id / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) 旧实验兼容：新字段缺省空串，_update_index / rebuild_index 不阻塞
# ---------------------------------------------------------------------------


def test_index_row_defaults_empty_for_legacy_manifest(tmp_path):
    exp_id, manifest = _make_manifest(tmp_path)
    # 旧实验：details 里没有 hypothesis_id / failure_reason
    assert "hypothesis_id" not in manifest["details"]
    assert "failure_reason" not in manifest["details"]

    exp_mod._update_index(_experiments_dir(tmp_path), manifest)
    row = _index_row(tmp_path, exp_id)
    assert row["hypothesis_id"] == ""
    assert row["failure_reason"] == ""
    # 既有字段顺序与值不受影响
    assert row["experiment_id"] == exp_id
    assert row["kind"] == "backtest"
    assert row["report"] == f"{exp_id}/report.json"


def test_rebuild_index_tolerates_legacy_manifests_and_counts(tmp_path):
    ids = [_make_manifest(tmp_path)[0] for _ in range(3)]
    # 混一个完全没有 details 键的旧 manifest（模拟历史数据）
    legacy_dir = _experiments_dir(tmp_path) / "EXP_LEGACY"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "experiment_id": "EXP_LEGACY",
                "kind": "backtest",
                "created_at": "2026-01-01T00:00:00+08:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    count = exp_mod.rebuild_index(_data_root(tmp_path))  # 不抛异常
    assert count == 4
    rows = {r["experiment_id"]: r for r in _read_index(tmp_path)["experiments"]}
    for exp_id in ids:
        assert rows[exp_id]["hypothesis_id"] == ""
        assert rows[exp_id]["failure_reason"] == ""
    # 完全没有 details 的旧 manifest 字段也回退空串
    assert rows["EXP_LEGACY"]["hypothesis_id"] == ""
    assert rows["EXP_LEGACY"]["failure_reason"] == ""


# ---------------------------------------------------------------------------
# 2) link_experiment_to_hypothesis：manifest + 索引 + 假设侧 三端一致
# ---------------------------------------------------------------------------


def test_link_experiment_updates_manifest_index_and_hypothesis(tmp_path):
    exp_id, _ = _make_manifest(tmp_path)
    exp_mod._update_index(_experiments_dir(tmp_path), _read_manifest(tmp_path, exp_id))
    # 基线：关联前索引行 hypothesis_id 为空
    assert _index_row(tmp_path, exp_id)["hypothesis_id"] == ""

    hyp = gov.register_hypothesis(tmp_path, title="动量有效", statement="...", symbol="rb")
    assert hyp["id"].startswith("H-") and hyp["id"].endswith("-001")
    gov.link_experiment_to_hypothesis(tmp_path, exp_id, hyp["id"])

    # manifest 写回正确
    assert _read_manifest(tmp_path, exp_id)["details"]["hypothesis_id"] == hyp["id"]
    # 刷新后的索引行含 hypothesis_id
    assert _index_row(tmp_path, exp_id)["hypothesis_id"] == hyp["id"]
    # 假设侧 linked_experiments 去重含 exp_id
    assert exp_id in gov.get_hypothesis(tmp_path, hyp["id"])["linked_experiments"]

    # 重建索引也保留该字段（不丢）
    exp_mod.rebuild_index(_data_root(tmp_path))
    assert _index_row(tmp_path, exp_id)["hypothesis_id"] == hyp["id"]


# ---------------------------------------------------------------------------
# 3) mark_experiment_failure：manifest + 索引 + list_failed 三端一致
# ---------------------------------------------------------------------------


def test_mark_failure_updates_manifest_index_and_list(tmp_path):
    exp_id, _ = _make_manifest(tmp_path)
    exp_mod._update_index(_experiments_dir(tmp_path), _read_manifest(tmp_path, exp_id))
    assert _index_row(tmp_path, exp_id)["failure_reason"] == ""

    gov.mark_experiment_failure(tmp_path, exp_id, "样本外收益为负")

    assert _read_manifest(tmp_path, exp_id)["details"]["failure_reason"] == "样本外收益为负"
    assert _index_row(tmp_path, exp_id)["failure_reason"] == "样本外收益为负"

    failed = gov.list_failed_experiments(tmp_path)
    match = next((f for f in failed if f["experiment_id"] == exp_id), None)
    assert match is not None
    assert match["failure_reason"] == "样本外收益为负"


# ---------------------------------------------------------------------------
# 4) 关联 + 失败串联：索引同时带 hypothesis_id 与 failure_reason，rebuild 不丢
# ---------------------------------------------------------------------------


def test_link_then_mark_both_fields_in_index(tmp_path):
    exp_id, _ = _make_manifest(tmp_path)
    exp_mod._update_index(_experiments_dir(tmp_path), _read_manifest(tmp_path, exp_id))
    hyp = gov.register_hypothesis(tmp_path, title="t", statement="s")
    gov.link_experiment_to_hypothesis(tmp_path, exp_id, hyp["id"])
    gov.mark_experiment_failure(tmp_path, exp_id, "样本外收益为负")

    row = _index_row(tmp_path, exp_id)
    assert row["hypothesis_id"] == hyp["id"]
    assert row["failure_reason"] == "样本外收益为负"

    # rebuild 后两字段都保留
    assert exp_mod.rebuild_index(_data_root(tmp_path)) >= 1
    row = _index_row(tmp_path, exp_id)
    assert row["hypothesis_id"] == hyp["id"]
    assert row["failure_reason"] == "样本外收益为负"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-q"])
