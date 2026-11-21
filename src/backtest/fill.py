"""回测成交近似（FillModel）。

从 engine.py 抽出来：成交价怎么定只跟玩法有关，跟K线推进逻辑无关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategy.types import config_flag


@dataclass(frozen=True)
class FillModel:
    """
    回测成交近似。

    - on_next_open：信号在当根收盘后才确认，按下一根开盘成交（跨休市跳到真正开盘的那根，不在午休整点成交）。
    - slippage_ticks：成交价按买卖方向各让几跳，模拟挂单排队和盘口差价。
    - price_tick：一跳多少钱，默认取交易配置里的 price_tick。
    """

    on_next_open: bool = True
    slippage_ticks: float = 1.0
    price_tick: float = 1.0
    # 打开后开仓再套一层实盘的过期K/追价闸。默认关，不改历史回测成绩。
    mirror_live_gates: bool = False

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any], symbol: str | None = None) -> "FillModel":
        bt = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
        trade = cfg.get("trade") if isinstance(cfg.get("trade"), dict) else {}
        ctp = trade.get("ctp") if isinstance(trade.get("ctp"), dict) else {}
        tick_raw = bt.get("price_tick")
        if tick_raw in (None, ""):
            # 品种表里填了每跳价格就优先用它，没填才回到全局 price_tick。
            from src.risk.universe_table import product_key

            key = product_key(str(symbol or ""))
            contracts = (cfg.get("risk") or {}).get("contracts") or {}
            box = contracts.get(key) if isinstance(contracts.get(key), dict) else {}
            tick_raw = (box or {}).get("price_tick")
        if tick_raw in (None, ""):
            tick_raw = ctp.get("price_tick", trade.get("price_tick", 1))
        try:
            tick = float(tick_raw)
        except (TypeError, ValueError):
            tick = 1.0
        if tick <= 0:
            tick = 1.0
        raw_slip = bt.get("slippage_ticks", 1)
        try:
            slip_ticks = max(0.0, float(raw_slip))
        except (TypeError, ValueError):
            slip_ticks = 1.0
        return cls(
            on_next_open=config_flag(bt.get("fill_on_next_open"), default=True),
            slippage_ticks=slip_ticks,
            price_tick=tick,
            # 必须从配置读进来；漏了的话界面打开开关也不会拦开仓。
            mirror_live_gates=config_flag(bt.get("mirror_live_gates"), default=False),
        )

    @property
    def slip(self) -> float:
        """一跳滑点折算成的价格差。"""
        return round(self.slippage_ticks * self.price_tick, 10)

    def buy(self, ref: float) -> float:
        """买入成交价：往上让滑点。"""
        return round(float(ref) + self.slip, 10)

    def sell(self, ref: float) -> float:
        """卖出成交价：往下让滑点。"""
        return round(float(ref) - self.slip, 10)

    def describe(self) -> str:
        """回测结果里说明这次是怎么成交的。"""
        parts = ["信号确认后按下一根开盘成交（休市整点会跳到重新开盘）" if self.on_next_open else "成交按策略价，未给则当根收盘"]
        if self.slip > 0:
            parts.append(f"滑点 {self.slippage_ticks:g} 跳（每跳 {self.price_tick:g}）")
        else:
            parts.append("未加滑点")
        parts.append("破止损按止损价成交，跳空按开盘价")
        parts.append("实盘按对手价/盘口成交并受单笔上限约束；回测按上述参考价加减固定跳数")
        if self.mirror_live_gates:
            parts.append("已套实盘过期K和追价闸（放弃的根不会成交）")
        return "成交近似  " + "；".join(parts)
