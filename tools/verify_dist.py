"""
打包验收：确认 dist/Auto-Trading 含升级后完整运行所需的文件与模块。
功能：不启动界面，只检查版本、资源、PYZ 里的关键 src 模块。
原理：漏打 VERSION / 复盘模块 / 备查文件时，这里先报错，避免拷到别的电脑才发现。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "Auto-Trading"


def _enumerate_modules() -> list[str]:
    """自动枚举 src/ 下所有 .py 模块，避免手动维护 REQUIRED_MODULES 漏写。"""
    modules: set[str] = set()
    self_path = Path(__file__).resolve()
    for root in (ROOT / "src",):
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            # 排除 __init__.py、自身（verify_dist 不进 PYZ）
            if py.name == "__init__.py":
                continue
            if py.resolve() == self_path:
                continue
            try:
                rel = py.relative_to(ROOT).with_suffix("")
            except ValueError:
                continue
            modules.add(str(rel).replace("\\", "/").replace("/", "."))
    return sorted(modules)


# 自动从源码目录枚举，加新文件后无需手动维护
# tools/ 下是命令行脚本，只有被界面动态 import 的才需要在 PYZ 里。
REQUIRED_MODULES = _enumerate_modules() + ["tools.refresh_universe"]

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
        "config/holidays.yaml",
        "config/多因子复合CTA回测评估与因子衰减监控模板.xlsx",
        "_internal/src/account/schema.sql",
        "_internal/config/多因子复合CTA回测评估与因子衰减监控模板.xlsx",
        "config/rules/dual_ma_equiv.yaml",
        "config/rules/dualthrust.yaml",
        "config/rules/r_breaker.yaml",
        "config/rules/turtle_adx.yaml",
        "config/rules/adx_atr_break.yaml",
        "使用说明.txt",
        "strategies",
        "strategies/daily_body.yaml",
        "strategies/supertrend.yaml",
        "strategies/dual_ma.yaml",
        "strategies/ema_swing.yaml",
        "strategies/macd_swing.yaml",
        "strategies/trend_follow.yaml",
        "strategies/bollinger.yaml",
        "strategies/sar_trend.yaml",
        "strategies/multi_factor.yaml",
        "strategies/swing_box.yaml",
        "docs/均线波段.md",
        "VERSION",
        "CHANGELOG.md",
        "docs/日内实体操作说明书.md",
        "docs/配置说明书.md",
        "docs/实盘交易简易说明书.md",
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
        "strategies/jesse_casper.yaml",
        "strategies/multi_hybrid.yaml",
        "strategies/multi_vote.yaml",
        "strategies/daily_body_expand.yaml",
    ):
        if (DIST / gone).exists():
            errors.append(f"发布包里仍有已删策略说明 {gone}，请从 strategies 去掉后再打包")

    for rel in ("data", "data/reviews", "logs", "config", "plugins"):
        path = DIST / rel
        if not path.exists():
            errors.append(f"缺少目录 {rel}")

    # 不得把密钥打进发布包
    if (DIST / "config" / "secrets.local.yaml").exists():
        errors.append("发布包里出现了 secrets.local.yaml，请删除后再分发")

    dlls = list((DIST / "ctp" / "api").glob("*.dll")) + list((DIST / "ctp" / "vendor_libs").glob("*.dll"))
    if not dlls:
        errors.append("缺少华泰 CTP 动态库（ctp/api 或 ctp/vendor_libs 下的 dll）")

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
