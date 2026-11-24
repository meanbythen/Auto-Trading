# 性能基线

对应版本 **53.7.22**（2026-11-24）。本文是 [P3 性能与长稳测试方案](P3性能与长稳测试方案.md) 4.2 节的入库摘要：记录基线的环境与数值，便于评审和回溯。

> 原始基线文件 `build/perf/baseline.json` 由本机生成，不进 Git；本文件只保存可读摘要。

---

## 1. 环境卡

| 项目 | 值 |
|---|---|
| 机型 | DESKTOP-ML56Q4S |
| 系统 | Windows 11（10.0.26100） |
| CPU | Intel64 Family 6 Model 183（32 核） |
| 内存 | 31.7 GB |
| Python | 3.12.10 |
| 依赖哈希 | `de4bd406a0ca`（`requirements.txt` + `requirements-dev.txt` + `pyproject.toml`） |
| 电源/后台 | 插电、高性能；未做额外后台进程白名单 |

---

## 2. 当前基线（2026-11-24，53.7.22 重设）

数据来源：`tests/perf/`，固定 4000 根 15 分钟 K 线，固定随机种子，不联网。53.7.22 把 pandas 升到 3.0.6、openpyxl 升到 3.1.5 后连续采样 7 次；PERF-09 受磁盘/杀软扫描影响出现 15 ms 与 22 ms 两簇，取稳定后的连续 3 次中位数作为基线：耗时类取 p95，吞吐类取中位数。

| 用例 | 指标 | 单位 | 基线值 | 绝对阈值 | 结果 |
|---|---|---|---|---|---|
| PERF-01 | 子进程导入 `src.ui.app` | ms | 611.0 | ≤ 3000 | 通过 |
| PERF-04 | 单品种 evaluate（600 根） | ms | 0.017 | ≤ 2000 | 通过 |
| PERF-06 | 10 品种一轮 evaluate | ms | 0.305 | ≤ 8000 | 通过 |
| PERF-07 | 回测吞吐（4000 根） | bars/s | 3071.7 | ≥ 1500 | 通过 |
| PERF-09 | 单笔入账 + SQLite 投影 | ms | 22.7 | ≤ 200 | 通过 |

说明：

- PERF-01 冷启动受杀毒/索引影响明显：5 次采样取中位数，p95 只作参考，避免单次 900 ms 抖动直接判红。
- PERF-04/06 是亚毫秒指标，比较时带 5 ms 噪声下限；差异小于下限只比绝对阈值。
- PERF-07 是吞吐类指标，相对门禁反过来：本次 ≥ 基线 × 0.85；采样 3 次取中位数，避免用最好那次判绿。
- 其余用例（PERF-02/03/05/08/10/11）依赖真实进程、预置持仓、优化器长任务或 GUI 会话，等对应夹具落地后补录基线。

上一版基线（53.7.10，哈希 `78a495e78f5e`）：PERF-01 626.4 ms、PERF-04 0.015 ms、PERF-06 0.362 ms、PERF-07 3419.3 bars/s、PERF-09 22.7 ms。pandas 3 升级后 PERF-01/09 与上一版基本持平，PERF-07 约低 10%，仍高于绝对阈值且无功能回退；PERF-09 的 15 ms 簇是磁盘/杀软扫描差异，不作为常态基线。

---

## 3. 复现与比对

```powershell
# 1) 跑性能基准（默认只记录，超阈值 warning）
.\.venv\Scripts\python.exe -m pytest -m perf tests\perf

# 2) 严格门禁：超阈值直接失败
$env:PERF_STRICT="1"; .\.venv\Scripts\python.exe -m pytest -m perf tests\perf

# 3) 生成 Markdown 报告（最近一次 + 趋势）
.\.venv\Scripts\python.exe tools\perf_report.py --latest
.\.venv\Scripts\python.exe tools\perf_report.py --output build\perf\report.md
.\.venv\Scripts\python.exe tools\perf_report.py --strict

# 4) 机型/依赖变化后重设基线
$env:PERF_WRITE_BASELINE="1"; .\.venv\Scripts\python.exe -m pytest -m perf tests\perf
```

报告会同时给出现次 p95、基线、比值（本次 ÷ 基线）与结论；依赖哈希与基线不一致时会明确标注「只看趋势」。

---

## 4. 更新规则

| 情况 | 动作 |
|---|---|
| 同机同依赖的日常复跑 | 不重设基线，超基线 120% 记回归并排查；吞吐类低于基线 85% 记回归 |
| 换机器、换 Python 大版本、关键依赖升级 | 重设基线，并更新本文环境卡与表格 |
| 核心算法或数据结构改动 | 先记录变化原因，再重设基线 |
| 指标长期稳定且阈值明显偏松 | 收紧绝对阈值，保留相对门禁 |

重设基线后必须重跑一次 `tools/perf_report.py --strict`，确认报告可生成且全部通过。
