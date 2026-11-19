"""
把各功能已有的结果收进同一个 Excel
功能：点一次「导出全部」，优化参数、交易记录、资金变动、复盘、回测、交叉测试、优化结果各占一张表。
原理：程序里原来的 json / 复盘 txt 仍是原始记录；这本 Excel 是给人打开看的汇总，不替代记账。
      表头跟当前界面字段对齐。结构大改时可调用 rebuild_workbook，丢掉旧 Excel 行，按程序现有数据重写。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.account.signal_book import change_kind_name, load_book
from src.backtest.param_excel import COLUMNS as PARAM_COLUMNS
from src.backtest.param_excel import collect_export_versions, merged_param_frame, version_to_row
from src.backtest.param_history import strategy_display_name
from src.export.book import (
    SHEET_BACKTEST,
    SHEET_BACKTEST_SIGNALS,
    SHEET_BACKTEST_TRADES,
    SHEET_CHANGES,
    SHEET_CROSS,
    SHEET_FILLS,
    SHEET_OPTIMIZE,
    SHEET_PARAMS,
    SHEET_REVIEWS,
    append_frames,
    default_workbook_path,
    read_sheet,
    write_book,
    write_sheet,
)

# 交易记录表的列。和账户分析里每一笔开平仓对得上。
FILL_COLUMNS = ["时间", "品种", "方向", "开平", "价格", "手数", "合约乘数", "原因"]
# 资金变动记录表的列。五种变动方式都写中文，并带上记完这一笔之后的余额。
CHANGE_COLUMNS = ["时间", "方式", "金额", "备注", "变动后余额"]

# 回测摘要：和历史回测页、params_snapshot 现在能看到的字段对齐。
BACKTEST_SUMMARY_COLUMNS = [
    "回测时间",
    "品种",
    "策略",
    "信号周期",
    "止损周期",
    "夜盘分钟",
    "收盘清仓",
    "K线根数",
    "成交笔数",
    "胜率",
    "平均盈利",
    "平均亏损",
    "盈亏比",
    "收益期望",
    "净盈亏",
    "盈利因子",
    "最大回撤",
    "盈利覆盖",
    "每笔收益",
    "开始时间",
    "结束时间",
    "天数",
    "日均",
]
# 回测成交：方向统一成「多/空」，方便在 Excel 里筛选。
BACKTEST_TRADE_COLUMNS = [
    "回测时间",
    "品种",
    "策略",
    "方向",
    "开仓时间",
    "平仓时间",
    "开仓价",
    "平仓价",
    "手数",
    "盈亏",
    "开仓原因",
    "平仓原因",
]
# 回测信号：出现了开仓信号、但被策略过滤或风控挡回去的。回测页结果区也列这些。
BACKTEST_SIGNAL_COLUMNS = [
    "回测时间",
    "品种",
    "信号时间",
    "方向",
    "信号价",
    "止损价",
    "拦截层",
    "原因",
]

# 和交叉测试页上的结果表同一套列，前面加测试时间，后面记下这一轮用的手数、开仓限制和止损周期。
CROSS_COLUMNS = [
    "测试时间",
    "品种",
    "策略",
    "胜率",
    "盈利因子",
    "盈亏比",
    "收益期望",
    "最大回撤",
    "卡尔玛",
    "最大连亏",
    "净盈亏",
    "盈利覆盖",
    "收益率",
    "交易频率",
    "笔数",
    "每笔收益",
    "实际测试时段",
    "开始时间",
    "结束时间",
    "天数",
    "日均",
    "K线根数",
    "得分",
    "评估",
    "未通过项",
    "流动性",
    "结果",
    "持仓手数",
    "每笔风险%",
    "开仓最小间隔秒",
    "每天最多开仓",
    "信号周期",
    "止损周期",
    "行情来源",
    "开始日期",
    "结束日期",
]

# 优化结果：最优 + 靠前尝试；检验相关字段跟策略优化页结论对齐。
# 盈利覆盖等和交叉测试同一算法，方便对照各组参数。
# 第一列「测试时间」是这次优化跑完的时刻，同一次的行时间相同，用来分清是哪一次。
OPTIMIZE_COLUMNS = [
    "测试时间",
    "名次",
    "得分",
    "评估",
    "未通过项",
    "成交笔数",
    "胜率",
    "净盈亏",
    "盈利因子",
    "最大回撤",
    "盈利覆盖",
    "每笔收益",
    "开始时间",
    "结束时间",
    "天数",
    "日均",
    "参数JSON",
    "参数说明",
    "品种",
    "策略",
    "区间",
    "检验方式",
    "检验比例%",
    "深度",
    "训练成交",
    "训练净盈亏",
    "训练盈利覆盖",
    "检验成交",
    "检验净盈亏",
    "检验盈利覆盖",
    "检验效率",
    "检验段赚钱比例",
    "是否通过",
    "结论",
]


def export_backtest(root: Path | str, excel_path: Path | str | None = None) -> tuple[Path, int, int, int, int]:
    """
    把最近一次回测追加进统一 Excel 的两张表。

    「回测摘要」每次多一行总成绩，「回测成交」把这一次的每一笔接在已有记录后面。
    两张表的第一列都是「回测时间」，同一次回测共用这个时间，用来分清前后几次导出。
    表里已经有的行留着。回测时间和其他列都相同的行不重复写。
    以前导出、还没有回测时间、但内容和这次完全一样的行，会补上这次的时间，不另起一行。
    「回测信号」也按同样口径追加，把没做成的信号和原因留在文件里。
    只改这两张回测表和信号表，优化参数、账户成交等其他表保持原样。

    返回 (文件路径, 摘要新增行数, 成交新增行数, 摘要总行数, 成交总行数)。
    还没跑过回测时抛出 ValueError，避免把「还没有」写进正式文件。
    """
    root_path = Path(root)
    path = Path(excel_path) if excel_path else default_workbook_path(root_path)
    payload = _read_json(root_path / "data" / "backtest_last.json")
    if not payload:
        raise ValueError("还没有回测结果。请先点「开始回测」，完成后再导出。")
    summary, trades = _backtest_frames(root_path)
    summary_all, summary_added = _append_backtest_sheet(path, SHEET_BACKTEST, summary, list(summary.columns))
    trades_all, trades_added = _append_backtest_sheet(path, SHEET_BACKTEST_TRADES, trades, list(trades.columns))
    signals = _backtest_signal_frame(root_path)
    signals_all, _signals_added = _append_backtest_sheet(
        path, SHEET_BACKTEST_SIGNALS, signals, list(signals.columns)
    )
    # 原文件被 Excel 占用时，第一次会改写到带时间的另一份；后面两张必须跟过去。
    path = write_sheet(path, SHEET_BACKTEST, summary_all)
    path = write_sheet(path, SHEET_BACKTEST_TRADES, trades_all)
    path = write_sheet(path, SHEET_BACKTEST_SIGNALS, signals_all)
    try:
        from src.export.cta_decay import write_decay_from_backtest

        write_decay_from_backtest(root_path, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("多因子衰减表没有写上: {}", exc)
    return path, summary_added, trades_added, int(len(summary_all)), int(len(trades_all))


def export_all(root: Path | str, excel_path: Path | str | None = None) -> tuple[Path, dict[str, int]]:
    """
    汇总写入一个工作簿。

    返回 (文件路径, 每张表的数据行数)。
    优化参数会保留这本 Excel 里已经追加过、但程序内已删掉的旧版本。
    回测摘要/成交、交叉测试、优化结果也会把本文件里以前追加的行接上。
    若回测摘要、交叉测试或优化结果还是旧表头（缺盈利覆盖等列），整本按新表重建，不再硬接旧列。
    """
    root_path = Path(root)
    path = Path(excel_path) if excel_path else default_workbook_path(root_path)
    if _export_schema_outdated(path):
        logger.info("检测到旧导出表头，按当前程序结构整本重建 {}", path)
        return rebuild_workbook(root_path, path)
    params = merged_param_frame(root_path, path)
    fills, changes = _account_frames(root_path)
    reviews = _reviews_frame(root_path)
    summary, trades = _appended_backtest_frames(root_path, path)
    signals = _appended_backtest_signals(root_path, path)
    cross = _appended_cross_frame(root_path, path)
    optimize = _appended_optimize_frame(root_path, path)
    sheets = {
        SHEET_PARAMS: params,
        SHEET_FILLS: fills,
        SHEET_CHANGES: changes,
        SHEET_REVIEWS: reviews,
        SHEET_BACKTEST: summary,
        SHEET_BACKTEST_TRADES: trades,
        SHEET_BACKTEST_SIGNALS: signals,
        SHEET_CROSS: cross,
        SHEET_OPTIMIZE: optimize,
    }
    written = write_book(path, sheets)
    return written, {name: int(len(frame)) for name, frame in sheets.items()}


def _export_schema_outdated(path: Path) -> bool:
    """回测摘要 / 交叉测试 / 优化结果缺了新列时，判定整本需要重建。"""
    from src.export.book import read_sheet, schema_matches

    if not path.exists():
        return False
    checks = {
        SHEET_BACKTEST: BACKTEST_SUMMARY_COLUMNS,
        SHEET_CROSS: CROSS_COLUMNS,
        SHEET_OPTIMIZE: OPTIMIZE_COLUMNS,
    }
    for sheet, columns in checks.items():
        try:
            frame = read_sheet(path, sheet)
        except Exception:  # noqa: BLE001
            return True
        if frame is None or frame.empty:
            continue
        if not schema_matches(frame, columns):
            return True
    return False


def rebuild_workbook(root: Path | str, excel_path: Path | str | None = None) -> tuple[Path, dict[str, int]]:
    """
    丢掉旧 Excel，按当前程序数据整本重建。

    用途：表头或工作表结构和程序对不上时，不再试图兼容旧行。
    优化参数只取程序里还在的版本；回测/交叉测试只取最近一次结果文件。
    账户分析、复盘仍按现在磁盘上的账本和 txt 写入。
    若旧文件正被 Excel 打开删不掉，会改写到同目录「Auto-Trading导出_重建.xlsx」，避免卡死。
    """
    root_path = Path(root)
    path = Path(excel_path) if excel_path else default_workbook_path(root_path)
    if path.exists():
        try:
            path.unlink()
            logger.info("已删除旧导出文件，准备按当前结构重建 {}", path)
        except OSError as exc:
            # Windows 上文件被 Excel 占用时 unlink 会失败；改写到旁路文件，旧文件留给用户关掉后自己删。
            alt = path.with_name(f"{path.stem}_重建{path.suffix}")
            logger.warning("旧导出文件正被占用，无法删除 {}（{}）。将写到 {}", path, exc, alt)
            path = alt
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
    # 不读旧 Excel：参数只来自程序历史；回测/交叉只来自最近一次 json。
    exported_at = datetime.now().isoformat(timespec="seconds")
    param_rows = [
        version_to_row(v, exported_at=exported_at)
        for v in collect_export_versions(root_path, include_factory=True)
    ]
    params = (
        pd.DataFrame(param_rows, columns=PARAM_COLUMNS)
        if param_rows
        else _empty(list(PARAM_COLUMNS), "还没有参数版本。到「策略优化」写入一组后再导出。")
    )
    fills, changes = _account_frames(root_path)
    reviews = _reviews_frame(root_path)
    summary, trades = _backtest_frames(root_path)
    signals = _backtest_signal_frame(root_path)
    cross = _cross_frame(root_path)
    optimize = _optimize_frame(root_path)
    sheets = {
        SHEET_PARAMS: params,
        SHEET_FILLS: fills,
        SHEET_CHANGES: changes,
        SHEET_REVIEWS: reviews,
        SHEET_BACKTEST: summary,
        SHEET_BACKTEST_TRADES: trades,
        SHEET_BACKTEST_SIGNALS: signals,
        SHEET_CROSS: cross,
        SHEET_OPTIMIZE: optimize,
    }
    written = write_book(path, sheets)
    logger.info("已按当前程序结构重建导出工作簿 {}", written)
    return written, {name: int(len(frame)) for name, frame in sheets.items()}


def _append_backtest_sheet(
    path: Path,
    sheet: str,
    fresh: pd.DataFrame,
    columns: list[str],
) -> tuple[pd.DataFrame, int]:
    """读出这张回测表里已有的行，再把这次的新行接上去。"""
    existing = read_sheet(path, sheet) if path.exists() else pd.DataFrame()
    if sheet == SHEET_BACKTEST_TRADES:
        existing = _fill_trade_strategy(path, existing)
    # 回测时间参与去重：同一时刻再导出不重复；另一次回测即使成交看起来一样，也会另起一组。
    return append_frames(existing, fresh, columns, stamp_column="回测时间")


def _fill_trade_strategy(path: Path, existing: pd.DataFrame) -> pd.DataFrame:
    """
    旧版导出的「回测成交」没有「策略」列。

    同一次回测在「回测摘要」里写着策略名，按回测时间对回去补上，
    这样升级表头后旧成交不会被整张丢掉。
    """
    if existing is None or existing.empty or "策略" in existing.columns:
        return existing
    out = existing.copy()
    out["策略"] = ""
    summary = read_sheet(path, SHEET_BACKTEST) if path.exists() else pd.DataFrame()
    if summary is None or summary.empty:
        return out
    if "回测时间" not in summary.columns or "策略" not in summary.columns:
        return out
    names: dict[str, str] = {}
    for _, row in summary.iterrows():
        key = _stamp_key(row.get("回测时间"))
        if key:
            names[key] = str(row.get("策略") or "").strip()
    out["策略"] = out["回测时间"].map(lambda value: names.get(_stamp_key(value), ""))
    return out


def _stamp_key(value: Any) -> str:
    """把回测时间统一成「2026-11-05 20:36:30」，方便两张表按时间对上。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        return str(value).strip()


def _appended_backtest_frames(root: Path, path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """导出全部时也追加回测，不能只用最近一次把以前导出的成交盖掉。"""
    summary, trades = _backtest_frames(root)
    summary_all, _added = _append_backtest_sheet(path, SHEET_BACKTEST, summary, list(summary.columns))
    trades_all, _added_trades = _append_backtest_sheet(path, SHEET_BACKTEST_TRADES, trades, list(trades.columns))
    return summary_all, trades_all


def _appended_backtest_signals(root: Path, path: Path) -> pd.DataFrame:
    """回测信号同样接上以前导出的行，旧文件没有这张表时自动新建。"""
    signals = _backtest_signal_frame(root)
    merged, _added = _append_backtest_sheet(
        path, SHEET_BACKTEST_SIGNALS, signals, list(signals.columns)
    )
    return merged


def export_cross(root: Path | str, excel_path: Path | str | None = None) -> tuple[Path, int, int]:
    """
    把最近一次交叉测试追加进统一 Excel 的「交叉测试」表。

    每一行是一个品种和一套策略的组合，列和页面上的结果表一样。
    第一列是这一轮跑完的时间。同一轮再导出不重复写，别的表不动。

    返回 (文件路径, 这次新增行数, 表里总行数)。
    还没跑过交叉测试时抛出 ValueError。
    """
    root_path = Path(root)
    path = Path(excel_path) if excel_path else default_workbook_path(root_path)
    fresh = _cross_frame(root_path)
    if fresh.empty or str(fresh.iloc[0].get("测试时间") or "").startswith("还没有"):
        raise ValueError("还没有交叉测试结果。请先点「开始交叉测试」，完成后再导出。")
    merged, added = _append_cross_sheet(path, fresh)
    written = write_sheet(path, SHEET_CROSS, merged)
    return written, added, int(len(merged))


def _eval_text(evaluation: Any) -> str:
    """优化导出用的评估结论。老结果没有这一块时留空。"""
    if not isinstance(evaluation, dict) or not evaluation:
        return ""
    return str(evaluation.get("conclusion_text") or "")


def _eval_failed(evaluation: Any) -> str:
    """优化导出用的未通过项。"""
    if not isinstance(evaluation, dict):
        return ""
    items = evaluation.get("failed_items") or []
    return "、".join(str(item) for item in items)


def export_optimize(root: Path | str, excel_path: Path | str | None = None) -> tuple[Path, int, int]:
    """
    把最近一次策略优化追加进统一 Excel 的「优化结果」表，不盖掉以前的优化。

    第一列「测试时间」是这次优化跑完的时刻，同一次的行时间相同。
    同一次再导出不重复写，别的表不动。

    返回 (文件路径, 这次新增行数, 表里总行数)。
    还没跑过策略优化时抛出 ValueError。
    """
    root_path = Path(root)
    path = Path(excel_path) if excel_path else default_workbook_path(root_path)
    fresh = _optimize_frame(root_path)
    if fresh.empty or str(fresh.iloc[0].get("测试时间") or "").startswith("还没有"):
        raise ValueError("还没有策略优化结果。请先点「开始策略优化」，完成后再导出。")
    merged, added = _append_optimize_sheet(path, fresh)
    written = write_sheet(path, SHEET_OPTIMIZE, merged)
    return written, added, int(len(merged))


def export_account_part(root: Path | str, excel_path: Path | str, part: str) -> tuple[Path, str, int]:
    """
    只导出账户分析里的一张表。

    part 为 trades 时写「交易记录」，为 changes 时写「资金变动记录」。
    写进同一本 Excel 时，只换这一张，别的表留着。
    """
    root_path = Path(root)
    path = Path(excel_path)
    fills, changes = _account_frames(root_path)
    if part == "trades":
        sheet, frame = SHEET_FILLS, fills
    elif part == "changes":
        sheet, frame = SHEET_CHANGES, changes
    else:
        raise ValueError("只能导出交易记录或资金变动记录")
    written = write_sheet(path, sheet, frame)
    return written, sheet, int(len(frame))


def _account_frames(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    从当前账户分析账本做出两张表。

    账户成交是开仓和平仓，表名是「交易记录」。资金明细是初始资金、入金、出金、每日结算差额和交易盈亏，表名是「资金变动记录」。
    还没初始化账户时，两张表各留一行说明，不把空表交给人猜。
    """
    book = load_book(root)
    if book is None:
        return (
            _empty(FILL_COLUMNS, "还没有账户分析账本。到「账户分析」点一次初始化后再导出。"),
            _empty(CHANGE_COLUMNS, "还没有资金变动。"),
        )
    trades = []
    for row in book.get("trades") or []:
        trades.append(
            {
                "时间": row.get("time"),
                "品种": row.get("symbol"),
                "方向": row.get("direction"),
                "开平": row.get("offset"),
                "价格": row.get("price"),
                "手数": row.get("volume"),
                "合约乘数": row.get("multiplier"),
                "原因": row.get("reason"),
            }
        )
    fills = pd.DataFrame(trades, columns=FILL_COLUMNS) if trades else _empty(FILL_COLUMNS, "这本账还没有开仓或平仓。")
    running = 0.0
    changes = []
    for row in book.get("changes") or []:
        amount = round(float(row.get("amount") or 0), 2)
        running = round(running + amount, 2)
        changes.append(
            {
                "时间": row.get("time"),
                "方式": change_kind_name(row.get("kind")),
                "金额": amount,
                "备注": row.get("note") or "",
                "变动后余额": running,
            }
        )
    change_frame = (
        pd.DataFrame(changes, columns=CHANGE_COLUMNS)
        if changes
        else _empty(CHANGE_COLUMNS, "这本账还没有资金变动。")
    )
    return fills, change_frame


def _reviews_frame(root: Path) -> pd.DataFrame:
    """
    每篇复盘一行。
    若有同名 JSON 成绩单，把有效性/稳定性/可信度也写上，方便在 Excel 里筛选。
    正文过长时截断，Excel 单元格大约只能放 3 万字。
    """
    import json

    folder = root / "data" / "reviews"
    columns = ["日期", "品种", "有效性", "稳定性", "可信度", "缺陷", "文件", "报告"]
    if not folder.exists():
        return _empty(columns, "还没有生成过复盘。到「复盘」页点一次生成后再导出。")
    rows: list[dict[str, object]] = []
    for file in sorted(folder.glob("*.txt")):
        stem = file.stem
        day, _, symbol = stem.partition("_")
        text = file.read_text(encoding="utf-8")
        if len(text) > 30000:
            text = text[:30000] + "\n……（报告过长，已截断。全文仍在 data/reviews 的 txt 里）"
        eff = stab = cred = defects = ""
        side = folder / f"{stem}.json"
        if side.exists():
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    eff = meta.get("effectiveness", "")
                    stab = meta.get("stability", "")
                    cred = meta.get("credibility", "")
                    tags = meta.get("defects") or []
                    if isinstance(tags, list):
                        defects = "、".join(str(x) for x in tags)
            except (OSError, json.JSONDecodeError):
                pass
        rows.append(
            {
                "日期": day,
                "品种": symbol,
                "有效性": eff,
                "稳定性": stab,
                "可信度": cred,
                "缺陷": defects,
                "文件": file.name,
                "报告": text,
            }
        )
    if not rows:
        return _empty(columns, "还没有生成过复盘。到「复盘」页点一次生成后再导出。")
    return pd.DataFrame(rows, columns=columns)


def _cross_frame(root: Path) -> pd.DataFrame:
    """
    最近一次交叉测试：每个品种和策略的组合一行。

    顺序和页面一样，较好的在前。没有结果时留一句说明，导出全部仍能生成这张表。
    多品种验收（mode=opt_cross）存的已经是表格字串，不能再走 list_rows 重排，否则策略名会丢。
    """
    from src.screen.picker import list_rows

    payload = _read_json(root / "data" / "symbol_picks.json")
    rows = list((payload or {}).get("rows") or [])
    if not payload or not rows:
        return _empty(CROSS_COLUMNS, "还没有交叉测试结果。到「交叉测试」页跑一次后再导出。")
    mode = str((payload or {}).get("mode") or "")
    if mode == "opt_cross":
        shown = list(rows)
    else:
        shown = list_rows(payload)
    spec = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    ran_at = str(payload.get("ran_at") or "").strip() or _backtest_ran_at(payload, root / "data" / "symbol_picks.json")
    lots = payload.get("volume")
    if lots in (None, ""):
        lots = spec.get("fixed_volume") or ""
    out = []
    for row in shown:
        # 盈利覆盖等几列：优先用表格已算好的字串；多品种验收那批没有时再当场补算。
        cover_cols = _cross_cover_cells(row)
        out.append(
            {
                "测试时间": ran_at,
                "品种": row.get("symbol") or "",
                "策略": row.get("strategy") or "—",
                "胜率": row.get("win_rate") or "",
                "盈利因子": row.get("profit_factor") or "",
                "盈亏比": row.get("payoff") or "",
                "收益期望": row.get("expectancy") or "",
                "最大回撤": row.get("max_drawdown") or "",
                "卡尔玛": row.get("calmar") or "",
                "最大连亏": row.get("loss_streak") or "",
                "净盈亏": row.get("net_pnl") or "",
                "盈利覆盖": cover_cols["盈利覆盖"],
                "收益率": row.get("return_rate") or "",
                "交易频率": row.get("frequency") or "",
                "笔数": cover_cols["笔数"],
                "每笔收益": cover_cols["每笔收益"],
                "实际测试时段": row.get("span") or "",
                "开始时间": cover_cols["开始时间"],
                "结束时间": cover_cols["结束时间"],
                "天数": cover_cols["天数"],
                "日均": cover_cols["日均"],
                "K线根数": row.get("bars") or "",
                "得分": row.get("score") or "",
                "评估": row.get("evaluation") or "",
                "未通过项": row.get("fail_items") or "",
                "流动性": row.get("liquidity") or "",
                "结果": row.get("picked") or "",
                "持仓手数": lots,
                "每笔风险%": spec.get("risk_per_trade_pct", ""),
                "开仓最小间隔秒": spec.get("min_order_interval_sec", ""),
                "每天最多开仓": spec.get("max_orders_per_day", ""),
                "信号周期": row.get("timeframe") or spec.get("timeframe") or "",
                "止损周期": row.get("timeframe") or spec.get("trail_timeframe") or spec.get("timeframe") or "",
                "行情来源": spec.get("data_source") or payload.get("data_source") or "",
                "开始日期": spec.get("start_date") or "",
                "结束日期": spec.get("end_date") or "",
            }
        )
    return pd.DataFrame(out, columns=CROSS_COLUMNS)


def _cross_cover_cells(row: dict[str, Any]) -> dict[str, Any]:
    """
    交叉导出里的盈利覆盖、笔数、每笔、起止、天数、日均。

    正常交叉测试行已经在 list_rows 里算好；多品种验收行只有净盈亏等原始数，这里再算一遍。
    导出尽量写成数字，方便在 Excel 里排序和筛选。
    """
    from src.screen.picker import span_cover_fields

    ready = {
        "盈利覆盖": row.get("profit_cover"),
        "笔数": row.get("trade_count"),
        "每笔收益": row.get("pnl_per_trade"),
        "开始时间": row.get("span_start"),
        "结束时间": row.get("span_end"),
        "天数": row.get("span_days"),
        "日均": row.get("daily_avg"),
    }
    # 没测成的行在表格里是「—」，导出留空，不要算成 0。
    if ready.get("盈利覆盖") == "—" or ready.get("笔数") == "—" or row.get("picked") == "没测成":
        return {
            "盈利覆盖": "",
            "笔数": "",
            "每笔收益": "",
            "开始时间": "",
            "结束时间": "",
            "天数": "",
            "日均": "",
        }
    if ready.get("盈利覆盖") not in (None, "") and ready.get("笔数") not in (None, ""):
        return {
            "盈利覆盖": _export_money_number(ready["盈利覆盖"]),
            "笔数": _export_int(ready["笔数"]),
            "每笔收益": _export_money_number(ready["每笔收益"], places=2),
            "开始时间": _export_dash(ready["开始时间"]),
            "结束时间": _export_dash(ready["结束时间"]),
            "天数": _export_int(ready["天数"]),
            "日均": _export_money_number(ready["日均"], places=2),
        }
    span = str(row.get("span") or "")
    start = ""
    end = ""
    if " ~ " in span:
        start, end = [part.strip()[:10] for part in span.split(" ~ ", 1)]
    elif "~" in span:
        start, end = [part.strip()[:10] for part in span.split("~", 1)]
    raw = span_cover_fields(
        _export_money_number(row.get("net_pnl")),
        _export_money_number(row.get("max_drawdown")),
        _export_int(row.get("trade_count") or row.get("trades") or _trades_from_frequency(row.get("frequency"))),
        start or str(row.get("data_from") or ""),
        end or str(row.get("data_to") or ""),
    )
    return {
        "盈利覆盖": _blank_or(raw["profit_cover"], places=0),
        "笔数": raw["trade_count"] if raw["trade_count"] else "",
        "每笔收益": _blank_or(raw["pnl_per_trade"], places=2),
        "开始时间": raw["span_start"] or "",
        "结束时间": raw["span_end"] or "",
        "天数": raw["span_days"] if raw["span_days"] is not None else "",
        "日均": _blank_or(raw["daily_avg"], places=2),
    }


def _trades_from_frequency(text: Any) -> int:
    """从「11笔，约2.7天1笔」里取出笔数。对不上就当 0。"""
    raw = str(text or "")
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _export_dash(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in ("", "—", "-") else text


def _export_int(value: Any) -> Any:
    text = str(value or "").strip().replace("元", "")
    if text in ("", "—", "-"):
        return ""
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return text


def _export_money_number(value: Any, places: int | None = None) -> Any:
    """表格上的「10380元」导出成数字，方便 Excel 排序。"""
    text = str(value if value is not None else "").strip().replace("元", "").replace(",", "")
    if text in ("", "—", "-"):
        return ""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    if places is None:
        return int(round(number)) if abs(number - round(number)) < 1e-9 else number
    return round(number, places)


def _blank_or(value: Any, places: int = 0) -> Any:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if places <= 0:
        if abs(number - round(number)) < 1e-9:
            return int(round(number))
        # 金额不是整数时保留两位小数；直接写原始浮点会带 3431.1000000000004 这种尾巴，
        # 读回来对不上，同一轮会被当成新行再写一次。
        rounded = round(number, 2)
        return int(round(rounded)) if abs(rounded - round(rounded)) < 1e-9 else rounded
    return round(number, places)


def _append_cross_sheet(path: Path, fresh: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """把这一轮交叉测试接到已有表后面。测试时间相同且内容相同的行不再写第二次。"""
    existing = read_sheet(path, SHEET_CROSS) if path.exists() else pd.DataFrame()
    return append_frames(existing, fresh, CROSS_COLUMNS, stamp_column="测试时间")


def _appended_cross_frame(root: Path, path: Path) -> pd.DataFrame:
    """导出全部时保留以前各轮交叉测试，再把最近一轮接上去。还没有结果时留一句说明。"""
    fresh = _cross_frame(root)
    merged, _added = _append_cross_sheet(path, fresh)
    if merged.empty:
        return fresh
    return merged


def _append_optimize_sheet(path: Path, fresh: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """把这次优化接到已有表后面。测试时间相同且内容相同的行不再写第二次。"""
    existing = read_sheet(path, SHEET_OPTIMIZE) if path.exists() else pd.DataFrame()
    return append_frames(existing, fresh, OPTIMIZE_COLUMNS, stamp_column="测试时间")


def _appended_optimize_frame(root: Path, path: Path) -> pd.DataFrame:
    """导出全部时保留以前各次优化结果，再把最近一次接上去。还没有结果时留一句说明。"""
    fresh = _optimize_frame(root)
    merged, _added = _append_optimize_sheet(path, fresh)
    if merged.empty:
        return fresh
    return merged


def _backtest_expectancy(perf: dict[str, Any]) -> float | str:
    """
    回测摘要里的无单位收益期望。始终用胜率、平均盈利、平均亏损当场算，
    避免旧报告里还是「元/笔」的数字。没有亏损笔时留空。
    """
    from src.backtest.metrics import unit_expectancy

    try:
        avg_loss = float(perf.get("avg_loss") or 0)
    except (TypeError, ValueError):
        avg_loss = 0.0
    if avg_loss <= 1e-9:
        return ""
    try:
        win_rate = float(perf.get("win_rate") or 0)
        avg_win = float(perf.get("avg_win") or 0)
    except (TypeError, ValueError):
        return ""
    return unit_expectancy(win_rate, avg_win, avg_loss)


def _backtest_frames(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """最近一次回测：摘要一张表，成交明细另一张。两张表第一列都是这次回测跑完的时间。"""
    report_path = root / "data" / "backtest_last.json"
    payload = _read_json(report_path)
    if not payload:
        return (
            _empty(BACKTEST_SUMMARY_COLUMNS, "还没有历史回测结果。到「历史回测」页跑一次后再导出。"),
            _empty(BACKTEST_TRADE_COLUMNS, "还没有历史回测成交。"),
        )
    # 同一次回测的摘要和每一笔成交写同一个时间，Excel 里按这一列筛选就能只看这一次。
    ran_at = _backtest_ran_at(payload, report_path)
    perf = payload.get("performance") or {}
    snap = payload.get("params_snapshot") or {}
    engine = str(snap.get("engine") or payload.get("engine") or "").strip()
    flatten = snap.get("flatten_at_close")
    if flatten is True:
        flatten_text = "是"
    elif flatten is False:
        flatten_text = "否"
    else:
        flatten_text = ""
    from src.screen.picker import cover_export_cells

    cover = cover_export_cells(
        perf.get("net_pnl"),
        perf.get("max_drawdown"),
        perf.get("trades"),
        str(snap.get("data_from") or ""),
        str(snap.get("data_to") or ""),
    )
    summary = pd.DataFrame(
        [
            {
                "回测时间": ran_at,
                "品种": payload.get("symbol"),
                "策略": strategy_display_name(engine) if engine else "",
                "信号周期": payload.get("timeframe") or snap.get("timeframe") or "",
                "止损周期": payload.get("trail_timeframe") or snap.get("trail_timeframe") or "",
                "夜盘分钟": snap.get("eth_minutes", ""),
                "收盘清仓": flatten_text,
                "K线根数": payload.get("bars"),
                "成交笔数": perf.get("trades"),
                "胜率": perf.get("win_rate"),
                "平均盈利": perf.get("avg_win"),
                "平均亏损": perf.get("avg_loss"),
                "盈亏比": perf.get("payoff"),
                "收益期望": _backtest_expectancy(perf),
                "净盈亏": perf.get("net_pnl"),
                "盈利因子": perf.get("profit_factor"),
                "最大回撤": perf.get("max_drawdown"),
                **cover,
            }
        ],
        columns=BACKTEST_SUMMARY_COLUMNS,
    )
    trades = []
    for item in payload.get("trades") or []:
        trades.append(
            {
                "回测时间": ran_at,
                "品种": item.get("symbol"),
                "策略": strategy_display_name(engine) if engine else "",
                "方向": _side_cn(item.get("side")),
                "开仓时间": item.get("entry_time"),
                "平仓时间": item.get("exit_time"),
                "开仓价": item.get("entry_price"),
                "平仓价": item.get("exit_price"),
                "手数": item.get("volume"),
                "盈亏": item.get("pnl"),
                "开仓原因": item.get("reason_entry"),
                "平仓原因": item.get("reason_exit"),
            }
        )
    trade_df = (
        pd.DataFrame(trades, columns=BACKTEST_TRADE_COLUMNS)
        if trades
        else _empty(BACKTEST_TRADE_COLUMNS, "这次回测没有成交。")
    )
    return summary, trade_df


def _backtest_signal_frame(root: Path) -> pd.DataFrame:
    """最近一次回测里「有开仓信号但没做」的记录。旧的结果文件没有这一项时给一句话说明。"""
    report_path = root / "data" / "backtest_last.json"
    payload = _read_json(report_path)
    if not payload:
        return _empty(BACKTEST_SIGNAL_COLUMNS, "还没有回测结果。到「历史回测」页跑一次后再导出。")
    ran_at = _backtest_ran_at(payload, report_path)
    rows = []
    for item in payload.get("rejected") or []:
        rows.append(
            {
                "回测时间": ran_at,
                "品种": item.get("symbol") or payload.get("symbol") or "",
                "信号时间": str(item.get("time") or "").replace("T", " "),
                "方向": item.get("side") or "",
                "信号价": item.get("price") if item.get("price") is not None else "",
                "止损价": item.get("stop") if item.get("stop") is not None else "",
                "拦截层": item.get("stage") or "",
                "原因": item.get("reason") or "",
            }
        )
    if not rows:
        return _empty(
            BACKTEST_SIGNAL_COLUMNS,
            "这次回测每个开仓信号都成交了，没有被拒绝的记录。",
        )
    return pd.DataFrame(rows, columns=BACKTEST_SIGNAL_COLUMNS)


def _day_gap(start: Any, end: Any) -> int | None:
    """两个日期字符串差几天；解析不了返回 None。"""
    try:
        left = datetime.fromisoformat(str(start).replace("T", " ")[:10]).date()
        right = datetime.fromisoformat(str(end).replace("T", " ")[:10]).date()
    except (TypeError, ValueError):
        return None
    return (right - left).days


def _walk_holdout_days(payload: dict[str, Any]) -> int | None:
    """滚动前进各段检验区间天数之和。一次切断返回 None，走首尾差。"""
    if str(payload.get("mode") or "") != "滚动前进":
        return None
    total = 0
    counted = False
    for fold in payload.get("walk_folds") or []:
        if not isinstance(fold, dict):
            continue
        gap = _day_gap(fold.get("holdout_start"), fold.get("holdout_end"))
        if gap is None:
            continue
        total += max(0, gap)
        counted = True
    return total if counted else None


def _optimize_ran_at(payload: dict[str, Any], report_path: Path) -> str:
    """
    取出这次优化跑完的时间，格式固定成「2026-10-19 14:59:12」。

    优先用跑完时写进结果文件的 finished_at，这样同一份结果导出两次，时间不变，不会被当成新的一次。
    更早的结果文件没有这个字段时，改用该文件的保存时间。
    """
    raw = str(payload.get("finished_at") or "").strip().replace("T", " ")
    if raw:
        return raw[:19] if len(raw) >= 19 else raw
    if report_path.exists():
        return datetime.fromtimestamp(report_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _optimize_frame(root: Path) -> pd.DataFrame:
    """最近一次优化：第一行是最优，后面是得分靠前的尝试。检验字段跟策略优化页对齐。"""
    from src.backtest.optimizer import describe_params
    from src.screen.picker import cover_export_cells

    report_path = root / "data" / "optimize_last.json"
    payload = _read_json(report_path)
    if not payload:
        return _empty(OPTIMIZE_COLUMNS, "还没有优化结果。到「策略优化」页跑一次后再导出。")
    ran_at = _optimize_ran_at(payload, report_path)
    rows: list[dict[str, Any]] = []
    best_perf = payload.get("best_performance") or {}
    train_perf = payload.get("train_performance") or best_perf
    hold_perf = payload.get("holdout_performance") or {}
    start = str(payload.get("start_date") or "")
    end = str(payload.get("end_date") or "")
    train_end = str(payload.get("train_end") or end)
    hold_start = str(payload.get("holdout_start") or start)
    span = f"{start}~{end}"
    engine = str(payload.get("engine") or "").strip()
    strategy_name = strategy_display_name(engine) if engine else ""
    accepted = payload.get("accepted")
    if accepted is True:
        pass_text = "通过"
    elif accepted is False:
        pass_text = "未通过"
    else:
        pass_text = ""
    # 训练段：页面开始日到切断日；检验段：切断日到页面结束日。试错行也按训练段算覆盖。
    train_cover = cover_export_cells(
        train_perf.get("net_pnl"),
        train_perf.get("max_drawdown"),
        train_perf.get("trades"),
        start,
        train_end,
    )
    hold_cover = cover_export_cells(
        hold_perf.get("net_pnl"),
        hold_perf.get("max_drawdown"),
        hold_perf.get("trades"),
        hold_start,
        end,
        span_days=_walk_holdout_days(payload),
    )
    best_cover = cover_export_cells(
        best_perf.get("net_pnl"),
        best_perf.get("max_drawdown"),
        best_perf.get("trades"),
        start,
        train_end,
    )
    # 最优行带完整检验结论；后面的尝试行只填名次和这组参数的训练成绩。
    # 同一次优化的所有行共用同一个「测试时间」，Excel 里按这一列筛选就能只看这一次。
    meta = {
        "测试时间": ran_at,
        "品种": payload.get("symbol"),
        "策略": strategy_name,
        "区间": span,
        "检验方式": payload.get("mode") or "",
        "检验比例%": payload.get("holdout_pct", ""),
        "深度": payload.get("depth") or "",
        "训练成交": train_perf.get("trades"),
        "训练净盈亏": train_perf.get("net_pnl"),
        "训练盈利覆盖": train_cover.get("盈利覆盖", ""),
        "检验成交": hold_perf.get("trades"),
        "检验净盈亏": hold_perf.get("net_pnl"),
        "检验盈利覆盖": hold_cover.get("盈利覆盖", ""),
        "检验效率": payload.get("walk_efficiency", ""),
        "检验段赚钱比例": payload.get("oos_win_rate", ""),
        "是否通过": pass_text,
        "结论": payload.get("accept_reason") or "",
    }
    rows.append(
        {
            "名次": "最优",
            "得分": payload.get("best_score"),
            "评估": _eval_text(payload.get("evaluation")),
            "未通过项": _eval_failed(payload.get("evaluation")),
            "成交笔数": best_perf.get("trades"),
            "胜率": best_perf.get("win_rate"),
            "净盈亏": best_perf.get("net_pnl"),
            "盈利因子": best_perf.get("profit_factor"),
            "最大回撤": best_perf.get("max_drawdown"),
            **best_cover,
            "参数JSON": json.dumps(payload.get("best_params") or {}, ensure_ascii=False),
            "参数说明": describe_params(payload.get("best_params") or {}),
            **meta,
        }
    )
    blank_meta = {key: "" for key in meta}
    blank_meta["测试时间"] = meta["测试时间"]
    blank_meta["品种"] = meta["品种"]
    blank_meta["策略"] = meta["策略"]
    blank_meta["区间"] = meta["区间"]
    blank_meta["检验方式"] = meta["检验方式"]
    blank_meta["检验比例%"] = meta["检验比例%"]
    blank_meta["深度"] = meta["深度"]
    for i, trial in enumerate(payload.get("trials") or [], start=1):
        perf = trial.get("performance") or {}
        trial_cover = cover_export_cells(
            perf.get("net_pnl"),
            perf.get("max_drawdown"),
            perf.get("trades"),
            start,
            train_end,
        )
        rows.append(
            {
                "名次": i,
                "得分": trial.get("score"),
                "成交笔数": perf.get("trades"),
                "胜率": perf.get("win_rate"),
                "净盈亏": perf.get("net_pnl"),
                "盈利因子": perf.get("profit_factor"),
                "最大回撤": perf.get("max_drawdown"),
                **trial_cover,
                "参数JSON": json.dumps(trial.get("params") or {}, ensure_ascii=False),
                "参数说明": describe_params(trial.get("params") or {}),
                **blank_meta,
            }
        )
    return pd.DataFrame(rows, columns=OPTIMIZE_COLUMNS)


def _side_cn(side: Any) -> str:
    """回测成交方向：程序里是 long/short，导出写成多/空。"""
    text = str(side or "").strip().lower()
    if text in {"long", "buy", "多"}:
        return "多"
    if text in {"short", "sell", "空"}:
        return "空"
    return str(side or "")


def _backtest_ran_at(payload: dict[str, Any], report_path: Path) -> str:
    """
    取出这次回测的时间，格式固定成「2026-10-19 14:59:12」。

    优先用回测跑完时写进结果文件的 ran_at，这样同一份结果导出两次，时间不变，不会被当成新的一次。
    更早的结果文件里没有这个字段时，改用该文件的保存时间。
    """
    raw = str(payload.get("ran_at") or "").strip().replace("T", " ")
    if raw:
        return raw[:19] if len(raw) >= 19 else raw
    if report_path.exists():
        return datetime.fromtimestamp(report_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return raw if isinstance(raw, dict) else None


def _empty(columns: list[str], note: str) -> pd.DataFrame:
    """没有数据时仍留下表头，并在第一格写明原因，打开 Excel 不会看到一张白表。"""
    row = {col: "" for col in columns}
    if columns:
        row[columns[0]] = note
    return pd.DataFrame([row], columns=columns)
