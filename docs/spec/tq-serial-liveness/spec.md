# 天勤序列活性保护与测试隔离 Spec（53.7.26）

## 1. 背景与已确认结论

2026-11-24 早盘实盘出现行情僵死：

- `lh2701` 15 分钟 K 线从开盘起一直是 `2026-11-23 15:00`，1002 条健康日志全部 `source_stale`，下载耗时 35–130ms 正常（网络没断）。
- 08:57–08:59 pytest 在实盘同一台机器上连续 8 次用真实天勤账号 `meanbythen` 登录（`天勤已登录 | meanbythen`），最后一次登录会顶掉实盘进程的行情连接。
- `src/data/tq_source.py:fetch_frames` 把序列缓存在 `client._at_kline_serials`，重连后旧序列不会自己前进；`_fill_history` 见历史已填满就 `break`，此后没有任何活性校验，所以僵死不会自愈。
- 10:34 / 13:58 重新登录后能取到当天数据，证明是「连接内序列僵死」，不是数据源缺失。
- `fu2701` 5 分钟里混入的 `2026-10-27 10:15` 假记录共 237 条，来自 `tests/test_catch_up.py` 的 `"_project_root": "."` + 假 K 线写进了生产 `data/market_health.jsonl` / `data/events.jsonl`。
- 启动/关闭在 events.jsonl 有 `shutdown` 记录，但普通日志没有一行明确的「程序启动 run_id=...」，排查时不好对齐进程生命周期。
- 次要不一致：日志打印 `max_bars=600`（`data.live_max_bars`），但 `src/data/kline.py:582` 实盘天勤取数硬编码 `limit=200`。

## 2. 目标

1. 测试进程不再用真实天勤账号登录，避免顶掉实盘行情连接。
2. 天勤 K 线序列在盘内连续两个周期没有前进时，自动 `reset_api()` 重建序列，且同一条序列只告警一次。
3. `tests/test_catch_up.py` 等使用 `"_project_root": "."` 的用例改到 `tmp_path`，测试不再污染生产数据文件。
4. 程序启动时打一行明确日志：`程序启动 run_id=... 版本=...`。
5. 实盘天勤取数按 `live_max_bars` 取，日志口径与真实请求一致。
6. 版本升到 53.7.26，按项目固定流程同步 CHANGELOG/README/VERSION/测试文档并跑门禁。

## 3. 非目标

- 不改策略、风控、下单、清仓逻辑。
- 不处理节前夜盘临时取消、天勤账号权限等外部因素。
- 不重构 `tq_source` 的序列缓存结构。

## 4. 具体改动

### 4.1 `src/data/tq_source.py`：测试不登录真实账号

- 新增 `import os`。
- 新增模块级判断：

```python
def _login_disabled() -> bool:
    """测试/自检进程禁止登录真实天勤账号：同账号并发登录会顶掉实盘行情连接。"""
    return os.environ.get("AUTO_TRADING_NO_TQ_LOGIN", "").strip().lower() in {"1", "true", "yes", "on"}
```

- 在 `_get_api(user, password)` 开头（整理完 user/password 后、建连前）判断，命中就抛：

```python
raise RuntimeError(
    "AUTO_TRADING_NO_TQ_LOGIN 已开启：测试/自检不登录天勤，避免顶掉实盘行情连接"
)
```

调用方 `KlineProvider` 已有「下载失败 → 回退本地库」的兜底，测试继续可跑。

### 4.2 `src/data/tq_source.py`：序列活性保护

- 新增模块级状态：

```python
# (天勤合约, 周期秒) → (最后一根K线时间, 第一次看到这个时间点的时刻)
_LIVENESS: dict[tuple[str, int], tuple[datetime, datetime]] = {}
_LIVENESS_WARNED: set[tuple[str, int]] = set()
```

- 新增辅助函数（`datetime` 需从 `datetime` 导入）：

```python
def _liveness_frozen(
    symbol: str,
    code: str,
    seconds: int,
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """盘内最后一根K线超过两个周期没前进，返回这根的时间；否则 None。"""
```

判定规则：

1. 取 `frame.index[-1]` 作为 `last`，去掉时区。
2. `(code, seconds)` 没有记录，或 `last` 比记录更大 → 记 `(last, now)`、清掉告警标记，返回 `None`。
3. 否则用 `src.data.periods.trading_minutes_between(首次看到时刻, now, symbol)` 计算盘内分钟（午休/收盘/周末自动为 0）。
4. 盘内分钟 `< 2 × 周期分钟` → `None`；否则返回 `last`（判死）。

- 在 `fetch_frames` 生成 `frames` 之后（仍在 `_LOCK` 内）逐个检查：

```python
frozen: list[tuple[tuple[str, int], datetime]] = []
for symbol, frame in frames.items():
    stopped = _liveness_frozen(symbol, codes[symbol], seconds, frame)
    if stopped is None:
        continue
    key = (codes[symbol], seconds)
    if key not in _LIVENESS_WARNED:
        _LIVENESS_WARNED.add(key)
        logger.warning(
            "天勤K线 {} {} 最后一根停在 {}，盘内已超过两个周期没有前进，重建行情序列",
            codes[symbol], period, stopped,
        )
    frozen.append((key, stopped))
```

- 离开 `_LOCK` 后，若 `frozen` 非空且 `api is None`（实盘全局连接）：

```python
reset_api()
for key, stopped in frozen:
    _LIVENESS[key] = (stopped, datetime.now())
```

重建后重新计时，避免每轮重复重置；`_LIVENESS_WARNED` 保持，直到该序列真的前进才清除。`api is not None`（回测/测试自传连接）只告警，不动全局连接。

### 4.3 `src/data/kline.py`：实盘取数按 live_max_bars

`_fetch_impl` 第 582 行：

```python
fresh = self._fetch_tq(symbol, period, limit=1_000_000 if keep_history else limit)
```

与第 506–513 行日志 `max_bars=` 口径一致。

### 4.4 `src/scheduler/runner.py`：启动日志

`TradingBot.__init__` 里 `set_run_id(self.run_id)` 之后新增：

```python
from src.version import __version__

logger.info(
    "程序启动 run_id={} 版本={} 根目录={} 启动时间={}",
    self.run_id,
    __version__,
    root,
    self.started_at,
)
```

### 4.5 `tests/conftest.py`：测试进程禁登录

新增 session 级 autouse fixture：

```python
@pytest.fixture(scope="session", autouse=True)
def _no_live_tq_login():
    """测试进程不登录真实天勤账号：同一账号并发登录会顶掉实盘行情连接。"""
    old = os.environ.get("AUTO_TRADING_NO_TQ_LOGIN")
    os.environ["AUTO_TRADING_NO_TQ_LOGIN"] = "1"
    yield
    if old is None:
        os.environ.pop("AUTO_TRADING_NO_TQ_LOGIN", None)
    else:
        os.environ["AUTO_TRADING_NO_TQ_LOGIN"] = old
```

需要补 `import os`。

### 4.6 测试隔离：`_project_root` 改 tmp_path

把测试里 `"_project_root": "."` 改成临时目录，避免写生产 `data/`、`logs/`：

- `tests/test_catch_up.py:309`（主要污染源，造 fu2701 假 K 线）
- `tests/test_runner_guard.py:775, 824`（TradingBot 会写 events/health）
- `tests/test_flat_wait_next_bar.py:10`（同上）
- `tests/test_strategy_params.py:16, 102`、`tests/test_strategy_factory.py:13`（无写盘副作用也一并统一）

对应测试函数需要 `tmp_path` 参数（fixture 已有则直接用，没有就加）。若某个测试依赖根目录下的真实配置，改用 `str(tmp_path)` 后需确认断言不依赖真实文件。

### 4.7 新增单元测试（`tests/test_tq_source.py`）

1. `test_serial_frozen_after_two_periods`：同一根 `last` 两次调用 `_liveness_frozen`，第二次 `now` 距首次 ≥ 10 个交易分钟（5 分钟周期）→ 返回该时间。
2. `test_serial_not_frozen_during_lunch_break`：首次 11:25、第二次 13:20（午休不计）→ `None`。
3. `test_serial_frozen_resets_api_once`：`fetch_frames(api=None)`，`monkeypatch` `_get_api` 返回固定序列、`reset_api` 记账；连续两轮同根 K 线且盘内过期 → `reset_api` 被调用 1 次、告警只有一条。

测试用现有 `_raw()` 造 tqsdk 结构，Fake client 只需 `get_kline_serial(code, seconds, data_length)` 与 `wait_update(deadline)`。测试前后调用 `_clear_history_short()` 清理模块状态。

## 5. 验证步骤

1. `python -m pytest tests/test_tq_source.py tests/test_catch_up.py tests/test_runner_guard.py tests/test_flat_wait_next_bar.py tests/test_strategy_params.py tests/test_strategy_factory.py -q`
2. `python tools/testkit.py baseline` 取实际用例数（预计 2432 + 新增 3 项）。
3. 升 `VERSION` 为 `53.7.26`，同步 `CHANGELOG.md`、`README.md`、`docs/TODO.md`、`docs/软件测试/软件测试方案.md`、`docs/软件测试/测试用例.md`、`docs/测试报告/软件测试报告-2026-11-24.md` 的版本号、用例数、修订记录和 HTML 行数。
4. `python tools/check_doc_versions.py`
5. `python tools/testkit.py gate --coverage`
6. `python tools/build_exe.py`
7. 全中文汇报：改了什么、为什么、验证结果、版本号。

## 6. 验收标准

- 测试收集数 ≥ 基线，全部通过；`gate --coverage` 6 步全过；打包成功。
- 新增用例能证明：盘内两周期不前进会重置，午休不误判，重置只发生一次。
- 测试运行后生产 `data/market_health.jsonl` / `data/events.jsonl` 不再新增假合约记录。
- 实盘日志出现 `程序启动 run_id=... 版本=53.7.26`，取数日志 `max_bars` 与实际请求一致。
