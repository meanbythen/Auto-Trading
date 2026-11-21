"""批次3 研究治理数据层测试：覆盖 TR-1.1 ~ TR-1.6，全部用 tmp_path，不碰真实 data/。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.research import governance as gov


def _write_manifest(root: Path, exp_id: str, **details: object) -> dict:
    exp_dir = Path(root) / "data" / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "experiment_id": exp_id,
        "kind": "backtest",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "details": dict(details),
    }
    (exp_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest


def _set_policy(root: Path, **fields: object) -> None:
    path = gov.governance_root(root) / "policy.json"
    policy = gov.ensure_policy(root)
    policy.update(fields)
    path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------ TR-1.1 假设 ------------------------------


def test_hypothesis_register_list_get_update(tmp_path):
    h1 = gov.register_hypothesis(tmp_path, title="t1", statement="s1", symbol="rb", owner="alice")
    assert h1["id"].startswith("H-") and h1["id"].endswith("-001")
    assert h1["status"] == "open"
    assert h1["linked_experiments"] == []
    assert h1["created_at"] == h1["updated_at"]

    h2 = gov.register_hypothesis(tmp_path, title="t2", statement="s2")
    assert h2["id"].endswith("-002")  # 同日自增

    assert gov.list_hypotheses(tmp_path) == [h1, h2]
    assert gov.get_hypothesis(tmp_path, h1["id"]) == h1
    assert gov.get_hypothesis(tmp_path, "nope") is None

    updated = gov.update_hypothesis(tmp_path, h1["id"], status="rejected", conclusion="不行")
    assert updated["status"] == "rejected"
    assert updated["conclusion"] == "不行"
    assert updated["updated_at"] >= h1["updated_at"]
    assert gov.get_hypothesis(tmp_path, h1["id"])["status"] == "rejected"

    # 找不到返回 None，且不抛异常
    assert gov.update_hypothesis(tmp_path, "missing", status="x") is None


def test_hypothesis_id_format_starts_at_001(tmp_path):
    today = datetime.now().strftime("%Y%m%d")
    h = gov.register_hypothesis(tmp_path, title="x", statement="y")
    assert h["id"] == f"H-{today}-001"


def test_update_hypothesis_only_updates_non_none(tmp_path):
    h = gov.register_hypothesis(tmp_path, title="t", statement="s")
    gov.update_hypothesis(tmp_path, h["id"], status="accepted")  # conclusion=None 不动
    got = gov.get_hypothesis(tmp_path, h["id"])
    assert got["status"] == "accepted"
    assert "conclusion" not in got


# ------------------------------ TR-1.2 决策 JSONL ------------------------------


def test_decisions_append_not_overwrite_and_filter(tmp_path):
    d1 = gov.append_decision(tmp_path, decision_type="go_live", decision="上线", strategy="dual_ma", symbol="rb")
    d2 = gov.append_decision(
        tmp_path, decision_type="select_params", decision="选参", strategy="dual_ma", symbol="rb"
    )
    d3 = gov.append_decision(tmp_path, decision_type="retire", decision="退役", strategy="turtle", symbol="ag")

    path = gov.governance_root(tmp_path) / "decisions.jsonl"
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 3

    all_decisions = gov.list_decisions(tmp_path)
    assert all_decisions == [d1, d2, d3]

    assert gov.list_decisions(tmp_path, strategy="dual_ma") == [d1, d2]
    assert gov.list_decisions(tmp_path, symbol="ag") == [d3]
    assert gov.list_decisions(tmp_path, strategy="dual_ma", symbol="rb") == [d1, d2]
    assert gov.list_decisions(tmp_path, strategy="none") == []
    assert gov.list_decisions(tmp_path, limit=2) == [d2, d3]

    # 空文件 / 缺失文件返回 []
    assert gov.list_decisions(tmp_path)  # 有数据
    empty_root = tmp_path / "empty"
    assert gov.list_decisions(empty_root) == []


def test_decision_id_increments(tmp_path):
    today = datetime.now().strftime("%Y%m%d")
    d1 = gov.append_decision(tmp_path, decision_type="x", decision="y")
    d2 = gov.append_decision(tmp_path, decision_type="x", decision="y")
    assert d1["id"] == f"D-{today}-001"
    assert d2["id"] == f"D-{today}-002"


# ------------------------------ TR-1.3 上线策略 + 变更 ------------------------------


def test_live_strategy_register_retire_list(tmp_path):
    s1 = gov.register_live_strategy(tmp_path, strategy="dual_ma", symbol="rb", experiment_id="E1")
    assert s1["status"] == "running"
    assert gov.list_live_strategies(tmp_path) == [s1]

    # 同 strategy+symbol 去重覆盖
    s2 = gov.register_live_strategy(tmp_path, strategy="dual_ma", symbol="rb", experiment_id="E2")
    assert gov.list_live_strategies(tmp_path) == [s2]
    assert gov.list_live_strategies(tmp_path, include_retired=True) == [s2]

    retired = gov.retire_live_strategy(tmp_path, "dual_ma", "rb", reason="表现差")
    assert retired is not None
    assert retired["status"] == "retired"
    assert retired["retire_reason"] == "表现差"
    assert gov.list_live_strategies(tmp_path) == []
    assert gov.list_live_strategies(tmp_path, include_retired=True) == [retired]

    assert gov.retire_live_strategy(tmp_path, "missing", "zz") is None


def test_changes_append_and_list(tmp_path):
    c1 = gov.append_change(tmp_path, strategy="dual_ma", symbol="rb", what="改参数", why="优化")
    c2 = gov.append_change(tmp_path, strategy="dual_ma", symbol="rb", what="再改")
    c3 = gov.append_change(tmp_path, strategy="turtle", symbol="ag", what="x")
    assert gov.list_changes(tmp_path) == [c1, c2, c3]
    assert gov.list_changes(tmp_path, strategy="dual_ma") == [c1, c2]
    assert gov.list_changes(tmp_path, symbol="rb") == [c1, c2]
    assert gov.list_changes(tmp_path, strategy="dual_ma", symbol="rb", limit=1) == [c2]
    assert c1["effective_at"] == c1["ts"]


# ------------------------------ TR-1.4 冻结 ------------------------------


def test_is_frozen_within_and_beyond_window(tmp_path):
    at = date(2026, 11, 21)
    gov.append_change(tmp_path, strategy="s", symbol="rb", what="a", effective_at=(at - timedelta(days=5)).isoformat())
    frozen, reason = gov.is_frozen(tmp_path, "s", "rb", at=at)
    assert frozen is True
    assert "5 天" in reason and "14 天" in reason

    gov.append_change(tmp_path, strategy="s", symbol="ag", what="b", effective_at=(at - timedelta(days=20)).isoformat())
    frozen, reason = gov.is_frozen(tmp_path, "s", "ag", at=at)
    assert frozen is False
    assert reason == "未冻结"

    # 无变更记录
    frozen, reason = gov.is_frozen(tmp_path, "s", "zn", at=at)
    assert frozen is False
    assert reason == "无变更记录"


def test_is_frozen_respects_policy_freeze_days(tmp_path):
    at = date(2026, 11, 21)
    gov.append_change(tmp_path, strategy="s", symbol="rb", what="a", effective_at=(at - timedelta(days=5)).isoformat())
    _set_policy(tmp_path, freeze_days=3)
    frozen, _ = gov.is_frozen(tmp_path, "s", "rb", at=at)
    assert frozen is False  # 5 >= 3

    _set_policy(tmp_path, freeze_days=10)
    frozen, _ = gov.is_frozen(tmp_path, "s", "rb", at=at)
    assert frozen is True  # 5 < 10


def test_next_reoptimization_window_monthly(tmp_path):
    w = gov.next_reoptimization_window(tmp_path, at=date(2026, 11, 21))
    assert w["cadence"] == "monthly"
    assert w["next_start"] == "2026-12-01"

    w = gov.next_reoptimization_window(tmp_path, at=date(2026, 12, 15))
    assert w["next_start"] == "2027-01-01"

    _set_policy(tmp_path, reoptimize_cadence="weekly")
    w = gov.next_reoptimization_window(tmp_path, at=date(2026, 11, 21))
    assert w["cadence"] == "weekly"
    assert w["next_start"] == ""


def test_ensure_policy_default_and_persist(tmp_path):
    policy = gov.ensure_policy(tmp_path)
    assert policy == {"freeze_days": 14, "reoptimize_cadence": "monthly", "reoptimize_day": 1}
    # 第二次读返回已落盘的
    assert gov.ensure_policy(tmp_path) == policy
    path = gov.governance_root(tmp_path) / "policy.json"
    assert json.loads(path.read_text(encoding="utf-8")) == policy


# ------------------------------ 实验关联 / 失败 ------------------------------


def test_link_experiment_to_hypothesis_both_sides(tmp_path):
    _write_manifest(tmp_path, "EXP1", symbol="rb")
    h = gov.register_hypothesis(tmp_path, title="t", statement="s")

    gov.link_experiment_to_hypothesis(tmp_path, "EXP1", h["id"])

    manifest = json.loads(
        (tmp_path / "data" / "experiments" / "EXP1" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["details"]["hypothesis_id"] == h["id"]
    # 索引已刷新（含该实验）
    index = json.loads((tmp_path / "data" / "experiments" / "index.json").read_text(encoding="utf-8"))
    ids = [r["experiment_id"] for r in index["experiments"]]
    assert "EXP1" in ids

    hyp = gov.get_hypothesis(tmp_path, h["id"])
    assert "EXP1" in hyp["linked_experiments"]

    # 重复关联去重
    gov.link_experiment_to_hypothesis(tmp_path, "EXP1", h["id"])
    assert gov.get_hypothesis(tmp_path, h["id"])["linked_experiments"] == ["EXP1"]


def test_link_experiment_missing_manifest_only_hypothesis_side(tmp_path):
    h = gov.register_hypothesis(tmp_path, title="t", statement="s")
    # 不抛异常；假设侧被更新
    gov.link_experiment_to_hypothesis(tmp_path, "NOPE", h["id"])
    assert gov.get_hypothesis(tmp_path, h["id"])["linked_experiments"] == ["NOPE"]


def test_mark_failure_and_list_failed(tmp_path):
    _write_manifest(tmp_path, "EXP_FAIL", symbol="rb")
    gov.mark_experiment_failure(tmp_path, "EXP_FAIL", "数据缺失")
    manifest = json.loads(
        (tmp_path / "data" / "experiments" / "EXP_FAIL" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["details"]["failure_reason"] == "数据缺失"

    failed = gov.list_failed_experiments(tmp_path)
    assert any(f["experiment_id"] == "EXP_FAIL" and f["failure_reason"] == "数据缺失" for f in failed)

    # 直接写一条 failed_count>0 的索引行（无 manifest）
    index_path = tmp_path / "data" / "experiments" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["experiments"].append(
        {
            "experiment_id": "EXP_OLD",
            "kind": "backtest",
            "symbol": "ag",
            "created_at": "2026-01-01T00:00:00+08:00",
            "failed_count": 3,
        }
    )
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    failed = gov.list_failed_experiments(tmp_path)
    exp_old = next(f for f in failed if f["experiment_id"] == "EXP_OLD")
    assert exp_old["failed_count"] == 3

    # 无 index 返回 []
    assert gov.list_failed_experiments(tmp_path / "nope") == []


# ------------------------------ TR-1.5 trace ------------------------------


def test_trace_missing_does_not_raise(tmp_path):
    trace = gov.trace_strategy(tmp_path, "ghost", "xx")
    assert trace["hypothesis"] == "未记录"
    assert trace["experiments"] == []
    assert trace["out_of_sample"] == "未记录"
    assert trace["release_sheet"] == "未记录"
    assert trace["live_strategy"] == "未记录"
    assert trace["changes"] == []
    assert trace["frozen"] == (False, "无变更记录")

    lines = gov.format_trace_lines(trace)
    assert any("未记录" in ln for ln in lines)
    # 每个环节都有一行
    for key in ("假设", "实验", "样本外", "评审单", "上线策略", "变更", "冻结"):
        assert any(ln.startswith(f"{key}：") for ln in lines)


def test_trace_populated(tmp_path):
    h = gov.register_hypothesis(tmp_path, title="动量有效", statement="...", symbol="rb")
    _write_manifest(tmp_path, "E1", symbol="rb")
    gov.link_experiment_to_hypothesis(tmp_path, "E1", h["id"])
    gov.register_live_strategy(tmp_path, strategy="dual_ma", symbol="rb", experiment_id="E1")
    gov.append_change(tmp_path, strategy="dual_ma", symbol="rb", what="改参数", why="优化")

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "release_sheet.json").write_text(
        json.dumps(
            {
                "symbol": "rb",
                "engine": "dual_ma",
                "generated_at": "2026-11-21 10:00",
                "decision": "允许小资金挂机",
                "verdict": "已签字：允许小资金挂机",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "release_sheet.md").write_text("# 评审单", encoding="utf-8")

    trace = gov.trace_strategy(tmp_path, "dual_ma", "rb")
    assert isinstance(trace["hypothesis"], dict)
    assert trace["hypothesis"]["id"] == h["id"]
    assert any(e["experiment_id"] == "E1" for e in trace["experiments"])
    assert "允许小资金挂机" in trace["out_of_sample"]
    assert isinstance(trace["release_sheet"], dict)
    assert trace["release_sheet"]["decision"] == "允许小资金挂机"
    assert isinstance(trace["live_strategy"], dict)
    assert len(trace["changes"]) == 1
    assert trace["frozen"][0] is True  # 今天变更 < 14 天

    lines = gov.format_trace_lines(trace)
    text = "\n".join(lines)
    assert h["id"] in text
    assert "允许小资金挂机" in text
    assert "冻结：是" in text


def test_trace_release_sheet_symbol_mismatch(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "release_sheet.json").write_text(
        json.dumps({"symbol": "ag", "generated_at": "x", "decision": "y"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "release_sheet.md").write_text("# 评审单", encoding="utf-8")
    trace = gov.trace_strategy(tmp_path, "s", "rb")  # symbol 不匹配
    assert trace["release_sheet"] == "未记录"
    assert trace["out_of_sample"] == "未记录"


# ------------------------------ TR-1.6 损坏 / 缺失兜底 ------------------------------


def test_corrupt_files_fall_back(tmp_path):
    root = gov.governance_root(tmp_path)
    (root / "hypotheses.json").write_text("{not json", encoding="utf-8")
    (root / "policy.json").write_text("garbage", encoding="utf-8")
    (root / "live_strategies.json").write_text("[", encoding="utf-8")
    (root / "decisions.jsonl").write_text("{bad}\n{\"ok\":1}\n", encoding="utf-8")
    (root / "changes.jsonl").write_text("notjson\n", encoding="utf-8")

    assert gov.list_hypotheses(tmp_path) == []
    assert gov.get_hypothesis(tmp_path, "x") is None
    assert gov.ensure_policy(tmp_path) == {"freeze_days": 14, "reoptimize_cadence": "monthly", "reoptimize_day": 1}
    assert gov.list_live_strategies(tmp_path) == []
    assert gov.list_decisions(tmp_path) == [{"ok": 1}]
    assert gov.list_changes(tmp_path) == []
    assert gov.is_frozen(tmp_path, "s", "rb") == (False, "无变更记录")
    # 损坏 index 不抛
    exp_index = tmp_path / "data" / "experiments" / "index.json"
    exp_index.parent.mkdir(parents=True, exist_ok=True)
    exp_index.write_text("{{{", encoding="utf-8")
    assert gov.list_failed_experiments(tmp_path) == []


def test_empty_root_all_defaults(tmp_path):
    # 完全空目录：所有读都不抛
    empty = tmp_path / "virgin"
    assert gov.list_hypotheses(empty) == []
    assert gov.ensure_policy(empty)["freeze_days"] == 14
    assert gov.list_decisions(empty) == []
    assert gov.list_changes(empty) == []
    assert gov.list_live_strategies(empty) == []
    assert gov.list_failed_experiments(empty) == []
    assert gov.trace_strategy(empty, "s", "x")["hypothesis"] == "未记录"
    assert gov.next_reoptimization_window(empty)["next_start"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
