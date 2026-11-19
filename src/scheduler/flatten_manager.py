"""收盘清仓时钟、重试和清仓待办管理。"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger

from src.strategy import StrategyAction, StrategyDecision
from src.utils.trading_day import is_trading_day, parse_closed_days


def in_trading_session(
    sessions: list[list[str]],
    now: datetime | None = None,
    *,
    grace_sec: float = 90.0,
) -> bool:
    """运行时转调 runner 入口，保留旧调用方对该函数的替换兼容。"""
    from src.scheduler.runner import in_trading_session as runner_check

    if grace_sec == 90.0:
        return runner_check(sessions, now)
    return runner_check(sessions, now, grace_sec=grace_sec)


class FlattenManagerMixin:
    """收盘清仓方法；共享运行状态仍保存在 TradingBot 实例上。"""

    def flatten_if_due(self, force: bool = False, now: datetime | None = None) -> None:
        """
        到「收盘清仓」钟点，按系统时钟直接平仓，不等新K线。

        行情拉不到也要能平：这里不取K线，只看持仓。到点后只要还没平掉，
        每 10 秒再试一次，不会因为 10 分钟窗口走完就放弃。
        策略自己在拿到收盘K线时也会平，这里是按时钟的兜底。
        """
        now = now or datetime.now()
        try:
            in_window = bool(self.strategy.is_flatten_time())
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查收盘清仓钟点失败: {}", exc)
            in_window = False
        due = bool(force or self._flatten_pending or in_window)
        if not due:
            self._flatten_try.clear()
            return
        # 节假日/临时休市不点单：待办保留到下一个交易日。手动强制平仓不受限制。
        if not force and not is_trading_day(
            now.date(),
            extra_closed=self._closed_days(),
            calendar=getattr(self, "holiday_calendar", None),
        ):
            return
        if force:
            # 新的一轮清仓开始。几秒内策略刚报过的平仓不要清掉，避免和止损各打一次。
            self._flatten_sent.clear()
            self._flatten_attempts.clear()
            recent = time.monotonic() - 15.0
            self._flatten_try = {
                name: stamp
                for name, stamp in (self._flatten_try or {}).items()
                if float(stamp or 0) >= recent
            }
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
            too_fresh = getattr(self, "_own_fill_too_fresh", None)
            fill_kind = getattr(self, "_own_fill_kind", None)
            # 刚平仓成交时，柜台持仓查询常慢几秒。这时不要按「程序账又有仓」再报一次平仓。
            if callable(too_fresh) and too_fresh(symbol):
                kind = fill_kind(symbol) if callable(fill_kind) else "open"
                if str(kind) == "close":
                    remaining = True
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
                    meta={"force": True},
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
                elif self._flatten_retry_limit_reached(symbol, in_window=in_window):
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

    def _flatten_retry_limit_reached(self, symbol: str, *, in_window: bool) -> bool:
        """
        清仓重试的上限。

        在收盘清仓窗口内一直重试（窗口本来就只有十分钟），直到确认空仓；
        窗口过去后最多再试 30 次，防止整夜反复点平仓。
        """
        if in_window:
            return False
        return int(self._flatten_attempts.get(symbol, 0)) >= 30

    def _has_any_position(self) -> bool:
        """还有没有没平的仓。用于判断清仓失败后要不要继续重试。"""
        return any(not self._symbol_is_flat(symbol) for symbol in self.scan_symbols)

    def _closed_days(self) -> set[date]:
        """配置里的额外休市日（临时休市/调休）。配置读不到时按没有。"""
        cfg = getattr(self, "cfg", None) or {}
        raw = (cfg.get("schedule") or {}).get("closed_days") if isinstance(cfg, dict) else None
        return parse_closed_days(raw)

    def _next_flatten_at(self, now: datetime | None = None) -> datetime | None:
        """下一次收盘清仓的钟点。向后找下一个真正会清仓的交易日，周末和节假日不预约。"""
        if not getattr(self.strategy, "flatten_at_close", False):
            return None
        clocks: list[Any] = list(getattr(self.strategy, "flatten_times", []) or [])
        if not clocks:
            return None
        now = now or datetime.now()
        closed = self._closed_days()
        calendar = getattr(self, "holiday_calendar", None)
        soonest: datetime | None = None
        # 不能只顺延一天：周五收盘后顺延到周六，周策略会说「那天不清仓」，
        # 定时器就再也定不上，周末的仓位没人管。国庆连休 7 天时还要跨过整段假期，
        # 所以向后翻半个月找交易日。
        for offset in range(0, 16):
            day = (now + timedelta(days=offset)).date()
            if not is_trading_day(day, extra_closed=closed, calendar=calendar):
                continue
            for clock in clocks:
                try:
                    when = datetime.combine(day, clock)
                except (TypeError, ValueError):
                    continue
                if when <= now:
                    continue
                # 夜日连续时策略会否掉夜盘 22:55。时钟预约必须问策略，不能按品种表硬撞。
                if not self._strategy_would_flatten_at(when):
                    continue
                if soonest is None or when < soonest:
                    soonest = when
        return soonest

    def _strategy_would_flatten_at(self, when: datetime) -> bool:
        """这个钟点策略会不会真的清仓。问 is_flatten_time(when)，不要只看 flatten_times。"""
        checker = getattr(self.strategy, "is_flatten_time", None)
        if checker is None:
            return True
        try:
            return bool(checker(when))
        except TypeError:
            return True
