"""
统一导出工作簿
功能：全程序只用一个 Excel。优化参数、交易记录、资金变动、复盘、回测、交叉测试、优化结果各写一张表。
原理：
  写入某一张表时，先打开已有文件，只替换这一张，别的表留着。
  读的时候优先找对应表名；老的单表文件没有表名时，只要表头对得上就还能读。
  「说明」页的顺序以 SHEET_HELP 为准，和程序现在的功能一一对应。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger

# 文件放在 data 目录，方便连同复盘、回测结果一起拷走
WORKBOOK_NAME = "Auto-Trading导出.xlsx"

SHEET_INDEX = "说明"
SHEET_PARAMS = "优化参数"
SHEET_FILLS = "交易记录"
SHEET_CHANGES = "资金变动记录"
SHEET_REVIEWS = "复盘"
SHEET_BACKTEST = "回测摘要"
SHEET_BACKTEST_TRADES = "回测成交"
SHEET_CROSS = "交叉测试"
SHEET_OPTIMIZE = "优化结果"

# 给「说明」页用的人话。顺序就是 Excel 里从左到右的表。
# 交叉测试必须写在这里，否则说明页漏掉、表顺序也不稳定。
SHEET_HELP: list[tuple[str, str]] = [
    (SHEET_INDEX, "本页说明后面每一张表是什么"),
    (SHEET_PARAMS, "策略优化页导出的参数版本。名字默认是品种合约-策略名称-写入时间，可以在策略优化页改。按版本ID追加，已经有的不会重复写，改过的名字会更新"),
    (SHEET_FILLS, "当前账户分析里的交易记录，也就是开仓和平仓。来自现在这本账"),
    (SHEET_CHANGES, "当前账户分析里的资金变动记录：初始资金、入金、出金、每日结算差额、交易盈亏"),
    (SHEET_REVIEWS, "已经生成过的收盘复盘；有成绩单时带有效性/稳定性/可信度"),
    (SHEET_BACKTEST, "每次历史回测的总成绩。含策略中文名、信号/止损周期、夜盘分钟、收盘清仓，以及盈利覆盖、每笔收益、日均。第一列「回测时间」是这次跑完的时刻"),
    (SHEET_BACKTEST_TRADES, "每次历史回测里的每一笔开平仓。方向写「多/空」。第一列「回测时间」用来分清是哪一次；时间相同且内容相同的行不重复写"),
    (SHEET_CROSS, "交叉测试每一轮的品种×策略成绩。第一列「测试时间」是这一轮跑完的时刻；同一轮再导出不重复。含盈利覆盖、每笔收益、日均，以及止损周期和行情来源"),
    (SHEET_OPTIMIZE, "最近一次策略优化：最优一行，后面是得分靠前的尝试。带检验方式、检验比例、训练/检验成绩、盈利覆盖、是否通过和结论"),
]


def default_workbook_path(root: Path | str) -> Path:
    """默认导出文件：data/Auto-Trading导出.xlsx。"""
    return Path(root) / "data" / WORKBOOK_NAME


def index_frame(counts: dict[str, int] | None = None) -> pd.DataFrame:
    """说明页：表名、用途、目前多少行。"""
    counts = counts or {}
    rows = [
        {"工作表": name, "内容": text, "数据行数": int(counts.get(name, 0))}
        for name, text in SHEET_HELP
        if name != SHEET_INDEX
    ]
    return pd.DataFrame(rows, columns=["工作表", "内容", "数据行数"])


# 只有这些列缺失时，仍可接旧行（再补时间），不算表头大改。
_SCHEMA_STAMP_ONLY = frozenset({"回测时间", "测试时间"})


def schema_matches(frame: pd.DataFrame | None, columns: list[str]) -> bool:
    """
    判断已有表是不是当前程序要的表头。

    缺了「盈利覆盖」这类新指标列 → 旧表，丢掉旧行按新表写。
    只缺「回测时间 / 测试时间」→ 仍可接上，并在追加时补时间。
    """
    if frame is None or frame.empty:
        return True
    have = {str(col) for col in frame.columns}
    missing = [col for col in columns if col not in have]
    if not missing:
        return True
    return all(col in _SCHEMA_STAMP_ONLY for col in missing)


def append_frames(
    existing: pd.DataFrame | None,
    fresh: pd.DataFrame,
    columns: list[str],
    stamp_column: str | None = None,
) -> tuple[pd.DataFrame, int]:
    """
    把新行接到已有表的后面。

    返回 (合并后的表, 这次真正新增的行数)。
    原理：按整行内容去重。已经在表里的记录留着，完全一样的新行不再写第二次。
    「还没有数据」这种占位说明，一旦有了真实记录就去掉。
    stamp_column 有值时：旧行这一列是空的、其余列又和新行一样，就把新的时间补进旧行，避免同一笔被写两次。
    旧行已经写着另一个时间，就保留，新的一次另外追加。这样能分清前后两次回测。
    表头和程序对不上时，丢掉旧行，只写新表，避免缺列的旧记录混进来。
    """
    fresh_clean = _align_columns(fresh, columns)
    fresh_clean = fresh_clean.loc[~fresh_clean.apply(_is_placeholder_row, axis=1)].reset_index(drop=True)
    if not schema_matches(existing, columns):
        logger.info("导出表头已升级，旧行不再保留，按当前列重写")
        return fresh_clean.reset_index(drop=True), int(len(fresh_clean))
    old_clean = _align_columns(existing, columns)
    old_clean = old_clean.loc[~old_clean.apply(_is_placeholder_row, axis=1)].reset_index(drop=True)
    if stamp_column and stamp_column in columns and not old_clean.empty and not fresh_clean.empty:
        old_clean = _copy_stamp_onto_blank_rows(old_clean, fresh_clean, columns, stamp_column)

    if old_clean.empty:
        return fresh_clean.reset_index(drop=True), int(len(fresh_clean))
    if fresh_clean.empty:
        return old_clean.reset_index(drop=True), 0

    seen = {_row_key(row, columns) for _, row in old_clean.iterrows()}
    new_rows: list[dict[str, str]] = []
    for _, row in fresh_clean.iterrows():
        key = _row_key(row, columns)
        if key in seen:
            continue
        seen.add(key)
        new_rows.append({col: row[col] for col in columns})
    if not new_rows:
        return old_clean.reset_index(drop=True), 0
    merged = pd.concat([old_clean, pd.DataFrame(new_rows, columns=columns)], ignore_index=True)
    return merged, len(new_rows)


def _copy_stamp_onto_blank_rows(
    old: pd.DataFrame,
    fresh: pd.DataFrame,
    columns: list[str],
    stamp_column: str,
) -> pd.DataFrame:
    """
    给以前导出、还没有回测时间的行补上时间。

    只补「除了时间列，其他列都相同」的旧行。
    旧行如果已经有另一个回测时间，说明那是更早的一次，这里不动它。
    """
    others = [col for col in columns if col != stamp_column]
    stamped = old.copy()
    blank_at: dict[tuple[str, ...], list[int]] = {}
    for idx, row in stamped.iterrows():
        if _cell_text(row[stamp_column]) != "":
            continue
        blank_at.setdefault(_row_key(row, others), []).append(int(idx))
    for _, row in fresh.iterrows():
        # 用去掉秒后的文本判断有没有时间，写回单元格时保留原来的「年月日 时分秒」。
        if _cell_text(row[stamp_column]) == "":
            continue
        for idx in blank_at.get(_row_key(row, others), []):
            stamped.at[idx, stamp_column] = row[stamp_column]
    return stamped


def _align_columns(frame: pd.DataFrame | None, columns: list[str]) -> pd.DataFrame:
    """缺的列补空，多出来的列丢掉，保证新旧表头能对齐。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    out = frame.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = ""
    return out[columns]


def _is_placeholder_row(row: pd.Series) -> bool:
    """没有成交时写进第一格的说明文字，不算一条真实记录。"""
    if len(row) == 0:
        return True
    text = _cell_text(row.iloc[0])
    return text.startswith("还没有") or text.startswith("这次回测没有")


def _row_key(row: pd.Series, columns: list[str]) -> tuple[str, ...]:
    """同一笔的判断依据：每一列都相同才算重复。"""
    return tuple(_cell_text(row[col]) for col in columns)


def _cell_text(value: object) -> str:
    """把 Excel 读回来的 200.0 和刚导出的 200 当成同一个数，避免误判成新行。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, pd.Timestamp):
        text = value.strftime("%Y-%m-%d %H:%M:%S")
    else:
        text = str(value).strip()
    if text.lower() == "nan":
        return ""
    # Excel 读回时间会带上秒，和导出时的「2026-09-18 10:15」当成同一时刻。
    if len(text) == 19 and text[13] == ":" and text.endswith(":00") and text[4] == "-" and text[10] == " ":
        text = text[:-3]
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    # 0.80 和 Excel 读回的 0.8 是同一个数，不能当成新的一行。
    return str(number)


def write_sheet(path: Path | str, sheet: str, frame: pd.DataFrame) -> Path:
    """
    把一张表写进工作簿。

    文件不存在就新建，并先放一张「说明」。
    文件已存在就只替换同名表，其他功能的表不动。
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            index_frame().to_excel(writer, sheet_name=SHEET_INDEX, index=False)
            frame.to_excel(writer, sheet_name=sheet, index=False)
        logger.info("新建导出工作簿 {}，写入表 {}", out, sheet)
        return out
    with pd.ExcelWriter(out, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        frame.to_excel(writer, sheet_name=sheet, index=False)
    logger.info("已更新工作簿 {} 的表 {}", out, sheet)
    return out


def write_book(path: Path | str, sheets: dict[str, pd.DataFrame]) -> Path:
    """
    一次写成完整工作簿。
    用于「导出全部」：按固定顺序重写，避免旧表名残留。
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ordered = [name for name, _text in SHEET_HELP if name in sheets]
    for name in sheets:
        if name not in ordered:
            ordered.append(name)
    counts = {name: int(len(sheets[name])) for name in ordered if name != SHEET_INDEX}
    sheets = dict(sheets)
    sheets[SHEET_INDEX] = index_frame(counts)
    ordered = [SHEET_INDEX] + [name for name in ordered if name != SHEET_INDEX]
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        for name in ordered:
            sheets[name].to_excel(writer, sheet_name=name, index=False)
    logger.info("已写出完整导出工作簿 {}，共 {} 张表", out, len(ordered))
    return out


def read_sheet(path: Path | str, sheet: str, required_column: str | None = None) -> pd.DataFrame:
    """
    读取指定工作表。

    没有这张表时，如果第一张表的表头里有 required_column，就把它当作旧版单表文件。
    这样以前单独导出的优化参数、账户成交仍能导入。
    """
    file = Path(path)
    if not file.exists():
        return pd.DataFrame()
    book = pd.ExcelFile(file, engine="openpyxl")
    if sheet in book.sheet_names:
        return pd.read_excel(book, sheet_name=sheet)
    if not book.sheet_names:
        return pd.DataFrame()
    first = pd.read_excel(book, sheet_name=book.sheet_names[0])
    if required_column and required_column in first.columns:
        logger.info("Excel 没有表「{}」，改读第一张表（旧版单表文件）", sheet)
        return first
    return pd.DataFrame()
