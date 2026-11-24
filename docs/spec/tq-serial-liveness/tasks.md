# 任务清单（53.7.26）

## T1 天勤登录隔离
- [x] `src/data/tq_source.py` 增加 `import os`、`_login_disabled()`，`_get_api` 开头拦截（放在账号密码校验之后）。
- [x] `tests/conftest.py` 增加 session autouse fixture `_no_live_tq_login`，设置 `AUTO_TRADING_NO_TQ_LOGIN=1`。
- 验证：`pytest tests/test_tq_source.py tests/test_kline_fallback.py -q` 通过，测试进程不再出现 `天勤已登录`。

## T2 序列活性保护
- [x] `src/data/tq_source.py` 增加 `_LIVENESS`/`_LIVENESS_WARNED`、`_liveness_frozen()`。
- [x] `fetch_frames` 里检查并 `reset_api()` 一次，告警限一次。
- [x] `reset_api()` 不清理活性基准，重建后由 `fetch_frames` 重新计时。
- 验证：新增 3 个单测覆盖「两周期判死 / 午休不判死 / 只重置一次」。

## T3 实盘取数口径
- [x] `src/data/kline.py:582` 实盘 limit 改用 `limit`。

## T4 启动日志
- [x] `src/scheduler/runner.py` `__init__` 打 `程序启动 run_id=... 版本=... 根目录=... 启动时间=...`。

## T5 测试隔离
- [x] `tests/test_catch_up.py`、`tests/test_runner_guard.py` 里会落盘的 `_project_root` 改 `str(tmp_path)`。
- 说明：`test_flat_wait_next_bar.py`（`TradingBot.__new__`）、`test_strategy_params.py`、`test_strategy_factory.py` 只建策略、不写盘，`_project_root` 不起作用，保持原样，避免无关改动。
- 补必要的 `tmp_path` 形参。

## T6 定向回归
- [x] `pytest tests/test_tq_source.py tests/test_catch_up.py tests/test_runner_guard.py tests/test_flat_wait_next_bar.py tests/test_strategy_params.py tests/test_strategy_factory.py tests/test_kline_fallback.py -q` 通过。
- [x] `python tools/testkit.py baseline`：选中 2435 项，收集 2440 项，取消 5 项。

## T7 升版本与文档
- [x] `VERSION` → `53.7.26`
- [x] `CHANGELOG.md` 顶部新增 53.7.26（错误修复，小更新）
- [x] `README.md` 版本、用例数同步
- [x] `docs/TODO.md`、`docs/软件测试/软件测试方案.md`、`docs/软件测试/软件测试用例.md`、`docs/测试报告/软件测试报告-2026-11-24.md` 同步版本/用例数/修订记录/HTML 行数
- [x] `python tools/check_doc_versions.py` 通过

## T8 门禁与打包
- [x] `python tools/testkit.py gate --coverage`：pytest / coverage / ruff / mypy / lint-imports / docs 6 步全通过
- [x] `python tools/build_exe.py`：53.7.26 打包成功，exe 37.36 MB，目录 479.31 MB
- [x] 全中文汇报
