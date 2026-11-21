"""批次3 研究治理：假设 / 决策 / 上线策略 / 变更 / 冻结 / 追踪链的数据层。

全部读写容错：缺文件、解析失败、空数据都不抛异常，返回空默认值。
数据落盘在 ``<root>/data/governance/``，与实验 / 评审单同属运行时数据，不碰交易链路。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

MISSING = "未记录"
_SCHEMA_VERSION = 1
_DEFAULT_POLICY = {"freeze_days": 14, "reoptimize_cadence": "monthly", "reoptimize_day": 1}


# ------------------------------ 基础工具 ------------------------------


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _today_compact() -> str:
    return datetime.now().strftime("%Y%m%d")


def _as_int(value: Any) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def governance_root(root: Path | str) -> Path:
    """返回 ``<root>/data/governance`` 并按需创建目录。"""
    path = Path(root) / "data" / "governance"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_json(path: Path, payload: Any) -> None:
    """原子写：temp + os.replace，避免半写损坏。复刻 ``experiment._atomic_json``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _load_json(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return rows
    return rows


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")


def _next_id(records: list[dict[str, Any]], prefix: str, date_str: str) -> str:
    needle = f"{prefix}{date_str}-"
    n = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or "")
        if rid.startswith(needle):
            try:
                n = max(n, int(rid[len(needle):]))
            except ValueError:
                pass
    return f"{needle}{n + 1:03d}"


def _to_date(value: Any) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return date.today()
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(s[:10])
            except ValueError:
                return date.today()
    return date.today()


def _to_datetime(value: Any) -> datetime:
    if value is None:
        return datetime.min
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return datetime.min
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromisoformat(s[:10])
            except ValueError:
                return datetime.min
    return datetime.min


# ------------------------------ policy ------------------------------


def ensure_policy(root: Path | str) -> dict[str, Any]:
    """读 policy.json；缺则创建默认并返回，损坏也返回默认（不覆盖损坏文件）。"""
    path = governance_root(root) / "policy.json"
    data = _load_json(path)
    if isinstance(data, dict) and data:
        return data
    default = dict(_DEFAULT_POLICY)
    if data is None:
        try:
            _atomic_json(path, default)
        except OSError:
            pass
    return default


# ------------------------------ hypotheses ------------------------------


def _hypotheses_path(root: Path | str) -> Path:
    return governance_root(root) / "hypotheses.json"


def _load_hypotheses(root: Path | str) -> list[dict[str, Any]]:
    data = _load_json(_hypotheses_path(root))
    rows = data.get("hypotheses") if isinstance(data, dict) else data
    return [row for row in (rows or []) if isinstance(row, dict)]


def _save_hypotheses(root: Path | str, hypotheses: list[dict[str, Any]]) -> None:
    try:
        _atomic_json(_hypotheses_path(root), {"schema_version": _SCHEMA_VERSION, "hypotheses": hypotheses})
    except OSError:
        pass


def register_hypothesis(
    root: Path | str,
    *,
    title: str,
    statement: str,
    symbol: str = "",
    period: str = "",
    param_ranges: Any = None,
    acceptance: str = "",
    owner: str | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    hypotheses = _load_hypotheses(root)
    hyp = {
        "id": _next_id(hypotheses, "H-", _today_compact()),
        "title": str(title or ""),
        "statement": str(statement or ""),
        "symbol": str(symbol or ""),
        "period": str(period or ""),
        "param_ranges": param_ranges if param_ranges is not None else {},
        "acceptance": str(acceptance or ""),
        "owner": owner,
        "status": "open",
        "linked_experiments": [],
        "created_at": now,
        "updated_at": now,
    }
    hypotheses.append(hyp)
    _save_hypotheses(root, hypotheses)
    return hyp


def list_hypotheses(root: Path | str) -> list[dict[str, Any]]:
    return _load_hypotheses(root)


def get_hypothesis(root: Path | str, hyp_id: str) -> dict[str, Any] | None:
    target = str(hyp_id or "")
    for hyp in _load_hypotheses(root):
        if str(hyp.get("id") or "") == target:
            return hyp
    return None


def update_hypothesis(
    root: Path | str,
    hyp_id: str,
    *,
    status: str | None = None,
    conclusion: str | None = None,
) -> dict[str, Any] | None:
    hypotheses = _load_hypotheses(root)
    target = str(hyp_id or "")
    for hyp in hypotheses:
        if str(hyp.get("id") or "") != target:
            continue
        if status is not None:
            hyp["status"] = str(status)
        if conclusion is not None:
            hyp["conclusion"] = str(conclusion)
        hyp["updated_at"] = _now_iso()
        _save_hypotheses(root, hypotheses)
        return hyp
    return None


# ------------------------------ experiments 关联 ------------------------------


def _experiments_dir(root: Path | str) -> Path:
    return Path(root) / "data" / "experiments"


def _manifest_path(root: Path | str, experiment_id: str) -> Path:
    return _experiments_dir(root) / str(experiment_id) / "manifest.json"


def _read_manifest(root: Path | str, experiment_id: str) -> dict[str, Any] | None:
    data = _load_json(_manifest_path(root, experiment_id))
    return data if isinstance(data, dict) else None


def _refresh_experiment_index(root: Path | str, manifest: dict[str, Any]) -> None:
    try:
        from src.backtest import experiment as _experiment  # 延迟导入，避免治理模块加载过重

        _experiment._update_index(_experiments_dir(root), manifest)
    except Exception:  # noqa: BLE001 - 治理是辅助，索引刷新失败不能阻断主流程
        pass


def link_experiment_to_hypothesis(root: Path | str, experiment_id: str, hyp_id: str) -> None:
    """双向关联：写 manifest.details.hypothesis_id + 刷新索引 + 假设 linked_experiments 去重。

    manifest 不存在时只更新假设侧，不抛异常。
    """
    manifest = _read_manifest(root, experiment_id)
    if manifest is not None:
        details = manifest.get("details")
        if not isinstance(details, dict):
            details = {}
            manifest["details"] = details
        details["hypothesis_id"] = str(hyp_id or "")
        try:
            _atomic_json(_manifest_path(root, experiment_id), manifest)
        except OSError:
            pass
        _refresh_experiment_index(root, manifest)

    hypotheses = _load_hypotheses(root)
    target = str(hyp_id or "")
    for hyp in hypotheses:
        if str(hyp.get("id") or "") != target:
            continue
        linked = hyp.get("linked_experiments")
        if not isinstance(linked, list):
            linked = []
        if experiment_id not in linked:
            linked.append(experiment_id)
        hyp["linked_experiments"] = linked
        hyp["updated_at"] = _now_iso()
        _save_hypotheses(root, hypotheses)
        break


def mark_experiment_failure(root: Path | str, experiment_id: str, reason: str) -> None:
    """写 manifest.details.failure_reason 并刷新索引；manifest 不存在则静默返回。"""
    manifest = _read_manifest(root, experiment_id)
    if manifest is None:
        return
    details = manifest.get("details")
    if not isinstance(details, dict):
        details = {}
        manifest["details"] = details
    details["failure_reason"] = str(reason or "")
    try:
        _atomic_json(_manifest_path(root, experiment_id), manifest)
    except OSError:
        pass
    _refresh_experiment_index(root, manifest)


def list_failed_experiments(root: Path | str) -> list[dict[str, Any]]:
    """扫描 index.json，返回 failed_count>0 或有 failure_reason 的实验。"""
    index = _load_json(_experiments_dir(root) / "index.json")
    rows = index.get("experiments") if isinstance(index, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fc = _as_int(row.get("failed_count"))
        failure_reason = str(row.get("failure_reason") or "")
        if not failure_reason:
            exp_id = row.get("experiment_id")
            manifest = _read_manifest(root, exp_id) if exp_id else None
            if manifest is not None:
                details = manifest.get("details")
                if isinstance(details, dict):
                    failure_reason = str(details.get("failure_reason") or "")
        if fc > 0 or failure_reason:
            out.append(
                {
                    "experiment_id": str(row.get("experiment_id") or ""),
                    "symbol": str(row.get("symbol") or ""),
                    "created_at": str(row.get("created_at") or ""),
                    "failed_count": fc,
                    "failure_reason": failure_reason,
                }
            )
    return out


# ------------------------------ decisions ------------------------------


def append_decision(
    root: Path | str,
    *,
    decision_type: str,
    decision: str,
    rationale: str = "",
    decided_by: str | None = None,
    hypothesis_id: str = "",
    experiment_id: str = "",
    strategy: str = "",
    symbol: str = "",
) -> dict[str, Any]:
    path = governance_root(root) / "decisions.jsonl"
    existing = _read_jsonl(path)
    row = {
        "id": _next_id(existing, "D-", _today_compact()),
        "ts": _now_iso(),
        "decision_type": str(decision_type or ""),
        "decision": str(decision or ""),
        "rationale": str(rationale or ""),
        "decided_by": decided_by,
        "hypothesis_id": str(hypothesis_id or ""),
        "experiment_id": str(experiment_id or ""),
        "strategy": str(strategy or ""),
        "symbol": str(symbol or ""),
    }
    try:
        _append_jsonl(path, row)
    except OSError:
        pass
    return row


def list_decisions(
    root: Path | str,
    *,
    strategy: str | None = None,
    symbol: str | None = None,
    experiment_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = _read_jsonl(governance_root(root) / "decisions.jsonl")
    if strategy is not None:
        rows = [r for r in rows if str(r.get("strategy") or "") == str(strategy)]
    if symbol is not None:
        rows = [r for r in rows if str(r.get("symbol") or "") == str(symbol)]
    if experiment_id is not None:
        rows = [r for r in rows if str(r.get("experiment_id") or "") == str(experiment_id)]
    if limit is not None:
        n = _as_int(limit)
        if n > 0:
            rows = rows[-n:]
    return rows


# ------------------------------ live strategies ------------------------------


def _live_path(root: Path | str) -> Path:
    return governance_root(root) / "live_strategies.json"


def _load_live(root: Path | str) -> list[dict[str, Any]]:
    data = _load_json(_live_path(root))
    rows = data.get("live") if isinstance(data, dict) else data
    return [row for row in (rows or []) if isinstance(row, dict)]


def _save_live(root: Path | str, items: list[dict[str, Any]]) -> None:
    try:
        _atomic_json(_live_path(root), {"schema_version": _SCHEMA_VERSION, "live": items})
    except OSError:
        pass


def register_live_strategy(
    root: Path | str,
    *,
    strategy: str,
    symbol: str,
    experiment_id: str = "",
    release_sheet_id: str = "",
    params_sha256: str = "",
    live_since: str | None = None,
) -> dict[str, Any]:
    items = _load_live(root)
    items = [
        s
        for s in items
        if not (str(s.get("strategy")) == str(strategy) and str(s.get("symbol")) == str(symbol))
    ]
    entry = {
        "strategy": str(strategy or ""),
        "symbol": str(symbol or ""),
        "experiment_id": str(experiment_id or ""),
        "release_sheet_id": str(release_sheet_id or ""),
        "params_sha256": str(params_sha256 or ""),
        "live_since": live_since or _now_iso(),
        "status": "running",
        "registered_at": _now_iso(),
    }
    items.append(entry)
    _save_live(root, items)
    return entry


def retire_live_strategy(root: Path | str, strategy: str, symbol: str, *, reason: str = "") -> dict[str, Any] | None:
    items = _load_live(root)
    for item in items:
        if str(item.get("strategy")) == str(strategy) and str(item.get("symbol")) == str(symbol):
            item["status"] = "retired"
            item["retired_at"] = _now_iso()
            item["retire_reason"] = str(reason or "")
            _save_live(root, items)
            return item
    return None


def list_live_strategies(root: Path | str, *, include_retired: bool = False) -> list[dict[str, Any]]:
    items = _load_live(root)
    if include_retired:
        return items
    return [s for s in items if str(s.get("status")) != "retired"]


# ------------------------------ changes ------------------------------


def append_change(
    root: Path | str,
    *,
    strategy: str,
    symbol: str,
    what: str,
    why: str = "",
    approved_by: str = "",
    effective_at: str | None = None,
    rollback_plan: str = "",
    experiment_id: str = "",
) -> dict[str, Any]:
    path = governance_root(root) / "changes.jsonl"
    existing = _read_jsonl(path)
    now = _now_iso()
    row = {
        "id": _next_id(existing, "C-", _today_compact()),
        "ts": now,
        "strategy": str(strategy or ""),
        "symbol": str(symbol or ""),
        "what": str(what or ""),
        "why": str(why or ""),
        "approved_by": str(approved_by or ""),
        "effective_at": effective_at or now,
        "rollback_plan": str(rollback_plan or ""),
        "experiment_id": str(experiment_id or ""),
    }
    try:
        _append_jsonl(path, row)
    except OSError:
        pass
    return row


def list_changes(
    root: Path | str,
    *,
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = _read_jsonl(governance_root(root) / "changes.jsonl")
    if strategy is not None:
        rows = [r for r in rows if str(r.get("strategy") or "") == str(strategy)]
    if symbol is not None:
        rows = [r for r in rows if str(r.get("symbol") or "") == str(symbol)]
    if limit is not None:
        n = _as_int(limit)
        if n > 0:
            rows = rows[-n:]
    return rows


# ------------------------------ freeze / reoptimize ------------------------------


def is_frozen(root: Path | str, strategy: str, symbol: str, *, at: Any = None) -> tuple[bool, str]:
    at_date = _to_date(at)
    policy = ensure_policy(root)
    freeze_days = _as_int(policy.get("freeze_days"))
    if freeze_days <= 0:
        freeze_days = 14
    changes = list_changes(root, strategy=strategy, symbol=symbol)
    if not changes:
        return False, "无变更记录"
    latest = max(changes, key=lambda c: _to_datetime(c.get("effective_at") or c.get("ts")))
    change_date = _to_datetime(latest.get("effective_at") or latest.get("ts")).date()
    delta_days = (at_date - change_date).days
    if delta_days < freeze_days:
        shown = max(0, delta_days)
        return True, f"处于变更冻结期：距最近变更 {shown} 天，冻结 {freeze_days} 天"
    return False, "未冻结"


def next_reoptimization_window(root: Path | str, *, at: Any = None) -> dict[str, Any]:
    at_date = _to_date(at)
    policy = ensure_policy(root)
    cadence = str(policy.get("reoptimize_cadence") or "monthly").strip().lower()
    reoptimize_day = _as_int(policy.get("reoptimize_day")) or 1
    if cadence == "monthly":
        year, month = at_date.year, at_date.month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        next_start = f"{year:04d}-{month:02d}-01"
        return {
            "cadence": "monthly",
            "next_start": next_start,
            "note": f"按月再优化，下个窗口自 {next_start} 起（建议执行日 {reoptimize_day} 号）",
        }
    return {
        "cadence": cadence or MISSING,
        "next_start": "",
        "note": f"当前再优化节奏为 {cadence or MISSING}，未计算自动窗口",
    }


# ------------------------------ trace ------------------------------


def _index_rows(root: Path | str) -> list[dict[str, Any]]:
    index = _load_json(_experiments_dir(root) / "index.json")
    rows = index.get("experiments") if isinstance(index, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict)]


def _hypothesis_id_of_manifest(root: Path | str, exp_id: Any) -> str:
    manifest = _read_manifest(root, exp_id) if exp_id else None
    if manifest is None:
        return ""
    details = manifest.get("details")
    if isinstance(details, dict):
        return str(details.get("hypothesis_id") or "")
    return ""


def _find_hypothesis_for(root: Path | str, strategy: str, symbol: str) -> dict[str, Any] | None:
    # 1) 从上线策略的 experiment_id 反查
    for live in list_live_strategies(root, include_retired=True):
        if str(live.get("strategy")) != str(strategy) or str(live.get("symbol")) != str(symbol):
            continue
        exp_id = live.get("experiment_id")
        if exp_id:
            hyp_id = _hypothesis_id_of_manifest(root, exp_id)
            if hyp_id:
                hyp = get_hypothesis(root, hyp_id)
                if hyp:
                    return hyp
    # 2) 同 symbol 实验里找有关联假设的
    for row in _index_rows(root):
        if str(row.get("symbol") or "") != str(symbol):
            continue
        hyp_id = _hypothesis_id_of_manifest(root, row.get("experiment_id"))
        if hyp_id:
            hyp = get_hypothesis(root, hyp_id)
            if hyp:
                return hyp
    return None


def _experiments_for(root: Path | str, symbol: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _index_rows(root):
        if symbol and str(row.get("symbol") or "") != str(symbol):
            continue
        out.append(
            {
                "experiment_id": str(row.get("experiment_id") or ""),
                "kind": str(row.get("kind") or ""),
                "created_at": str(row.get("created_at") or ""),
                "symbol": str(row.get("symbol") or ""),
                "failed_count": _as_int(row.get("failed_count")),
                "hypothesis_id": str(row.get("hypothesis_id") or "")
                or _hypothesis_id_of_manifest(root, row.get("experiment_id")),
            }
        )
    return out


def _read_release_sheet(root: Path | str, symbol: str) -> dict[str, Any] | None:
    data_dir = Path(root) / "data"
    json_path = data_dir / "release_sheet.json"
    md_path = data_dir / "release_sheet.md"
    data = _load_json(json_path)
    if not isinstance(data, dict) or not data:
        return None
    rs_symbol = str(data.get("symbol") or "").strip()
    if symbol and rs_symbol and rs_symbol != str(symbol):
        return None
    if not md_path.exists():
        return None
    decision = str(data.get("decision") or data.get("verdict") or "")
    generated_at = str(data.get("generated_at") or "")
    return {
        "release_sheet": {"generated_at": generated_at, "decision": decision},
        "out_of_sample": f"样本外/评审结论：{decision or '—'}"
        + (f"（{generated_at}）" if generated_at else ""),
    }


def trace_strategy(root: Path | str, strategy: str, symbol: str) -> dict[str, Any]:
    """尽力收集追踪链，绝不抛异常；缺失环节标字符串 ``未记录``。"""
    trace: dict[str, Any] = {
        "hypothesis": MISSING,
        "experiments": [],
        "out_of_sample": MISSING,
        "release_sheet": MISSING,
        "live_strategy": MISSING,
        "changes": [],
        "frozen": (False, MISSING),
    }
    try:
        trace["frozen"] = is_frozen(root, strategy, symbol)
    except Exception:  # noqa: BLE001
        trace["frozen"] = (False, MISSING)
    try:
        trace["changes"] = list_changes(root, strategy=strategy, symbol=symbol) or []
    except Exception:  # noqa: BLE001
        trace["changes"] = []
    try:
        match = next(
            (
                s
                for s in list_live_strategies(root, include_retired=True)
                if str(s.get("strategy")) == str(strategy) and str(s.get("symbol")) == str(symbol)
            ),
            None,
        )
        if match:
            trace["live_strategy"] = match
    except Exception:  # noqa: BLE001
        pass
    try:
        hyp = _find_hypothesis_for(root, strategy, symbol)
        if hyp:
            trace["hypothesis"] = hyp
    except Exception:  # noqa: BLE001
        pass
    try:
        trace["experiments"] = _experiments_for(root, symbol)
    except Exception:  # noqa: BLE001
        trace["experiments"] = []
    try:
        rs = _read_release_sheet(root, symbol)
        if rs:
            trace["out_of_sample"] = rs.get("out_of_sample") or MISSING
            trace["release_sheet"] = rs.get("release_sheet") or MISSING
    except Exception:  # noqa: BLE001
        pass
    return trace


def format_trace_lines(trace: dict[str, Any]) -> list[str]:
    """把 trace 转成人类可读多行文本，供 UI / 评审单展示。"""
    lines: list[str] = []

    hyp = trace.get("hypothesis")
    if isinstance(hyp, dict):
        lines.append(
            f"假设：{hyp.get('id', '')} {hyp.get('title', '')}（{hyp.get('status', MISSING)}）"
        )
    else:
        lines.append(f"假设：{MISSING}")

    exps = trace.get("experiments") or []
    if exps:
        lines.append(f"实验：{len(exps)} 个")
        for exp in exps[:5]:
            lines.append(
                f"  - {exp.get('experiment_id', '')} {exp.get('kind', '')} "
                f"{exp.get('symbol', '')} 失败 {exp.get('failed_count', 0)} 次"
                + (f" 假设 {exp.get('hypothesis_id', '')}" if exp.get("hypothesis_id") else "")
            )
    else:
        lines.append(f"实验：{MISSING}")

    oos = trace.get("out_of_sample")
    lines.append(f"样本外：{oos if isinstance(oos, str) and oos else MISSING}")

    rs = trace.get("release_sheet")
    if isinstance(rs, dict):
        lines.append(
            f"评审单：{rs.get('generated_at', '')} 决定 {rs.get('decision', '') or MISSING}"
        )
    else:
        lines.append(f"评审单：{MISSING}")

    live = trace.get("live_strategy")
    if isinstance(live, dict):
        lines.append(
            f"上线策略：{live.get('strategy', '')}/{live.get('symbol', '')} "
            f"{live.get('status', MISSING)} 自 {live.get('live_since', '')}"
        )
    else:
        lines.append(f"上线策略：{MISSING}")

    changes = trace.get("changes") or []
    if changes:
        lines.append(f"变更：{len(changes)} 条")
        for change in changes[-3:]:
            lines.append(
                f"  - {change.get('effective_at', '')[:10]} {change.get('what', '')}"
                + (f"（{change.get('why', '')}）" if change.get("why") else "")
            )
    else:
        lines.append(f"变更：{MISSING}")

    frozen = trace.get("frozen")
    if isinstance(frozen, (tuple, list)) and len(frozen) >= 2:
        state = "是" if frozen[0] else "否"
        lines.append(f"冻结：{state} - {frozen[1]}")
    else:
        lines.append(f"冻结：{MISSING}")

    return lines
