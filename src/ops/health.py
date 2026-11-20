"""
策略健康分数（对照最近一次历史回测）。

功能：拿账本里最近若干笔已平仓，和 data/backtest_last.json 的胜率、盈利因子比一比。
     差太多只记告警、给运行台看一行字，默认不拦开仓。
原理：蓝图第一期「人能盯住」——衰减要看见，不要程序自己换策略或停手。
     样本不够时不评分，避免刚开始几笔就把人吓到。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.strategy.types import config_flag


LOOKBACK = 20
MIN_TRADES = 10
WINRATE_DROP = 0.20
PF_RATIO = 0.50
# 连续多少个交易日偏离才允许按配置停开新仓。默认 5 天，和书面退役线一致。
WATCH_DAYS_TO_BLOCK = 5
STATE_NAME = "health_state.json"
_SCOPE_WORD = {
    "engine": "策略",
    "symbol": "品种",
    "timeframe": "周期",
    "data_source": "数据源",
    "slippage_ticks": "滑点",
    "fill_on_next_open": "成交方式",
    "sizing_mode": "手数模式",
    "chain_capital": "换月链资金",
}


@dataclass
class HealthReport:
    """一次健康检查的结果。status: skip=样本不够，ok=还行，watch=该看一眼。"""

    status: str
    message: str
    live_n: int = 0
    live_win_rate: float | None = None
    live_pf: float | None = None
    bt_n: int = 0
    bt_win_rate: float | None = None
    bt_pf: float | None = None
    # 连续偏离回测的交易日数。skip 表示没有新证据，不会清零。
    watch_days: int = 0
    # 最近回测是否记录了引擎 / 品种 / 周期。旧结果没记时只能标「仅供参考」。
    scope_recorded: bool = True
    # 回测口径与当前配置不一致时为真，此时不做胜负率比较。
    scope_mismatch: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "live_n": self.live_n,
            "live_win_rate": self.live_win_rate,
            "live_pf": self.live_pf,
            "bt_n": self.bt_n,
            "bt_win_rate": self.bt_win_rate,
            "bt_pf": self.bt_pf,
            "watch_days": self.watch_days,
            "scope_recorded": self.scope_recorded,
            "scope_mismatch": self.scope_mismatch,
        }


def _profit_factor(pnls: list[float]) -> float:
    """赚到的钱除以亏掉的钱。没亏过时，有盈利记一个很大的数，方便比较。"""
    gain = sum(x for x in pnls if x > 0)
    loss = abs(sum(x for x in pnls if x < 0))
    if loss <= 1e-9:
        return 99.0 if gain > 0 else 0.0
    return gain / loss


def _win_rate(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    return sum(1 for x in pnls if x > 0) / len(pnls)


def _live_pnls(
    root: Path,
    lookback: int,
    *,
    symbol: str = "",
    account: str | None = None,
) -> list[float]:
    """
    账本里最近的已平仓盈亏。

    account 有值时只读那本账；不传时读当前选中的账（账本本身按账户隔离）。
    传 symbol 时按品种过滤，避免多品种账本混进别的品种。
    """
    from src.account.signal_book import load_book, load_named_book, matched_closes
    from src.account.slippage_log import product_of

    book = load_named_book(root, account) if account else load_book(root)
    if not book:
        return []
    want = product_of(symbol) if symbol else ""
    pnls: list[float] = []
    for row in matched_closes(book):
        if want and product_of(str(row.get("symbol") or "")) != want:
            continue
        pnls.append(float(row.get("pnl") or 0.0))
    if lookback > 0:
        pnls = pnls[-lookback:]
    return pnls


def _backtest_doc(root: Path) -> dict[str, Any]:
    """读最近一次回测结果。文件缺失或格式不对时返回空，调用方按「没记录」处理。"""
    path = root / "data" / "backtest_last.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _backtest_perf(root: Path) -> dict[str, Any]:
    perf = _backtest_doc(root).get("performance") or {}
    return perf if isinstance(perf, dict) else {}


def _backtest_scope(root: Path) -> dict[str, Any]:
    """最近回测记录下来的口径。旧结果可能只记了品种和周期。"""
    data = _backtest_doc(root)
    snap = data.get("params_snapshot") or {}
    if not isinstance(snap, dict):
        snap = {}
    return {
        "engine": str(snap.get("engine") or "").strip(),
        "symbol": str(data.get("symbol") or "").strip(),
        "timeframe": str(data.get("timeframe") or "").strip(),
        "data_source": str(snap.get("data_source") or "").strip(),
        "slippage_ticks": snap.get("slippage_ticks"),
        "fill_on_next_open": snap.get("fill_on_next_open"),
        "sizing_mode": str(snap.get("sizing_mode") or "").strip(),
        "chain_capital": snap.get("chain_capital"),
    }


def _current_scope(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """当前配置里与回测快照同名的口径字段。没写的项不比较，避免凭空判不一致。"""
    if not isinstance(cfg, dict):
        return {}
    data = cfg.get("data") if isinstance(cfg.get("data"), dict) else {}
    bt = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
    risk = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
    out: dict[str, Any] = {}
    if data.get("source"):
        out["data_source"] = data.get("source")
    if "slippage_ticks" in bt:
        out["slippage_ticks"] = bt.get("slippage_ticks")
    if "fill_on_next_open" in bt:
        out["fill_on_next_open"] = bt.get("fill_on_next_open")
    if risk.get("sizing_mode"):
        out["sizing_mode"] = risk.get("sizing_mode")
    if "chain_capital" in bt:
        out["chain_capital"] = bt.get("chain_capital")
    return out


def _norm_scope(key: str, value: Any) -> str:
    """口径比较用的统一写法：品种归到品种码，周期只留数字，布尔归成 0/1。"""
    if value is None or value == "":
        return ""
    if key == "symbol":
        from src.account.slippage_log import product_of

        return product_of(str(value))
    if key == "slippage_ticks":
        try:
            return f"{float(value):.6f}"
        except (TypeError, ValueError):
            return ""
    if key in ("fill_on_next_open", "chain_capital"):
        return "1" if config_flag(value, default=(key == "fill_on_next_open")) else "0"
    text = str(value or "").strip().lower()
    if key == "timeframe":
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits or text
    return text


def _scope_text(engine: str, symbol: str, timeframe: str) -> str:
    return f"{engine or '—'} / {symbol or '—'} / {timeframe or '—'} 分钟"


def _state_path(root: Path) -> Path:
    return root / "data" / STATE_NAME


def _load_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 健康状态只是证据，不因写盘失败影响开平仓。
        return


def _state_days(root: Path) -> int:
    try:
        return int(_load_state(root).get("watch_days") or 0)
    except (TypeError, ValueError):
        return 0


def _note_watch_day(root: Path, status: str) -> int:
    """
    连续偏离天数。同一天多次检查只累计一次；恢复正常时清零；样本不足时保留。

    只记自然日，不猜期货交易日；跨夜盘时宁可少算一天，也不虚报。
    """
    state = _load_state(root)
    try:
        days = int(state.get("watch_days") or 0)
    except (TypeError, ValueError):
        days = 0
    today = date.today().isoformat()
    last_day = str(state.get("last_day") or "")
    if status == "watch":
        if last_day != today:
            days += 1
        _save_state(root, {"last_day": today, "watch_days": days})
        return days
    if status == "ok" and days:
        _save_state(root, {"last_day": today, "watch_days": 0})
        return 0
    return days


def evaluate_health(
    root: Path | str,
    *,
    lookback: int = LOOKBACK,
    min_trades: int = MIN_TRADES,
    winrate_drop: float = WINRATE_DROP,
    pf_ratio: float = PF_RATIO,
    engine: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    cfg: dict[str, Any] | None = None,
    account: str | None = None,
    track_days: bool = False,
) -> HealthReport:
    """
    比较近 N 笔实盘账本和最近一次历史回测。

    不改开平仓。样本不够返回 skip。传入 engine / symbol / timeframe 时先核对口径：
    回测记的是别的策略或品种，就不拿它当对照，显示「未对照」而不是误报偏差。
    传 cfg 时再核对数据源、滑点、成交方式、手数模式与换月链资金；回测缺记的项只作参考。
    实盘样本按品种过滤；account 有值时读指定账本，不传读当前账。
    """
    base = Path(root)
    live = _live_pnls(base, lookback, symbol=str(symbol or ""), account=account)
    perf = _backtest_perf(base)
    scope = _backtest_scope(base)
    wanted: dict[str, Any] = {"engine": engine, "symbol": symbol, "timeframe": timeframe}
    wanted.update(_current_scope(cfg))
    mismatched: list[str] = []
    unknown: list[str] = []
    for key, want in wanted.items():
        if want is None or want == "":
            continue
        have = scope.get(key)
        if have is None or have == "":
            unknown.append(key)
            continue
        if _norm_scope(key, have) != _norm_scope(key, want):
            mismatched.append(key)
    watch_days = _note_watch_day(base, "skip") if track_days else _state_days(base)

    if mismatched:
        diff = "，".join(
            f"{_SCOPE_WORD[key]} {scope.get(key)}≠{wanted.get(key)}" for key in mismatched
        )
        return HealthReport(
            status="skip",
            message=(
                "策略健康：未对照。最近回测是 "
                f"{_scope_text(str(scope.get('engine') or ''), str(scope.get('symbol') or ''), str(scope.get('timeframe') or ''))}，"
                "当前是 "
                f"{_scope_text(str(engine or ''), str(symbol or ''), str(timeframe or ''))}"
                f"（{diff}）。先跑一次同策略、同品种、同周期、同口径的回测再比。"
            ),
            live_n=len(live),
            live_win_rate=_win_rate(live),
            live_pf=_profit_factor(live),
            bt_n=0,
            watch_days=watch_days,
            scope_recorded=True,
            scope_mismatch=True,
        )
    try:
        bt_n = int(perf.get("trades") or 0)
    except (TypeError, ValueError):
        bt_n = 0
    try:
        bt_wr = float(perf.get("win_rate"))
    except (TypeError, ValueError):
        bt_wr = None
    try:
        bt_pf = float(perf.get("profit_factor"))
    except (TypeError, ValueError):
        bt_pf = None

    # 旧回测没记引擎时，只能把数字当参考，不能假装口径已对齐，也不累计停开天数。
    note = ""
    if unknown:
        note = "（最近回测未记录" + "、".join(_SCOPE_WORD[key] for key in unknown) + "，仅供参考）"

    def report(status: str, message: str, **extra: Any) -> HealthReport:
        if unknown:
            days = _state_days(base)
        else:
            days = _note_watch_day(base, status) if track_days else _state_days(base)
        return HealthReport(
            status=status,
            message=message + note,
            watch_days=days,
            scope_recorded=not unknown,
            **extra,
        )

    if len(live) < min_trades:
        return report(
            "skip",
            f"策略健康：账本已平仓 {len(live)} 笔，满 {min_trades} 笔才和回测对照",
            live_n=len(live),
            bt_n=bt_n,
            bt_win_rate=bt_wr,
            bt_pf=bt_pf,
        )
    if bt_n < min_trades or bt_wr is None:
        return report(
            "skip",
            "策略健康：还没有一次成交够数的历史回测，先跑一回再对照",
            live_n=len(live),
            live_win_rate=_win_rate(live),
            live_pf=_profit_factor(live),
            bt_n=bt_n,
            bt_win_rate=bt_wr,
            bt_pf=bt_pf,
        )

    live_wr = _win_rate(live)
    live_pf = _profit_factor(live)
    reasons: list[str] = []
    if live_wr + 1e-9 < bt_wr - winrate_drop:
        reasons.append(
            f"近{len(live)}笔胜率 {live_wr:.0%} ，最近回测 {bt_wr:.0%} ，低了超过 {winrate_drop:.0%}"
        )
    if bt_pf is not None and bt_pf > 0 and live_pf + 1e-9 < bt_pf * pf_ratio:
        reasons.append(
            f"近{len(live)}笔盈利因子 {live_pf:.2f} ，最近回测 {bt_pf:.2f} ，不到一半"
        )
    if reasons:
        if unknown or not track_days:
            days = _state_days(base)
        else:
            days = _note_watch_day(base, "watch")
        tail = f"；连续偏离第 {days} 个交易日" if days else ""
        return report(
            "watch",
            "策略健康偏差（只提醒，不停手" + tail + "）：" + "；".join(reasons),
            live_n=len(live),
            live_win_rate=live_wr,
            live_pf=live_pf,
            bt_n=bt_n,
            bt_win_rate=bt_wr,
            bt_pf=bt_pf,
        )
    return report(
        "ok",
        (
            f"策略健康正常：近{len(live)}笔胜率 {live_wr:.0%} / 盈利因子 {live_pf:.2f}，"
            f"回测 {bt_wr:.0%} / {bt_pf if bt_pf is not None else 0:.2f}"
        ),
        live_n=len(live),
        live_win_rate=live_wr,
        live_pf=live_pf,
        bt_n=bt_n,
        bt_win_rate=bt_wr,
        bt_pf=bt_pf,
    )


def entry_block_decision(
    cfg: dict[str, Any] | None,
    report: HealthReport,
    *,
    required_days: int | None = None,
) -> tuple[bool, str]:
    """
    按配置决定这一轮要不要只停开新仓。

    默认关，行为仍是「只提醒」。打开后还要求连续偏离达到天数，
    且只拦开新仓；止损、反手平仓和收盘清仓不受影响。
    """
    runtime = (cfg or {}).get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    if not config_flag(runtime.get("health_block_entry"), default=False):
        return False, ""
    if report.status != "watch":
        return False, ""
    try:
        need = int(
            runtime.get("health_block_days", WATCH_DAYS_TO_BLOCK)
            if required_days is None
            else required_days
        )
    except (TypeError, ValueError):
        need = WATCH_DAYS_TO_BLOCK
    if need > 0 and report.watch_days >= need:
        return True, (
            f"策略连续 {report.watch_days} 个交易日偏离最近回测（阈值 {need} 天），"
            "按配置停开新仓；止损与清仓不受影响。"
        )
    return False, ""


def maybe_emit_health(
    root: Path | str,
    *,
    engine: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    cfg: dict[str, Any] | None = None,
    account: str | None = None,
) -> HealthReport:
    """评分后，偏差时写入告警文件。十分钟冷却在 emit_alert 里。"""
    report = evaluate_health(
        root,
        engine=engine,
        symbol=symbol,
        timeframe=timeframe,
        cfg=cfg,
        account=account,
        track_days=True,
    )
    if report.status == "watch":
        from src.ops.alerts import emit_alert

        emit_alert("strategy_health", report.message, level="warning", root=root)
    return report
