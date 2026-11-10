# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 规格：onedir 独立运行包（无 Python 环境可用）。
"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve()

datas = [
    (str(ROOT / "config" / "config.example.yaml"), "config"),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "CHANGELOG.md"), "."),
    # 版本号只有这一份。不打进去的话，exe 窗口会显示 0.0.0。
    (str(ROOT / "VERSION"), "."),
]
# 品种与合约总表是保证金/乘数的唯一来源。漏打会退回配置里的旧数字，风险手数会算错。
universe_xlsx = ROOT / "config" / "品种与合约.xlsx"
if universe_xlsx.exists():
    datas.append((str(universe_xlsx), "config"))
# 优化参数表：策略优化的网格档位、中文名、取值说明。用户不装 IDE 也能改。
param_xlsx = ROOT / "config" / "优化参数.xlsx"
if param_xlsx.exists():
    datas.append((str(param_xlsx), "config"))
# 交叉测试页说明书提到的备查名单；打进资源后，首次运行可拷到 exe 旁。
beicha = ROOT / "config" / "全品种备查.txt"
if beicha.exists():
    datas.append((str(beicha), "config"))
# 主力合约表：回测按它的「主力时段」在换月日自动切到新主力。放进 config，用户可自己更新。
for table in sorted((ROOT / "config").glob("*主力合约表*.xlsx")):
    datas.append((str(table), "config"))
if (ROOT / "strategies").exists():
    datas.append((str(ROOT / "strategies"), "strategies"))

binaries = []
hiddenimports = []

# 华泰指定的 CTP 动态库。放到 ctp/api 后，下次打包会打进程序。
api_dir = ROOT / "ctp" / "api"
if api_dir.is_dir():
    for dll in api_dir.glob("*.dll"):
        binaries.append((str(dll), "ctp/api"))

# 只对「带资源文件或动态库」的包做 collect_all；pandas/numpy 走官方 hook，避免打进 tests。
# uiautomation / comtypes 负责交易登录框；curl_cffi 是行情库自带的网络库；tqsdk 带行情的配置与数据文件。
for pkg in ("customtkinter", "akshare", "uiautomation", "comtypes", "curl_cffi", "openctp_ctp", "tqsdk"):
    try:
        b, d, h = collect_all(pkg)
        binaries += b
        datas += d
        hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules("src")
hiddenimports += collect_submodules("comtypes")
# 复盘 / 测试工具等新模块显式列入，避免个别环境下 collect_submodules 漏扫。
hiddenimports += [
    "src.review.quality",
    "src.review.diagnose",
    "src.review.scorecard",
    "src.review.baseline",
    "src.review.gap",
    "src.review.sources",
    "src.review.service",
    "src.review.report",
    "src.review.replay",
    "src.review.fundamental",
    "src.review.actual",
    "src.review.technical",
    "src.review.session",
    "src.ui.test_tools",
    "src.ui.order_simulator",
    # 主力合约换月与主力表生成：回测页的换月开关和「更新主力合约表」按钮依赖它们
    "src.backtest.rollover",
    "src.backtest.main_table",
    "src.backtest.optimizer",
    # 优化参数 Excel 表读写：优化页「打开优化参数表」和网格加载依赖它
    "src.backtest.param_table",
    # 当前仍保留的策略（已删的 ema_pin / macd / sar 等不要再写进来）
    "src.strategy.daily_body",
    "src.strategy.dual_ma",
    "src.strategy.supertrend",
    "src.strategy.trend_follow",
    "src.strategy.swing_trend",
    "src.strategy.morning_edge",
    "src.ui.datetime_pickers",
    "src.data.bar_inspect",
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
