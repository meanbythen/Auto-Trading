"""回测/优化实验的不可变快照、manifest 与版本化归档。"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import os
import subprocess
import tempfile
import uuid
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.utils.paths import project_root
from src.version import read_version

SCHEMA_VERSION = 1
REDACTED = "<已脱敏>"
_SENSITIVE_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "auth_code",
    "authcode",
    "app_id",
    "appid",
    "investor",
    "user_id",
    "username",
    "account",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    # 环境变量名本身不是秘密，保留它才能复现配置；变量值绝不写入快照。
    if normalized.endswith("_env"):
        return False
    return any(part in normalized for part in _SENSITIVE_PARTS)


def sanitize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """递归脱敏并转成稳定 JSON 值；不修改调用方配置。"""

    def walk(value: Any, key: str = "") -> Any:
        if key and _sensitive_key(key):
            return REDACTED
        if isinstance(value, dict):
            return {str(k): walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [walk(item) for item in value]
        return _json_safe(value)

    return walk(cfg or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _code_identity() -> dict[str, Any]:
    identity: dict[str, Any] = {"version": read_version(), "git_commit": "", "git_dirty": None}
    root = project_root()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if commit.returncode == 0:
            identity["git_commit"] = (commit.stdout or "").strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if dirty.returncode == 0:
            identity["git_dirty"] = bool((dirty.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return identity


def new_experiment(
    kind: str,
    cfg: dict[str, Any] | None,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建内存 manifest；K 线文件信息在归档时补齐。

    ``details`` 是自由 dict，可携带治理字段（如 ``hypothesis_id`` / ``failure_reason``），
    归档与索引重建时会从 ``manifest.details`` 透传到实验索引。
    """
    now = datetime.now().astimezone()
    snapshot = sanitize_config(cfg)
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": f"{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:10]}",
        "kind": str(kind),
        "created_at": now.isoformat(timespec="seconds"),
        "code": dict(_code_identity()),
        "config": {"sha256": config_sha256(snapshot), "snapshot": snapshot},
        "details": _json_safe(details or {}),
        "data": {"snapshot_format": "csv.gz", "files": []},
        "promotion": {
            "stage": "research",
            "simulation_status": "not_run",
            "live_approval": "not_approved",
        },
    }


def research_dataset_reference(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """读取实验显式绑定的研究数据集；配置了但读不到时直接阻止实验。"""
    backtest = (cfg or {}).get("backtest") or {}
    raw_path = str(backtest.get("research_dataset") or "").strip()
    if not raw_path:
        return {}
    root = Path(str((cfg or {}).get("_project_root") or project_root()))
    path = Path(raw_path)
    if not path.is_absolute():
        root_candidate = root / path
        path = root_candidate if root_candidate.exists() else root / "data" / "research_datasets" / raw_path
        if path.is_dir() or (not path.exists() and path.suffix.lower() != ".json"):
            path = path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    file_info = fingerprint_file(path)
    return {
        "dataset_id": str(manifest.get("dataset_id") or ""),
        "dataset_sha256": str(manifest.get("dataset_sha256") or ""),
        "manifest_path": str(path),
        "manifest_file_sha256": file_info["sha256"],
        "requested_start": (manifest.get("window") or {}).get("start", ""),
        "requested_end": (manifest.get("window") or {}).get("end", ""),
        "readiness": manifest.get("readiness") or {},
    }


def snapshot_bars(**frames: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """在实验结束时复制输入 K 线；同一个对象只保留一份副本。"""
    copies: dict[int, pd.DataFrame] = {}
    out: dict[str, pd.DataFrame] = {}
    for name, frame in frames.items():
        if frame is None or len(frame) == 0:
            continue
        key = id(frame)
        if key not in copies:
            copies[key] = frame.copy(deep=True)
        out[name] = copies[key]
    return out


def fingerprint_bars(frame: pd.DataFrame) -> dict[str, Any]:
    """指纹包含顺序、时间戳、列名和值；不依赖 CSV 文件时间。"""
    digest = hashlib.sha256()
    digest.update(_canonical_json(list(frame.columns)).encode("utf-8"))
    try:
        hashed = pd.util.hash_pandas_object(frame, index=True).to_numpy(dtype="uint64", copy=False)
        digest.update(hashed.tobytes())
    except (TypeError, ValueError):
        digest.update(frame.to_csv(index=True).encode("utf-8"))
    return {
        "rows": int(len(frame)),
        "columns": [str(col) for col in frame.columns],
        "from": str(frame.index[0]) if len(frame) else "",
        "to": str(frame.index[-1]) if len(frame) else "",
        "sha256": digest.hexdigest(),
    }


def fingerprint_file(path: Path | str) -> dict[str, Any]:
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = file_path.stat()
    return {
        "path": str(file_path),
        "size": int(stat.st_size),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "sha256": digest.hexdigest(),
    }


def _atomic_json(path: Path, payload: Any) -> None:
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
            os.unlink(temp_name)


def _write_bars(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 让同一 K 线快照重复归档时文件本身也可复现。
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            frame.to_csv(zipped, index_label="datetime", encoding="utf-8")


def _trial_counts(manifest: dict[str, Any], base: Path | None = None) -> tuple[int, int]:
    """取试验次数与失败数：优先 details，旧实验回退读 report.json 现场数，失败回退 0。

    只读，不抛异常。trial_count = 全部参数组合数；failed_count = 含 error 的组合数。
    """
    details = manifest.get("details") or {}
    tc = details.get("trial_count")
    fc = details.get("failed_count")
    if isinstance(tc, int) and isinstance(fc, int):
        return tc, fc
    if base is not None:
        exp_id = manifest.get("experiment_id")
        report_path = base / "experiments" / str(exp_id) / "report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            trials = report.get("trials") or []
            if isinstance(trials, list):
                failed = sum(1 for t in trials if isinstance(t, dict) and t.get("error"))
                return len(trials), failed
        except (OSError, json.JSONDecodeError):
            pass
    return 0, 0


def _index_row(manifest: dict[str, Any], base: Path | None = None) -> dict[str, Any]:
    """一条实验索引。归档和重建索引共用同一份字段。"""
    details = manifest.get("details") or {}
    trial_count, failed_count = _trial_counts(manifest, base)
    return {
        "experiment_id": manifest["experiment_id"],
        "kind": manifest.get("kind", ""),
        "created_at": manifest.get("created_at", ""),
        "symbol": details.get("symbol", ""),
        "code_version": (manifest.get("code") or {}).get("version", ""),
        "git_commit": (manifest.get("code") or {}).get("git_commit", ""),
        "config_sha256": (manifest.get("config") or {}).get("sha256", ""),
        "data_sha256": [item.get("sha256", "") for item in (manifest.get("data") or {}).get("files", [])],
        "snapshot_complete": (manifest.get("review_gate") or {}).get("snapshot_complete", False),
        # 批次1可信度底座：试验留痕。旧实验缺字段时回退 0，不阻塞加载。
        "trial_count": trial_count,
        "failed_count": failed_count,
        # 批次3治理：假设编号与失败原因。旧实验缺 details 键时回退空串，不阻塞重建。
        "hypothesis_id": details.get("hypothesis_id", ""),
        "failure_reason": details.get("failure_reason", ""),
        "report": f"{manifest['experiment_id']}/report.json",
    }


def _update_index(root: Path, manifest: dict[str, Any]) -> None:
    index_path = root / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {"schema_version": SCHEMA_VERSION, "experiments": []}
    rows = [row for row in index.get("experiments", []) if row.get("experiment_id") != manifest["experiment_id"]]
    rows.append(_index_row(manifest, root.parent))
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    index["experiments"] = rows
    _atomic_json(index_path, index)


def rebuild_index(root: Path | str) -> int:
    """扫描 experiments/*/manifest.json 重建 index.json，返回条目数。

    只读历史实验、只写索引文件；不搬动、不删除任何实验目录。
    """
    base = Path(root)
    exp_dir = base / "experiments"
    if not exp_dir.is_dir():
        return 0
    rows: list[dict[str, Any]] = []
    skipped = 0
    for manifest_path in sorted(exp_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        if not isinstance(manifest, dict) or not manifest.get("experiment_id"):
            skipped += 1
            continue
        rows.append(_index_row(manifest, base))
    if not rows:
        return 0
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    _atomic_json(exp_dir / "index.json", {"schema_version": SCHEMA_VERSION, "experiments": rows})
    if skipped:
        # 损坏的 manifest 不阻塞重建，但要在日志里留个数字方便排查。
        logger.warning("实验索引重建跳过 {} 个无法解析的 manifest.json", skipped)
    return len(rows)


def archive_report(
    result: Any,
    out_path: Path | str,
    payload: dict[str, Any],
    *,
    kind: str,
    details: dict[str, Any],
) -> Path:
    """写当前报告、独立实验目录和索引；返回实验目录。"""
    path = Path(out_path)
    manifest = copy.deepcopy(getattr(result, "experiment", {}) or {})
    if not manifest:
        manifest = new_experiment(kind, {}, details={**details, "snapshot_origin": "legacy_result"})
    snapshots = getattr(result, "_experiment_data", {}) or {}
    files: list[dict[str, Any]] = []
    folder = path.parent / "experiments" / str(manifest["experiment_id"])
    for name, frame in snapshots.items():
        if frame is None or len(frame) == 0:
            continue
        safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(name))
        relative = Path("data") / f"{safe_name}.csv.gz"
        _write_bars(folder / relative, frame)
        info = fingerprint_bars(frame)
        info.update({"name": str(name), "path": relative.as_posix()})
        files.append(info)
    manifest["data"] = {"snapshot_format": "csv.gz", "files": files}
    costs = (manifest.get("details") or {}).get("costs") or {}
    missing: list[str] = []
    if not (manifest.get("config") or {}).get("sha256"):
        missing.append("config_snapshot")
    if not files:
        missing.append("data_snapshot")
    if costs.get("slippage_ticks") is None:
        missing.append("cost_assumption")
    manifest["review_gate"] = {
        "snapshot_complete": not missing,
        "missing": missing,
        "note": "快照完整只表示可复核，不代表研究、模拟或实盘验收通过。",
    }
    result.experiment = manifest
    payload["experiment"] = manifest
    _atomic_json(folder / "manifest.json", manifest)
    _atomic_json(folder / "report.json", payload)
    _atomic_json(path, payload)
    _update_index(path.parent / "experiments", manifest)
    return folder
