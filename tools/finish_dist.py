"""
打包收尾：在 dist/Auto-Trading 旁补齐可写目录与使用说明。
功能：把示例配置、策略说明、备查名单、VERSION 拷到 exe 旁；建好 data/reviews。
原理：PyInstaller 资源在 _internal（只读）；用户改配置、写复盘要在 exe 同级目录。
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "Auto-Trading"


def _readme_txt(version: str) -> str:
    return f"""Auto-Trading 独立运行包
========================
版本：{version}

一、怎么用
1. 把整个「Auto-Trading」文件夹拷到目标电脑（不要只拷 exe）
2. 双击 Auto-Trading.exe 打开图形界面
3. 首次运行会自动生成 config\\config.yaml，请在「账号」页填号码密码和通道，在「交易配置」改策略品种并保存

二、重要说明
- 不需要安装 Python
- 配置、日志、回测/复盘数据都在本文件夹内（config / logs / data）
- 点「启动常驻」会按 config\\config.yaml 里的 app.path 拉起同花顺（需本机已安装期货通）
- 路径请填：...\\bin\\hexinlauncher.exe（不要填错成其它 exe）
- 同花顺点击下单仍需本机安装「期货通」
- 收盘后关机、防休眠等选项在「配置」页
- 「复盘」页含成绩单、信号质量、缺陷诊断；基本面补充框下有按品种推荐的手动来源
- 运行页「测试工具」含：试跑一轮信号、点击下单自检、完整自检（三项分选）
- 历史回测「把本页参数写入配置页」会写回策略、品种、周期、开关和开仓限制（不改实盘余额）
- 当前策略：日内实体、趋势跟随、SuperTrend、双均线、长线趋势加减仓、长线箱体突破、多因子、自定义规则（DSL）
- 策略插件：把导出 SPEC（code/label/factory/version）的 .py 放进本文件夹的 plugins\\，重启后自动出现在策略下拉框；加载失败只跳过该插件
- 自定义规则：config\\rules\\ 下的 YAML 用指标条件组合搭策略，不写代码；改完重启生效，写错会停止开仓并写告警
- 日内实体怎么挂机：看 docs\\日内实体操作说明书.md
- 日期用日历选择，查看/删除本地K线用时分下拉；同花顺图只给回测，不写进实盘来源
- 已删除的策略（如均线Pin动量、缺口C、SAR、MACD 等）不会再出现在程序里；旧配置会回退到日内实体
- 开平仓规则以当前版本说明书为准，换版本后请重新回测
- 交叉测试 / 回测 / 优化导出含盈利覆盖、每笔收益、日均等列
- 交易密码、仿真密码、认证码、天勤密码请到界面「账号」页填写（发布包故意不带密钥文件）
- config\\品种与合约.xlsx 是保证金比例和每手数量的来源，请勿删除；比例填 23 表示 23%，占用随开仓价变
- config\\多因子复合CTA回测评估与因子衰减监控模板.xlsx 是 CTA 因子衰减导出的表头模板，请勿删除
- config\\主力合约表.xlsx 是回测换月的依据：回测页「更新主力合约表」手动重算，「打开表格」可在「手动修正」页覆盖任意换月段，更新时该页保留
- config\\优化参数.xlsx 是策略优化的网格档位和中文名：优化页「打开优化参数表」直接改，保存后下次优化生效

三、目录
  Auto-Trading.exe     主程序
  VERSION              版本号（与窗口左上角一致）
  config\\              配置（可改）；含品种与合约.xlsx（保证金/乘数）、优化参数.xlsx、全品种备查.txt 与 rules\\（自定义规则）
  data\\                状态、回测、复盘（reviews 子目录）
  logs\\                运行日志
  strategies\\          策略说明
  plugins\\             外置策略插件（放 .py，重启生效）
  _internal\\           程序依赖（勿删）

四、命令行（可选）
  Auto-Trading.exe --once          只扫描一次
  Auto-Trading.exe --gui           打开界面

五、若无法启动
- 被杀毒软件拦截时，请加入信任/白名单
- 可用「打包成EXE.bat」重新打包；调试时可把 spec 里 console=True
"""


def main() -> int:
    if not DIST.exists():
        print(f"未找到 {DIST}，请先成功执行 PyInstaller")
        return 1

    # 发布必需文件缺失时直接失败：宁可打不出包，也不要发出一个点到该功能才报错的版本。
    required = (
        "VERSION",
        "config/config.example.yaml",
        "config/品种与合约.xlsx",
        "config/holidays.yaml",
        "config/多因子复合CTA回测评估与因子衰减监控模板.xlsx",
        "config/rules",
        "strategies",
        "src/account/schema.sql",
    )
    missing = [rel for rel in required if not (ROOT / rel).exists()]
    if missing:
        print("[失败] 源码树缺少发布必需文件：")
        for rel in missing:
            print(f"  - {rel}")
        return 1

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"

    for name in ("config", "data", "data/reviews", "logs", "strategies", "plugins"):
        (DIST / name).mkdir(parents=True, exist_ok=True)

    # 版本号放到 exe 旁，方便一眼看到；程序读的是 _internal 里那份。
    shutil.copy2(ROOT / "VERSION", DIST / "VERSION")

    # 确保示例配置在 exe 旁（Analysis 已打进 _internal，这里再放一份方便用户看见）
    example_src = ROOT / "config" / "config.example.yaml"
    example_dst = DIST / "config" / "config.example.yaml"
    if example_src.exists():
        shutil.copy2(example_src, example_dst)

    beicha_src = ROOT / "config" / "全品种备查.txt"
    if beicha_src.exists():
        shutil.copy2(beicha_src, DIST / "config" / "全品种备查.txt")

    # 保证金/乘数只认这张总表。不带它，程序会退回配置里的旧乘数，风险手数会算错。
    universe_src = ROOT / "config" / "品种与合约.xlsx"
    if universe_src.exists():
        shutil.copy2(universe_src, DIST / "config" / "品种与合约.xlsx")
    else:
        print(f"[警告] 未找到 {universe_src}，发布包将缺少保证金/乘数总表")

    # 优化参数表：策略优化的网格档位和中文名。用户不装 IDE 也能直接改。
    param_src = ROOT / "config" / "优化参数.xlsx"
    if param_src.exists():
        shutil.copy2(param_src, DIST / "config" / "优化参数.xlsx")
    else:
        print(f"[警告] 未找到 {param_src}，首次运行会按内置默认生成")

    holidays_src = ROOT / "config" / "holidays.yaml"
    if holidays_src.exists():
        shutil.copy2(holidays_src, DIST / "config" / "holidays.yaml")
    else:
        print("[警告] 未找到 config/holidays.yaml，实盘可能因节假日表不够而禁开仓")

    # CTA 因子衰减导出模板：导出函数按 exe 旁的 config 查找，漏带会直接导不出。
    decay_src = ROOT / "config" / "多因子复合CTA回测评估与因子衰减监控模板.xlsx"
    if decay_src.exists():
        shutil.copy2(decay_src, DIST / "config" / decay_src.name)
    else:
        print(f"[警告] 未找到 {decay_src}，CTA 衰减导出会失败")

    # 自定义规则模板：不写代码加策略的入口，用户复制一份改成自己的。
    rules_src = ROOT / "config" / "rules"
    if rules_src.is_dir():
        shutil.copytree(rules_src, DIST / "config" / "rules", dirs_exist_ok=True)

    # 主力合约表：回测按它的「主力时段」换月；放 exe 旁的 config，用户可改、界面可更新。
    main_tables = sorted((ROOT / "config").glob("*主力合约表*.xlsx"))
    for table in main_tables:
        shutil.copy2(table, DIST / "config" / table.name)
    if not main_tables:
        print("[警告] 未找到 config/主力合约表.xlsx，回测无法按主力时段换月（界面可点「更新主力合约表」生成）")

    # 说明书：根目录 README / CHANGELOG 拷一份到 exe 旁，便于离线查阅
    for name in ("README.md", "CHANGELOG.md"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, DIST / name)
    docs_dst = DIST / "docs"
    docs_dst.mkdir(parents=True, exist_ok=True)
    for doc_name in (
        "日内实体操作说明书.md",
        "均线波段.md",
        "配置说明书.md",
        "实盘交易简易说明书.md",
        "CTP上线流程.md",
        "华泰程序化外接测试操作说明.md",
        "系统蓝图.md",
        "策略编辑器使用说明.md",
    ):
        candidates = (
            ROOT / "docs" / doc_name,
            ROOT / "docs" / "archive" / doc_name,
        )
        doc_src = next((p for p in candidates if p.exists()), None)
        if doc_src is not None:
            shutil.copy2(doc_src, docs_dst / doc_name)

    cfg_dst = DIST / "config" / "config.yaml"
    local_cfg = ROOT / "config" / "config.yaml"
    # 优先带上本机已调好的配置（含同花顺路径），方便 exe 开箱能拉起软件
    if local_cfg.exists():
        shutil.copy2(local_cfg, cfg_dst)
        print(f"已复制本机 config.yaml → {cfg_dst}")
    elif not cfg_dst.exists() and example_dst.exists():
        shutil.copy2(example_dst, cfg_dst)

    # 标定坐标（若本机有）一起带走，换电脑仍可点同花顺；密钥文件绝不拷贝
    coords = ROOT / "config" / "gui_coords.yaml"
    if coords.exists():
        shutil.copy2(coords, DIST / "config" / "gui_coords.yaml")
        print("已复制 gui_coords.yaml")

    secrets = DIST / "config" / "secrets.local.yaml"
    if secrets.exists():
        secrets.unlink()
        print("已从发布包删除 secrets.local.yaml（避免把密码带走）")

    # 策略说明：只拷当前还在用的，已删策略的 yaml 不带进发布包
    _ACTIVE_STRATEGY_DOCS = {
        "daily_body.yaml",
        "trend_follow.yaml",
        "supertrend.yaml",
        "dual_ma.yaml",
        "ema_swing.yaml",
        "macd_swing.yaml",
        "bollinger.yaml",
        "sar_trend.yaml",
        "multi_factor.yaml",
        "swing_box.yaml",
    }
    strat_dst = DIST / "strategies"
    strat_dst.mkdir(parents=True, exist_ok=True)
    # 先清掉上次打包残留的已删说明书
    if strat_dst.exists():
        for old in strat_dst.glob("*.yaml"):
            if old.name not in _ACTIVE_STRATEGY_DOCS:
                old.unlink(missing_ok=True)
    src_strat = ROOT / "strategies"
    if src_strat.exists():
        for yaml_path in src_strat.glob("*.yaml"):
            if yaml_path.name in _ACTIVE_STRATEGY_DOCS:
                shutil.copy2(yaml_path, strat_dst / yaml_path.name)
    # 华泰指定的 CTP 动态库。没有 dll 时目录也可以是空的，不报错。
    for rel in ("ctp/api", "ctp/vendor_libs"):
        api_src = ROOT / Path(rel)
        if not api_src.is_dir():
            continue
        api_dst = DIST / rel
        api_dst.mkdir(parents=True, exist_ok=True)
        for dll in api_src.glob("*.dll"):
            shutil.copy2(dll, api_dst / dll.name)

    (DIST / "使用说明.txt").write_text(_readme_txt(version), encoding="utf-8")
    print(f"已整理: {DIST}（版本 {version}）")
    print("请分发整个 Auto-Trading 文件夹。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
