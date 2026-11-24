# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格：onedir 独立运行包（目标机无需 Python）。

依赖清单的固化原则（避免每次打包后临时补文件）：

1. ``src`` 下的模块按文件系统静态枚举，不依赖 ``collect_submodules`` 的导入结果，
   新增 .py 文件不需要改本规格；被排除的只有 ``__init__``。
2. 运行期会读取的资源（xlsx / yaml / sql / txt）逐个列在这里；缺一个就在打包阶段
   直接失败，而不是等用户点到那个功能才报错。
3. ``collect_all`` 失败不再静默跳过，失败即终止打包。
"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve()


def _src_modules() -> list[str]:
    """静态枚举 src 下所有非 __init__ 模块，避免导入失败导致漏扫。"""
    modules: list[str] = []
    for py in sorted((ROOT / "src").rglob("*.py")):
        if py.name == "__init__.py":
            continue
        modules.append(".".join(py.relative_to(ROOT).with_suffix("").parts))
    return modules


# 发布必需文件：缺任何一个都说明源码树不完整，直接失败。
_REQUIRED = {
    "VERSION": "窗口版本号，缺失会显示 0.0.0",
    "README.md": "打包版内置说明",
    "CHANGELOG.md": "打包版内置更新记录",
    "config/config.example.yaml": "首次运行生成 config.yaml 的模板",
    "config/品种与合约.xlsx": "保证金比例与每手乘数的唯一来源",
    "config/holidays.yaml": "实盘开仓的节假日表",
    "config/多因子复合CTA回测评估与因子衰减监控模板.xlsx": "CTA 因子衰减导出模板",
    "src/account/schema.sql": "SQLite 账号/成交库建表脚本",
    "strategies": "策略说明目录",
}
_missing = [f"{rel}（{why}）" for rel, why in _REQUIRED.items() if not (ROOT / rel).exists()]
if _missing:
    raise SystemExit("[spec] 缺少发布必需文件，已中止打包：\n  - " + "\n  - ".join(_missing))

datas = [
    (str(ROOT / "config" / "config.example.yaml"), "config"),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "CHANGELOG.md"), "."),
    (str(ROOT / "VERSION"), "."),
    # 账号/成交 SQLite 的建表脚本由 sqlite_migrate 按 __file__ 同目录读取，
    # PyInstaller 不会自动收集 .sql，漏打会让账号投影在打包版初始化失败。
    (str(ROOT / "src" / "account" / "schema.sql"), "src/account"),
    # 品种与合约总表是保证金/乘数的唯一来源。漏打会退回配置里的旧数字，风险手数会算错。
    (str(ROOT / "config" / "品种与合约.xlsx"), "config"),
    # CTA 因子衰减导出模板：导出函数按项目根 config 查找，用户不应自己补文件。
    (
        str(ROOT / "config" / "多因子复合CTA回测评估与因子衰减监控模板.xlsx"),
        "config",
    ),
    # 节假日表：实盘开仓要盖到未来 60 天。漏打会退回内置表或缺年份禁开仓。
    (str(ROOT / "config" / "holidays.yaml"), "config"),
]

# 优化参数表：策略优化的网格档位、中文名、取值说明。用户不装 IDE 也能改。
_param_xlsx = ROOT / "config" / "优化参数.xlsx"
if _param_xlsx.exists():
    datas.append((str(_param_xlsx), "config"))
# 交叉测试页说明书提到的备查名单；打进资源后，首次运行可拷到 exe 旁。
_beicha = ROOT / "config" / "全品种备查.txt"
if _beicha.exists():
    datas.append((str(_beicha), "config"))
# 主力合约表：回测按它的「主力时段」在换月日自动切到新主力。放进 config，用户可自己更新。
for _table in sorted((ROOT / "config").glob("*主力合约表*.xlsx")):
    datas.append((str(_table), "config"))
# 自定义规则模板：rule_dsl 引擎的出厂规则，首次运行拷到 exe 旁 config/rules 供用户复制修改。
_rules_dir = ROOT / "config" / "rules"
if _rules_dir.is_dir():
    datas.append((str(_rules_dir), "config/rules"))
# 策略说明：整目录进资源，首次运行按白名单拷到 exe 旁。
datas.append((str(ROOT / "strategies"), "strategies"))

binaries = []
hiddenimports = []

# 柜台指定的 CTP 动态库。优先 ctp/vendor_libs；旧目录 ctp/api 也一起打包。
for _lib_dir in (ROOT / "ctp" / "vendor_libs", ROOT / "ctp" / "api"):
    if not _lib_dir.is_dir():
        continue
    for dll in _lib_dir.glob("*.dll"):
        binaries.append((str(dll), f"ctp/{_lib_dir.name}"))

# 只对「带资源文件或动态库」的包做 collect_all；pandas/numpy 走官方 hook，避免打进 tests。
# uiautomation / comtypes 负责交易登录框；curl_cffi 是行情库自带的网络库；tqsdk 带行情配置与数据文件。
_collect_failures: list[str] = []
for _pkg in (
    "customtkinter",
    "akshare",
    "uiautomation",
    "comtypes",
    "curl_cffi",
    "openctp_ctp",
    "tqsdk",
    "vnpy_sqlite",
    "matplotlib",
):
    try:
        _b, _d, _h = collect_all(_pkg)
    except Exception as exc:  # noqa: BLE001 - 统一在下面汇总报错
        _collect_failures.append(f"{_pkg}: {exc}")
        continue
    binaries += _b
    datas += _d
    hiddenimports += _h

if _collect_failures:
    raise SystemExit(
        "[spec] collect_all 失败，依赖不完整，已中止打包：\n  - " + "\n  - ".join(_collect_failures)
    )

hiddenimports += _src_modules()
hiddenimports += collect_submodules("comtypes")
# src 外的脚本只有被界面按字符串动态 import 的才需要显式列出。
hiddenimports += [
    # 配置页「刷新全市场主力」会动态 import 这个文件。
    "tools.refresh_universe",
]

hiddenimports += [
    "yaml",
    "loguru",
    "schedule",
    "psutil",
    "pyautogui",
    "pygetwindow",
    "mouseinfo",
    "keyboard",
    "openpyxl",
    "PIL",
    "PIL.Image",
    "pandas",
    "numpy",
    "requests",
    "certifi",
    "charset_normalizer",
    "sqlite3",
    "uiautomation",
    "comtypes",
    "comtypes.client",
    "curl_cffi",
    "tqsdk",
    "bs4",
    "lxml",
    "html5lib",
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "matplotlib",
    "matplotlib.pyplot",
    "matplotlib.backends.backend_agg",
    "vnpy",
    "vnpy.trader.setting",
    "vnpy.trader.constant",
    "vnpy.trader.object",
    "vnpy.trader.database",
    "vnpy_sqlite",
    "vnpy_sqlite.sqlite_database",
    "peewee",
]

excludes = [
    "pytest",
    "IPython",
    "jupyter",
    "notebook",
    "pandas.tests",
    "numpy.tests",
]

a = Analysis(
    [str(ROOT / "app_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Auto-Trading",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Auto-Trading",
)
