"""
策略档案：每个正式引擎的开平规则、周期、隔夜与清仓口径。

功能：给回测、复盘和评估报告一个统一的「这套策略到底是什么」的说法，
不靠界面文字现拼，也不在报告里各写一份。

原理：字段全部来自引擎代码的默认值和配置页说明；没有把握的写「以规则文件为准」，
不猜。别名统一用 factory.ENGINE_ALIASES 换算成本名后再查。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategy.factory import ENGINE_ALIASES, ENGINE_LABELS


@dataclass(frozen=True)
class EngineProfile:
    """一张规则卡。close_default=True 表示默认跟着收盘清仓。"""

    code: str
    entry: str
    exit_rule: str
    timeframe: str
    close_default: bool
    public_filter: str
    weak_market: str

    @property
    def label(self) -> str:
        return ENGINE_LABELS.get(self.code, self.code)

    @property
    def overnight(self) -> str:
        return "默认不隔夜" if self.close_default else "默认可隔夜"

    def header(self, close_flat: bool | None = None) -> str:
        """评估 / 复盘抬头的一句话。配置里显式改过清仓开关时以配置为准。"""
        if close_flat is None:
            close = "跟随收盘清仓" if self.close_default else "默认可留过收盘"
        else:
            close = "跟随收盘清仓" if close_flat else "可留过收盘"
        return f"策略 {self.label}（{self.code}）｜隔夜 {self.overnight}｜清仓 {close}｜公共过滤 {self.public_filter}"


PROFILES: dict[str, EngineProfile] = {
    "daily_body": EngineProfile(
        code="daily_body",
        entry="开盘画 ETH 箱体，收盘突破箱沿才开；急破超过 ATR 限值不追",
        exit_rule="止损固定在箱体对沿，不做回撤止损；到截止钟点或收盘清仓",
        timeframe="信号周期，常用 5 / 15 分钟；日盘、夜盘各有开仓截止",
        close_default=True,
        public_filter="区间反向（ADX 过高视为已走单边，反而不开）",
        weak_market="已经走出的单边急行情、开盘跳空过大、箱体过宽",
    ),
    "trend_follow": EngineProfile(
        code="trend_follow",
        entry="收盘突破前 N 根高低点后顺势开仓",
        exit_rule="ATR 保护止损，并用更短一段的高低点收紧",
        timeframe="任意，常用 15 / 60 分钟",
        close_default=False,
        public_filter="趋势（可选，默认关）",
        weak_market="长期无趋势、波动持续压缩、假突破密集",
    ),
    "supertrend": EngineProfile(
        code="supertrend",
        entry="ATR 通道方向翻转时开仓",
        exit_rule="SuperTrend 线本身当移动止损",
        timeframe="任意，常用 15 / 60 分钟",
        close_default=False,
        public_filter="趋势（可选，默认关）",
        weak_market="震荡里通道来回翻转，连续小亏",
    ),
    "dual_ma": EngineProfile(
        code="dual_ma",
        entry="短 EMA 与长 EMA 交叉，可配置反向时反手",
        exit_rule="反向交叉平仓；反手开关打开时同时反向开仓",
        timeframe="任意，常用 15 / 60 分钟",
        close_default=True,
        public_filter="趋势（可选，默认关）",
        weak_market="横盘震荡，均线频繁交叉",
    ),
    "ema_swing": EngineProfile(
        code="ema_swing",
        entry="方向均线定多空；三条信号均线排好、收盘在多空分界线正确一侧、倾角够才开",
        exit_rule="止损跟随止损均线；可选「倾角不够就平仓」",
        timeframe="建议信号与止损都用 15 分钟，5 分钟容易被影线扫",
        close_default=False,
        public_filter="无（只用均线自身，不接公共 ADX）",
        weak_market="5 分钟级别影线密集、倾角反复",
    ),
    "bollinger": EngineProfile(
        code="bollinger",
        entry="收盘突破布林上 / 下轨才顺势开仓，不做轨道反转",
        exit_rule="跌回中轨离场，另设 ATR 止损",
        timeframe="任意，常用 15 / 60 分钟",
        close_default=False,
        public_filter="趋势（可选，默认关）",
        weak_market="通道明显缩窄时的假突破",
    ),
    "liq_stop": EngineProfile(
        code="liq_stop",
        entry="止损区被击穿并把止损单吃掉、收盘收回后才顺势进场",
        exit_rule="止损放止损区外侧 ATR；可选固定盈亏比止盈",
        timeframe="任意，常用 15 / 60 分钟",
        close_default=True,
        public_filter="无（ADX 门槛写在策略自己里）",
        weak_market="震荡市反复击穿又收回，容易二次扫损",
    ),
    "kelly_break": EngineProfile(
        code="kelly_break",
        entry="收盘突破最近 N 根高低点，影线刺出不算",
        exit_rule="ATR 止损；可选固定盈亏比止盈，否则靠止损和收盘清仓",
        timeframe="任意，常用 15 / 60 分钟",
        close_default=True,
        public_filter="无（自带波动率下限）",
        weak_market="ATR / 价格低于下限的低波动阶段",
    ),
    "sar_trend": EngineProfile(
        code="sar_trend",
        entry="SAR 翻转并符合 EMA 方向过滤时开仓",
        exit_rule="SAR 线当移动止损",
        timeframe="任意，常用 15 / 60 分钟",
        close_default=False,
        public_filter="趋势（可选，默认关）",
        weak_market="震荡里 SAR 快速来回翻转",
    ),
    "multi_factor": EngineProfile(
        code="multi_factor",
        entry="日线方向 + 四个因子加权打分过阈值后开仓",
        exit_rule="ATR 止损，只往有利方向收；弱趋势按配置减仓",
        timeframe="方向看日线，信号周期跟公共设置",
        close_default=True,
        public_filter="趋势（ADX / ATR / 因子分歧）",
        weak_market="因子互相打架、ADX 20～25 的弱趋势",
    ),
    "swing_trend": EngineProfile(
        code="swing_trend",
        entry="大周期定方向，小周期回调进场；浮盈后可加仓、冲高减仓",
        exit_rule="止损跟止损周期；离小周期均线过远时先减仓",
        timeframe="大周期日线或 60 分钟，回调跟信号周期",
        close_default=False,
        public_filter="趋势（可选，默认关）",
        weak_market="大周期没有趋势时加减仓失去意义",
    ),
    "swing_box": EngineProfile(
        code="swing_box",
        entry="按信号周期画震荡箱，收盘走出箱体才顺势开",
        exit_rule="假突破收回箱体就走；浮盈够后按 ATR 跟踪",
        timeframe="信号周期（日线约两个月，60 分钟约两周，按 40 根算）",
        close_default=False,
        public_filter="无（ADX 门槛写在策略自己里）",
        weak_market="ADX 无方向、箱子高度不足",
    ),
    "dual_donchian": EngineProfile(
        code="dual_donchian",
        entry="大周期通道 + ADX 只回答允许多还是空，小周期唐奇安突破开仓",
        exit_rule="更短通道离场，另设 ATR 止损",
        timeframe="大周期与信号周期分开设置",
        close_default=False,
        public_filter="无（大周期 ADX 写在策略自己里）",
        weak_market="大周期 ADX 低于 22 的震荡段",
    ),
    "dual_fractal": EngineProfile(
        code="dual_fractal",
        entry="大周期定方向，小周期出现强分型且 DIF 不创新低 / 新高才开",
        exit_rule="ATR 止损",
        timeframe="大周期与信号周期分开设置",
        close_default=False,
        public_filter="无（大周期 ADX 写在策略自己里）",
        weak_market="大周期 ADX 低于 22、或长期没有强分型",
    ),
    "rule_dsl": EngineProfile(
        code="rule_dsl",
        entry="由用户规则文件定义开仓条件",
        exit_rule="规则文件里的止损 / 移动止损 / 保本 / 限持仓根数",
        timeframe="规则文件定义",
        close_default=True,
        public_filter="无（过滤条件写在规则里）",
        weak_market="规则未覆盖的行情结构，需人工复核",
    ),
    "option_long": EngineProfile(
        code="option_long",
        entry="期权权利金收盘突破前 N 根高点才买入（只做买方）",
        exit_rule="权利金回撤比例止损；盈利目标止盈；最后交易日前强制平仓",
        timeframe="权利金 K 线周期，末日期权常用 1 / 5 分钟",
        close_default=True,
        public_filter="无（期权买方只做权利金突破）",
        weak_market="权利金横盘、流动性差、临近到期时间价值快速衰减",
    ),
    "option_hedge": EngineProfile(
        code="option_hedge",
        entry="权利金回落到近期区间偏低位置时买入虚值保护（只做买方）",
        exit_rule="权利金回撤止损；不设止盈；最后交易日前强制平仓",
        timeframe="权利金 K 线周期，保护单常用 15 / 60 分钟",
        close_default=False,
        public_filter="无（只看权利金自身区间）",
        weak_market="权利金长期偏高、隐含波动率处于高位时买入不划算",
    ),
}


def profile_of(engine: str | None) -> EngineProfile | None:
    """按引擎名或别名取规则卡。认不出返回 None，调用方不要假装知道。"""
    code = str(engine or "").strip()
    if not code:
        return None
    code = ENGINE_ALIASES.get(code, code)
    return PROFILES.get(code)


def header_line(engine: str | None, cfg: dict[str, Any] | None = None) -> str:
    """给评估 / 复盘抬头用。配置里显式写过 flatten_at_close 时以它为准。"""
    profile = profile_of(engine)
    if profile is None:
        return ""
    close_flat: bool | None = None
    strategy = (cfg or {}).get("strategy") if isinstance(cfg, dict) else None
    if isinstance(strategy, dict) and "flatten_at_close" in strategy:
        value = strategy.get("flatten_at_close")
        if isinstance(value, bool):
            close_flat = value
        elif isinstance(value, str):
            close_flat = value.strip().lower() in {"1", "true", "yes", "on", "是"}
    return profile.header(close_flat)


def profile_text(engine: str | None) -> str:
    """规则卡正文，控制台 / 导出用。认不出时明确说认不出。"""
    profile = profile_of(engine)
    if profile is None:
        return f"策略档案：没有登记 {engine or '（空）'}，先在 factory.ENGINE_LABELS 里补中文名。"
    return "\n".join(
        [
            profile.header(),
            f"  开仓  {profile.entry}",
            f"  离场  {profile.exit_rule}",
            f"  周期  {profile.timeframe}",
            f"  慎用  {profile.weak_market}",
        ]
    )
