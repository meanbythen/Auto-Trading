"""
主力合约表（config/主力合约表.xlsx）的生成、更新与读取

功能：读 config/品种与合约.xlsx 里启用的品种，按交易所日行情算出每个交易日的主力合约，
      再把连续同一个合约的交易日合并成时段，写成回测用的「主力时段」表；
      同时保留「手动修正」页，更新时原样保留，回测时以手动段覆盖自动段。
原理：主力 = 当日持仓量最大的合约；为避免临近交割的合约因持仓量微幅领先造成抖动，
      新合约需比现任合约持仓量高出 OI_SWITCH_MARGIN（默认 5%），且连续 OI_CONFIRM_DAYS
      个交易日成立才切换；现任合约当天没有行情则立即切换。
      短于 MIN_SEGMENT_DAYS 个交易日的段视为判定抖动，并入相邻段。
      上期所 / 郑商所 / 能源中心用 akshare get_futures_daily 的交易所日行情；
      大商所该接口不可用时，用 akshare futures_zh_daily_sina 的 hold 字段（持仓量）逐合约取。
用法：在「历史回测」页点「更新主力合约表」按钮；命令行见 tools/update_main_contract_table.py。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from loguru import logger

from src.risk.universe_table import product_key, read_universe_table
from src.trader.ctp_logic import ctp_exchange_id

UNIVERSE_FILE = "config/品种与合约.xlsx"
DEFAULT_OUT = "config/主力合约表.xlsx"

SHEET_PERIODS = "主力时段"
SHEET_MANUAL = "手动修正"
MANUAL_HEADERS = ("品种", "主力合约", "开始日期", "结束日期", "备注")

# 新合约持仓量要比现任高出这个比例，且连续这么多天成立，才认换月。
OI_SWITCH_MARGIN = 0.05
OI_CONFIRM_DAYS = 2
# 短于这么多交易日的段算判定抖动，并入相邻段。
MIN_SEGMENT_DAYS = 10

FONT_NAME = "微软雅黑"
HEADER_FILL = "1F4E78"
HEAD_WHITE = "FFFFFF"
TEXT_DARK = "1F2937"
ZEBRA_FILL = "F7F9FC"
BORDER_COLOR = "D9DEE7"
DATE_FMT = "yyyy-mm-dd"


# ---------------------------------------------------------------- 行情


def _to_dates(series: pd.Series) -> pd.Series:
    """日期列统一成 Timestamp。交易所接口给的是 20261008 这种整数，不能按纳秒解析。"""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series.astype("Int64").astype(str), format="%Y%m%d", errors="coerce")
    return pd.to_datetime(series, errors="coerce")


def _fetch_exchange_daily(exchange: str, start: date, end: date) -> pd.DataFrame:
    """交易所日行情。返回列：date, symbol, volume, open_interest。"""
    import akshare as ak

    raw = None
    for attempt in range(2):
        try:
            raw = ak.get_futures_daily(
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                market=exchange,
            )
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 1:
                raise RuntimeError(f"{exchange} 日行情下载失败: {e}") from e
            logger.warning("{} 日行情第一次失败，重试: {}", exchange, e)
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=["date", "symbol", "volume", "open_interest"])
    names = {str(c).strip().lower(): c for c in raw.columns}
    sym_col = names.get("symbol") or names.get("合约") or names.get("代码")
    date_col = names.get("date") or names.get("日期")
    oi_col = names.get("open_interest") or names.get("持仓量")
    vol_col = names.get("volume") or names.get("成交量")
    if not sym_col or not date_col or not oi_col:
        raise RuntimeError(f"{exchange} 日行情缺少合约/日期/持仓量列：{list(raw.columns)}")
    out = pd.DataFrame(
        {
            "date": _to_dates(raw[date_col]),
            "symbol": raw[sym_col].astype(str).str.strip(),
            "volume": pd.to_numeric(raw[vol_col], errors="coerce").fillna(0) if vol_col else 0,
            "open_interest": pd.to_numeric(raw[oi_col], errors="coerce").fillna(0),
        }
    )
    out["variety"] = out["symbol"].str.extract(r"^([A-Za-z]+)")[0].str.upper()
    return out.dropna(subset=["date"])


def _dce_months(start: date, end: date) -> list[str]:
    """大商所合约月份：覆盖统计区间里可能还在交易的合约。"""
    months: list[str] = []
    for year in range(start.year, end.year + 2):
        for month in range(1, 13):
            months.append(f"{year % 100:02d}{month:02d}")
    return months


def _fetch_dce_contract(symbol: str, start: date, end: date) -> pd.DataFrame | None:
    import akshare as ak

    try:
        raw = ak.futures_zh_daily_sina(symbol=symbol.upper())
    except Exception:  # noqa: BLE001
        return None
    if raw is None or len(raw) == 0:
        return None
    frame = pd.DataFrame(
        {
            "date": _to_dates(raw["date"]),
            "symbol": symbol,
            "volume": pd.to_numeric(raw["volume"], errors="coerce").fillna(0),
            "open_interest": pd.to_numeric(raw["hold"], errors="coerce").fillna(0),
        }
    )
    frame = frame[(frame["date"].dt.date >= start) & (frame["date"].dt.date <= end)]
    frame["variety"] = product_key(symbol).upper()
    return frame if len(frame) else None


def fetch_product_history(
    products: list[dict],
    start: date,
    end: date,
    progress: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """把各品种的日行情汇总成一张表：date, variety, symbol, volume, open_interest。"""

    def say(text: str) -> None:
        logger.info(text)
        if progress:
            progress(text)

    groups: dict[str, list[str]] = {}
    for row in products:
        groups.setdefault(row["exchange"], []).append(row["product"])

    frames: list[pd.DataFrame] = []
    for exchange, codes in groups.items():
        if exchange in {"SHFE", "CZCE", "INE"}:
            say(f"下载 {exchange} 日行情（{len(codes)} 个品种）…")
            try:
                daily = _fetch_exchange_daily(exchange, start, end)
            except Exception as e:  # noqa: BLE001
                logger.warning("{} 日行情没取到，这部分品种本次跳过: {}", exchange, e)
                continue
            frames.append(daily[daily["variety"].str.lower().isin(codes)])
            say(f"{exchange} 日行情 {len(daily)} 行")
        elif exchange == "DCE":
            for code in codes:
                got = 0
                say(f"下载 DCE {code} 各月合约日线…")
                for yymm in _dce_months(start, end):
                    one = _fetch_dce_contract(f"{code}{yymm}", start, end)
                    if one is not None:
                        frames.append(one)
                        got += 1
                say(f"DCE {code} 拉到 {got} 个合约")
        else:
            logger.warning("{} 暂不支持自动更新，跳过 {}", exchange, codes)
    if not frames:
        return pd.DataFrame(columns=["date", "variety", "symbol", "volume", "open_interest"])
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["variety"] = out["variety"].str.lower()
    return out


# ---------------------------------------------------------------- 主力判定


def normalize_contract(symbol: str, day: date, exchange: str = "") -> tuple[str, str]:
    """
    合约代码统一成四位年月，并返回交割月份。

    郑商所日行情是三位年月：TA605 → TA2605（6 表示 2026 年）。
    郑商所代码大写，上期所和大商所小写，和配置里的写法一致。
    """
    text = str(symbol).strip()
    alpha = "".join(ch for ch in text if ch.isalpha())
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 3:
        year = (day.year // 10) * 10 + int(digits[0])
        if year < day.year - 5:
            year += 10
        digits = f"{year % 100:02d}{digits[1:]}"
    code = alpha.upper() if exchange == "CZCE" else alpha.lower()
    delivery = f"20{digits[:2]}-{digits[2:]}" if len(digits) == 4 else ""
    return f"{code}{digits}", delivery


def _merge_short_periods(periods: list[dict], min_days: int) -> list[dict]:
    """
    把短于 min_days 的段并进下一段（下一段的开始日期提前）。

    主力判定在临近交割的合约上会抖动，抖出来的段通常只有几天。最后一段不并：
    表尾新换的主力往往只走了几天，硬并进上一段会用到已经到期的合约。
    """
    if min_days <= 0 or len(periods) <= 1:
        return periods
    out = list(periods)
    changed = True
    while changed:
        changed = False
        for i in range(len(out) - 1):
            item = out[i]
            if item["days"] >= min_days:
                continue
            nxt = out[i + 1]
            nxt["start"] = item["start"]
            nxt["days"] += item["days"]
            nxt["oi_sum"] += item["oi_sum"]
            nxt["avg_oi"] = nxt["oi_sum"] / nxt["days"] if nxt["days"] else 0
            del out[i]
            changed = True
            break
    return out


def dominant_periods(
    daily: pd.DataFrame,
    product: str,
    name: str,
    exchange: str,
    *,
    margin: float = OI_SWITCH_MARGIN,
    confirm: int = OI_CONFIRM_DAYS,
    min_days: int = MIN_SEGMENT_DAYS,
) -> list[dict]:
    """
    逐日选主力，再把连续同一个合约的交易日合并成时段。

    margin / confirm：新合约持仓量要领先现任 margin 且连续 confirm 天，才认换月，
    避免临近交割的合约因持仓量只差几十手就换月。
    """
    sub = daily[daily["variety"] == product].copy()
    if len(sub) == 0:
        return []
    sub["contract"] = [
        normalize_contract(s, d.date(), exchange)[0] for s, d in zip(sub["symbol"], sub["date"])
    ]
    sub["delivery"] = [
        normalize_contract(s, d.date(), exchange)[1] for s, d in zip(sub["symbol"], sub["date"])
    ]
    sub = sub.sort_values(["date", "open_interest", "volume"], ascending=[True, False, False])

    current: str | None = None
    streak = 0
    periods: list[dict] = []
    for _, group in sub.groupby("date", sort=True):
        top = group.iloc[0]
        contracts = set(group["contract"])
        if current is None:
            current = top["contract"]
            streak = 0
        elif current not in contracts:
            # 现任合约到期、当天没行情：直接换到当天持仓量最大的。
            current = top["contract"]
            streak = 0
        elif top["contract"] != current:
            cur = group[group["contract"] == current].iloc[0]
            if float(top["open_interest"]) >= float(cur["open_interest"]) * (1 + margin):
                streak += 1
            else:
                streak = 0
            if streak >= max(1, int(confirm)):
                current = top["contract"]
                streak = 0
        else:
            streak = 0

        mine = group[group["contract"] == current]
        row = mine.iloc[0] if len(mine) else top
        day = row["date"].date()
        if periods and periods[-1]["contract"] == current and periods[-1]["end"] < day:
            periods[-1]["end"] = day
            periods[-1]["days"] += 1
            periods[-1]["oi_sum"] += float(row["open_interest"])
        else:
            periods.append(
                {
                    "product": product,
                    "name": name,
                    "exchange": exchange,
                    "contract": current,
                    "delivery": row["delivery"],
                    "start": day,
                    "end": day,
                    "days": 1,
                    "oi_sum": float(row["open_interest"]),
                }
            )
    for item in periods:
        item["avg_oi"] = item["oi_sum"] / item["days"] if item["days"] else 0
    return _merge_short_periods(periods, min_days)


# ---------------------------------------------------------------- 读手动修正


@dataclass(frozen=True)
class ManualRow:
    """「手动修正」页的一行：这段时间用这个合约。"""

    product: str
    symbol: str
    start: date
    end: date
    note: str = ""


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _find_header(raw: pd.DataFrame, names_need: tuple[str, ...]) -> tuple[int, dict[str, int]] | None:
    """在前 12 行里找同时写着 names_need 的表头行。"""
    for i in range(min(12, len(raw))):
        names = [_as_text(v) for v in raw.iloc[i].tolist()]
        if all(n in names for n in names_need):
            return i, {name: idx for idx, name in enumerate(names) if name}
    return None


def read_manual_rows(path: Path | str) -> list[ManualRow]:
    """读「手动修正」页。页不存在或没有有效行时返回空列表。"""
    file = Path(path)
    if not file.exists():
        return []
    try:
        raw = pd.read_excel(file, sheet_name=SHEET_MANUAL, header=None)
    except ValueError:
        return []
    found = _find_header(raw, ("品种", "主力合约", "开始日期", "结束日期"))
    if found is None:
        return []
    header_row, columns = found
    out: list[ManualRow] = []
    for i in range(header_row + 1, len(raw)):
        row = raw.iloc[i]
        product = product_key(_as_text(row.iloc[columns["品种"]]))
        symbol = _as_text(row.iloc[columns["主力合约"]])
        start = _as_date(row.iloc[columns["开始日期"]])
        end = _as_date(row.iloc[columns["结束日期"]])
        if not product or not symbol or start is None or end is None or end < start:
            continue
        note = _as_text(row.iloc[columns["备注"]]) if "备注" in columns else ""
        out.append(ManualRow(product, symbol, start, end, note))
    out.sort(key=lambda r: (r.product, r.start))
    return out


def merge_manual(periods: list[dict], manual: list[ManualRow]) -> list[dict]:
    """
    手动段覆盖同品种同区间的自动段。

    自动段被手动段截掉重叠部分，剩下的片段保留；这样只改一段时不用把整个品种都重写。
    """
    if not manual:
        return periods
    by_product: dict[str, list[ManualRow]] = {}
    for row in manual:
        by_product.setdefault(row.product, []).append(row)

    out: list[dict] = []
    for item in periods:
        cuts = by_product.get(item["product"]) or []
        pieces = [(item["start"], item["end"])]
        for cut in cuts:
            nxt: list[tuple[date, date]] = []
            for a, b in pieces:
                if cut.end < a or cut.start > b:
                    nxt.append((a, b))
                    continue
                if cut.start > a:
                    nxt.append((a, cut.start - timedelta(days=1)))
                if cut.end < b:
                    nxt.append((cut.end + timedelta(days=1), b))
            pieces = nxt
        for a, b in pieces:
            if b < a:
                continue
            copy_item = dict(item)
            copy_item["start"], copy_item["end"] = a, b
            out.append(copy_item)
    for row in manual:
        name = ""
        for item in periods:
            if item["product"] == row.product:
                name = item["name"]
                break
        symbol = row.symbol
        code = "".join(ch for ch in symbol if ch.isalpha())
        exchange = ctp_exchange_id(symbol) or ""
        delivery = normalize_contract(symbol, row.start, exchange)[1]
        out.append(
            {
                "product": row.product,
                "name": name or product_key(symbol).upper(),
                "exchange": exchange,
                "contract": symbol,
                "delivery": delivery,
                "start": row.start,
                "end": row.end,
                "days": (row.end - row.start).days + 1,
                "oi_sum": 0.0,
                "avg_oi": 0,
                "manual": True,
            }
        )
    out.sort(key=lambda x: (x["product"], x["start"]))
    return out


# ---------------------------------------------------------------- 写表


def _style_header(cell) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    cell.font = Font(name=FONT_NAME, size=10.5, bold=True, color=HEAD_WHITE)
    cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    cell.border = _thin_border()


def _style_body(cell, *, zebra: bool, align: str = "center", bold: bool = False) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    cell.font = Font(name=FONT_NAME, size=10, bold=bold, color=TEXT_DARK)
    cell.fill = PatternFill("solid", fgColor=ZEBRA_FILL if zebra else "FFFFFF")
    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
    cell.border = _thin_border()


def _thin_border():
    from openpyxl.styles import Border, Side

    side = Side(style="thin", color=BORDER_COLOR)
    return Border(left=side, right=side, top=side, bottom=side)


def _title(ws, text: str, subtitle: str, columns: int) -> None:
    from openpyxl.styles import Alignment, Font

    ws["A1"] = text
    ws["A1"].font = Font(name=FONT_NAME, size=16, bold=True, color=HEADER_FILL)
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws["A2"] = subtitle
    ws["A2"].font = Font(name=FONT_NAME, size=9, color="595959")
    ws["A2"].alignment = Alignment(horizontal="left", vertical="center")
    if columns > 1:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=columns)
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=columns)


def _table(ws, headers: list[str], rows: list[list], widths: list[float]) -> None:
    from openpyxl.utils import get_column_letter

    for idx, name in enumerate(headers, start=1):
        cell = ws.cell(row=3, column=idx, value=name)
        _style_header(cell)
        ws.column_dimensions[get_column_letter(idx)].width = widths[idx - 1]
    for r, values in enumerate(rows, start=4):
        zebra = (r % 2) == 1
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            _style_body(cell, zebra=zebra)
    ws.freeze_panes = "A4"


def _write_sheet_current(ws, products: list[dict], periods: list[dict], start: date, end: date) -> None:
    from openpyxl.styles import Font, PatternFill

    _title(
        ws,
        f"{start.year} 年当前品种主力合约总览",
        f"统计区间 {start} ~ {end} ｜ 主力按日终持仓量判定 ｜ 生成日期 {date.today()}",
        9,
    )
    by_product: dict[str, list[dict]] = {}
    for item in periods:
        by_product.setdefault(item["product"], []).append(item)
    rows = []
    for item in products:
        got = by_product.get(item["product"]) or []
        latest = got[-1] if got else None
        if latest is None:
            rows.append([item["symbol"], item["name"], item["exchange"], item["product"], "", "", "无数据", 0, "该区间没有行情"])
            continue
        same = "是" if normalize_contract(item["symbol"], end, item["exchange"])[0] == latest["contract"] else "否"
        note = "配置已是最新主力" if same == "是" else f"当前主力已变为 {latest['contract']}"
        if latest.get("manual"):
            note = f"手动修正：{latest['contract']}"
        rows.append(
            [
                item["symbol"],
                item["name"],
                item["exchange"],
                item["product"],
                latest["contract"],
                latest["delivery"],
                same,
                len(got),
                note,
            ]
        )
    _table(
        ws,
        ["配置合约", "中文名", "交易所", "品种代码", "当前主力", "交割月份", "与配置一致", "本表换月次数", "说明"],
        rows,
        [14, 10, 9, 10, 13, 10, 12, 14, 24],
    )
    for r in range(4, 4 + len(rows)):
        if ws.cell(row=r, column=5).value:
            ws.cell(row=r, column=5).fill = PatternFill("solid", fgColor="EAF2FF")
            ws.cell(row=r, column=5).font = Font(name=FONT_NAME, size=10, bold=True, color=TEXT_DARK)
        if ws.cell(row=r, column=7).value == "否":
            ws.cell(row=r, column=5).fill = PatternFill("solid", fgColor="FFF4CE")


def _display_product(product: str, exchange: str) -> str:
    """品种代码和合约一样：郑商所大写，上期所和大商所小写。"""
    return product.upper() if exchange == "CZCE" else product.lower()


def _write_sheet_periods(ws, periods: list[dict], start: date, end: date) -> None:
    _title(
        ws,
        f"{start.year} 年主力合约时段明细",
        "自动段：换月需新合约持仓量领先 5% 且连续 2 个交易日 ｜ 手动修正页的段优先 ｜ 每段可直接用于回测",
        9,
    )
    rows = [
        [
            _display_product(p["product"], p["exchange"]),
            p["name"],
            p["exchange"],
            p["contract"],
            p["delivery"],
            p["start"],
            p["end"],
            p["days"],
            round(p["avg_oi"]),
        ]
        for p in periods
    ]
    _table(
        ws,
        ["品种", "中文名", "交易所", "主力合约", "交割月份", "开始日期", "结束日期", "交易日数", "平均持仓量（手）"],
        rows,
        [8, 10, 9, 12, 10, 12, 12, 10, 16],
    )
    for r in range(4, 4 + len(rows)):
        for c in (6, 7):
            ws.cell(row=r, column=c).number_format = DATE_FMT
    ws.auto_filter.ref = f"A3:I{max(3, 3 + len(rows))}"


def _write_sheet_monthly(ws, products: list[dict], periods: list[dict], start: date, end: date) -> None:
    from openpyxl.styles import Alignment

    _title(
        ws,
        f"{start.year} 年逐月主力合约与交割月份",
        "单元格 = 当月交易日数最多的主力合约（括号内为交割月份）｜ 没有行情的月份留空",
        15,
    )
    headers = ["品种", "中文名", "交易所"] + [f"{m} 月" for m in range(1, 13)]
    rows = []
    for item in products:
        cells = [_display_product(item["product"], item["exchange"]), item["name"], item["exchange"]]
        for month in range(1, 13):
            best = None
            best_days = -1
            for p in periods:
                if p["product"] != item["product"]:
                    continue
                overlap = _overlap_days(p, start.year, month)
                if overlap > best_days:
                    best, best_days = p, overlap
            cells.append(f"{best['contract']}\n({best['delivery']})" if best and best_days > 0 else "")
        rows.append(cells)
    _table(ws, headers, rows, [8, 10, 9] + [15] * 12)
    for r in range(4, 4 + len(rows)):
        ws.cell(row=r, column=1).alignment = Alignment(horizontal="left", vertical="center")


def _overlap_days(period: dict, year: int, month: int) -> int:
    first = max(period["start"], date(year, month, 1))
    last_day = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1))
    last = min(period["end"], last_day)
    return (last - first).days + 1 if last >= first else 0


def _write_sheet_notes(ws, start: date, end: date, products: list[dict], source_note: str) -> None:
    from openpyxl.styles import Alignment, Font

    _title(ws, "数据说明与回测用法", "本表用于把历史回测按当时的主力合约分段，贴近实盘成交与流动性", 2)
    rows = [
        ["统计区间", f"{start} 至 {end}（区间内无行情的日期不计）"],
        ["覆盖品种", f"{len(products)} 个：" + "、".join(p["name"] for p in products)],
        ["数据来源", source_note],
        ["主力判定", "每个交易日收盘后取持仓量最大的合约；为避开临近交割合约的微幅抖动，新合约需领先 5% 且连续 2 个交易日才换月"],
        ["短段处理", f"短于 {MIN_SEGMENT_DAYS} 个交易日的段视为判定抖动，已并入相邻段"],
        ["交割月份", "合约代码里的年月，例如 fu2701 为 2027 年 1 月交割；它是交割月，不等同最后交易日"],
        ["换月时点", "按日终持仓量切换，换月当天起算新主力。天勤主连按成交量与持仓量综合挑选，盘中可能比本表提前半天到一天切换"],
        ["回测用法", "在「历史回测」里打开「主力合约换月」，程序会自动按「主力时段」+「手动修正」的分段使用对应合约"],
        ["手动修正", "「手动修正」页写 品种/主力合约/开始日期/结束日期；同品种同区间的自动段会被手动段覆盖，更新表时该页原样保留"],
        ["跨段持仓", "换月时旧仓在段末按收盘价强平，这与实盘换月前平旧仓的做法一致；资金口径二选一：每段同一份起始资金（默认，便于和单合约对照），或真实账户口径（上一段权益接到下一段）"],
        ["临近交割", "交割月前流动性会明显下降，本表只按持仓量判定，实盘还需看盘口深度；最后交易日以交易所公告为准"],
        ["数据局限", "已到期合约的分钟行情通常只剩最近约 1000 根，回测时较早的段可能因缺分钟线而被跳过"],
        ["更新方式", "本表不会自动更新。在「历史回测」页点「更新主力合约表」才会重新拉行情并重算；命令行也可跑 tools/update_main_contract_table.py"],
        ["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ]
    _table(ws, ["项目", "说明"], rows, [14, 105])
    for r in range(4, 4 + len(rows)):
        ws.cell(row=r, column=1).font = Font(name=FONT_NAME, size=10, bold=True, color=TEXT_DARK)
        ws.cell(row=r, column=1).alignment = Alignment(horizontal="left", vertical="center")
        ws.cell(row=r, column=2).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)


def _read_manual_raw(path: Path | None) -> list[list[Any]]:
    """把旧表「手动修正」页的数据行原样取出来（不动用户手填的内容）。"""
    if path is None or not Path(path).exists():
        return []
    try:
        import openpyxl

        book = openpyxl.load_workbook(Path(path), data_only=True, read_only=True)
        if SHEET_MANUAL not in book.sheetnames:
            book.close()
            return []
        sheet = book[SHEET_MANUAL]
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        book.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("旧表手动修正页读出失败，本次按空表写: {}", e)
        return []
    header_at = -1
    for i, row in enumerate(rows[:12]):
        names = [_as_text(v) for v in row]
        if "品种" in names and "主力合约" in names:
            header_at = i
            break
    if header_at < 0:
        return []
    return rows[header_at + 1:]


def _write_sheet_manual(ws, old_rows: list[list[Any]]) -> None:
    from openpyxl.styles import Alignment, Font

    _title(
        ws,
        "手动修正：自定义某段日期用哪个主力合约",
        "在这里加行，回测就按你写的来（同品种同区间的自动段会被覆盖）。更新表时本页内容原样保留。",
        5,
    )
    _table(ws, list(MANUAL_HEADERS), [], [10, 13, 13, 13, 34])
    for r, values in enumerate(old_rows, start=4):
        zebra = (r % 2) == 1
        for c in range(1, 6):
            value = values[c - 1] if c - 1 < len(values) else None
            if isinstance(value, datetime):
                value = value.date()
            cell = ws.cell(row=r, column=c, value=value)
            _style_body(cell, zebra=zebra)
        for c in (3, 4):
            ws.cell(row=r, column=c).number_format = DATE_FMT
    ws.auto_filter.ref = f"A3:E{max(3, 3 + len(old_rows))}"
    for row in ws.iter_rows(min_row=4, max_row=3 + len(old_rows), min_col=5, max_col=5):
        for cell in row:
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            cell.font = Font(name=FONT_NAME, size=10, color=TEXT_DARK)


def write_workbook(
    out: Path,
    products: list[dict],
    periods: list[dict],
    start: date,
    end: date,
    source_note: str,
    manual_rows: list[list[Any]] | None = None,
) -> None:
    from openpyxl import Workbook

    if manual_rows is None:
        manual_rows = _read_manual_raw(out)
    wb = Workbook()
    ws = wb.active
    ws.title = "当前品种"
    _write_sheet_current(ws, products, periods, start, end)
    _write_sheet_periods(wb.create_sheet(SHEET_PERIODS), periods, start, end)
    _write_sheet_monthly(wb.create_sheet("月度对照"), products, periods, start, end)
    _write_sheet_manual(wb.create_sheet(SHEET_MANUAL), manual_rows)
    _write_sheet_notes(wb.create_sheet("说明"), start, end, products, source_note)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)


# ---------------------------------------------------------------- 入口


def _parse_day(text: str | None, default: date) -> date:
    if not text:
        return default
    return datetime.strptime(text, "%Y-%m-%d").date()


def load_products(universe: Path, only: list[str] | None = None) -> list[dict]:
    """读配置品种表。删掉或停用的品种不会出现在这里。"""
    rows = [r for r in read_universe_table(universe) if r.get("enabled", True)]
    out = []
    for row in rows:
        product = product_key(row["symbol"])
        if only and product.lower() not in {p.lower() for p in only}:
            continue
        exchange = ctp_exchange_id(row["symbol"])
        if not exchange:
            logger.warning("认不出 {} 的交易所，跳过", row["symbol"])
            continue
        out.append({"symbol": row["symbol"], "name": row["name"], "product": product, "exchange": exchange})
    return out


def update_table(
    root: str | Path | None = None,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    products: list[str] | None = None,
    out: str | Path | None = None,
    universe: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    拉行情、重算主力时段、重写主力合约表。

    「手动修正」页原样保留。返回本次汇总：输出路径、区间、每个品种的段数和当前主力。
    本函数不会自动被调用，只在界面按钮或命令行触发时执行。
    """
    base = Path(root) if root else Path(".")
    today = date.today()
    end_day = end if isinstance(end, date) else _parse_day(end, today - timedelta(days=1))
    start_day = start if isinstance(start, date) else _parse_day(start, date(end_day.year, 1, 1))
    if end_day < start_day:
        raise ValueError("结束日期不能早于开始日期")

    out_path = Path(out) if out else base / DEFAULT_OUT
    if not out_path.is_absolute():
        out_path = base / out_path
    universe_path = Path(universe) if universe else base / UNIVERSE_FILE
    if not universe_path.is_absolute():
        universe_path = base / universe_path

    items = load_products(universe_path, products)
    if not items:
        raise ValueError("config/品种与合约.xlsx 里没有可用品种（检查「启用」是否为是）")

    summary: dict[str, Any] = {
        "path": str(out_path),
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "products": len(items),
        "by_product": {},
    }
    daily = fetch_product_history(items, start_day, end_day, progress)
    if len(daily) == 0:
        raise RuntimeError("没取到任何日行情，主力合约表未更新")

    periods: list[dict] = []
    for item in items:
        got = dominant_periods(daily, item["product"], item["name"], item["exchange"])
        if not got:
            logger.warning("{} 在该区间没有行情，跳过", item["product"])
            continue
        periods.extend(got)
        summary["by_product"][item["product"]] = {
            "segments": len(got),
            "current": got[-1]["contract"],
        }
        logger.info("{} 共 {} 段，当前主力 {}", item["product"], len(got), got[-1]["contract"])
        if progress:
            progress(f"{item['name']}：{len(got)} 段，当前主力 {got[-1]['contract']}")

    source_note = (
        "上期所、郑商所、能源中心：akshare get_futures_daily 交易所日行情（含持仓量）；"
        "大商所：akshare futures_zh_daily_sina 新浪日线（hold 字段为持仓量）"
    )
    write_workbook(out_path, items, periods, start_day, end_day, source_note)
    summary["periods"] = len(periods)
    logger.info("已写出 {}", out_path)
    return summary
