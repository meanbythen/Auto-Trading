"""
品种与合约总表（Excel）

功能：一张表同时管「关注哪些合约」和「每手保证金 / 乘数 / 手续费」。
原理：只改这一处，启动和保存时读入 → 写入 strategy.symbols 与 risk.contracts。
      同品种（如 fu2701 / fu2705）保证金、乘数、手续费必须相同，否则拒绝载入并提示。
      表里没写到的品种，仍用配置页「每手保证金」兜底。
      每手手续费：开仓、平昨各扣一次；平今手续费可单独填（空=跟每手相同，0=平今免费）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_REL_PATH = "config/品种与合约.xlsx"
LEGACY_MARGIN_REL = "config/品种保证金.xlsx"
SHEET_NAME = "品种与合约"
# 每跳价格（最小变动价位）留空时，用交易配置里的全局 price_tick。
HEADERS = ("合约代码", "中文名", "每手保证金", "乘数", "每手手续费", "平今手续费", "启用", "每跳价格")


def product_key(symbol: str) -> str:
    """fu2701 / BU2612 → fu / bu。只留字母。"""
    return "".join(ch for ch in str(symbol or "").strip().lower() if "a" <= ch <= "z")


def resolve_universe_path(cfg: dict[str, Any] | None = None, root: str | Path | None = None) -> Path:
    """算出总表路径。优先 risk.universe_table，否则默认文件名。"""
    from src.utils.paths import project_root

    base = Path(root) if root else Path(str((cfg or {}).get("_project_root") or project_root()))
    risk = (cfg or {}).get("risk") or {}
    raw = str(risk.get("universe_table") or DEFAULT_REL_PATH).strip() or DEFAULT_REL_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return path


def _as_float(value: object, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_enabled(value: object) -> bool:
    text = str(value or "").strip().lower()
    if text in ("", "是", "y", "yes", "true", "1", "启用", "on"):
        return True
    if text in ("否", "n", "no", "false", "0", "停用", "off"):
        return False
    # 空单元格在 Excel 里常是 None，上面已当「是」；其它奇怪值也当启用，避免误关。
    return True


def _find_col(names: list[str], aliases: tuple[str, ...]) -> int | None:
    lowered = [n.lower() for n in names]
    for alias in aliases:
        key = alias.lower()
        if key in lowered:
            return lowered.index(key)
    return None


def rows_from_cfg(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    用当前配置拼出总表行：关注合约优先，再补上保证金表里有、但还没合约的品种（启用=否）。
    """
    st = cfg.get("strategy") or {}
    risk = cfg.get("risk") or {}
    symbols = [str(x).strip() for x in (st.get("symbols") or []) if str(x).strip()]
    contracts = risk.get("contracts") or {}
    rows: list[dict[str, Any]] = []
    seen_sym: set[str] = set()
    covered_products: set[str] = set()

    for sym in symbols:
        key = product_key(sym)
        if not key or sym.lower() in seen_sym:
            continue
        seen_sym.add(sym.lower())
        covered_products.add(key)
        box = contracts.get(key) if isinstance(contracts.get(key), dict) else {}
        box = box or {}
        rows.append(_normalize_row(
            {
                "symbol": sym,
                "name": str(box.get("name") or key),
                "margin": max(0.0, _as_float(box.get("margin"), 0.0)),
                "multiplier": max(0.1, _as_float(box.get("multiplier"), 10.0)),
                "commission": max(0.0, _as_float(box.get("commission"), 0.0)),
                "commission_close_today": _optional_close_today(box),
                "price_tick": _optional_tick(box),
                "enabled": True,
            }
        ))

    # 保证金里有、关注里还没有对应合约的品种：留一行启用=否，方便你以后填合约。
    for key, item in (contracts or {}).items():
        code = product_key(str(key))
        if not code or code in covered_products:
            continue
        if not isinstance(item, dict):
            continue
        covered_products.add(code)
        rows.append(_normalize_row(
            {
                "symbol": code,
                "name": str(item.get("name") or code),
                "margin": max(0.0, _as_float(item.get("margin"), 0.0)),
                "multiplier": max(0.1, _as_float(item.get("multiplier"), 10.0)),
                "commission": max(0.0, _as_float(item.get("commission"), 0.0)),
                "commission_close_today": _optional_close_today(item),
                "price_tick": _optional_tick(item),
                "enabled": False,
            }
        ))
    return rows


def _optional_close_today(box: dict[str, Any]) -> float | None:
    """配置里没写平今时返回 None（表里留空，表示跟每手手续费相同）。"""
    if "commission_close_today" not in box:
        return None
    raw = box.get("commission_close_today")
    if raw is None or raw == "":
        return None
    return max(0.0, _as_float(raw, 0.0))


def _optional_tick(box: dict[str, Any]) -> float | None:
    """配置里没写每跳价格时返回 None（表里留空，表示用全局 price_tick）。"""
    raw = box.get("price_tick")
    if raw is None or raw == "":
        return None
    return max(0.0, _as_float(raw, 0.0))


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """补齐手续费/每跳字段，方便写表和校验。"""
    out = dict(row)
    out["commission"] = max(0.0, _as_float(out.get("commission"), 0.0))
    if "commission_close_today" not in out:
        out["commission_close_today"] = None
    elif out.get("commission_close_today") == "":
        out["commission_close_today"] = None
    elif out.get("commission_close_today") is not None:
        out["commission_close_today"] = max(0.0, _as_float(out.get("commission_close_today"), 0.0))
    out["price_tick"] = _optional_tick(out)
    return out


def _excel_number(value: float | None) -> float | int | None:
    if value is None:
        return None
    num = float(value)
    return int(num) if num == int(num) else num


def write_universe_table(path: Path | str, rows: list[dict[str, Any]]) -> Path:
    """整表重写 Excel。"""
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 openpyxl 才能写品种与合约表。请执行: pip install openpyxl") from exc

    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = SHEET_NAME
    sheet.append(list(HEADERS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        item = _normalize_row(row)
        margin = float(item.get("margin") or 0)
        mult = float(item.get("multiplier") or 10)
        fee = float(item.get("commission") or 0)
        today = item.get("commission_close_today")
        tick = item.get("price_tick")
        sheet.append(
            [
                str(item.get("symbol") or ""),
                str(item.get("name") or ""),
                _excel_number(margin),
                _excel_number(mult),
                _excel_number(fee),
                _excel_number(today) if today is not None else None,
                "是" if item.get("enabled", True) else "否",
                _excel_number(tick) if tick is not None else None,
            ]
        )
    sheet.column_dimensions["A"].width = 12
    sheet.column_dimensions["B"].width = 14
    sheet.column_dimensions["C"].width = 14
    sheet.column_dimensions["D"].width = 10
    sheet.column_dimensions["E"].width = 12
    sheet.column_dimensions["F"].width = 12
    sheet.column_dimensions["G"].width = 8
    sheet.column_dimensions["H"].width = 12
    book.save(file)
    return file


def read_universe_table(path: Path | str) -> list[dict[str, Any]]:
    """
    读总表全部行。

    抛出:
        同品种保证金或乘数不一致时 ValueError（避免静默用错数）。
    """
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"品种与合约表不存在: {file}")
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 openpyxl 才能读品种与合约表。请执行: pip install openpyxl") from exc

    book = openpyxl.load_workbook(file, data_only=True, read_only=True)
    try:
        sheet = book[SHEET_NAME] if SHEET_NAME in book.sheetnames else book[book.sheetnames[0]]
        rows_iter = sheet.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if not header:
            return []
        names = [str(c or "").strip() for c in header]
        idx_sym = _find_col(names, ("合约代码", "合约", "代码", "symbol", "code"))
        idx_name = _find_col(names, ("中文名", "名称", "name"))
        idx_margin = _find_col(names, ("每手保证金", "保证金", "margin"))
        idx_mult = _find_col(names, ("乘数", "合约乘数", "multiplier"))
        idx_fee = _find_col(names, ("每手手续费", "手续费", "开仓手续费", "commission"))
        idx_today = _find_col(names, ("平今手续费", "平今", "commission_close_today"))
        idx_on = _find_col(names, ("启用", "关注", "enabled", "on"))
        idx_tick = _find_col(names, ("每跳价格", "每跳", "最小变动价位", "tick", "price_tick"))
        if idx_sym is None or idx_margin is None:
            raise ValueError("表格至少要有「合约代码」「每手保证金」两列")

        rows: list[dict[str, Any]] = []
        for raw in rows_iter:
            if not raw:
                continue
            sym = str(raw[idx_sym] if idx_sym < len(raw) else "" or "").strip()
            if not sym:
                continue
            name_raw = raw[idx_name] if idx_name is not None and idx_name < len(raw) else ""
            name = str(name_raw or "").strip() or product_key(sym) or sym
            margin = max(0.0, _as_float(raw[idx_margin] if idx_margin < len(raw) else 0, 0.0))
            mult = 10.0
            if idx_mult is not None and idx_mult < len(raw):
                mult = max(0.1, _as_float(raw[idx_mult], 10.0))
            fee = 0.0
            if idx_fee is not None and idx_fee < len(raw):
                fee = max(0.0, _as_float(raw[idx_fee], 0.0))
            today: float | None = None
            if idx_today is not None and idx_today < len(raw):
                cell = raw[idx_today]
                if cell is not None and str(cell).strip() != "":
                    today = max(0.0, _as_float(cell, 0.0))
            enabled = True
            if idx_on is not None and idx_on < len(raw):
                enabled = _as_enabled(raw[idx_on])
            tick: float | None = None
            if idx_tick is not None and idx_tick < len(raw):
                cell = raw[idx_tick]
                if cell is not None and str(cell).strip() != "":
                    tick = max(0.0, _as_float(cell, 0.0))
            rows.append(
                _normalize_row(
                    {
                        "symbol": sym,
                        "name": name,
                        "margin": margin,
                        "multiplier": mult,
                        "commission": fee,
                        "commission_close_today": today,
                        "price_tick": tick,
                        "enabled": enabled,
                    }
                )
            )
        _assert_product_margins_consistent(rows)
        return rows
    finally:
        book.close()


def _fee_signature(row: dict[str, Any]) -> tuple[float, float | None]:
    fee = float(row.get("commission") or 0)
    today = row.get("commission_close_today")
    if today is None or today == "":
        return fee, None
    return fee, float(today)


def _assert_product_margins_consistent(rows: list[dict[str, Any]]) -> None:
    """同品种多行时，保证金、乘数、手续费、每跳价格必须完全一致。"""
    seen: dict[str, tuple[float, float, tuple[float, float | None], float | None, str]] = {}
    for row in rows:
        key = product_key(str(row.get("symbol") or ""))
        if not key:
            continue
        margin = float(row.get("margin") or 0)
        mult = float(row.get("multiplier") or 10)
        fees = _fee_signature(row)
        tick = _optional_tick(row)
        prev = seen.get(key)
        if prev is None:
            seen[key] = (margin, mult, fees, tick, str(row.get("symbol")))
            continue
        if (
            abs(prev[0] - margin) > 1e-9
            or abs(prev[1] - mult) > 1e-9
            or prev[2] != fees
            or prev[3] != tick
        ):
            raise ValueError(
                f"品种 {key} 出现不同的保证金/乘数/手续费/每跳价格："
                f"{prev[4]} 是保证金{prev[0]:g}、乘数{prev[1]:g}、"
                f"每手手续费{prev[2][0]:g}、平今{prev[2][1] if prev[2][1] is not None else '空'}、"
                f"每跳{prev[3] if prev[3] is not None else '空'}；"
                f"{row.get('symbol')} 是保证金{margin:g}、乘数{mult:g}、"
                f"每手手续费{fees[0]:g}、平今{fees[1] if fees[1] is not None else '空'}、"
                f"每跳{tick if tick is not None else '空'}。"
                f"同品种必须填成一样。"
            )


def parse_universe_rows(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """
    拆成：启用中的合约列表 + 品种→{name,margin,multiplier,commission,...}。

    未启用的行仍贡献品种保证金（方便以后打开启用），但不进关注名单。
    """
    symbols: list[str] = []
    seen_sym: set[str] = set()
    contracts: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _normalize_row(row)
        sym = str(item.get("symbol") or "").strip()
        key = product_key(sym)
        if not key:
            continue
        # 合约代码若只是品种字母（如 c），不当成可交易合约，除非带数字。
        has_month = any(ch.isdigit() for ch in sym)
        if item.get("enabled", True) and has_month and sym.lower() not in seen_sym:
            seen_sym.add(sym.lower())
            symbols.append(sym)
        box: dict[str, Any] = {
            "name": str(item.get("name") or key),
            "margin": float(item.get("margin") or 0),
            "multiplier": float(item.get("multiplier") or 10),
            "commission": float(item.get("commission") or 0),
        }
        if item.get("commission_close_today") is not None:
            box["commission_close_today"] = float(item["commission_close_today"])
        tick = _optional_tick(item)
        if tick is not None:
            box["price_tick"] = tick
        contracts[key] = box
    return symbols, contracts


def _merge_legacy_margins_into_cfg(cfg: dict[str, Any], legacy_path: Path) -> None:
    """生成总表前，用旧「品种保证金.xlsx」把缺的保证金/乘数补进 cfg。"""
    if not legacy_path.exists():
        return
    try:
        from src.risk.margin_table import read_margin_table

        legacy_map = read_margin_table(legacy_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读旧保证金表失败，生成总表时跳过: {}", exc)
        return
    contracts = cfg.setdefault("risk", {}).setdefault("contracts", {})
    for code, item in legacy_map.items():
        box = contracts.get(code)
        if not isinstance(box, dict):
            box = {"name": item.get("name") or code, "margin": 0.0, "multiplier": 10.0}
            contracts[code] = box
        if float(box.get("margin") or 0) <= 0 and float(item.get("margin") or 0) > 0:
            box["margin"] = float(item["margin"])
        if float(box.get("multiplier") or 0) <= 0:
            box["multiplier"] = float(item.get("multiplier") or 10)
        if not str(box.get("name") or "").strip() or str(box.get("name")) == code:
            if item.get("name"):
                box["name"] = str(item["name"])


def ensure_universe_table(path: Path | str, cfg: dict[str, Any]) -> Path:
    """没有总表时，按当前配置（关注合约+保证金，必要时合并旧表）生成第一张。"""
    file = Path(path)
    if file.exists():
        return file
    legacy = file.parent / Path(LEGACY_MARGIN_REL).name
    _merge_legacy_margins_into_cfg(cfg, legacy)
    rows = rows_from_cfg(cfg)
    write_universe_table(file, rows)
    logger.info("已生成品种与合约总表: {}（{} 行）", file, len(rows))
    return file


def apply_universe_table_to_cfg(
    cfg: dict[str, Any],
    *,
    path: Path | str | None = None,
    create_if_missing: bool = True,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """
    用总表覆盖 cfg 里的关注合约和品种保证金。

    返回 (symbols, contracts)。
    """
    risk = cfg.setdefault("risk", {})
    st = cfg.setdefault("strategy", {})
    table_path = Path(path) if path is not None else resolve_universe_path(cfg)
    risk["universe_table"] = str(risk.get("universe_table") or DEFAULT_REL_PATH)
    # 兼容旧键：界面若还显示 margin_table，指向同一张总表。
    risk["margin_table"] = str(risk.get("universe_table") or DEFAULT_REL_PATH)

    if create_if_missing:
        ensure_universe_table(table_path, cfg)

    rows = read_universe_table(table_path)
    # 旧表没有手续费列时，写回一遍，让 Excel 里出现新列（数字先是 0）。
    upgrade_universe_fee_columns(table_path, rows)
    # 旧表没有每跳价格列时也补上，留空表示用全局 price_tick。
    upgrade_universe_tick_column(table_path, rows)
    symbols, contracts = parse_universe_rows(rows)
    if symbols:
        st["symbols"] = symbols
        trade = cfg.setdefault("trade", {})
        if isinstance(trade, dict):
            trade["symbol"] = symbols[0]
    risk["contracts"] = {}
    for code, item in contracts.items():
        box = {
            "name": item["name"],
            "margin": float(item["margin"]),
            "multiplier": float(item["multiplier"]),
            "commission": float(item.get("commission") or 0),
        }
        if "commission_close_today" in item:
            box["commission_close_today"] = float(item["commission_close_today"])
        tick = _optional_tick(item)
        if tick is not None:
            box["price_tick"] = tick
        risk["contracts"][code] = box
    logger.info(
        "已从品种与合约表载入：关注 {} 个合约、{} 个品种保证金/手续费 → {}",
        len(symbols),
        len(contracts),
        table_path,
    )
    return symbols, contracts


def _universe_headers_have(path: Path | str, aliases: tuple[str, ...]) -> bool:
    """表头里是否已经有某一列。"""
    file = Path(path)
    if not file.exists():
        return False
    try:
        import openpyxl
    except ImportError:
        return False
    book = openpyxl.load_workbook(file, data_only=True, read_only=True)
    try:
        sheet = book[SHEET_NAME] if SHEET_NAME in book.sheetnames else book[book.sheetnames[0]]
        header = next(sheet.iter_rows(values_only=True), None)
        if not header:
            return False
        names = [str(c or "").strip() for c in header]
        return _find_col(names, aliases) is not None
    finally:
        book.close()


def universe_headers_have_fees(path: Path | str) -> bool:
    """表头里是否已经有手续费列。"""
    return _universe_headers_have(path, ("每手手续费", "手续费", "开仓手续费", "commission"))


def upgrade_universe_fee_columns(path: Path | str, rows: list[dict[str, Any]] | None = None) -> bool:
    """旧表缺手续费列时重写表头；返回是否写过文件。"""
    file = Path(path)
    if universe_headers_have_fees(file):
        return False
    data = rows if rows is not None else read_universe_table(file)
    write_universe_table(file, data)
    logger.info("品种与合约表已补上手续费列: {}", file)
    return True


def upgrade_universe_tick_column(path: Path | str, rows: list[dict[str, Any]] | None = None) -> bool:
    """旧表缺「每跳价格」列时补一列（先留空，表示用全局 price_tick）。"""
    file = Path(path)
    if _universe_headers_have(file, ("每跳价格", "每跳", "最小变动价位", "tick", "price_tick")):
        return False
    data = rows if rows is not None else read_universe_table(file)
    write_universe_table(file, data)
    logger.info("品种与合约表已补上每跳价格列: {}", file)
    return True


def summarize_universe(symbols: list[str], contracts: dict[str, dict[str, Any]]) -> str:
    """界面摘要。"""
    sym_text = ",".join(symbols[:8])
    if len(symbols) > 8:
        sym_text += f"…共{len(symbols)}个"
    margins = []
    fees = []
    for key in sorted(contracts.keys()):
        m = float(contracts[key].get("margin") or 0)
        if m > 0:
            text = str(int(m)) if m == int(m) else str(m)
            margins.append(f"{key}:{text}")
        fee = float(contracts[key].get("commission") or 0)
        today = contracts[key].get("commission_close_today")
        if fee > 0 or today is not None:
            today_text = "空" if today is None else f"{float(today):g}"
            fees.append(f"{key}:{fee:g}/{today_text}")
    parts = []
    if sym_text:
        parts.append(f"关注 {sym_text}")
    if margins:
        parts.append(f"保证金 {','.join(margins)}")
    if fees:
        parts.append(f"手续费(开/平今) {','.join(fees)}")
    ticks = []
    for key in sorted(contracts.keys()):
        tick = contracts[key].get("price_tick")
        if tick:
            ticks.append(f"{key}:{float(tick):g}")
    if ticks:
        parts.append(f"每跳 {','.join(ticks)}")
    return "；".join(parts) if parts else "表是空的"


def sync_watch_symbols_to_table(
    path: Path | str,
    symbols: list[str],
    *,
    contracts: dict[str, dict[str, Any]] | None = None,
) -> None:
    """
    界面添加/移出关注后，把启用名单写回总表（保留未启用行和原保证金）。

    原理：读出现有表 → 启用集合改成 symbols → 缺的合约补行 → 写回。
    """
    file = Path(path)
    existing: list[dict[str, Any]] = []
    if file.exists():
        existing = read_universe_table(file)
    by_sym = {str(r["symbol"]).lower(): dict(r) for r in existing}
    wanted = [str(s).strip() for s in symbols if str(s).strip()]
    wanted_keys = {w.lower() for w in wanted}
    contracts = contracts or {}

    # 先把不在名单里的旧启用行改成否。
    for row in existing:
        if str(row.get("symbol") or "").lower() not in wanted_keys:
            row["enabled"] = False

    new_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sym in wanted:
        low = sym.lower()
        key = product_key(sym)
        if low in by_sym:
            row = by_sym[low]
            row["enabled"] = True
            new_rows.append(row)
        else:
            box = contracts.get(key) if isinstance(contracts.get(key), dict) else {}
            box = box or {}
            new_rows.append(
                _normalize_row(
                    {
                        "symbol": sym,
                        "name": str(box.get("name") or key or sym),
                        "margin": float(box.get("margin") or 0),
                        "multiplier": float(box.get("multiplier") or 10),
                        "commission": float(box.get("commission") or 0),
                        "commission_close_today": _optional_close_today(box),
                "price_tick": _optional_tick(box),
                        "enabled": True,
                    }
                )
            )
        seen.add(low)
    # 追加未启用的其它旧行
    for row in existing:
        low = str(row.get("symbol") or "").lower()
        if low and low not in seen:
            row["enabled"] = False
            new_rows.append(row)
            seen.add(low)
    _assert_product_margins_consistent(new_rows)
    write_universe_table(file, new_rows)
