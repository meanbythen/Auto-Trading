"""
回测滑点三档

功能：把「基线 / 压力 / 实测」三档滑点摆在一起，避免回测、压力测试、实盘各说各的。
原理：
  - 基线：本次回测填的 backtest.slippage_ticks。
  - 压力：基线 + backtest.evaluation.slippage_extra_ticks（默认 1 跳），评估里的滑点压力用这一档。
  - 实测：data/slippage_log.csv 里同品种真实成交的 70 分位，向上取 0.5 跳；满 3 笔才给。
三档只拿来对照，不会自动写配置；要改仍由人在回测页点确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.account.slippage_log import product_of, summarize, summarize_by_symbol

DEFAULT_EXTRA_TICKS = 1.0


@dataclass(frozen=True)
class SlippageLadder:
    """一次回测要看的三种滑点口径。"""

    baseline: float
    extra: float
    stress: float
    actual: float | None
    actual_n: int
    symbol: str
    has_log: bool

    @property
    def actual_label(self) -> str:
        if self.actual is not None:
            scope = self.symbol or "全部品种"
            return f"{self.actual:g} 跳（{scope} 真实 {self.actual_n} 笔 70 分位）"
        if not self.has_log:
            return "还没有滑点记录"
        return "真实成交不足 3 笔，先不给"


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cfg_box(cfg: dict[str, Any] | None, name: str) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        return {}
    box = cfg.get(name)
    return box if isinstance(box, dict) else {}


def _actual_ticks(
    root: Path | str | None,
    symbol: str,
) -> tuple[float | None, int, bool]:
    """实测档：同品种真实成交的 70 分位。没有记录或样本不足就明确给空。"""
    if root is None:
        return None, 0, False
    has_log = bool(summarize(root)["rows"])
    if not has_log:
        return None, 0, False
    if symbol:
        want = product_of(symbol)
        for item in summarize_by_symbol(root):
            if item["symbol"] == want and item["used_real"]:
                return float(item["suggested"]), int(item["n"]), True
        return None, 0, True
    info = summarize(root)
    if info["used_real"] and info["n"] >= 3:
        return float(info["suggested"]), int(info["n"]), True
    return None, 0, True


def build_ladder(
    cfg: dict[str, Any] | None = None,
    *,
    symbol: str = "",
    root: Path | str | None = None,
    baseline: float | None = None,
) -> SlippageLadder:
    """
    读配置和滑点记录，组出三档。

    baseline：回测摘要用快照里的实际值覆盖，避免页面已经改了但摘要还是旧配置。
    """
    bt = _cfg_box(cfg, "backtest")
    raw_base = baseline if baseline is not None else _number(bt.get("slippage_ticks"))
    base = max(0.0, raw_base if raw_base is not None else 0.0)
    evaluation = bt.get("evaluation") if isinstance(bt.get("evaluation"), dict) else {}
    raw_extra = _number(evaluation.get("slippage_extra_ticks"))
    extra = max(0.0, raw_extra if raw_extra is not None else DEFAULT_EXTRA_TICKS)
    actual, actual_n, has_log = _actual_ticks(root, symbol)
    return SlippageLadder(
        baseline=base,
        extra=extra,
        stress=base + extra,
        actual=actual,
        actual_n=actual_n,
        symbol=product_of(symbol),
        has_log=has_log,
    )


def ladder_lines(ladder: SlippageLadder, *, used: float | None = None) -> list[str]:
    """回测摘要用的两行：三档对照 + 本次用的哪档。"""
    lines = [
        f"滑点三档  基线 {ladder.baseline:g} 跳 / 压力 {ladder.stress:g} 跳"
        f"（+{ladder.extra:g}）/ 实测 {ladder.actual_label}",
    ]
    if used is not None:
        lines.append(f"本次滑点  {used:g} 跳（改档只在回测页确认，不自动写配置）")
    return lines
