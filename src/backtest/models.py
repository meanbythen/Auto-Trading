"""回测结果的数据结构（RejectedSignal / BacktestResult）。

从 engine.py 抽出来：这些只是结果载体，不含计算。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.backtest.evaluation import EvaluationReport
from src.backtest.metrics import Performance, TradeRecord


@dataclass
class RejectedSignal:
    """一次出现了开仓信号、但没有开成的记录。成交明细之外单独列，方便查为什么没做。"""

    symbol: str
    time: str
    side: str  # 多 / 空
    price: float | None  # 信号价（下一根开盘时就是准备成交的价）
    stop: float | None  # 如果开了，止损放在哪
    stage: str  # 策略过滤 / 风控手数 / 开仓限制 / 已有持仓
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    trail_timeframe: str
    bars: int
    performance: Performance
    trades: list[TradeRecord] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    # 权益曲线每一段对应的时间。评估算年化、回撤持续期要用；老结果可能为空。
    equity_times: list[str] = field(default_factory=list)
    params_snapshot: dict[str, Any] = field(default_factory=dict)
    # 按 docs/策略评估标准 打的分。未启用评估或没有成交时为空。
    evaluation: EvaluationReport | None = None
    # 有信号但没开成的记录。为空表示这段行情里每个开仓信号都成交了。
    rejected: list[RejectedSignal] = field(default_factory=list)
    # 同一根 OHLC 内止损与离场先后不明时，保守成交及其相对乐观口径的影响。
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    # 多参数联合扰动结果。没跑或参数不足两个时为空。
    joint_perturbation: dict[str, Any] = field(default_factory=dict)
    # 这次回测跑完的本地时间。导出 Excel 时原样写进每一行，用来区分前后几次回测。
    ran_at: str = ""
    # 实验清单与归档前暂存的 K 线副本。老结果不填也能继续读取和导出。
    experiment: dict[str, Any] = field(default_factory=dict)
    # P4 统一运行清单：run_id/trace_id/策略版本/数据版本/耗时。
    run: dict[str, Any] = field(default_factory=dict)
    _experiment_data: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "trail_timeframe": self.trail_timeframe,
            "bars": self.bars,
            "ran_at": self.ran_at,
            "performance": self.performance.to_dict(),
            "trades": [t.to_dict() for t in self.trades],
            "rejected": [r.to_dict() for r in self.rejected],
            "ambiguities": self.ambiguities,
            "equity_last": self.equity[-1] if self.equity else None,
            # 完整权益曲线随归档保存，复盘页和审计视图不用重跑回测。
            "equity": [round(float(value), 2) for value in self.equity],
            "equity_times": self.equity_times,
            "params_snapshot": self.params_snapshot,
            "evaluation": self.evaluation.to_dict() if self.evaluation else {},
            "joint_perturbation": self.joint_perturbation,
            "experiment": self.experiment,
            "run": self.run,
        }
