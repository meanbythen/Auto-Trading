"""实盘策略腿路由：一个品种一条腿，各自引擎与周期，共享同一份账户状态。

组合回测早就按 strategy.legs 分引擎跑；实盘以前只有一个全局引擎。
这里把每条腿各建一个策略实例，再把持仓、信号去重、挂单意图等状态指向同一份，
保证运行层仍然只看到「一个账户、一份持仓」。
"""

from __future__ import annotations

import copy
from typing import Any

from loguru import logger

# 这些状态必须跨腿共享；否则 A 腿开的仓，B 腿的信号去重和持仓检查都看不见。
SHARED_STATE_ATTRS = (
    "_signaled_bar",
    "_last_entry_bar",
    "_pending",
    "_close_side",
    "_close_entry",
    "_used_bar",
    "_used_session",
    "_base_stop",
    "_pending_add",
)


class LegStrategyRouter:
    """按品种把 evaluate/on_filled 分给对应腿；其余属性委托给第一条腿。"""

    def __init__(
        self,
        strategies: dict[str, Any],
        symbols: list[str],
        cfgs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._strategies = {str(key).strip().lower(): value for key, value in strategies.items()}
        self._cfgs = {str(key).strip().lower(): value for key, value in (cfgs or {}).items()}
        self._primary = next(iter(self._strategies.values()))
        self.symbols = list(symbols)

    def __getattr__(self, item: str) -> Any:
        if item in ("_strategies", "_primary", "_cfgs"):
            raise AttributeError(item)
        return getattr(self._primary, item)

    def __setattr__(self, name: str, value: Any) -> None:
        """共享状态被整体重置时，所有腿一起换，不能只换第一条腿。"""
        object.__setattr__(self, name, value)
        if name in SHARED_STATE_ATTRS:
            for strategy in self.__dict__.get("_strategies", {}).values():
                setattr(strategy, name, value)

    def strategy_for(self, symbol: str) -> Any:
        """取这个品种对应的腿；没配腿的品种退回第一条腿。"""
        key = str(symbol or "").strip().lower()
        return self._strategies.get(key) or self._primary

    def cfg_for(self, symbol: str) -> dict[str, Any] | None:
        """这个品种的腿配置。回溯补开仓要用它另建临时实例，别污染实盘状态。"""
        key = str(symbol or "").strip().lower()
        return self._cfgs.get(key) or self._cfgs.get(next(iter(self._cfgs), ""))

    def evaluate(self, symbol: str, bars: Any, **kwargs: Any) -> Any:
        return self.strategy_for(symbol).evaluate(symbol, bars, **kwargs)

    def on_filled(self, symbol: str, *args: Any, **kwargs: Any) -> Any:
        return self.strategy_for(symbol).on_filled(symbol, *args, **kwargs)

    def on_trade_closed(self, symbol: str, *args: Any, **kwargs: Any) -> Any:
        """平仓后的进化/统计跟着这条腿走；腿不支持就静默跳过。"""
        callback = getattr(self.strategy_for(symbol), "on_trade_closed", None)
        return callback(symbol, *args, **kwargs) if callable(callback) else None

    def note_factor_pnl(self, symbol: str, *args: Any, **kwargs: Any) -> Any:
        callback = getattr(self.strategy_for(symbol), "note_factor_pnl", None)
        return callback(symbol, *args, **kwargs) if callable(callback) else None

    def bind_symbol_sessions(self, symbol: str) -> None:
        """每条腿建实例时已按自己的品种绑定时段，这里不再用选中品种覆盖。"""
        del symbol

    def leg_summary(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for name, strategy in self._strategies.items():
            out.append(
                {
                    "symbol": name,
                    "engine": str(getattr(strategy, "engine_name", "") or type(strategy).__name__),
                    "timeframe": str(getattr(strategy, "timeframe", "") or ""),
                }
            )
        return out


def _share_state(strategies: list[Any]) -> None:
    primary = strategies[0]
    for attr in SHARED_STATE_ATTRS:
        container = next(
            (getattr(strategy, attr) for strategy in strategies if hasattr(strategy, attr)),
            None,
        )
        if not isinstance(container, dict):
            continue
        for strategy in strategies:
            own = getattr(strategy, attr, None)
            if isinstance(own, dict) and own is not container:
                container.update(own)
        for strategy in strategies:
            if hasattr(strategy, attr):
                setattr(strategy, attr, container)

    positions: dict[str, Any] = {}
    for strategy in strategies:
        own = getattr(strategy, "positions", None)
        if isinstance(own, dict):
            positions.update(own)
    primary.positions = positions
    for strategy in strategies:
        strategy.positions = positions


def create_leg_router(cfg: dict[str, Any], fallback: Any) -> Any:
    """按 strategy.legs 建实盘腿路由；腿不足两条或建不起来时退回原策略。"""
    try:
        from src.backtest.portfolio import parse_legs
        from src.strategy.factory import create_strategy

        legs = parse_legs(cfg)
        if len(legs) <= 1:
            return fallback
        built: dict[str, Any] = {}
        cfgs: dict[str, dict[str, Any]] = {}
        for leg in legs:
            symbol = str(leg.get("symbol") or "").strip()
            if not symbol:
                continue
            leg_cfg = copy.deepcopy(cfg)
            box = leg_cfg.setdefault("strategy", {})
            box["engine"] = leg.get("engine") or box.get("engine")
            box["timeframe"] = leg.get("timeframe") or box.get("timeframe")
            box["trail_timeframe"] = leg.get("trail_timeframe") or box.get("trail_timeframe")
            box["symbols"] = [symbol]
            box.pop("legs", None)
            built[symbol] = create_strategy(leg_cfg)
            cfgs[symbol] = leg_cfg
        if len(built) <= 1:
            return fallback
        strategies = list(built.values())
        _share_state(strategies)
        router = LegStrategyRouter(built, list(built), cfgs)
        logger.info("实盘策略腿已启用：{}", router.leg_summary())
        return router
    except Exception as exc:  # noqa: BLE001
        # 建腿失败不能把实盘带崩：退回全局引擎，按单策略继续跑。
        logger.error("实盘策略腿创建失败，退回全局引擎：{}", exc)
        return fallback
