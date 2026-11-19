"""
交叉测试
功能：勾选的策略、合约、周期两两交叉，每组跑一遍历史回测，再标出这一轮里相对较好的组合。
      信号周期和止损周期用同一个。全品种主力连续只留着备查，这一轮不用那份。
原理：这是「策略×合约×周期」对照筛选，用的是配置里的固定参数，不是多品种交叉验证。
      要验优化参数稳不稳，请用策略优化页自动带上的其它关注品种验收，
      或交叉测试页的「用最近优化参数做多品种验收」。
      得分看盈利因子和净赚能不能盖住回撤，不看胜率。
      净赚、盈利因子大于 1、成交不少于 5 笔，并且得分排在这些组合前四分之一的，标成较好。
      这里只改回测用的副本，不改实盘配置，也不改开仓和止损规则。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.backtest.evaluation import verdict_text
from src.strategy.factory import ENGINE_ALIASES, cross_engine_choices

# 主力连续：代码末尾的 0 表示「当前主力」，例如 rb0 会到新浪去拉 RB0。
# 每一项是（代码，中文名，乘数，是否流动性偏少）。
# 乘数是「价格动 1 个点，1 手大约盈亏多少元」。回撤要折成钱才能横向比。
# 没写进配置的品种才用这里的乘数；配置里已经写过的，仍用配置里的。
# 最后一项为 True 的，是平时很难成交的品种。优选直接跳过，不下载、不回测、不进结果表。
# 期权和月均价合约不在这里。那些不是普通的主力连续。
MAIN_UNIVERSE: list[tuple[str, str, float, bool]] = [
    # 上期所
    ("cu0", "铜", 5, False),
    ("al0", "铝", 5, False),
    ("zn0", "锌", 5, False),
    ("pb0", "铅", 5, False),
    ("ni0", "镍", 1, False),
    ("sn0", "锡", 1, False),
    ("au0", "黄金", 1000, False),
    ("ag0", "白银", 15, False),
    ("rb0", "螺纹", 10, False),
    ("hc0", "热卷", 10, False),
    ("ss0", "不锈钢", 5, False),
    ("wr0", "线材", 10, True),
    ("fu0", "燃油", 10, False),
    ("bu0", "沥青", 10, False),
    ("ru0", "橡胶", 10, False),
    ("br0", "丁二烯橡胶", 5, False),
    ("sp0", "纸浆", 10, False),
    ("ao0", "氧化铝", 20, False),
    ("ad0", "铸造铝合金", 10, False),
    ("op0", "胶版印刷纸", 40, True),
    # 能源中心
    ("sc0", "原油", 1000, False),
    ("lu0", "低硫燃油", 10, False),
    ("nr0", "20号胶", 10, False),
    ("bc0", "国际铜", 5, False),
    ("ec0", "欧线集运", 50, False),
    # 大商所
    ("m0", "豆粕", 10, False),
    ("y0", "豆油", 10, False),
    ("a0", "豆一", 10, False),
    ("b0", "豆二", 10, True),
    ("p0", "棕榈", 10, False),
    ("c0", "玉米", 10, False),
    ("cs0", "淀粉", 10, False),
    ("jd0", "鸡蛋", 10, False),
    ("lh0", "生猪", 16, False),
    ("rr0", "粳米", 10, True),
    ("i0", "铁矿", 100, False),
    ("j0", "焦炭", 100, False),
    ("jm0", "焦煤", 60, False),
    ("l0", "塑料", 5, False),
    ("v0", "PVC", 5, False),
    ("pp0", "聚丙烯", 5, False),
    ("eg0", "乙二醇", 10, False),
    ("eb0", "苯乙烯", 5, False),
    ("pg0", "液化气", 20, False),
    ("bz0", "纯苯", 30, False),
    ("lg0", "原木", 90, False),
    ("fb0", "纤维板", 10, True),
    ("bb0", "胶合板", 500, True),
    # 郑商所
    ("ta0", "PTA", 5, False),
    ("ma0", "甲醇", 10, False),
    ("sa0", "纯碱", 20, False),
    ("fg0", "玻璃", 20, False),
    ("sr0", "白糖", 10, False),
    ("cf0", "棉花", 5, False),
    ("cy0", "棉纱", 5, True),
    ("oi0", "菜油", 10, False),
    ("rm0", "菜粕", 10, False),
    ("rs0", "菜籽", 10, True),
    ("ur0", "尿素", 20, False),
    ("pf0", "短纤", 5, False),
    ("px0", "对二甲苯", 5, False),
    ("sh0", "烧碱", 30, False),
    ("pr0", "瓶片", 15, False),
    ("pl0", "丙烯", 20, False),
    ("sf0", "硅铁", 5, False),
    ("sm0", "锰硅", 5, False),
    ("ap0", "苹果", 10, False),
    ("cj0", "红枣", 5, False),
    ("pk0", "花生", 5, False),
    ("zc0", "动力煤", 100, True),
    ("wh0", "强麦", 20, True),
    ("pm0", "普麦", 50, True),
    ("ri0", "早籼稻", 20, True),
    ("lr0", "晚籼稻", 20, True),
    ("jr0", "粳稻", 20, True),
    # 中金所。乘数是「指数或国债价格动 1 个点，1 手大约多少元」。
    ("if0", "沪深300", 300, False),
    ("ih0", "上证50", 300, False),
    ("ic0", "中证500", 200, False),
    ("im0", "中证1000", 200, False),
    ("t0", "十年国债", 10000, False),
    ("tf0", "五年国债", 10000, False),
    ("ts0", "二年国债", 20000, False),
    ("tl0", "三十年国债", 10000, False),
    # 广期所
    ("si0", "工业硅", 5, False),
    ("lc0", "碳酸锂", 1, False),
    ("ps0", "多晶硅", 3, False),
    ("pt0", "铂", 1000, False),
    ("pd0", "钯", 1000, False),
]

# 少于这么多笔，净盈亏和回撤都晃得太厉害，不进备选。
MIN_TRADES = 3
# 交叉测试里，少于这么多笔不参加「较好」比较。和打分时「不满 5 笔直接垫底」是同一条线。
MIN_COMPARE_TRADES = 5
# 以前交叉测试把本金改成十亿，回撤比例几乎变成 0，评估会假「通过」。
# 开几手仍固定；权益曲线和评估本金改回配置里的账户权益，和历史回测同一把尺子。
CROSS_COMPARE_CAPITAL = 1_000_000_000.0


def product_key(symbol: str) -> str:
    """从 rb0、bu2612 里取出品种字母。只认字母，避免把数字当成品种。"""
    return "".join(ch for ch in str(symbol).lower() if "a" <= ch <= "z")


def _contract(item: tuple) -> tuple[str, str, float, bool]:
    """
    拆开名单里的一项。

    旧的测试数据只有代码、名字、乘数三项。没有第四项时，不当成流动性差。
    """
    code = str(item[0])
    name = str(item[1])
    mult = float(item[2])
    thin = bool(item[3]) if len(item) > 3 else False
    return code, name, mult, thin


def _without_thin(universe: list[tuple]) -> tuple[list[tuple], int]:
    """
    把流动性偏少的合约从这一轮测试里拿掉。

    这些品种平时挂单少、买卖价差大，回测出来的胜率和回撤不能当成能下单的结果。
    跳过之后不再去拉 K 线，结果表里也不会留一行。
    旧名单只有代码、名字、乘数三项时，没有第四项，仍然参加测试。
    """
    kept: list[tuple] = []
    skipped = 0
    for item in universe:
        code, name, mult, thin = _contract(item)
        if thin:
            skipped += 1
            continue
        kept.append((code, name, mult, False))
    return kept, skipped


def liquidity_text(symbol: str) -> str:
    """
    这个品种平时好不好成交。

    对得上名单里标过「偏少」的，返回「偏少」。对得上但没标的，返回「正常」。
    名单里没有的具体月份，例如基准用的 bu2612，按品种字母去认。认不出就空着。
    """
    key = product_key(symbol)
    if not key:
        return "—"
    for item in MAIN_UNIVERSE:
        code, _name, _mult, thin = _contract(item)
        if product_key(code) == key:
            return "偏少" if thin else "正常"
    return "—"


def baseline_from_report(data: dict[str, Any]) -> dict[str, Any]:
    """
    从一次回测报告里取出当尺子用的数字。

    胜率、盈利因子、最大回撤用那次已经算好的结果，不再重跑基准品种。
    手数用报告里记下的固定手数，后面每个品种都按这个手数开。
    """
    perf = data.get("performance") or {}
    snap = data.get("params_snapshot") or {}
    volume = int(snap.get("fixed_volume") or 1)
    if volume < 1:
        volume = 1
    return {
        "symbol": str(data.get("symbol") or ""),
        "trades": int(perf.get("trades") or 0),
        "win_rate": float(perf.get("win_rate") or 0),
        "profit_factor": float(perf.get("profit_factor") or 0),
        "max_drawdown": float(perf.get("max_drawdown") or 0),
        "net_pnl": float(perf.get("net_pnl") or 0),
        "fixed_volume": volume,
        "sizing_mode": str(snap.get("sizing_mode") or ""),
        "engine": str(snap.get("engine") or ""),
        "timeframe": str(snap.get("timeframe") or ""),
        "trail_timeframe": str(snap.get("trail_timeframe") or ""),
        "start_date": str(snap.get("start_date") or ""),
        "end_date": str(snap.get("end_date") or ""),
        "data_from": str(snap.get("data_from") or ""),
        "data_to": str(snap.get("data_to") or ""),
        "bars": int(data.get("bars") or 0),
        "flatten_at_close": bool(snap.get("flatten_at_close", True)),
        # 空字符串表示这份旧结果没记下行情来源，界面再去看历史回测页的下拉框。
        "data_source": str(snap.get("data_source") or ""),
    }


def better_items(row: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """
    这一行比基准好在哪几项。

    净盈亏和盈利因子要更高。最大回撤要更小（同样手数下，亏得少才算更好）。
    胜率更高不算赢。打平也不算赢，避免一堆和基准一模一样的品种挤进备选。
    """
    won: list[str] = []
    if float(row.get("net_pnl") or 0) > float(baseline.get("net_pnl") or 0):
        won.append("净盈亏")
    if float(row.get("profit_factor") or 0) > float(baseline.get("profit_factor") or 0):
        won.append("盈利因子")
    if float(row.get("max_drawdown") or 0) < float(baseline.get("max_drawdown") or 0):
        won.append("最大回撤")
    return won


def is_candidate(row: dict[str, Any], baseline: dict[str, Any]) -> bool:
    """成交够 3 笔，并且三样指标里至少两样更好，才是备选。"""
    if row.get("error"):
        return False
    if int(row.get("trades") or 0) < MIN_TRADES:
        return False
    return len(better_items(row, baseline)) >= 2


def settings_from_baseline(baseline: dict[str, Any]) -> dict[str, Any]:
    """带基准时用的那一套：策略、周期、日期、手数都跟那次回测相同，并且不看保证金。"""
    return {
        "engine": str(baseline.get("engine") or ""),
        "timeframe": str(baseline.get("timeframe") or ""),
        "trail_timeframe": str(baseline.get("trail_timeframe") or ""),
        "start_date": str(baseline.get("start_date") or ""),
        "end_date": str(baseline.get("end_date") or ""),
        "flatten_at_close": bool(baseline.get("flatten_at_close", True)),
        "has_flatten": "flatten_at_close" in baseline,
        "fixed_volume": max(1, int(baseline.get("fixed_volume") or 1)),
        "fair_compare": True,
    }


def prepare_screen_cfg(
    cfg: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    *,
    settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """
    做出只给交叉测试用的配置副本。

    没另给 settings 时，跟基准那次回测走：同样手数，保证金清成 0，
    当天亏损和开仓次数也不再拦住，免得不同品种被风控提前停掉。
    传入 settings 且 fair_compare 为假时，改用交叉测试页自己填的仓位和限制。
    原配置不会被改。
    """
    from src.risk.sizing import bind_run_margin

    spec = settings if settings is not None else settings_from_baseline(baseline or {})
    out = copy.deepcopy(cfg)
    st = out.setdefault("strategy", {})
    if spec.get("engine"):
        st["engine"] = spec["engine"]
    if spec.get("timeframe"):
        st["timeframe"] = spec["timeframe"]
    if spec.get("trail_timeframe"):
        st["trail_timeframe"] = spec["trail_timeframe"]
    if spec.get("has_flatten", True):
        st["flatten_at_close"] = bool(spec.get("flatten_at_close", True))

    trade = out.setdefault("trade", {})
    risk = out.setdefault("risk", {})
    if spec.get("fair_compare", True):
        volume = max(1, int(spec.get("fixed_volume") or 1))
        trade["volume"] = volume
        risk["sizing_mode"] = "fixed"
        risk["apply_limits"] = True
        # 交叉测试按固定手数比；仓位靠固定手数，日亏损放宽到几乎不拦。
        risk.pop("max_position", None)
        risk["max_daily_loss"] = 1_000_000_000_000
        risk["max_orders_per_day"] = 100_000
        risk["min_order_interval_sec"] = 0
        _attach_universe_contracts(risk)
        bind_run_margin(out, 0)
        if spec.get("ignore_equity"):
            # 交叉测试不按权益算手数。保证金清掉，避免高保证金品种开不出固定手数。
            # 本金优先用交叉测试页填的数，没有才用配置里的账户权益。
            eval_capital = 0.0
            try:
                eval_capital = float(spec.get("capital") or risk.get("capital") or 0)
            except (TypeError, ValueError):
                eval_capital = 0.0
            for box in (risk.get("contracts") or {}).values():
                if isinstance(box, dict):
                    box["margin"] = 0
                    box["margin_rate_pct"] = 0
            if eval_capital > 0:
                risk["capital"] = eval_capital
            else:
                risk.pop("capital", None)
            # 开几手仍是持仓手数。每笔风险记下来，但不拿它按权益重算手数。
            if "risk_per_trade_pct" in spec:
                risk["risk_per_trade_pct"] = float(spec.get("risk_per_trade_pct") or 0)
            if "min_order_interval_sec" in spec:
                risk["min_order_interval_sec"] = max(0, int(spec.get("min_order_interval_sec") or 0))
            if "max_orders_per_day" in spec:
                risk["max_orders_per_day"] = max(1, int(spec.get("max_orders_per_day") or 1))
        return out, volume

    _attach_universe_contracts(risk)
    if not spec.get("apply_limits", True):
        trade["volume"] = 1
        risk["sizing_mode"] = "fixed"
        risk["apply_limits"] = False
        return out, 1
    volume = max(1, int(spec.get("fixed_volume") or 1))
    trade["volume"] = volume
    risk["apply_limits"] = True
    risk["sizing_mode"] = str(spec.get("sizing_mode") or "fixed")
    risk["capital"] = float(spec.get("capital") or risk.get("capital") or 0)
    risk["risk_per_trade_pct"] = float(spec.get("risk_per_trade_pct") or 0)
    risk["min_volume"] = max(1, int(spec.get("min_volume") or 1))
    risk.pop("max_position", None)
    risk["max_daily_loss"] = float(spec.get("max_daily_loss") or 0)
    risk["min_order_interval_sec"] = max(0, int(spec.get("min_order_interval_sec") or 0))
    risk["max_orders_per_day"] = max(1, int(spec.get("max_orders_per_day") or 1))
    bind_run_margin(out, float(spec.get("margin_per_lot") or 0))
    return out, volume


def _attach_universe_contracts(risk: dict[str, Any]) -> None:
    """没写过乘数的品种补上。已经写在配置里的乘数不动，避免把用户改过的数盖掉。"""
    contracts = risk.setdefault("contracts", {})
    for item in MAIN_UNIVERSE:
        code, name, mult, _thin = _contract(item)
        key = product_key(code)
        box = contracts.get(key)
        if not isinstance(box, dict):
            contracts[key] = {"multiplier": float(mult), "name": name, "margin": 0}
        else:
            box.setdefault("multiplier", float(mult))
            box.setdefault("name", name)


def listed_cross_engines() -> list[tuple[str, str]]:
    """交叉测试勾选名单：每次现问工厂，删掉的立刻没了，新加的会出现。"""
    return cross_engine_choices()

# 配置里可能写着旧的短名字。交叉测试只认左边这一套，避免同一套跑两遍。
_ENGINE_ALIAS = ENGINE_ALIASES


# 交叉测试可勾选的周期。信号和止损用同一个，不再分开。
CROSS_PERIODS: list[tuple[str, str]] = [
    ("5", "5分钟"),
    ("15", "15分钟"),
    ("30", "30分钟"),
    ("60", "60分钟"),
    ("D", "日线"),
    ("W", "周线"),
]


def engines_for_cross(cfg: dict[str, Any] | None) -> list[tuple[str, str]]:
    """
    这一轮要测的策略。

    读配置里的 cross_engines。没写或写空时，用当前程序能交叉测试的全部策略。
    写了哪些就测哪些，不再自动把没勾的补回来。已删名字和交叉测试不测的丢掉。
    """
    known = {code: label for code, label in listed_cross_engines()}
    raw = ((cfg or {}).get("strategy") or {}).get("cross_engines")
    if not isinstance(raw, list) or not raw:
        return list(listed_cross_engines())
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        name = _ENGINE_ALIAS.get(str(item).strip(), str(item).strip())
        if not name or name in seen or name not in known:
            continue
        seen.add(name)
        chosen.append((name, known[name]))
    return chosen or list(listed_cross_engines())


def engines_from_codes(codes: list[str] | None) -> list[tuple[str, str]]:
    """把勾选的引擎名收成（代号, 中文名）。不认识的丢掉。"""
    known = {code: label for code, label in listed_cross_engines()}
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in codes or []:
        name = _ENGINE_ALIAS.get(str(item).strip(), str(item).strip())
        if not name or name in seen or name not in known:
            continue
        seen.add(name)
        chosen.append((name, known[name]))
    return chosen


def normalize_cross_periods(raw: list[Any] | None) -> list[str]:
    """勾选的周期收成 5 / 15 / D 这种代码。信号和止损共用。"""
    from src.data.periods import canonical_period

    known = {code for code, _label in CROSS_PERIODS}
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        code = canonical_period(str(item or "").strip())
        if code not in known or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def sync_cross_engines_in_cfg(cfg: dict[str, Any], codes: list[str] | None = None) -> list[str]:
    """
    把勾选的策略写进 cfg['strategy']['cross_engines']。

    传入 codes 时按勾选写入；没传时只整理现有名单，空名单才填全部。
    """
    st = cfg.setdefault("strategy", {})
    if codes is not None:
        engines = engines_from_codes(codes)
    else:
        engines = engines_for_cross(cfg)
    names = [code for code, _label in engines]
    st["cross_engines"] = names
    return names


def contracts_for_cross(symbols: list[str] | None) -> list[tuple[str, str, float, bool]]:
    """
    把配置里的具体合约收成交叉测试要用的名单。

    例如 rb2701。中文名和乘数按品种字母去全品种名单里认。
    这些是你点名要测的，即使那个品种平时成交少，也不从这一轮里拿掉。
    """
    found: dict[str, tuple[str, float]] = {}
    for item in MAIN_UNIVERSE:
        code, name, mult, _thin = _contract(item)
        found[product_key(code)] = (name, mult)
    out: list[tuple[str, str, float, bool]] = []
    seen: set[str] = set()
    for raw in symbols or []:
        code = str(raw).strip()
        key = code.lower()
        if not code or code == "（还没有）" or key in seen:
            continue
        seen.add(key)
        name, mult = found.get(product_key(code), (code, 1.0))
        out.append((code, name, mult, False))
    return out


def reference_universe_text() -> str:
    """全部主力连续，只给人查，不参加交叉测试。"""
    lines = [
        "全品种备查",
        "这是能做主力连续的名单。交叉测试不跑这一份，只跑配置里的具体合约。",
        "一行是：中文名、主力代码、1手大约的乘数。最后写「偏少」的是平时很难成交的。",
        "",
    ]
    for item in MAIN_UNIVERSE:
        code, name, mult, thin = _contract(item)
        mark = "  偏少" if thin else ""
        lines.append(f"{name}  {code}  乘数{mult:g}{mark}")
    return "\n".join(lines) + "\n"


def _same_symbol(a: str, b: str) -> bool:
    """基准合约自己不再测一遍。bu2612 和 bu0 不是同一个合约，仍会测 bu0。"""
    return str(a).strip().lower() == str(b).strip().lower()


def screen_symbols(
    cfg: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    *,
    compare_baseline: bool = True,
    settings: dict[str, Any] | None = None,
    universe: list[tuple[str, str, float]] | None = None,
    runner: Callable[..., Any] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    按顺序回测主力连续合约。

    compare_baseline 为真：跟基准的净盈亏、盈利因子、最大回撤比，至少两项更好才入选。胜率不参加。
    为假：不跟基准比，用 settings 里的策略和仓位，按综合得分从高到低排名。
    runner 留给测试替换，正式使用时就是普通的单品种回测。
    某一个品种拉不到 K 线，记下来跳过，不影响后面的品种。
    """
    if runner is None:
        from src.backtest.engine import run_backtest

        runner = run_backtest

    # 偏少的合约整批不测。进度条的总数也只算真正会回测的那些。
    items, skipped_thin = _without_thin(list(universe or MAIN_UNIVERSE))
    ruler = dict(baseline or {})
    if compare_baseline:
        prepared, volume = prepare_screen_cfg(cfg, ruler)
        start = ruler.get("start_date") or None
        end = ruler.get("end_date") or None
        engine = ruler.get("engine") or None
        used = settings_from_baseline(ruler)
    else:
        used = dict(settings or {})
        used["fair_compare"] = False
        prepared, volume = prepare_screen_cfg(cfg, settings=used)
        start = used.get("start_date") or None
        end = used.get("end_date") or None
        engine = used.get("engine") or None
        ruler = {}
    rows: list[dict[str, Any]] = []
    stopped = False
    total = len(items)

    for index, item in enumerate(items, start=1):
        code, name, _mult, thin = _contract(item)
        if cancel is not None and cancel():
            stopped = True
            break
        if compare_baseline and _same_symbol(code, str((baseline or {}).get("symbol") or "")):
            continue
        if on_progress is not None:
            on_progress(index, total, f"{name} {code}")
        row: dict[str, Any] = {
            "symbol": code,
            "name": name,
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "payoff": 0.0,
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "net_pnl": 0.0,
            "error": "",
            # 能走到这里的都不是偏少品种。这一列留给基准行，或以后认不出的合约。
            "liquidity": "偏少" if thin else "正常",
        }
        try:
            # 带基准，或者本页选了固定手数时，强制这一手。按风险算手数时不传，免得把本页的仓位模式改回固定。
            call_volume = volume
            if not compare_baseline and used.get("apply_limits", True) and str(used.get("sizing_mode") or "") == "risk_pct":
                call_volume = None
            result = runner(
                prepared,
                symbol=code,
                volume=call_volume,
                start_date=start,
                end_date=end,
                engine=engine,
            )
            _fill_backtest_row(result, row)
        except Exception as exc:  # noqa: BLE001
            # 行情缺失或网络失败只记在这一行，不让整批优选停掉。
            row["error"] = str(exc)
        row["frequency"] = trade_frequency(
            int(row.get("trades") or 0),
            str(row.get("data_from") or ""),
            str(row.get("data_to") or ""),
        )
        if compare_baseline:
            row["better"] = better_items(row, ruler)
            row["picked"] = pick_label(row, ruler)
        else:
            # 打分模式先占位，循环结束后再按分数写名次。频率不参与排名。
            row["better"] = []
            row["picked"] = "没测成" if row.get("error") else ""
        rows.append(row)

    if compare_baseline:
        candidates = [row for row in rows if is_candidate(row, ruler)]
        # 先看赢了几项，再看不包含胜率的得分，最后看净赚了多少。
        candidates.sort(
            key=lambda row: (
                len(row.get("better") or []),
                float(row.get("score") or 0),
                float(row.get("net_pnl") or 0),
                float(row.get("profit_factor") or 0),
            ),
            reverse=True,
        )
        mode = "baseline"
    else:
        _assign_ranks(rows)
        candidates = [row for row in rows if str(row.get("picked") or "").startswith("第")]
        candidates.sort(key=lambda row: int(row.get("rank") or 999))
        mode = "rank"
    return {
        "baseline": ruler,
        "volume": volume,
        "rows": rows,
        "candidates": candidates,
        "stopped": stopped,
        "mode": mode,
        "settings": used,
        "skipped_thin": skipped_thin,
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _fill_backtest_row(result: Any, row: dict[str, Any]) -> None:
    """
    把一次历史回测的成绩写进表格行。

    单品种优选、比策略、交叉测试都走这里，字段和口径保持一致。
    """
    perf = result.performance
    row["trades"] = int(perf.trades)
    row["win_rate"] = float(perf.win_rate)
    row["profit_factor"] = float(perf.profit_factor)
    row["payoff"] = float(getattr(perf, "payoff", 0.0) or 0.0)
    row["avg_win"] = float(getattr(perf, "avg_win", 0.0) or 0.0)
    row["avg_loss"] = float(getattr(perf, "avg_loss", 0.0) or 0.0)
    row["losses"] = int(getattr(perf, "losses", 0) or 0)
    _exp = getattr(perf, "expectancy", None)
    row["expectancy"] = 0.0 if _exp is None else float(_exp)
    row["max_drawdown"] = float(perf.max_drawdown)
    row["net_pnl"] = float(getattr(perf, "net_pnl", 0.0) or 0.0)
    row["score"] = float(getattr(perf, "score", 0.0) or 0.0)
    snap = getattr(result, "params_snapshot", None) or {}
    margin_used = float(snap.get("margin_used") or 0)
    row["margin_used"] = margin_used
    row["return_rate"] = (row["net_pnl"] / margin_used) if margin_used > 0 else None
    row["data_from"] = str(snap.get("data_from") or "")
    row["data_to"] = str(snap.get("data_to") or "")
    row["bars"] = int(getattr(result, "bars", 0) or 0)
    _mark_picker_eval_no_stress(result)
    row.update(_evaluation_cells(result))


def _mark_picker_eval_no_stress(result: Any) -> None:
    """
    交叉测试组合太多，不跑滑点加压和参数扰动。

    在评估里标明「没跑」，避免表格上的「通过」被理解成已经过了历史回测页那两道压力。
    结论本身仍只看已经算过的项（盈利因子、回撤、卡尔玛等）。
    """
    from src.backtest.evaluation import mark_stress_not_run

    report = getattr(result, "evaluation", None)
    if report is None:
        return
    mark_stress_not_run(report, reason="交叉测试不跑；请用历史回测页看这一项")
    result.evaluation = report


def _evaluation_cells(result: Any) -> dict[str, Any]:
    """
    把回测自带的评估报告展开成表格字段。

    直接用 run_backtest 按配置门槛算出的 evaluation，不在表格层另算一套。
    评估没启用或没有成交时留「未评估」，和历史回测页一致。
    """
    try:
        report = getattr(result, "evaluation", None)
        if report is None:
            return {
                "evaluation": "未评估",
                "verdict": "skip",
                "fail_items": "",
                "r_expectancy": None,
                "calmar": None,
                "consecutive_losses": None,
                "eval_checks": [],
                "eval_metrics": {},
            }
        metrics = getattr(report, "metrics", {}) or {}
        checks = []
        for check in getattr(report, "checks", []) or []:
            to_dict = getattr(check, "to_dict", None)
            checks.append(to_dict() if callable(to_dict) else dict(getattr(check, "__dict__", {})))
        return {
            "evaluation": verdict_text(report),
            "verdict": getattr(report, "conclusion", "watch"),
            "fail_items": report.failed_text() if hasattr(report, "failed_text") else "",
            "r_expectancy": metrics.get("expectancy", metrics.get("r_expectancy")),
            "calmar": metrics.get("calmar"),
            "consecutive_losses": metrics.get("consecutive_losses"),
            "eval_checks": checks,
            "eval_metrics": dict(metrics),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "evaluation": "评估失败",
            "verdict": "skip",
            "fail_items": str(exc),
            "r_expectancy": None,
            "calmar": None,
            "consecutive_losses": None,
            "eval_checks": [],
            "eval_metrics": {},
        }


def _run_cross_backtest(
    cfg: dict[str, Any],
    settings: dict[str, Any],
    *,
    symbol: str,
    engine: str,
    runner: Callable[..., Any],
    risk_sizing: bool = False,
) -> tuple[Any, int | None]:
    """
    交叉测试里的一个组合：按这组条件调用同一次历史回测。

    准备配置和手数仍走 prepare_screen_cfg，保证各组对照变量一致。
    交叉测试始终按固定手数比；比策略时才允许按每笔风险重算手数。
    """
    prepared, volume = prepare_screen_cfg(cfg, settings=settings)
    call_volume: int | None = volume
    if risk_sizing and settings.get("apply_limits", True) and str(settings.get("sizing_mode") or "") == "risk_pct":
        call_volume = None
    result = runner(
        prepared,
        symbol=symbol,
        volume=call_volume,
        start_date=settings.get("start_date") or None,
        end_date=settings.get("end_date") or None,
        engine=engine,
    )
    return result, volume


def aligned_backtest_runner(cfg: dict[str, Any] | None = None) -> Callable[..., Any]:
    """
    交叉测试默认回测函数：换月开关和真实账户口径跟历史回测页同一套。

    历史回测勾了「主力合约换月」，这里也按主力时段分段，不再只拿当前合约从年初测到尾。
    测试传入自己的 runner 时不会走到这里。
    """
    del cfg

    def _run(prepared: dict[str, Any], **kwargs: Any) -> Any:
        from src.backtest.engine import run_backtest
        from src.backtest.rollover import run_backtest_rollover

        bt = prepared.get("backtest") if isinstance(prepared.get("backtest"), dict) else {}
        kwargs.setdefault("chain_capital", bool(bt.get("chain_capital", False)))
        if bool(bt.get("rollover_main", True)):
            return run_backtest_rollover(prepared, **kwargs)
        return run_backtest(prepared, **kwargs)

    return _run


def screen_strategies(
    cfg: dict[str, Any],
    symbol: str,
    settings: dict[str, Any] | None = None,
    *,
    runner: Callable[..., Any] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    固定一个合约，把每一套策略都回测一遍。

    周期、日期和手数跟交叉测试页上填的一样，只有策略不同。
    得分高的排前面。不改正在用的策略，也不拿这一轮去改配置。
    """
    if runner is None:
        runner = aligned_backtest_runner(cfg)

    contract = str(symbol or "").strip()
    if not contract or contract == "（还没有）":
        raise ValueError("请先选一个合约")
    spec = dict(settings or {})
    spec["fair_compare"] = False
    spec["symbol"] = contract
    rows: list[dict[str, Any]] = []
    stopped = False
    engines = listed_cross_engines()
    total = len(engines)
    volume = 1

    for index, (engine, label) in enumerate(engines, start=1):
        if cancel is not None and cancel():
            stopped = True
            break
        if on_progress is not None:
            on_progress(index, total, label)
        one = dict(spec)
        one["engine"] = engine
        row: dict[str, Any] = {
            "symbol": contract,
            "name": label,
            "engine": engine,
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "payoff": 0.0,
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "net_pnl": 0.0,
            "error": "",
            "liquidity": liquidity_text(contract),
        }
        try:
            result, volume = _run_cross_backtest(
                cfg, one, symbol=contract, engine=engine, runner=runner, risk_sizing=True
            )
            _fill_backtest_row(result, row)
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)
        row["frequency"] = trade_frequency(
            int(row.get("trades") or 0),
            str(row.get("data_from") or ""),
            str(row.get("data_to") or ""),
        )
        row["better"] = []
        row["picked"] = "没测成" if row.get("error") else ""
        rows.append(row)

    _assign_ranks(rows)
    candidates = [row for row in rows if str(row.get("picked") or "").startswith("第")]
    candidates.sort(key=lambda row: int(row.get("rank") or 999))
    return {
        "baseline": {},
        "volume": volume,
        "rows": rows,
        "candidates": candidates,
        "stopped": stopped,
        "mode": "strategy",
        "settings": spec,
        "skipped_thin": 0,
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _assign_ranks(rows: list[dict[str, Any]]) -> None:
    """成交够 3 笔的按得分从高到低写上第几名。样本不够和没测成的不占名次。"""
    pool: list[dict[str, Any]] = []
    for row in rows:
        if row.get("error"):
            row["picked"] = "没测成"
            row["rank"] = 0
            continue
        if int(row.get("trades") or 0) < MIN_TRADES:
            row["picked"] = "未入选"
            row["rank"] = 0
            continue
        pool.append(row)
    pool.sort(key=lambda row: float(row.get("score") or -999), reverse=True)
    for index, row in enumerate(pool, start=1):
        row["rank"] = index
        row["picked"] = f"第{index}名"


def mark_relative_better(rows: list[dict[str, Any]]) -> None:
    """
    在这一轮已经测完的组合里，标出相对较好的那些。

    先按得分排名。然后只在「净赚、盈利因子大于 1、成交不少于 5 笔」的组合里，
    取得分最高的前四分之一，标成「较好」。至少留 1 个，避免一个都不标。
    其余有成交的标成「一般」。这只是这一轮里相对好看，不代表以后会赚钱。
    """
    _assign_ranks(rows)
    eligible = [
        row
        for row in rows
        if not row.get("error")
        and int(row.get("trades") or 0) >= MIN_COMPARE_TRADES
        and float(row.get("net_pnl") or 0) > 0
        and float(row.get("profit_factor") or 0) > 1
    ]
    eligible.sort(key=lambda row: float(row.get("score") or -999), reverse=True)
    keep = max(1, (len(eligible) + 3) // 4) if eligible else 0
    better = {id(row) for row in eligible[:keep]}
    for row in rows:
        if row.get("error"):
            row["picked"] = "没测成"
        elif int(row.get("trades") or 0) < MIN_TRADES:
            row["picked"] = "未入选"
        elif id(row) in better:
            row["picked"] = "较好"
        else:
            row["picked"] = "一般"


def screen_cross(
    cfg: dict[str, Any],
    settings: dict[str, Any] | None = None,
    *,
    universe: list[tuple] | None = None,
    engines: list[tuple[str, str]] | None = None,
    periods: list[str] | None = None,
    runner: Callable[..., Any] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    策略 × 合约 × 周期 交叉测试。

    每一组都用同一套手数、日期和开仓限制。信号和止损用同一个周期。
    全品种主力连续只留着备查，这一轮不用那份。
    不跟某一次历史回测比。同一个品种、同一个周期的 K 线下载一次后，后面的策略接着用。
    不改正在用的策略，也不写历史回测那份结果。
    """
    if runner is None:
        runner = aligned_backtest_runner(cfg)

    spec = dict(settings or {})
    # 不看账户权益，也不按风险百分比算手数。每个组合都用页面上的固定手数。
    try:
        spec["fixed_volume"] = max(1, int(spec.get("fixed_volume") or 1))
    except (TypeError, ValueError):
        spec["fixed_volume"] = 1
    spec["fair_compare"] = True
    spec["ignore_equity"] = True
    spec.pop("engine", None)
    from src.data.periods import period_label

    strategies = list(engines or listed_cross_engines())
    items, skipped_thin = _without_thin(list(universe or MAIN_UNIVERSE))
    period_codes = normalize_cross_periods(periods)
    if not period_codes:
        fallback = str(spec.get("timeframe") or "5")
        period_codes = normalize_cross_periods([fallback]) or ["5"]
    # 每个品种只拉一次最新 K 线，给「当下行情」列用；和回测那份数据分开取，避免把回测末日当成现在。
    from src.strategy.session_audit import make_bars_provider

    bars_fn = make_bars_provider(cfg)
    rows: list[dict[str, Any]] = []
    stopped = False
    total = max(1, len(items) * len(strategies) * len(period_codes))
    step = 0
    volume = 1

    for period in period_codes:
        if stopped:
            break
        for item in items:
            if stopped:
                break
            code, product, _mult, _thin = _contract(item)
            for engine, label in strategies:
                step += 1
                if cancel is not None and cancel():
                    stopped = True
                    break
                if on_progress is not None:
                    on_progress(step, total, f"{product} · {label} · {period_label(period)}")
                one = dict(spec)
                one["engine"] = engine
                # 信号和止损用同一个周期，这是这一轮的对照变量。
                one["timeframe"] = period
                one["trail_timeframe"] = period
                row: dict[str, Any] = {
                    "symbol": code,
                    "product": product,
                    "strategy": label,
                    "engine": engine,
                    "timeframe": period,
                    "trades": 0,
                    "win_rate": 0.0,
                    "profit_factor": 0.0,
                    "payoff": 0.0,
                    "expectancy": 0.0,
                    "max_drawdown": 0.0,
                    "net_pnl": 0.0,
                    "error": "",
                    # 成交好不好按品种字母去全品种名单里认。你点名的合约即使偏少也照样测。
                    "liquidity": liquidity_text(code),
                }
                try:
                    # 每一个组合就是一次普通历史回测：同一套配置、日期、手数和评估门槛。
                    result, volume = _run_cross_backtest(
                        cfg, one, symbol=code, engine=engine, runner=runner
                    )
                    _fill_backtest_row(result, row)
                except Exception as exc:  # noqa: BLE001
                    # 某一个组合拉不到 K 线，只记这一行，后面的品种和策略继续测。
                    row["error"] = str(exc)
                row["frequency"] = trade_frequency(
                    int(row.get("trades") or 0),
                    str(row.get("data_from") or ""),
                    str(row.get("data_to") or ""),
                )
                try:
                    row.update(
                        _market_snapshot(
                            cfg,
                            bars_fn,
                            code,
                            period,
                            engine,
                        )
                    )
                except Exception:  # noqa: BLE001
                    # 附加信息，拿不到就留空，不能让它把整行回测结果带崩。
                    row.update({"market_score": None, "market_ok": None, "market_note": "取数失败"})
                row["better"] = []
                row["picked"] = "没测成" if row.get("error") else ""
                rows.append(row)

    mark_relative_better(rows)
    candidates = [row for row in rows if row.get("picked") == "较好"]
    candidates.sort(key=lambda row: int(row.get("rank") or 999))
    spec["strategies"] = [engine for engine, _label in strategies]
    spec["periods"] = period_codes
    spec["timeframe"] = period_codes[0] if period_codes else spec.get("timeframe")
    spec["trail_timeframe"] = spec["timeframe"]
    return {
        "baseline": {},
        "volume": volume,
        "rows": rows,
        "candidates": candidates,
        "stopped": stopped,
        "mode": "cross",
        "settings": spec,
        "skipped_thin": skipped_thin,
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _market_snapshot(
    cfg: dict[str, Any],
    bars_fn: Callable[[str, str, int], Any],
    symbol: str,
    timeframe: str,
    engine: str,
) -> dict[str, Any]:
    """
    交叉测试附带的「当下行情」：用这套引擎的过滤模式给现在这一刻打分。

    口径和盘前审计一致；行情过滤开关关着也照算，分数只作参考，不影响回测成绩。
    """
    from dataclasses import replace

    from src.data.periods import drop_forming_bar
    from src.strategy.market_state import (
        MarketFilterParams,
        evaluate_market_state,
        ENGINE_TREND_SECTION,
        range_params_for_daily_body,
        score_market_state,
        trend_params_for_section,
    )
    from src.strategy.session_audit import CTA_OWN_FILTER, ENGINE_AUDIT_MODES

    mode = ENGINE_AUDIT_MODES.get(engine)
    if mode is None:
        return {"market_score": None, "market_ok": None, "market_note": "未接入行情过滤"}

    if engine in CTA_OWN_FILTER:
        from src.strategy.cta_filter import CtaFilterParams, evaluate_cta_filter

        kind = {"cta": "multi_factor"}.get(
            engine, engine
        )
        st = cfg.get("strategy") or {}
        box = st.get(kind) if isinstance(st.get(kind), dict) else {}
        cta = CtaFilterParams.from_box(box, kind=kind)
        need = max(cta.need_bars, cta.adx_period + 5)
        bars = bars_fn(symbol, timeframe, need + 1)
        closed = drop_forming_bar(bars) if bars is not None else None
        if closed is None or len(closed) < need:
            return {"market_score": None, "market_ok": None, "market_note": "K线不足"}
        gate = evaluate_cta_filter(closed, cta)
        score = min(100.0, max(0.0, gate.adx) + max(0.0, min(1.0, gate.atr_rank)) * 50.0)
        return {
            "market_score": round(float(score), 1),
            "market_ok": bool(gate.ok),
            "market_note": gate.describe() if gate.ok else gate.reason,
        }

    if engine == "daily_body":
        params = range_params_for_daily_body(cfg)
        if not params.enabled:
            params = replace(params, enabled=True, mode="range")
    else:
        section = ENGINE_TREND_SECTION.get(engine)
        if section:
            base = trend_params_for_section(cfg, section)
        else:
            base = MarketFilterParams(enabled=True, mode=mode or "trend")
        params = replace(base, enabled=True, mode=mode or "trend")
    bars = bars_fn(symbol, timeframe, params.need_bars + 1)
    closed = drop_forming_bar(bars) if bars is not None else None
    if closed is None or len(closed) < params.need_bars:
        return {"market_score": None, "market_ok": None, "market_note": "K线不足"}

    state = evaluate_market_state(closed, params)
    return {
        "market_score": round(float(score_market_state(closed, params)), 1),
        "market_ok": bool(state.ok),
        "market_note": state.describe() if state.ok else state.reason,
    }


def _market_text(source: dict[str, Any]) -> str:
    """结果表「当下行情」格子：分数 + 适配/不适配，没有数据就给原因。"""
    score = source.get("market_score")
    if score is None:
        return str(source.get("market_note") or "—")
    return f"{float(score):.0f} {'适配' if source.get('market_ok') else '不适配'}"


def choice_labels(candidates: list[dict[str, Any]]) -> list[str]:
    """下拉框里的一行：代码在前，中文名在后。没有备选时给一句占位，避免空下拉框。"""
    labels = [f"{row['symbol']} {row['name']}" for row in candidates if row.get("symbol")]
    return labels or ["还没有备选"]


def code_from_label(label: str) -> str:
    """从「rb0 螺纹」里取出要写进关注品种的代码。占位文字不当成品种。"""
    text = (label or "").strip()
    if not text or text.startswith("还没有"):
        return ""
    return text.split()[0]


def trade_frequency(trades: int, data_from: str, data_to: str) -> str:
    """
    这段行情里成交有多密。

    用实际第一根到最后一根的日历天数来除笔数。一天不到一笔，就写成「约几天 1 笔」；
    一天不止一笔，就写成「约 1 天几笔」。没有起止日期时只写笔数。
    这个数字只给人看，不参与入选。
    """
    count = max(0, int(trades or 0))
    if count <= 0:
        return "没有成交"
    start = _as_day(data_from)
    end = _as_day(data_to)
    if start is None or end is None:
        return f"{count}笔"
    days = max(1, (end - start).days + 1)
    per_day = count / days
    if per_day >= 1:
        return f"{count}笔，约1天{per_day:.1f}笔"
    return f"{count}笔，约{days / count:.1f}天1笔"


def pick_label(row: dict[str, Any], baseline: dict[str, Any]) -> str:
    """入选、未入选或没测成。交易频率不在这里判断。"""
    if row.get("error"):
        return "没测成"
    if is_candidate(row, baseline):
        return "入选"
    return "未入选"


def list_rows(result: dict[str, Any]) -> list[dict[str, str]]:
    """
    给结果表用的每一行。

    带基准时，第一行是基准。入选的排在前面。
    打分排名时没有基准行，按名次从第 1 名往下排。
    每一行都有胜率、盈利因子、盈亏比、收益期望、最大回撤、净盈亏、盈利覆盖、收益率、交易频率、笔数、每笔收益、
    实际测试时段、起止日、天数、日均、K 线根数、得分和结果。
    净盈亏、覆盖相关列、收益率、实际时段、根数和收益期望只给人看，不参与入选，也不参与排名。
    """
    base = result.get("baseline") or {}
    ranking = result.get("mode") in ("rank", "strategy")
    by_strategy = result.get("mode") == "strategy"
    by_cross = result.get("mode") == "cross"
    from src.data.periods import period_label

    period = str((result.get("settings") or {}).get("timeframe") or "")
    if not period:
        period = str(base.get("timeframe") or "")
    shown: list[dict[str, str]] = []
    if base.get("symbol") and not ranking:
        base_row = dict(base)
        base_row["liquidity"] = liquidity_text(str(base.get("symbol") or ""))
        base_shown = _display_row(
            str(base.get("symbol")),
            base_row,
            trade_frequency(
                int(base.get("trades") or 0),
                str(base.get("data_from") or ""),
                str(base.get("data_to") or ""),
            ),
            "基准",
            period,
        )
        base_shown["strategy"] = "—"
        shown.append(base_shown)
    tested = list(result.get("rows") or [])
    if by_cross:
        tested.sort(key=_cross_sort_key)
    elif ranking:
        tested.sort(key=_rank_sort_key)
    else:
        tested.sort(
            key=lambda row: (
                {"入选": 0, "未入选": 1, "没测成": 2}.get(str(row.get("picked") or pick_label(row, base)), 1),
                -float(row.get("profit_factor") or 0),
            )
        )
    for row in tested:
        if by_cross:
            name = f"{row.get('product') or ''} {row.get('symbol') or ''}".strip()
            strategy_text = str(row.get("strategy") or "—")
        elif by_strategy:
            name = str(row.get("name") or row.get("engine") or "").strip()
            strategy_text = name or "—"
        else:
            name = f"{row.get('name') or ''} {row.get('symbol') or ''}".strip()
            strategy_text = "—"
        picked = str(row.get("picked") or ("" if ranking or by_cross else pick_label(row, base)))
        if picked == "没测成":
            empty = _empty_cover_display()
            shown.append({
                "symbol": name or "-",
                "strategy": strategy_text,
                "period": period_label(str(row.get("timeframe") or period or "")) or "—",
                "timeframe": str(row.get("timeframe") or period or ""),
                "win_rate": "—",
                "profit_factor": "—",
                "payoff": "—",
                "expectancy": "—",
                "max_drawdown": "—",
                "calmar": "—",
                "loss_streak": "—",
                "net_pnl": "—",
                "profit_cover": empty["profit_cover"],
                "return_rate": "—",
                "frequency": "—",
                "trade_count": empty["trade_count"],
                "pnl_per_trade": empty["pnl_per_trade"],
                "span": "—",
                "span_start": empty["span_start"],
                "span_end": empty["span_end"],
                "span_days": empty["span_days"],
                "daily_avg": empty["daily_avg"],
                "bars": "—",
                "score": "—",
                "evaluation": str(row.get("evaluation") or "未评估"),
                "fail_items": _fail_text(row.get("fail_items")),
                "market": _market_text(row),
                "liquidity": str(row.get("liquidity") or "—"),
                "picked": "没测成",
            })
            continue
        item = _display_row(
            name,
            row,
            str(row.get("frequency") or trade_frequency(
                int(row.get("trades") or 0),
                str(row.get("data_from") or ""),
                str(row.get("data_to") or ""),
            )),
            picked,
            period,
        )
        item["strategy"] = strategy_text
        tf = str(row.get("timeframe") or period or "")
        item["timeframe"] = tf
        item["period"] = period_label(tf) if tf else "—"
        shown.append(item)
    return shown


# 窗口里一行要画十几个小格子。一次画一千行，程序会在打开时卡住并自己退出。
WINDOW_ROW_LIMIT = 60


def window_rows(rows: list[dict[str, str]] | None, limit: int = WINDOW_ROW_LIMIT) -> tuple[list[dict[str, str]], str]:
    """
    决定窗口里实际画出的行。

    较好的已经排在前面。超过上限时只画前面这些，其余仍在导出的 Excel 里。
    行数不多时原样返回，不额外加说明。
    """
    items = list(rows or [])
    if len(items) <= limit:
        return items, ""
    note = (
        f"这一轮一共 {len(items)} 行。窗口里先显示前 {limit} 行，较好的在前面。"
        "要看全部，点「导出本次交叉测试」。"
    )
    return items[:limit], note


def _cross_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    """较好的排最前，然后是一般、样本不够、没测成。同一档里按名次。"""
    picked = str(row.get("picked") or "")
    group = {"较好": 0, "一般": 1, "未入选": 2, "没测成": 3}.get(picked, 4)
    rank = int(row.get("rank") or 0)
    return (group, rank if rank > 0 else 9999)


def _rank_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    """第 1 名在最上面，接着是没进排名的，最后是没测成的。"""
    picked = str(row.get("picked") or "")
    if picked.startswith("第"):
        return (0, int(row.get("rank") or 999))
    if picked == "没测成":
        return (2, 0)
    return (1, 0)


def _thin_note(result: dict[str, Any]) -> str:
    """
    说明有多少流动性差的品种被留在测试外面。

    旧的优选记录里没有这个数字，就不补这句话，避免和当时真正测过的表对不上。
    """
    if "skipped_thin" not in result:
        return ""
    count = int(result.get("skipped_thin") or 0)
    if count <= 0:
        return "流动性偏少的不参加测试。"
    return f"流动性偏少的 {count} 个不参加测试。"


def board_summary(result: dict[str, Any]) -> str:
    """表格上面的说明。带基准和打分排名各写各的规则。"""
    tested = list(result.get("rows") or [])
    stopped = "中途停了，表里是已经测完的。" if result.get("stopped") else ""
    thin_note = _thin_note(result)
    if result.get("mode") == "cross":
        spec = result.get("settings") or {}
        span = f"{spec.get('start_date') or '不限'} ~ {spec.get('end_date') or '不限'}"
        better = sum(1 for row in tested if row.get("picked") == "较好")
        lots = int(result.get("volume") or spec.get("fixed_volume") or 1)
        tail = "点「开始交叉测试」后，下表按组合列出结果。" if not tested else f"已测 {len(tested)} 个组合，较好 {better} 个。"
        return (
            f"交叉测试 · 每个组合固定 {lots} 手 · 区间 {span}\n"
            f"不看账户权益，也不看保证金。只测你勾选的策略、合约和周期。信号和止损用同一个周期。全品种名单只备查，不参加这一轮。日期和手数用本页，不跟某一次回测比。"
            f"较好的条件：净赚、盈利因子大于 1、成交不少于 5 笔，并且得分排在这些组合的前四分之一。"
            f"得分看盈利因子和净赚能不能盖住回撤，不看胜率。这只是这段历史上相对好看，不代表以后会赚钱。"
            f"正在用的策略不会被换掉。{thin_note}{tail}{stopped}"
        )
    if result.get("mode") == "strategy":
        spec = result.get("settings") or {}
        span = f"{spec.get('start_date') or '不限'} ~ {spec.get('end_date') or '不限'}"
        ranked = sum(1 for row in tested if str(row.get("picked") or "").startswith("第"))
        tail = "点「开始优选」后，下表按得分列出每一套策略。" if not tested else f"已测 {len(tested)} 套，有名次 {ranked} 套。"
        return (
            f"比策略 · {spec.get('symbol') or '-'} · 区间 {span}\n"
            f"同一合约、同一段日期、同一套手数，把每一套策略都跑一遍。得分高的排前面。"
            f"得分看盈利因子和净赚能不能盖住回撤，不看胜率。成交少于 3 笔不排名。"
            f"正在用的策略不会被换掉。{tail}{stopped}"
        )
    if result.get("mode") == "rank":
        spec = result.get("settings") or {}
        span = f"{spec.get('start_date') or '不限'} ~ {spec.get('end_date') or '不限'}"
        ranked = sum(1 for row in tested if str(row.get("picked") or "").startswith("第"))
        tail = "点「开始优选」后，下表按得分列出全部品种。" if not tested else f"已测 {len(tested)} 个，有名次 {ranked} 个。"
        return (
            f"不带基准 · {spec.get('engine') or '当前策略'} · 区间 {span}\n"
            f"按本页设置打分，分数高的排前面。得分看盈利因子和净赚能不能盖住回撤，不看胜率。成交少于 3 笔不排名。收益率、交易频率、实际时段和 K 线根数只供参考。收益率＝净盈亏÷这次占用最多的一笔保证金，保证金按开仓价×乘数×手数×10%估算。{thin_note}{tail}{stopped}"
        )
    base = result.get("baseline") or {}
    volume = int(result.get("volume") or base.get("fixed_volume") or 1)
    span = f"{base.get('start_date') or '不限'} ~ {base.get('end_date') or '不限'}"
    picked = sum(1 for row in tested if str(row.get("picked") or pick_label(row, base)) == "入选")
    tail = "点「开始优选」后，下表列出每个测过的品种。" if not tested else f"已测 {len(tested)} 个，入选 {picked} 个。"
    return (
        f"基准 {base.get('symbol') or '-'} · 每笔 {volume} 手，不看保证金 · 区间 {span}\n"
        f"入选看净盈亏、盈利因子、最大回撤至少两项更好，不看胜率，并且成交不少于 3 笔。收益率、交易频率、实际时段、K线根数和得分只供参考。收益率＝净盈亏÷这次占用最多的一笔保证金，保证金按开仓价×乘数×手数×10%估算。{thin_note}{tail}{stopped}"
    )


def _display_row(
    name: str,
    source: dict[str, Any],
    frequency: str,
    picked: str,
    period: str = "",
) -> dict[str, str]:
    """把一组成绩收成表格上的字串。得分过低表示样本不够，格子里留空。"""
    cover = _cover_display(source)
    return {
        "symbol": name or "-",
        "win_rate": _pct(source.get("win_rate")),
        "profit_factor": _num(source.get("profit_factor")),
        "payoff": _payoff_text(source.get("payoff")),
        "expectancy": _expectancy_text(source),
        "max_drawdown": _money(source.get("max_drawdown")),
        "calmar": _calmar_text(source.get("calmar")),
        "loss_streak": _streak_text(source.get("consecutive_losses")),
        "net_pnl": _money(source.get("net_pnl")),
        "profit_cover": cover["profit_cover"],
        "return_rate": _return_text(source.get("return_rate")),
        "frequency": frequency or "—",
        "trade_count": cover["trade_count"],
        "pnl_per_trade": cover["pnl_per_trade"],
        "span": actual_span(str(source.get("data_from") or ""), str(source.get("data_to") or "")),
        "span_start": cover["span_start"],
        "span_end": cover["span_end"],
        "span_days": cover["span_days"],
        "daily_avg": cover["daily_avg"],
        "bars": _bars_text(source, period),
        "score": _score(source.get("score")),
        "evaluation": str(source.get("evaluation") or "未评估"),
        "fail_items": _fail_text(source.get("fail_items")),
        "market": _market_text(source),
        "liquidity": str(source.get("liquidity") or "—"),
        "picked": picked,
    }


def span_cover_fields(
    net_pnl: Any,
    max_drawdown: Any,
    trades: Any,
    data_from: str,
    data_to: str,
    *,
    span_days: int | None = None,
) -> dict[str, Any]:
    """
    交叉测试额外几列的算法。

    盈利覆盖 = 净盈亏 − 最大回撤，表示净赚盖住回撤后还剩多少。
    每笔收益、日均都用盈利覆盖去除，和你在导出表里手加的公式一致。
    天数用结束日减开始日（不多算一天），和 Excel 的 DATEDIF 一样。
    滚动前进的检验段是多段拼起来的，用 span_days 传各段天数之和，不能拿首尾一大段算。
    """
    try:
        net = float(net_pnl or 0)
    except (TypeError, ValueError):
        net = 0.0
    try:
        drawdown = float(max_drawdown or 0)
    except (TypeError, ValueError):
        drawdown = 0.0
    try:
        count = int(trades or 0)
    except (TypeError, ValueError):
        count = 0
    cover = net - drawdown
    start = _as_day(data_from)
    end = _as_day(data_to)
    if span_days is not None:
        days: int | None = int(span_days)
    else:
        days = (end - start).days if start is not None and end is not None else None
    per_trade = (cover / count) if count > 0 else None
    daily = (cover / days) if days is not None and days > 0 else None
    return {
        "profit_cover": cover,
        "trade_count": count,
        "pnl_per_trade": per_trade,
        "span_start": start.isoformat() if start else "",
        "span_end": end.isoformat() if end else "",
        "span_days": days,
        "daily_avg": daily,
    }


def _empty_cover_display() -> dict[str, str]:
    """没测成时，覆盖相关列一律留空。"""
    return {
        "profit_cover": "—",
        "trade_count": "—",
        "pnl_per_trade": "—",
        "span_start": "—",
        "span_end": "—",
        "span_days": "—",
        "daily_avg": "—",
    }


def _cover_display(source: dict[str, Any]) -> dict[str, str]:
    """把盈利覆盖等几列收成表格字串。缺日期或没成交时，除法相关列留空。"""
    raw = span_cover_fields(
        source.get("net_pnl"),
        source.get("max_drawdown"),
        source.get("trades"),
        str(source.get("data_from") or ""),
        str(source.get("data_to") or ""),
    )
    per_trade = raw["pnl_per_trade"]
    daily = raw["daily_avg"]
    days = raw["span_days"]
    return {
        "profit_cover": _money(raw["profit_cover"]),
        "trade_count": str(int(raw["trade_count"])),
        "pnl_per_trade": _money2(per_trade) if per_trade is not None else "—",
        "span_start": str(raw["span_start"] or "—"),
        "span_end": str(raw["span_end"] or "—"),
        "span_days": str(int(days)) if days is not None else "—",
        "daily_avg": _money2(daily) if daily is not None else "—",
    }


def cover_export_cells(
    net_pnl: Any,
    max_drawdown: Any,
    trades: Any,
    data_from: str = "",
    data_to: str = "",
    *,
    span_days: int | None = None,
) -> dict[str, Any]:
    """
    导出 Excel 用的盈利覆盖相关列。

    历史回测摘要、策略优化结果和交叉测试共用同一套算法，方便三张表对照。
    """
    raw = span_cover_fields(
        net_pnl, max_drawdown, trades, data_from, data_to, span_days=span_days
    )
    days = raw["span_days"]
    per_trade = raw["pnl_per_trade"]
    daily = raw["daily_avg"]
    return {
        "盈利覆盖": _round_export(raw["profit_cover"], 0),
        "每笔收益": _round_export(per_trade, 2) if per_trade is not None else "",
        "开始时间": raw["span_start"] or "",
        "结束时间": raw["span_end"] or "",
        "天数": int(days) if days is not None else "",
        "日均": _round_export(daily, 2) if daily is not None else "",
    }


def format_cover_lines(
    net_pnl: Any,
    max_drawdown: Any,
    trades: Any,
    data_from: str = "",
    data_to: str = "",
) -> list[str]:
    """
    历史回测 / 策略优化结果文字里的盈利覆盖几行。

    和交叉测试导出用同一套算法：覆盖＝净盈亏−回撤；每笔、日均用覆盖去除。
    """
    raw = span_cover_fields(net_pnl, max_drawdown, trades, data_from, data_to)
    lines = [f"盈利覆盖  {raw['profit_cover']:.0f} 元（净盈亏−最大回撤）"]
    per_trade = raw["pnl_per_trade"]
    if per_trade is not None:
        lines.append(f"每笔收益  {per_trade:.2f} 元（盈利覆盖÷笔数）")
    else:
        lines.append("每笔收益  —（没有成交）")
    days = raw["span_days"]
    daily = raw["daily_avg"]
    if daily is not None and days is not None:
        lines.append(f"日均      {daily:.2f} 元（盈利覆盖÷{int(days)}天）")
    elif raw["span_start"] and raw["span_end"]:
        lines.append(f"实际时段  {raw['span_start']} ~ {raw['span_end']}")
    else:
        lines.append("日均      —（没有起止日期）")
    return lines


def _round_export(value: Any, places: int) -> Any:
    """导出成数字。整数列尽量不带小数；有小数时保留两位（和 Excel 读回来的数一致，避免同一笔被当成新行）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if places <= 0:
        if abs(number - round(number)) < 1e-9:
            return int(round(number))
        # 金额列不是整数时保留两位小数；直接写原始浮点会带上 3524.6200000000003 这种尾巴。
        rounded = round(number, 2)
        return int(round(rounded)) if abs(rounded - round(rounded)) < 1e-9 else rounded
    # 先按位数取整；取整后恰好是整数（如 3431.0）就写整数，不然保留小数。
    rounded = round(number, places)
    return int(round(rounded)) if abs(rounded - round(rounded)) < 1e-9 else rounded


def actual_span(data_from: str, data_to: str) -> str:
    """
    这次真正用到的第一根到最后一根，只写日期。

    页面上填的开始、结束可以比这段更宽。分钟线常常只有最近一个月，
    所以各品种要看自己实际测到的日子，不能只看填写的区间。
    """
    start = str(data_from or "").strip()[:10]
    end = str(data_to or "").strip()[:10]
    if len(start) < 10 and len(end) < 10:
        return "—"
    if not start or not end or start == end:
        return start or end or "—"
    return f"{start} ~ {end}"


def bar_count_text(value: Any) -> str:
    """K 线根数。没记下来时留空，避免把上一轮旧记录显示成 0 根。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return "—"
    if count <= 0:
        return "—"
    return str(count)


def _bars_text(source: dict[str, Any], period: str) -> str:
    """
    优先用回测当时记下的根数。

    上一轮优选还没记这个数时，按第一根和最后一根，去电脑里已经存好的行情里数。
    数的是信号周期。文件不在就空着。
    """
    saved = bar_count_text(source.get("bars"))
    if saved != "—":
        return saved
    counted = cached_bar_count(
        str(source.get("symbol") or ""),
        period,
        str(source.get("data_from") or ""),
        str(source.get("data_to") or ""),
    )
    return bar_count_text(counted)


def cached_bar_count(
    symbol: str,
    period: str,
    data_from: str,
    data_to: str,
    cache_dir: Path | None = None,
) -> int:
    """
    在本地行情文件里，数实际时段内有多少根信号 K 线。

    只数第一根到最后一根之间的行，新下载的更晚行情不会被算进上一次测试。
    """
    code = str(symbol or "").strip()
    start = str(data_from or "").strip()[:19]
    end = str(data_to or "").strip()[:19]
    if not code or not str(period or "").strip() or len(start) < 10 or len(end) < 10:
        return 0
    from src.data.periods import cache_tag

    folder = cache_dir if cache_dir is not None else _kline_dir()
    path = folder / f"{code.replace('/', '_')}_{cache_tag(period)}.csv"
    if not path.exists():
        return 0
    count = 0
    try:
        # utf-8-sig 能吃掉文件开头的 BOM，避免第一行日期对不上。
        with path.open(encoding="utf-8-sig") as handle:
            next(handle, None)
            for line in handle:
                stamp = line.split(",", 1)[0].strip()[:19]
                if start <= stamp <= end:
                    count += 1
    except OSError:
        return 0
    return count


def _kline_dir() -> Path:
    """行情文件放在项目的 data/klines。打包后也写在程序旁边，不写进只读的打包目录。"""
    from src.utils.paths import project_root

    return project_root() / "data" / "klines"


def _as_day(text: str):
    """从「2026-10-01 21:05:00」里取出日期。对不上就当没有。"""
    raw = str(text or "").strip()[:10]
    if len(raw) < 10:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_report(result: dict[str, Any]) -> str:
    """把结果表收成一段文字，写入优选记录，方便以后打开还能对上同一张表。"""
    lines = [board_summary(result), ""]
    lines.append(
        "品种 | 策略 | 胜率 | 盈利因子 | 盈亏比 | 收益期望 | 最大回撤 | 净盈亏 | 盈利覆盖 | 收益率 | 交易频率 | 笔数 | "
        "每笔收益 | 实际测试时段 | 开始时间 | 结束时间 | 天数 | 日均 | K线根数 | 得分 | 流动性 | 结果"
    )
    for row in list_rows(result):
        lines.append(
            " | ".join(
                [
                    row["symbol"],
                    row.get("strategy") or "—",
                    row["win_rate"],
                    row["profit_factor"],
                    row.get("payoff") or "—",
                    row.get("expectancy") or "—",
                    row["max_drawdown"],
                    row["net_pnl"],
                    row.get("profit_cover") or "—",
                    row["return_rate"],
                    row["frequency"],
                    row.get("trade_count") or "—",
                    row.get("pnl_per_trade") or "—",
                    row["span"],
                    row.get("span_start") or "—",
                    row.get("span_end") or "—",
                    row.get("span_days") or "—",
                    row.get("daily_avg") or "—",
                    row["bars"],
                    row["score"],
                    row["liquidity"],
                    row["picked"],
                ]
            )
        )
    if result.get("ran_at"):
        lines.append("")
        lines.append(f"完成时间  {result.get('ran_at')}")
    return "\n".join(lines)


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "-"


def _num(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _payoff_text(value: Any) -> str:
    """
    盈亏比：平均盈利÷平均亏损。和历史回测同一口径。

    没有亏损笔时算不出，格子里写横杠，避免和盈利因子混在一起。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number <= 1e-9:
        return "—"
    return f"{number:.2f}"


def _expectancy_text(source: dict[str, Any]) -> str:
    """
    无单位收益期望：胜率×盈亏比 − 败率。每亏 1 元风险期望赚多少。

    有平均盈亏就当场算，避免旧结果里还是「元/笔」的数字。
    没有亏损笔时写横杠。0 也是有效结果，写成 0.00。
    """
    from src.backtest.metrics import unit_expectancy

    try:
        trades = int(source.get("trades") or 0)
    except (TypeError, ValueError):
        trades = 0
    if trades <= 0:
        return "—"
    try:
        win_rate = float(source.get("win_rate") or 0)
    except (TypeError, ValueError):
        win_rate = 0.0
    avg_win = source.get("avg_win")
    avg_loss = source.get("avg_loss")
    if avg_win not in (None, "") and avg_loss not in (None, ""):
        try:
            loss = abs(float(avg_loss))
        except (TypeError, ValueError):
            return "—"
        if loss <= 1e-9:
            return "—"
        value = unit_expectancy(win_rate, avg_win, loss)
        return f"{value:.2f}"
    try:
        payoff = float(source.get("payoff") or 0)
    except (TypeError, ValueError):
        payoff = 0.0
    if payoff > 1e-9:
        value = win_rate * payoff - (1.0 - win_rate)
        return f"{value:.2f}"
    try:
        losses = int(source.get("losses") or 0)
    except (TypeError, ValueError):
        losses = 0
    if losses > 0:
        value = unit_expectancy(win_rate, 0.0, 1.0)
        return f"{value:.2f}"
    return "—"


def _score(value: Any) -> str:
    """综合得分保留两位。样本少被垫成很大的负数时，不拿这个数去吓用户。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number <= -100:
        return "—"
    return f"{number:.2f}"


def _calmar_text(value: Any) -> str:
    """卡尔玛保留两位。没有回撤或没算出来时留空。"""
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _streak_text(value: Any) -> str:
    """最大连亏写成「N笔」。"""
    if value is None or value == "":
        return "—"
    try:
        return f"{int(float(value))}笔"
    except (TypeError, ValueError):
        return "—"


def _fail_text(value: Any) -> str:
    """未通过项合并成一行。没有时写横杠。"""
    if isinstance(value, (list, tuple)):
        text = "、".join(str(item) for item in value if str(item).strip())
    else:
        text = str(value or "").strip()
    return text or "—"


def _return_text(value: Any) -> str:
    """收益率写成百分数。没有保证金可除时留空，避免显示成 0%。"""
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number * 100:.1f}%"


def _money(value: Any) -> str:
    try:
        return f"{float(value):.0f}元"
    except (TypeError, ValueError):
        return "-"


def _money2(value: Any) -> str:
    """每笔收益、日均保留两位，方便和导出表对照。"""
    try:
        return f"{float(value):.2f}元"
    except (TypeError, ValueError):
        return "—"


def save_picks(path: Path | str, payload: dict[str, Any]) -> None:
    """把基准和备选写到 data/symbol_picks.json。不覆盖回测报告。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_saved_picks(path: Path | str) -> None:
    """
    删掉界面下次打开时要重画的那份交叉测试结果。

    Excel 里已经导出的行不在这个文件里，所以删掉不会少掉导出记录。
    文件本来就没有时，当作已经清空。
    """
    target = Path(path)
    if target.exists():
        target.unlink()


def load_picks(path: Path | str) -> dict[str, Any]:
    """读上一次优选。文件没有或坏了就当还没做过。"""
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
