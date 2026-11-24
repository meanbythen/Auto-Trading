# testkit 使用说明

`tools/testkit.py` 是本项目的测试编排入口，把「用例数量、单包覆盖率缺口、全量门禁」三件事收敛成一条命令。本地开发和 CI 用同一入口，避免命令各写一份、口径慢慢走偏。

源码：[tools/testkit.py](../tools/testkit.py)

## 1. 前置条件

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows（项目依赖同花顺窗口操作、keyboard、uiautomation，CI 也是 windows-latest） |
| Python | 3.12，使用项目虚拟环境 `.venv` |
| 依赖 | 已安装 `requirements-dev.txt`（pytest、coverage、ruff、mypy、import-linter） |

不需要额外安装 testkit 本身，直接用解释器运行：

```powershell
.venv\Scripts\python.exe tools\testkit.py --help
```

## 2. 三个子命令

### 2.1 baseline：用例规模

```powershell
.venv\Scripts\python.exe tools\testkit.py baseline
```

输出示例：

```
选中用例：2420 项
收集总数：2425 项（取消选中 5 项）
已写入 build/testkit/baseline.json
```

口径说明：

- 「选中」= 实际参与回归的用例数，等于 pyproject 默认的 `-m "not perf and not soak"`。
- 「收集总数」包含被 marker 排除的 perf/soak 用例。
- 文档、报告里写「N 项回归」时用「选中」这一项。

参数：无。退出码：收集失败为 `1`。

产物 `build/testkit/baseline.json`：

```json
{ "selected": 2420, "total": 2425, "deselected": 5, "seconds": 12.3 }
```

### 2.2 cov：单包覆盖率与缺口

```powershell
.venv\Scripts\python.exe tools\testkit.py cov src/scheduler --target 80
```

输出示例：

```
文件                                                  语句    未覆盖      语句覆盖      合并覆盖
src\scheduler\runner.py                           1665    463     72.2%       69%
src\scheduler\order_executor.py                    661     57     91.4%       89%
...
合计：语句 3576，未覆盖 620，语句覆盖 82.66%，合并覆盖 80%（目标 80%）
[PASS] 语句覆盖率达标
已写入 build/testkit/cov-src_scheduler.json
```

行为说明：

- 内部执行 `coverage run --branch --source=<包> -m pytest -q`，跑的是**全量用例**，但只统计指定包。
- 「语句覆盖」= `(语句 - 未覆盖) / 语句`，与测试报告的 5.1 表口径一致；「合并覆盖」是 coverage 输出的语句+分支合并值。
- 目标按**语句覆盖率**判断，未达标时退出码为 `1`，便于脚本/CI 直接拦截。
- 覆盖率数据写到 `build/testkit/.coverage-<包名>`，不覆盖仓库根目录的 `.coverage`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `source` | 必填 | 传给 `coverage --source`，如 `src/scheduler`、`src/account` |
| `--target` | `80` | 语句覆盖率目标（百分数） |
| `--no-branch` | 关 | 只统计语句，不统计分支；缺省开启分支统计 |

产物 `build/testkit/cov-<包>.json`（节选）：

```json
{
  "source": "src/scheduler",
  "branch": true,
  "target": 80.0,
  "total": { "stmt": 3576, "miss": 620, "cover": 80, "statement_cover": 82.66 },
  "files": [
    { "path": "src\\scheduler\\runner.py", "stmt": 1665, "miss": 463, "statement_cover": 72.2, "missing": "..." }
  ]
}
```

### 2.3 gate：全量门禁

```powershell
.venv\Scripts\python.exe tools\testkit.py gate
```

依次执行：

| 顺序 | 步骤 | 实际命令 |
|---|---|---|
| 1 | pytest | `python -m pytest -q -p no:warnings` |
| 2 | ruff | `python -m ruff check .` |
| 3 | mypy | `python -m mypy` |
| 4 | lint-imports | `python -m importlinter.cli lint-imports` |
| 5 | docs | `python tools/check_doc_versions.py` |

加 `--coverage` 时，第 1 步改为 `coverage run --branch -m pytest`，并在其后追加 `coverage report --show-missing`，用 pyproject 的 `fail_under = 56` 校验全局覆盖率。CI 用的就是这个模式：

```powershell
.venv\Scripts\python.exe tools\testkit.py gate --coverage
```

只跑其中几步：

```powershell
.venv\Scripts\python.exe tools\testkit.py gate --only ruff,mypy,docs
```

可选值：`pytest`、`coverage`、`ruff`、`mypy`、`lint-imports`、`docs`。未知步骤返回退出码 `2`。

输出示例：

```
==> ruff
[PASS] ruff（0.1s）
...
门禁汇总：
  [PASS] pytest        243.3s
  [PASS] coverage        3.2s
  [PASS] ruff            0.1s
  [PASS] mypy            0.2s
  [PASS] lint-imports    0.1s
  [PASS] docs            0.1s
[PASS] 全部门禁通过
```

失败时会打印该步骤输出的最后 30 行，然后继续跑后续步骤，最后统一汇总。产物 `build/testkit/gate.json` 记录每步的退出码、耗时和输出末尾，便于事后排查或 CI 归档。

## 3. 退出码约定

| 退出码 | 含义 |
|---|---|
| `0` | 通过 |
| `1` | `cov` 未达目标 / `gate` 有步骤失败 / `baseline` 收集失败 |
| `2` | 参数错误（如 `--only` 写了不存在的步骤） |
| `124` | 子进程超时（单步骤上限 1800 秒） |

## 4. 典型用法

### 4.1 提交前

```powershell
.venv\Scripts\python.exe tools\testkit.py gate
```

### 4.2 按包补测

```powershell
# 1. 看缺口
.venv\Scripts\python.exe tools\testkit.py cov src/account --target 80

# 2. 补测试

# 3. 复查，达标后跑门禁
.venv\Scripts\python.exe tools\testkit.py cov src/account --target 80
.venv\Scripts\python.exe tools\testkit.py gate
```

### 4.3 只改文档时

```powershell
.venv\Scripts\python.exe tools\testkit.py gate --only docs
```

### 4.4 CI

`.github/workflows/tests.yml` 中：

```yaml
- name: Dependency audit
  run: python -m pip_audit -r requirements.lock

- name: Test gate
  run: python tools/testkit.py gate --coverage
```

## 5. 与原始命令的对应关系

| testkit | 等价原始命令 |
|---|---|
| `baseline` | `pytest --collect-only -o addopts= -m "not perf and not soak"` |
| `cov src/x` | `coverage run --branch --source=src/x -m pytest -q` + `coverage report --show-missing` |
| `gate` | `pytest -q`、`ruff check .`、`mypy`、`lint-imports`、`check_doc_versions.py` |
| `gate --coverage` | `coverage run --branch -m pytest -q` + `coverage report --show-missing` + 其余四项 |

## 6. 常见问题

**Q：为什么不用 `pytest --cov`？**
本机 pytest-cov / coverage / numpy 组合会在收集阶段报 `numpy: cannot load module more than once per process`。testkit 固定用 `coverage run` 采集，CI 与本地一致。

**Q：跑 `cov` 为什么要执行全量用例，能只跑某个文件吗？**
`--source` 只控制统计范围，不控制执行范围；目前不支持 `-k` 过滤。需要更快迭代时，先手工 `pytest tests/test_xxx.py -q` 调试，再用 `cov` 取正式数字。后续可加 `--pytest-args`。

**Q：`gate --coverage` 会不会覆盖本地 `.coverage`？**
不会。覆盖率文件固定在 `build/testkit/.coverage-gate`，`cov` 用 `.coverage-<包名>`，互不干扰。

**Q：`docs` 步骤失败了怎么办？**
按输出提示改文档。常见原因是新增/删除了 `tests/test_*.py`（测试文件数变化）、改了 `src`（文件数/行数变化）、或 `VERSION` 与 README/CHANGELOG/TODO/架构芯片不一致。

**Q：为什么没有 pip-audit？**
依赖审计需要联网且较慢，仍留在 CI 单独执行，不放进本地高频门禁。

**Q：Windows 控制台中文乱码？**
testkit 以 UTF-8 捕获子进程输出；若终端本身是旧代码页，先执行 `chcp 65001`。

## 7. 扩展方式

- 新增子命令：在 `build_parser()` 注册，并实现 `cmd_xxx(args) -> int`，返回退出码。
- 新增门禁步骤：在 `gate_steps()` 中按顺序追加 `(名称, 命令列表)`，`--only` 会自动识别。
- 新增解析逻辑：保持纯函数（如 `parse_baseline`、`parse_coverage_rows`），在 `tests/test_testkit.py` 里补单元测试。

## 8. 已知限制

- 仅支持 Windows；没有做跨平台路径/编码抽象。
- 单步骤超时固定 1800 秒，尚无命令行参数覆盖。
- 不替代人工演练：CTP 四步上线、GUI 点单、24h/72h 长稳仍按测试方案的人工清单执行。
- 产物只落本地 `build/testkit/`；CI 归档需要额外配置 artifact 上传。
