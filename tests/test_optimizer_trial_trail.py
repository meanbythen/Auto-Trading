"""批次1可信度底座：优化试验全量留痕 + 索引计数 + 旧实验兼容。"""

from __future__ import annotations

import json

from src.backtest.experiment import rebuild_index
from src.backtest.optimizer import OptimizationResult, save_optimization_report


def _make_trials(n: int, n_fail: int) -> list[dict]:
    trials: list[dict] = []
    for i in range(n):
        if i < n_fail:
            trials.append({"params": {"n": i}, "score": -999.0, "error": "grid error"})
        else:
            trials.append({
                "params": {"n": i},
                "score": float(i),
                "performance": {"net_pnl": float(i), "trades": 10},
            })
    return trials


def test_all_trials_persisted_and_indexed(tmp_path):
    trials = _make_trials(80, n_fail=5)
    result = OptimizationResult(
        symbol="rb",
        best_params={"n": 1},
        best_score=79.0,
        best_performance={"net_pnl": 79.0, "trades": 10},
        trials=trials,
        best_pnls=[10.0, -5.0, 20.0],
        engine="test",
        depth="日线",
    )
    out = tmp_path / "opt_report.json"
    save_optimization_report(result, out)

    exp_dir = tmp_path / "experiments"
    report_files = list(exp_dir.glob("*/report.json"))
    assert len(report_files) == 1, "应归档一份 report.json"
    report = json.loads(report_files[0].read_text(encoding="utf-8"))
    # 全量留痕：不再截断到 30。
    assert len(report["trials"]) == 80
    assert report["best_pnls"] == [10.0, -5.0, 20.0]

    index = json.loads((exp_dir / "index.json").read_text(encoding="utf-8"))
    row = index["experiments"][0]
    assert row["trial_count"] == 80
    assert row["failed_count"] == 5


def test_legacy_manifest_without_counts_does_not_crash(tmp_path):
    # 模拟旧实验：manifest 无 trial_count/failed_count，report 只有 30 条 trials。
    legacy_dir = tmp_path / "experiments" / "exp_legacy"
    legacy_dir.mkdir(parents=True)
    manifest = {
        "experiment_id": "exp_legacy",
        "kind": "optimization",
        "created_at": "2024-01-01 00:00:00",
        "details": {"symbol": "rb", "engine": "test"},  # 故意不含计数
        "code": {"version": "0.0.0", "git_commit": "deadbeef"},
        "config": {"sha256": "abc"},
        "data": {"files": []},
        "review_gate": {"snapshot_complete": True},
    }
    (legacy_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    old_trials = _make_trials(30, n_fail=2)
    (legacy_dir / "report.json").write_text(
        json.dumps({"trials": old_trials}, ensure_ascii=False), encoding="utf-8"
    )

    # 重建索引不得抛异常。
    rebuilt = rebuild_index(str(tmp_path))
    assert rebuilt > 0

    index = json.loads((tmp_path / "experiments" / "index.json").read_text(encoding="utf-8"))
    row = index["experiments"][0]
    # 回退路径：从 report.json 现场数出 30 条、2 条失败。
    assert row["trial_count"] == 30
    assert row["failed_count"] == 2
