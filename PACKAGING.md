# EXE 打包方法（PyInstaller）

本文档描述 `Auto-Trading` 的实际打包方式。目标：**打包一次固化依赖清单，不再出现「打完发现缺文件、临时补一行」的循环。**

- 入口：`app_entry.py`
- 规格：`Auto-Trading.spec`（纳入版本控制）
- 形态：**onedir**（`dist/Auto-Trading/`，整目录分发；不是 onefile）
- 环境：Windows + Python 3.12 + PyInstaller 6.22.3（`requirements-build.txt` 固定）
- 一键构建：`python tools/build_exe.py`（`打包成EXE.bat` 同一条路径）

## 一、以前为什么会「临时补依赖」

不是 PyInstaller 不稳定，而是依赖被**隐式**收集，漏了也不报错。本项目已定位并修掉 5 个具体原因：

| # | 现象 | 根因 | 现在的做法 |
|---|---|---|---|
| 1 | 打包版账号页/成交库初始化报错 | `src/account/sqlite_migrate.py` 按 `__file__` 同目录读 `schema.sql`，PyInstaller 不收 `.sql` | spec 增加 `datas: src/account/schema.sql`，verify 检查 `_internal/src/account/schema.sql` |
| 2 | CTA 因子衰减导出失败 | `config/多因子复合CTA回测评估与因子衰减监控模板.xlsx` 未被收集 | spec 收集 + finish 拷到 exe 旁 + verify 双处检查 |
| 3 | 某个第三方包缺数据/子模块，运行到才报错 | `collect_all(...)` 外层 `except: pass` 静默跳过 | 失败汇总后 `SystemExit`，打包阶段直接失败 |
| 4 | 新增 `src` 模块后 PyZ 里没有 | `collect_submodules("src")` 需要导入模块，导入失败会静默漏掉，只能手工补 hiddenimports | spec **按文件系统静态枚举** `src/**/*.py`，新增文件零维护 |
| 5 | 改了 spec 产物没变 / 旧文件残留 | 不删 `dist`，且 bat 每次 `pip install -U` 最新 PyInstaller | 构建脚本先删 `build/Auto-Trading`、`dist/Auto-Trading`；版本固定 6.22.3 |

## 二、打包链路

```
app_entry.py
  └─ PyInstaller 6.22.3（Auto-Trading.spec，--clean）
       ├─ Analysis：static 枚举 src 模块 + hiddenimports + collect_all
       ├─ PYZ：Python 模块
       └─ COLLECT → dist/Auto-Trading/
            ├─ Auto-Trading.exe
            ├─ _internal/            ← 只读资源（datas / 依赖 / VERSION）
            ├─ config/               ← finish 拷出的可写配置与模板
            ├─ data/ logs/           ← 首次运行创建的运行数据
            ├─ plugins/              ← 外置策略插件（用户可写 .py）
            └─ strategies/ docs/     ← 策略说明与内置文档
```

`tools/build_exe.py` 的完整顺序：

```
预检（Python/PyInstaller 版本、必需文件、compileall）
  → 清理（只删 build/Auto-Trading、dist/Auto-Trading）
  → PyInstaller（.spec，--noconfirm --clean）
  → tools/finish_dist.py（补可写目录、拷配置/模板/文档、删密钥）
  → tools/verify_dist.py（版本、资源、PYZ 模块、已删策略残留）
  → build/packaging/build-info.json（版本/大小/SHA256/时间）
  → 12 秒启动冒烟
```

`build/testkit/` 里的覆盖率与基线产物不在清理范围内，不会被误删。

## 三、依赖固化的三张清单

### 1. 运行时读取的资源（必须进 `datas`）

| 文件 | 用途 | 缺失后果 |
|---|---|---|
| `VERSION` | 窗口与关于页版本 | 显示 `0.0.0` |
| `README.md` / `CHANGELOG.md` | 内置说明 | 离线查阅缺内容 |
| `config/config.example.yaml` | 首次运行生成 `config.yaml` | 首次启动无配置 |
| `config/品种与合约.xlsx` | 保证金比例、每手乘数 | 风险手数算错 |
| `config/优化参数.xlsx` | 优化网格档位与中文名 | 优化页参数表为空 |
| `config/全品种备查.txt` | 交叉测试备查名单 | 备查页缺数据 |
| `config/*主力合约表*.xlsx` | 回测按主力时段换月 | 不换月 |
| `config/holidays.yaml` | 实盘开仓节假日表 | 可能整年禁开仓 |
| `config/多因子复合CTA回测评估与因子衰减监控模板.xlsx` | CTA 衰减导出表头 | 导出失败 |
| `config/rules/` | 自定义规则出厂模板 | 规则引擎无模板 |
| `strategies/` | 策略说明 | 策略页说明缺失 |
| `src/account/schema.sql` | SQLite 建表 | 账号/成交库初始化失败 |

`spec` 对其中带「缺失后果」的必需项做 fail-fast：文件不存在直接中止打包。

### 2. 动态导入模块

| 模块 | 为什么动态 | 处理 |
|---|---|---|
| `tools.refresh_universe` | 配置页按字符串 import | 显式写进 `hiddenimports` |
| `src/**/*.py` | 各页按需加载，导入失败会被 `collect_submodules` 静默跳过 | spec 静态枚举文件，不再依赖导入 |

> 策略插件在打包版从 **exe 同级的 `plugins/`** 加载：PyInstaller 只保留字节码，`src/strategy/plugins/` 内没有 `.py`；`plugins/` 是用户可写目录，放一个导出 `SPEC`（`code`/`label`/`factory`/`version`）的 `.py`，重启即生效。加载失败只记 warning 并跳过，不影响内置策略。

### 3. 二进制与第三方数据

| 来源 | 处理 |
|---|---|
| 华泰 CTP 动态库 `ctp/vendor_libs`、`ctp/api` | 逐 dll 写入 `binaries`，落到 `ctp/<目录>` |
| `customtkinter`、`akshare`、`uiautomation`、`comtypes`、`curl_cffi`、`openctp_ctp`、`tqsdk`、`vnpy_sqlite`、`matplotlib` | `collect_all` 一次收全；任一失败即中止 |
| `pandas` / `numpy` | 官方 hook 足够，不用 `collect_all`，避免把 tests 打进来 |

## 四、spec 关键段落

```python
# ① src 模块静态枚举：新增 .py 不用改 spec
def _src_modules() -> list[str]:
    return [
        ".".join(py.relative_to(ROOT).with_suffix("").parts)
        for py in sorted((ROOT / "src").rglob("*.py"))
        if py.name != "__init__.py"
    ]

# ② 必需资源缺失 → 打包阶段失败
_missing = [rel for rel in _REQUIRED if not (ROOT / rel).exists()]
if _missing:
    raise SystemExit("[spec] 缺少发布必需文件：\n  - " + "\n  - ".join(_missing))

# ③ 运行期读取的 .sql 必须显式收集
datas = [(str(ROOT / "src" / "account" / "schema.sql"), "src/account"), ...]

# ④ collect_all 失败不再静默
if _collect_failures:
    raise SystemExit("[spec] collect_all 失败：\n  - " + "\n  - ".join(_collect_failures))
```

## 五、路径规范（只读与可写分离）

集中实现在 `src/utils/paths.py`，其它模块禁止自行拼路径：

| 用途 | 路径 | 说明 |
|---|---|---|
| 程序**读**的随包资源 | `resource_root()` = `sys._MEIPASS` | 打包后是 `_internal/`，只读、临时 |
| 程序**写**的数据 | `project_root()` = exe 所在目录 | 配置、日志、SQLite、回测/复盘 |
| 首次运行补配置 | `ensure_runtime_layout()` | 从 `_MEIPASS` 拷 `config.example.yaml`、xlsx、rules、strategies、VERSION 等到 exe 旁，不覆盖用户文件 |

判断只看一条：**会不会被写**。会写就必须落在 exe 同目录，不能放 `datas`。

## 六、构建

### 推荐：一条命令

```powershell
.venv\Scripts\python.exe tools\build_exe.py
```

或双击 `打包成EXE.bat`（会先按 `requirements-build.txt` 安装固定版本）。

可选参数：

| 参数 | 用途 |
|---|---|
| `--no-smoke` | 跳过启动冒烟（本机已有实例在跑时用） |
| `--no-clean` | 保留上次 Analysis 缓存，仅排错 |
| `--skip-compile` | 跳过 `compileall` |

### 手工等价步骤（脚本不可用时）

```powershell
# 1. 源码自检
.venv\Scripts\python.exe -m compileall -q src tools app_entry.py

# 2. 删旧产物（不要删整个 build，build/testkit 里有覆盖率产物）
Remove-Item -Recurse -Force build\Auto-Trading, dist\Auto-Trading -ErrorAction SilentlyContinue

# 3. 打包（必须走 spec，不要命令行堆 --add-data）
.venv\Scripts\python.exe -m PyInstaller Auto-Trading.spec --noconfirm --clean

# 4. 收尾 + 验收
.venv\Scripts\python.exe tools\finish_dist.py
.venv\Scripts\python.exe tools\verify_dist.py
```

## 七、产物与元数据

- 分发物是 **整个 `dist/Auto-Trading/` 文件夹**，不是单个 exe（onedir 形态，删 `_internal` 必崩）。
- `build/packaging/build-info.json` 记录：

```json
{
  "version": "53.7.x",
  "built_at": "2026-11-24 15:00:00",
  "exe_size_mb": 3.2,
  "dist_size_mb": 420.5,
  "sha256": "……",
  "pyinstaller": "6.22.3",
  "layout": "onedir"
}
```

发版前核对 `version`、`built_at`、`sha256`，确认打的是最新代码，而不是旧产物。

## 八、验收

### 自动（构建脚本已包含）

| 层 | 检查 |
|---|---|
| spec | 必需资源存在；`collect_all` 全成功 |
| finish | 必需文件齐全；可写目录创建；`secrets.local.yaml` 删除；已删策略说明清理 |
| verify | `_internal/VERSION` 与源码一致；资源（含 `schema.sql`、CTA 模板）在包内；PYZ 含全部 `src` 模块；CTP dll 存在；无密钥文件 |
| smoke | exe 启动后存活 12 秒 |

### 人工（换一台没装 Python 的机器）

- [ ] 双击能开界面，无缺 DLL/缺模块弹窗
- [ ] 「账号」页能初始化 SQLite（验证 `schema.sql`）
- [ ] CTA 因子衰减能导出 Excel（验证模板）
- [ ] 首次运行在 exe 旁生成 `config/config.yaml`、`data/`、`logs/`
- [ ] 「更新主力合约表」「打开优化参数表」可用
- [ ] 自定义规则页能列出 `config/rules/*.yaml`
- [ ] 往 `plugins\` 放一个插件 `.py`，重启后策略下拉框出现该策略
- [ ] 关闭再开，配置与数据仍在
- [ ] 杀软未拦截；如拦截，加信任

## 九、故障对照表

| 现象 | 根因 | 对策 |
|---|---|---|
| 窗口显示版本 `0.0.0` | `_internal/VERSION` 缺失或与源码不一致 | 重新打包；看 `verify_dist` 报错 |
| 账号/成交库初始化失败 | 漏打 `src/account/schema.sql` | 确认 spec 有该 datas；verify 会拦 |
| CTA 衰减导出找不到模板 | 漏打 CTA 模板 | spec + finish + verify 三处已覆盖 |
| 打包版策略插件列表为空 | 插件没放在 exe 同级的 `plugins/`，或加载失败 | 看 `logs` 里的 warning；插件只能 import 已打进包的依赖 |
| 启动即退出，无提示 | `console=False` 吞了 traceback；或已有实例占用单实例锁 | 先确认没有在跑的实例；临时 `console=True` 重打 |
| `ModuleNotFoundError: src.xxx` | 新模块没进 PYZ | 静态枚举已覆盖；若仍在 `src` 外，补 `hiddenimports` |
| 改了 spec 产物没变 | Analysis 缓存 | `--clean` + 删 `build/Auto-Trading`（脚本默认做） |
| 分发后别人机器缺文件 | 只拷了 exe | 必须拷整个 `dist/Auto-Trading/` |
| 配置里出现本机绝对路径 | finish 复制了本机 `config.yaml` | 对外发布可先删 `dist/Auto-Trading/config/config.yaml`，让首次运行生成 |

## 十、维护约定

1. **新增 `src` 模块**：什么都不用改（spec 静态枚举）。
2. **新增运行期资源**：四处同步——`Auto-Trading.spec` 的 `datas`、`src/utils/config_loader.ensure_runtime_layout()` 的拷贝清单、`tools/finish_dist.py`、`tools/verify_dist.py`。
3. **新增按字符串动态 import 的模块**（`src` 之外）：加进 spec 的 `hiddenimports`。
4. **新增可写目录**：加到 `ensure_runtime_layout()` 和 `finish_dist.py`，不要放 `datas`。
5. **升级 PyInstaller**：同时改 `requirements-build.txt`、`tools/build_exe.py` 的 `PINNED_PYINSTALLER`、CI 缓存依赖路径说明，并完整打一次包验证。
6. **发版流程**：改代码 → 升 `VERSION` 并同步文档 → `tools/testkit.py gate` → `tools/build_exe.py` → 核对 `build-info.json` → 人工清单。

## 十一、与参考项目（Auto-Quotation）的差异

| 维度 | Auto-Quotation | 本项目 |
|---|---|---|
| 形态 | onefile 单 exe | onedir 整目录（CTP dll、tqsdk 资源、启动速度） |
| 浏览器 | 运行时探测本机 Chrome/Edge | 无浏览器依赖；同花顺/期货通由本机安装 |
| 动态库 | playwright 自带 driver | 华泰 CTP dll 逐目录收集 |
| 配置 | 打包内置只读资源 + exe/data | 首次运行从 `_internal` 复制到 exe 旁，用户可改 |
| 构建 | spec + 手工命令 | spec + `tools/build_exe.py` 一条命令（含验收与冒烟） |

## 十二、已知限制

- 打包版插件只能使用已打进包的依赖；插件 import 未打包的库会加载失败并被跳过。
- 插件是外部可执行代码，来源需自行确认可信。
- 本地 `config.yaml` 会被收进发布包（含本机同花顺路径）；对外分发前请按需删除。
- 未做代码签名，部分杀软可能拦截；需要时加白名单或采购签名证书。
- onedir 首次启动较快，但目录文件多，拷贝/升级时请整目录替换。
