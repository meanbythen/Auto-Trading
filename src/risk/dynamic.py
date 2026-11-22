"""
动态仓位（管钱和风险，不管连亏笔数）。

三层各管一件事，不重复：
1. 日最大亏损：在 RiskGuardian 里，当天亏到限额就停开。这里不再做一遍。
2. 账户从最高点回撤：掉到减半线就少开；掉到停手线就暂停开新仓。
   余额再创新高，或回撤收回到减半线以下，手数自己恢复。不要求再赚一笔。
3. 波动比近段中位大很多：下一笔少开。晃完自己解除。
   不因为波动变大就暂停开仓，也不把最后 1 手强平。

已经拿着的 2 手及以上，只在账户回撤触发时减到一半或留 1 手。
连亏几笔、影子单、必须再赢一笔，都不再用来改手数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from loguru import logger

from src.domain.ledger import ctp_history_equity, equity_and_peak, load_active_book, shown_equity
from src.strategy.indicators import average_true_range


def drawdown_frac(equity: float | None, peak: float | None) -> float | None:
    """从最高点往下掉了百分之几，用 0~1 的小数。峰值无效时按已触发处理。"""
    if equity is None or peak is None:
        return None
    top = float(peak)
    if top <= 1e-12:
        # 账本存在但峰值为 0，说明权益数据不可信；fail-closed，按满回撤暂停开仓。
        return 1.0
    now = float(equity)
    return max(0.0, (top - now) / top)


def atr_ratio(
    bars: pd.DataFrame | None,
    *,
    period: int = 14,
    lookback: int = 20,
) -> float | None:
    """
    当前 ATR 相对「不含当前这根」的近 lookback 根中位数。

    大于 1 表示现在比最近一段更晃。K 线不够或算不出时返回 None，不加这一层。
    """
    if bars is None or len(bars) < int(period) + int(lookback) + 2:
        return None
    need = {"high", "low", "close"}
    if need - set(bars.columns):
        return None
    atr = average_true_range(bars["high"], bars["low"], bars["close"], period=int(period))
    valid = atr.dropna()
    if len(valid) < int(lookback) + 1:
        return None
    now = float(valid.iloc[-1])
    base = float(valid.iloc[-(int(lookback) + 1) : -1].median())
    if base <= 1e-12 or now <= 0:
        return None
    return now / base


def _as_pct(raw: Any, default: float) -> float:
    """界面和配置都按「百分之几」读。写成 0.05 也当成 5%。夹在 0.5～50。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default)
    if value <= 0:
        value = float(default)
    if value <= 1.0:
        value *= 100.0
    return max(0.5, min(50.0, value))


@dataclass
class DynamicExposure:
    """动态减仓参数。人定边界，程序只按数字执行。"""

    enabled: bool = True
    dd_half_pct: float = 5.0
    dd_pause_pct: float = 10.0
    atr_half_mult: float = 1.8
    atr_period: int = 14
    atr_lookback: int = 20
    reduce_held: bool = True

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "DynamicExposure":
        """
        从 risk.dynamic 读取。整节都没有时默认打开。
        enabled 写成 false 就和以前不压手数一样。
        旧配置里的连亏笔数、暂停天数不再读取，避免和回撤抢着管仓。
        """
        risk = (cfg or {}).get("risk") if isinstance(cfg, dict) else {}
        raw = (risk or {}).get("dynamic") if isinstance(risk, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        enabled = raw.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "no", "off", "否"}
        half = _as_pct(raw.get("dd_half_pct", 5), 5.0)
        pause = _as_pct(raw.get("dd_pause_pct", 10), 10.0)
        if pause <= half:
            pause = min(50.0, half + 1.0)
        return cls(
            enabled=bool(enabled),
            dd_half_pct=half,
            dd_pause_pct=pause,
            atr_half_mult=max(1.01, float(raw.get("atr_half_mult", 1.8) or 1.8)),
            atr_period=max(2, int(raw.get("atr_period", 14) or 14)),
            atr_lookback=max(5, int(raw.get("atr_lookback", 20) or 20)),
            reduce_held=bool(raw.get("reduce_held", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        """写回配置。不写已经废弃的连亏项。"""
        return {
            "enabled": bool(self.enabled),
            "dd_half_pct": float(self.dd_half_pct),
            "dd_pause_pct": float(self.dd_pause_pct),
            "atr_half_mult": float(self.atr_half_mult),
            "atr_period": int(self.atr_period),
            "atr_lookback": int(self.atr_lookback),
            "reduce_held": bool(self.reduce_held),
        }

    def open_scale(
        self,
        ratio: float | None,
        dd: float | None,
    ) -> tuple[float, str]:
        """
        下一笔开仓用的倍数：1=不改，0.5=减半，0=暂停开仓。

        暂停只由账户回撤触发；波动放大最多减半。两者取更严的那一档。
        """
        if not self.enabled:
            return 1.0, ""
        scale = 1.0
        bits: list[str] = []
        half_dd = self.dd_half_pct / 100.0
        pause_dd = max(half_dd, self.dd_pause_pct / 100.0)
        if dd is not None:
            pct = dd * 100.0
            if dd >= pause_dd:
                scale = 0.0
                bits.append(
                    f"账户从最高点回撤{pct:.1f}%，已达停手线{self.dd_pause_pct:.0f}%，暂停开新仓"
                    "（赚回来、回撤低于减半线后恢复）"
                )
            elif dd >= half_dd:
                scale = min(scale, 0.5)
                bits.append(
                    f"账户从最高点回撤{pct:.1f}%，已达减半线{self.dd_half_pct:.0f}%，下一笔手数减半"
                )
        if ratio is not None and ratio >= self.atr_half_mult:
            scale = min(scale, 0.5)
            bits.append(f"波动约是近段的{ratio:.1f}倍，下一笔手数减半")
        return scale, "；".join(bits)

    def adjust_open(
        self,
        volume: int,
        ratio: float | None,
        dd: float | None,
    ) -> tuple[int, str]:
        """把已经算好的手数再压一档。1 手减半仍是 1 手，只有账户回撤到停手线才会变成 0 手。"""
        vol = int(volume)
        if vol <= 0 or not self.enabled:
            return vol, ""
        scale, why = self.open_scale(ratio, dd)
        if not why:
            return vol, ""
        if scale <= 0:
            return 0, why
        if scale >= 0.999:
            return vol, why
        scaled = int(vol * scale)
        if scaled < 1:
            return vol, f"{why}；已经是{vol}手，没法再减，这一笔仍开{vol}手"
        new_vol = min(vol, max(1, scaled))
        return new_vol, f"{why} → {new_vol}手"

    def held_target(self, held: int, dd: float | None) -> int | None:
        """
        已经拿着的仓要减到几手。None=不减。

        只看账户回撤，不看波动、不看连亏笔数。至少留 1 手。
        """
        if not self.enabled or not self.reduce_held:
            return None
        lots = int(held)
        if lots < 2 or dd is None:
            return None
        half_dd = self.dd_half_pct / 100.0
        pause_dd = max(half_dd, self.dd_pause_pct / 100.0)
        if dd >= pause_dd:
            return 1
        if dd >= half_dd:
            return max(1, lots // 2)
        return None


def apply_dynamic_open(
    cfg: dict[str, Any] | None,
    volume: int,
    bars: pd.DataFrame | None,
    *,
    equity: float | None = None,
    peak: float | None = None,
) -> tuple[int, str]:
    """开仓前最后一刀：回撤 + 波动。开关关掉时原手数原样返回。"""
    dyn = DynamicExposure.from_config(cfg)
    if not dyn.enabled:
        return int(volume), ""
    ratio = atr_ratio(bars, period=dyn.atr_period, lookback=dyn.atr_lookback)
    dd = drawdown_frac(equity, peak)
    return dyn.adjust_open(int(volume), ratio, dd)


def live_equity_and_peak_or_error(
    root: Any,
) -> tuple[float | None, float | None, str]:
    """
    账本当前余额和曾经到过的最高点，附带读取错误。

    账本不存在时返回 (None, None, "")，视为新账；读取异常时返回错误文本，
    调用方必须 fail-closed（暂停开仓），不能当成 0 回撤放行。
    """
    try:
        book = load_active_book(root)
        if not book:
            return None, None, ""
        shown = shown_equity(book)
        if str(book.get("funding") or "") == "ctp":
            series = ctp_history_equity(book)
            peak = max((point[1] for point in series), default=shown)
            return shown, max(peak, shown), ""
        last, peak = equity_and_peak(book)
        if shown > 0:
            return shown, max(peak, shown), ""
        return last, peak, ""
    except Exception as exc:  # noqa: BLE001
        logger.error("读取账本权益失败，按已触发回撤暂停开仓: {}", exc)
        return None, None, f"读取账本权益失败: {exc}"


def live_equity_and_peak(root: Any) -> tuple[float | None, float | None]:
    """账本当前余额和最高点，仅供界面展示。读不到返回 None，不再伪装成 0。"""
    equity, peak, _ = live_equity_and_peak_or_error(root)
    return equity, peak


def describe_status(
    cfg: dict[str, Any] | None,
    *,
    ratio: float | None = None,
    last_note: str = "",
    equity: float | None = None,
    peak: float | None = None,
) -> str:
    """运行台一行说明。关掉时也写一句，避免以为程序坏了。"""
    dyn = DynamicExposure.from_config(cfg)
    if not dyn.enabled:
        return "动态减仓已关：账户回撤和波动放大都不会自动少开。"
    dd = drawdown_frac(equity, peak)
    scale, why = dyn.open_scale(ratio, dd)
    if last_note:
        return f"动态减仓  {last_note}"
    if why:
        extra = "（1手账户减半仍是1手）" if scale == 0.5 else ""
        return f"动态减仓  {why}{extra}"
    bits: list[str] = []
    if dd is not None:
        bits.append(f"回撤{dd * 100:.1f}%")
    else:
        bits.append("回撤未算")
    if ratio is not None:
        bits.append(f"波动{ratio:.1f}倍")
    bits.append("未触发减仓")
    return "动态减仓  " + "，".join(bits)
