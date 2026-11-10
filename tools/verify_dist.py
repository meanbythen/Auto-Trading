"""
打包验收：确认 dist/Auto-Trading 含升级后完整运行所需的文件与模块。
功能：不启动界面，只检查版本、资源、PYZ 里的关键 src 模块。
原理：漏打 VERSION / 复盘模块 / 备查文件时，这里先报错，避免拷到别的电脑才发现。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "Auto-Trading"

# 复盘升级与主流程相关的模块，必须进 PYZ
REQUIRED_MODULES = [
    "src.review.quality",
    "src.review.diagnose",
    "src.review.scorecard",
    "src.review.baseline",
    "src.review.gap",
    "src.review.sources",
    "src.review.service",
    "src.backtest.optimizer",
    "src.backtest.param_table",
    "src.backtest.rollover",
    "src.backtest.main_table",
    "src.screen.picker",
    "src.account.signal_book",
    "src.export.bundle",
    "src.ui.app",
    "src.ui.test_tools",
    "src.ui.order_simulator",
    "src.strategy.daily_body",
    "src.strategy.morning_edge",
    "src.strategy.supertrend",
    "src.strategy.dual_ma",
    "src.strategy.trend_follow",
    "src.strategy.swing_trend",
    "src.strategy.market_state",
    "src.strategy.trend_base",
    "src.strategy.bollinger_break",
    "src.strategy.sar_trend",
    "src.strategy.multi_factor",
    "src.strategy.multi_factor_adapt",
    "src.strategy.adaptive_weights",
    "src.strategy.cta_factors",
    "src.strategy.cta_filter",
    "src.export.cta_decay",
    "src.export.factor_decay_plot",
    "src.data.quality",
    "src.data.inventory",
    "src.data.ths_chart",
    "src.ops.alerts",
    "src.risk.dynamic",
    "src.ops.health",
    "src.research.scan",
    "src.research.pool",
    "src.research.factors",
    # 21:00 期货交易日、保证金表等运行必需模块
    "src.utils.trading_day",
    "src.risk.margin_table",
    "src.account.signal_stats",
    "src.scheduler.runner",
    "src.trader.ctp_logic",
    "src.trader.sim_trader",
    "src.strategy.levels",
    "src.strategy.explain",
    "src.ui.config_presets",
    "src.ui.datetime_pickers",
    "src.data.bar_inspect",
    "src.data.bar_store",
]

def main() -> int:
    errors: list[str] = []
    if not DIST.exists():
        print(f"[失败] 没有 {DIST}，请先打包")
        return 1

    exe = DIST / "Auto-Trading.exe"
    if not exe.exists():
        errors.append("缺少 Auto-Trading.exe")

    expected_ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    internal_ver_path = DIST / "_internal" / "VERSION"
    if not internal_ver_path.exists():
        errors.append("缺少 _internal/VERSION（窗口会显示 0.0.0）")
    else:
        got = internal_ver_path.read_text(encoding="utf-8").strip()
        if got != expected_ver:
            errors.append(f"_internal/VERSION 是 {got}，源码是 {expected_ver}，请重新打包")

    # 可读写旁路资源
    for rel in (
        "config/config.example.yaml",
        "config/全品种备查.txt",
        "config/品种与合约.xlsx",
        "config/优化参数.xlsx",
        "使用说明.txt",
        "strategies",
        "strategies/daily_body.yaml",
        "strategies/supertrend.yaml",
        "strategies/dual_ma.yaml",
        "strategies/morning_box_a.yaml",
        "strategies/trend_follow.yaml",
        "strategies/bollinger.yaml",
        "strategies/sar_trend.yaml",
        "strategies/multi_factor.yaml",
        "strategies/multi_vote.yaml",
        "strategies/multi_hybrid.yaml",
        "strategies/jesse_casper.yaml",
        "VERSION",
        "CHANGELOG.md",
        "docs/策略迭代进化说明.md",
        "docs/日内实体操作说明书.md",
    ):
        path = DIST / rel
        if not path.exists():
            errors.append(f"缺少 {rel}")

    # 主力合约表：少了也能启动，但回测不会按主力时段换月。
    if not any(DIST.glob("config/*主力合约表*.xlsx")) and not any(DIST.glob("*主力合约表*.xlsx")):
        errors.append("缺少 config/主力合约表.xlsx（回测无法按主力时段换月）")

    # 已删策略的说明书不应再进发布包，避免用户误以为还能选
    for gone in (
        "strategies/custom.yaml",
        "strategies/ema_pin_momentum.yaml",
        "strategies/liquidity_exhaustion.yaml",
        "strategies/macd_divergence.yaml",
        "strategies/sar_flip.yaml",
        "strategies/mean_reversion.yaml",
        "strategies/morning_gap_c.yaml",
    ):
        if (DIST / gone).exists():
            errors.append(f"发布包里仍有已删策略说明 {gone}，请从 strategies 去掉后再打包")

    for rel in ("data", "data/reviews", "logs", "config"):
        path = DIST / rel
        if not path.exists():
            errors.append(f"缺少目录 {rel}")

    # 不得把密钥打进发布包
    if (DIST / "config" / "secrets.local.yaml").exists():
        errors.append("发布包里出现了 secrets.local.yaml，请删除后再分发")

    pyz = ROOT / "build" / "Auto-Trading" / "PYZ-00.toc"
    if pyz.exists():
        text = pyz.read_text(encoding="utf-8", errors="ignore")
        for name in REQUIRED_MODULES:
            if name not in text:
                errors.append(f"PYZ 未包含模块 {name}")
    else:
        errors.append("没有 build/Auto-Trading/PYZ-00.toc，无法核对模块是否打进包")

    # 内置 README 版本行应与当前版本一致（防止旧说明进包）
    readme = DIST / "_internal" / "README.md"
    if readme.exists():
        head = readme.read_text(encoding="utf-8", errors="ignore")[:200]
        if expected_ver not in head:
            errors.append(f"_internal/README.md 开头未写当前版本 {expected_ver}")

    if errors:
        print("[失败] 打包验收未通过：")
        for item in errors:
            print(f"  - {item}")
        return 1

    print(f"[通过] dist 验收 OK | 版本 {expected_ver} | 关键模块 {len(REQUIRED_MODULES)} 个已在 PYZ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
