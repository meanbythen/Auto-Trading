"""
研究内核：回测/交叉/优化/复盘/规律共用的运行清单与结果口径。

功能：生成可回溯的 run manifest，并从回测结果统一取净盈亏、交易笔数等指标。
原理：清单只组装、不写库；需要落库时转给 src.ops.journal，研究模块不依赖调度或界面。
注意：本模块统一的是运行清单与结果口径，不是字面上的 BacktestKernel，各页编排仍各自保留。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def new_run_id(source: str) -> str:
    """生成本地研究运行号；source 只用于可读性，不参与唯一性。"""
    now = datetime.now().astimezone()
    return f"RS-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    from src.backtest.experiment import _json_safe as safe

    return safe(value)


def build_run_manifest(
    source: str,
    cfg: dict[str, Any] | None,
    *,
    symbols: list[str] | None = None,
    period: str = "",
    started_at: str = "",
    duration_ms: float | None = None,
    status: str = "ok",
    message: str = "",
    run_id: str | None = None,
    strategy: dict[str, Any] | None = None,
    data_catalog: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装一份运行清单；任何辅助信息取不到时降级为空，不阻断研究。"""
    from src.kernel.trace import ensure_trace_id

    now = _iso_now()
    started = str(started_at or now)
    strategy_info = dict(strategy or {})
    params_sha256 = ""
    if not strategy_info:
        try:
            from src.strategy.factory import strategy_identity

            strategy_info = dict(strategy_identity(cfg) or {})
        except Exception:  # noqa: BLE001
            strategy_info = {}
    try:
        from src.backtest.experiment import config_sha256, sanitize_config

        params_sha256 = config_sha256(sanitize_config(cfg))
    except Exception:  # noqa: BLE001
        params_sha256 = ""
    catalog = dict(data_catalog or {})
    if not catalog:
        try:
            from src.data.catalog import catalog_reference

            root = str((cfg or {}).get("_project_root") or Path.cwd())
            catalog = dict(catalog_reference(root, cfg) or {})
        except Exception:  # noqa: BLE001
            catalog = {}
    try:
        from src.backtest.experiment import _code_identity

        commit = dict(_code_identity())
    except Exception:  # noqa: BLE001
        commit = {}
    manifest = {
        "run_id": str(run_id or new_run_id(source)),
        "trace_id": ensure_trace_id(),
        "source": str(source or ""),
        "status": str(status or "ok"),
        "started_at": started,
        "finished_at": now,
        "duration_ms": round(float(duration_ms or 0.0), 3),
        "strategy": strategy_info,
        "params_sha256": params_sha256,
        "data_catalog": catalog,
        "symbols": [str(item) for item in (symbols or [])],
        "period": str(period or ""),
        "commit": commit,
        "metrics": dict(metrics or {}),
        "message": str(message or ""),
        "extra": dict(extra or {}),
    }
    return dict(_json_safe(manifest))


def result_summary(result: Any) -> dict[str, float | int]:
    """所有研究页共用的结果取值，避免各页各算一套口径。"""
    perf = getattr(result, "performance", None)
    if perf is None:
        return {
            "trades": 0,
            "net_pnl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
        }
    return {
        "trades": int(getattr(perf, "trades", 0) or 0),
        "net_pnl": float(getattr(perf, "net_pnl", 0.0) or 0.0),
        "win_rate": float(getattr(perf, "win_rate", 0.0) or 0.0),
        "profit_factor": float(getattr(perf, "profit_factor", 0.0) or 0.0),
        "max_drawdown": float(getattr(perf, "max_drawdown", 0.0) or 0.0),
    }


def record_run(manifest: dict[str, Any], *, root: str | Path | None = None) -> bool:
    """把运行清单落库；失败只返回 False，不影响研究结果。"""
    from src.ops.journal import record_research_run

    return record_research_run(manifest, root=root)
