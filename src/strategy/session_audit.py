"""
盘前行情适配审计（pre_session_audit）

功能：对关注列表里的每个合约 × 每套策略，跑一次该策略自己的行情过滤，
     输出一份"今日谁适配、谁不适配"的清单，打印到日志。

触发时机：BotWorker 启动时 + 每天 08:55 / 13:25 / 20:25（盘前 10 分钟）。

用法（单次直接调）：
    from src.strategy.session_audit import run_pre_session_audit, format_audit_report
    results = run_pre_session_audit(cfg)
    for line in format_audit_report(results):
        logger.info(line)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

import pandas as pd

from .cta_filter import CtaFilterParams, evaluate_cta_filter
from .market_state import (
    ENGINE_TREND_SECTION,
    MarketFilterParams,
    MarketFilterMode,
    MarketState,
    evaluate_market_state,
    range_params_for_daily_body,
    trend_params_for_section,
)

# 加权打分走自己小节里的行情闸门，不看公共 market_filter。
CTA_OWN_FILTER = {
    "multi_factor",
    "cta",
}


# 每个策略引擎的"预期过滤模式"：
# - daily_body 日内实体 → range（写在策略自己里）
# - 海龟 / SuperTrend / 双均线 / 布林 / SAR / 长线 → trend（各自 filter_*）
# - 多因子 → 自己的 cta_filter
# - swing_box → 箱子和 ADX 写在策略里
ENGINE_AUDIT_MODES: dict[str, MarketFilterMode | None] = {
    "daily_body": "range",
    "float_hold": None,  # 过滤默认关；方向交给方向选择器，默认开盘箱体 + 跳空顺向
    "trend_follow": "trend",
    "supertrend": "trend",
    "st": "trend",          # 别名
    "dual_ma": "trend",
    "ma_cross": "trend",    # 别名
    "ema_swing": None,  # 只用均线斜率，不用公共 ADX 过滤
    "macd_swing": None,
    "bollinger": "trend",
    "boll": "trend",        # 别名
    "sar_trend": "trend",
    "parabolic_sar": "trend",  # 别名
    "multi_factor": "trend",
    "cta": "trend",         # 别名
    "swing_trend": "trend",
    "swing_box": None,  # 箱子和 ADX 写在策略里
}

# 策略中文名（和 ENGINE_LABELS 一致，这里单独放一份避免循环 import）
ENGINE_NAMES: dict[str, str] = {
    "daily_body": "日内实体",
    "float_hold": "日内浮盈飘单",
    "trend_follow": "趋势跟随",
    "supertrend": "SuperTrend",
    "st": "SuperTrend",
    "dual_ma": "双均线",
    "ma_cross": "双均线",
    "ema_swing": "均线波段",
    "macd_swing": "均线波段",
    "bollinger": "布林突破",
    "boll": "布林突破",
    "sar_trend": "SAR波段",
    "parabolic_sar": "SAR波段",
    "multi_factor": "多因子CTA（加权打分）",
    "cta": "多因子CTA（加权打分）",
    "swing_trend": "长线加减仓",
    "swing_box": "长线箱体突破",
}


@dataclass
class SymbolAuditRow:
    symbol: str
    engine: str
    engine_name: str
    expected_mode: MarketFilterMode | None
    state: MarketState | None  # None = 跳过（未接入过滤 / 无数据）
    ok: bool | None  # True=行情适配；False=不适配；None=跳过
    note: str  # 一句话解释


# —— 数据拉取接口 ——
# run_pre_session_audit 接受一个可选的 bars_provider(symbol, timeframe, need_bars)
# 调用者可以传入任何能返回 pd.DataFrame(high/low/close) 的函数。
# 不传时用 make_bars_provider()，和 runner 挂机用的是同一个 KlineProvider。

def make_bars_provider(
    cfg: dict[str, Any],
    provider: Any | None = None,
) -> Callable[[str, str, int], pd.DataFrame | None]:
    """拉 K 线并在同一轮内缓存，避免同一品种被多套策略重复请求。

    provider 传入已有的 KlineProvider（复用 runner 的单例，省掉重复建表/开库）；
    不传时按 cfg 新建一个。
    返回的函数签名：(symbol, timeframe, need_bars) -> DataFrame | None。
    任何异常都返回 None，调用方按"K线不足"处理，不中断审计。
    """
    if provider is None:
        from src.data import KlineProvider

        provider = KlineProvider(cfg or {})
    cache: dict[tuple[str, str], pd.DataFrame | None] = {}

    def _fetch(symbol: str, timeframe: str, need_bars: int) -> pd.DataFrame | None:
        key = (str(symbol), str(timeframe))
        if key not in cache:
            try:
                cache[key] = provider.fetch(symbol, period=str(timeframe))
            except Exception:  # noqa: BLE001
                cache[key] = None
        df = cache[key]
        if df is None or len(df) < need_bars:
            return None
        return df

    return _fetch


def run_pre_session_audit(
    cfg: dict[str, Any],
    bars_provider: Callable[[str, str, int], pd.DataFrame | None] | None = None,
    *,
    engines: Iterable[str] | None = None,
    timeframe: str | None = None,
    include_aliases: bool = False,
) -> list[SymbolAuditRow]:
    """盘前扫描：所有关注品种 × 所有接了 market_filter 的策略。

    参数
    ----
    cfg : 完整配置 dict（和 BotWorker 用的那份一样）
    bars_provider : 可选，K 线拉取函数，返回 DataFrame(high/low/close) 或 None
    engines : 可选，只扫指定引擎；None=扫全部接入了过滤的引擎（不含别名）
    timeframe : 可选，强制用什么周期；None=用 cfg.strategy.timeframe（默认 15）
    include_aliases : True=把 st/ma_cross/boll 等旧别名也扫一遍；False=只扫主推名

    返回
    ----
    list[SymbolAuditRow]，可以直接喂给 format_audit_report 打印。
    """
    bars_fn = bars_provider or make_bars_provider(cfg)
    st = (cfg or {}).get("strategy") or {}
    symbols = list(st.get("symbols") or [])
    tf = str(timeframe or st.get("timeframe") or "15")

    # 主推引擎名（不含别名）
    PRIMARY = {
        "daily_body",
        "float_hold",
        "trend_follow",
        "supertrend",
        "dual_ma",
        "ema_swing",
        "bollinger",
        "sar_trend",
        "multi_factor",
        "swing_trend",
        "swing_box",
    }
    all_engines = list(engines) if engines is not None else list(
        ENGINE_AUDIT_MODES.keys() if include_aliases else PRIMARY
    )

    rows: list[SymbolAuditRow] = []
    for symbol in symbols:
        for eng in all_engines:
            expected_mode = ENGINE_AUDIT_MODES.get(eng)
            eng_name = ENGINE_NAMES.get(eng, eng)

            if expected_mode is None:
                if eng == "swing_box":
                    skip_note = "日线箱子和 ADX 写在策略里，不走公共行情过滤"
                elif eng == "float_hold":
                    skip_note = "行情过滤默认关，方向靠观察窗口和跳空顺向"
                else:
                    skip_note = "未接入行情过滤"
                rows.append(
                    SymbolAuditRow(
                        symbol=symbol,
                        engine=eng,
                        engine_name=eng_name,
                        expected_mode=None,
                        state=None,
                        ok=None,
                        note=skip_note,
                    )
                )
                continue

            if eng in CTA_OWN_FILTER:
                st = cfg.get("strategy") or {}
                kind = {
                    "cta": "multi_factor",
                }.get(eng, eng)
                box = st.get(kind) if isinstance(st.get(kind), dict) else {}
                cta_params = CtaFilterParams.from_box(box, kind=kind)
                if not cta_params.enabled:
                    rows.append(
                        SymbolAuditRow(
                            symbol=symbol,
                            engine=eng,
                            engine_name=eng_name,
                            expected_mode=expected_mode,
                            state=None,
                            ok=True,
                            note="本策略行情过滤已关闭（不走公共过滤器）",
                        )
                    )
                    continue
                need = max(cta_params.need_bars, cta_params.adx_period + 5)
                bars = bars_fn(symbol, tf, need)
                if bars is None or len(bars) < need:
                    rows.append(
                        SymbolAuditRow(
                            symbol=symbol,
                            engine=eng,
                            engine_name=eng_name,
                            expected_mode=expected_mode,
                            state=None,
                            ok=None,
                            note=f"K线不足（需 {need} 根，拉到 {0 if bars is None else len(bars)}）",
                        )
                    )
                    continue
                gate = evaluate_cta_filter(bars, cta_params)
                rows.append(
                    SymbolAuditRow(
                        symbol=symbol,
                        engine=eng,
                        engine_name=eng_name,
                        expected_mode=expected_mode,
                        state=None,
                        ok=gate.ok,
                        note=gate.reason if not gate.ok else f"[多因子] {gate.describe()}",
                    )
                )
                continue

            # 日内实体的箱体突破过滤写在策略里；趋势策略看各自小节。
            if eng == "daily_body":
                params = range_params_for_daily_body(cfg)
            else:
                section = ENGINE_TREND_SECTION.get(eng)
                if section:
                    params = trend_params_for_section(cfg, section)
                else:
                    params = MarketFilterParams(enabled=False, mode=expected_mode or "trend")
            if not params.enabled:
                rows.append(
                    SymbolAuditRow(
                        symbol=symbol,
                        engine=eng,
                        engine_name=eng_name,
                        expected_mode=expected_mode,
                        state=None,
                        ok=True,
                        note="本策略行情过滤已关闭",
                    )
                )
                continue
            bars = bars_fn(symbol, tf, params.need_bars)
            if bars is None or len(bars) < params.need_bars:
                rows.append(
                    SymbolAuditRow(
                        symbol=symbol,
                        engine=eng,
                        engine_name=eng_name,
                        expected_mode=expected_mode,
                        state=None,
                        ok=None,
                        note=f"K线不足（需 {params.need_bars} 根，拉到 {0 if bars is None else len(bars)}）",
                    )
                )
                continue

            state = evaluate_market_state(bars, params)
            # 构造一句话 note
            if state.ok:
                note = _note_ok(state, expected_mode)
            else:
                note = state.reason  # evaluate 里已经拼好"…，停手不开新仓"
            rows.append(
                SymbolAuditRow(
                    symbol=symbol,
                    engine=eng,
                    engine_name=eng_name,
                    expected_mode=expected_mode,
                    state=state,
                    ok=state.ok,
                    note=note,
                )
            )

    return rows


def _note_ok(state: MarketState, mode: MarketFilterMode) -> str:
    """ok=True 时拼一句人类可读的说明。"""
    mood = "趋势" if state.adx >= 25 else ("偏弱" if state.adx >= 20 else "震荡")
    if mode == "range":
        tag = "区间"
    else:
        tag = "趋势"
    return (
        f"[{tag}] 行情适配：ADX={state.adx:.1f}({mood}) "
        f"ATR分位={state.atr_rank:.0%}"
    )


def format_audit_report(
    rows: list[SymbolAuditRow],
    *,
    now: datetime | None = None,
) -> list[str]:
    """把 run_pre_session_audit 的结果格式化成日志行列表。"""
    now = now or datetime.now()
    out: list[str] = []
    out.append(
        f"=== 盘前行情适配审计 ({now.strftime('%Y-%m-%d %H:%M')}) ==="
    )
    out.append("")

    # 按 symbol 分组
    by_symbol: dict[str, list[SymbolAuditRow]] = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, []).append(r)

    if not by_symbol:
        out.append("（关注品种列表为空，config.yaml → strategy.symbols 未配置）")
        return out

    total_ok = 0
    total_block = 0
    total_skip = 0

    for sym, sym_rows in by_symbol.items():
        out.append(f"--- {sym} ---")
        for r in sym_rows:
            if r.ok is None:
                tag = "SKIP"
                total_skip += 1
            elif r.ok:
                tag = "OK  "
                total_ok += 1
            else:
                tag = "BLOCK"
                total_block += 1
            out.append(f"  [{tag}] {r.engine_name:<10} {r.note}")
        out.append("")

    out.append(
        f"汇总：{total_ok} 项适配 / {total_block} 项不适配 / {total_skip} 项跳过"
    )
    out.append("建议：BLOCK 多的合约今天少看，SKIP 多的检查行情源是否通畅。")
    return out


# —— 定时触发点（供 runner.py / BotWorker 调用）——

# 盘前 10 分钟触发的钟点（北京时间）
SESSION_AUDIT_TIMES: list[tuple[int, int]] = [
    (8, 55),   # 日盘开盘前 5 分钟
    (13, 25),  # 午盘开盘前 5 分钟
    (20, 25),  # 夜盘开盘前 5 分钟
]


def should_run_session_audit(now: datetime, last_run: datetime | None) -> bool:
    """判断 now 是否落在"应该触发盘前审计"的时间窗口里。

    规则：now 的 HH:MM 落在 SESSION_AUDIT_TIMES 某个时间点的前后 3 分钟内，
    且距离上次触发已经超过 1 小时（避免同一时段重复触发）。
    """
    for hh, mm in SESSION_AUDIT_TIMES:
        target_min = hh * 60 + mm
        now_min = now.hour * 60 + now.minute
        if abs(now_min - target_min) <= 3:
            if last_run is None or (now - last_run).total_seconds() > 3600:
                return True
    return False
