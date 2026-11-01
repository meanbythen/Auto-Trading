"""
优化参数 Excel 导入/导出
功能：
  - 程序内历史只保留最近 30 条（见 param_history.MAX_KEEP）
  - 导出：把当前历史（及可选出厂默认）追加写入本地 Excel，便于长期存档
  - 导入：从 Excel 读回，合并进程序历史（按 id 去重，仍只留最近 30 条）
原理：每一行 = 一个版本；复杂字段（snapshot / opt_params）用 JSON 字符串存进单元格。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.backtest.param_history import (
    FACTORY_DEFAULT_ID,
    MAX_KEEP,
    factory_default_entry,
    load_history,
    save_history,
    version_display_name,
)

# 旧版单文件名。新导出改写到统一工作簿的「优化参数」表，读到旧文件时仍会并进去。
LEGACY_EXCEL_NAME = "optimize_params_archive.xlsx"

# 表头中文，方便在 Excel 里直接看
COLUMNS = [
    "版本ID",
    "名称",
    "时间",
    "来源",
    "说明",
    "策略引擎",
    "品种",
    "得分",
    "成交笔数",
    "胜率",
    "净盈亏",
    "盈利因子",
    "优化参数JSON",
    "完整快照JSON",
    "导出时间",
]


def default_excel_path(root: Path | str) -> Path:
    """优化参数也写进统一工作簿 data/Auto-Trading导出.xlsx 的「优化参数」表。"""
    from src.export.book import default_workbook_path

    return default_workbook_path(root)


def _perf(v: dict[str, Any]) -> dict[str, Any]:
    p = v.get("performance") or {}
    return p if isinstance(p, dict) else {}


def version_to_row(v: dict[str, Any], *, exported_at: str | None = None) -> dict[str, Any]:
    """把一条历史版本压成 Excel 一行。"""
    perf = _perf(v)
    return {
        "版本ID": str(v.get("id", "")),
        "名称": version_display_name(v),
        "时间": str(v.get("time", "")),
        "来源": str(v.get("source", "")),
        "说明": str(v.get("note", "")),
        "策略引擎": str(v.get("engine", "")),
        "品种": str(v.get("symbol", "")),
        "得分": v.get("score"),
        "成交笔数": perf.get("trades"),
        "胜率": perf.get("win_rate"),
        "净盈亏": perf.get("net_pnl"),
        "盈利因子": perf.get("profit_factor"),
        "优化参数JSON": json.dumps(v.get("opt_params") or {}, ensure_ascii=False),
        "完整快照JSON": json.dumps(v.get("snapshot") or {}, ensure_ascii=False),
        "导出时间": exported_at or datetime.now().isoformat(timespec="seconds"),
    }


def row_to_version(row: dict[str, Any]) -> dict[str, Any] | None:
    """把 Excel 一行还原成历史版本；缺关键字段则跳过。"""
    vid = str(row.get("版本ID") or row.get("id") or "").strip()
    if not vid:
        return None
    try:
        opt_raw = row.get("优化参数JSON") or row.get("opt_params") or "{}"
        snap_raw = row.get("完整快照JSON") or row.get("snapshot") or "{}"
        if pd.isna(opt_raw):
            opt_raw = "{}"
        if pd.isna(snap_raw):
            snap_raw = "{}"
        opt_params = json.loads(str(opt_raw)) if str(opt_raw).strip() else {}
        snapshot = json.loads(str(snap_raw)) if str(snap_raw).strip() else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("解析 Excel 行 JSON 失败 id={}: {}", vid, e)
        return None

    score = row.get("得分", row.get("score"))
    if score is not None and not (isinstance(score, float) and pd.isna(score)):
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None
    else:
        score = None

    def _num(key_cn: str, key_en: str) -> Any:
        val = row.get(key_cn, row.get(key_en))
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    trades = _num("成交笔数", "trades")
    performance = {
        "trades": int(trades) if trades is not None else None,
        "win_rate": _num("胜率", "win_rate"),
        "net_pnl": _num("净盈亏", "net_pnl"),
        "profit_factor": _num("盈利因子", "profit_factor"),
    }
    # 去掉全空绩效
    if all(v is None for v in performance.values()):
        performance = {}

    raw_name = row.get("名称", row.get("name"))
    if raw_name is None or (isinstance(raw_name, float) and pd.isna(raw_name)):
        raw_name = ""
    name = str(raw_name).strip()
    if name.lower() == "nan":
        name = ""

    entry = {
        "id": vid,
        "time": str(row.get("时间") or row.get("time") or ""),
        "source": str(row.get("来源") or row.get("source") or "import"),
        "note": str(row.get("说明") or row.get("note") or "从Excel导入"),
        "engine": str(row.get("策略引擎") or row.get("engine") or ""),
        "symbol": str(row.get("品种") or row.get("symbol") or ""),
        "score": score,
        "performance": performance,
        "opt_params": opt_params if isinstance(opt_params, dict) else {},
        "snapshot": snapshot if isinstance(snapshot, dict) else {},
    }
    # 旧 Excel 没有「名称」列时不硬写，打开列表会按「品种-时间」现算。
    if name:
        entry["name"] = name
    return entry


def collect_export_versions(root: Path | str, *, include_factory: bool = True) -> list[dict[str, Any]]:
    """准备导出的版本列表：可选带出厂默认 + 程序内最近最多 30 条。"""
    versions: list[dict[str, Any]] = []
    if include_factory:
        versions.append(factory_default_entry())
    for v in load_history(root)[:MAX_KEEP]:
        if str(v.get("id")) == FACTORY_DEFAULT_ID:
            continue
        versions.append(v)
    return versions


def merged_param_frame(
    root: Path | str,
    excel_path: Path | str | None = None,
    *,
    include_factory: bool = True,
) -> pd.DataFrame:
    """
    合成「优化参数」表：Excel 里已有的行保留，程序里新的版本按 ID 追加。

    若统一工作簿还没有这张表，会顺便读入旧文件 optimize_params_archive.xlsx，避免以前导出的版本丢掉。
    """
    from src.export.book import SHEET_PARAMS, read_sheet

    path = Path(excel_path) if excel_path else default_excel_path(root)
    exported_at = datetime.now().isoformat(timespec="seconds")
    new_rows = [
        version_to_row(v, exported_at=exported_at)
        for v in collect_export_versions(root, include_factory=include_factory)
    ]
    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    old_df = _existing_param_frame(root, path, SHEET_PARAMS, read_sheet)
    existing_ids = {
        str(x).strip()
        for x in old_df["版本ID"].tolist()
        if x is not None and str(x).strip() and str(x).lower() != "nan"
    }
    add_df = new_df[~new_df["版本ID"].astype(str).isin(existing_ids)]
    merged = pd.concat([old_df, add_df], ignore_index=True)
    # 已经导出过的行，改名后再导出时把「名称」更新掉，不另起一行。
    return _refresh_exported_names(merged, new_df)


def _refresh_exported_names(merged: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    """
    用程序里现在的名字，改掉 Excel 里同一版本ID的「名称」。

    版本ID不变，所以不会多出一行。程序里已经删掉的旧行，名字保持 Excel 里原来的。
    """
    if merged.empty:
        return merged
    out = merged.copy()
    for col in COLUMNS:
        if col not in out.columns:
            out[col] = None
    names: dict[str, str] = {}
    if current is not None and not current.empty and "版本ID" in current.columns:
        for _, row in current.iterrows():
            vid = str(row.get("版本ID", "")).strip()
            name = row.get("名称")
            if not vid or name is None or (isinstance(name, float) and pd.isna(name)):
                continue
            text = str(name).strip()
            if text and text.lower() != "nan":
                names[vid] = text
    for idx in out.index:
        vid = str(out.at[idx, "版本ID"]).strip()
        if vid in names:
            out.at[idx, "名称"] = names[vid]
    return out[COLUMNS]


def _existing_param_frame(root: Path | str, path: Path, sheet: str, read_sheet: Any) -> pd.DataFrame:
    """读已有优化参数行。统一文件没有时，再试旧的单表归档。"""
    frames: list[pd.DataFrame] = []
    if path.exists():
        frames.append(_normalize_param_frame(read_sheet(path, sheet, "版本ID")))
    legacy = Path(root) / "data" / LEGACY_EXCEL_NAME
    if legacy.exists() and legacy.resolve() != path.resolve():
        frames.append(_normalize_param_frame(read_sheet(legacy, sheet, "版本ID")))
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    merged = pd.concat(frames, ignore_index=True)
    if merged.empty:
        return pd.DataFrame(columns=COLUMNS)
    merged = merged.drop_duplicates(subset=["版本ID"], keep="first")
    return merged


def _normalize_param_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "版本ID" not in frame.columns:
        return pd.DataFrame(columns=COLUMNS)
    out = frame.copy()
    for col in COLUMNS:
        if col not in out.columns:
            out[col] = None
    return out[COLUMNS]


def export_append_excel(
    root: Path | str,
    excel_path: Path | str | None = None,
    *,
    include_factory: bool = True,
) -> tuple[Path, int, int]:
    """
    把当前程序内历史追加到统一 Excel 的「优化参数」表。

    返回:
        (文件路径, 本次新增行数, 该表总行数)
    原理：按「版本ID」去重；只替换这一张表，账户成交、复盘等其他表保持不变。
    """
    from src.export.book import SHEET_PARAMS, read_sheet, write_sheet

    path = Path(excel_path) if excel_path else default_excel_path(root)
    old_ids: set[str] = set()
    if path.exists():
        old = read_sheet(path, SHEET_PARAMS, "版本ID")
        if not old.empty and "版本ID" in getattr(old, "columns", []):
            old_ids = {
                str(x).strip()
                for x in old["版本ID"].tolist()
                if x is not None and str(x).strip() and str(x).lower() != "nan"
            }
    merged = merged_param_frame(root, path, include_factory=include_factory)
    write_sheet(path, SHEET_PARAMS, merged)
    new_ids = {
        str(x).strip()
        for x in merged["版本ID"].tolist()
        if x is not None and str(x).strip() and str(x).lower() != "nan"
    }
    added = len(new_ids - old_ids)
    logger.info(
        "已导出参数到 Excel: {} 表 {} | 新增约 {} 行 | 合计 {} 行",
        path,
        SHEET_PARAMS,
        added,
        len(merged),
    )
    return path, added, int(len(merged))


def import_from_excel(
    root: Path | str,
    excel_path: Path | str,
    *,
    keep: int = MAX_KEEP,
) -> tuple[int, int]:
    """
    从 Excel 导入版本，合并进程序历史。

    返回:
        (读到的有效行数, 写入程序后的历史条数)
    原理：与现有历史按 id 合并；导入的排在前面（更新）；仍只保留 keep 条。
    """
    from src.export.book import SHEET_PARAMS, read_sheet

    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到 Excel: {path}")

    df = read_sheet(path, SHEET_PARAMS, "版本ID")
    if df.empty or "版本ID" not in df.columns:
        raise ValueError("这个 Excel 里没有「优化参数」表，也没有带「版本ID」的旧表。")
    imported: list[dict[str, Any]] = []
    for _, series in df.iterrows():
        ver = row_to_version(series.to_dict())
        if not ver:
            continue
        if ver["id"] == FACTORY_DEFAULT_ID:
            # 出厂默认由代码维护，不必写进 json
            continue
        if not ver.get("snapshot"):
            logger.warning("跳过无快照的行 id={}", ver["id"])
            continue
        imported.append(ver)

    if not imported:
        return 0, len(load_history(root))

    # 合并：导入优先（放前面），再用旧历史补齐未覆盖的 id
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for v in imported + load_history(root):
        vid = str(v.get("id", ""))
        if not vid or vid in seen or vid == FACTORY_DEFAULT_ID:
            continue
        seen.add(vid)
        merged.append(v)

    save_history(root, merged[:keep])
    logger.info("已从 Excel 导入 {} 条有效版本，程序内保留 {} 条", len(imported), min(len(merged), keep))
    return len(imported), min(len(merged), keep)
