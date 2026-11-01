"""
品种保证金 / 乘数表（Excel）

功能：用一张 Excel 统一维护各品种的每手保证金和合约乘数；程序启动与保存时从表读入。
原理：避免配置页手打「bu:7794,c:1500」和 yaml 里再改一份对不齐。
      Excel 是你改的地方；读入后写进内存里的 risk.contracts，实盘/回测仍走原来的读法。
      表里没有的品种，手数与保证金仍用配置页「每手保证金(元)」兜底。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

# 相对项目根目录。首次没有文件时，按当前配置里的 contracts 生成一张。
DEFAULT_REL_PATH = "config/品种保证金.xlsx"
SHEET_NAME = "品种保证金"
HEADERS = ("品种代码", "中文名", "每手保证金", "乘数")


def product_key(symbol: str) -> str:
    """bu2612 / BU → bu。只留字母，和仓位计算里取品种的方法一致。"""
    return "".join(ch for ch in str(symbol or "").strip().lower() if "a" <= ch <= "z")


def resolve_table_path(cfg: dict[str, Any] | None = None, root: str | Path | None = None) -> Path:
    """
    算出保证金表的完整路径。

    配置里可写 risk.margin_table（相对项目根或绝对路径）；空则用默认文件名。
    """
    from src.utils.paths import project_root

    base = Path(root) if root else Path(str((cfg or {}).get("_project_root") or project_root()))
    risk = (cfg or {}).get("risk") or {}
    raw = str(risk.get("margin_table") or DEFAULT_REL_PATH).strip() or DEFAULT_REL_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return path


def contracts_from_cfg(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从配置里取出 risk.contracts，做成小写品种代码 → {name, margin, multiplier}。"""
    risk = cfg.get("risk") or {}
    raw = risk.get("contracts") or {}
    out: dict[str, dict[str, Any]] = {}
    for key, item in raw.items():
        code = product_key(str(key))
        if not code:
            continue
        if isinstance(item, dict):
            try:
                mult = float(item.get("multiplier", 10) or 10)
            except (TypeError, ValueError):
                mult = 10.0
            try:
                margin = float(item.get("margin", 0) or 0)
            except (TypeError, ValueError):
                margin = 0.0
            out[code] = {
                "name": str(item.get("name") or code),
                "margin": max(0.0, margin),
                "multiplier": max(0.1, mult),
            }
        else:
            try:
                mult = float(item)
            except (TypeError, ValueError):
                mult = 10.0
            out[code] = {"name": code, "margin": 0.0, "multiplier": max(0.1, mult)}
    return out


def summarize_contracts(contracts: dict[str, dict[str, Any]]) -> str:
    """界面摘要：只列出保证金 > 0 的品种，例如 bu:8276,fu:11232。"""
    parts: list[str] = []
    for key in sorted(contracts.keys()):
        item = contracts.get(key) or {}
        margin = float(item.get("margin", 0) or 0)
        if margin <= 0:
            continue
        text = str(int(margin)) if margin == int(margin) else str(margin)
        parts.append(f"{key}:{text}")
    return ",".join(parts)


def read_margin_table(path: Path | str) -> dict[str, dict[str, Any]]:
    """
    读 Excel 品种表。

    返回:
        品种代码 → {name, margin, multiplier}
    抛出:
        文件不存在、缺库、或表格被 Excel 占用导致读失败时的异常。
    """
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"品种保证金表不存在: {file}")

    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 openpyxl 才能读品种保证金表。请执行: pip install openpyxl") from exc

    book = openpyxl.load_workbook(file, data_only=True, read_only=True)
    try:
        sheet = book[SHEET_NAME] if SHEET_NAME in book.sheetnames else book[book.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            return {}
        # 用表头找列，顺序乱了也能认。
        names = [str(c or "").strip() for c in header]
        idx_code = _find_col(names, ("品种代码", "代码", "品种", "symbol", "code"))
        idx_name = _find_col(names, ("中文名", "名称", "name"))
        idx_margin = _find_col(names, ("每手保证金", "保证金", "margin"))
        idx_mult = _find_col(names, ("乘数", "合约乘数", "multiplier"))
        if idx_code is None or idx_margin is None:
            raise ValueError("表格至少要有「品种代码」「每手保证金」两列")

        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not row:
                continue
            code = product_key(row[idx_code] if idx_code < len(row) else "")
            if not code:
                continue
            name_raw = row[idx_name] if idx_name is not None and idx_name < len(row) else ""
            name = str(name_raw or "").strip() or code
            margin = _as_float(row[idx_margin] if idx_margin < len(row) else 0, 0.0)
            mult = 10.0
            if idx_mult is not None and idx_mult < len(row):
                mult = _as_float(row[idx_mult], 10.0)
            out[code] = {
                "name": name,
                "margin": max(0.0, margin),
                "multiplier": max(0.1, mult),
            }
        return out
    finally:
        book.close()


def write_margin_table(path: Path | str, contracts: dict[str, dict[str, Any]]) -> Path:
    """
    把品种字典写成 Excel（整表重写）。

    原理：一张简单表，方便你在 Excel 里增删改；程序下次读取整表覆盖内存里的 contracts。
    """
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 openpyxl 才能写品种保证金表。请执行: pip install openpyxl") from exc

    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = SHEET_NAME
    sheet.append(list(HEADERS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for code in sorted(contracts.keys()):
        item = contracts.get(code) or {}
        margin = float(item.get("margin", 0) or 0)
        mult = float(item.get("multiplier", 10) or 10)
        sheet.append(
            [
                code,
                str(item.get("name") or code),
                int(margin) if margin == int(margin) else margin,
                int(mult) if mult == int(mult) else mult,
            ]
        )
    # 列宽大致够看中文名和数字。
    sheet.column_dimensions["A"].width = 12
    sheet.column_dimensions["B"].width = 14
    sheet.column_dimensions["C"].width = 14
    sheet.column_dimensions["D"].width = 10
    book.save(file)
    return file


def ensure_margin_table(
    path: Path | str,
    contracts: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """
    表不存在时，用当前配置里的品种生成第一张；已存在则不动。

    返回实际文件路径。
    """
    file = Path(path)
    if file.exists():
        return file
    data = contracts or {}
    write_margin_table(file, data)
    logger.info("已生成初始品种保证金表: {}（共 {} 个品种）", file, len(data))
    return file


def apply_margin_table_to_cfg(
    cfg: dict[str, Any],
    *,
    path: Path | str | None = None,
    create_if_missing: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    用 Excel 覆盖 cfg['risk']['contracts']（品种保证金+乘数的唯一来源）。

    create_if_missing=True 时：没有表就按现有 contracts 先生成再读。
    返回读入后的 contracts。
    """
    risk = cfg.setdefault("risk", {})
    table_path = Path(path) if path is not None else resolve_table_path(cfg)
    # 配置里记下相对/原路径，方便界面显示；实际读写用绝对路径。
    if not str(risk.get("margin_table") or "").strip():
        risk["margin_table"] = DEFAULT_REL_PATH

    existing = contracts_from_cfg(cfg)
    if create_if_missing:
        ensure_margin_table(table_path, existing)

    loaded = read_margin_table(table_path)
    risk["contracts"] = {
        code: {
            "name": item["name"],
            "margin": float(item["margin"]),
            "multiplier": float(item["multiplier"]),
        }
        for code, item in loaded.items()
    }
    logger.info(
        "已从品种保证金表载入 {} 个品种: {}",
        len(loaded),
        table_path,
    )
    return risk["contracts"]


def _find_col(names: list[str], aliases: tuple[str, ...]) -> int | None:
    lowered = [n.lower() for n in names]
    for alias in aliases:
        key = alias.lower()
        if key in lowered:
            return lowered.index(key)
    return None


def _as_float(value: object, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
