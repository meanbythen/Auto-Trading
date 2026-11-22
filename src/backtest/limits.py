"""回测账本与开仓限制（Ledger / BacktestLimits）。

从 engine.py 抽出来：手数、日亏损熔断、开仓间隔、没开成的原因归类。
算法和实盘一致，只是时间轴换成K线自己的时间。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.utils.trading_day import futures_trading_day


def skip_reason_key(reason: str) -> str:
    """把没开成的原因归成稳定的类别，同一时段同一类只记一次。"""
    text = (reason or "未开仓").strip()
    if "止损过宽" in text or "低于最小" in text or "只允许约" in text:
        return "止损过宽，手数不够"
    if "熔断" in text or "当日亏损" in text:
        return "当日亏损达到上限"
    if "开仓次数" in text:
        return "当天开仓次数达到上限"
    if "间隔" in text:
        return "两笔开仓间隔太近"
    if "仓位上限" in text or "保证金" in text:
        return "仓位上限/保证金不够"
    return text[:24] or "未开仓"


class BacktestLimits:
    """
    回测用的开仓限制，算法和实盘相同。

    实盘的下单间隔用的是电脑时钟。回测一根根K线瞬间就算完，如果也用电脑时钟，
    第二笔就会被当成「刚刚才下过单」而全部拒绝。所以这里改用K线自己的时间。
    换日也按K线所属的期货交易日清零：夜盘 21:00 就是下一交易日，不是等到零点。
    这样和实盘一致，夜盘跨过零点也不会把当日计数清掉。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        from src.risk.guardian import RiskGuardian
        from src.risk.sizing import PositionSizer

        self.sizer = PositionSizer(cfg)
        self.guard = RiskGuardian.from_config(cfg)
        # 回测里日亏熔断只拦这一次回测的开仓，不要写进运行台的「最近告警」。
        self.guard.write_alerts = False
        # 关掉时固定 1 手，不再因为止损远、日亏损或开仓次数把信号丢掉。
        from src.strategy.types import config_flag

        self.apply = config_flag((cfg.get("risk") or {}).get("apply_limits", True), default=True)
        # 和实盘同一个单笔上限：trade.ctp.max_order_volume。只往下压手数，不放大。
        # 回测没有登录账号，不读取券商档案里的更小上限。
        ctp = (cfg.get("trade") or {}).get("ctp") or {}
        raw_cap = ctp.get("max_order_volume")
        self.max_per_order: int | None = None
        if raw_cap not in (None, ""):
            try:
                cap = int(raw_cap)
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                self.max_per_order = cap
        # 和回测页「真实账户口径」是同一个开关：开=手数跟着剩余权益走；关=一直按这一段的起始资金。
        self.chain_capital = config_flag(
            (cfg.get("backtest") or {}).get("chain_capital"), default=False
        )
        self.day: Any = None
        self.last_open_at: datetime | None = None
        self.skipped: dict[str, int] = {}
        # 回测对照：调用方明确给了 volume 时强制用这个手数，不参与风险算仓。
        self.force_volume: int | None = None
        self.closed_pnls: list[float] = []
        self.closed_ats: list[datetime] = []
        # 和实盘 matched_closes 同构的净平仓样本；空列表也代表本回测真实零样本。
        self.kelly_rows: list[dict[str, Any]] = []
        # 账户权益最高点，给回撤减仓用。开跑时用初始资金垫上。
        self.start_capital: float = float(self.sizer.capital)
        self.equity: float = 0.0
        self.peak_equity: float = 0.0
        self.cfg = cfg

    def note_equity(self, equity: float) -> None:
        """记下当前权益；创新高就抬峰值。回撤按峰值往下量。"""
        now_eq = float(equity)
        self.equity = now_eq
        if now_eq > self.peak_equity:
            self.peak_equity = now_eq

    def on_bar(self, now: datetime) -> None:
        """新交易日开始时，当日亏损和开仓次数清零，熔断解除。持仓不动。"""
        day = futures_trading_day(now)
        if self.day == day:
            return
        self.day = day
        self.guard.daily_pnl = 0.0
        self.guard.orders_today = 0
        self.guard.open_orders_today = 0
        self.guard.halted = False
        self.guard.halt_reason = ""

    def size_open(
        self,
        symbol: str,
        entry: float | None,
        stop: float | None,
    ) -> tuple[int, str]:
        """按每笔风险和止损距离算手数。算不出 1 手就返回 0。限制关掉时固定 1 手。"""
        if not self.apply:
            return 1, "未启用开仓限制，固定1手"
        if self.force_volume is not None:
            return max(0, self.force_volume), f"回测指定{self.force_volume}手（对照用）"
        # 真实账户口径开：这一笔按当前剩余权益算（前面赚了可能多开、亏多了可能开不了）。
        # 关：这一段从头到尾都用起始资金，便于和当时实盘、和单合约对照。净值曲线仍跟着盈亏走。
        if self.chain_capital and self.equity > 1e-9:
            self.sizer.capital = float(self.equity)
        else:
            self.sizer.capital = float(self.start_capital)
        sized = self.sizer.calc_open(
            symbol,
            entry,
            stop,
            current_abs_position=self.guard.total_abs_position(),
            positions=self.guard.positions,
            max_per_order=self.max_per_order,
            kelly_rows=self.kelly_rows,
        )
        return int(sized.volume), sized.reason

    def allow_open(
        self,
        now: datetime,
        volume: int,
        side: str = "",
        symbol: str = "",
    ) -> tuple[bool, str]:
        """日亏损、开仓次数、两笔之间的间隔。平仓不走这里。限制关掉时直接放行。

        直接调用实盘同一个 RiskGuardian，只在时间轴上换成K线自己的时间。
        """
        if not self.apply:
            return True, "未启用开仓限制"
        if volume <= 0:
            return False, "手数必须大于 0"
        return self.guard.allow(
            side,
            int(volume),
            symbol=symbol or None,
            now=now.timestamp(),
        )

    def note_skip(self, reason: str) -> None:
        """记下没开成的原因，回测结果里汇总给用户看。"""
        key = skip_reason_key(reason)
        self.skipped[key] = self.skipped.get(key, 0) + 1

    def on_open(self, now: datetime, side: str, volume: int, symbol: str) -> None:
        self.last_open_at = now
        self.guard.on_order_filled(side, volume, symbol=symbol)
        # Guardian 默认用墙上时钟记上一笔；回测必须改成K线时间，否则下一根永远被间隔挡住。
        self.guard.last_order_ts = now.timestamp()
        self.guard.last_open_ts = now.timestamp()

    def on_close(self, pnl: float, volume: int, symbol: str, at: datetime | None = None) -> None:
        self.guard.update_pnl(pnl)
        self.guard.on_order_filled("close", volume, symbol=symbol)
        if at is not None:
            # 平仓也按K线时间记最后一笔，否则下一根会被墙上时钟的间隔挡住。
            self.guard.last_order_ts = at.timestamp()
        self.closed_pnls.append(float(pnl))
        self.closed_ats.append(at if at is not None else datetime.now())
        # on_close 的 pnl 已由回测引擎扣过手续费和滑点，直接作为凯利净样本。
        self.kelly_rows.append({"symbol": symbol, "pnl": float(pnl), "volume": int(volume)})


def cap_open_volume(allowed: int, meta_volume: Any, *, limits_apply: bool) -> tuple[int, str]:
    """
    策略指定手数只能往下压，不能超过资金管理允许的手数。

    关掉开仓限制（apply_limits=false）时才按策略手数，方便做「不限仓」对比。
    """
    if meta_volume is None:
        return int(allowed), ""
    try:
        want = max(0, int(meta_volume))
    except (TypeError, ValueError):
        return int(allowed), "策略手数无效，按风控"
    if want <= 0:
        return 0, f"策略指定{want}手"
    if not limits_apply:
        return want, f"策略指定{want}手（未启用开仓限制）"
    vol = min(int(allowed), want)
    return vol, f"策略指定{want}手，风控允许{allowed}手 → 取小{vol}手"


def apply_size_scale(volume: int, meta_scale: Any) -> tuple[int, str]:
    """混合版多因子按信号强弱把手数再打一折，不能超过已经算好的风控手数。"""
    if meta_scale is None or int(volume) <= 0:
        return int(volume), ""
    try:
        ratio = float(meta_scale)
    except (TypeError, ValueError):
        return int(volume), ""
    if ratio <= 0:
        return 0, f"信号强度×{ratio:.2f}→0手"
    if ratio >= 0.999:
        return int(volume), ""
    scaled = max(1, int(int(volume) * ratio))
    vol = min(int(volume), scaled)
    return vol, f"信号强度×{ratio:.2f}→{vol}手"
