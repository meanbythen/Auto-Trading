"""
主调度与交易循环
功能：到点登录、拉取K线、跑 Pinbar+SAR 策略、风控后下单/平仓。
原理：schedule 负责定时登录；主循环按间隔扫描各品种；策略只决策，Trader 才执行。
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime
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
from src.trader import create_trader
from src.trader.base import OrderRequest
from src.utils.state_store import (
    default_state_path,
    format_position_text,
    load_state,
    restore_positions,
    save_state,
    snapshot_positions,
)
from src.utils.trading_day import futures_trading_day


def _parse_hhmm(value: str) -> dtime:
    """把 '09:30' 转成 time 对象。"""
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def in_trading_session(sessions: list[list[str]], now: datetime | None = None) -> bool:
    """判断当前是否在配置的交易时段内。"""
    now = now or datetime.now()
    current = now.time()
    for start_s, end_s in sessions:
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        if start <= current <= end:
            return True
    return False


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


def _eval_now(eval_bars: Any, closed: Any, *, catch_up: bool) -> datetime | None:
    """
    这一次评估该把什么当成「现在」。

    只看已收盘的K线时不用传，策略自己拿最后一根的时间。
    持仓时会把还在走的那根也拼进去看影线，但那根的时间戳是「结束时刻」，比墙上时钟晚最多一个周期，
    拿它当现在会让 22:55 的清仓在 22:50 那一段里就触发，所以这时给真实时间。
    回溯补开仓是另一回事：那时「现在」就是补开那根的时间。
    """
    if eval_bars is closed:
        return None
    if catch_up:
        return _bar_now_from_prefix(eval_bars)
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

        trade = cfg.get("trade", {})
        self.default_symbol = str(trade.get("symbol", "bu2505"))
        self.volume = int(trade.get("volume", 1))  # 固定手数模式 / 缺止损价时的后备
        self.symbols = list(self.strategy.symbols)
        self.scan_interval = int(cfg.get("schedule", {}).get("scan_interval_sec", 30))
        self.sessions = cfg.get("schedule", {}).get("trading_sessions", [])
        self.logged_in = False
        self._running = False  # GUI / 外部可调用 stop() 结束循环
        # 同花顺真下单前，必须先把价格改成对手价。没改成就不允许点买卖。
        executor_name = str((cfg.get("trade") or {}).get("executor", "sim"))
        self._gui_price_ready = not (executor_name == "gui" and not bool(cfg.get("dry_run")))
        self.heartbeat_at: str = ""
        self.last_scan_at: str = ""
        self.last_error: str = ""
        # GUI：点击成功后是否立刻当作成交记账（默认 True 可挂机；False=只记「已发单」不改持仓，更安全）
        gui_cfg = (trade.get("gui") or {}) if isinstance(trade, dict) else {}
        self.gui_assume_fill = bool(gui_cfg.get("assume_fill", True))
        # 收盘后关机：每天最多预约一次
        self._shutdown_armed_date: str = ""
        self.max_stale_minutes = float((cfg.get("data") or {}).get("max_stale_minutes", 90))
        # 启动常驻后的第一轮扫描才做回溯补开仓；做完就关掉，避免每轮重复补单。
        self._catch_up_pending = False

        root = Path(cfg.get("_project_root", "."))
        self.root = root
        self.event_log = root / "data" / "events.jsonl"
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = default_state_path(root)
        # 先记下当前交易日（夜盘 21:00 起算下一个）。读到磁盘后如果文件里已有交易日，再用文件里的。
        self.trading_day = str(futures_trading_day())
        self._load_persisted_state()
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
        if len(self.symbols) > 1:
            logger.error(
                "GUI 模式关注了多个品种 {}：不会自动切合约，极易点错。"
                "请只保留一个品种（如 bu2612）。",
                self.symbols,
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

    def _record_event(self, event: str, **kwargs: Any) -> None:
        """把关键事件追加写入 data/events.jsonl。"""
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kwargs}
        with self.event_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

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
        if balance is not None:
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

    def status_snapshot(self) -> dict[str, Any]:
        """给界面看的健康快照。"""
        return {
            "running": self._running,
            "heartbeat_at": self.heartbeat_at,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
            "daily_pnl": self.risk.daily_pnl,
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "positions": snapshot_positions(self.strategy),
            "symbols": self.symbols,
        }

    def emergency_flatten(self, reason: str = "人工紧急清仓") -> list[str]:
        """
        紧急平掉策略内所有持仓（主流程序的 Kill Switch）。
        返回每条执行说明。
        """
        logs: list[str] = []
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        # 沥青、玉米要按柜台的今仓、昨仓拆平仓。先查一次，避免紧急平仓时还不知道怎么拆。
        if executor == "ctp" and hasattr(self.trader, "reconcile"):
            try:
                self.trader.reconcile(self._internal_nets())
            except Exception as exc:  # noqa: BLE001
                logger.warning("紧急清仓前查询柜台失败: {}", exc)
        for sym, pos in list((getattr(self.strategy, "positions", {}) or {}).items()):
            if getattr(pos, "side", "flat") == "flat" or int(getattr(pos, "volume", 0) or 0) <= 0:
                continue
            vol = abs(int(pos.volume))
            decision = StrategyDecision(StrategyAction.CLOSE, str(sym), reason, entry_price=None)
            # 走统一执行，但 force 平仓
            side = "close"
            allowed, msg = self.risk.allow(side, vol, symbol=str(sym), force=True)
            if not allowed:
                logs.append(f"{sym}: 风控拒绝 {msg}")
                continue
            if executor == "ctp":
                result = self._emergency_ctp_close(str(sym), pos, vol, decision)
            else:
                result = self.trader.close_position(str(sym), vol, None)
                if result.ok:
                    entry = float(getattr(pos, "entry_price", 0) or 0)
                    # 用最新收盘近似平仓价，避免 exit=entry 导致日盈亏永远为 0
                    exit_px = entry
                    try:
                        df = self.kline.fetch(str(sym), period=str(getattr(self.strategy, "timeframe", "15")))
                        if df is not None and len(df) > 0:
                            exit_px = float(df["close"].iloc[-1])
                    except Exception:  # noqa: BLE001
                        pass
                    self._apply_fill_accounting(str(sym), decision, vol, entry, exit_px)
            logs.append(f"{sym}: ok={result.ok} {result.message}")
            self._record_event("emergency_flatten", symbol=sym, ok=result.ok, message=result.message)
        self._persist_state()
        logger.warning("紧急清仓完成: {}", logs)
        return logs

    def _emergency_ctp_close(self, symbol: str, pos: Any, volume: int, decision: StrategyDecision) -> Any:
        """
        用 CTP 限价把这一笔仓平掉。

        价格用最新收盘价。多仓卖平，空仓买平。沥青和玉米仍按今仓、昨仓拆开。
        柜台回报里已经记过账的手数不再记。只补记还没入账的成交，避免把整笔仓提前清掉。
        """
        price = self._ctp_limit_price(decision)
        if price is None or price <= 0:
            from src.trader.base import OrderResult

            return OrderResult(ok=False, message="没有最新价，CTP 没法报平仓", assumed_fill=False)
        result = self.trader.place_order(
            OrderRequest(
                symbol=symbol,
                side="close",
                volume=volume,
                price=price,
                position_side=getattr(pos, "side", None),
                remark=decision.reason,
            )
        )
        raw = result.raw or {}
        filled = int(raw.get("filled_volume") or 0)
        already = int(raw.get("accounted_volume") or 0)
        # 旧的回报没有这个字段时，全部成交且已入账就不要再记。
        if "accounted_volume" not in raw and raw.get("accounted"):
            already = filled
        leftover = filled - already
        fill_price = float(raw.get("fill_price") or 0)
        if leftover > 0 and fill_price > 0:
            entry = float(getattr(pos, "entry_price", 0) or 0)
            self._apply_fill_accounting(symbol, decision, leftover, entry, fill_price)
        return result

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

        # 3) 当日盈亏统一到账本口径；账本没记上（模拟或重复回报）时退回原算法。
        if balance is not None:
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
            if abs(balance - before) > 0.009:
                self.sizer.capital = balance
            self.cfg.setdefault("risk", {})["capital"] = balance
            write_capital_to_config(balance)
            return balance
        except Exception as exc:  # noqa: BLE001
            logger.exception("信号账户记账失败，这次成交仍按原流程继续: {}", exc)
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
        if self.cfg.get("dry_run"):
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
            return sized.volume, sized.reason
        try:
            want = max(0, int(raw))
        except (TypeError, ValueError):
            return sized.volume, f"{sized.reason}（策略手数无效，按风控）"
        if want <= 0:
            return 0, f"策略指定{want}手"
        vol = min(sized.volume, want)
        note = f"策略指定{want}手，风控允许{sized.volume}手 → 取小{vol}手（{sized.reason}）"
        return vol, note

    def _execute(self, decision: StrategyDecision) -> None:
        """把策略决策交给风控 + 交易通道。"""
        if decision.action == StrategyAction.HOLD:
            logger.debug("{}: {}", decision.symbol, decision.reason)
            return

        # 均线/SuperTrend 等策略报了「反手」时，先平掉原来的仓，再开新方向。平仓没完成就不再开。
        pos_now = (getattr(self.strategy, "positions", {}) or {}).get(decision.symbol)
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
            pos_now = (getattr(self.strategy, "positions", {}) or {}).get(decision.symbol)
            if pos_now and pos_now.side != "flat":
                logger.warning("反向开仓的平仓没有完成，本根不再开新仓")
                return

        side = decision.action.value
        size_note = ""
        meta = decision.meta or {}
        if decision.action == StrategyAction.CLOSE:
            pos = self.strategy.positions.get(decision.symbol)
            held = abs(int(pos.volume)) if pos and pos.volume else 0
            # 长线减仓会在 meta 里写要平几手；没有就全平。
            raw_cut = meta.get("close_volume", meta.get("volume"))
            if raw_cut is not None and held > 0:
                try:
                    vol = max(1, min(held, int(raw_cut)))
                    size_note = f"策略指定平{vol}手"
                except (TypeError, ValueError):
                    vol = held or self.volume
            else:
                vol = held if held > 0 else self.volume
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
        # 上一笔还在柜台里时，先别再报。避免断线重连后，或限价还挂着时，重复开平。
        if executor == "ctp" and getattr(self.trader, "has_working_order", lambda _symbol: False)(decision.symbol):
            logger.warning("上一笔 {} 还没有最终回报，先不报新单", decision.symbol)
            self._record_event("order_working", symbol=decision.symbol, side=side, reason="等待上一笔回报")
            return

        # 柜台持仓和程序记账不一致时，只停开仓。平仓仍然要能发出去。
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

        if executor == "ctp":
            result = self.trader.place_order(
                OrderRequest(
                    symbol=decision.symbol,
                    side=decision.action.value,  # type: ignore[arg-type]
                    volume=vol,
                    price=price,
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
            return

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
                return

            # 记账用参考价：开仓用策略 entry；平仓尽量用最新收盘近似盈亏
            if decision.action == StrategyAction.CLOSE:
                fill_price = float(decision.entry_price or 0.0)
                try:
                    df = self.kline.fetch(decision.symbol, period=str(self.strategy.timeframe))
                    if df is not None and len(df) > 0:
                        fill_price = float(df["close"].iloc[-1])
                except Exception:  # noqa: BLE001
                    pos = (getattr(self.strategy, "positions", {}) or {}).get(decision.symbol)
                    fill_price = float(getattr(pos, "entry_price", 0) or 0)
            else:
                fill_price = float(decision.entry_price or 0.0)
                if fill_price <= 0:
                    logger.error("开仓无参考入场价，拒绝更新内部持仓，避免 entry=0")
                    self._record_event("order_no_price", symbol=decision.symbol, side=side)
                    return

            if getattr(result, "assumed_fill", False):
                logger.warning(
                    "GUI 假定成交记账（请尽快核对同花顺真实持仓）| {} {} vol={}",
                    decision.action.value,
                    decision.symbol,
                    vol,
                )

            self._apply_fill_accounting(decision.symbol, decision, vol, fill_price, fill_price)
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
        else:
            logger.error("下单失败: {}", result.message)
            self.last_error = result.message
            self._record_event("order_fail", symbol=decision.symbol, side=side, message=result.message)

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
        if str(trail_tf) != str(signal_tf):
            trail_bars = self.kline.fetch(symbol, period=str(trail_tf))
            trail_closed = drop_forming_bar(trail_bars)

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
        # 有持仓时，把还在走的那根也拼进去，让箱沿止损能看到当前影线，不必等这根收完。
        # 空仓仍只用已收盘 K 线，避免未收盘收盘价误触发开仓。
        if (
            not self._symbol_is_flat(symbol)
            and closed is not None
            and forming is not None
            and len(forming) > 0
        ):
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
        if stale and decision.action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            logger.error(
                "{} 的{}K线过期，拒绝开仓 | {}",
                symbol,
                period_label(str(signal_tf)),
                decision.reason,
            )
            self._record_event("stale_block_open", symbol=symbol, reason=decision.reason)
            return

        self._execute(decision)

    def _evaluate_symbol(
        self,
        symbol: str,
        bars: Any,
        *,
        trail_bars: Any = None,
        trend_bars: Any = None,
        now: datetime | None = None,
    ) -> StrategyDecision:
        """调用正式策略。回溯时可能带上信号时刻；老策略不收 now 就退回。"""
        uses_trend = bool(getattr(self.strategy, "uses_trend_bars", False))
        if uses_trend and now is not None:
            try:
                return self.strategy.evaluate(
                    symbol, bars, trail_bars=trail_bars, trend_bars=trend_bars, now=now
                )
            except TypeError:
                pass
        if uses_trend:
            return self.strategy.evaluate(
                symbol, bars, trail_bars=trail_bars, trend_bars=trend_bars
            )
        if now is not None:
            try:
                return self.strategy.evaluate(symbol, bars, trail_bars=trail_bars, now=now)
            except TypeError:
                pass
        return self.strategy.evaluate(symbol, bars, trail_bars=trail_bars)

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
        """扫描前向柜台对一次账。对不上时，后面的开仓会被拦住，平仓不受影响。"""
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor != "ctp" or not hasattr(self.trader, "reconcile"):
            return
        try:
            self.trader.reconcile(self._internal_nets())
        except Exception as exc:  # noqa: BLE001
            logger.exception("柜台对账失败: {}", exc)

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
        if (
            not force
            and self.sessions
            and not in_trading_session(self.sessions)
            and not self.strategy.is_flatten_time()
        ):
            logger.debug("非交易时段，跳过扫描")
            return

        if not self.logged_in:
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

        self._reconcile_ctp()
        for symbol in self.symbols:
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
        self._persist_state()

    def setup_schedule(self) -> None:
        """注册每日登录时间点。"""
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
        # GUI 多品种硬拦：没有自动切合约时挂机过于危险
        allow_multi = bool((self.cfg.get("trade") or {}).get("gui", {}).get("allow_multi_symbol", False))
        if executor == "gui" and len(self.symbols) > 1 and not allow_multi:
            raise RuntimeError(
                f"GUI 模式禁止多品种同时挂机：当前 {self.symbols}。"
                "请在配置里只留一个合约，或标定 symbol_input 并设 trade.gui.allow_multi_symbol=true。"
            )

        self.setup_schedule()
        self._running = True
        # 第一轮扫描才允许回溯补开仓；scan_symbol 里用掉后会关掉。
        self._catch_up_pending = True
        logger.info(
            "机器人启动 | dry_run={} | 品种={} | 周期={} | 扫描间隔={}s | 模式={}",
            self.cfg.get("dry_run"),
            self.symbols,
            period_label(str(self.strategy.timeframe)),
            self.scan_interval,
            getattr(self.strategy, "__class__", type(self.strategy)).__name__,
        )
        logger.info("账号通道: 华泰期货_CTP主席（未开通程序化→信号用数据计算，下单用sim/gui）")
        logger.info("把鼠标快速移到屏幕左上角可触发 pyautogui 紧急停止（FAILSAFE）")

        # 挂机防休眠：否则电脑一睡，扫描与 GUI 点击都会停
        from src.utils.keepalive import KeepAwake

        keep_display = bool((self.cfg.get("runtime") or {}).get("keep_display_awake", True))
        keepalive = KeepAwake(keep_display=keep_display)
        keepalive.start()

        from src.login.prepare import startup_should_login

        # 准备按钮刚刚登录过，这里再登录会把交易菜单再点开一次。
        if startup_should_login(self.cfg):
            self.do_login()
        else:
            logger.info("启动前刚刚登录过，常驻不再登录第二次。只核对对手价。")
            self.logged_in = True
            self._arm_order_price()
        self._arm_ctp()

        try:
            while self._running:
                schedule.run_pending()
                self.scan_once()
                # 可选：当日最后收盘后预约关机（有夜盘不会在下午就关）
                self._maybe_shutdown_after_close()
                # 分段 sleep，便于更快响应 stop()
                for _ in range(int(max(self.scan_interval, 1))):
                    if not self._running:
                        break
                    time.sleep(1)
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
        """执行通道是 CTP 时，启动就完成认证、登录和结算单确认。"""
        executor = str((self.cfg.get("trade") or {}).get("executor", "sim"))
        if executor != "ctp":
            return
        if self.cfg.get("dry_run", False):
            logger.warning("模拟开关开着，CTP 不会连接柜台，也不会发单")
            return
        if not hasattr(self.trader, "connect"):
            return
        ok, message = self.trader.connect()
        if ok:
            logger.info("CTP {}", message)
        else:
            logger.error("CTP 没有连上：{}。扫描会继续，但开新仓会停，直到登录和对账都通过。", message)

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
        )
        if not ok:
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
