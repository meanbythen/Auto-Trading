"""
策略优化参数表（Excel）

功能：把优化网格的档位、参数中文名、取值中文说明放到 config/优化参数.xlsx，
      用户不装 Python / IDE 也能直接改；程序读取表里的值再交给策略优化引擎。
原理：
  - 表不存在时，按代码里的内置默认生成一张，保证首次运行和打包后都能用。
  - 「参数网格」一张表管：哪个策略、哪个深度、搜哪些参数、每个参数试哪几档。
  - 「参数名称」按策略列出用到的参数中文名，可在「策略名称」列筛选；「取值说明」写开关取值的中文。
  - 同一策略 + 深度没有任何行时，回退到内置默认，避免误删整段后静默不搜参数。
  - 改完 Excel 保存即可，下次点「开始策略优化」自动重新读表（按文件修改时间缓存）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_REL_PATH = "config/优化参数.xlsx"

SHEET_GRID = "参数网格"
SHEET_LABELS = "参数名称"
SHEET_VALUES = "取值说明"
SHEET_NOTES = "使用说明"

GRID_HEADERS = ("策略引擎", "策略名称", "搜索深度", "参数键", "参数名称", "取值", "启用", "说明")
LABEL_HEADERS = ("策略名称", "参数键", "参数名称")
VALUE_HEADERS = ("参数键", "取值", "取值说明")

DEPTHS = ("精简", "标准", "完整")

# 写进 Excel / 从 Excel 读回时用的策略引擎键。界面里的旧名字会先换成这里的键。
ENGINE_NAMES: dict[str, str] = {
    "daily_body": "日内实体（主推）",
    "float_hold": "日内浮盈飘单",
    "trend_follow": "趋势跟随",
    "supertrend": "SuperTrend趋势",
    "dual_ma": "双均线趋势",
    "ema_swing": "均线波段",
    "bollinger": "布林带突破",
    "sar_trend": "SAR波段趋势",
    "multi_factor": "多因子CTA（加权打分）",
    "swing_trend": "长线趋势加减仓",
    "swing_box": "长线箱体突破",
}

_ENGINE_ALIASES = {
    "st": "supertrend",
    "ma_cross": "dual_ma",
    "boll": "bollinger",
    "bollinger_break": "bollinger",
    "sar": "sar_trend",
    "parabolic_sar": "sar_trend",
    "cta": "multi_factor",
    "macd_swing": "ema_swing",
}

_DEPTH_ALIASES = {
    "light": "精简",
    "精简": "精简",
    "standard": "标准",
    "标准": "标准",
    "full": "完整",
    "完整": "完整",
}

# 这些参数的取值是文字：即使 Excel 里写成 5、15，也要当字符串用。
_TEXT_PARAMS = {
    "timeframe",
    "day_cutoff",
    "night_cutoff",
    "stop_mode",
    "session_scope",
    "generic_stop.kind",
    "macd_timeframe",
    "macd_combo",
    "dir_ema_pair",
    "sig_ema_triple",
}

_TRUE_WORDS = {"true", "yes", "y", "1", "是", "启用", "on"}
_FALSE_WORDS = {"false", "no", "n", "0", "否", "停用", "off"}
# 开关型参数：0/1 也按 true/false 理解。
_BOOL_PARAMS = {"reverse_on_cross", "reverse_on_flip", "flatten_at_close", "trend_align_enabled"}
_SPLIT_RE = re.compile(r"[，,、;；]+")


@dataclass
class ParamTable:
    """一张读进来的优化参数表。空表表示没读到，调用方应回退内置默认。"""

    grids: dict[str, dict[str, dict[str, list[Any]]]] = field(default_factory=dict)
    # 表里出现过的「策略 + 深度」。出现过但参数全被停用时，应当返回空网格而不是回退默认。
    pairs: set[tuple[str, str]] = field(default_factory=set)
    labels: dict[str, str] = field(default_factory=dict)
    value_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    path: Path | None = None

    def grid(self, engine: str, depth: str) -> dict[str, list[Any]] | None:
        """取某个策略/深度的网格；表里没有这一段时返回 None。"""
        key = (canonical_engine(engine), normalize_depth(depth))
        if key not in self.pairs:
            return None
        return self.grids.get(key[0], {}).get(key[1], {})


_CACHE: dict[str, tuple[tuple[int, int], ParamTable]] = {}


def default_table_path(root: str | Path | None = None) -> Path:
    """默认路径：项目根目录 / config/优化参数.xlsx。"""
    from src.utils.paths import project_root

    base = Path(root) if root else project_root()
    return base / DEFAULT_REL_PATH


def resolve_param_table_path(cfg: dict[str, Any] | None = None, root: str | Path | None = None) -> Path:
    """按配置 backtest.optimizer_table 算路径，没写就用默认文件名。"""
    from src.utils.paths import project_root

    base = Path(root) if root else Path(str((cfg or {}).get("_project_root") or project_root()))
    raw = str(((cfg or {}).get("backtest") or {}).get("optimizer_table") or DEFAULT_REL_PATH).strip()
    path = Path(raw or DEFAULT_REL_PATH)
    return path if path.is_absolute() else base / path


def canonical_engine(name: object) -> str:
    """策略名统一成表里的键。未知名字原样返回，交给上层当「没有网格」。"""
    text = str(name or "").strip()
    if not text:
        return ""
    low = text.lower()
    return _ENGINE_ALIASES.get(low, low)


def normalize_depth(depth: object) -> str:
    """搜索深度统一成 精简 / 标准 / 完整。"""
    text = str(depth or "标准").strip()
    return _DEPTH_ALIASES.get(text.lower(), "标准")


def _cell(row: tuple[Any, ...], index: int | None) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def _find_col(names: list[str], aliases: tuple[str, ...]) -> int | None:
    lowered = [n.lower() for n in names]
    for alias in aliases:
        key = alias.lower()
        if key in lowered:
            return lowered.index(key)
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _as_enabled(value: Any) -> bool:
    text = _text(value).lower()
    if not text:
        return True
    return text not in _FALSE_WORDS


def _typed_token(token: str, key: str) -> Any:
    """把单元格里的一个取值转成程序用的类型。"""
    text = token.strip()
    low = text.lower()
    if key not in _TEXT_PARAMS:
        if low in _TRUE_WORDS:
            return True
        if low in _FALSE_WORDS and (key in _BOOL_PARAMS or not text.lstrip("-").replace(".", "", 1).isdigit()):
            return False
    if key in _TEXT_PARAMS:
        return text
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    try:
        return float(text)
    except ValueError:
        return text


def parse_values(raw: Any, key: str) -> list[Any]:
    """
    解析一档参数的取值。

    支持两种写法：
      - 逗号分隔：15,20,30 / touch,close / true,false
      - JSON 数组：[15, 20, 30]
    """
    if raw is None:
        return []
    if isinstance(raw, bool):
        return [raw]
    if isinstance(raw, (int, float)):
        return [int(raw) if float(raw).is_integer() else float(raw)]
    if isinstance(raw, (time, datetime)):
        return [raw.strftime("%H:%M")]
    text = _text(raw)
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            return data
    tokens = [t for t in _SPLIT_RE.split(text) if t.strip()]
    return [_typed_token(t, key) for t in tokens]


def read_param_table(path: str | Path) -> ParamTable:
    """读 Excel。读不出或缺表头时返回空表，让调用方回退内置默认。"""
    file = Path(path)
    if not file.exists():
        return ParamTable(path=file)
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        logger.warning("缺少 openpyxl，无法读取优化参数表: {}", file)
        return ParamTable(path=file)

    table = ParamTable(path=file)
    book = None
    try:
        book = openpyxl.load_workbook(file, data_only=True, read_only=True)
        if SHEET_GRID in book.sheetnames:
            _read_grid_sheet(book[SHEET_GRID], table)
        else:
            logger.warning("优化参数表里没有「{}」工作表，回退内置默认: {}", SHEET_GRID, file)
        if SHEET_LABELS in book.sheetnames:
            _read_labels_sheet(book[SHEET_LABELS], table)
        if SHEET_VALUES in book.sheetnames:
            _read_values_sheet(book[SHEET_VALUES], table)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取优化参数表失败，回退内置默认: {} ({})", file, exc)
        return ParamTable(path=file)
    finally:
        if book is not None:
            book.close()
    return table


def _read_grid_sheet(sheet: Any, table: ParamTable) -> None:
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        return
    names = [_text(c) for c in header]
    idx_engine = _find_col(names, ("策略引擎", "策略", "engine"))
    idx_depth = _find_col(names, ("搜索深度", "深度", "depth"))
    idx_key = _find_col(names, ("参数键", "参数", "key"))
    idx_values = _find_col(names, ("取值", "取值范围", "values"))
    idx_on = _find_col(names, ("启用", "enabled"))
    if idx_engine is None or idx_depth is None or idx_key is None or idx_values is None:
        logger.warning("优化参数表「{}」缺必要表头，回退内置默认", SHEET_GRID)
        return

    from src.backtest.optimizer import PARAM_LABELS_CN

    known_keys = set(PARAM_LABELS_CN)

    for raw in rows:
        if not raw:
            continue
        engine = canonical_engine(_cell(raw, idx_engine))
        depth = normalize_depth(_cell(raw, idx_depth))
        key = _text(_cell(raw, idx_key))
        if not engine or not key or engine not in ENGINE_NAMES:
            continue
        if key not in known_keys and not key.startswith("generic_stop."):
            logger.warning("优化参数表里跳过不认识的参数键: {}（策略 {}）", key, engine)
            continue
        values = parse_values(_cell(raw, idx_values), key)
        if not values:
            logger.warning("优化参数表里 {} / {} / {} 没有取值，跳过", engine, depth, key)
            continue
        table.pairs.add((engine, depth))
        if not _as_enabled(_cell(raw, idx_on)):
            continue
        table.grids.setdefault(engine, {}).setdefault(depth, {})[key] = values


def _read_labels_sheet(sheet: Any, table: ParamTable) -> None:
    """读「参数名称」。新表是 策略名称 / 参数键 / 参数名称 三列，也兼容旧的两列。"""
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        return
    names = [_text(c) for c in header]
    idx_key = _find_col(names, ("参数键", "参数", "key"))
    idx_label = _find_col(names, ("参数名称", "中文名", "label"))
    if idx_key is None or idx_label is None:
        # 认不出表头就按旧格式当两列处理，避免整表失效。
        idx_key, idx_label = 0, 1
        rows = sheet.iter_rows(values_only=True)
        next(rows, None)
    for raw in rows:
        key = _text(_cell(raw, idx_key))
        label = _text(_cell(raw, idx_label))
        if key and label and key != "参数键":
            table.labels[key] = label


def _read_values_sheet(sheet: Any, table: ParamTable) -> None:
    for raw in sheet.iter_rows(values_only=True):
        if not raw or len(raw) < 3:
            continue
        key = _text(raw[0])
        value = _text(raw[1])
        label = _text(raw[2])
        if not key or not value or key == "参数键":
            continue
        table.value_labels.setdefault(key, {})[value.lower()] = label


def load_param_table(
    path: str | Path | None = None,
    *,
    create_if_missing: bool = True,
) -> ParamTable:
    """
    读优化参数表，按「路径 + 修改时间 + 文件大小」缓存。

    表不存在时按内置默认生成一张（首次运行 / 用户误删都能自愈）。
    """
    file = Path(path) if path else default_table_path()
    if create_if_missing and not file.exists():
        try:
            write_default_param_table(file)
            logger.info("已生成优化参数表: {}", file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("生成优化参数表失败，先用内置默认: {} ({})", file, exc)
            return ParamTable(path=file)
    if not file.exists():
        return ParamTable(path=file)

    try:
        stat = file.stat()
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return ParamTable(path=file)

    cache_key = str(file.resolve())
    cached = _CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return cached[1]
    table = read_param_table(file)
    _CACHE[cache_key] = (signature, table)
    return table


def clear_param_table_cache() -> None:
    """测试或界面「重新读取」时手动清缓存。"""
    _CACHE.clear()


def _label_rows(labels: dict[str, str]) -> list[list[str]]:
    """按策略拆参数名称：一个策略用到的每个参数一行，方便在「策略名称」列筛选。"""
    from src.backtest.optimizer import default_grid_for_engine

    rows: list[list[str]] = []
    used: set[str] = set()
    for engine, engine_name in ENGINE_NAMES.items():
        keys: list[str] = []
        for depth in DEPTHS:
            keys.extend(default_grid_for_engine(engine, depth).keys())
        for key in dict.fromkeys(keys):
            used.add(key)
            rows.append([engine_name, key, labels.get(key, key)])
    # 内置有、但暂时没有策略用到的键也保留，避免以后加参数时找不到中文名。
    for key, label in labels.items():
        if key not in used:
            rows.append(["（未使用）", key, label])
    return rows


def _values_text(values: list[Any]) -> str:
    parts = []
    for value in values:
        if isinstance(value, bool):
            parts.append("true" if value else "false")
        else:
            parts.append(str(value))
    return ",".join(parts)


def build_default_table(path: str | Path) -> Path:
    """按代码内置默认整表重写一次（会覆盖原文件的自定义内容）。"""
    from src.backtest.optimizer import PARAM_LABELS_CN, PARAM_VALUE_LABELS_CN, default_grid_for_engine

    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 openpyxl 才能写优化参数表。请执行: pip install openpyxl") from exc

    book = openpyxl.Workbook()
    grid_sheet = book.active
    grid_sheet.title = SHEET_GRID
    grid_sheet.append(list(GRID_HEADERS))
    for cell in grid_sheet[1]:
        cell.font = Font(bold=True)
    for engine, engine_name in ENGINE_NAMES.items():
        for depth in DEPTHS:
            for key, values in default_grid_for_engine(engine, depth).items():
                grid_sheet.append(
                    [
                        engine,
                        engine_name,
                        depth,
                        key,
                        PARAM_LABELS_CN.get(key, key),
                        _values_text(values),
                        "是",
                        "",
                    ]
                )
    for col, width in zip("ABCDEFGH", (16, 20, 10, 22, 24, 34, 8, 28)):
        grid_sheet.column_dimensions[col].width = width
    grid_sheet.freeze_panes = "A2"
    grid_sheet.auto_filter.ref = f"A1:H{max(1, grid_sheet.max_row)}"

    label_sheet = book.create_sheet(SHEET_LABELS)
    label_sheet.append(list(LABEL_HEADERS))
    for cell in label_sheet[1]:
        cell.font = Font(bold=True)
    for row in _label_rows(PARAM_LABELS_CN):
        label_sheet.append(row)
    label_sheet.column_dimensions["A"].width = 24
    label_sheet.column_dimensions["B"].width = 24
    label_sheet.column_dimensions["C"].width = 30
    label_sheet.freeze_panes = "A2"
    label_sheet.auto_filter.ref = f"A1:C{max(1, label_sheet.max_row)}"

    value_sheet = book.create_sheet(SHEET_VALUES)
    value_sheet.append(list(VALUE_HEADERS))
    for cell in value_sheet[1]:
        cell.font = Font(bold=True)
    for key, mapping in PARAM_VALUE_LABELS_CN.items():
        for value, label in mapping.items():
            value_sheet.append([key, value, label])
    value_sheet.column_dimensions["A"].width = 24
    value_sheet.column_dimensions["B"].width = 16
    value_sheet.column_dimensions["C"].width = 20
    value_sheet.freeze_panes = "A2"

    notes = book.create_sheet(SHEET_NOTES)
    notes.append(["项目", "说明"])
    for cell in notes[1]:
        cell.font = Font(bold=True)
    for item, text in _notes_rows():
        notes.append([item, text])
    notes.column_dimensions["A"].width = 16
    notes.column_dimensions["B"].width = 110
    for row in notes.iter_rows(min_row=2, min_col=2, max_col=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="center")

    book.save(file)
    return file


def _notes_rows() -> list[tuple[str, str]]:
    return [
        ("这张表做什么", "改策略优化（网格搜索）试哪些参数、每档试哪几个值。程序每次开始优化前读一次，保存即生效，不用重启。"),
        ("不要改什么", "不要改四张工作表的名字，也不要删掉第一行表头；认不出表头时程序会整表回退内置默认。"),
        ("打包后的位置", "改程序旁的 config/优化参数.xlsx。_internal 里也有一份，那份只是首次启动的种子；要改就改外面这一份。"),
        ("参数网格 · 策略引擎", "用表里的键：daily_body / float_hold / trend_follow / supertrend / dual_ma / ema_swing / bollinger / sar_trend / multi_factor / swing_trend / swing_box。"),
        ("参数网格 · 搜索深度", "精简 / 标准 / 完整。三档各写各的行；界面选哪一档就只读那一档。"),
        ("参数网格 · 参数键", "程序认识的键，见「参数名称」页。不要自己发明；认不出的行会被忽略。"),
        ("参数网格 · 取值", "逗号分隔即可：15,20,30；文字参数直接写：touch,close；开关写 true,false。也支持 JSON 数组 [15, 20, 30]。均线波段的方向选择器可写 21/122、60/120 这类任意两根；信号发生器写 5/10/30。"),
        ("参数网格 · 启用", "填「否」可临时关掉一个旋钮，同策略同深度其余参数照常搜；整段都填「否」等于这一档不搜参数。"),
        ("参数网格 · 说明", "最后一列随便写备注，程序不读，方便自己记为什么这样填。"),
        ("缺行怎么办", "某个策略 + 深度一行都没有时，程序回退到内置默认，不会因为误删整段而静默不搜参数。"),
        ("重复行", "同一策略、同一深度、同一参数键写了多行时，以最后一行为准。"),
        ("参数名称", "一个策略用到的每个参数一行，可按「策略名称」列筛选。只影响结果、写入确认、参数历史和导出 Excel 里显示的中文名，不影响实际搜什么。同一参数键在不同策略下请保持名称一致。"),
        ("取值说明", "开关型参数的取值中文，例如 touch=触及即止损、sar=SAR 移动止损。"),
        ("恢复默认", "删掉 config/优化参数.xlsx，下次开始优化（或点「打开优化参数表」）会自动按内置默认重建；自定义内容会一起丢掉。"),
        ("改错了怎么办", "先关掉 Excel 再点开始优化。文件被 Excel 占用时读不到，程序会回退内置默认，日志里会写明。"),
        ("生效时间", "保存 Excel 后，下一次点「开始策略优化」就用新表；已经开跑的这次不受影响。"),
    ]


def write_default_param_table(path: str | Path) -> Path:
    """表不存在时写一张内置默认表；已存在则不动。"""
    file = Path(path)
    if file.exists():
        return file
    return build_default_table(file)


def ensure_param_table(path: str | Path) -> Path:
    """确保表存在（界面「打开优化参数表」用）。"""
    return write_default_param_table(path)
