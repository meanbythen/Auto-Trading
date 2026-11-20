"""
按主力合约时段自动换月的回测

功能：读 config/主力合约表.xlsx 的「主力时段」页，把一个品种的历史拆成若干段；
      每段用它当期的主力合约回测，换月时旧仓在段末按最后收盘价平掉。
      新主力从空仓开始，按策略自己重新评估，不再沿用上一手的多空。
      每段用同一份起始资金（便于和单合约回测对照），各段盈亏累计平移成一条净值曲线。
原理：单合约回测仍用 run_backtest；这里只做分段、权益衔接和结果汇总。
      换月日当天仍用旧合约，下一交易日起才改用新合约。
      新主力用它自己更早的K线把均线算热，再等策略给开仓信号。
"""

from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestResult, run_backtest
from src.backtest.evaluation import EvaluationThresholds, evaluate_backtest
from src.backtest.experiment import fingerprint_file, new_experiment, research_dataset_reference
from src.backtest.main_table import ManualRow, read_manual_rows
from src.backtest.metrics import TradeRecord, calc_performance, peak_margin
from src.risk.delivery import apply_delivery_cutoff, extend_last_before_delivery
from src.risk.sizing import PositionSizer
from src.risk.universe_table import product_key
from src.utils.paths import project_root, resource_root

SHEET_NAME = "主力时段"
TABLE_NAME = "config/主力合约表.xlsx"
TABLE_GLOB = "*主力合约表*.xlsx"

# 结果里最多列出多少段明细，太长时只写前几段。
MAX_LISTED_SEGMENTS = 12

# 两段之间的自然日空档不超过这么多天时，仍看作连续换月（中间只是周末/节假日）。
MAX_ROLLOVER_GAP_DAYS = 15


@dataclass(frozen=True)
class MainPeriod:
    """一个品种在某段日期里用哪个主力合约。manual=True 表示来自「手动修正」页。"""

    product: str
    symbol: str
    start: date
    end: date
    manual: bool = False


@dataclass
class RolloverRun:
    """分段回测跑完的汇总，供结果说明和导出使用。"""

    table_path: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


def resolve_main_table(
    cfg: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> Path | None:
    """
    找主力合约表。

    优先用调用方给的路径；否则先找 config/主力合约表.xlsx，再在项目根目录和 config 里
    找 *主力合约表*.xlsx（例如按年份命名的旧表），取最新的一个。
    打包后首次运行会把它拷到 exe 旁的 config 目录。
    """
    root = Path(str((cfg or {}).get("_project_root") or project_root()))
    if path:
        candidate = Path(str(path))
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate if candidate.exists() else None
    bases = (root, resource_root())
    for base in bases:
        configured = base / TABLE_NAME
        if configured.is_file():
            return configured
    for base in bases:
        for folder in (base / "config", base):
            if not folder.exists():
                continue
            found = sorted(
                (p for p in folder.glob(TABLE_GLOB) if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if found:
                return found[0]
    return None


def _today() -> date:
    """回测「到最新」用的日历日。单独写成函数，方便测试把今天固定住。"""
    return date.today()


def _as_date(value: Any) -> date | None:
    """Excel 里的日期单元格可能是 Timestamp、字符串或空值。"""
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


def _read_table_frame(path: Path) -> pd.DataFrame:
    """读「主力时段」页；页名变了就用第一页。表头上面通常有几行标题，这里按内容找表头。"""
    try:
        raw = pd.read_excel(path, sheet_name=SHEET_NAME, header=None)
    except ValueError:
        raw = pd.read_excel(path, sheet_name=0, header=None)
    return raw


def _find_header(raw: pd.DataFrame) -> tuple[int, dict[str, int]]:
    """找到写着「品种」「主力合约」的那一行，返回行号和列位置。"""
    for i in range(min(12, len(raw))):
        names = [_as_text(v) for v in raw.iloc[i].tolist()]
        if "品种" in names and "主力合约" in names:
            return i, {name: idx for idx, name in enumerate(names) if name}
    raise ValueError("主力合约表里找不到「品种」「主力合约」表头")


def _apply_manual(
    auto: dict[str, list[MainPeriod]],
    manual: list[ManualRow],
) -> dict[str, list[MainPeriod]]:
    """
    手动段覆盖同品种同区间的自动段。

    自动段被手动段截掉重叠部分，剩下的片段保留；这样只改一段时不用把整个品种都重写。
    手动段末尾不足一天也算一天；日期范围按自然日，回测时再按交易日筛K线。
    """
    if not manual:
        return auto
    cuts: dict[str, list[ManualRow]] = {}
    for row in manual:
        cuts.setdefault(row.product, []).append(row)

    out: dict[str, list[MainPeriod]] = {}
    for product, periods in auto.items():
        for period in periods:
            pieces = [(period.start, period.end)]
            for cut in cuts.get(product) or []:
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
                if b >= a:
                    out.setdefault(product, []).append(replace(period, start=a, end=b))
    for row in manual:
        out.setdefault(row.product, []).append(
            MainPeriod(row.product, row.symbol, row.start, row.end, True)
        )
    for periods in out.values():
        periods.sort(key=lambda p: p.start)
    return out


def load_main_periods(path: str | Path) -> dict[str, list[MainPeriod]]:
    """
    读主力合约表，按品种分组返回时段。

    「手动修正」页有行的品种，同区间的自动段被手动段覆盖。
    品种统一用去掉数字的小写字母，例如 TA2605 → ta，和配置里的合约写法无关。
    """
    raw = _read_table_frame(Path(path))
    header_row, columns = _find_header(raw)
    need = ("品种", "主力合约", "开始日期", "结束日期")
    for name in need:
        if name not in columns:
            raise ValueError(f"主力合约表缺少「{name}」列")
    out: dict[str, list[MainPeriod]] = {}
    for i in range(header_row + 1, len(raw)):
        row = raw.iloc[i]
        product = product_key(_as_text(row.iloc[columns["品种"]]))
        symbol = _as_text(row.iloc[columns["主力合约"]])
        start = _as_date(row.iloc[columns["开始日期"]])
        end = _as_date(row.iloc[columns["结束日期"]])
        if not product or not symbol or start is None or end is None or end < start:
            continue
        out.setdefault(product, []).append(MainPeriod(product, symbol, start, end))
    for periods in out.values():
        periods.sort(key=lambda p: p.start)
    try:
        manual = read_manual_rows(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("「手动修正」页读不出来，本次只按自动段: {}", e)
        manual = []
    return _apply_manual(out, manual)


def keep_old_contract_on_rollover_day(periods: list[MainPeriod]) -> list[MainPeriod]:
    """
    换月日当天仍用旧合约，下一交易日起才切到新合约。

    主力合约表把新合约的「开始日期」记在换月当天，旧合约的「结束日期」记在前一天。
    直接按表分段，旧仓会在换月前一天收盘就被强平，比实际早一天。这里只把
    「相邻且确实换了合约」的边界挪一天：旧段多做换月日，新段从换月日的下一天开始。
    相邻两段之间常夹着周末或节假日（自然日会空几天），这种空档仍算同一个换月边界；
    超过 15 天才当作表里本来就不连续，不动。同合约被手动拆成两段也不动。
    """
    ordered = sorted(periods, key=lambda p: p.start)
    out: list[MainPeriod] = []
    for i, period in enumerate(ordered):
        start, end = period.start, period.end
        if i > 0:
            prev = ordered[i - 1]
            gap = (period.start - prev.end).days
            if prev.symbol != period.symbol and 1 <= gap <= MAX_ROLLOVER_GAP_DAYS:
                start = period.start + timedelta(days=1)
        if i + 1 < len(ordered):
            nxt = ordered[i + 1]
            gap = (nxt.start - period.end).days
            if period.symbol != nxt.symbol and 1 <= gap <= MAX_ROLLOVER_GAP_DAYS:
                end = nxt.start
        if end < start:
            continue
        out.append(replace(period, start=start, end=end))
    return out


def clip_periods(
    periods: list[MainPeriod],
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[tuple[MainPeriod, str, str]]:
    """把时段裁到用户选的起止日期；不重叠的段直接丢掉。"""
    req_start = _as_date(start_date)
    req_end = _as_date(end_date)
    out: list[tuple[MainPeriod, str, str]] = []
    for period in periods:
        seg_start = max(period.start, req_start) if req_start else period.start
        seg_end = min(period.end, req_end) if req_end else period.end
        if seg_end < seg_start:
            continue
        out.append((period, seg_start.isoformat(), seg_end.isoformat()))
    return out


# 换月时旧主力段末仍持仓，引擎会先按「回测结束强平」记账；
# 拼完整条连续后，中间段改写成这句，最后一段才保留「回测结束强平」。
ROLLOVER_FLAT_REASON = "换月平仓：不接仓，新主力按策略重新开"


def _is_segment_end_flatten(reason: str) -> bool:
    """这一笔是不是「这段合约测完了、仓还在」：换月或交割都算。"""
    text = str(reason or "")
    return (
        "回测结束强平" in text
        or "交割月" in text
        or "换月平仓" in text
        or "换月移仓：平掉旧主力" in text
    )


def _relabel_rollover_exits(trades: list[TradeRecord]) -> None:
    """
    中间段的「回测结束强平」改成换月平仓。

    原理：每一段回测自己收尾，引擎不知道后面还有新主力。后面还有成交，
    就说明这不是整个回测结束，只是换合约。最后一笔如果还是强平，才是测到头了。
    新主力不再沿用上一手方向，所以这里只改平仓说明，不去改后面的开仓。
    """
    for trade in trades[:-1]:
        if _is_segment_end_flatten(trade.reason_exit):
            trade.reason_exit = ROLLOVER_FLAT_REASON


def _fallback(
    cfg: dict[str, Any],
    symbol: str,
    note: str,
    *,
    initial_capital: float | None,
    volume: int | None,
    warmup: int,
    start_date: str | None,
    end_date: str | None,
    engine: str | None,
    chain_capital: bool = False,
    source: str = "backtest",
    run_id: str | None = None,
) -> BacktestResult:
    """表里没有可用时段时，仍按原来的单合约方式回测，只补一句说明。"""
    result = run_backtest(
        cfg,
        symbol=symbol,
        initial_capital=initial_capital,
        volume=volume,
        warmup=warmup,
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        chain_capital=bool(chain_capital),
        source=source,
        run_id=run_id,
    )
    snap = dict(result.params_snapshot or {})
    snap["rollover"] = False
    snap["rollover_note"] = note
    result.params_snapshot = snap
    return result


def run_backtest_rollover(
    cfg: dict[str, Any],
    *,
    symbol: str | None = None,
    initial_capital: float | None = None,
    volume: int | None = None,
    warmup: int = 40,
    start_date: str | None = None,
    end_date: str | None = None,
    engine: str | None = None,
    table_path: str | Path | None = None,
    chain_capital: bool = False,
    source: str = "backtest",
    run_id: str | None = None,
) -> BacktestResult:
    """
    按主力合约时段分段回测，换月时自动切换合约。

    参数与 run_backtest 一致。找不到表、表里没有这个品种、或所选区间和表不重叠时，
    自动退回单合约回测，并在 params_snapshot 里写明原因。

    chain_capital=False（默认，真实账户口径关）：每段用同一份起始资金，段内每一笔
    也按这份资金算手数，可直接和单合约、和当时实盘对照；各段盈亏累计平移成一条净值曲线。
    chain_capital=True（真实账户口径开）：上一段剩余权益接到下一段，段内手数也跟着
    剩余权益走；前面亏多了后面可能开不了仓。
    换月只换合约：旧仓段末平掉，新主力空仓等策略信号，不把上一手的多空搬过去。
    """
    st = (cfg.get("strategy") or {}) if isinstance(cfg, dict) else {}
    trade = (cfg.get("trade") or {}) if isinstance(cfg, dict) else {}
    symbol = symbol or (st.get("symbols") or [trade.get("symbol", "bu2612")])[0]
    product = product_key(symbol)
    started_perf = time.perf_counter()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    common = dict(
        initial_capital=initial_capital,
        volume=volume,
        warmup=warmup,
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        chain_capital=bool(chain_capital),
        source=source,
        run_id=run_id,
    )

    table = resolve_main_table(cfg, table_path)
    if table is None:
        note = "没找到主力合约表（*主力合约表*.xlsx），这次按原合约回测。"
        logger.warning(note)
        return _fallback(cfg, symbol, note, **common)
    try:
        periods_by_product = load_main_periods(table)
    except Exception as e:  # noqa: BLE001
        note = f"主力合约表读不出来（{e}），这次按原合约回测。"
        logger.warning(note)
        return _fallback(cfg, symbol, note, **common)

    periods = periods_by_product.get(product) or []
    if not periods:
        note = f"主力合约表里没有 {product} 的时段，这次按原合约回测。"
        logger.warning(note)
        return _fallback(cfg, symbol, note, **common)

    # 换月日当天仍用旧合约：旧段多做一天，新段从次日开始，避免提前平掉旧仓。
    periods = keep_old_contract_on_rollover_day(periods)
    # 任何合约都不把交易留进自己的交割月；多出来的日子交给下一手。
    periods = apply_delivery_cutoff(periods)
    segments = clip_periods(periods, start_date, end_date)
    if not segments:
        note = "所选区间与主力合约表的时段不重叠，这次按原合约回测。"
        logger.warning(note)
        return _fallback(cfg, symbol, note, **common)

    # 主力合约表一般只更新到上一个交易日；用户把结束日选到今天时，最后一段会短几天。
    # 这几天仍按最后已知的主力合约顺延，否则会静默漏测——表现为「只开关了换月，
    # 11月3日那笔就没了」。表过期时结果里会提示去更新表。
    tail_note = ""
    req_end = _as_date(end_date)
    # 结束日空着 = 测到今天。主力表常常只更新到上一个交易日，不顺延就会少后面几天。
    empty_end = req_end is None
    if empty_end:
        req_end = _today()
    if req_end is not None:
        last_period, seg_start, seg_end = segments[-1]
        covered = _as_date(seg_end)
        if covered is not None and covered < req_end:
            # 顺延不得进入交割月：旧合约停在交割月前一天，余下日子改用下一手。
            stretched, tail_note = extend_last_before_delivery(
                [replace(last_period, start=_as_date(seg_start) or last_period.start, end=covered)],
                req_end,
            )
            rebuilt: list[tuple[MainPeriod, str, str]] = [
                (row, row.start.isoformat(), row.end.isoformat()) for row in stretched
            ]
            segments = segments[:-1] + rebuilt
            if empty_end and tail_note:
                tail_note = "结束日空着，按今天测到最新。" + tail_note

    run = RolloverRun(table_path=str(table))
    trades: list[TradeRecord] = []
    equity: list[float] = []
    equity_times: list[str] = []
    total_bars = 0
    lasts: list[BacktestResult] = []
    # 各段里「有信号但没开成」的记录也接在一起，换月回测同样能看到为什么没做。
    rejected: list[Any] = []
    ambiguities: list[dict[str, Any]] = []
    # 每段默认用同一份起始资金：每一段的手数都能直接和单合约回测对照，
    # 不会因为前面几段亏损导致后面的段开不了仓。
    start_capital = float(
        initial_capital
        if initial_capital is not None
        else ((cfg.get("risk") or {}).get("capital", 1_000_000) or 1_000_000)
    )
    # 真实账户口径时，上一段结束的权益就是下一段的起点。
    capital = start_capital
    # 各段盈亏仍累计到同一条净值曲线：段间只做平移，不影响段内手数。
    equity_offset = 0.0
    for period, seg_start, seg_end in segments:
        try:
            # 新主力从空仓开始。不把上一手方向搬过去：远近月价差会让止损立刻被扫。
            result = run_backtest(
                cfg,
                symbol=period.symbol,
                initial_capital=capital,
                volume=volume,
                warmup=warmup,
                start_date=seg_start,
                end_date=seg_end,
                engine=engine,
                chain_capital=bool(chain_capital),
                source=source,
            )
        except Exception as e:  # noqa: BLE001
            reason = str(e).strip() or type(e).__name__
            logger.warning(
                "主力分段没跑成 {} {}~{}: {}", period.symbol, seg_start, seg_end, reason
            )
            run.skipped.append(
                {
                    "symbol": period.symbol,
                    "start": seg_start,
                    "end": seg_end,
                    "reason": reason[:120],
                }
            )
            continue
        lasts.append(result)
        total_bars += int(result.bars)
        trades.extend(result.trades)
        rejected.extend(result.rejected)
        ambiguities.extend(result.ambiguities)
        if result.equity:
            # 固定资金口径：段内从同一份起始资金起算，拼总曲线时把本段整体平移到
            # 上一段的落点；真实账户口径：段内起点已是上段落点，直接接上。
            # 两种口径衔接处都不重复记一笔，总盈亏 = 各段盈亏之和。
            seg_equity = result.equity
            seg_times = list(getattr(result, "equity_times", []) or [])
            if chain_capital:
                equity.extend(seg_equity[1:] if equity else seg_equity)
                if seg_times:
                    equity_times.extend(seg_times[1:] if equity_times else seg_times)
                capital = float(seg_equity[-1])
            else:
                shifted = [e + equity_offset for e in seg_equity]
                equity.extend(shifted[1:] if equity else shifted)
                if seg_times:
                    equity_times.extend(seg_times[1:] if equity_times else seg_times)
                equity_offset += float(seg_equity[-1]) - float(seg_equity[0])
        perf = result.performance
        run.segments.append(
            {
                "symbol": period.symbol,
                "start": seg_start,
                "end": seg_end,
                "bars": int(result.bars),
                "trades": int(perf.trades),
                "pnl": round(float(perf.net_pnl), 2),
                "win_rate": round(float(perf.win_rate), 4),
                "manual": bool(period.manual),
            }
        )

    if not lasts:
        note = "主力分段的行情都拿不到（已到期合约的分钟线通常只剩最近一段），这次按原合约回测。"
        logger.warning(note)
        return _fallback(cfg, symbol, note, **common)

    # 先改平仓原因再算绩效：只改文字，盈亏和曲线不变。
    _relabel_rollover_exits(trades)
    min_trades = 3 if len(trades) >= 3 else 2
    perf = calc_performance(trades, equity, min_trades=min_trades)
    snap = copy.deepcopy(lasts[-1].params_snapshot or {})
    mult = PositionSizer(cfg).multiplier_for(product)
    skipped_note = ""
    if run.skipped:
        skipped_note = f"其中 {len(run.skipped)} 段没跑成（该段行情拿不到或K线不足），已跳过。"
    manual_note = ""
    manual_count = sum(1 for row in run.segments if row.get("manual"))
    if manual_count:
        manual_note = f"其中 {manual_count} 段来自「手动修正」页。"
    run.note = skipped_note
    snap.update(
        {
            "symbol": f"{product}主力连续",
            "rollover": True,
            "rollover_chain": bool(chain_capital),
            "chain_capital": bool(chain_capital),
            "rollover_table": str(table),
            "rollover_segments": run.segments,
            "rollover_skipped": run.skipped,
            "rollover_manual": manual_count,
            "rollover_note": (tail_note + manual_note + skipped_note).strip(),
            "rollover_carry": False,
            # 外层写用户选的起止。不要用最后一段自己的日期，否则结束日空着
            # 也会显示成主力表最后一天（例如 10-30），看起来像没测到最新。
            "start_date": start_date or "",
            "end_date": end_date or "",
            "bars": total_bars,
            "data_from": min((r.params_snapshot.get("data_from", "") for r in lasts), default=""),
            "data_to": max((r.params_snapshot.get("data_to", "") for r in lasts), default=""),
            "available_from": min(
                (r.params_snapshot.get("available_from", "") for r in lasts), default=""
            ),
            "available_to": max(
                (r.params_snapshot.get("available_to", "") for r in lasts), default=""
            ),
            # 手续费、保证金都按同一个品种算；这里按全部成交重算峰值。
            "margin_used": peak_margin(trades, mult),
            "ambiguity_count": len(ambiguities),
            "ambiguity_pnl_impact": round(
                sum(float(row.get("pnl_impact_vs_optimistic") or 0) for row in ambiguities), 2
            ),
            "ambiguity_note": (
                "各换月分段合并；同根冲突取更差候选，需细粒度复核。" if ambiguities else ""
            ),
        }
    )
    # 各段的「没开成原因」合并，方便看出是不是某一类限制一直在拦。
    merged_skips: dict[str, int] = {}
    for key, count in (snap.get("skipped_opens") or {}).items():
        merged_skips[key] = int(count or 0)
    for result in lasts[:-1]:
        for key, count in (result.params_snapshot.get("skipped_opens") or {}).items():
            merged_skips[key] = merged_skips.get(key, 0) + int(count or 0)
    snap["skipped_opens"] = merged_skips

    logger.info(
        "主力换月回测结束 {} | {}段 成交{}笔 胜率{:.0%} 净盈亏{:.0f}",
        f"{product}主力连续",
        len(run.segments),
        perf.trades,
        perf.win_rate,
        perf.net_pnl,
    )
    details = {
        "symbol": f"{product}主力连续",
        "engine": snap.get("engine", ""),
        "timeframe": lasts[-1].timeframe,
        "trail_timeframe": lasts[-1].trail_timeframe,
        "data_source": snap.get("data_source", ""),
        "start_date": start_date or "",
        "end_date": end_date or "",
        "data_from": snap.get("data_from", ""),
        "data_to": snap.get("data_to", ""),
        "random_seed": (cfg.get("backtest") or {}).get("seed", "not_used"),
        "research_dataset": research_dataset_reference(cfg),
        "execution_ambiguity": {
            "count": len(ambiguities),
            "pnl_impact_vs_optimistic": snap["ambiguity_pnl_impact"],
        },
        "main_contract_table": fingerprint_file(table),
        "costs": {
            "slippage_ticks": snap.get("slippage_ticks"),
            "price_tick": snap.get("price_tick"),
            "fill_note": snap.get("fill_note", ""),
        },
    }
    experiment = new_experiment("backtest_rollover", cfg, details=details)
    experiment["components"] = [
        {
            "experiment_id": (item.experiment or {}).get("experiment_id", ""),
            "symbol": item.symbol,
            "config_sha256": (item.experiment.get("config") or {}).get("sha256", "")
            if item.experiment
            else "",
            "details": (item.experiment or {}).get("details") or {},
        }
        for item in lasts
    ]
    experiment_data: dict[str, Any] = {}
    for index, item in enumerate(lasts, 1):
        for name, frame in (item._experiment_data or {}).items():
            experiment_data[f"segment_{index:03d}_{name}"] = frame
    from src.research.kernel import build_run_manifest

    manifest = build_run_manifest(
        source,
        cfg,
        symbols=[f"{product}主力连续"],
        period=lasts[-1].timeframe,
        started_at=started_at,
        duration_ms=(time.perf_counter() - started_perf) * 1000.0,
        run_id=run_id,
        metrics={
            "bars": int(total_bars),
            "trades": int(perf.trades),
            "net_pnl": float(perf.net_pnl),
            "segments": len(run.segments),
        },
        extra={"rollover": True},
    )
    result = BacktestResult(
        symbol=f"{product}主力连续",
        timeframe=lasts[-1].timeframe,
        trail_timeframe=lasts[-1].trail_timeframe,
        bars=total_bars,
        performance=perf,
        trades=trades,
        equity=equity,
        equity_times=equity_times,
        params_snapshot=snap,
        rejected=rejected,
        ambiguities=ambiguities,
        ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        experiment=experiment,
        run=manifest,
        _experiment_data=experiment_data,
    )
    th = EvaluationThresholds.from_cfg(cfg)
    if th.enabled:
        result.evaluation = evaluate_backtest(result, thresholds=th)
    return result


def format_rollover_lines(snap: dict[str, Any] | None) -> str:
    """结果区用的换月说明。没换月时返回空串。"""
    snap = snap or {}
    note = str(snap.get("rollover_note") or "").strip()
    if not snap.get("rollover"):
        return note
    segments = snap.get("rollover_segments") or []
    mode = "真实账户口径，资金连续" if snap.get("rollover_chain") else "每段固定起始资金"
    lines = [
        f"主力切换  按主力时段换月，共 {len(segments)} 段（{mode}；"
        "旧仓段末平掉，新主力空仓按策略重新开；交割月不再交易旧合约）"
    ]
    for row in segments[:MAX_LISTED_SEGMENTS]:
        mark = "（手动）" if row.get("manual") else ""
        lines.append(
            f"  {row.get('symbol', '')}{mark}  {row.get('start', '')}~{row.get('end', '')}  "
            f"{row.get('bars', 0)}根 {row.get('trades', 0)}笔 {row.get('pnl', 0):+.0f}元"
        )
    if len(segments) > MAX_LISTED_SEGMENTS:
        lines.append(f"  …另 {len(segments) - MAX_LISTED_SEGMENTS} 段")
    if note:
        lines.append(f"  {note}")
    return "\n".join(lines)
