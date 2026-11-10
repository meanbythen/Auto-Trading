"""
主调度与交易循环
功能：到点登录、拉取K线、跑 Pinbar+SAR 策略、风控后下单/平仓。
原理：schedule 负责定时登录；主循环按间隔扫描各品种；策略只决策，Trader 才执行。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import schedule
from loguru import logger

from src.data import KlineProvider
from src.data.kline import is_bars_stale
from src.data.periods import drop_forming_bar, period_label, stale_after_minutes
from src.login import AppLoginManager
from src.risk import PositionSizer, RiskGuardian
from src.strategy import StrategyAction, StrategyDecision, create_strategy
from src.strategy.generic_stop import GenericStopOverlay
from src.trader import create_trader
from src.trader.ctp_orders import ctp_order
from src.utils.state_store import (
    default_state_path,
    format_position_text,
    load_state,
    restore_positions,
    save_state,
    snapshot_positions,
)
from src.utils.trading_day import futures_trading_day

# events.jsonl 超过这个大小就轮转成 .1，避免长期挂机把单文件撑到几百 MB。
EVENT_LOG_MAX_BYTES = 20 * 1024 * 1024


def _safe_last_alert() -> dict[str, Any] | None:
    """读最近一条告警；文件坏了也不要拖垮运行台刷新。"""
    try:
        from src.ops.alerts import last_alert

        return last_alert()
    except Exception:  # noqa: BLE001
        return None


def _parse_hhmm(value: str) -> dtime:
    """把 '09:30' 转成 time 对象。"""
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def _same_contract(left: object, right: object) -> bool:
    """两个写法是不是同一个合约。郑商所三位和四位年月写法算同一个。"""
    a = str(left or "").strip().lower()
    b = str(right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    product_a = "".join(ch for ch in a if ch.isalpha())
    product_b = "".join(ch for ch in b if ch.isalpha())
    return product_a == product_b and a[-3:] == b[-3:]


# 时段结束时刻常等于最后一根 K 线收线时刻。刚过这一秒再留 90 秒，避免漏掉这根。
_SESSION_END_GRACE_SEC = 90.0


def in_trading_session(
    sessions: list[list[str]],
    now: datetime | None = None,
    *,
    grace_sec: float = _SESSION_END_GRACE_SEC,
) -> bool:
    """判断当前是否在配置的交易时段内（含收线后很短的宽限，夜盘可跨零点）。"""
    from src.utils.session_clock import in_trading_sessions

    return in_trading_sessions(sessions, now, grace_sec=grace_sec)


def _bar_now_from_prefix(bars: Any) -> datetime | None:
    """回溯补开仓时，把策略的「现在」定在信号那根 K 线的时间上。"""
    if bars is None or len(bars) == 0:
        return None
    try:
        import pandas as pd

        ts = pd.Timestamp(bars.index[-1])
        if pd.isna(ts):
            return None
        return ts.to_pydatetime()
    except Exception:  # noqa: BLE001
        return None


def entry_slip_blocked(
    entry_price: float,
    live_price: float,
    atr: float,
    *,
    is_long: bool,
    limit_atr: float,
    limit_points: float = 0.0,
) -> tuple[bool, float, float]:
    """
    开仓滑点闸门：信号价和现价差太多就不追。

    返回 (是否拦下, 反向偏移点数, 允许的上限点数)。
    上限有两种算法，同时开着就取更严的那个：limit_atr×ATR，或直接写死的点数。
    追价成本只算「不利于持仓方向」的那一边：做多现价更高、做空现价更低才算。
    数据不全（价格取不到）时一律放行，宁可漏拦也不要卡死下单。
    """
    if entry_price <= 0 or live_price <= 0:
        return False, 0.0, 0.0
    limit = 0.0
    if limit_atr > 0 and atr > 0:
        limit = float(limit_atr) * float(atr)
    if limit_points > 0:
        limit = min(limit, float(limit_points)) if limit > 0 else float(limit_points)
    if limit <= 0:
        return False, 0.0, 0.0
    chase = (live_price - entry_price) if is_long else (entry_price - live_price)
    return chase > limit, chase, limit


def _last_close(bars: Any) -> float:
    """最后一根K线的收盘价。取不到返回 0。"""
    try:
        if bars is None or len(bars) == 0:
            return 0.0
        return float(bars["close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return 0.0


def _latest_atr(bars: Any, strategy: Any) -> float:
    """按策略自己的 ATR 周期算最新一根的 ATR。取不到返回 0（闸门放行）。"""
    try:
        from src.strategy.indicators import average_true_range

        period = int(getattr(strategy, "atr_period", 14) or 14)
        value = float(
            average_true_range(bars["high"], bars["low"], bars["close"], period=period).iloc[-1]
        )
        return value if value > 0 else 0.0  # NaN 也在这里变成 0
    except Exception:  # noqa: BLE001
        return 0.0


def _eval_now(eval_bars: Any, closed: Any, *, catch_up: bool) -> datetime | None:
    """
    这一次评估该把什么当成「现在」。

    实盘一律用墙上时钟：K线最多比现在晚一个周期，但周末、节假日、午休之后，
    最后一根已收盘K线可能停在上一段的收盘时刻（例如周五 23:00），
    拿它当现在会让 22:55–23:05 的清仓窗口在周一早上被误命中，报「已经到收盘时间」。
    回溯补开仓是另一回事：那时「现在」就是补开那根的时间。
    """
    if catch_up:
        return _bar_now_from_prefix(eval_bars)
    del eval_bars, closed
    return datetime.now()


class TradingBot:
    """把登录、数据、策略、风控、交易串起来的总控制器。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.login_mgr = AppLoginManager(cfg)
        self.risk = RiskGuardian.from_config(cfg)
        self.sizer = PositionSizer(cfg)
        self.trader = create_trader(cfg)
        self.kline = KlineProvider(cfg)
        self.strategy = create_strategy(cfg)
        # 通用止损：独立于策略自身止损，谁先触发就按谁平。
        self.generic_stop = GenericStopOverlay(cfg)

        trade = cfg.get("trade", {})
        self.default_symbol = str(trade.get("symbol", "bu2505"))
        self.volume = int(trade.get("volume", 1))  # 固定手数模式 / 缺止损价时的后备
        self.symbols = list(self.strategy.symbols)
        # 常驻只扫「当前选中」这一个品种：其余关注品种保留在 self.symbols 里，
        # 只给交叉测试等需要多品种的功能用。
        self.scan_symbols = self._pick_scan_symbols()
        self.scan_interval = int(cfg.get("schedule", {}).get("scan_interval_sec", 30))
        from src.utils.session_clock import sessions_for_symbol

        # 交易时段跟当前盯的合约走（品种表优先；白银黄金默认收到 02:30）。
        live_symbol = (self.scan_symbols[0] if getattr(self, "scan_symbols", None) else "") or self.default_symbol
        self.sessions = sessions_for_symbol(cfg, live_symbol)
        self._session_skip_log_at: datetime | None = None
        self._live_session_key = ""
        self.logged_in = False
        self._running = False  # GUI / 外部可调用 stop() 结束循环
        # 暂停报单：扫描还在跑，开平仓、止损、清仓都不发出去。和交易台「暂停报单」同一标志。
        self._orders_paused = False
        # 同花顺真下单前，必须先把价格改成对手价。没改成就不允许点买卖。
        executor_name = str((cfg.get("trade") or {}).get("executor", "sim"))
        self._gui_price_ready = not (executor_name == "gui" and not bool(cfg.get("dry_run", True)))
        self.heartbeat_at: str = ""
        self.last_scan_at: str = ""
        self.last_error: str = ""
        self._health_report: dict[str, Any] | None = None
        # 动态减仓：最近一次说明、各品种已收盘 K 线、已经减过的标记（避免每轮重复减）。
        self._dynamic_note: str = ""
        self._last_signal_bars: dict[str, Any] = {}
        self._dyn_cut_key: str = ""
        # GUI：点击成功后是否立刻当作成交记账（默认 True 可挂机；False=只记「已发单」不改持仓，更安全）
        gui_cfg = (trade.get("gui") or {}) if isinstance(trade, dict) else {}
        self.gui_assume_fill = bool(gui_cfg.get("assume_fill", True))
        # 收盘后关机：每天最多预约一次
        self._shutdown_armed_date: str = ""
        self.max_stale_minutes = float((cfg.get("data") or {}).get("max_stale_minutes", 90))
        # 启动常驻后的第一轮扫描才做回溯补开仓；做完就关掉，避免每轮重复补单。
        self._catch_up_pending = False
        # 收盘清仓：按时钟直接平，不等新K线。每个品种隔多少秒重试一次。
        self._flatten_retry_sec = 10.0
        self._flatten_try: dict[str, float] = {}
        # 到点但还没平掉：这个待办一直保持，直到持仓为空（不再受 10 分钟窗口限制）。
        self._flatten_pending: bool = False
        # 每个品种已经试过几次，只用来降低日志频率，不影响重试。
        self._flatten_attempts: dict[str, int] = {}
        # GUI 点击成功但 assume_fill=false 时，内部持仓不会变。记下已经发过单的品种，
        # 避免清仓阶段每 10 秒重复点一次平仓。
        self._flatten_sent: set[str] = set()
        # 同一根信号 K 线只发一次开仓单（内存记录，不落盘）。
        self._sent_bar: dict[str, str] = {}
        # 已经进入「真正点单」这一步。异常处理靠它判断：还没点单才允许撤销信号标记重试，
        # 点出去之后再撤销会有重复下单的风险。
        self._order_dispatch_started = False
        # 信号账户写失败时置真：还能平仓，但不再开新仓，避免账本和真实仓位越差越远。
        self._ledger_failed = False
        # 盘前行情适配审计：启动时跑一次 + 每天 08:55 / 13:25 / 20:25 各跑一次。
        # 记录上次触发时间避免同一时段重复跑。
        self._last_audit_at: datetime | None = None
        # 审计同一时刻只跑一个线程。候选池大、触发密时不让它们叠加。
        self._audit_thread: threading.Thread | None = None

        root = Path(cfg.get("_project_root", "."))
        self.root = root
        self.event_log = root / "data" / "events.jsonl"
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = default_state_path(root)
        # 下单意图写前日志：点击前先落盘，成功后清除。若程序在点击与记账之间崩溃，
        # 下次启动会看到这个文件，提示人工核对同花顺持仓，避免重复开仓/漏平仓。
        from src.account.workspace import intent_file

        # 点单意图跟当前账走：模拟盘崩溃核对时，不会误用实盘那份意图文件。
        self.intent_path = intent_file(root)
        self._pending_intent: dict[str, Any] | None = None
        # 先记下当前交易日（夜盘 21:00 起算下一个）。读到磁盘后如果文件里已有交易日，再用文件里的。
        self.trading_day = str(futures_trading_day())
        self._load_persisted_state()
        self._check_pending_intent()
        self._warn_gui_safety()
        # 成交回报可能比 place_order 返回更早。先挂上记账函数，避免全部成交后没人改持仓。
        if str(trade.get("executor", "sim")) == "ctp" and hasattr(self.trader, "set_fill_handler"):
            self.trader.set_fill_handler(self._on_ctp_fill)

    def _position_for(self, symbol: str) -> Any | None:
        """按品种找程序记账持仓，大小写不敏感。"""
        positions = getattr(self.strategy, "positions", {}) or {}
        if symbol in positions:
            return positions.get(symbol)
        key = next(
            (name for name in positions if str(name).lower() == str(symbol).lower()),
            None,
        )
        return positions.get(key) if key is not None else None

    def _symbol_is_flat(self, symbol: str) -> bool:
        """程序记账里这个品种是不是空仓。有仓就不做启动回溯。"""
        pos = self._position_for(symbol)
        if pos is None:
            return True
        side = str(getattr(pos, "side", "flat") or "flat")
        volume = int(getattr(pos, "volume", 0) or 0)
        return side == "flat" or volume <= 0

    def flatten_if_due(self, force: bool = False) -> None:
        """
        到「收盘清仓」钟点，按系统时钟直接平仓，不等新K线。

        行情拉不到也要能平：这里不取K线，只看持仓。到点后只要还没平掉，
        每 10 秒再试一次，不会因为 10 分钟窗口走完就放弃。
        策略自己在拿到收盘K线时也会平，这里是按时钟的兜底。
        """
        try:
            in_window = bool(self.strategy.is_flatten_time())
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查收盘清仓钟点失败: {}", exc)
            in_window = False
        due = bool(force or self._flatten_pending or in_window)
        if not due:
            self._flatten_try.clear()
            return
        if force:
            # 新的一轮清仓开始：重新计数、允许重新尝试（上一轮的「已发单」标记作废）。
            self._flatten_sent.clear()
            self._flatten_attempts.clear()
            self._flatten_try.clear()
        # 已经过了 10 分钟窗口、但仓位还没平：只在交易时段内继续重试。
        # 收盘后的夜里不再反复点同花顺，待办保留到下一节开盘再平。
        if not in_window and self.sessions and not in_trading_session(self.sessions):
            return

        mono = time.monotonic()
        remaining = False
        for symbol in self.scan_symbols:
            if self._symbol_is_flat(symbol):
                self._flatten_attempts.pop(symbol, None)
                self._flatten_sent.discard(symbol)
                self._flatten_try.pop(symbol, None)
                continue
            if symbol in self._flatten_sent:
                # GUI 已经点出发单但配置不改内部持仓，别再重复点。
                continue
            remaining = True
            last = float(self._flatten_try.get(symbol, 0.0))
            if last and mono - last < self._flatten_retry_sec:
                continue
            self._flatten_try[symbol] = mono
            tries = int(self._flatten_attempts.get(symbol, 0)) + 1
            self._flatten_attempts[symbol] = tries
            # 不是每次失败都刷屏：第 1 次和之后每分钟一次。
            if tries == 1 or tries % 6 == 0:
                logger.warning("收盘清仓还没完成，第 {} 次按时钟平 {}，不等新K线", tries, symbol)
            try:
                sent = bool(
                    self._execute(
                        StrategyDecision(
                            StrategyAction.CLOSE,
                            symbol,
                            "到收盘清仓钟点，按时钟平仓",
                        )
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("按时钟清仓失败: {}", exc)
                sent = False
            if sent:
                # assume_fill=关：点了平仓也不改程序账，若再点一次可能点出反向仓。
                unconfirmed = not bool(getattr(self, "gui_assume_fill", True))
                if unconfirmed:
                    self._flatten_sent.add(symbol)
                    logger.warning(
                        "{} 收盘清仓已点出发单，程序账未改仓，不再自动连点；请核对同花顺持仓",
                        symbol,
                    )
                    self._record_event(
                        "flatten_sent_unconfirmed",
                        symbol=symbol,
                        attempts=tries,
                        reason="已发单且不假定成交，停止连点",
                    )
                elif self._flatten_retry_limit_reached(symbol):
                    self._flatten_sent.add(symbol)
                    self.last_error = "收盘清仓多次未确认，已停止自动重试"
                    logger.error(
                        "{} 收盘清仓尝试 {} 次仍未确认空仓，先停止自动重试；请人工核对同花顺持仓",
                        symbol,
                        tries,
                    )
                    self._record_event(
                        "flatten_give_up",
                        symbol=symbol,
                        attempts=tries,
                        reason="多次尝试后仍未确认空仓",
                    )
        self._flatten_pending = remaining

    def _flatten_retry_limit_reached(self, symbol: str) -> bool:
        """清仓重试的上限。只在「点了但内部账仍不空」时用，防止整夜反复点平仓。"""
        return int(self._flatten_attempts.get(symbol, 0)) >= 30

    def _has_any_position(self) -> bool:
        """还有没有没平的仓。用于判断清仓失败后要不要继续重试。"""
        return any(not self._symbol_is_flat(symbol) for symbol in self.scan_symbols)

    def _next_flatten_at(self, now: datetime | None = None) -> datetime | None:
        """下一次收盘清仓的钟点。今天还没到就用今天，过了就顺延到明天。"""
        if not getattr(self.strategy, "flatten_at_close", False):
            return None
        clocks: list[Any] = list(getattr(self.strategy, "flatten_times", []) or [])
        if not clocks:
            return None
        now = now or datetime.now()
        soonest: datetime | None = None
        for clock in clocks:
            try:
                when = datetime.combine(now.date(), clock)
            except (TypeError, ValueError):
                continue
            if when <= now:
                when += timedelta(days=1)
            if soonest is None or when < soonest:
                soonest = when
        return soonest

    def _already_took_recent_entry(self, symbol: str, closed: Any, lookback: int) -> bool:
        """
        最近几根信号 K 线里，是不是已经按信号开过仓。

        重启后若磁盘还记着 last_entry_bars，即使持仓字段异常，也不再回溯补同一根。
        """
        last_map = getattr(self.strategy, "_last_entry_bar", None) or {}
        last_id = str(last_map.get(symbol) or "")
        if not last_id:
            key = next(
                (name for name in last_map if str(name).lower() == str(symbol).lower()),
                None,
            )
            last_id = str(last_map.get(key) or "") if key is not None else ""
        if not last_id or closed is None or len(closed) == 0:
            return False
        n = max(1, int(lookback))
        recent = closed.iloc[-n:]
        for label in recent.index:
            if str(label) == last_id:
                return True
        return False

    def _warn_gui_safety(self) -> None:
        """启动时打印 GUI 路径的关键安全提醒。"""
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor != "gui":
            return
        if len(self.scan_symbols) > 1:
            logger.error(
                "GUI 模式会同时扫描多个品种 {}：不会自动切合约，极易点错。"
                "请只保留一个扫描品种。",
                self.scan_symbols,
            )
        gui = (self.cfg.get("trade") or {}).get("gui") or {}
        if not gui.get("symbol_input"):
            logger.warning(
                "未标定 trade.gui.symbol_input：程序不会改合约框，"
                "请确认同花顺盘面合约与策略品种一致。"
            )
        if self.gui_assume_fill and not self.cfg.get("dry_run", True):
            logger.warning(
                "gui.assume_fill=true：点击成功后会立刻改内部持仓"
                "（点击≠交易所成交）。建议定期核对同花顺真实持仓；"
                "也可改为 assume_fill=false，只记发单不自动改仓。"
            )

    # —— 下单意图写前日志（GUI 通道崩溃对账） ——

    _INTENT_HALT_PREFIX = "发现未完成的下单记录"

    def _write_order_intent(self, decision: StrategyDecision, vol: int) -> None:
        """点击前先落盘这一笔委托。成功后由 _clear_order_intent 删除。"""
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "symbol": decision.symbol,
            "side": decision.action.value,
            "volume": int(vol),
            "bar_id": str((decision.meta or {}).get("bar_id", "") or ""),
            "reason": decision.reason,
            "executor": "gui",
        }
        tmp = self.intent_path.with_name(self.intent_path.name + ".tmp")
        try:
            self.intent_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.intent_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写委托意图失败（不影响下单，但崩溃后无法核对）: {}", exc)

    def _clear_order_intent(self) -> None:
        try:
            self.intent_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理委托意图失败: {}", exc)
        self._pending_intent = None

    def _check_pending_intent(self) -> None:
        """启动时检查上次是否崩在「点击后、记账前」。GUI 真下单时熔断等人工核对。"""
        self._pending_intent = None
        if not self.intent_path.exists():
            return
        try:
            data = json.loads(self.intent_path.read_text(encoding="utf-8"))
            self._pending_intent = data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取委托意图失败，按有未完成委托处理: {}", exc)
            self._pending_intent = {}
        if not self._pending_intent:
            self._clear_order_intent()
            return
        trade = self.cfg.get("trade") or {}
        if str(trade.get("executor", "sim")) != "gui" or bool(self.cfg.get("dry_run", True)):
            # 非 GUI 真下单通道由柜台回报/对账兜底，这个文件只是上一次残留。
            self._clear_order_intent()
            return
        message = (
            f"{self._INTENT_HALT_PREFIX}：{self._pending_intent.get('symbol')} "
            f"{self._pending_intent.get('side')} {self._pending_intent.get('volume')} 手 "
            f"（{self._pending_intent.get('ts')}）。程序可能在点击后崩溃。"
            "请先在同花顺核对持仓与委托，再到界面点「对账确认」解除熔断。"
        )
        self.risk.halted = True
        self.risk.halt_reason = message
        self.last_error = message
        logger.error(message)
        self._record_event("pending_intent_found", **self._pending_intent)
        try:
            from src.ops.alerts import emit_alert

            emit_alert("pending_intent", message, level="error")
        except Exception:  # noqa: BLE001
            pass

    def pending_intent_matches(
        self, symbol: str, side: str, volume: int
    ) -> dict[str, Any] | None:
        """
        手工录入的这一笔，如果和遗留委托是同一笔，返回那份委托内容。

        开仓比方向+手数；平仓的手数由程序持仓决定，只比方向。
        """
        intent = self._pending_intent
        if not isinstance(intent, dict) or not intent:
            return None
        if not _same_contract(symbol, str(intent.get("symbol") or "")):
            return None
        if str(intent.get("side") or "") != str(side or ""):
            return None
        if str(side) in {"buy_open", "sell_open"}:
            try:
                want = int(intent.get("volume") or 0)
            except (TypeError, ValueError):
                return None
            if want != int(volume):
                return None
        return dict(intent)

    def confirm_positions_reviewed(self) -> tuple[bool, str]:
        """人工核对同花顺持仓后，清掉遗留委托意图并解除对应熔断。"""
        if self._pending_intent is None:
            return False, "没有待核对的委托记录。"
        intent = dict(self._pending_intent)
        self._clear_order_intent()
        if self.risk.halted and str(self.risk.halt_reason).startswith(self._INTENT_HALT_PREFIX):
            self.risk.halted = False
            self.risk.halt_reason = ""
        self._persist_state()
        self._record_event("position_review_confirmed", **intent)
        return True, "已解除熔断，可以继续扫描。"

    def _record_event(self, event: str, **kwargs: Any) -> None:
        """把关键事件追加写入 data/events.jsonl；文件过大时先轮转。"""
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kwargs}
        try:
            if self.event_log.exists() and self.event_log.stat().st_size >= EVENT_LOG_MAX_BYTES:
                backup = self.event_log.with_name(self.event_log.name + ".1")
                backup.unlink(missing_ok=True)
                self.event_log.replace(backup)
                logger.info("事件日志已轮转: {} -> {}", self.event_log.name, backup.name)
        except OSError as exc:  # noqa: BLE001
            logger.warning("事件日志轮转失败，继续写当前文件: {}", exc)
        with self.event_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _refresh_sessions(self) -> None:
        """每轮按当前合约读品种表里的时段和清仓时刻。"""
        from src.utils.session_clock import (
            flatten_times_for_symbol,
            format_session_text,
            parse_hhmm,
            sessions_for_symbol,
        )

        symbol = (self.scan_symbols[0] if self.scan_symbols else "") or self.default_symbol
        self.sessions = sessions_for_symbol(self.cfg, symbol)
        flats = flatten_times_for_symbol(self.cfg, symbol)
        if hasattr(self.strategy, "entry_sessions"):
            self.strategy.entry_sessions = [
                (parse_hhmm(a), parse_hhmm(b)) for a, b in self.sessions
            ]
        if hasattr(self.strategy, "flatten_times"):
            self.strategy.flatten_times = [parse_hhmm(x) for x in flats]
        if hasattr(self.strategy, "bind_symbol_sessions"):
            self.strategy.bind_symbol_sessions(symbol)
        key = f"{symbol}|{self.sessions}|{flats}"
        if key != self._live_session_key:
            logger.info(
                "{} 交易时段 {} ，清仓 {}",
                symbol,
                format_session_text(self.sessions) or "未写",
                ",".join(flats) or "未写",
            )
            self._live_session_key = key

    def _pick_scan_symbols(self) -> list[str]:
        """常驻扫描清单：只认当前选中的品种（trade.symbol），找不到就退回列表第一个。"""
        if not self.symbols:
            return []
        wanted = str(self.default_symbol or "").strip().lower()
        for symbol in self.symbols:
            if symbol.lower() == wanted:
                return [symbol]
        return [self.symbols[0]]

    def _load_persisted_state(self) -> None:
        """启动时恢复持仓/当日盈亏/开仓计数（主流程序标配）。"""
        st = load_state(self.state_path)
        self.trading_day = str(st.get("trading_day") or futures_trading_day())
        restore_positions(self.strategy, st.get("positions") or {})
        self.risk.daily_pnl = float(st.get("daily_pnl", 0) or 0)
        self.risk.orders_today = int(st.get("orders_today", 0) or 0)
        self.risk.open_orders_today = int(st.get("open_orders_today", self.risk.orders_today) or 0)
        self.risk.halted = bool(st.get("halted", False))
        self.risk.halt_reason = str(st.get("halt_reason", "") or "")
        # 从策略持仓重建风控净仓
        for sym, pos in (getattr(self.strategy, "positions", {}) or {}).items():
            side = getattr(pos, "side", "flat")
            vol = int(getattr(pos, "volume", 0) or 0)
            if side == "long":
                self.risk.positions[str(sym)] = vol
                self.risk.current_position += vol
            elif side == "short":
                self.risk.positions[str(sym)] = -vol
                self.risk.current_position -= vol
        last_bars = st.get("last_entry_bars") or {}
        if isinstance(last_bars, dict) and last_bars:
            # 策略类里不一定预先声明这个字典；重启后要能认出「哪根已经开过」。
            if not hasattr(self.strategy, "_last_entry_bar") or getattr(
                self.strategy, "_last_entry_bar", None
            ) is None:
                self.strategy._last_entry_bar = {}
            self.strategy._last_entry_bar.update({str(k): str(v) for k, v in last_bars.items()})
        # 日内实体等：把「这一段已经开过」灌回去，避免重启后又开第二次。
        self._restore_used_sessions(st)
        # 以信号账户为准对一次账：持仓、风控净仓、当日盈亏都和账本对齐。
        self._reconcile_books()
        logger.info(
            "状态已加载 | 持仓={} | 当日盈亏={:.2f} | 熔断={}",
            snapshot_positions(self.strategy),
            self.risk.daily_pnl,
            self.risk.halted,
        )
        scanned = {s.lower() for s in self.scan_symbols}
        idle_with_position = [
            s for s in self.symbols if s.lower() not in scanned and not self._symbol_is_flat(s)
        ]
        if idle_with_position:
            logger.warning(
                "这些关注品种有持仓但不在常驻扫描里，程序不会自动止损/清仓，请手工处理：{}",
                idle_with_position,
            )

    def _restore_used_sessions(self, st: dict[str, Any]) -> None:
        """
        恢复「日盘/夜盘已用名额」。

        优先读磁盘里的 used_sessions；没有时，用持仓开仓时间或 last_entry_bars 推一段，
        避免旧状态文件升级后同一段再开一枪。
        """
        if not hasattr(self.strategy, "_used_session"):
            return
        used = getattr(self.strategy, "_used_session", None)
        used_bar = getattr(self.strategy, "_used_bar", None)
        if not isinstance(used, dict):
            self.strategy._used_session = {}
            used = self.strategy._used_session
        if not isinstance(used_bar, dict):
            self.strategy._used_bar = {}
            used_bar = self.strategy._used_bar

        raw_used = st.get("used_sessions") or {}
        raw_bars = st.get("used_bars") or {}
        if isinstance(raw_used, dict):
            used.update({str(k): str(v) for k, v in raw_used.items() if v})
        if isinstance(raw_bars, dict):
            used_bar.update({str(k): str(v) for k, v in raw_bars.items() if v})

        # 正在拿着的仓：按开仓时间占掉那一段。
        for sym, pos in (getattr(self.strategy, "positions", {}) or {}).items():
            if str(used.get(sym) or ""):
                continue
            side = getattr(pos, "side", "flat")
            if side not in ("long", "short"):
                continue
            opened = str(getattr(pos, "opened_at", "") or "")
            key = self._session_key_from_timestamp(opened)
            if key:
                used[str(sym)] = key
                used_bar[str(sym)] = used_bar.get(str(sym)) or "restored"

        # 已平仓但磁盘还记着开仓 K 线：同样占名额。
        last_bars = st.get("last_entry_bars") or {}
        if isinstance(last_bars, dict):
            for sym, bar_id in last_bars.items():
                if str(used.get(str(sym)) or ""):
                    continue
                key = self._session_key_from_timestamp(str(bar_id or ""))
                if key:
                    used[str(sym)] = key
                    used_bar[str(sym)] = str(bar_id)

    def _session_key_from_timestamp(self, raw: str) -> str:
        """从开仓时间或 K 线时间推日盘/夜盘名额键，格式与日内实体一致。"""
        text = str(raw or "").strip()
        if not text:
            return ""
        try:
            stamp = pd.Timestamp(text)
        except Exception:  # noqa: BLE001
            return ""
        if stamp is None or pd.isna(stamp):
            return ""
        if stamp.tzinfo is not None:
            stamp = stamp.tz_localize(None)
        clock = stamp.to_pydatetime().time()
        day_open = dtime(9, 0)
        night_open = dtime(21, 0)
        if day_open <= clock < night_open:
            anchor = day_open
        elif clock >= night_open:
            anchor = night_open
        else:
            return ""
        return f"{stamp.date().isoformat()} {anchor.strftime('%H:%M')}"

    def _reconcile_books(self) -> None:
        """
        启动时以信号账户为准对一次账。

        账本还没有成交记录（例如刚开新账期）时跳过，避免把磁盘里的真实持仓清掉。
        """
        from src.account.signal_book import load_book, open_position_rows, today_realized_pnl
        from src.strategy.types import PositionState

        book = load_book(self.root)
        if book is None or not (book.get("trades") or []):
            logger.info("启动对账跳过：信号账户还没有成交记录")
            return

        derived = {row["symbol"]: row for row in open_position_rows(book)}
        positions = getattr(self.strategy, "positions", None)
        if positions is None:
            return

        before = snapshot_positions(self.strategy)
        changed = False
        for sym in sorted(set(positions) | set(derived)):
            current = positions.get(sym)
            cur_side = str(getattr(current, "side", "flat") or "flat") if current else "flat"
            cur_vol = int(getattr(current, "volume", 0) or 0) if current else 0
            want = derived.get(sym)
            if want is None:
                if cur_side in ("long", "short") and cur_vol > 0:
                    positions[sym] = PositionState(symbol=sym)
                    changed = True
                    logger.warning(
                        "启动对账：账本已无 {} 持仓，清掉程序里的 {} {} 手",
                        sym,
                        cur_side,
                        cur_vol,
                    )
                continue
            want_side = str(want["side"])
            want_vol = int(want["volume"])
            if cur_side == want_side and cur_vol == want_vol:
                continue
            stop = float(want.get("stop_price") or 0)
            if stop <= 0 and current is not None and cur_side == want_side:
                stop = float(
                    getattr(current, "trail_stop", 0)
                    or getattr(current, "initial_stop", 0)
                    or 0
                )
            if stop <= 0:
                logger.warning("启动对账：{} 账本开仓没有止损价，重建时先不设止损", sym)
            positions[sym] = PositionState(
                side=want_side,
                volume=want_vol,
                entry_price=float(want.get("entry_price") or 0),
                initial_stop=stop,
                trail_stop=stop,
                trail_armed=bool(stop > 0),
                symbol=sym,
                opened_at=str(want.get("opened_at") or ""),
            )
            changed = True
            logger.warning(
                "启动对账：{} 按账本校准为 {} {} 手 @{}",
                sym,
                want_side,
                want_vol,
                want.get("entry_price"),
            )

        # 风控净仓按策略持仓重建，避免两处再各记一套。
        self.risk.positions = {}
        self.risk.current_position = 0
        for sym, pos in positions.items():
            side = getattr(pos, "side", "flat")
            vol = int(getattr(pos, "volume", 0) or 0)
            if side == "long":
                self.risk.positions[str(sym)] = vol
                self.risk.current_position += vol
            elif side == "short":
                self.risk.positions[str(sym)] = -vol
                self.risk.current_position -= vol

        old_daily = self.risk.daily_pnl
        self.risk.set_daily_pnl(today_realized_pnl(book))
        if changed:
            self._record_event(
                "ledger_reconcile",
                before=before,
                after=snapshot_positions(self.strategy),
            )
            try:
                from src.ops.alerts import emit_alert

                emit_alert(
                    "reconcile",
                    "启动对账发现程序持仓和账本不一致，已按账本校准",
                    level="warning",
                )
            except Exception:  # noqa: BLE001
                pass
        if changed or abs(self.risk.daily_pnl - old_daily) > 0.009:
            self._persist_state()

    def manual_close(
        self,
        symbol: str,
        exit_price: float,
        *,
        reason: str = "按实际成交价平账",
    ) -> tuple[dict[str, Any] | None, float | None]:
        """
        按人工填写的成交价平账，并同时更新策略、风控、bot_state 和信号账户。

        返回 (被平掉的这一笔, 新余额)。账上没有仓或价格无效时返回 (None, None)。
        """
        from src.risk.commission import fee_close, is_close_today
        from src.utils.state_store import flatten_strategy_position

        pos = self._position_for(symbol)
        if pos is None:
            return None, None
        side = str(getattr(pos, "side", "flat") or "flat")
        volume = int(getattr(pos, "volume", 0) or 0)
        if side not in ("long", "short") or volume <= 0:
            return None, None
        opened_at = str(getattr(pos, "opened_at", "") or "")

        closed = flatten_strategy_position(self.strategy, symbol)
        if closed is None:
            return None, None
        booked_symbol = str(closed.get("symbol") or symbol)
        self.risk.on_order_filled("close", volume, symbol=booked_symbol)

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        fee = fee_close(
            contracts,
            booked_symbol,
            volume,
            close_today=is_close_today(opened_at, datetime.now()),
        )
        decision = StrategyDecision(
            StrategyAction.CLOSE,
            booked_symbol,
            reason,
            entry_price=float(exit_price),
        )
        balance = self._record_signal_account(
            booked_symbol,
            decision,
            volume,
            float(exit_price),
            side,
            fee=fee,
            fill_key=f"manual-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        )
        if self._uses_ctp_broker_funds():
            self._apply_broker_equity()
        elif balance is not None:
            from src.account.signal_book import load_book, today_realized_pnl

            book = load_book(self.root)
            if book is not None:
                self.risk.set_daily_pnl(today_realized_pnl(book))

        self._persist_state()
        self._record_event(
            "manual_close",
            symbol=booked_symbol,
            side=side,
            volume=volume,
            price=float(exit_price),
            balance=balance,
        )
        return closed, balance

    def manual_open(
        self,
        symbol: str,
        side: str,
        volume: int,
        entry_price: float,
        *,
        initial_stop: float = 0.0,
        reason: str = "手工录入开仓",
    ) -> tuple[dict[str, Any] | None, float | None]:
        """
        人工在同花顺上开了仓后，把这笔仓回填进程序的策略内存、风控、bot_state 和信号账户。

        side: "long" / "short"。只改程序的账，不会去点同花顺。
        initial_stop: 可选的策略级止损价（填了之后策略会按规则自动止损；0 只靠反向信号和收盘清仓）。
        返回 (写进去的这一笔, 新余额)。参数无效或品种不在关注列表里时返回 (None, None)。
        """
        from src.risk.commission import fee_open
        from src.utils.state_store import strategy_position_open

        if side not in ("long", "short") or int(volume) <= 0 or float(entry_price) <= 0:
            return None, None

        # 品种必须在程序内存里已有 position state（启动时就初始化了），
        # 否则 runner 下一轮扫描可能把这笔仓当成"程序没有的"处理掉。
        positions = getattr(self.strategy, "positions", None) or {}
        key = next((name for name in positions if str(name).lower() == str(symbol).lower()), None)
        if key is None:
            return None, None

        direction = "多" if side == "long" else "空"
        booked_symbol = str(key)

        ok = strategy_position_open(
            self.strategy, booked_symbol, side, int(volume), float(entry_price),
            initial_stop=float(initial_stop or 0),
        )
        if not ok:
            return None, None

        self.risk.on_order_filled("open", int(volume), symbol=booked_symbol)

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        fee = fee_open(contracts, booked_symbol, int(volume))

        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        decision = StrategyDecision(
            action,
            booked_symbol,
            reason,
            entry_price=float(entry_price),
            stop_price=float(initial_stop or 0),
        )
        balance = self._record_signal_account(
            booked_symbol,
            decision,
            int(volume),
            float(entry_price),
            "flat",  # 开仓前是空仓
            fee=fee,
            fill_key=f"manual-open-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        )

        self._persist_state()
        self._record_event(
            "manual_open",
            symbol=booked_symbol,
            side=side,
            volume=int(volume),
            price=float(entry_price),
            balance=balance,
        )
        return (
            {
                "symbol": booked_symbol,
                "side": side,
                "volume": int(volume),
                "entry_price": float(entry_price),
            },
            balance,
        )

    def _persist_state(self) -> None:
        """把关键运行态写盘。"""
        save_state(
            self.state_path,
            {
                "trading_day": self.trading_day,
                "daily_pnl": self.risk.daily_pnl,
                "orders_today": self.risk.orders_today,
                "open_orders_today": self.risk.open_orders_today,
                "halted": self.risk.halted,
                "halt_reason": self.risk.halt_reason,
                "positions": snapshot_positions(self.strategy),
                "last_entry_bars": dict(getattr(self.strategy, "_last_entry_bar", {}) or {}),
                "used_sessions": dict(getattr(self.strategy, "_used_session", {}) or {}),
                "used_bars": dict(getattr(self.strategy, "_used_bar", {}) or {}),
                "heartbeat_at": self.heartbeat_at,
                "last_scan_at": self.last_scan_at,
                "last_error": self.last_error,
                "symbols": self.symbols,
                "dry_run": bool(self.cfg.get("dry_run", True)),
                "executor": str((self.cfg.get("trade") or {}).get("executor", "")),
            },
        )

    def _rollover_trading_day(self) -> bool:
        """
        常驻运行跨交易日时清零日内计数。

        夜盘 21:00 就是下一个交易日的开始，不是等到零点。挂机过夜时 daily_pnl、
        开仓次数、熔断会一直沿用上一个交易日，第二天可能一开盘就拒绝开仓。
        持仓和账本不动：跨日夜的单子、开仓价、止损都保留。
        只往前推进：周末顺延后候选日期可能没有变化，或系统时间被往回拨，都不重复清零。
        """
        candidate = futures_trading_day()
        try:
            current = date.fromisoformat(str(self.trading_day)[:10])
        except ValueError:
            current = None
        if current is not None and candidate <= current:
            return False
        today = str(candidate)
        old = self.trading_day
        old_pnl = float(self.risk.daily_pnl or 0)
        self.trading_day = today
        self.risk.daily_pnl = 0.0
        self.risk.orders_today = 0
        self.risk.open_orders_today = 0
        self.risk.halted = False
        self.risk.halt_reason = ""
        self.risk.last_order_ts = 0.0
        self.risk.last_open_ts = 0.0
        # 隔夜持仓不归零，只把切换时的仓位留一份记录，方便第二天对账。
        carried = snapshot_positions(self.strategy)
        held_text = format_position_text(carried)
        logger.info(
            "交易日切换 {} → {}，已清零当日盈亏、下单次数和熔断；"
            "昨日盈亏 {:+.2f}，隔夜持仓保留: {}",
            old,
            today,
            old_pnl,
            held_text,
        )
        self._record_event(
            "day_rollover",
            old_day=old,
            new_day=today,
            daily_pnl_before=old_pnl,
            positions=carried,
        )
        self._persist_state()
        return True

    def _contract_multiplier(self, symbol: str) -> float:
        return float(self.sizer.multiplier_for(symbol))

    # —— 盘前行情适配审计 ——

    def _maybe_run_session_audit(self) -> None:
        """定时触发：盘前 08:55 / 13:25 / 20:25 各跑一次审计。"""
        from src.strategy.session_audit import (
            should_run_session_audit,
        )

        now = datetime.now()
        if should_run_session_audit(now, self._last_audit_at):
            self._run_session_audit(f"盘前定时（{now.strftime('%H:%M')}）")

    def _audit_bars_provider(self):
        """复用 runner 的单例 KlineProvider，避免审计每轮新建连接。"""
        from src.strategy.session_audit import make_bars_provider

        return make_bars_provider(self.cfg, provider=self.kline)

    def _run_session_audit(self, trigger: str = "") -> None:
        """立即跑一次盘前行情适配审计，结果写日志。

        独立线程执行，K 线拉取有网络延迟时不阻塞主扫描循环。
        """

        def _thread() -> None:
            try:
                from src.strategy.session_audit import (
                    format_audit_report,
                    run_pre_session_audit,
                )

                rows = run_pre_session_audit(
                    self.cfg,
                    bars_provider=self._audit_bars_provider(),
                )
                header = f"[盘前审计·{trigger}]" if trigger else "[盘前审计]"
                for line in format_audit_report(rows):
                    logger.info(line)
            except Exception as exc:  # noqa: BLE001
                logger.warning("盘前行情审计跳过（不影响主流程）：{}", exc)

        if self._audit_thread is not None and self._audit_thread.is_alive():
            # 上一次还没跑完，这次不再开第二条线程，也不刷新触发时间，稍后自然重试。
            logger.info("上一次盘前审计还在跑，这次跳过。")
            return
        self._last_audit_at = datetime.now()
        self._audit_thread = threading.Thread(target=_thread, name="SessionAudit", daemon=True)
        self._audit_thread.start()

    def _dynamic_status_text(self) -> str:
        """运行台动态减仓一行。算不出波动时只看连亏。"""
        from src.risk.dynamic import atr_ratio, describe_status, live_equity_and_peak, DynamicExposure

        dyn = DynamicExposure.from_config(self.cfg)
        bars = next(iter(self._last_signal_bars.values()), None) if self._last_signal_bars else None
        ratio = atr_ratio(bars, period=dyn.atr_period, lookback=dyn.atr_lookback) if bars is not None else None
        equity, peak = live_equity_and_peak(self.root)
        return describe_status(
            self.cfg,
            ratio=ratio,
            last_note=str(getattr(self, "_dynamic_note", "") or ""),
            equity=equity,
            peak=peak,
        )

    def _maybe_dynamic_cut(self, symbol: str) -> Any:
        """
        账户从最高点回撤够深时，把已经拿着的 2 手及以上减到一半（至少留 1 手）。

        返回一条平仓决策；不该减时返回 None。同一档回撤只减一次。
        """
        from src.risk.dynamic import DynamicExposure, drawdown_frac, live_equity_and_peak
        from src.strategy.types import StrategyAction, StrategyDecision

        dyn = DynamicExposure.from_config(self.cfg)
        if not dyn.enabled or not dyn.reduce_held:
            return None
        pos = self._position_for(symbol)
        held = abs(int(pos.volume)) if pos and getattr(pos, "volume", 0) else 0
        if held < 2:
            return None
        equity, peak = live_equity_and_peak(self.root)
        dd = drawdown_frac(equity, peak)
        target = dyn.held_target(held, dd)
        if target is None or target >= held:
            return None
        key = f"{symbol}|{dd}|{target}"
        if getattr(self, "_dyn_cut_key", "") == key:
            return None
        cut = held - target
        self._dyn_cut_key = key
        pct = (dd or 0) * 100
        note = f"账户回撤{pct:.1f}%，持仓从{held}手减到{target}手"
        self._dynamic_note = note
        logger.warning("{} {}", symbol, note)
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            note,
            meta={"close_volume": cut},
        )

    def _maybe_check_health(self) -> None:
        """近 N 笔对照最近回测。只报警，不改变这一轮开平仓。"""
        try:
            from src.ops.health import maybe_emit_health
            from src.utils.paths import project_root

            report = maybe_emit_health(project_root())
            self._health_report = report.as_dict()
        except Exception as exc:  # noqa: BLE001
            logger.debug("策略健康检查跳过：{}", exc)

    def status_snapshot(self) -> dict[str, Any]:
        """给界面看的健康快照。"""
        return {
            "running": self._running,
            "orders_paused": bool(self._orders_paused),
            "ctp_flow": self._ctp_flow_text(),
            "heartbeat_at": self.heartbeat_at,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
            "daily_pnl": self.risk.daily_pnl,
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "positions": snapshot_positions(self.strategy),
            "symbols": self.symbols,
            "last_alert": _safe_last_alert(),
            "health": getattr(self, "_health_report", None),
            "dynamic": self._dynamic_status_text(),
        }

    def _apply_fill_accounting(
        self,
        symbol: str,
        decision: StrategyDecision,
        vol: int,
        entry_price: float,
        fill_price: float,
        *,
        fill_key: str = "",
        stop_price: float | None = None,
    ) -> None:
        """更新策略仓位、风控、信号账户，并且只落盘一次。"""
        # 同一份回报再来一次时，账本会跳过。这里也直接返回，避免把仓位再改一遍。
        if fill_key and not self.cfg.get("dry_run", True):
            from src.account.signal_book import load_book

            book = load_book(self.root)
            if book is not None and any(
                str(row.get("fill_key") or "") == fill_key
                for row in book.get("trades") or []
            ):
                logger.info("同一笔成交回报已记过，跳过 | {} {}", symbol, fill_key)
                self._record_event(
                    "ledger_skip",
                    symbol=symbol,
                    side=decision.action.value,
                    volume=vol,
                    fill_key=fill_key,
                )
                return

        pos_before = (getattr(self.strategy, "positions", {}) or {}).get(symbol)
        side_before = getattr(pos_before, "side", "flat") if pos_before else "flat"
        entry_before = float(getattr(pos_before, "entry_price", 0) or 0) if pos_before else entry_price

        from src.risk.commission import fee_close, fee_open, is_close_today

        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        fee = 0.0
        gross = 0.0
        if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            fee = fee_open(contracts, symbol, vol)
        elif decision.action == StrategyAction.CLOSE and entry_before > 0 and fill_price > 0:
            mult = self._contract_multiplier(symbol)
            if side_before == "long":
                gross = (fill_price - entry_before) * vol * mult
            elif side_before == "short":
                gross = (entry_before - fill_price) * vol * mult
            opened_at = str(getattr(pos_before, "opened_at", "") or "") if pos_before else ""
            today = is_close_today(opened_at, datetime.now())
            fee = fee_close(contracts, symbol, vol, close_today=today)

        # 1) 先写信号账户。实盘以这本账为准；dry_run 只看信号，不写账。
        balance = None
        if not self.cfg.get("dry_run", True):
            balance = self._record_signal_account(
                symbol,
                decision,
                vol,
                fill_price,
                side_before,
                fee=fee,
                fill_key=fill_key,
                stop_price=stop_price,
            )

        # 2) 再同步策略持仓和风控净仓。
        self.strategy.on_filled(
            symbol,
            decision.action,
            fill_price,
            vol,
            initial_stop=decision.stop_price,
        )
        if decision.action == StrategyAction.CLOSE and hasattr(self.strategy, "on_trade_closed"):
            try:
                self.strategy.on_trade_closed(
                    symbol,
                    float(gross) - float(fee),
                    volume=int(vol),
                    side=str(side_before or ""),
                    entry_price=float(entry_before or 0),
                    exit_price=float(fill_price or 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("{} 平仓后进化没跑完: {}", symbol, exc)
        if decision.action == StrategyAction.CLOSE and hasattr(self.strategy, "note_factor_pnl"):
            try:
                self.strategy.note_factor_pnl(
                    symbol,
                    float(gross) - float(fee),
                    volume=int(vol),
                    multiplier=float(self._contract_multiplier(symbol) or 0),
                    side=str(side_before or ""),
                    entry_time=str(getattr(pos_before, "opened_at", "") or "") if pos_before else "",
                    exit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    entry_price=float(entry_before or 0),
                    exit_price=float(fill_price or 0),
                    reason_exit=str(decision.reason or ""),
                    commission=float(fee or 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("{} 因子盈亏分摊没记下: {}", symbol, exc)
        bar_id = str((decision.meta or {}).get("bar_id", ""))
        if bar_id:
            # 各策略不一定自带这个字典。统一挂上，重启后才能认出「这根已经开过」。
            if not hasattr(self.strategy, "_last_entry_bar") or getattr(
                self.strategy, "_last_entry_bar", None
            ) is None:
                self.strategy._last_entry_bar = {}
            self.strategy._last_entry_bar[symbol] = bar_id

        self.risk.on_order_filled(decision.action.value, vol, symbol=symbol)

        # 滑点记录：理论价（开仓=信号入场价，止损平仓=止损价）对比实际成交价，追加到 data/slippage_log.csv。
        # 只记录，不参与任何决策；出错不能影响记账。
        try:
            from src.account.slippage_log import record_fill
            from src.risk.universe_table import product_key

            theory = None
            if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
                theory = float(decision.entry_price or 0) or None
            elif decision.action == StrategyAction.CLOSE and decision.stop_price:
                theory = float(decision.stop_price)
            tick_box = contracts.get(product_key(symbol)) if isinstance(contracts, dict) else {}
            tick_raw = (tick_box or {}).get("price_tick")
            if tick_raw in (None, ""):
                trade_box = self.cfg.get("trade") or {}
                ctp_box = trade_box.get("ctp") if isinstance(trade_box.get("ctp"), dict) else {}
                tick_raw = ctp_box.get("price_tick", trade_box.get("price_tick", 1))
            slip = record_fill(
                self.root,
                symbol=symbol,
                action=decision.action.value,
                volume=vol,
                theory_price=theory,
                fill_price=fill_price,
                tick=float(tick_raw or 1),
                side_before=side_before,
                executor=str((self.cfg.get("trade") or {}).get("executor", "")),
                dry_run=bool(self.cfg.get("dry_run", True)),
            )
            if slip is not None:
                logger.info("滑点记录 | {} {} 理论 {} 成交 {} | {:+.2f} 跳", symbol, decision.action.value, theory, fill_price, slip)
        except Exception:  # noqa: BLE001
            logger.debug("滑点记录失败，已忽略", exc_info=True)

        # 3) 当日盈亏：CTP 跟柜台（下一轮扫描对账时刷新，成交回调里不再查一次，以免拖住报单）。
        # 其它通道跟账本。账本没记上时退回原算法。
        if self._uses_ctp_broker_funds():
            self._apply_broker_equity()
        elif balance is not None:
            from src.account.signal_book import load_book, today_realized_pnl

            book = load_book(self.root)
            if book is not None:
                self.risk.set_daily_pnl(today_realized_pnl(book))
        elif decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            if fee > 0:
                self.risk.update_pnl(-fee)
                self._record_event(
                    "commission",
                    symbol=symbol,
                    fee=fee,
                    offset="开仓",
                    daily_pnl=self.risk.daily_pnl,
                )
        elif decision.action == StrategyAction.CLOSE and entry_before > 0 and fill_price > 0:
            pnl = gross - fee
            self.risk.update_pnl(pnl)
            self._record_event(
                "pnl",
                symbol=symbol,
                pnl=pnl,
                fee=fee,
                daily_pnl=self.risk.daily_pnl,
            )

        # 模拟测试也会走到这里，用来看内存里的仓对不对。不能把假成交写进正在用的账，也不能盖掉真实持仓文件。
        if self.cfg.get("dry_run", True):
            return
        self._persist_state()

    def _record_signal_account(
        self,
        symbol: str,
        decision: StrategyDecision,
        vol: int,
        fill_price: float,
        side_before: str,
        *,
        fee: float = 0.0,
        fill_key: str = "",
        stop_price: float | None = None,
    ) -> float | None:
        """
        把这一笔写进信号账户，返回记完后的余额。

        只在程序自己已经记账之后调用。下单失败、只点了但没确认成交，都不会走到这里。
        模拟模式直接返回，避免完整模拟测试把假价格写进当前账本。
        开仓本身不改余额（手续费会扣）；平仓后余额变了，才写回配置里的账户权益。
        同一份回报用同一个 fill_key 再来一次时，账本会跳过，这里返回 None。
        """
        if decision.action == StrategyAction.CLOSE:
            if side_before == "long":
                direction = "多"
            elif side_before == "short":
                direction = "空"
            else:
                return None
            offset = "平仓"
        elif decision.action == StrategyAction.BUY_OPEN:
            direction = "多"
            offset = "开仓"
        elif decision.action == StrategyAction.SELL_OPEN:
            direction = "空"
            offset = "开仓"
        else:
            return None
        if self.cfg.get("dry_run", True):
            return None
        try:
            from src.account.signal_book import record_program_trade, write_capital_to_config

            before = float(self.sizer.capital)
            balance = record_program_trade(
                self.root,
                initial_capital=before,
                symbol=symbol,
                offset=offset,
                direction=direction,
                price=fill_price,
                volume=vol,
                multiplier=self._contract_multiplier(symbol),
                reason=str(decision.reason or ""),
                bar_id=str((decision.meta or {}).get("bar_id") or ""),
                commission=float(fee or 0),
                fill_key=fill_key,
                stop_price=stop_price,
            )
            if balance is None:
                self._record_event(
                    "ledger_skip",
                    symbol=symbol,
                    side=decision.action.value,
                    volume=vol,
                    fill_key=str(fill_key or ""),
                )
                return None
            # CTP 常驻时，手数和配置权益跟期货公司动态权益，不把程序估的成交盈亏写进去。
            # 成交记录仍记下，方便账户分析看开平仓；权益等柜台查询回来再对齐。
            if self._uses_ctp_broker_funds():
                self._ledger_failed = False
                return balance
            if abs(balance - before) > 0.009:
                self.sizer.capital = balance
            self.cfg.setdefault("risk", {})["capital"] = balance
            write_capital_to_config(balance)
            self._ledger_failed = False
            return balance
        except Exception as exc:  # noqa: BLE001
            self._ledger_failed = True
            self.last_error = "信号账户写入失败"
            logger.exception("信号账户记账失败，已暂停新开仓（仍可平仓）: {}", exc)
            self._record_event(
                "ledger_error",
                symbol=symbol,
                side=decision.action.value,
                volume=vol,
                error=str(exc),
            )
            return None

    def do_login(self) -> None:
        """登录。只打开软件和登录框，不点买开、卖开、平仓。点菜单前会先看下单窗口。"""
        logger.info("=== 登录前先看下单窗口 ===")
        ok = self.login_mgr.login()
        self.logged_in = ok
        self._record_event("login", ok=ok)
        # 同一条线程里，登录返回之后才会进入下一轮扫描，所以登录过程中不会同时去点下单。
        self._arm_order_price()

    def _arm_order_price(self) -> None:
        """
        启动常驻后把同花顺价格设成对手价。

        模拟模式只看信号、不点鼠标，所以这里也不去点价格框。
        设失败只写警告，扫描照常开始。后面的开仓和平仓都不会再改价格。
        """
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor != "gui":
            self._gui_price_ready = True
            return
        if self.cfg.get("dry_run", True):
            self._gui_price_ready = True
            logger.info("模拟模式不点击同花顺。真下单前请确认价格框是「对手价」。")
            return
        try:
            from src.trader.price_mode import ensure_opponent_price

            ok, msg = ensure_opponent_price(self.cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("设置对手价失败")
            ok, msg = False, str(exc)
        self._gui_price_ready = bool(ok)
        if ok:
            logger.info(msg)
        else:
            logger.warning("启动时对手价没有设好。出现信号时还会再看一次价格框。{}", msg)

    def _size_open(self, decision: StrategyDecision) -> tuple[int, str]:
        """
        开仓手数：先按资金管理算，策略指定的手数只能往下压。

        长线趋势等策略会在 meta 里写「单元手数×加仓次数」，这个数不能绕过
        每笔风险%和仓位上限，否则小权益下会开出买不起的仓。
        """
        meta = decision.meta or {}
        sized = self.sizer.calc_open(
            decision.symbol,
            decision.entry_price,
            decision.stop_price,
            current_abs_position=self.risk.total_abs_position(),
            positions=self.risk.positions,
        )
        raw = meta.get("volume")
        if raw is None:
            vol, note = sized.volume, sized.reason
        else:
            try:
                want = max(0, int(raw))
            except (TypeError, ValueError):
                return sized.volume, f"{sized.reason}（策略手数无效，按风控）"
            if want <= 0:
                return 0, f"策略指定{want}手"
            vol = min(sized.volume, want)
            note = f"策略指定{want}手，风控允许{sized.volume}手 → 取小{vol}手（{sized.reason}）"
        scale = meta.get("size_scale")
        if scale is not None and vol > 0:
            try:
                ratio = float(scale)
            except (TypeError, ValueError):
                ratio = 1.0
            else:
                if ratio <= 0:
                    return 0, f"{note}；信号强度×{ratio:.2f}→0手"
                if ratio < 0.999:
                    scaled = max(1, int(vol * ratio))
                    vol = min(vol, scaled)
                    note = f"{note}；信号强度×{ratio:.2f}→{vol}手"
        from src.risk.dynamic import apply_dynamic_open, live_equity_and_peak

        bars = self._last_signal_bars.get(decision.symbol)
        equity, peak = live_equity_and_peak(self.root)
        adj, dyn_note = apply_dynamic_open(
            self.cfg,
            vol,
            bars,
            equity=equity,
            peak=peak,
        )
        if dyn_note:
            self._dynamic_note = dyn_note
            note = f"{note}；{dyn_note}" if note else dyn_note
        return adj, note

    def set_orders_paused(self, paused: bool) -> None:
        """运行页「暂停报单」：扫描继续，柜台和同花顺都不发单。"""
        self._orders_paused = bool(paused)
        gateway = getattr(self.trader, "gateway", None)
        if gateway is not None and hasattr(gateway, "trading_paused"):
            gateway.trading_paused = self._orders_paused
        logger.warning("已暂停报单" if self._orders_paused else "已恢复报单")

    def _ctp_flow_text(self) -> str:
        """今日报撤笔数。到阈值只提醒。"""
        activity = getattr(getattr(self.trader, "gateway", None), "activity", None)
        if activity is None or not hasattr(activity, "describe"):
            return ""
        return str(activity.describe() or "")

    def _execute(self, decision: StrategyDecision) -> bool:
        """把策略决策交给风控 + 交易通道。返回 True 表示这一笔已经发出（不代表成交）。"""
        self._order_dispatch_started = False
        if decision.action == StrategyAction.HOLD:
            logger.debug("{}: {}", decision.symbol, decision.reason)
            return False
        if self._orders_paused:
            logger.warning(
                "已暂停报单，本笔没有发出 | {} {}",
                decision.symbol,
                decision.action.value,
            )
            self._record_event(
                "order_paused",
                symbol=decision.symbol,
                side=decision.action.value,
            )
            return False

        # 信号账户写失败后不再开新仓（平仓仍放行），避免账本和真实仓位继续偏离。
        if getattr(self, "_ledger_failed", False) and decision.action in (
            StrategyAction.BUY_OPEN,
            StrategyAction.SELL_OPEN,
        ):
            message = "信号账户上一次写入失败，暂不允许开新仓；平仓不受影响。请检查磁盘空间和 data 目录权限。"
            logger.error("{} | {}", decision.symbol, message)
            self.last_error = message
            self._record_event("ledger_error_block", symbol=decision.symbol, side=decision.action.value)
            return False

        # 同一根信号 K 线只开一次。扫描间隔 30 秒时，一根 K 线可能被两轮扫描看到；
        # 第一轮点击实际成功但返回失败时，不能在同一分钟里再点一次。
        bar_key = str((decision.meta or {}).get("bar_id", "") or "")
        if decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN) and bar_key:
            if self._sent_bar.get(decision.symbol) == bar_key:
                logger.info("{} 这根K线已经发过开仓单，跳过重复发单 | {}", decision.symbol, bar_key)
                self._record_event("order_dedup", symbol=decision.symbol, bar_id=bar_key)
                return False

        # 均线/SuperTrend 等策略报了「反手」时，先平掉原来的仓，再开新方向。平仓没完成就不再开。
        pos_now = self._position_for(decision.symbol)
        want = None
        if decision.action == StrategyAction.BUY_OPEN:
            want = "long"
        elif decision.action == StrategyAction.SELL_OPEN:
            want = "short"
        if (
            want
            and pos_now
            and pos_now.side not in ("flat", want)
            and (decision.meta or {}).get("reverse")
        ):
            self._execute(
                StrategyDecision(
                    StrategyAction.CLOSE,
                    decision.symbol,
                    "先平掉原来的仓，再开反向",
                    entry_price=decision.entry_price,
                    stop_price=decision.stop_price,
                )
            )
            pos_now = self._position_for(decision.symbol)
            if pos_now and pos_now.side != "flat":
                logger.warning("反向开仓的平仓没有完成，本根不再开新仓")
                return False

        side = decision.action.value
        size_note = ""
        meta = decision.meta or {}
        if decision.action == StrategyAction.CLOSE:
            pos = self._position_for(decision.symbol)
            held = abs(int(pos.volume)) if pos and pos.volume else 0
            if held <= 0:
                # 程序账上没有仓就不发平仓单：不拿配置手数去猜，避免平出反向仓。
                logger.warning(
                    "策略要求平仓，但程序账上无持仓，跳过 {} | {}",
                    decision.symbol,
                    decision.reason,
                )
                self._record_event(
                    "close_skip_no_position",
                    symbol=decision.symbol,
                    reason=decision.reason,
                )
                return False
            # 长线减仓会在 meta 里写要平几手；没有就全平。
            raw_cut = meta.get("close_volume", meta.get("volume"))
            if raw_cut is not None:
                try:
                    vol = max(1, min(held, int(raw_cut)))
                    size_note = f"策略指定平{vol}手"
                except (TypeError, ValueError):
                    vol = held
            else:
                vol = held
        else:
            # 开仓/加仓：策略手数作为上限，实际手数不能超过资金管理算出的值。
            vol, size_note = self._size_open(decision)
            if vol <= 0:
                logger.warning("仓位为0，跳过开仓 {}: {}", decision.symbol, size_note)
                self._record_event(
                    "size_skip",
                    symbol=decision.symbol,
                    side=side,
                    reason=size_note,
                )
                return

        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor == "ctp":
            from src.trader.go_live import strategy_order_block_reason

            blocked_stage = strategy_order_block_reason(self.cfg)
            if blocked_stage:
                logger.warning(blocked_stage)
                self._record_event("order_blocked_stage", symbol=decision.symbol, side=side, reason=blocked_stage)
                return
        # 上一笔还在柜台里时，先别再报。避免断线重连后，或限价还挂着时，重复开平。
        if executor == "ctp" and getattr(self.trader, "has_working_order", lambda _symbol: False)(decision.symbol):
            logger.warning("上一笔 {} 还没有最终回报，先不报新单", decision.symbol)
            self._record_event("order_working", symbol=decision.symbol, side=side, reason="等待上一笔回报")
            return

        # 柜台持仓和程序记账不一致时，只停开仓。资金已按柜台改过本地，不在这里拦。平仓仍然要能发出去。
        if (
            executor == "ctp"
            and decision.action != StrategyAction.CLOSE
            and getattr(self.trader, "blocks_new_opens", lambda: False)()
        ):
            note = getattr(self.trader, "_last_note", "柜台对账未通过")
            logger.warning("停止开新仓 {}: {}", decision.symbol, note)
            self._record_event("risk_block", symbol=decision.symbol, side=side, reason=note)
            return

        allowed, reason = self.risk.allow(side, vol, symbol=decision.symbol)
        if not allowed:
            logger.warning("风控拒绝 {}: {}", decision.symbol, reason)
            self._record_event("risk_block", symbol=decision.symbol, side=side, reason=reason)
            return

        # 对手价只在真正下单时看一次。下单会先把同花顺拉到最前，没有交易窗口才登录。
        # 不在这里提前读价格，免得日志刷新把本程序叫到前面，也免得价格菜单被点开两次。

        # 同花顺点击时永远不把策略里的价格写进价格框。启动时已经选好对手价。
        # CTP 必须带限价。平仓策略通常不给价格，就用这根 K 线的收盘价。
        position_side = None
        if executor == "ctp":
            price = self._ctp_limit_price(decision)
            if price is None or price <= 0:
                logger.error("CTP 没有可用价格，这次不报单 | {}", decision.symbol)
                self._record_event("order_no_price", symbol=decision.symbol, side=side)
                return
            if decision.action == StrategyAction.CLOSE:
                held = (getattr(self.strategy, "positions", {}) or {}).get(decision.symbol)
                position_side = getattr(held, "side", None)
        elif executor == "gui" or decision.action == StrategyAction.CLOSE:
            price = None
        else:
            price = decision.entry_price

        # GUI 真下单先落一笔「意图」，避免点击成功后程序崩溃、重启又开一次。
        if executor == "gui" and not self.cfg.get("dry_run", True):
            self._write_order_intent(decision, vol)

        # 从这里开始真正点单。下面的异常再撤销信号标记就可能重复下单，所以先置位。
        self._order_dispatch_started = True
        if executor == "ctp":
            # 和交易台同一份 OrderRequest、同一个 place_order。价格来自策略/最新收盘，人点时来自盘口。
            result = self.trader.place_order(
                ctp_order(
                    decision.symbol,
                    decision.action.value,  # type: ignore[arg-type]
                    vol,
                    float(price),
                    position_side=position_side,
                    stop_price=decision.stop_price,
                    bar_id=str((decision.meta or {}).get("bar_id", "") or ""),
                    remark=decision.reason,
                )
            )
        elif decision.action == StrategyAction.BUY_OPEN:
            result = self.trader.buy_open(decision.symbol, vol, price)
        elif decision.action == StrategyAction.SELL_OPEN:
            result = self.trader.sell_open(decision.symbol, vol, price)
        else:
            result = self.trader.close_position(decision.symbol, vol, price)

        if executor == "ctp":
            self._finish_ctp_order(decision, vol, result, side, size_note)
            # CTP 是异步发单：result.ok 表示已报入柜台。只有成功报入才标记 sent，
            # 没发出去（缺配置、前置没连上、暂停报单）的话下一轮应该再试。
            if result.ok and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN) and bar_key:
                self._sent_bar[decision.symbol] = bar_key
            elif decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
                self._release_signal_mark(decision)
            return bool(getattr(result, "ok", False))

        # 发单动作已经送出（result.ok=True），这根 K 线不再重复点击。
        # result.ok=False 的情况（价格被挡、窗口被盖、手数没写进框）不算"已发单"，
        # 下一轮扫描还在同一根 K 线里时应该再试一次——那才是用户真正想要的行为。
        if result.ok and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN) and bar_key:
            self._sent_bar[decision.symbol] = bar_key
        elif decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            self._release_signal_mark(decision)

        if result.ok:
            # GUI 且关闭 assume_fill：只记「已发单」，不改内部持仓（点击≠成交）
            if (
                executor == "gui"
                and getattr(result, "assumed_fill", False)
                and not self.gui_assume_fill
            ):
                logger.warning(
                    "GUI 已发单但 assume_fill=false，不更新内部持仓 | {} {} vol={} | {}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                    result.message,
                )
                self._record_event(
                    "order_sent_unconfirmed",
                    symbol=decision.symbol,
                    side=side,
                    volume=vol,
                    order_id=result.order_id,
                    reason=decision.reason,
                )
                self._clear_order_intent()
                return True

            # 记账用参考价：开仓用策略 entry；平仓尽量用最新收盘近似盈亏
            if decision.action == StrategyAction.CLOSE:
                fill_price = float(decision.entry_price or 0.0)
                try:
                    df = self.kline.fetch(decision.symbol, period=str(self.strategy.timeframe))
                    if df is not None and len(df) > 0:
                        fill_price = float(df["close"].iloc[-1])
                except Exception:  # noqa: BLE001
                    pos = self._position_for(decision.symbol)
                    fill_price = float(getattr(pos, "entry_price", 0) or 0)
            else:
                fill_price = float(decision.entry_price or 0.0)
                if fill_price <= 0:
                    logger.error("开仓无参考入场价，拒绝更新内部持仓，避免 entry=0")
                    self._record_event("order_no_price", symbol=decision.symbol, side=side)
                    self._clear_order_intent()
                    return False

            if getattr(result, "assumed_fill", False):
                logger.warning(
                    "GUI 假定成交记账（请尽快核对同花顺真实持仓）| {} {} vol={}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                )

            self._apply_fill_accounting(decision.symbol, decision, vol, fill_price, fill_price)
            if executor == "gui" and not self.cfg.get("dry_run", True):
                self._clear_order_intent()
            logger.info(
                "已执行 {} {} vol={} | {} | stop={} | {}",
                decision.action.value,
                decision.symbol,
                vol,
                decision.reason,
                decision.stop_price,
                size_note or "平仓",
            )
            self._record_event(
                "order",
                symbol=decision.symbol,
                side=side,
                volume=vol,
                order_id=result.order_id,
                reason=decision.reason,
                stop=decision.stop_price,
                sizing=size_note,
                assumed_fill=bool(getattr(result, "assumed_fill", False)),
            )
            return True
        else:
            logger.error("下单失败: {}", result.message)
            self.last_error = result.message
            self._record_event("order_fail", symbol=decision.symbol, side=side, message=result.message)
            if executor == "gui" and not self.cfg.get("dry_run", True):
                self._clear_order_intent()
            return False

    def _release_signal_mark(self, decision: StrategyDecision) -> None:
        """
        下单没发出去时，把策略里「这根 K 线已处理」的标记撤销。

        策略在产生信号时就写标记，原本是为了同一根 K 线不重复评估；
        但下单失败也被一起锁住，信号会静默丢掉。这里只在确认没点单时撤销，
        让下一轮扫描还能在同一根 K 线上再试一次。
        """
        bar_key = str((decision.meta or {}).get("bar_id", "") or "")
        if not bar_key:
            return
        marks = getattr(self.strategy, "_signaled_bar", None)
        if isinstance(marks, dict) and marks.get(decision.symbol) == bar_key:
            marks.pop(decision.symbol, None)
            logger.warning(
                "{} 下单没有发出去，撤销「{} 已处理」标记，下一轮再试",
                decision.symbol,
                bar_key,
            )
        pending = getattr(self.strategy, "_pending", None)
        if isinstance(pending, dict):
            item = pending.get(decision.symbol)
            if isinstance(item, dict) and str(item.get("bar_id", "")) == bar_key:
                pending.pop(decision.symbol, None)

    def scan_symbol(self, symbol: str) -> None:
        """对单个品种：拉信号周期 + 止损周期K线 → 策略评估 → 执行。"""
        signal_tf = getattr(self.strategy, "timeframe", "15")
        trail_tf = getattr(self.strategy, "trail_timeframe", signal_tf)

        bars = self.kline.fetch(symbol, period=str(signal_tf))
        # 只去掉「墙上时钟还没到结束时刻」的最后一根。
        # 午休后第一根已收盘若被盲删，会一直以为最新还是上午那根，止损会晚好几分钟。
        closed = drop_forming_bar(bars)
        if closed is None or len(closed) == 0:
            closed = bars
        forming = None
        if bars is not None and closed is not None and len(bars) > len(closed):
            forming = bars.iloc[len(closed) :]

        trail_closed = None
        trail_forming = None
        if str(trail_tf) != str(signal_tf):
            trail_bars = self.kline.fetch(symbol, period=str(trail_tf))
            trail_closed = drop_forming_bar(trail_bars)
            if (
                trail_bars is not None
                and trail_closed is not None
                and len(trail_bars) > len(trail_closed)
            ):
                trail_forming = trail_bars.iloc[len(trail_closed) :]

        # 有的策略会另外要日线。拉不到就不开新仓，不用分钟线自己拼日线，也不改用别的策略。
        trend_closed = None
        trend_tf = getattr(self.strategy, "trend_timeframe", None)
        if getattr(self.strategy, "uses_trend_bars", False) and trend_tf and str(trend_tf) != str(signal_tf):
            try:
                trend_bars = self.kline.fetch(symbol, period=str(trend_tf))
                # 最后一根日线可能还在走，先拿掉。策略还会再核对一次是不是已经走完。
                trend_closed = drop_forming_bar(trend_bars)
            except Exception as e:  # noqa: BLE001
                logger.warning("{} 的日线没拿到，这一轮看不清方向，不开新仓: {}", symbol, e)

        # 启动后第一轮才考虑回溯。已有持仓、或最近信号 K 线已经开过，都不再补。
        from src.scheduler.catch_up import CatchUpSettings, find_catch_up_open

        catch_cfg = CatchUpSettings.from_cfg(self.cfg)
        try_catch_up = bool(self._catch_up_pending)
        if self._catch_up_pending:
            self._catch_up_pending = False
        if try_catch_up and not self._symbol_is_flat(symbol):
            pos = self._position_for(symbol)
            logger.info(
                "{} 程序已有持仓（{} {} 手），跳过启动回溯补开仓",
                symbol,
                getattr(pos, "side", "?"),
                int(getattr(pos, "volume", 0) or 0),
            )
            self._record_event("catch_up_skip", symbol=symbol, reason="已有持仓")
            try_catch_up = False
        elif try_catch_up and self._already_took_recent_entry(
            symbol, closed, catch_cfg.lookback_bars
        ):
            logger.info("{} 最近信号 K 线已经开过仓，跳过启动回溯补开仓", symbol)
            self._record_event("catch_up_skip", symbol=symbol, reason="信号K线已开过")
            try_catch_up = False
        elif try_catch_up and not catch_cfg.enabled:
            try_catch_up = False

        decision: StrategyDecision
        eval_bars = closed
        eval_trail = trail_closed
        eval_trend = trend_closed
        catch_up_used = False
        # 空仓/持仓时是否把还在走的那根也拼进去：
        #   - 止损用「触及即止损」（stop_on_touch=True）时，持仓拼未收线那根，让箱沿止损能看到当前影线。
        #   - 开仓用「触发即开仓」（entry_mode=trigger）时，空仓也拼未收线那根，让开仓能看到当前影线。
        # 否则只用已收盘 K 线，避免影线假突破。
        from src.strategy.types import ENTRY_MODE_TRIGGER

        touch_stop = bool(getattr(self.strategy, "stop_on_touch", True))
        entry_trigger = (
            str(getattr(self.strategy, "entry_mode", None) or "").strip().lower()
            == ENTRY_MODE_TRIGGER
        )
        flat = self._symbol_is_flat(symbol)
        use_forming_for_entry = entry_trigger and flat
        use_forming_for_stop = touch_stop and not flat
        if (use_forming_for_entry or use_forming_for_stop) and closed is not None and forming is not None and len(forming) > 0:
            try:
                eval_bars = pd.concat([closed, forming])
            except Exception:  # noqa: BLE001
                eval_bars = closed
        if try_catch_up:
            hit = find_catch_up_open(
                cfg=self.cfg,
                symbol=symbol,
                closed=closed,
                trail_closed=trail_closed,
                trend_closed=trend_closed,
                forming=forming,
                settings=catch_cfg,
            )
            if hit is not None:
                eval_bars = hit.prefix
                eval_trail = hit.trail_prefix
                eval_trend = hit.trend_prefix
                catch_up_used = True
                self._record_event(
                    "catch_up_open",
                    symbol=symbol,
                    reason=hit.decision.reason,
                    bar_time=hit.bar_time.isoformat(timespec="seconds"),
                    atr=hit.atr,
                    price_gap=hit.price_gap,
                )

        decision = self._evaluate_symbol(
            symbol,
            eval_bars,
            trail_bars=eval_trail,
            trend_bars=eval_trend,
            now=_eval_now(eval_bars, closed, catch_up=catch_up_used),
        )

        # 通用止损：和策略自身止损相互独立，策略没让平仓时由公共止损补一刀。
        # 策略要反手/开仓时不抢，它本身也会先平掉旧仓；谁先触发都算平仓。
        if decision.action == StrategyAction.HOLD and not self._symbol_is_flat(symbol):
            held_for_stop = self._position_for(symbol)
            if held_for_stop is not None:
                gs_bars = trail_closed if trail_closed is not None else closed
                gs_forming = (
                    trail_forming if str(trail_tf) != str(signal_tf) else forming
                )
                gs_hit = self.generic_stop.check(
                    symbol,
                    held_for_stop,
                    gs_bars,
                    forming=gs_forming,
                    mode=getattr(self.strategy, "stop_mode", "touch"),
                    # 平仓未成交时别每轮都重复发单，和策略自身止损的等待口径一致。
                    cooldown_sec=120.0,
                )
                if gs_hit is not None:
                    decision = StrategyDecision(
                        StrategyAction.CLOSE,
                        symbol,
                        gs_hit.reason,
                        stop_price=gs_hit.stop,
                    )
                    self._record_event(
                        "generic_stop",
                        symbol=symbol,
                        kind=gs_hit.kind,
                        stop=gs_hit.stop,
                    )

        # 给人看：最新K线长什么样 + 本轮是否符合开仓（不影响下单逻辑）
        try:
            from src.strategy.explain import log_scan_brief

            log_scan_brief(
                symbol,
                closed,
                decision,
                self.strategy,
                signal_tf=str(signal_tf),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("扫描解说失败（可忽略）: {}", e)

        # 过期缓存：禁止新开仓，仍允许平仓（防断网用旧K线乱开）
        # 日线、周线不能沿用「90 分钟就算过期」，否则昨天的日线永远开不了仓。
        stale_limit = stale_after_minutes(str(signal_tf), self.max_stale_minutes)
        stale = is_bars_stale(closed, stale_limit)
        self._last_signal_bars[symbol] = closed
        if decision.action == StrategyAction.HOLD:
            cut = self._maybe_dynamic_cut(symbol)
            if cut is not None:
                decision = cut

        if stale and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            logger.error(
                "{} 的{}K线过期，拒绝开仓 | {}",
                symbol,
                period_label(str(signal_tf)),
                decision.reason,
            )
            self._record_event("stale_block_open", symbol=symbol, reason=decision.reason)
            return

        # 滑点闸门：信号价是那根 K 的收盘，真正点出去还要几秒。
        # 急拉急跌时现价已经跑远，追进去等于买在最高点，宁可放弃这一根等下一根。
        if (
            decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)
            and not catch_up_used
            and not (decision.meta or {}).get("reverse")
        ):
            # 现价优先用未收线那根（就是当前价）；数据只到收盘那根时，去要一次最新的 1 分钟价。
            live_price = _last_close(forming)
            if live_price <= 0:
                live_price = self.kline.latest_price(symbol)
            if live_price <= 0:
                live_price = _last_close(closed)
            atr = _latest_atr(closed, self.strategy)
            limit_atr = float((self.cfg.get("trade") or {}).get("max_entry_slip_atr", 0.5) or 0)
            limit_points = float((self.cfg.get("trade") or {}).get("max_entry_slip_points", 0) or 0)
            blocked, chase, limit = entry_slip_blocked(
                float(decision.entry_price or 0),
                live_price,
                atr,
                is_long=decision.action == StrategyAction.BUY_OPEN,
                limit_atr=limit_atr,
                limit_points=limit_points,
            )
            if blocked:
                logger.warning(
                    "{} 开仓滑点过大，放弃这一根 | 信号价 {:.2f} 现价 {:.2f} 偏移 {:.2f} 点 > 上限 {:.2f} 点"
                    "（ATR 系数 {}，点数上限 {}）",
                    symbol,
                    float(decision.entry_price or 0),
                    live_price,
                    chase,
                    limit,
                    limit_atr,
                    limit_points,
                )
                self._record_event("slip_block_open", symbol=symbol, reason=decision.reason)
                return

        try:
            self._execute(decision)
        except Exception:
            # 还没进到点单就出异常（比如下单前拼参数写错），把信号标记放开，下一轮还能重试；
            # 已经点出去了就不放开，避免重复下单。
            if (
                decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)
                and not getattr(self, "_order_dispatch_started", False)
            ):
                self._release_signal_mark(decision)
            raise

    def _evaluate_accepts(self, name: str) -> bool:
        """策略的 evaluate 是否真的接受这个关键字参数。

        以前用 `except TypeError: pass` 回调一次，会把策略内部的 TypeError
        （真正的 bug）也吞掉，还会让同一根K线被评估两次。这里只看签名。
        """
        cached = getattr(self, f"_evaluate_accepts_{name}", None)
        if cached is None:
            import inspect

            try:
                params = inspect.signature(self.strategy.evaluate).parameters
            except (TypeError, ValueError):
                params = {}
            cached = name in params or any(
                param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
            )
            setattr(self, f"_evaluate_accepts_{name}", cached)
        return bool(cached)

    def _evaluate_symbol(
        self,
        symbol: str,
        bars: Any,
        *,
        trail_bars: Any = None,
        trend_bars: Any = None,
        now: datetime | None = None,
    ) -> StrategyDecision:
        """调用正式策略。回溯时可能带上信号时刻；策略不支持就不传。"""
        uses_trend = bool(getattr(self.strategy, "uses_trend_bars", False))
        kwargs: dict[str, Any] = {"trail_bars": trail_bars}
        if uses_trend:
            kwargs["trend_bars"] = trend_bars
        if now is not None and self._evaluate_accepts("now"):
            kwargs["now"] = now
        return self.strategy.evaluate(symbol, bars, **kwargs)

    def _ensure_runtime_ready(self) -> bool:
        """
        运行期健康检查：期货通窗口是否还在。
        原理：登录成功后若窗口被关掉，不能继续当「已登录」去点鼠标。
        """
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor != "gui" or self.cfg.get("dry_run", True):
            return True
        if self.login_mgr.window_exists():
            return True
        logger.error("未找到同花顺窗口，本轮跳过扫描。请重新打开并登录后再挂机。")
        self.logged_in = False
        self.last_error = "期货通窗口丢失"
        self._record_event("window_lost")
        try:
            from src.ops.alerts import emit_alert

            emit_alert("window_lost", "期货通窗口丢失，本轮跳过扫描", level="error")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _ctp_limit_price(self, decision: StrategyDecision) -> float | None:
        """CTP 限价。开仓用策略价格；平仓用最新收盘价。策略本身不改。"""
        if decision.action != StrategyAction.CLOSE and decision.entry_price and float(decision.entry_price) > 0:
            return float(decision.entry_price)
        try:
            df = self.kline.fetch(decision.symbol, period=str(self.strategy.timeframe))
            if df is not None and len(df) > 0:
                return float(df["close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 {} 最新价失败: {}", decision.symbol, exc)
        return None

    def _internal_nets(self) -> dict[str, int]:
        """程序自己记的净持仓。多仓为正，空仓为负，用来和柜台对比。"""
        nets: dict[str, int] = {}
        for sym, pos in (getattr(self.strategy, "positions", {}) or {}).items():
            volume = int(getattr(pos, "volume", 0) or 0)
            held = getattr(pos, "side", "flat")
            if held == "long":
                nets[str(sym)] = volume
            elif held == "short":
                nets[str(sym)] = -volume
            else:
                nets[str(sym)] = 0
        return nets

    def _reconcile_ctp(self) -> None:
        """扫描前向柜台对一次账。持仓对不上才拦开仓；资金按期货公司数字改本地。"""
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor != "ctp" or not hasattr(self.trader, "reconcile"):
            return
        try:
            self.trader.reconcile(self._internal_nets())
            self._apply_broker_equity()
        except Exception as exc:  # noqa: BLE001
            logger.exception("柜台对账失败: {}", exc)

    def _uses_ctp_broker_funds(self) -> bool:
        """当前是不是已经连上 CTP、并且有期货公司资金数字可以跟。"""
        trade = self.cfg.get("trade") or {}
        if str(trade.get("executor") or "").strip().lower() != "ctp":
            return False
        if getattr(self.trader, "last_broker_snapshot", None):
            return True
        return getattr(self.trader, "last_broker_equity", None) is not None

    def _apply_broker_equity(self) -> None:
        """常驻算手数、风控、配置权益都改成期货公司刚查到的数字。"""
        if not hasattr(self.trader, "gateway"):
            return
        try:
            from src.account.ctp_sync import apply_broker_runtime

            apply_broker_runtime(
                self.root,
                cfg=self.cfg,
                sizer=self.sizer,
                risk=self.risk,
                trader=self.trader,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("按柜台资金改本地账失败，开平仓规则不变: {}", exc)

    def _on_ctp_fill(self, report: dict[str, Any]) -> None:
        """
        柜台确认全部成交后改内部持仓。

        回报可能比报单函数返回更早，所以真实成交只在这里记一次账。
        """
        offset = str(report.get("offset") or "")
        direction = str(report.get("direction") or "")
        if offset == "0":
            action = StrategyAction.BUY_OPEN if direction == "0" else StrategyAction.SELL_OPEN
        else:
            action = StrategyAction.CLOSE
        price = float(report.get("avg_price") or report.get("price") or 0)
        volume = int(report.get("filled") or report.get("volume") or 0)
        if volume <= 0 or price <= 0:
            logger.error("CTP 成交回报缺少价格或手数，不记账")
            return False
        decision = StrategyDecision(
            action=action,
            symbol=str(report.get("symbol") or ""),
            reason="CTP成交回报",
            stop_price=report.get("stop_price"),
            entry_price=price,
            meta={"bar_id": str(report.get("bar_id") or "")},
        )
        ref = str(report.get("order_ref") or "").strip()
        fill_key = f"{ref}|{action.value}|{volume}" if ref else ""
        stop = report.get("stop_price")
        self._apply_fill_accounting(
            decision.symbol,
            decision,
            volume,
            price,
            price,
            fill_key=fill_key,
            stop_price=float(stop) if stop else None,
        )
        self._record_event(
            "order",
            symbol=decision.symbol,
            side=action.value,
            volume=volume,
            price=price,
            order_id=str(report.get("order_ref") or ""),
            reason=decision.reason,
        )
        return True

    def _finish_ctp_order(
        self,
        decision: StrategyDecision,
        vol: int,
        result: Any,
        side: str,
        size_note: str,
    ) -> None:
        """根据 CTP 回报决定记账还是继续等。部分成交按实际成交手数记账。"""
        if not result.ok:
            logger.warning("CTP 报单没有成功 | {} {} | {}", decision.symbol, side, result.message)
            self._record_event(
                "order_rejected",
                symbol=decision.symbol,
                side=side,
                volume=vol,
                reason=result.message,
            )
            try:
                from src.ops.alerts import emit_alert

                emit_alert(
                    "ctp_reject",
                    f"{decision.symbol} {side}：{result.message}",
                    level="error",
                    root=self.root,
                )
            except Exception:  # noqa: BLE001
                pass
            return
        raw = result.raw or {}
        filled = int(raw.get("filled_volume") or 0)
        accounted = int(raw.get("accounted_volume") or 0)
        if "accounted_volume" not in raw and raw.get("accounted"):
            # 旧回报没有分项字段时，记过账就当作全部入账。
            accounted = filled
        leftover = max(0, filled - accounted)
        fill_price = float(raw.get("fill_price") or 0)
        booked = leftover <= 0
        if leftover > 0:
            if fill_price <= 0:
                logger.error(
                    "CTP 有成交但没有成交价，不记账 | {} {} | {} 手",
                    decision.symbol,
                    side,
                    leftover,
                )
                self._record_event("order_no_price", symbol=decision.symbol, side=side, volume=leftover)
            else:
                # 回报回调没记账（例如没挂记账函数），这里补记实际成交的部分。
                ref = str(result.order_id or "").strip()
                self._apply_fill_accounting(
                    decision.symbol,
                    decision,
                    leftover,
                    fill_price,
                    fill_price,
                    fill_key=f"{ref}|{side}|{filled}" if ref else "",
                    stop_price=decision.stop_price,
                )
                self._record_event(
                    "order",
                    symbol=decision.symbol,
                    side=side,
                    volume=leftover,
                    price=fill_price,
                    order_id=result.order_id,
                    reason=decision.reason,
                )
                booked = True
        if raw.get("filled"):
            if leftover <= 0:
                logger.info("CTP 成交已记账 | {} {} | {}", decision.symbol, side, result.message)
            if booked:
                logger.info(
                    "已执行 {} {} vol={} | {} | stop={} | {}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                    decision.reason,
                    decision.stop_price,
                    size_note or "平仓",
                )
            return
        if filled > 0:
            logger.warning(
                "CTP 只成交 {} / {} 手，剩余没有成交，先按成交部分记账 | {} {} | {}",
                filled,
                vol,
                decision.symbol,
                side,
                result.message,
            )
            self._record_event(
                "order_partial",
                symbol=decision.symbol,
                side=side,
                volume=filled,
                target=vol,
                message=result.message,
            )
            return
        if raw.get("working"):
            logger.warning(
                "CTP 回报还不是全部成交，先不改内部持仓 | {} {} | {}",
                decision.symbol,
                side,
                result.message,
            )
            self._record_event(
                "order_working",
                symbol=decision.symbol,
                side=side,
                volume=vol,
                order_id=result.order_id,
                reason=result.message,
            )
            return
        logger.warning("CTP 报单没有成交 | {} {} | {}", decision.symbol, side, result.message)
        self._record_event(
            "order_unfilled",
            symbol=decision.symbol,
            side=side,
            volume=vol,
            order_id=result.order_id,
            reason=result.message,
        )

    def scan_once(self, force: bool = False) -> None:
        """
        执行一轮扫描（全部关注品种）。

        参数:
            force: True 时忽略交易时段限制（给 --once 调试用）
        """
        self._rollover_trading_day()
        self._refresh_sessions()

        # 盘前定时审计：每轮扫描开头检查一次，落在 08:55 / 13:25 / 20:25 前后 3 分钟内且距上次
        # 触发已超过 1 小时就跑一次，避免同一时段重复跑。
        self._maybe_run_session_audit()
        if (
            not force
            and self.sessions
            and not in_trading_session(self.sessions)
            and not self.strategy.is_flatten_time()
        ):
            now = datetime.now()
            last = self._session_skip_log_at
            if last is None or (now - last).total_seconds() >= 60:
                night = next((row for row in self.sessions if str(row[0]).startswith("21")), None)
                night_txt = f"{night[0]}～{night[1]}" if night else "未写夜盘"
                logger.info(
                    "现在不在交易时段，本轮不拉K线、不扫描（夜盘按 {}）",
                    night_txt,
                )
                self._session_skip_log_at = now
            return

        if not self.logged_in:
            executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
            if executor == "ctp":
                self.logged_in = True
                self._gui_price_ready = True
                self._arm_ctp()
            else:
                self.do_login()
                if not self.logged_in:
                    return

        # 每轮复检窗口，避免「已登录」标志过期后仍瞎点
        if not self._ensure_runtime_ready():
            self._persist_state()
            return

        now_s = datetime.now().isoformat(timespec="seconds")
        self.heartbeat_at = now_s
        self.last_scan_at = now_s
        self._dynamic_note = ""

        self._reconcile_ctp()
        for symbol in self.scan_symbols:
            try:
                self.scan_symbol(symbol)
            except RuntimeError as exc:
                # 行情没拿到时只写一句原因。整段英文报错会占满运行日志，看起来像程序崩了。
                self.last_error = str(exc)
                logger.warning("扫描 {} 这一轮没有做成: {}", symbol, exc)
                self._record_event("scan_error", symbol=symbol, error=str(exc))
            except Exception as e:  # noqa: BLE001
                # 含行情拉取失败：跳过本品种，不中断其它品种
                self.last_error = str(e)
                logger.exception("扫描 {} 失败: {}", symbol, e)
                self._record_event("scan_error", symbol=symbol, error=str(e))
        self._maybe_check_health()
        self._persist_state()

    def setup_schedule(self) -> None:
        """注册每日登录时间点。柜台通道不点同花顺，所以不注册这些钟点。"""
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor == "ctp":
            logger.info("执行通道是 CTP，跳过同花顺定时登录")
            return
        times = self.cfg.get("schedule", {}).get("login_times", [])
        for t in times:
            schedule.every().day.at(t).do(self.do_login)
            logger.info("已注册每日登录时间: {}", t)

    def stop(self) -> None:
        """请求停止主循环（供图形界面使用）。"""
        self._running = False
        logger.info("已请求停止机器人主循环")

    @property
    def is_running(self) -> bool:
        """是否仍在主循环中。"""
        return self._running

    def run_forever(self) -> None:
        """阻塞运行：定时登录 + 周期扫描。Ctrl+C 或 stop() 退出。"""
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        # GUI 多品种硬拦：没有自动切合约时挂机过于危险。只检查常驻实际扫描的品种。
        allow_multi = bool((self.cfg.get("trade") or {}).get("gui", {}).get("allow_multi_symbol", False))
        if executor == "gui" and len(self.scan_symbols) > 1 and not allow_multi:
            raise RuntimeError(
                f"GUI 模式禁止多品种同时挂机：当前 {self.scan_symbols}。"
                "请在配置里只留一个合约，或标定 symbol_input 并设 trade.gui.allow_multi_symbol=true。"
            )

        self.setup_schedule()
        self._running = True
        # 第一轮扫描才允许回溯补开仓；scan_symbol 里用掉后会关掉。
        self._catch_up_pending = True
        logger.info(
            "机器人启动 | dry_run={} | 扫描={} | 关注={} | 周期={} | 扫描间隔={}s | 模式={}",
            self.cfg.get("dry_run"),
            self.scan_symbols,
            self.symbols,
            period_label(str(self.strategy.timeframe)),
            self.scan_interval,
            getattr(self.strategy, "__class__", type(self.strategy)).__name__,
        )
        logger.info("账号通道: 华泰期货_CTP主席。执行通道 sim/gui/ctp 都可用；ctp 与测试窗口同一套接口")

        # 启动即跑一次盘前行情适配审计：打印所有关注品种 × 各策略的行情适配状态。
        # 独立线程跑，不阻塞启动（K 线拉取有网络延迟）。
        self._run_session_audit("启动")

        # 挂机防休眠：否则电脑一睡，扫描与 GUI 点击都会停
        from src.utils.keepalive import KeepAwake

        keep_display = bool((self.cfg.get("runtime") or {}).get("keep_display_awake", True))
        keepalive = KeepAwake(keep_display=keep_display)
        keepalive.start()

        from src.login.prepare import startup_should_login

        # 准备按钮刚刚登录过，这里再登录会把交易菜单再点开一次。
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor == "ctp":
            # 柜台通道自己登录 CTP，不再点同花顺。
            self.logged_in = True
            self._gui_price_ready = True
            self._arm_ctp()
        elif startup_should_login(self.cfg):
            self.do_login()
            self._arm_ctp()
        else:
            logger.info("启动前刚刚登录过，常驻不再登录第二次。只核对对手价。")
            self.logged_in = True
            self._arm_order_price()
            self._arm_ctp()

        # 清仓改成定时触发：先算好下一次钟点，平时每秒只比一次时间，不重复跑策略。
        # 启动时已经在清仓窗口里，就把定时器设成现在，先平一次再说。
        try:
            in_window = bool(self.strategy.is_flatten_time())
        except Exception:  # noqa: BLE001
            in_window = False
        flatten_at = datetime.now() if in_window else self._next_flatten_at()

        try:
            while self._running:
                try:
                    schedule.run_pending()
                    self.scan_once()
                    # 可选：当日最后收盘后预约关机（有夜盘不会在下午就关）
                    self._maybe_shutdown_after_close()
                    # 分段 sleep，便于更快响应 stop()
                    for _ in range(int(max(self.scan_interval, 1))):
                        if not self._running:
                            break
                        if flatten_at is not None and datetime.now() >= flatten_at:
                            if not self._flatten_pending:
                                # 刚到清仓钟点：开一轮新的清仓待办（允许重新尝试）。
                                self._flatten_pending = True
                                self.flatten_if_due(force=True)
                            else:
                                # 上一轮没平掉：继续重试，不受 10 分钟窗口限制。
                                self.flatten_if_due()
                            if self._flatten_pending:
                                flatten_at = datetime.now() + timedelta(seconds=self._flatten_retry_sec)
                            else:
                                flatten_at = self._next_flatten_at()
                        time.sleep(1)
                except Exception as exc:  # noqa: BLE001
                    # 单轮异常不能让常驻线程整体退出（磁盘、登录、日志都可能偶发失败）。
                    self.last_error = str(exc)
                    logger.exception("主循环这一轮异常，5 秒后继续: {}", exc)
                    try:
                        self._record_event("loop_error", error=str(exc))
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(5)
        except KeyboardInterrupt:
            logger.info("收到键盘退出信号")
        finally:
            self._running = False
            keepalive.stop()
            if hasattr(self.trader, "shutdown"):
                self.trader.shutdown()
            self._persist_state()
            logger.info("机器人已停止")
            self._record_event("shutdown")

    def _arm_ctp(self) -> None:
        """执行通道是 CTP 时，启动就完成认证、登录、结算单确认，并按柜台资金改本地账。"""
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor != "ctp":
            return
        from src.trader.go_live import strategy_order_block_reason

        blocked_stage = strategy_order_block_reason(self.cfg)
        if blocked_stage:
            logger.warning(blocked_stage)
            return
        from src.account.ctp_sync import resident_book_guard

        book_block = resident_book_guard(self.root, self.cfg)
        if book_block:
            logger.error("CTP 常驻未连接：{}", book_block)
            try:
                from src.ops.alerts import emit_alert

                emit_alert("ctp_book_mismatch", book_block, level="error")
            except Exception:  # noqa: BLE001
                pass
            return
        if self.cfg.get("dry_run", True):
            logger.warning("模拟开关开着，CTP 不会连接柜台，也不会发单")
            return
        if not hasattr(self.trader, "connect"):
            return
        ok, message = self.trader.connect()
        if ok:
            logger.info("CTP {}", message)
            self._startup_equity_sync()
        else:
            logger.error("CTP 没有连上：{}。扫描会继续，但开新仓会停，直到登录和对账都通过。", message)
            try:
                from src.ops.alerts import emit_alert

                emit_alert("ctp_down", f"CTP 没有连上：{message}", level="error")
            except Exception:  # noqa: BLE001
                pass

    def _startup_equity_sync(self) -> None:
        """连上柜台后先查权益，按期货公司数字改本地账，再进入扫描。"""
        if not hasattr(self.trader, "reconcile"):
            return
        try:
            ok, note = self.trader.reconcile(self._internal_nets())
            self._apply_broker_equity()
            if ok:
                logger.info("启动权益核对完成：{}", note)
            else:
                logger.warning("启动权益已按柜台改本地账，但持仓还对不上：{}", note)
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动时向柜台核对资金失败: {}", exc)

    def _maybe_shutdown_after_close(self) -> None:
        """
        若配置开启，则在「当日最后一节收盘」后预约 Windows 关机，并停止机器人循环。
        """
        rt = self.cfg.get("runtime") or {}
        if not bool(rt.get("auto_shutdown_after_close", False)):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._shutdown_armed_date == today:
            return

        from src.utils.shutdown import ready_for_shutdown, schedule_windows_shutdown

        flatten = list(getattr(self.strategy, "flatten_times", None) or [])
        # strategy 里可能是 time 对象；统一成 HH:MM 字符串
        flat_s: list[str] = []
        for x in flatten:
            if hasattr(x, "strftime"):
                flat_s.append(x.strftime("%H:%M"))
            else:
                flat_s.append(str(x))
        ok, why = ready_for_shutdown(
            sessions=self.sessions,
            flatten_times=flat_s or list((self.cfg.get("strategy") or {}).get("flatten_times") or []),
            grace_minutes=int(rt.get("shutdown_grace_minutes", 5)),
            has_position=self._has_any_position(),
        )
        if not ok:
            if "未平仓位" in why:
                logger.info("{}，继续持仓运行", why)
            return

        delay = int(rt.get("shutdown_delay_sec", 180))
        self._shutdown_armed_date = today
        logger.warning("收盘后关机条件满足：{} → 开始预约关机", why)
        success, msg = schedule_windows_shutdown(delay)
        if success:
            logger.warning("{}", msg)
            self._record_event("auto_shutdown_scheduled", delay_sec=delay, reason=why)
            # 先停机器人，再等系统倒计时关机（防休眠也会在 finally 里释放）
            self._running = False
        else:
            logger.error("收盘后关机失败: {}", msg)
            self._shutdown_armed_date = ""  # 允许下一轮重试
            self.last_error = msg
