"""
交易执行器工厂（多通道路线注册表）
功能：根据 trade.executor 创建 sim / gui / ctp。
原理：与策略工厂同样用注册表，非法名称回退 sim。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from loguru import logger

from .base import TraderBase
from .ctp_trader import CtpTrader
from .gui_trader import GuiTrader
from .sim_trader import SimTrader

TraderFactory = Callable[[dict[str, Any]], TraderBase]

DEFAULT_EXECUTOR = "sim"


def _make_sim(cfg: dict[str, Any]) -> TraderBase:
    root = Path(cfg.get("_project_root", "."))
    return SimTrader(data_dir=root / "data")


def _make_gui(cfg: dict[str, Any]) -> TraderBase:
    return GuiTrader(cfg)


def _make_ctp(cfg: dict[str, Any]) -> TraderBase:
    return CtpTrader(cfg)


# 执行通道菜单：配置里 executor 填左边的名字
REGISTRY: dict[str, TraderFactory] = {
    "sim": _make_sim,
    "gui": _make_gui,
    "ctp": _make_ctp,
}


def list_executors() -> list[str]:
    """返回可供配置的执行通道名。"""
    return sorted(REGISTRY.keys())


def create_trader(cfg: dict[str, Any]) -> TraderBase:
    """
    创建交易执行器。

    参数:
        cfg: 全局配置

    返回:
        TraderBase 子类实例
    """
    raw = str((cfg.get("trade") or {}).get("executor") or "").lower().strip()
    is_live = not bool(cfg.get("dry_run", True))
    if not raw:
        if is_live:
            raise ValueError(
                "trade.executor 未填写，真实交易下拒绝启动；"
                f"可选: {', '.join(list_executors())}"
            )
        raw = DEFAULT_EXECUTOR

    factory = REGISTRY.get(raw)
    if factory is None:
        if is_live:
            # 通道名写错时宁可启动失败，也不能把实盘静默换成模拟盘。
            raise ValueError(
                f"未知 trade.executor={raw!r}，真实交易下拒绝启动；"
                f"可选: {', '.join(list_executors())}"
            )
        logger.warning(
            "未知 trade.executor={}，可选: {}；回退到 {}",
            raw,
            ", ".join(list_executors()),
            DEFAULT_EXECUTOR,
        )
        raw = DEFAULT_EXECUTOR
        factory = REGISTRY[DEFAULT_EXECUTOR]

    trader = factory(cfg)
    from src.trader.go_live import current_stage_from_cfg, stage_label

    logger.info(
        "执行通道已加载: executor={} class={} 上线阶段={}",
        raw,
        type(trader).__name__,
        stage_label(current_stage_from_cfg(cfg)),
    )
    return trader
