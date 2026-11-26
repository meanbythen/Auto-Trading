"""
品种与合约总表（Excel）

功能：一张表同时管「关注哪些合约」和「保证金比例 / 每手数量 / 手续费」。
原理：只改这一处，启动和保存时读入 → 写入 strategy.symbols 与 risk.contracts。
      开仓占用 = 开仓价 × 每手数量 × 保证金比例%。价格涨了，同一手要交的钱也变多。
      只有比例是唯一口径，不再保留「每手保证金」固定金额，避免长期不更新导致占用失真。
      同品种（如 fu2701 / fu2705）比例、每手数量、手续费必须相同，否则拒绝载入并提示。
      没填比例的行读入后比例为 0；实盘/回测算仓时会拒绝开仓并提示补比例。
      每手数量就是合约乘数（CTP「每手数量」），1 点价格变动时每手盈亏≈这个数（元）。
      开仓手续费 / 平仓手续费 / 平今手续费：每手多少元。
      平仓手续费空着时跟开仓相同；平今空着时跟平仓（或开仓）相同；填 0 表示免费。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from src.domain.contracts import DEFAULT_MULTIPLIER

# 点「启动常驻」时界面会连着读配置、读总表、再存盘，几秒内同一张表会被问很多次。
# 文件没改就复用刚读过的结果，避免重复打开 Excel，也不改开平仓规则。
_UNIVERSE_CACHE: dict[str, tuple[float, float, list[str], dict[str, dict[str, Any]]]] = {}
_UNIVERSE_CACHE_SEC = 3.0
_LAST_UNIVERSE_LOG = ""

DEFAULT_REL_PATH = "config/品种与合约.xlsx"
SHEET_NAME = "品种与合约"
# 每跳价格（最小变动价位）留空时，用交易配置里的全局 price_tick。
HEADERS = (
    "合约代码",
    "中文名",
    "保证金比例%",
    "每手数量",
    "开仓手续费",
    "平仓手续费",
    "平今手续费",
    "启用",
    "每跳价格",
    "交易时段",
    "清仓时刻",
    "合约类型",
    "标的",
    "到期日",
    "行权价",
    "期权方向",
)

# 期权专用列。旧表没有这几列时，启动会整表重写补上（空值不填）。
OPTION_HEADERS = ("合约类型", "标的", "到期日", "行权价", "期权方向")


def product_key(symbol: str) -> str:
    """fu2701 / BU2612 → fu / bu；期权取标的品种（rb2601C3500 → rb）。"""
    from src.domain.options import option_product

    return option_product(symbol)


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


def parse_margin_rate_pct(value: object) -> float:
    """
    把表格里的保证金比例收成「百分之几」。

    23 或 23% → 23；0.23 → 也当成 23%（CTP 回报是小数）。
    大于 100 的数明显不是保证金比例，按无效处理（返回 0）。
    """
    text = str(value or "").strip().replace("%", "")
    if not text:
        return 0.0
    try:
        num = float(text)
    except (TypeError, ValueError):
        return 0.0
    if num <= 0:
        return 0.0
    if num <= 1.0:
        return round(num * 100.0, 4)
    if num <= 100.0:
        return round(num, 4)
    return 0.0


def _as_float(value: Any, default: float) -> float:
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

    from src.domain.options import is_option

    for sym in symbols:
        is_opt = is_option(sym)
        key = sym.lower() if is_opt else product_key(sym)
        if not key or sym.lower() in seen_sym:
            continue
        seen_sym.add(sym.lower())
        covered_products.add(key)
        box = contracts.get(key) if isinstance(contracts.get(key), dict) else {}
        if not box and not is_opt:
            box = contracts.get(product_key(sym)) if isinstance(contracts.get(product_key(sym)), dict) else {}
        box = box or {}
        rows.append(_normalize_row(
            {
                "symbol": sym,
                "name": str(box.get("name") or product_key(sym)),
                "margin_rate_pct": parse_margin_rate_pct(box.get("margin_rate_pct")),
                "multiplier": max(0.1, _as_float(box.get("multiplier"), 10.0)),
                "commission": max(0.0, _as_float(box.get("commission"), 0.0)),
                "commission_close": _optional_named(box, "commission_close"),
                "commission_close_today": _optional_close_today(box),
                "price_tick": _optional_tick(box),
                "trading_sessions": str(box.get("trading_sessions") or ""),
                "flatten_times": str(box.get("flatten_times") or ""),
                "instrument_type": str(box.get("instrument_type") or ("期权" if is_opt else "")),
                "underlying": str(box.get("underlying") or (product_key(sym) if is_opt else "")),
                "expiry": str(box.get("expiry") or ""),
                "strike": _as_float(box.get("strike"), 0.0),
                "option_type": str(box.get("option_type") or ""),
                "enabled": True,
            }
        ))

    # 保证金里有、关注里还没有对应合约的品种：留一行启用=否，方便你以后填合约。
    for key, item in (contracts or {}).items():
        raw_key = str(key)
        is_opt = is_option(raw_key)
        code = raw_key.lower() if is_opt else product_key(raw_key)
        if not code or code in covered_products:
            continue
        if not isinstance(item, dict):
            continue
        covered_products.add(code)
        rows.append(_normalize_row(
            {
                "symbol": raw_key,
                "name": str(item.get("name") or product_key(raw_key)),
                "margin_rate_pct": parse_margin_rate_pct(item.get("margin_rate_pct")),
                "multiplier": max(0.1, _as_float(item.get("multiplier"), 10.0)),
                "commission": max(0.0, _as_float(item.get("commission"), 0.0)),
                "commission_close": _optional_named(item, "commission_close"),
                "commission_close_today": _optional_close_today(item),
                "price_tick": _optional_tick(item),
                "trading_sessions": str(item.get("trading_sessions") or ""),
                "flatten_times": str(item.get("flatten_times") or ""),
                "instrument_type": str(item.get("instrument_type") or ("期权" if is_opt else "")),
                "underlying": str(item.get("underlying") or (product_key(raw_key) if is_opt else "")),
                "expiry": str(item.get("expiry") or ""),
                "strike": _as_float(item.get("strike"), 0.0),
                "option_type": str(item.get("option_type") or ""),
                "enabled": False,
            }
        ))
    return rows


def _optional_named(box: dict[str, Any], key: str) -> float | None:
    """配置里没写这一项时返回 None（表里留空）。写了 0 也保留。"""
    if key not in box:
        return None
    raw = box.get(key)
    if raw is None or raw == "":
        return None
    return max(0.0, _as_float(raw, 0.0))


def _optional_close_today(box: dict[str, Any]) -> float | None:
    """配置里没写平今时返回 None（表里留空，表示跟平仓/开仓相同）。"""
    return _optional_named(box, "commission_close_today")


def _optional_tick(box: dict[str, Any]) -> float | None:
    """配置里没写每跳价格时返回 None（表里留空，表示用全局 price_tick）。"""
    raw = box.get("price_tick")
    if raw is None or raw == "":
        return None
    return max(0.0, _as_float(raw, 0.0))


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """补齐手续费/每跳字段，方便写表和校验。"""
    out = dict(row)
    out["margin_rate_pct"] = parse_margin_rate_pct(out.get("margin_rate_pct"))
    out["multiplier"] = max(0.1, _as_float(out.get("multiplier"), 10.0))
    out["commission"] = max(0.0, _as_float(out.get("commission"), 0.0))
    for fee_key in ("commission_close", "commission_close_today"):
        if fee_key not in out:
            out[fee_key] = None
        elif out.get(fee_key) == "":
            out[fee_key] = None
        elif out.get(fee_key) is not None:
            out[fee_key] = max(0.0, _as_float(out.get(fee_key), 0.0))
    out["price_tick"] = _optional_tick(out)
    out["trading_sessions"] = str(out.get("trading_sessions") or "").strip()
    out["flatten_times"] = str(out.get("flatten_times") or "").strip()
    out["instrument_type"] = str(out.get("instrument_type") or "").strip()
    out["underlying"] = str(out.get("underlying") or "").strip().lower()
    out["expiry"] = str(out.get("expiry") or "").strip()
    out["strike"] = _as_float(out.get("strike"), 0.0)
    out["option_type"] = str(out.get("option_type") or "").strip().upper()
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
        rate = float(item.get("margin_rate_pct") or 0)
        mult = float(item.get("multiplier") or DEFAULT_MULTIPLIER)
        fee = float(item.get("commission") or 0)
        close_fee = item.get("commission_close")
        today = item.get("commission_close_today")
        tick = item.get("price_tick")
        sheet.append(
            [
                str(item.get("symbol") or ""),
                str(item.get("name") or ""),
                _excel_number(rate) if rate > 0 else None,
                _excel_number(mult),
                _excel_number(fee),
                _excel_number(close_fee) if close_fee is not None else None,
                _excel_number(today) if today is not None else None,
                "是" if item.get("enabled", True) else "否",
                _excel_number(tick) if tick is not None else None,
                str(item.get("trading_sessions") or ""),
                str(item.get("flatten_times") or ""),
                str(item.get("instrument_type") or ""),
                str(item.get("underlying") or ""),
                str(item.get("expiry") or ""),
                _excel_number(float(item.get("strike") or 0)) if float(item.get("strike") or 0) > 0 else None,
                str(item.get("option_type") or ""),
            ]
        )
    sheet.column_dimensions["A"].width = 12
    sheet.column_dimensions["B"].width = 14
    sheet.column_dimensions["C"].width = 14
    sheet.column_dimensions["D"].width = 12
    sheet.column_dimensions["E"].width = 12
    sheet.column_dimensions["F"].width = 12
    sheet.column_dimensions["G"].width = 12
    sheet.column_dimensions["H"].width = 8
    sheet.column_dimensions["I"].width = 12
    sheet.column_dimensions["J"].width = 42
    sheet.column_dimensions["K"].width = 16
    sheet.column_dimensions["L"].width = 10
    sheet.column_dimensions["M"].width = 10
    sheet.column_dimensions["N"].width = 12
    sheet.column_dimensions["O"].width = 10
    sheet.column_dimensions["P"].width = 10
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
        idx_rate = _find_col(
            names,
            ("保证金比例%", "保证金比例", "保证金率", "margin_rate", "margin_rate_pct"),
        )
        idx_qty = _find_col(names, ("每手数量", "volume_multiple", "VolumeMultiple"))
        idx_mult = _find_col(names, ("乘数", "合约乘数", "multiplier"))
        idx_fee = _find_col(names, ("开仓手续费", "每手手续费", "手续费", "commission"))
        idx_close_fee = _find_col(names, ("平仓手续费", "平昨手续费", "commission_close"))
        idx_today = _find_col(names, ("平今手续费", "平今", "commission_close_today"))
        idx_on = _find_col(names, ("启用", "关注", "enabled", "on"))
        idx_tick = _find_col(names, ("每跳价格", "每跳", "最小变动价位", "tick", "price_tick"))
        idx_sess = _find_col(names, ("交易时段", "时段", "trading_sessions", "sessions"))
        idx_flat = _find_col(names, ("清仓时刻", "清仓", "flatten_times", "flatten"))
        idx_type = _find_col(names, ("合约类型", "类型", "instrument_type"))
        idx_under = _find_col(names, ("标的", "标的品种", "underlying"))
        idx_expiry = _find_col(
            names, ("到期日", "最后交易日", "expiry", "expire_date", "last_trading_day")
        )
        idx_strike = _find_col(names, ("行权价", "strike"))
        idx_kind = _find_col(names, ("期权方向", "看涨看跌", "option_type"))
        if idx_sym is None:
            raise ValueError("表格至少要有「合约代码」")
        if idx_rate is None:
            raise ValueError("表格要有「保证金比例%」；保证金只按 价格×每手数量×比例 计算")

        rows: list[dict[str, Any]] = []
        for raw in rows_iter:
            if not raw:
                continue
            sym = str(raw[idx_sym] if idx_sym < len(raw) else "" or "").strip()
            if not sym:
                continue
            name_raw = raw[idx_name] if idx_name is not None and idx_name < len(raw) else ""
            name = str(name_raw or "").strip() or product_key(sym) or sym
            rate = 0.0
            if idx_rate is not None and idx_rate < len(raw):
                rate = parse_margin_rate_pct(raw[idx_rate])
            mult = 10.0
            if idx_qty is not None and idx_qty < len(raw):
                mult = max(0.1, _as_float(raw[idx_qty], 10.0))
            elif idx_mult is not None and idx_mult < len(raw):
                mult = max(0.1, _as_float(raw[idx_mult], 10.0))
            fee = 0.0
            if idx_fee is not None and idx_fee < len(raw):
                fee = max(0.0, _as_float(raw[idx_fee], 0.0))
            close_fee: float | None = None
            if idx_close_fee is not None and idx_close_fee < len(raw):
                cell = raw[idx_close_fee]
                if cell is not None and str(cell).strip() != "":
                    close_fee = max(0.0, _as_float(cell, 0.0))
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
            sessions = ""
            if idx_sess is not None and idx_sess < len(raw):
                sessions = str(raw[idx_sess] or "").strip()
            flats = ""
            if idx_flat is not None and idx_flat < len(raw):
                flats = str(raw[idx_flat] or "").strip()
            instrument_type = ""
            if idx_type is not None and idx_type < len(raw):
                instrument_type = str(raw[idx_type] or "").strip()
            underlying = ""
            if idx_under is not None and idx_under < len(raw):
                underlying = str(raw[idx_under] or "").strip()
            expiry = ""
            if idx_expiry is not None and idx_expiry < len(raw):
                expiry = str(raw[idx_expiry] or "").strip()
            strike = 0.0
            if idx_strike is not None and idx_strike < len(raw):
                strike = max(0.0, _as_float(raw[idx_strike], 0.0))
            option_type = ""
            if idx_kind is not None and idx_kind < len(raw):
                option_type = str(raw[idx_kind] or "").strip()
            rows.append(
                _normalize_row(
                    {
                        "symbol": sym,
                        "name": name,
                        "margin_rate_pct": rate,
                        "multiplier": mult,
                        "commission": fee,
                        "commission_close": close_fee,
                        "commission_close_today": today,
                        "price_tick": tick,
                        "trading_sessions": sessions,
                        "flatten_times": flats,
                        "instrument_type": instrument_type,
                        "underlying": underlying,
                        "expiry": expiry,
                        "strike": strike,
                        "option_type": option_type,
                        "enabled": enabled,
                    }
                )
            )
        _assert_product_margins_consistent(rows)
        return rows
    finally:
        book.close()


def _fee_signature(row: dict[str, Any]) -> tuple[float, float | None, float | None]:
    fee = float(row.get("commission") or 0)
    close_fee = row.get("commission_close")
    today = row.get("commission_close_today")
    close_v = None if close_fee is None or close_fee == "" else float(close_fee)
    today_v = None if today is None or today == "" else float(today)
    return fee, close_v, today_v


def _assert_product_margins_consistent(rows: list[dict[str, Any]]) -> None:
    """同品种多行时，保证金比例、每手数量、手续费、每跳价格必须完全一致。

    期权每个行权价都是独立合约，比例/乘数可以不同，按完整合约各自校验。
    """
    from src.domain.options import is_option

    seen: dict[str, tuple[float, float, tuple[float, float | None, float | None], float | None, str]] = {}
    for row in rows:
        text = str(row.get("symbol") or "")
        key = text.strip().lower() if is_option(text) else product_key(text)
        if not key:
            continue
        rate = float(row.get("margin_rate_pct") or 0)
        mult = float(row.get("multiplier") or DEFAULT_MULTIPLIER)
        fees = _fee_signature(row)
        tick = _optional_tick(row)
        prev = seen.get(key)
        if prev is None:
            seen[key] = (rate, mult, fees, tick, str(row.get("symbol")))
            continue
        if (
            abs(prev[0] - rate) > 1e-9
            or abs(prev[1] - mult) > 1e-9
            or prev[2] != fees
            or prev[3] != tick
        ):
            raise ValueError(
                f"品种 {key} 出现不同的保证金比例/每手数量/手续费/每跳价格："
                f"{prev[4]} 是比例{prev[0]:g}%、每手数量{prev[1]:g}、"
                f"开仓手续费{prev[2][0]:g}、平仓{prev[2][1] if prev[2][1] is not None else '空'}、"
                f"平今{prev[2][2] if prev[2][2] is not None else '空'}、"
                f"每跳{prev[3] if prev[3] is not None else '空'}；"
                f"{row.get('symbol')} 是比例{rate:g}%、每手数量{mult:g}、"
                f"开仓手续费{fees[0]:g}、平仓{fees[1] if fees[1] is not None else '空'}、"
                f"平今{fees[2] if fees[2] is not None else '空'}、"
                f"每跳{tick if tick is not None else '空'}。"
                f"同品种必须填成一样。"
            )


def parse_universe_rows(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """
    拆成：启用中的合约列表 + 品种→{name,margin_rate_pct,multiplier,commission,...}。

    未启用的行仍贡献品种保证金比例（方便以后打开启用），但不进关注名单。
    """
    symbols: list[str] = []
    seen_sym: set[str] = set()
    contracts: dict[str, dict[str, Any]] = {}
    from src.domain.options import is_option

    for row in rows:
        item = _normalize_row(row)
        sym = str(item.get("symbol") or "").strip()
        is_opt = is_option(sym)
        key = sym.lower() if is_opt else product_key(sym)
        if not key:
            continue
        # 合约代码若只是品种字母（如 c），不当成可交易合约，除非带数字。
        has_month = any(ch.isdigit() for ch in sym)
        if item.get("enabled", True) and has_month and sym.lower() not in seen_sym:
            seen_sym.add(sym.lower())
            symbols.append(sym)
        box: dict[str, Any] = {
            "name": str(item.get("name") or key),
            "margin_rate_pct": float(item.get("margin_rate_pct") or 0),
            "multiplier": float(item.get("multiplier") or DEFAULT_MULTIPLIER),
            "commission": float(item.get("commission") or 0),
        }
        if item.get("commission_close") is not None:
            box["commission_close"] = float(item["commission_close"])
        if item.get("commission_close_today") is not None:
            box["commission_close_today"] = float(item["commission_close_today"])
        tick = _optional_tick(item)
        if tick is not None:
            box["price_tick"] = tick
        sess = str(item.get("trading_sessions") or "").strip()
        if sess:
            box["trading_sessions"] = sess
        flats = str(item.get("flatten_times") or "").strip()
        if flats:
            box["flatten_times"] = flats
        if is_opt:
            box["instrument_type"] = str(item.get("instrument_type") or "期权")
            box["underlying"] = str(item.get("underlying") or product_key(sym))
            if item.get("expiry"):
                box["expiry"] = str(item["expiry"])
            if float(item.get("strike") or 0) > 0:
                box["strike"] = float(item["strike"])
            if item.get("option_type"):
                box["option_type"] = str(item["option_type"]).upper()
        contracts[key] = box
    return symbols, contracts


def ensure_universe_table(path: Path | str, cfg: dict[str, Any]) -> Path:
    """没有总表时，按当前配置（关注合约+保证金比例）生成第一张。"""
    file = Path(path)
    if file.exists():
        return file
    rows = rows_from_cfg(cfg)
    write_universe_table(file, rows)
    logger.info("已生成品种与合约总表: {}（{} 行）", file, len(rows))
    return file


def _universe_fingerprint(
    table_path: Path, symbols: list[str], contracts: dict[str, dict[str, Any]]
) -> str:
    """用来判断「和刚才读到的是不是同一份」，避免启动时刷同一句日志。"""
    bits = [str(table_path), ",".join(symbols)]
    for code in sorted(contracts):
        item = contracts[code]
        bits.append(
            f"{code}:{item.get('margin_rate_pct')}:"
            f"{item.get('multiplier')}:{item.get('commission')}:"
            f"{item.get('commission_close')}:{item.get('commission_close_today')}"
        )
    return "|".join(bits)


def _put_universe_into_cfg(
    cfg: dict[str, Any],
    symbols: list[str],
    contracts: dict[str, dict[str, Any]],
) -> None:
    """把已经解析好的关注名单和保证金写进配置字典。"""
    risk = cfg.setdefault("risk", {})
    st = cfg.setdefault("strategy", {})
    if symbols:
        st["symbols"] = list(symbols)
        trade = cfg.setdefault("trade", {})
        if isinstance(trade, dict):
            trade["symbol"] = symbols[0]
    risk["contracts"] = {}
    for code, item in contracts.items():
        box = {
            "name": item["name"],
            "margin_rate_pct": float(item.get("margin_rate_pct") or 0),
            "multiplier": float(item["multiplier"]),
            "commission": float(item.get("commission") or 0),
        }
        if "commission_close" in item:
            box["commission_close"] = float(item["commission_close"])
        if "commission_close_today" in item:
            box["commission_close_today"] = float(item["commission_close_today"])
        tick = _optional_tick(item)
        if tick is not None:
            box["price_tick"] = tick
        if item.get("trading_sessions"):
            box["trading_sessions"] = str(item["trading_sessions"])
        if item.get("flatten_times"):
            box["flatten_times"] = str(item["flatten_times"])
        for field in ("instrument_type", "underlying", "expiry", "option_type"):
            if item.get(field):
                box[field] = str(item[field])
        if float(item.get("strike") or 0) > 0:
            box["strike"] = float(item["strike"])
        risk["contracts"][code] = box


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
    global _LAST_UNIVERSE_LOG
    risk = cfg.setdefault("risk", {})
    table_path = Path(path) if path is not None else resolve_universe_path(cfg)
    risk["universe_table"] = str(risk.get("universe_table") or DEFAULT_REL_PATH)
    # 兼容旧键：界面若还显示 margin_table，指向同一张总表。
    risk["margin_table"] = str(risk.get("universe_table") or DEFAULT_REL_PATH)

    if create_if_missing:
        ensure_universe_table(table_path, cfg)

    cache_key = str(table_path.resolve()) if table_path.exists() else str(table_path)
    now = time.monotonic()
    mtime = table_path.stat().st_mtime if table_path.exists() else -1.0
    hit = _UNIVERSE_CACHE.get(cache_key)
    if hit and hit[0] == mtime and (now - hit[1]) < _UNIVERSE_CACHE_SEC:
        symbols, contracts = hit[2], hit[3]
        _put_universe_into_cfg(cfg, symbols, contracts)
        return symbols, contracts

    rows = read_universe_table(table_path)
    # 旧表还留着「每手保证金」列时整表重写，删掉这一列。
    upgrade_universe_drop_margin_amount_column(table_path, rows)
    # 旧表没有手续费列时，写回一遍，让 Excel 里出现新列（数字先是 0）。
    upgrade_universe_fee_columns(table_path, rows)
    upgrade_universe_close_fee_column(table_path, rows)
    # 旧表没有每跳价格列时也补上，留空表示用全局 price_tick。
    upgrade_universe_tick_column(table_path, rows)
    upgrade_universe_session_columns(table_path, rows)
    upgrade_universe_option_columns(table_path, rows)
    symbols, contracts = parse_universe_rows(rows)
    _put_universe_into_cfg(cfg, symbols, contracts)
    if table_path.exists():
        _UNIVERSE_CACHE[cache_key] = (
            table_path.stat().st_mtime,
            now,
            list(symbols),
            dict(contracts),
        )
    fp = _universe_fingerprint(table_path, symbols, contracts)
    if fp != _LAST_UNIVERSE_LOG:
        logger.info(
            "已从品种与合约表载入：关注 {} 个合约、{} 个品种比例/手续费 → {}",
            len(symbols),
            len(contracts),
            table_path,
        )
        _LAST_UNIVERSE_LOG = fp
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


def upgrade_universe_drop_margin_amount_column(
    path: Path | str, rows: list[dict[str, Any]] | None = None
) -> bool:
    """旧表还有「每手保证金」列时，按新表头整表重写，把这列删掉。"""
    file = Path(path)
    if not _universe_headers_have(file, ("每手保证金", "margin")):
        return False
    data = rows if rows is not None else read_universe_table(file)
    write_universe_table(file, data)
    logger.info("品种与合约表已删除「每手保证金」列: {}", file)
    return True


def upgrade_universe_close_fee_column(path: Path | str, rows: list[dict[str, Any]] | None = None) -> bool:
    """旧表没有「平仓手续费」列时补上，空着表示跟开仓相同。"""
    file = Path(path)
    if _universe_headers_have(file, ("平仓手续费", "commission_close")):
        return False
    data = rows if rows is not None else read_universe_table(file)
    write_universe_table(file, data)
    logger.info("品种与合约表已补上平仓手续费列: {}", file)
    return True


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


def upgrade_universe_session_columns(path: Path | str, rows: list[dict[str, Any]] | None = None) -> bool:
    """
    旧表缺「交易时段」「清仓时刻」时补上。

    空单元格按品种填出厂值：时段看品种有没有夜盘、夜盘收到几点；
    清仓时刻由时段推「收盘前 5 分钟」，不再单独维护一张钟点表。
    表正在被 Excel 打开写不进去时，只记一句日志，不让启动失败。
    """
    from src.utils.session_clock import (
        default_flatten_for_product,
        default_sessions_for_product,
        format_session_text,
        product_code,
    )

    file = Path(path)
    has_sess = _universe_headers_have(file, ("交易时段", "时段", "trading_sessions", "sessions"))
    has_flat = _universe_headers_have(file, ("清仓时刻", "清仓", "flatten_times", "flatten"))
    data = rows if rows is not None else read_universe_table(file)
    filled = False
    for row in data:
        code = product_code(row.get("symbol"))
        if not str(row.get("trading_sessions") or "").strip():
            row["trading_sessions"] = format_session_text(default_sessions_for_product(code))
            filled = True
        if not str(row.get("flatten_times") or "").strip():
            row["flatten_times"] = ",".join(default_flatten_for_product(code))
            filled = True
    if has_sess and has_flat and not filled:
        return False
    try:
        write_universe_table(file, data)
    except PermissionError:
        logger.warning(
            "品种与合约表正在被 Excel 打开，没能写入「交易时段」「清仓时刻」两列。"
            "请先关掉表格再重新打开本程序。白银/黄金即使没写进表，夜盘也会按收到 02:30 扫。"
        )
        return False
    logger.info("品种与合约表已补上交易时段/清仓时刻: {}", file)
    return True


def upgrade_universe_option_columns(path: Path | str, rows: list[dict[str, Any]] | None = None) -> bool:
    """旧表缺期权五列（类型/标的/到期日/行权价/方向）时整表重写补上，值先留空。"""
    file = Path(path)
    if all(_universe_headers_have(file, (name,)) for name in OPTION_HEADERS):
        return False
    data = rows if rows is not None else read_universe_table(file)
    try:
        write_universe_table(file, data)
    except PermissionError:
        logger.warning(
            "品种与合约表正在被 Excel 打开，没能写入期权列。请先关掉表格再重新打开本程序。"
        )
        return False
    logger.info("品种与合约表已补上期权列（合约类型/标的/到期日/行权价/期权方向）: {}", file)
    return True


def summarize_universe(symbols: list[str], contracts: dict[str, dict[str, Any]]) -> str:
    """界面摘要。"""
    sym_text = ",".join(symbols[:8])
    if len(symbols) > 8:
        sym_text += f"…共{len(symbols)}个"
    margins = []
    fees = []
    for key in sorted(contracts.keys()):
        rate = float(contracts[key].get("margin_rate_pct") or 0)
        qty = float(contracts[key].get("multiplier") or 0)
        bits = []
        if rate > 0:
            bits.append(f"{rate:g}%")
        if qty > 0:
            bits.append(f"每手{int(qty) if qty == int(qty) else qty}")
        if bits:
            margins.append(f"{key}:{','.join(bits)}")
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
    界面添加/移出关注后，把启用名单写回总表（保留未启用行和原保证金比例）。

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
                        "margin_rate_pct": parse_margin_rate_pct(box.get("margin_rate_pct")),
                        "multiplier": float(box.get("multiplier") or DEFAULT_MULTIPLIER),
                        "commission": float(box.get("commission") or 0),
                        "commission_close": _optional_named(box, "commission_close"),
                        "commission_close_today": _optional_close_today(box),
                        "price_tick": _optional_tick(box),
                        "trading_sessions": str(box.get("trading_sessions") or ""),
                        "flatten_times": str(box.get("flatten_times") or ""),
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


def invalidate_universe_cache(path: Path | str | None = None) -> None:
    """写过 Excel 之后清掉内存缓存，下次启动/读取会拿到新数字。"""
    if path is None:
        _UNIVERSE_CACHE.clear()
        return
    file = Path(path)
    _UNIVERSE_CACHE.pop(str(file), None)
    try:
        _UNIVERSE_CACHE.pop(str(file.resolve()), None)
    except OSError:
        pass


def sync_ctp_contract_to_universe(
    path: Path | str,
    fields: dict[str, Any],
) -> str:
    """
    把交易台查到的合约资料写进品种表对应行。

    只改每手数量、最小变动、保证金比例、开仓/平仓/平今手续费。
    不改启用、交易时段、清仓时刻。同品种其它月份一并改手续费和比例，避免表读不出来。
    """
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"品种与合约表不存在: {file}")
    symbol = str(fields.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("没有合约代码，不能写入品种表。")
    rows = read_universe_table(file)
    from src.domain.options import is_option

    is_opt = is_option(symbol)
    key = product_key(symbol)
    matched = False
    cost_keys = (
        "multiplier",
        "price_tick",
        "margin_rate_pct",
        "commission",
        "commission_close",
        "commission_close_today",
    )
    option_keys = ("instrument_type", "underlying", "expiry", "strike", "option_type")
    for row in rows:
        same = str(row.get("symbol") or "").strip().lower() == symbol.lower()
        # 期权每个行权价是独立合约，不能顺手改同品种的期货行。
        same_product = (not is_opt) and product_key(str(row.get("symbol") or "")) == key
        if not same and not same_product:
            continue
        matched = True
        if same and fields.get("name") and (
            not str(row.get("name") or "").strip()
            or str(row.get("name") or "").strip() == key
        ):
            row["name"] = str(fields["name"]).strip()
        for ck in cost_keys:
            if ck in fields and fields.get(ck) is not None:
                row[ck] = fields[ck]
        for ck in option_keys:
            if fields.get(ck) not in (None, ""):
                row[ck] = fields[ck]
    if not matched:
        rows.append(
            _normalize_row(
                {
                    "symbol": symbol,
                    "name": str(fields.get("name") or key or symbol),
                    "margin_rate_pct": parse_margin_rate_pct(fields.get("margin_rate_pct")),
                    "multiplier": float(fields.get("multiplier") or DEFAULT_MULTIPLIER),
                    "commission": float(fields.get("commission") or 0),
                    "commission_close": fields.get("commission_close"),
                    "commission_close_today": fields.get("commission_close_today"),
                    "price_tick": fields.get("price_tick"),
                    "instrument_type": fields.get("instrument_type") or "",
                    "underlying": fields.get("underlying") or "",
                    "expiry": fields.get("expiry") or "",
                    "strike": fields.get("strike") or 0,
                    "option_type": fields.get("option_type") or "",
                    "enabled": False,
                }
            )
        )
    _assert_product_margins_consistent(rows)
    write_universe_table(file, rows)
    invalidate_universe_cache(file)
    return symbol
