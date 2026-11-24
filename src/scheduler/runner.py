"""
主调度与交易循环
功能：协调登录、K线扫描、策略评估、风控下单和收盘清仓。
原理：schedule 负责定时；登录、订单执行、清仓和盘前审计分模块，TradingBot 只协调主流程。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pandas as pd
import schedule
from loguru import logger

from src.data import KlineProvider
from src.data.kline import bars_age_minutes, is_bar_gap, is_bars_stale
from src.data.periods import drop_forming_bar, period_label, stale_after_minutes
from src.kernel.commands import Command, CommandBus, FLATTEN, SCAN, STOP
from src.kernel.events import EventBus, SIGNAL
from src.kernel.trace import new_trace_id, set_trace_id
from src.login import AppLoginManager
from src.ops import journal
from src.risk import PositionSizer, RiskService
from src.scheduler.flatten_manager import FlattenManagerMixin
from src.scheduler.login_flow import LoginFlowMixin
from src.scheduler.order_executor import OrderExecutorMixin
from src.scheduler.order_pricing import entry_slip_blocked as entry_slip_blocked
from src.scheduler.session_audit import SessionAuditMixin
from src.strategy import StrategyAction, StrategyDecision, create_strategy
from src.strategy.decision_guard import (
    apply_delivery_gate,
    apply_generic_stop,
    apply_partial_tail_gate,
)
from src.strategy.generic_stop import GenericStopOverlay
from src.trader import create_trader
from src.trader.ctp_logic import position_avg_price
from src.trader.order_timeline import OrderTimeline, now_iso
from src.utils.state_store import (
    default_state_path,
    format_position_text,
    load_state,
    restore_positions,
    save_state,
    snapshot_positions,
)
from src.utils.trading_day import calendar_coverage_ok, futures_trading_day

# events.jsonl 超过这个大小就轮转成 .1，避免长期挂机把单文件撑到几百 MB。
EVENT_LOG_MAX_BYTES = 20 * 1024 * 1024


def _safe_last_alert(max_daily_loss: float | None = None) -> dict[str, Any] | None:
    """读最近一条告警；文件坏了也不要拖垮运行台刷新。限额对不上现在配置的熔断行会删掉。"""
    try:
        from src.ops.alerts import last_alert

        return last_alert(max_daily_loss=max_daily_loss)
    except Exception:  # noqa: BLE001
        return None


def _parse_hhmm(value: str) -> dtime:
    """把 '09:30' 转成 time 对象。"""
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


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


class TradingBot(
    LoginFlowMixin,
    OrderExecutorMixin,
    FlattenManagerMixin,
    SessionAuditMixin,
):
    """协调登录、行情扫描、策略评估、订单执行、清仓和运行状态。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        root = Path(cfg.get("_project_root", "."))
        self.root = root
        # 本次运行标识：事件、告警、状态文件都带它，多次运行的数据不会混在一起。
        # 同进程同秒内建两个实例（测试、脚本）也不会撞号，末尾加 4 位随机。
        self.run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{uuid4().hex[:4]}"
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        from src.ops.alerts import set_run_id

        set_run_id(self.run_id)
        from src.version import __version__

        logger.info(
            "程序启动 run_id={} 版本={} 根目录={} 启动时间={}",
            self.run_id,
            __version__,
            root,
            self.started_at,
        )
        self.login_mgr = AppLoginManager(cfg)
        self.risk = RiskService.from_config(cfg, root=root)
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
        # 多品种实盘开关：默认关。GUI 通道只开当前选中品种，开了也强制忽略。
        self._multi_cfg = self._read_multi_cfg()
        enabled = bool(self._multi_cfg.get("enabled", False))
        if enabled and self.trader_capabilities.window_driven:
            logger.warning("GUI 通道不支持多品种开仓，live.multi_symbol.enabled 已忽略")
            enabled = False
        if enabled and self._is_live_account() and not bool(self._multi_cfg.get("allow_live", False)):
            # 方案门 3/4：真实账户必须单独放行；仿真阶段先跑通再谈实盘。
            logger.warning(
                "真实账户未放行多品种（live.multi_symbol.allow_live=false），已按单品种运行"
            )
            enabled = False
        self._multi_enabled = enabled
        self._multi_symbols = [
            str(item).strip()
            for item in (self._multi_cfg.get("symbols") or [])
            if str(item).strip()
        ]
        if self._multi_enabled:
            self._validate_multi_symbol_config()
            from src.strategy.leg_router import create_leg_router

            self.strategy = create_leg_router(cfg, self.strategy)
        # 常驻只扫「当前选中」这一个品种：其余关注品种保留在 self.symbols 里，
        # 只给交叉测试等需要多品种的功能用。多品种开关打开时，白名单内的空仓品种也扫。
        self.scan_symbols = self._pick_scan_symbols()
        self.scan_interval = int(cfg.get("schedule", {}).get("scan_interval_sec", 30))
        if self._multi_enabled:
            # 两个品种串行拉K线、等回报，5 秒一轮扫不完；按决策 D3 抬到 15 秒。
            multi_interval = int(self._multi_cfg.get("scan_interval_sec", 0) or 0)
            if multi_interval > 0:
                self.scan_interval = max(self.scan_interval, multi_interval)
        self._symbol_sessions: dict[str, list[list[str]]] = {}
        self._symbol_flatten_times: dict[str, list[str]] = {}
        from src.utils.session_clock import sessions_for_symbol

        # 交易时段跟当前盯的合约走（品种表优先；白银黄金默认收到 02:30）。
        live_symbol = (self.scan_symbols[0] if getattr(self, "scan_symbols", None) else "") or self.default_symbol
        self.sessions = sessions_for_symbol(cfg, live_symbol)
        self._session_skip_log_at: datetime | None = None
        self._live_session_key = ""
        self.logged_in = False
        # CTP 登录失败后的下次重试时刻；None 表示可以立即尝试。
        self._ctp_retry_at: datetime | None = None
        self._running = False  # GUI / 外部可调用 stop() 结束循环
        self._stopped = threading.Event()
        self._stop_requested_at: float | None = None
        # P2：命令/事件总线。命令由运行页或脚本投递，主循环按 FIFO 处理；
        # 事件只做旁路通知，账本落库仍在同步链路里完成，顺序不变。
        self.commands = CommandBus()
        self.events = EventBus()
        # 暂停报单：扫描还在跑，开平仓、止损、清仓都不发出去。和交易台「暂停报单」同一标志。
        self._orders_paused = False
        # 窗口点击真下单前，必须先把价格改成对手价。没改成就不允许点买卖。
        self._gui_price_ready = not (
            self.trader_capabilities.window_driven and not bool(cfg.get("dry_run", True))
        )
        self.heartbeat_at: str = ""
        self.last_scan_at: str = ""
        self.last_error: str = ""
        self._health_report: dict[str, Any] | None = None
        # 只有 runtime.health_block_entry 打开且连续偏离达标时为真，只停开新仓。
        self._health_blocked = False
        self._health_block_reason = ""
        # 行情健康按「合约|周期」保留；完整延迟逐轮写独立日志，异常事件/告警限流。
        self._market_health: dict[str, dict[str, Any]] = {}
        self._market_scan_at: dict[str, float] = {}
        self._throttled_event_at: dict[str, float] = {}
        # 动态减仓：最近一次说明、各品种已收盘 K 线、已经减过的标记（避免每轮重复减）。
        self._dynamic_note: str = ""
        self._last_signal_bars: dict[str, Any] = {}
        self._dyn_cut_key: str = ""
        self._dyn_cut_pending: str = ""
        # GUI：点击成功后是否立刻当作成交记账（默认 True 可挂机；False=只记「已发单」不改持仓，更安全）
        gui_cfg = (trade.get("gui") or {}) if isinstance(trade, dict) else {}
        self.gui_assume_fill = bool(gui_cfg.get("assume_fill", True))
        # 收盘后关机：每天最多预约一次
        self._shutdown_armed_date: str = ""
        self.max_stale_minutes = float((cfg.get("data") or {}).get("max_stale_minutes", 90))
        # 天勤失败回退备用源时，是否允许用回退行情开新仓。false 时只保护持仓、不加新仓。
        self.fallback_allow_entry = bool((cfg.get("data") or {}).get("fallback_allow_entry", True))
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
        # 手工录入与后台扫描共用一把交易锁，避免同时改同一份策略仓位。
        self._trade_lock = threading.RLock()
        # 盘前行情适配审计：启动时跑一次 + 每天 08:55 / 13:25 / 20:25 各跑一次。
        # 记录上次触发时间避免同一时段重复跑。
        self._last_audit_at: datetime | None = None
        # 审计同一时刻只跑一个线程。候选池大、触发密时不让它们叠加。
        self._audit_thread: threading.Thread | None = None

        self.event_log = root / "data" / "events.jsonl"
        self.market_health_log = root / "data" / "market_health.jsonl"
        self._order_timeline = OrderTimeline(root)
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        # 休市日历外置：读不到就用内置 2026 表（老行为）。实盘启动再检查覆盖范围。
        from src.utils.holidays import calendar_from_config

        self.holiday_calendar = calendar_from_config(cfg, root)
        # 退出保护标记：上次退出还有持仓/挂单时留下，下次启动先对账。
        # 路径要在读盘之前定好：_load_persisted_state 里会顺带落一次状态。
        self._unprotected_path = root / "data" / "unprotected_positions.json"
        self.state_path = default_state_path(root)
        # 下单意图写前日志：点击前先落盘，成功后清除。若程序在点击与记账之间崩溃，
        # 下次启动会看到这个文件，提示人工核对交易软件持仓，避免重复开仓/漏平仓。
        from src.account.workspace import intent_dir, intent_file

        # 点单意图跟当前账走：模拟盘崩溃核对时，不会误用实盘那份意图文件。
        # 新委托一笔一个文件；旧版单文件路径只用于读取升级前的遗留记录。
        self.intent_dir = intent_dir(root)
        self.intent_path = intent_file(root)
        self._pending_intent: dict[str, Any] | None = None
        # 先记下当前交易日（夜盘 21:00 起算下一个）。读到磁盘后如果文件里已有交易日，再用文件里的。
        self.trading_day = str(futures_trading_day())
        # 三方对账：最近一次结果、冻结自动交易的品种、幽灵仓连续确认轮数。
        self._frozen_symbols: set[str] = set()
        self._frozen_noted: set[str] = set()
        self._reconcile_state: dict[str, Any] = {}
        self._phantom_rounds: dict[str, int] = {}
        self._startup_reconcile_pending = False
        self._load_persisted_state()
        # 退出保护：上次退出时还有持仓/挂单，重启后先对账、评估止损，再恢复报单。
        self._unprotected_resume = False
        self._watchdog: Any = None
        self._protective_state = "none"
        marker = self._load_unprotected_flag()
        if marker is not None:
            self._unprotected_resume = True
            self.set_orders_paused(True)
            self._record_event(
                "unprotected_resume",
                reason=marker.get("reason"),
                positions=marker.get("positions"),
            )
            logger.error(
                "上次退出时仍有持仓/挂单（{}），本轮先对账并评估止损，恢复前不报单",
                marker.get("reason"),
            )
        self._check_pending_intent()
        self._warn_gui_safety()
        # 成交回报可能比 place_order 返回更早。先挂上记账函数，避免全部成交后没人改持仓。
        if self.trader_capabilities.orders_via_broker and hasattr(self.trader, "set_fill_handler"):
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

    def _unprotected_flag_path(self) -> Path:
        """退出保护标记文件路径。"""
        return self._unprotected_path

    def _load_unprotected_flag(self) -> dict[str, Any] | None:
        """读上次退出留下的持仓标记。文件坏了按「有仓未确认」处理，宁可多拦一轮。"""
        path = self._unprotected_flag_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("退出保护标记读不出来，按未对账处理: {}", exc)
            return {"reason": "unreadable", "positions": {}}
        if not isinstance(raw, dict):
            logger.warning("退出保护标记格式不对，按未对账处理: {}", path)
            return {"reason": "unreadable", "positions": {}}
        return raw

    def _clear_unprotected_flag(self) -> None:
        """清掉退出保护标记。"""
        try:
            self._unprotected_flag_path().unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            logger.warning("清除退出保护标记失败: {}", exc)

    def _has_open_position(self) -> bool:
        """程序账上还有没有非空仓。"""
        for pos in (getattr(self.strategy, "positions", {}) or {}).values():
            side = str(getattr(pos, "side", "flat") or "flat")
            volume = int(getattr(pos, "volume", 0) or 0)
            if side != "flat" and volume > 0:
                return True
        return False

    def _has_open_risk(self) -> bool:
        """有持仓或柜台还有没走完的委托，重启后都要先对账。"""
        if self._has_open_position():
            return True
        check = getattr(self.trader, "has_working_order", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询未完成委托失败，按有挂单处理: {}", exc)
            return True

    def _write_unprotected_flag(self, reason: str) -> None:
        """退出前还有持仓/挂单就落个标记，下次启动先对账再恢复报单。"""
        if not self._has_open_risk():
            self._clear_unprotected_flag()
            return
        check = getattr(self.trader, "has_working_order", None)
        working = bool(check()) if callable(check) else False
        positions = snapshot_positions(self.strategy)
        payload = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "reason": str(reason or ""),
            "positions": positions,
            "working_orders": working,
        }
        try:
            path = self._unprotected_flag_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            logger.warning("写退出保护标记失败: {}", exc)
        self._record_event("exit_with_positions", reason=reason, positions=positions)
        try:
            from src.ops.alerts import emit_alert

            emit_alert(
                "exit_with_positions",
                "退出时仍有持仓或挂单，重启后先对账再交易",
                level="error",
            )
        except Exception:  # noqa: BLE001
            logger.exception("写退出持仓告警失败")

    def _watchdog_threshold(self) -> float:
        """心跳停滞阈值：三个扫描周期，最少 180 秒。"""
        return max(3.0 * float(self.scan_interval), 180.0)

    def _check_guardian_actions(self) -> None:
        """
        上次运行如果被冷备守护平过仓，启动时提醒一句。

        启动对账本来就在报单前跑三方核对，这里只负责让人知道「仓不是自己平的」。
        """
        actions = self.root / "data" / "guardian_actions.jsonl"
        if not actions.exists():
            return
        try:
            if actions.stat().st_mtime <= Path(self.state_path).stat().st_mtime:
                return
            last_row: dict[str, Any] = {}
            for line in actions.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    last_row = json.loads(line)
            stamp = str(last_row.get("ts") or "")
            run_id = str(last_row.get("run_id") or "")
            message = f"冷备守护在 {stamp or '上次运行'} 平过仓（主进程 {run_id[-6:]}），已按对账流程先核对柜台"
        except (OSError, json.JSONDecodeError):
            logger.exception("读取冷备守护动作记录失败")
            return
        self._record_event("guardian_flatten_seen", run_id=run_id, ts_guardian=stamp)
        try:
            from src.ops.alerts import emit_alert

            emit_alert("guardian_flatten", message, level="error")
        except Exception:  # noqa: BLE001
            logger.exception("写冷备守护告警失败")

    def _holiday_min_cover_days(self) -> int:
        """节假日表至少要覆盖未来多少天；配置写坏时按 60 天。"""
        try:
            return max(int((self.cfg.get("schedule") or {}).get("holiday_min_cover_days", 60)), 0)
        except (TypeError, ValueError):
            return 60

    def _check_holiday_calendar(self) -> bool:
        """
        实盘启动检查休市日历覆盖范围，缺年份时只禁止开仓。

        用 risk.halt 而不是暂停报单：熔断不拦平仓，止损和收盘清仓还能正常执行，
        只是不再开新仓。补上日历年重启后会自动解除这条限制。
        """
        if str((self.cfg.get("trade") or {}).get("executor", "sim")) != "ctp":
            return True
        if bool(self.cfg.get("dry_run", True)):
            return True
        calendar = getattr(self, "holiday_calendar", None)
        if calendar is None:
            # 没配外置日历：沿用内置表，保持旧行为。
            return True
        min_days = self._holiday_min_cover_days()
        if calendar_coverage_ok(calendar, min_days=min_days):
            if self.risk.halted and self.risk.halt_kind == "holiday_calendar_missing":
                self.risk.clear_halt("holiday_calendar_missing")
                logger.info("节假日表已覆盖到 {}，解除开仓限制", calendar.covered_until())
            return True
        horizon = (datetime.now() + timedelta(days=min_days)).date()
        covered = calendar.covered_until()
        message = f"节假日表只配到 {covered}，未覆盖 {horizon}，已禁止开仓；请补 config/holidays.yaml"
        if self.risk.halted and self.risk.halt_kind == "holiday_calendar_missing":
            # 已经在同一条限制里：只刷新原因，不重复写事件和告警。
            self.risk.halt_reason = message
            return False
        self.risk.set_halt("holiday_calendar_missing", message)
        self._record_event(
            "holiday_calendar_missing",
            covered_until=str(covered),
            required=str(horizon),
        )
        try:
            from src.ops.alerts import emit_alert

            emit_alert("holiday_calendar_missing", message, level="error")
        except Exception:  # noqa: BLE001
            logger.exception("写节假日表缺失告警失败")
        return False

    def _watchdog_snapshot(self) -> dict[str, Any]:
        """看门狗要看的运行快照。"""
        from src.utils.session_clock import in_trading_sessions

        return {
            "last_scan_at": self.last_scan_at,
            "has_position": self._has_open_position(),
            "in_session": in_trading_sessions(self.sessions, now=datetime.now()),
            "positions": snapshot_positions(self.strategy),
        }

    def _on_watchdog_stale(self, snap: dict[str, Any]) -> None:
        """主循环停摆：停报单、熔断、留标记并告警。"""
        self.set_orders_paused(True)
        self.risk.set_halt("stale_heartbeat", "主循环超过阈值没有心跳，已暂停报单")
        self._write_unprotected_flag("watchdog_stale")
        age = float(snap.get("age_sec") or 0)
        self._record_event("watchdog_stale", age_sec=snap.get("age_sec"))
        try:
            from src.ops.alerts import emit_alert

            emit_alert("heartbeat_stale", f"主循环 {int(age)} 秒没有心跳，已暂停报单", level="error")
        except Exception:  # noqa: BLE001
            logger.exception("写心跳停滞告警失败")

    def _start_watchdog(self) -> None:
        """启动主循环心跳看门狗。界面关掉也照跑。"""
        if not bool((self.cfg.get("runtime") or {}).get("watchdog_enabled", True)):
            return
        if getattr(self, "_watchdog", None) is not None:
            return
        from src.ops.watchdog import TradingWatchdog

        threshold = self._watchdog_threshold()
        self._watchdog = TradingWatchdog(
            get_snapshot=self._watchdog_snapshot,
            stale_after_sec=threshold,
            on_stale=self._on_watchdog_stale,
        )
        self._watchdog.start()
        self._record_event("watchdog_started", stale_after_sec=threshold)

    def _stop_watchdog(self) -> None:
        """停止看门狗。退出路径里调用，异常不往外抛。"""
        watchdog = getattr(self, "_watchdog", None)
        if watchdog is None:
            return
        try:
            watchdog.stop()
        except Exception:  # noqa: BLE001
            logger.exception("停止看门狗失败")
        finally:
            self._watchdog = None

    def _protective_status(self) -> str:
        """当前有没有柜台原生保护单。CTP 条件单还没接，固定是 none。"""
        try:
            from src.trader.protective import create_protective_adapter

            adapter = getattr(self, "_protective_adapter", None)
            if adapter is None:
                adapter = create_protective_adapter(self.trader)
                self._protective_adapter = adapter
            return "native" if adapter.supported() else "none"
        except Exception as exc:  # noqa: BLE001
            logger.debug("检查柜台保护单失败: {}", exc)
            return "unknown"

    def _already_took_recent_entry(self, symbol: str, closed: Any, lookback: int) -> bool:
        """
        最近几根信号 K 线里，是不是已经按信号开过仓。

        重启后若磁盘还记着 last_entry_bars，即使持仓字段异常，也不再回溯补同一根。
        """
        last_map = self.strategy.get_last_entry_bars()
        last_id = self.strategy.get_last_entry_bar(symbol)
        if not last_id:
            key = next(
                (name for name in last_map if str(name).lower() == str(symbol).lower()),
                None,
            )
            last_id = self.strategy.get_last_entry_bar(str(key)) if key is not None else ""
        if not last_id or closed is None or len(closed) == 0:
            return False
        n = max(1, int(lookback))
        recent = closed.iloc[-n:]
        for label in recent.index:
            if str(label) == last_id:
                return True
        return False

    def _record_event(self, event: str, **kwargs: Any) -> None:
        """把关键事件追加写入 data/events.jsonl；文件过大时先轮转。"""
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "event": event,
            **kwargs,
        }
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
        """每轮按当前扫描清单读各品种的时段和清仓时刻。"""
        from src.utils.session_clock import (
            flatten_times_for_symbol,
            format_session_text,
            parse_hhmm,
            sessions_for_symbol,
        )

        symbols = list(getattr(self, "scan_symbols", None) or []) or [self.default_symbol]
        symbol_sessions: dict[str, list[list[str]]] = {}
        symbol_flats: dict[str, list[str]] = {}
        for name in symbols:
            symbol_sessions[name] = sessions_for_symbol(self.cfg, name)
            symbol_flats[name] = flatten_times_for_symbol(self.cfg, name)
        self._symbol_sessions = symbol_sessions
        self._symbol_flatten_times = symbol_flats

        symbol = self._primary_symbol() or (symbols[0] if symbols else "")
        self.sessions = symbol_sessions.get(symbol) or sessions_for_symbol(self.cfg, symbol)
        flats = symbol_flats.get(symbol) or flatten_times_for_symbol(self.cfg, symbol)
        if hasattr(self.strategy, "entry_sessions"):
            self.strategy.entry_sessions = [
                (parse_hhmm(a), parse_hhmm(b)) for a, b in self.sessions
            ]
        if hasattr(self.strategy, "flatten_times"):
            self.strategy.flatten_times = [parse_hhmm(x) for x in flats]
        if hasattr(self.strategy, "bind_symbol_sessions"):
            self.strategy.bind_symbol_sessions(symbol)
        key = "|".join(
            f"{name}:{symbol_sessions.get(name)}:{symbol_flats.get(name)}" for name in symbols
        )
        if key != self._live_session_key:
            if self._multi_symbol_enabled():
                for name in symbols:
                    logger.info(
                        "{} 交易时段 {} ，清仓 {}",
                        name,
                        format_session_text(symbol_sessions.get(name)) or "未写",
                        ",".join(symbol_flats.get(name) or []) or "未写",
                    )
            else:
                logger.info(
                    "{} 交易时段 {} ，清仓 {}",
                    symbol,
                    format_session_text(self.sessions) or "未写",
                    ",".join(flats) or "未写",
                )
            self._live_session_key = key

    def _symbol_sessions_for(self, symbol: str) -> list[list[str]]:
        """某个品种自己的交易时段。多品种关闭或没缓存时按品种表重算。"""
        if self._multi_symbol_enabled():
            cached = getattr(self, "_symbol_sessions", None) or {}
            if symbol in cached:
                return cached[symbol]
            from src.utils.session_clock import sessions_for_symbol

            return sessions_for_symbol(self.cfg, symbol)
        return list(getattr(self, "sessions", None) or [])

    def _symbol_flatten_clocks(self, symbol: str) -> list[str]:
        """某个品种自己的清仓钟点。"""
        if self._multi_symbol_enabled():
            cached = getattr(self, "_symbol_flatten_times", None) or {}
            if symbol in cached:
                return list(cached[symbol])
        from src.utils.session_clock import flatten_times_for_symbol

        return list(flatten_times_for_symbol(self.cfg, symbol))

    def _symbol_flatten_due(self, symbol: str, now: datetime | None = None) -> bool:
        """这个品种现在是不是到自己的收盘清仓钟点了。"""
        from src.utils.session_clock import flatten_times_for_symbol, in_flatten_window

        if not bool(getattr(self.strategy, "flatten_at_close", False)):
            return False
        clocks = self._symbol_flatten_clocks(symbol)
        if not clocks:
            clocks = list(flatten_times_for_symbol(self.cfg, symbol))
        # 夜日连续策略只在日盘收盘清仓；这里跟 daily_body.is_flatten_time 保持同一口径。
        linked = getattr(self.strategy, "_linked", None)
        if callable(linked) and linked():
            from src.utils.session_clock import SCOPE_NIGHT, session_kind

            clocks = [clock for clock in clocks if session_kind(clock) != SCOPE_NIGHT]
        return in_flatten_window(clocks, now or datetime.now())

    def _any_flatten_due(self, now: datetime | None = None) -> bool:
        """任一扫描品种到清仓钟点就算。多品种关闭时仍问策略自己。"""
        if self._multi_symbol_enabled():
            symbols = list(getattr(self, "scan_symbols", None) or [])
            return any(self._symbol_flatten_due(name, now) for name in symbols)
        try:
            return bool(self.strategy.is_flatten_time(now))
        except TypeError:
            return bool(self.strategy.is_flatten_time())

    def _any_symbol_in_session(self, symbols: list[str], now: datetime | None = None) -> bool:
        """这些品种里是否至少有一个在自己的交易时段内。"""
        for name in symbols:
            sessions = self._symbol_sessions_for(name)
            if not sessions or in_trading_session(sessions, now):
                return True
        return False

    def _scan_session_active(self, now: datetime | None = None) -> bool:
        """本轮扫描是否还有意义：多品种看各自时段，单品种保持原口径。"""
        if self._multi_symbol_enabled():
            names = list(getattr(self, "scan_symbols", None) or [])
            return self._any_symbol_in_session(names, now) or self._any_flatten_due(now)
        sessions = getattr(self, "sessions", None)
        if not sessions:
            return True
        if in_trading_session(sessions, now):
            return True
        try:
            return bool(self.strategy.is_flatten_time(now))
        except TypeError:
            return bool(self.strategy.is_flatten_time())

    def _read_multi_cfg(self) -> dict[str, Any]:
        """读 live.multi_symbol。没写或写错都当成关闭，不影响单品种老行为。"""
        live = self.cfg.get("live") if isinstance(self.cfg, dict) else None
        box = (live or {}).get("multi_symbol") if isinstance(live, dict) else None
        return box if isinstance(box, dict) else {}

    def _multi_symbol_enabled(self) -> bool:
        """多品种开仓开关。用 __new__ 建的轻量测试对象没这个属性时按关闭处理。"""
        return bool(getattr(self, "_multi_enabled", False))

    def _is_live_account(self) -> bool:
        """当前执行通道是不是真实账户。仿真/自测都返回 False。"""
        trade = self.cfg.get("trade") if isinstance(self.cfg, dict) else None
        ctp = (trade or {}).get("ctp") if isinstance(trade, dict) else None
        box = ctp if isinstance(ctp, dict) else {}
        return str(box.get("account_kind") or "").strip().lower() == "live"

    def _validate_multi_symbol_config(self) -> None:
        """多品种启动前体检：白名单不能为空，同一合约不能配两条腿。"""
        if not self._multi_symbols:
            logger.warning("live.multi_symbol 没有配置品种白名单，按关注列表全部品种扫描")
        strategy_box = self.cfg.get("strategy") if isinstance(self.cfg, dict) else None
        legs = (strategy_box or {}).get("legs") if isinstance(strategy_box, dict) else None
        seen: set[str] = set()
        duplicated: set[str] = set()
        for item in legs or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("symbol") or "").strip().lower()
            if not name:
                continue
            if name in seen:
                duplicated.add(name)
            seen.add(name)
        if duplicated:
            raise ValueError(
                "strategy.legs 同一合约只能有一条腿，重复："
                + "、".join(sorted(duplicated))
            )

    def _primary_symbol(self) -> str:
        """当前选中品种：界面/配置指定的那个；找不到就用关注列表第一个。"""
        symbols = list(getattr(self, "symbols", None) or [])
        if not symbols:
            return str(getattr(self, "default_symbol", "") or "")
        wanted = str(getattr(self, "default_symbol", "") or "").strip().lower()
        return next((name for name in symbols if name.lower() == wanted), symbols[0])

    def _pick_scan_symbols(self) -> list[str]:
        """
        常驻扫描清单。

        单品种（默认）：当前选中品种 + 程序账上所有非空持仓。
        多品种开关打开：白名单内的空仓品种也扫，选中品种仍排第一。
        只扫当前品种会让「已移出关注列表的旧仓」裸奔，没人止损/收盘清仓。
        """
        out: list[str] = []
        symbols = list(getattr(self, "symbols", None) or [])
        if self._multi_symbol_enabled() and symbols:
            known = {name.lower(): name for name in symbols}
            primary = self._primary_symbol()
            if primary:
                out.append(known.get(primary.lower(), primary))
            wanted = list(getattr(self, "_multi_symbols", None) or symbols)
            warned = getattr(self, "_multi_warned", None)
            if warned is None:
                warned = set()
                self._multi_warned = warned
            for item in wanted:
                symbol = known.get(str(item).strip().lower())
                if symbol is None:
                    key = str(item).strip().lower()
                    if key and key not in warned:
                        warned.add(key)
                        logger.warning(
                            "live.multi_symbol 里的 {} 不在 strategy.symbols，已跳过", item
                        )
                    continue
                if symbol.lower() not in {name.lower() for name in out}:
                    out.append(symbol)
        elif symbols:
            wanted_name = str(self.default_symbol or "").strip().lower()
            current = next(
                (symbol for symbol in symbols if symbol.lower() == wanted_name),
                symbols[0],
            )
            out.append(current)
        seen = {symbol.lower() for symbol in out}
        positions = getattr(self.strategy, "positions", None) or {}
        for name, pos in positions.items():
            side = str(getattr(pos, "side", "flat") or "flat")
            volume = int(getattr(pos, "volume", 0) or 0)
            if side not in ("long", "short") or volume <= 0:
                continue
            symbol = str(name)
            if symbol.lower() in seen:
                continue
            out.append(symbol)
            seen.add(symbol.lower())
        return out

    def _refresh_scan_symbols(self) -> None:
        """重新计算扫描清单；启动恢复、每轮扫描、手工开仓后都可安全调用。"""
        picked = self._pick_scan_symbols()
        if picked != self.scan_symbols:
            logger.info("扫描清单更新: {} → {}", self.scan_symbols, picked)
            self.scan_symbols = picked

    def _load_persisted_state(self) -> None:
        """启动时恢复持仓/当日盈亏/开仓计数（主流程序标配）。"""
        st = load_state(self.state_path)
        self.trading_day = str(st.get("trading_day") or futures_trading_day())
        restore_positions(self.strategy, st.get("positions") or {})
        restore_risk = getattr(self.risk, "restore", None)
        if callable(restore_risk):
            self.trading_day = str(restore_risk(st) or self.trading_day)
        else:
            # 兼容旧测试和外部嵌入的轻量风控对象。
            self.risk.daily_pnl = float(st.get("daily_pnl", 0) or 0)
            self.risk.orders_today = int(st.get("orders_today", 0) or 0)
            self.risk.open_orders_today = int(st.get("open_orders_today", self.risk.orders_today) or 0)
            self.risk.halted = bool(st.get("halted", False))
            self.risk.halt_reason = str(st.get("halt_reason", "") or "")
            self.risk.halt_kind = str(st.get("halt_kind", "") or "")
        # 从策略持仓重建风控净仓
        self._rebuild_risk_positions()
        last_bars = st.get("last_entry_bars") or {}
        if isinstance(last_bars, dict) and last_bars:
            # 重启后通过公共接口恢复「哪根已经开过」。
            self.strategy.restore_last_entry_bars(last_bars)
        # 日内实体等：把「这一段已经开过」灌回去，避免重启后又开第二次。
        self._restore_used_sessions(st)
        # 以信号账户为准对一次账：持仓、风控净仓、当日盈亏都和账本对齐。
        self._reconcile_books()
        # 再和柜台对一次：柜台有、账本没有的仓要冻结，等人工认领。
        self._reconcile_broker_at_startup()
        # 恢复出来的隔夜仓也要纳入扫描，否则没人管它的止损和收盘清仓。
        self._refresh_scan_symbols()
        # 对账可能改了净仓、日亏或熔断，启动收尾再保存一次最终状态。
        save_risk = getattr(self.risk, "save_state", None)
        if callable(save_risk):
            try:
                save_risk(self.trading_day)
            except Exception as exc:  # noqa: BLE001
                logger.warning("启动对账后的风控状态保存失败: {}", exc)
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
        """从开仓时间或 K 线时间推日盘/夜盘名额键，格式与日内实体一致。

        名额键的日期用期货交易日 futures_trading_day：21:00 之后归入下一交易日，
        这样 21:00 与次日 00:30 的夜盘共用同一个键，重启后名额保持。
        """
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
        dt = stamp.to_pydatetime()
        clock = dt.time()
        day_open = dtime(9, 0)
        night_open = dtime(21, 0)
        if day_open <= clock < night_open:
            anchor = day_open
        else:
            # 包含 >=21:00 和 <09:00（跨零点的夜盘），都算夜盘。
            anchor = night_open
        fday = futures_trading_day(dt)
        return f"{fday.isoformat()} {anchor.strftime('%H:%M')}"

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
                side=cast(Any, want_side),
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
        self._rebuild_risk_positions()

        old_daily = self.risk.daily_pnl
        # 柜台账的盈亏变动不记进 changes，用账本加总会把日亏打成 0，熔断会短暂失效。
        if str(book.get("funding") or "") == "ctp":
            logger.info("柜台账当日盈亏跟柜台，不对账本改日亏（当前 {:.2f}）", old_daily)
        else:
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


    def _ledger_rows(self) -> dict[str, dict[str, Any]]:
        """信号账户里还没平的仓，按小写品种做键。"""
        from src.account.signal_book import load_book, open_position_rows

        book = load_book(self.root)
        if book is None:
            return {}
        return {
            str(row.get("symbol") or "").strip().lower(): row
            for row in open_position_rows(book)
        }

    def _ledger_nets(self) -> dict[str, int]:
        """信号账户净持仓：多仓为正，空仓为负。"""
        nets: dict[str, int] = {}
        for key, row in self._ledger_rows().items():
            volume = int(row.get("volume") or 0)
            if volume <= 0:
                continue
            nets[key] = volume if str(row.get("side") or "") == "long" else -volume
        return nets

    def _reconcile_repair_enabled(self) -> bool:
        """配置里允许按对账结果自动修复持仓。默认允许。"""
        trade = self.cfg.get("trade") or {}
        ctp = (trade.get("ctp") or {}) if isinstance(trade, dict) else {}
        return bool((ctp or {}).get("reconcile_repair", True))

    def _call_reconcile(self) -> Any:
        """向柜台对一次账并取回最近报告。旧通道不支持 ledger 参数时退回单参调用。"""
        try:
            self.trader.reconcile(self._internal_nets(), ledger=self._ledger_nets())
        except TypeError:
            self.trader.reconcile(self._internal_nets())
        return getattr(self.trader, "last_report", None)

    def _position_key(self, symbol: str) -> str:
        """策略持仓字典里和这个品种对应的键（大小写不敏感）。"""
        positions = getattr(self.strategy, "positions", {}) or {}
        return next(
            (name for name in positions if str(name).lower() == str(symbol).lower()),
            str(symbol),
        )

    def _install_position(
        self,
        symbol: str,
        net: int,
        entry_price: float,
        origin: str,
        *,
        stop_price: float = 0.0,
    ) -> None:
        """按净仓重建一个持仓。同方向旧仓的止损保留，方向变了就丢掉旧止损。"""
        from src.strategy.types import PositionState

        positions = getattr(self.strategy, "positions", None)
        if positions is None:
            return
        key = self._position_key(symbol)
        current = positions.get(key)
        lots = abs(int(net))
        if lots <= 0:
            positions[key] = PositionState(symbol=key)
            return
        side = "long" if int(net) > 0 else "short"
        initial = 0.0
        trail = 0.0
        opened_at = ""
        booked_symbol = key
        if current is not None:
            opened_at = str(getattr(current, "opened_at", "") or "")
            booked_symbol = str(getattr(current, "symbol", "") or key)
            same_side = str(getattr(current, "side", "flat") or "flat") == side
            if same_side:
                initial = float(getattr(current, "initial_stop", 0) or 0)
                trail = float(getattr(current, "trail_stop", 0) or 0)
            # 手数、方向、价格没变就不要再写一遍「对账校准」，也不换对象。
            if (
                same_side
                and int(getattr(current, "volume", 0) or 0) == lots
                and abs(float(getattr(current, "entry_price", 0) or 0) - float(entry_price or 0)) < 1e-9
                and str(getattr(current, "origin", "") or "") == str(origin or "")
            ):
                return
        if float(stop_price or 0) > 0:
            initial = float(stop_price)
            trail = float(stop_price)
        if trail <= 0:
            trail = initial
        if initial <= 0:
            initial = trail
        positions[key] = PositionState(
            side=cast(Any, side),
            volume=lots,
            entry_price=float(entry_price or 0),
            initial_stop=initial,
            trail_stop=trail,
            trail_armed=bool(trail > 0),
            symbol=booked_symbol,
            opened_at=opened_at,
            origin=origin,
        )
        logger.warning(
            "对账校准 {} 为 {} {} 手 @{}（来源 {}）", key, side, lots, entry_price, origin
        )

    def _reconcile_price(self, symbol: str, net: int) -> float:
        """重建仓位的均价：柜台开仓成本 → 旧仓开仓价 → 最新价。"""
        price = 0.0
        gateway = getattr(self.trader, "gateway", None)
        views = getattr(gateway, "positions", None) if gateway is not None else None
        view = None
        if isinstance(views, dict):
            view = views.get(symbol)
            if view is None:
                view = next(
                    (item for name, item in views.items() if str(name).lower() == symbol),
                    None,
                )
        if view is not None:
            multiplier = float(self.sizer.multiplier_for(symbol))
            if int(net) > 0:
                price = (
                    position_avg_price(view.long_open_cost, view.long_lots, multiplier) or 0.0
                )
            else:
                price = (
                    position_avg_price(view.short_open_cost, view.short_lots, multiplier) or 0.0
                )
        if price <= 0:
            pos = self._position_for(symbol)
            if pos is not None:
                price = float(getattr(pos, "entry_price", 0) or 0)
        if price <= 0:
            try:
                price = float(self.kline.latest_price(symbol) or 0)
            except Exception:  # noqa: BLE001
                price = 0.0
        return price

    def _rebuild_risk_positions(self) -> None:
        """风控净仓按策略持仓重建，避免两处各记一套。"""
        positions = getattr(self.strategy, "positions", {}) or {}
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

    def _safe_event(self, event: str, **kwargs: Any) -> None:
        """写事件日志失败也不能拖垮对账流程。"""
        try:
            self._record_event(event, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("事件 {} 写入失败: {}", event, exc)

    def _reconcile_alert(self, kind: str, message: str, level: str = "warning") -> None:
        """对账告警；告警通道异常时只写日志。"""
        try:
            from src.ops.alerts import emit_alert

            emit_alert(kind, message, level=level)
        except Exception as exc:  # noqa: BLE001
            logger.warning("对账告警 {} 发送失败: {}", kind, exc)

    def _apply_reconcile(self, report: Any, phase: str) -> None:
        """
        按一次三方对账结果修复程序内存。

        phase: startup（启动/刚连上柜台）或 runtime（每轮扫描）。
        柜台没查成就只告警、不动内存；幽灵仓在 runtime 要连续两轮确认才补记平仓。
        """
        if report is None:
            return
        ctx = getattr(self, "_own_fill_context", None)

        def _fill_ctx(name: str) -> tuple[bool, str]:
            if not callable(ctx):
                return False, "open"
            try:
                return ctx(name)
            except Exception:  # noqa: BLE001
                return False, "open"

        state = report.to_dict()
        state["phase"] = phase
        self._reconcile_state = state
        if not report.queried:
            self._reconcile_alert("reconcile_unavailable", "柜台对账未完成，本轮不修复持仓")
            self._safe_event("reconcile_unavailable", phase=phase, diffs=list(report.diffs))
            return

        real_unclaimed: list[str] = []
        for name in report.unclaimed:
            fresh, kind = _fill_ctx(name)
            if fresh:
                continue
            real_unclaimed.append(name)
        real_phantom: list[str] = []
        for name in report.phantom:
            fresh, _kind = _fill_ctx(name)
            if fresh:
                continue
            real_phantom.append(name)
        stale_names = {
            str(name).strip().lower()
            for name in list(report.unclaimed) + list(report.phantom)
            if _fill_ctx(name)[0]
        }
        state["unclaimed"] = list(real_unclaimed)
        state["phantom"] = list(real_phantom)
        if stale_names:
            state["diffs"] = [
                str(item)
                for item in (state.get("diffs") or [])
                if not any(token in str(item).lower() for token in stale_names)
            ]
        if not real_unclaimed and not real_phantom and not state.get("diffs"):
            state["ok"] = True
        self._reconcile_state = state
        if real_unclaimed:
            self._reconcile_alert(
                "position_unclaimed", "柜台有未认领持仓：" + "、".join(real_unclaimed)
            )
        if real_phantom:
            self._reconcile_alert(
                "position_phantom", "账本有、柜台无的持仓：" + "、".join(real_phantom)
            )
        if report.diffs:
            noisy = False
            for name in report.unclaimed + list(report.phantom):
                fresh, _kind = _fill_ctx(name)
                if fresh:
                    noisy = True
                    break
            if not noisy:
                self._reconcile_alert("reconcile_mismatch", "；".join(report.diffs))

        # 幽灵仓：runtime 连续两轮确认才补记，避免刚成交、柜台还没更新时误平。
        phantom_now = {str(name).strip().lower() for name in real_phantom}
        for name in list(self._phantom_rounds):
            if name not in phantom_now:
                self._phantom_rounds.pop(name, None)
        confirmed: set[str] = set()
        for name in phantom_now:
            if phase == "runtime":
                self._phantom_rounds[name] = int(self._phantom_rounds.get(name, 0)) + 1
                if self._phantom_rounds[name] >= 2:
                    confirmed.add(name)
            else:
                confirmed.add(name)

        if not self._reconcile_repair_enabled():
            self._persist_state()
            return

        broker = {str(k).lower(): int(v or 0) for k, v in (report.broker or {}).items()}
        ledger = {str(k).lower(): int(v or 0) for k, v in (report.ledger or {}).items()}
        memory = {str(k).lower(): int(v or 0) for k, v in (report.memory or {}).items()}
        rows = self._ledger_rows()
        from src.strategy.types import PositionState

        positions = getattr(self.strategy, "positions", None)
        for key in sorted(set(broker) | set(ledger) | set(memory)):
            at_broker = int(broker.get(key, 0))
            at_ledger = int(ledger.get(key, 0))
            at_memory = int(memory.get(key, 0))
            if at_broker != 0 and at_ledger == 0:
                fresh, kind = _fill_ctx(key)
                if fresh and kind == "close":
                    # 刚平仓：账本已是 0，柜台查询还停在旧仓。不能当成未认领再开回来。
                    logger.info("{} 刚平仓，柜台持仓可能还没刷新，本轮不认领、不冻结", key)
                    continue
                if fresh:
                    # 刚开仓：柜台已有仓、账本还是 0。按柜台补进账本，不要冻结等人。
                    self._install_position(
                        key, at_broker, self._reconcile_price(key, at_broker), "broker"
                    )
                    adopt = getattr(self, "adopt_broker_position", None)
                    if callable(adopt):
                        try:
                            ok, msg = adopt(key)
                            logger.info("{} 刚开仓，已按柜台持仓自动认领：{}", key, msg)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("{} 自动认领失败：{}", key, exc)
                    self._frozen_symbols.discard(key)
                    continue
                # 柜台有、账本没有：补进内存并冻结，等人工认领。
                self._install_position(key, at_broker, self._reconcile_price(key, at_broker), "broker")
                self._frozen_symbols.add(key)
            elif at_broker == 0 and at_ledger != 0 and key in confirmed:
                # 账本有、柜台没有：两轮确认后补记平仓。
                price = self._reconcile_price(key, at_ledger)
                if price > 0:
                    self.manual_close(key, price, reason="对账确认柜台无持仓，补记平仓")
                    if positions is not None:
                        booked = self._position_key(key)
                        positions[booked] = PositionState(symbol=booked)
                    self._frozen_symbols.discard(key)
                    self._phantom_rounds.pop(key, None)
                else:
                    self._safe_event("reconcile_phantom_no_price", symbol=key, ledger=at_ledger)
            elif at_broker != 0 and at_ledger != 0 and at_broker != at_ledger:
                # 柜台和账本各说各话：内存对齐柜台，不冻结（等人工核对账本）。
                self._install_position(key, at_broker, self._reconcile_price(key, at_broker), "broker")
            elif at_broker == 0 and at_ledger == 0:
                if at_memory != 0 and positions is not None:
                    booked = self._position_key(key)
                    positions[booked] = PositionState(symbol=booked)
                self._frozen_symbols.discard(key)
            elif at_broker == at_ledger and at_broker != 0 and at_memory != at_ledger:
                # 柜台和账本一致，只是内存不同：按账本行重建。
                row = rows.get(key) or {}
                side = str(row.get("side") or ("long" if at_ledger > 0 else "short"))
                volume = int(row.get("volume") or abs(at_ledger))
                net = volume if side == "long" else -volume
                price = float(row.get("entry_price") or 0) or self._reconcile_price(key, net)
                self._install_position(
                    key, net, price, "ledger", stop_price=float(row.get("stop_price") or 0)
                )

        self._rebuild_risk_positions()
        self._persist_state()
        self._safe_event("reconcile_applied", phase=phase, ok=bool(report.ok))

    def _reconcile_broker_at_startup(self) -> None:
        """启动时先和柜台对一次账；柜台还没登录就推迟到登录完成后再做。"""
        if not self.trader_capabilities.can_query_positions or self.cfg.get("dry_run", True):
            return
        if not hasattr(self.trader, "reconcile"):
            return
        session = getattr(getattr(self.trader, "gateway", None), "session", None)
        if session is not None and not getattr(session, "ready", False):
            self._startup_reconcile_pending = True
            logger.info("柜台还没登录，启动对账推迟到登录完成后再做")
            return
        self._apply_reconcile(self._call_reconcile(), phase="startup")
        self._startup_reconcile_pending = False

    def _persist_state(self) -> None:
        """把关键运行态写盘。"""
        save_risk = getattr(self.risk, "save_state", None)
        if callable(save_risk):
            try:
                save_risk(self.trading_day)
            except Exception as exc:  # noqa: BLE001
                # 新状态端口故障时仍保留旧 bot_state，升级/回退都有恢复依据。
                logger.warning("风控状态保存失败，继续写旧运行状态: {}", exc)
        save_state(
            self.state_path,
            {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "pid": os.getpid(),
                "trading_day": self.trading_day,
                "daily_pnl": self.risk.daily_pnl,
                "orders_today": self.risk.orders_today,
                "open_orders_today": self.risk.open_orders_today,
                "halted": self.risk.halted,
                "halt_reason": self.risk.halt_reason,
                "halt_kind": self.risk.halt_kind,
                "positions": snapshot_positions(self.strategy),
                "last_entry_bars": self.strategy.get_last_entry_bars(),
                "used_sessions": dict(getattr(self.strategy, "_used_session", {}) or {}),
                "used_bars": dict(getattr(self.strategy, "_used_bar", {}) or {}),
                "heartbeat_at": self.heartbeat_at,
                "last_scan_at": self.last_scan_at,
                "last_error": self.last_error,
                "symbols": self.symbols,
                "dry_run": bool(self.cfg.get("dry_run", True)),
                "executor": str((self.cfg.get("trade") or {}).get("executor", "")),
                "reconcile": dict(self._reconcile_state or {}),
                "frozen_symbols": sorted(self._frozen_symbols),
                "protective": str(getattr(self, "_protective_state", "none")),
                "unprotected": self._unprotected_flag_path().exists(),
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
        # 只解除「日亏」熔断；崩溃、遗留委托、对账不符必须人工核对。
        self.risk.clear_halt("daily_loss")
        self.risk.last_order_ts = 0.0
        self.risk.last_open_ts = 0.0
        # 隔夜持仓不归零，只把切换时的仓位留一份记录，方便第二天对账。
        carried = snapshot_positions(self.strategy)
        held_text = format_position_text(carried)
        logger.info(
            "交易日切换 {} → {}，已清零当日盈亏、下单次数；"
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
        # 新交易日先复查上次点击后是否留下未完成委托，有就继续熔断。
        self._check_pending_intent()
        self._persist_state()
        return True


    def _dynamic_status_text(self) -> str:
        """运行台动态减仓一行。算不出波动时只看连亏。"""
        from src.risk.dynamic import (
            DynamicExposure,
            atr_ratio,
            describe_status,
            live_equity_and_peak_or_error,
        )

        dyn = DynamicExposure.from_config(self.cfg)
        bars = next(iter(self._last_signal_bars.values()), None) if self._last_signal_bars else None
        ratio = atr_ratio(bars, period=dyn.atr_period, lookback=dyn.atr_lookback) if bars is not None else None
        equity, peak, _ = live_equity_and_peak_or_error(self.root)
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
        from src.risk.dynamic import (
            DynamicExposure,
            drawdown_frac,
            live_equity_and_peak_or_error,
        )
        from src.strategy.types import StrategyAction, StrategyDecision

        dyn = DynamicExposure.from_config(self.cfg)
        if not dyn.enabled or not dyn.reduce_held:
            return None
        pos = self._position_for(symbol)
        held = abs(int(pos.volume)) if pos and getattr(pos, "volume", 0) else 0
        if held < 2:
            return None
        equity, peak, _ = live_equity_and_peak_or_error(self.root)
        dd = drawdown_frac(equity, peak)
        target = dyn.held_target(held, dd)
        if target is None or target >= held:
            return None
        key = f"{symbol}|{dd}|{target}"
        if getattr(self, "_dyn_cut_key", "") == key or getattr(self, "_dyn_cut_pending", "") == key:
            return None
        cut = held - target
        self._dyn_cut_pending = key
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
        """
        近 N 笔对照最近回测，先核对策略、品种、周期是否同一口径。

        默认只报警。只有 runtime.health_block_entry 打开、且连续偏离达标时，
        才把「停开新仓」的标志交给风控闸门；止损和清仓不受影响。
        """
        try:
            from src.ops.health import entry_block_decision, maybe_emit_health
            from src.utils.paths import project_root

            strategy = self.cfg.get("strategy")
            strategy = strategy if isinstance(strategy, dict) else {}
            trade = self.cfg.get("trade")
            trade = trade if isinstance(trade, dict) else {}
            symbol = str(trade.get("symbol") or (self.symbols[0] if self.symbols else ""))
            report = maybe_emit_health(
                project_root(),
                engine=str(strategy.get("engine") or ""),
                symbol=symbol,
                timeframe=str(strategy.get("timeframe") or ""),
                cfg=self.cfg,
            )
            blocked, reason = entry_block_decision(self.cfg, report)
            self._health_blocked = blocked
            self._health_block_reason = reason
            data = report.as_dict()
            data["entry_blocked"] = blocked
            if blocked:
                data["message"] = reason
                logger.warning("策略健康停开新仓：{}", reason)
            self._health_report = data
        except Exception as exc:  # noqa: BLE001
            logger.debug("策略健康检查跳过：{}", exc)

    def sync_risk_limits(self, max_daily_loss: float | None = None) -> None:
        """
        把配置页刚保存的日亏限额同步给运行中的风控。

        常驻机器人启动时就把限额固定下来了，之后改配置只改文件不改内存；
        不同步的话机器人会一直按旧限额熔断、界面按新限额删告警。
        """
        if max_daily_loss is None:
            risk_cfg = self.cfg.get("risk") or {}
            try:
                max_daily_loss = float(risk_cfg.get("max_daily_loss", self.risk.max_daily_loss))
            except (TypeError, ValueError):
                return
        self.cfg.setdefault("risk", {})["max_daily_loss"] = max_daily_loss
        self.risk.set_max_daily_loss(float(max_daily_loss))

    def set_capital(self, balance: float) -> None:
        """
        账户页或账本改了余额后，把这笔钱同步给算手数的一侧。

        和下单共用交易锁：避免一轮下单正在按旧权益算手数时被改掉，
        出现同一批单前后口径不一致。UI 不再直接改 sizer / cfg。
        """
        amount = float(balance)
        with self._trade_lock:
            self.sizer.capital = amount
            self.cfg.setdefault("risk", {})["capital"] = amount

    def apply_runtime_config(self, cfg: dict[str, Any]) -> None:
        """
        配置页保存后，把运行中的扫描间隔、合约时段、合约表和日亏限额推给常驻机器人。

        和下单共用交易锁：避免下一轮扫描按旧参数算手数或旧时段开仓。
        UI 只发这一条命令，不再直接改 bot 的字段。
        """
        interval = int((cfg.get("schedule") or {}).get("scan_interval_sec") or 30)
        sessions = list((cfg.get("schedule") or {}).get("trading_sessions") or [])
        contracts = (cfg.get("risk") or {}).get("contracts")
        limit = float((cfg.get("risk") or {}).get("max_daily_loss") or 0)
        with self._trade_lock:
            self.scan_interval = interval
            self.cfg.setdefault("schedule", {})["trading_sessions"] = sessions
            if isinstance(contracts, dict):
                self.cfg.setdefault("risk", {})["contracts"] = contracts
            self._refresh_sessions()
            self.sync_risk_limits(limit)
            # 单笔最大手数改了要立刻生效；公共配置与当前券商档案取小，和启动时一致。
            ctp = (cfg.get("trade") or {}).get("ctp") or {}
            from src.trader.ctp_limits import effective_max_order_volume
            from src.trader.ctp_profile import resolve_ctp_profile

            resolved = resolve_ctp_profile(
                ctp,
                account=cfg.get("account") or {},
                kind=ctp.get("account_kind"),
            )
            max_volume = effective_max_order_volume(
                ctp.get("max_order_volume"), resolved.get("max_order_volume")
            )
            gateway = getattr(getattr(self, "trader", None), "gateway", None)
            settings = getattr(gateway, "settings", None) or getattr(
                getattr(self, "trader", None), "settings", None
            )
            if settings is not None and hasattr(settings, "max_order_volume"):
                settings.max_order_volume = max_volume

    def positions_snapshot(self) -> dict[str, dict[str, Any]]:
        """程序账持仓的只读副本，供界面与报告展示；改返回值不影响运行状态。"""
        with self._trade_lock:
            rows = getattr(self.strategy, "positions", None) or {}
            return {
                str(symbol): {
                    "side": str(getattr(pos, "side", "flat") or "flat"),
                    "volume": int(getattr(pos, "volume", 0) or 0),
                    "entry_price": float(getattr(pos, "entry_price", 0.0) or 0.0),
                    "opened_at": str(getattr(pos, "opened_at", "") or ""),
                }
                for symbol, pos in rows.items()
            }

    def note_force_close(self) -> None:
        """界面在后台线程未退出时强制关窗，记一条关键事件供复盘。"""
        self._record_event("force_close")

    def _record_event_throttled(
        self, event: str, key: str, *, cooldown_sec: float = 600.0, **kwargs: Any
    ) -> None:
        """持续异常只按间隔写关键事件，完整行情延迟另走 market_health.jsonl。"""
        now = time.monotonic()
        stamps = getattr(self, "_throttled_event_at", None)
        if stamps is None:
            stamps = {}
            self._throttled_event_at = stamps
        if now - float(stamps.get(key, float("-inf"))) < cooldown_sec:
            return
        stamps[key] = now
        self._record_event(event, **kwargs)

    def _capture_market_health(
        self,
        symbol: str,
        period: str,
        bars: Any = None,
        *,
        error: str = "",
    ) -> dict[str, Any]:
        """从 K 线 attrs 或 Provider 取健康记录；测试替身没有 attrs 时也保留基本年龄。"""
        raw = getattr(bars, "attrs", {}).get("market_health", {}) if bars is not None else {}
        health = dict(raw) if isinstance(raw, dict) else {}
        if not health:
            getter = getattr(self.kline, "get_market_health", None)
            if callable(getter):
                try:
                    stored = getter(symbol, str(period))
                    if isinstance(stored, dict):
                        health = dict(stored)
                except Exception:  # noqa: BLE001
                    pass
        age = bars_age_minutes(bars, symbol=symbol, period=str(period))
        stale_limit = stale_after_minutes(str(period), self.max_stale_minutes)
        health.setdefault("symbol", symbol)
        health.setdefault("period", str(period))
        health.setdefault("source", str(getattr(self.kline, "source", "") or ""))
        health.setdefault("fetch_started_at", "")
        health.setdefault(
            "received_at", datetime.now().astimezone().isoformat(timespec="milliseconds")
        )
        health.setdefault("download_ms", None)
        health.setdefault("latest_bar_time", str(bars.index[-1]) if bars is not None and len(bars) else "")
        health.setdefault("latest_bar_age_minutes", None if age is None else round(age, 3))
        health.setdefault("stale_limit_minutes", stale_limit)
        health.setdefault("from_cache", False)
        health.setdefault("synthetic", health.get("source") == "demo")
        health.setdefault("error", "")
        health.setdefault(
            "stale", bool(age is None or age > stale_limit) if bars is not None else True
        )
        health.setdefault("status", "stale" if health["stale"] else "ok")
        if error:
            health.update({"status": "error", "error": str(error), "stale": True})
        return health

    def _store_market_health(self, health: dict[str, Any]) -> None:
        """逐轮写延迟证据；异常写关键事件和告警，但同一种异常最多 10 分钟一次。"""
        symbol = str(health.get("symbol") or "")
        period = str(health.get("period") or "")
        key = f"{symbol.strip().lower()}|{period}"
        rows = getattr(self, "_market_health", None)
        if rows is None:
            rows = {}
            self._market_health = rows
        rows[key] = dict(health)
        try:
            path = getattr(self, "market_health_log", None)
            if path is None:
                path = self.root / "data" / "market_health.jsonl"
                self.market_health_log = path
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= EVENT_LOG_MAX_BYTES:
                backup = path.with_name(path.name + ".1")
                backup.unlink(missing_ok=True)
                path.replace(backup)
            row = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "run_id": self.run_id,
                **health,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        except OSError as exc:
            logger.warning("写行情健康日志失败（不影响平仓保护）: {}", exc)

        issue = str(health.get("issue") or "")
        status = str(health.get("status") or "")
        if not issue and status == "ok" and not health.get("synthetic"):
            return
        event_key = f"market_health|{key}|{issue or status}"
        age = health.get("signal_bar_age_minutes", health.get("latest_bar_age_minutes"))
        age_text = "未知" if age is None else f"{float(age):.1f}分钟"
        message = (
            f"{symbol} {period} 行情异常：{issue or status}；来源 {health.get('source') or '未知'}；"
            f"K线 {health.get('signal_bar_time') or health.get('latest_bar_time') or '未知'}；"
            f"年龄 {age_text}；下载 {health.get('download_ms') or 0}ms"
        )
        if health.get("error"):
            message += f"；{health['error']}"
        before = dict(getattr(self, "_throttled_event_at", {}) or {})
        self._record_event_throttled(
            "market_health_warning", event_key, market_health=dict(health), message=message
        )
        after = getattr(self, "_throttled_event_at", {})
        if after.get(event_key) == before.get(event_key):
            return
        try:
            from src.ops.alerts import emit_alert

            emit_alert("market_health", message, level="warning", root=self.root)
        except Exception as exc:  # noqa: BLE001
            logger.debug("行情告警写入失败: {}", exc)

    def _ctp_card_fields(self) -> dict[str, Any]:
        """
        顶部「执行通道」卡片要用的柜台状态。

        交易台和常驻看同一套：session 已就绪，并且本轮 logged_in 为真，才算连上。
        午休也可以已登录；卡片不能因为本轮没扫描就写成未连接。
        """
        # 不是柜台通道时卡片不走 CTP 文案，这里仍给空字段，避免界面缺键。
        if not self.trader_capabilities.orders_via_broker:
            return {
                "ctp_ready": False,
                "ctp_user": "",
                "ctp_kind": "",
                "ctp_error": "",
            }
        gateway = getattr(self.trader, "gateway", None)
        session = getattr(gateway, "session", None)
        settings = getattr(gateway, "settings", None) or getattr(self.trader, "settings", None)
        ready = bool(getattr(session, "ready", False)) and bool(self.logged_in)
        return {
            "ctp_ready": ready,
            "ctp_user": str(getattr(settings, "user_id", "") or ""),
            "ctp_kind": str(getattr(settings, "account_kind", "") or ""),
            "ctp_error": str(getattr(session, "last_error", "") or ""),
        }

    def status_snapshot(self) -> dict[str, Any]:
        """给界面看的健康快照。"""
        return {
            "running": self._running,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "orders_paused": bool(self._orders_paused),
            "dry_run": bool(self.cfg.get("dry_run", True)),
            "logged_in": bool(self.logged_in),
            "ctp_flow": self._ctp_flow_text(),
            **self._ctp_card_fields(),
            "heartbeat_at": self.heartbeat_at,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
            "daily_pnl": self.risk.daily_pnl,
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "halt_kind": str(getattr(self.risk, "halt_kind", "") or ""),
            "positions": snapshot_positions(self.strategy),
            "symbols": self.symbols,
            "scan_symbols": list(getattr(self, "scan_symbols", None) or []),
            "last_alert": _safe_last_alert(self.risk.max_daily_loss),
            "health": getattr(self, "_health_report", None),
            "market_health": {
                key: dict(value)
                for key, value in (getattr(self, "_market_health", {}) or {}).items()
            },
            "dynamic": self._dynamic_status_text(),
            "reconcile": dict(self._reconcile_state or {}),
            "frozen_symbols": sorted(self._frozen_symbols),
            "protective": str(getattr(self, "_protective_state", "none")),
            "unprotected_resume": bool(getattr(self, "_unprotected_resume", False)),
        }

    def _journal_signal(self, decision: StrategyDecision, bar_time: str) -> str:
        """非 HOLD 决策落 signals + 审计，并让后续报单/成交复用同一个 trace_id。"""
        trace_id = str((decision.meta or {}).get("trace_id") or "") or new_trace_id()
        set_trace_id(trace_id)
        decision.meta = {**(decision.meta or {}), "trace_id": trace_id}
        strategy_id = str(
            getattr(self.strategy, "name", "") or type(self.strategy).__name__
        )
        account_id = str(getattr(self, "account_id", "") or "")
        price = float(decision.entry_price or 0)
        try:
            score = float((decision.meta or {}).get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        journal.record_signal(
            trace_id=trace_id,
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=decision.symbol,
            bar_time=str(bar_time or ""),
            action=decision.action.value,
            price=price,
            score=score,
            reason=str(decision.reason or ""),
            payload={"stop_price": decision.stop_price},
            root=self.cfg.get("_project_root"),
        )
        journal.record_audit(
            "signal_generated",
            trace_id=trace_id,
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=decision.symbol,
            message=str(decision.reason or ""),
            payload={"bar_time": str(bar_time or ""), "price": price},
            root=self.cfg.get("_project_root"),
        )
        # P2：类型化信号事件。账本/审计已经同步写完，这里只通知旁路订阅者，
        # 订阅者出问题不能反过来影响交易决定。
        try:
            self.events.publish(
                SIGNAL,
                {
                    "trace_id": trace_id,
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "symbol": decision.symbol,
                    "bar_time": str(bar_time or ""),
                    "action": decision.action.value,
                    "price": price,
                    "score": score,
                    "reason": str(decision.reason or ""),
                    "stop_price": decision.stop_price,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 旁路订阅失败不能改交易结果
            logger.warning("SIGNAL 事件订阅者异常，已忽略: {}", exc)
        return trace_id

    def scan_symbol(self, symbol: str, force: bool = False) -> None:
        """对单个品种：拉信号周期 + 止损周期K线 → 策略评估 → 执行。"""
        key = symbol.strip().lower()
        if key in getattr(self, "_frozen_symbols", set()):
            if key not in self._frozen_noted:
                self._frozen_noted.add(key)
                logger.warning("{} 有未认领柜台持仓，已冻结自动交易，等待人工处理", symbol)
                self._record_event("symbol_frozen", symbol=symbol)
            return

        # 多品种：每个品种按自己的时段跑。燃油有夜盘、生猪没有，不能拿选中品种的时段套。
        if (
            not force
            and self._multi_symbol_enabled()
            and symbol.strip().lower() != self._primary_symbol().strip().lower()
            and not in_trading_session(self._symbol_sessions_for(symbol))
            and not self._symbol_flatten_due(symbol)
        ):
            self._record_event_throttled(
                "multi_symbol_outside_session",
                f"multi_symbol_outside_session|{key}",
                symbol=symbol,
            )
            return

        scan_started = time.perf_counter()
        previous_scan = getattr(self, "_market_scan_at", {}).get(key)
        scan_gap_sec = None if previous_scan is None else scan_started - previous_scan
        self._market_scan_at[key] = scan_started
        leg = self._strategy_for(symbol)
        signal_tf = getattr(leg, "timeframe", "15")
        trail_tf = getattr(leg, "trail_timeframe", signal_tf)

        try:
            bars = self.kline.fetch(symbol, period=str(signal_tf))
        except Exception as exc:
            health = self._capture_market_health(
                symbol, str(signal_tf), error=str(exc)
            )
            health["issue"] = "download_error"
            self._store_market_health(health)
            raise
        health = self._capture_market_health(symbol, str(signal_tf), bars)
        # 只去掉「墙上时钟还没到结束时刻」的最后一根。
        # 午休后第一根已收盘若被盲删，会一直以为最新还是上午那根，止损会晚好几分钟。
        closed = drop_forming_bar(bars, period=str(signal_tf))
        if closed is None or len(closed) == 0:
            closed = bars
        forming = None
        if bars is not None and closed is not None and len(bars) > len(closed):
            forming = bars.iloc[len(closed) :]

        trail_closed = None
        if str(trail_tf) != str(signal_tf):
            trail_bars = self.kline.fetch(symbol, period=str(trail_tf))
            trail_closed = drop_forming_bar(trail_bars, period=str(trail_tf))

        # 有的策略会另外要日线。拉不到就不开新仓，不用分钟线自己拼日线，也不改用别的策略。
        trend_closed = None
        trend_tf = getattr(leg, "trend_timeframe", None)
        if getattr(leg, "uses_trend_bars", False) and trend_tf and str(trend_tf) != str(signal_tf):
            try:
                trend_bars = self.kline.fetch(symbol, period=str(trend_tf))
                # 最后一根日线可能还在走，先拿掉。策略还会再核对一次是不是已经走完。
                trend_closed = drop_forming_bar(trend_bars, period=str(trend_tf))
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
        # 策略评估和通用止损都只看已收线 K 线：未收线的盘中影线不触发
        # trigger/touch，和回测「只用已收盘 K 线、下一根成交」保持同一口径。
        # forming 仍保留给健康状态和启动回溯判断，不参与本轮决策。
        if try_catch_up:
            hit = find_catch_up_open(
                cfg=self.cfg,
                symbol=symbol,
                closed=closed,
                trail_closed=trail_closed,
                trend_closed=trend_closed,
                forming=forming,
                settings=catch_cfg,
                strategy_cfg=self._strategy_cfg_for(symbol),
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

        evaluation_started_at = datetime.now().astimezone()
        evaluation_started = time.perf_counter()
        try:
            decision = self._evaluate_symbol(
                symbol,
                eval_bars,
                trail_bars=eval_trail,
                trend_bars=eval_trend,
                now=_eval_now(eval_bars, closed, catch_up=catch_up_used),
            )
        except Exception as exc:
            health.update(
                {
                    "issue": "compute_error",
                    "compute_error": str(exc),
                    "evaluation_started_at": evaluation_started_at.isoformat(timespec="milliseconds"),
                    "evaluation_ms": round((time.perf_counter() - evaluation_started) * 1000.0, 3),
                    "scan_gap_sec": scan_gap_sec,
                }
            )
            self._store_market_health(health)
            raise
        evaluation_ms = (time.perf_counter() - evaluation_started) * 1000.0

        # 柜台对账补进来的仓：不让策略加仓、反手，只保留通用止损。
        pos_now = self._position_for(symbol)
        if (
            str(getattr(pos_now, "origin", "") or "") == "broker"
            and decision.action != StrategyAction.HOLD
        ):
            self._record_event(
                "broker_origin_block_strategy",
                symbol=symbol,
                action=decision.action.value,
                reason=decision.reason,
            )
            logger.warning(
                "{} 持仓来自柜台对账，暂停策略动作（{}），只保留通用止损",
                symbol,
                decision.action.value,
            )
            decision = StrategyDecision(
                StrategyAction.HOLD, symbol, "柜台对账持仓，仅允许通用止损"
            )

        # 通用止损：和策略自身止损相互独立，策略没让平仓时由公共止损补一刀。
        # 策略要反手/开仓时不抢，它本身也会先平掉旧仓；谁先触发都算平仓。
        if decision.action == StrategyAction.HOLD and not self._symbol_is_flat(symbol):
            gs_bars = trail_closed if trail_closed is not None else closed
            decision, gs_hit = apply_generic_stop(
                self.generic_stop,
                symbol,
                decision,
                self._position_for(symbol),
                gs_bars,
                flat=False,
                forming=None,
                mode=getattr(leg, "stop_mode", "touch"),
                # 平仓未成交时别每轮都重复发单，和策略自身止损的等待口径一致。
                cooldown_sec=120.0,
            )
            if gs_hit is not None:
                self._record_event(
                    "generic_stop",
                    symbol=symbol,
                    kind=gs_hit.kind,
                    stop=gs_hit.stop,
                )

        # 交割月不再交易这一手：日盘收完、下一交易日已进交割月时平仓，也不开新仓。
        gate_outcome = apply_delivery_gate(
            symbol,
            _eval_now(eval_bars, closed, catch_up=catch_up_used),
            decision,
            flat=self._symbol_is_flat(symbol),
        )
        if gate_outcome.flattened:
            self._record_event(
                "delivery_flatten", symbol=symbol, reason=gate_outcome.gate.reason
            )
        elif gate_outcome.rejected is not None:
            self._reject_gate(decision, "delivery_month", gate_outcome.gate.reason, "")
        decision = gate_outcome.decision

        # 30/60 分钟尾盘不足整周期的半根：保留给指标，但不拿它开新仓；平仓、止损照常。
        # 启动回溯补开仓（catch_up_used）是补历史信号，不加这道闸门。
        decision, partial_blocked = apply_partial_tail_gate(
            decision,
            closed,
            str(signal_tf),
            symbol=symbol,
            allow=catch_up_used,
        )
        if partial_blocked:
            partial_bar_time = str(closed.index[-1]) if closed is not None and len(closed) else ""
            logger.info("{} 尾盘半根不开新仓 | {}", symbol, decision.reason)
            self._record_event(
                "partial_bar_block_open",
                symbol=symbol,
                reason=decision.reason,
                bar_id=partial_bar_time,
            )
            journal.record_audit(
                "signal_blocked",
                trace_id=str((decision.meta or {}).get("trace_id") or ""),
                symbol=symbol,
                message=str(decision.reason or ""),
                payload={"gate": "partial_bar", "bar_time": partial_bar_time},
                root=self.cfg.get("_project_root"),
            )

        # 给人看：最新K线长什么样 + 本轮是否符合开仓（不影响下单逻辑）
        try:
            from src.strategy.explain import log_scan_brief

            log_scan_brief(
                symbol,
                closed,
                decision,
                leg,
                signal_tf=str(signal_tf),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("扫描解说失败（可忽略）: {}", e)

        # 过期缓存：禁止新开仓，仍允许平仓（防断网用旧K线乱开）
        # 日线、周线不能沿用「90 分钟就算过期」，否则昨天的日线永远开不了仓。
        stale_limit = stale_after_minutes(str(signal_tf), self.max_stale_minutes)
        stale = is_bars_stale(closed, stale_limit, symbol=symbol, period=str(signal_tf))
        gap = is_bar_gap(
            closed,
            str(signal_tf),
            symbol=symbol,
            max_stale_minutes=self.max_stale_minutes,
        )
        stale = stale or gap
        signal_age = bars_age_minutes(closed, symbol=symbol, period=str(signal_tf))
        signal_bar_time = str(closed.index[-1]) if closed is not None and len(closed) else ""
        download_ms = float(health.get("download_ms") or 0.0)
        evaluation_ms = (time.perf_counter() - evaluation_started) * 1000.0
        issues: list[str] = []
        if health.get("error"):
            issues.append("download_fallback" if health.get("from_cache") else "download_error")
        if health.get("fallback_from"):
            issues.append("fallback_source")
        if health.get("synthetic"):
            issues.append("synthetic")
        if stale:
            if forming is not None and len(forming) > 0:
                issues.append("bar_forming")
            if gap:
                issues.append("bar_gap")
            issues.append("cache_stale" if health.get("from_cache") else "source_stale")
        slow_ms = max(5.0, float(getattr(self, "scan_interval", 30))) * 1000.0
        if download_ms > slow_ms:
            issues.append("download_slow")
        if evaluation_ms > slow_ms:
            issues.append("compute_slow")
        if stale:
            market_status = "stale"
        elif health.get("synthetic"):
            market_status = "synthetic"
        elif health.get("fallback_from"):
            market_status = "fallback"
        elif issues:
            market_status = "slow" if any(x.endswith("_slow") for x in issues) else "cache"
        else:
            market_status = "ok"
        health.update(
            {
                "status": market_status,
                "stale": stale,
                "signal_bar_time": signal_bar_time,
                "signal_bar_age_minutes": None if signal_age is None else round(signal_age, 3),
                "forming_bar": bool(forming is not None and len(forming) > 0),
                "evaluation_started_at": evaluation_started_at.isoformat(timespec="milliseconds"),
                "evaluated_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "evaluation_ms": round(evaluation_ms, 3),
                "scan_gap_sec": None if scan_gap_sec is None else round(scan_gap_sec, 3),
                "issue": "+".join(issues),
                "issues": issues,
                "decision_action": decision.action.value,
                "bar_id": str((decision.meta or {}).get("bar_id", "") or signal_bar_time),
            }
        )
        self._last_signal_bars[symbol] = closed
        if decision.action == StrategyAction.HOLD:
            cut = self._maybe_dynamic_cut(symbol)
            if cut is not None:
                decision = cut
        health["decision_action"] = decision.action.value
        health["bar_id"] = str((decision.meta or {}).get("bar_id", "") or signal_bar_time)
        decision.meta = {**(decision.meta or {}), "market_health": dict(health)}
        self._store_market_health(health)

        if decision.action != StrategyAction.HOLD:
            self._journal_signal(
                decision,
                str((decision.meta or {}).get("bar_id") or signal_bar_time),
            )

        if stale and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            logger.error(
                "{} 的{}K线过期，拒绝开仓 | {}",
                symbol,
                period_label(str(signal_tf)),
                decision.reason,
            )
            self._record_event(
                "stale_block_open",
                symbol=symbol,
                reason=decision.reason,
                bar_id=health.get("bar_id", ""),
                signal_bar_age_minutes=health.get("signal_bar_age_minutes"),
                market_health=dict(health),
            )
            journal.record_audit(
                "signal_blocked",
                trace_id=str((decision.meta or {}).get("trace_id") or ""),
                symbol=symbol,
                message=str(decision.reason or ""),
                payload={"gate": "stale", "bar_time": signal_bar_time},
                root=self.cfg.get("_project_root"),
            )
            return

        # 回退行情：是不是允许开新仓由配置决定。不允许时仍保留持仓、止损和平仓信号。
        if (
            health.get("fallback_from")
            and not self.fallback_allow_entry
            and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)
        ):
            logger.error(
                "{} 行情已回退到备用源 {}（天勤失败：{}），按配置拒绝开仓 | {}",
                symbol,
                health.get("source"),
                health.get("fallback_reason"),
                decision.reason,
            )
            self._record_event(
                "fallback_block_open",
                symbol=symbol,
                fallback_from=health.get("fallback_from"),
                fallback_reason=health.get("fallback_reason"),
                reason=decision.reason,
                bar_id=health.get("bar_id", ""),
                market_health=dict(health),
            )
            journal.record_audit(
                "signal_blocked",
                trace_id=str((decision.meta or {}).get("trace_id") or ""),
                symbol=symbol,
                message=str(decision.reason or ""),
                payload={"gate": "fallback", "bar_time": signal_bar_time},
                root=self.cfg.get("_project_root"),
            )
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
            latest_price_source = "forming_bar_close"
            if live_price <= 0:
                live_price = self.kline.latest_price(symbol)
                latest_price_source = "latest_price_api"
            if live_price <= 0:
                live_price = _last_close(closed)
                latest_price_source = "signal_bar_close"
            decision.meta = {
                **(decision.meta or {}),
                "latest_price": live_price,
                "latest_price_at": now_iso(),
                "latest_price_source": latest_price_source if live_price > 0 else "unavailable",
            }
            atr = _latest_atr(closed, leg)
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
                journal.record_audit(
                    "signal_blocked",
                    trace_id=str((decision.meta or {}).get("trace_id") or ""),
                    symbol=symbol,
                    message=str(decision.reason or ""),
                    payload={"gate": "slippage", "bar_time": signal_bar_time, "chase": chase},
                    root=self.cfg.get("_project_root"),
                )
                return

        sent = False
        try:
            sent = bool(self._execute(decision))
        except Exception as exc:
            dispatch_started = bool(getattr(self, "_order_dispatch_started", False))
            # 还没进到点单就出异常（比如下单前拼参数写错），把信号标记放开，下一轮还能重试；
            # 已经点出去了就不放开，避免重复下单。
            if (
                decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN)
                and not dispatch_started
            ):
                self._release_signal_mark(decision)
            self._dyn_cut_pending = ""
            # 发单阶段异常且可能已报单：委托结果未知，停开新仓 + 告警，等人工确认。
            # 保护性平仓（close）不受影响，沿用现有 set_orders_paused / set_halt 语义。
            if dispatch_started:
                self._halt_on_unknown_dispatch(exc)
            raise
        pending_cut = str(getattr(self, "_dyn_cut_pending", "") or "")
        if pending_cut:
            if sent:
                self._dyn_cut_key = pending_cut
            self._dyn_cut_pending = ""

    def _strategy_for(self, symbol: str) -> Any:
        """这个品种该用哪条策略腿。单策略时就是原来的全局策略。"""
        picker = getattr(self.strategy, "strategy_for", None)
        if callable(picker):
            return picker(symbol)
        return self.strategy

    def _strategy_cfg_for(self, symbol: str) -> dict[str, Any] | None:
        """这个品种的腿配置；没有腿路由时返回 None，回溯仍用全局配置。"""
        getter = getattr(self.strategy, "cfg_for", None)
        return getter(symbol) if callable(getter) else None

    def _evaluate_accepts(self, name: str, strategy: Any = None) -> bool:
        """策略的 evaluate 是否真的接受这个关键字参数。

        以前用 `except TypeError: pass` 回调一次，会把策略内部的 TypeError
        （真正的 bug）也吞掉，还会让同一根K线被评估两次。这里只看签名。
        """
        target = strategy or self.strategy
        cache_key = f"_evaluate_accepts_{id(target)}_{name}"
        cached = getattr(self, cache_key, None)
        if cached is None:
            import inspect

            try:
                params: Any = inspect.signature(target.evaluate).parameters
            except (TypeError, ValueError):
                params = {}
            cached = name in params or any(
                param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
            )
            setattr(self, cache_key, cached)
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
        target = self._strategy_for(symbol)
        uses_trend = bool(getattr(target, "uses_trend_bars", False))
        kwargs: dict[str, Any] = {"trail_bars": trail_bars}
        if uses_trend:
            kwargs["trend_bars"] = trend_bars
        if now is not None and self._evaluate_accepts("now", target):
            kwargs["now"] = now
        return target.evaluate(symbol, bars, **kwargs)

    def _halt_on_unknown_dispatch(self, exc: Exception) -> None:
        """发单阶段异常且委托结果未知：停开新仓 + 持久化 + 告警，等人工确认。

        只停新开仓，保护性平仓继续；人工在持仓核对页确认后由 confirm_positions_reviewed 解除。
        """
        message = f"发单阶段异常，委托结果未知，已停止开新仓，请人工核对：{exc}"
        try:
            self.set_orders_paused(True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.risk.set_halt("order_dispatch_unknown", message)
            save = getattr(self.risk, "save_state", None)
            if callable(save):
                save()
        except Exception:  # noqa: BLE001
            pass
        try:
            from src.ops.alerts import emit_alert

            emit_alert("order_dispatch_unknown", message, level="error")
        except Exception:  # noqa: BLE001
            pass
        logger.error(message)

    def scan_once(self, force: bool = False) -> None:
        """
        执行一轮扫描（全部关注品种）。

        参数:
            force: True 时忽略交易时段限制（给 --once 调试用）
        """
        self._refresh_scan_symbols()
        self._rollover_trading_day()
        self._refresh_sessions()

        # 盘前定时审计：每轮扫描开头检查一次，落在 08:55 / 13:25 / 20:25 前后 3 分钟内且距上次
        # 触发已超过 1 小时就跑一次，避免同一时段重复跑。
        self._maybe_run_session_audit()
        if not force and not self._scan_session_active():
            now = datetime.now()
            last = self._session_skip_log_at
            if last is None or (now - last).total_seconds() >= 60:
                if self._multi_symbol_enabled():
                    logger.info(
                        "扫描清单 {} 都不在各自交易时段，本轮不拉K线、不扫描",
                        ",".join(self.scan_symbols),
                    )
                else:
                    night = next((row for row in self.sessions if str(row[0]).startswith("21")), None)
                    night_txt = f"{night[0]}～{night[1]}" if night else "未写夜盘"
                    logger.info(
                        "现在不在交易时段，本轮不拉K线、不扫描（夜盘按 {}）",
                        night_txt,
                    )
                self._session_skip_log_at = now
            return

        if not self.logged_in:
            if self.trader_capabilities.orders_via_broker:
                self._gui_price_ready = True
                # 连不上就停手，按间隔在下一轮重试；不能拿「已登录」硬着头皮开仓。
                if not self._arm_ctp():
                    return
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
                self.scan_symbol(symbol, force=force)
            except RuntimeError as exc:
                # 行情没拿到时只写一句原因。整段英文报错会占满运行日志，看起来像程序崩了。
                self.last_error = str(exc)
                logger.warning("扫描 {} 这一轮没有做成: {}", symbol, exc)
                self._record_event_throttled(
                    "scan_error",
                    f"scan_error|{symbol.strip().lower()}|runtime",
                    symbol=symbol,
                    error=str(exc),
                )
            except Exception as e:  # noqa: BLE001
                # 含行情拉取失败：跳过本品种，不中断其它品种
                self.last_error = str(e)
                logger.exception("扫描 {} 失败: {}", symbol, e)
                self._record_event_throttled(
                    "scan_error",
                    f"scan_error|{symbol.strip().lower()}|exception",
                    symbol=symbol,
                    error=str(e),
                )
            if not self._running:
                return
        if getattr(self, "_unprotected_resume", False):
            queried = (self._reconcile_state or {}).get("queried", True)
            if queried is False:
                logger.warning("退出保护恢复未完成：柜台对账未成功，继续保持暂停报单")
            else:
                self._unprotected_resume = False
                self.set_orders_paused(False)
                self._clear_unprotected_flag()
                self._record_event("unprotected_recovered")
                logger.warning("启动保护恢复：已完成对账与止损评估，恢复报单")
        self._protective_state = self._protective_status()
        self._maybe_check_health()
        self._persist_state()

    def setup_schedule(self) -> None:
        """注册每日登录时间点。柜台通道不点交易软件，所以不注册这些钟点。"""
        if self.trader_capabilities.orders_via_broker:
            logger.info("执行通道直接连柜台，跳过 GUI 定时登录")
            return
        times = self.cfg.get("schedule", {}).get("login_times", [])
        for t in times:
            schedule.every().day.at(t).do(self.do_login)
            logger.info("已注册每日登录时间: {}", t)

    def stop(self) -> None:
        """请求停止主循环（供图形界面使用）。"""
        if self._stop_requested_at is None:
            self._stop_requested_at = time.monotonic()
        self._running = False
        logger.info("已请求停止机器人主循环")

    def post_command(self, name: str, **data: Any) -> bool:
        """界面/脚本下发命令；常驻主循环按 FIFO 处理。返回 False 表示队列已满。"""
        return self.commands.post(name, data)

    def _handle_command(self, command: Command) -> None:
        """执行一条命令。命令在调度线程里同步执行，不改变风控与报单顺序。"""
        if command.name == STOP:
            self.stop()
            return
        if command.name == FLATTEN:
            self._flatten_pending = True
            self.flatten_if_due(force=True)
            return
        if command.name == SCAN:
            # 主循环拿到 SCAN 会把下一拍提前到立即执行，这里不重复扫描。
            logger.info("收到立即扫描命令")
            return
        logger.warning("未知命令 {}，已忽略", command.name)

    def wait_stopped(self, timeout: float = 15.0) -> bool:
        """等待主循环真正结束并完成落盘。"""
        return self._stopped.wait(timeout)

    @property
    def is_running(self) -> bool:
        """是否仍在主循环中。"""
        return self._running

    def run_forever(self) -> None:
        """阻塞运行：定时登录 + 周期扫描。Ctrl+C 或 stop() 退出。"""
        caps = self.trader_capabilities
        # GUI 通道只交易「当前选中」这一个品种，关注列表留多少都不影响启动。
        # 扫描清单可能多于 1 个（选中品种 + 已有持仓），那是为了给旧仓止损/清仓，不参与开仓。

        # 告警外发（默认关）。配置写错只降级成本地告警，不影响扫描。
        from src.ops.notify import configure as configure_notify

        configure_notify(self.cfg)
        self._check_guardian_actions()
        # 实盘先看休市日历够不够用：缺年份就禁止开仓，但不影响止损与清仓。
        self._check_holiday_calendar()
        self.setup_schedule()
        self._stopped.clear()
        # P2 发件箱：先把上次没入账的成交补进账本，再启动异步消费线程。
        # 失败只记日志，不拦住交易；下次启动会继续补。
        try:
            from src.account.outbox import enqueue_order_stage, recover_pending, start_dispatcher
            from src.account.signal_book import apply_outbox_event
            from src.trader.order_timeline import set_record_listener

            def _record_order(row: dict[str, Any]) -> None:
                enqueue_order_stage(self.root, row)

            set_record_listener(_record_order)
            recover_pending(self.root, lambda event: apply_outbox_event(self.root, event))
            start_dispatcher(self.root, lambda event: apply_outbox_event(self.root, event))
        except Exception as exc:  # noqa: BLE001
            logger.exception("发件箱启动失败，继续直接记账: {}", exc)
        self._running = True
        # 第一轮扫描才允许回溯补开仓；上次退出留下持仓时先不补，等对账恢复后再按需开。
        self._catch_up_pending = not bool(getattr(self, "_unprotected_resume", False))
        logger.info(
            "机器人启动 | dry_run={} | 扫描={} | 关注={} | 周期={} | 扫描间隔={}s | 模式={}",
            self.cfg.get("dry_run"),
            self.scan_symbols,
            self.symbols,
            period_label(str(self.strategy.timeframe)),
            self.scan_interval,
            getattr(self.strategy, "__class__", type(self.strategy)).__name__,
        )
        logger.info(
            "执行通道: {} | 查持仓={} 查委托={} 确认成交={} 窗口点击={}",
            type(self.trader).__name__,
            caps.can_query_positions,
            caps.can_query_orders,
            caps.confirms_fill,
            caps.window_driven,
        )

        # 启动即跑一次盘前行情适配审计：打印所有关注品种 × 各策略的行情适配状态。
        # 独立线程跑，不阻塞启动（K 线拉取有网络延迟）。
        self._run_session_audit("启动")

        # 挂机防休眠：否则电脑一睡，扫描与 GUI 点击都会停
        from src.utils.keepalive import KeepAwake

        keep_display = bool((self.cfg.get("runtime") or {}).get("keep_display_awake", True))
        keepalive = KeepAwake(keep_display=keep_display)
        keepalive.start()

        # 心跳看门狗：界面关掉、扫描卡住时也能自己留证据并暂停报单。
        self._start_watchdog()

        from src.login.prepare import startup_should_login

        # 准备按钮刚刚登录过，这里再登录会把交易菜单再点开一次。
        if caps.orders_via_broker:
            # 柜台通道自己登录 CTP，不再点交易软件。连不上时 logged_in 保持 False，下一轮重试。
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
            in_window = self._any_flatten_due()
        except Exception:  # noqa: BLE001
            in_window = False
        flatten_at = datetime.now() if in_window else self._next_flatten_at()

        try:
            # P2：第一拍立即扫描；之后由命令队列的超时等待驱动，不再每秒 sleep。
            next_scan_at = time.monotonic()
            while self._running:
                try:
                    schedule.run_pending()
                    wait = max(0.0, min(1.0, next_scan_at - time.monotonic()))
                    command = self.commands.get(timeout=wait)
                    flatten_from_command = False
                    if command is not None:
                        self._handle_command(command)
                        if command.name == SCAN:
                            next_scan_at = 0.0
                        elif command.name == FLATTEN:
                            flatten_from_command = True
                    if not flatten_from_command and flatten_at is not None and datetime.now() >= flatten_at:
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
                    if self._running and time.monotonic() >= next_scan_at:
                        self.scan_once()
                        # 可选：当日最后收盘后预约关机（有夜盘不会在下午就关）
                        self._maybe_shutdown_after_close()
                        next_scan_at = time.monotonic() + max(self.scan_interval, 1)
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
            try:
                keepalive.stop()
            except Exception:
                logger.exception("停止防休眠失败")
            # 先停看门狗、再写退出标记，最后才关交易通道。
            try:
                self._stop_watchdog()
            except Exception:  # noqa: BLE001
                logger.exception("停止看门狗失败")
            try:
                self._write_unprotected_flag("clean_exit")
            except Exception:  # noqa: BLE001
                logger.exception("写退出保护标记失败")
            try:
                from src.account.outbox import stop_dispatcher
                from src.trader.order_timeline import set_record_listener

                stop_dispatcher(self.root)
                set_record_listener(None)
            except Exception:  # noqa: BLE001
                logger.exception("停止发件箱消费线程失败")
            try:
                if hasattr(self.trader, "shutdown"):
                    self.trader.shutdown()
            except Exception:
                logger.exception("关闭交易通道失败")
            try:
                self._persist_state()
            finally:
                self._stopped.set()
                logger.info("机器人已停止")
                self._record_event("shutdown")


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
        sessions_for_check = getattr(self, "sessions", None)
        if self._multi_symbol_enabled():
            # 多品种关机要等最后一个品种收盘：清仓钟点取并集，时段也取并集。
            flat_s = []
            seen_flat: set[str] = set()
            merged: list[list[str]] = []
            for name in self.scan_symbols:
                for text in self._symbol_flatten_clocks(name):
                    key = str(text)[:5]
                    if key and key not in seen_flat:
                        seen_flat.add(key)
                        flat_s.append(key)
                merged.extend(self._symbol_sessions_for(name) or [])
            sessions_for_check = merged
        ok, why = ready_for_shutdown(
            sessions=sessions_for_check,
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
