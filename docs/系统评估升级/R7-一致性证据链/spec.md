# R7 一致性证据链收口 — 规格

版本：49.14.0
日期：2026-11-20
范围：验收报告第六节 R7，P1-1 ～ P1-8。
约束：只修证据口径与签名绑定，不改开平仓规则、不新增业务配置键、不引入新依赖。

---

## 1. 目标

让「今日一致性」「策略健康」「上线评审单」三处结论只比较同口径、同窗口的证据：
回测与实盘必须同品种、同周期、同数据源、同滑点、同成交方式、同手数模式、同换月链资金口径；
评审单签字必须绑定当时看到的证据内容；旧滑点文件自动补齐 `account` 列。

验收口径：
1. 评审单任一项为「未记录」时，`sign_release_sheet(..., decision="允许小资金挂机")` 必须拒绝。
2. 签字后再回测/改配置，评审单要显示「证据已变化」，不能继续显示「已签字」。
3. 今日一致性只把落在本次复盘时间窗内的回测镜像命中与实盘闸门次数放在一起比；全周期累计只作背景说明。
4. 老滑点 CSV（10 列无 `account`）被读取或追加时自动迁移成 11 列，新记录落到 `account` 列。
5. 三档滑点与评审单读根级 `evaluation.slippage_extra_ticks`。
6. 单合约回测的 `params_snapshot` 含 `bars`，评审单 K 线数不再为 0。
7. 健康对照按品种过滤实盘样本；回测缺口径字段时只作参考，不累计 `watch_days`。
8. 首页健康卡传引擎/品种/周期与配置，走同一套口径校验。

---

## 2. 逐项改动

### P1-1 今日一致性统一时间窗

文件：`src/review/consistency.py`

- 新增 `_hit_in_windows(row: dict, windows: list[SessionWindow]) -> bool`：读 `row["time"]`（退回 `bar_id`），用 `_parse_ts` 解析后判断是否落在任一 `window.contains`。
- `build_consistency_lines`：`mirror_rows` 拆成 `same_window` 与全周期累计。
  - 镜像段改用 `format_mirror_gate_lines(same_window, enabled=True)`，首行标签替换为「回测镜像（同窗口）」。
  - 当 `len(same_window) != len(mirror_rows)` 时追加：「回测镜像  全周期累计放弃 N 次；今日只对比同窗口 M 次。」
- `_verdict` 改为 `_verdict(rows, total, enabled, live_total, compared)`：
  - 有同窗口命中才写「同窗口内回测放弃 X 次，实盘拦下 Y 次」。
  - 无同窗口命中但有全周期累计、且有实盘拦截时，明确写「回测今天这个窗口没有记录，全周期累计 N 次不与今日互比」。
  - 两边都无同窗口记录时写「同一时间窗内两边都没有放弃记录」，全周期数字只作附注。

### P1-2 健康对照按品种过滤，缺口径不累计观察天数

文件：`src/ops/health.py`

- `_live_pnls(root, lookback, *, symbol="", account=None)`：
  - `account` 有值走 `load_named_book`，否则 `load_book`（账本本身按当前账户隔离）。
  - 用 `matched_closes` 后按 `product_of(symbol)` 过滤；请求 `lh2701` 时只取 `lh` 品种的平仓。
- `evaluate_health` 新增关键字参数 `cfg: dict | None = None`、`account: str | None = None`，均保持可选，旧调用不变。
- `_backtest_scope` 扩展字段：`data_source`、`slippage_ticks`、`fill_on_next_open`、`sizing_mode`、`chain_capital`（换月链资金）。
- 当前口径 `wanted` 由显式 `engine/symbol/timeframe` 加 `cfg` 派生；只有 `cfg` 提供且回测也记录了该字段时才比较，缺记录进 `unknown`。
- `_norm_scope` 增加 `symbol` 按 `product_of` 归一、`slippage_ticks` 按数值保留 6 位、布尔按 `config_flag` 归一。
- `report()`：`unknown` 非空时 `watch_days = _state_days(base)`，不调用 `_note_watch_day`，避免旧回测缺 engine 仍累计天数。
- `maybe_emit_health` 透传 `cfg`、`account`。

### P1-3 评审单签名绑定证据摘要，缺证据禁签

文件：`src/review/release_sheet.py`

- 新增 `_evidence_sections(sections)`（排除「人工签字」）与 `_evidence_digest(sections)`：对 `title/status/lines` 做 `json.dumps(sort_keys=True)` 后 `sha256`，取前 16 位。
- `_signature_section(signature, digest)`：
  - 无签字 → `MISSING`。
  - 有签字但无 `evidence_digest` → `WATCH`，注明「旧版签字未绑定证据摘要」。
  - 摘要不一致 → `FAIL`，注明「签字后证据已变化，需重新生成并签字」。
  - 一致 → `PASS`，显示签字人、时间、决定、摘要前 8 位与证据生成时间。
- `build_release_sheet`：先建四节证据，算摘要，再建签字节，最后 `_verdict`；签字失效时 verdict 写「证据已变化，需重新签字」。
- `sign_release_sheet`：
  - `decision == "允许小资金挂机"` 且任一证据节为 `MISSING` → 抛 `ValueError("证据未齐，不能签「允许小资金挂机」：<节名>")`。
  - payload 增加 `evidence_digest`、`evidence_generated_at`（取 `sheet.generated_at`）。
  - 签字后写回 md/json 的行为不变。
- UI 调用方（`src/ui` 内签评审单处）捕获 `ValueError` 并弹窗提示，不写文件。

### P1-4 滑点 CSV 旧表头迁移

文件：`src/account/slippage_log.py`

- 新增 `_migrate_header(path: Path) -> None`：
  - 读首行；已是 `_FIELDS` 或已含 `account` 则返回。
  - 否则按旧表头建索引，重写成 `_FIELDS` 顺序，缺失列补空串；写临时文件后 `os.replace`。
  - `OSError` 一律吞掉，不影响追加与读取。
- `record_fill` 在 `new = not path.exists()` 之前调用。
- `load_rows` 开头调用；返回行统一成 `{field: row.get(field) or "" for field in _FIELDS}`，杜绝 `None` 键。

### P1-5 三档滑点与评审单读根级 evaluation

文件：`src/backtest/slippage.py`、`src/review/release_sheet.py`

- `build_ladder`：`extra` 改读 `_cfg_box(cfg, "evaluation")["slippage_extra_ticks"]`；docstring 同步。
- `_sim_slippage`：`extra = _number(_box((cfg or {}).get("evaluation")).get("slippage_extra_ticks"))`。
- 测试夹具改为根级 `evaluation`。

### P1-6 快照补 bars

文件：`src/backtest/engine.py`、`src/review/release_sheet.py`

- `_params_snapshot` 增加 `"bars": max(0, len(bars_signal) - int(trade_from))`。
- `_data_gate`：`bars` 先读 `snap["bars"]`，退回 `backtest["bars"]`。
- 补测试：快照无 bars、结果有 bars 时评审单不再判 0。

### P1-7 口径比对补齐字段

文件：`src/review/release_sheet.py`、`src/ops/health.py`、`src/scheduler/runner.py`、`src/review/live_eval.py`、`src/ui/dashboard.py`

- 评审单 `_data_gate` 新增 `_extra_scope_mismatches(cfg, snap)`，逐项比较：
  - `data.source` ↔ `snap.data_source`
  - `backtest.slippage_ticks` ↔ `snap.slippage_ticks`（数值）
  - `backtest.fill_on_next_open`（默认 true）↔ `snap.fill_on_next_open`
  - `risk.sizing_mode`（默认 risk_pct）↔ `snap.sizing_mode`
  - `backtest.chain_capital`（默认 false）↔ `snap.chain_capital`
- 品种统一按 `product_of` 比较（`bu` 与 `bu2612` 视为同品种，`fu` 与 `lh` 仍不一致）。
- 缺字段不判不一致，只在回测行说明「未记录」。
- 健康模块按 P1-2 的同一套字段与归一规则比较。
- `runner`、`live_eval`、`dashboard` 调用健康检查时传 `cfg`。

### P1-8 首页健康卡传口径

文件：`src/ui/dashboard.py`

- `evaluate_health(self.root_dir)` 改为：
  - `engine=self.engine_var.get()`
  - `timeframe=self._period_code(self.tf_var, self.tf_custom_var)`
  - `symbol=self.watch_choice_var.get().strip() or (self._watch_symbols[0] if self._watch_symbols else "")`
  - `cfg=self.cfg`
- 保留现有 `try/except`，异常时仍显示空。

---

## 3. 测试

改动/新增：
- `tests/test_consistency.py`：窗口外命中不计入今日对比；全周期累计有单独说明；无同窗口记录时不直接相减。
- `tests/test_health.py`：品种过滤；缺 engine 不累计 `watch_days`；数据源不一致显示「未对照」。
- `tests/test_release_sheet.py`：缺证据禁签；摘要绑定与篡改后失效；旧签字无摘要降级；根级 `evaluation` 生效；快照缺 bars 时回退结果 bars。
- `tests/test_slippage_log.py`：旧 10 列表头读取后迁移；迁移后追加的新记录 `account` 正确。
- `tests/test_backtest_slippage.py`：夹具改为根级 `evaluation`。

门禁：
```
python -m pytest -q -p no:cacheprovider
python -m ruff check src/review/consistency.py src/ops/health.py src/review/release_sheet.py src/account/slippage_log.py src/backtest/slippage.py src/backtest/engine.py src/ui/dashboard.py src/scheduler/runner.py src/review/live_eval.py tests/test_consistency.py tests/test_health.py tests/test_release_sheet.py tests/test_slippage_log.py tests/test_backtest_slippage.py
python -m mypy src/review/consistency.py src/ops/health.py src/review/release_sheet.py src/account/slippage_log.py src/backtest/slippage.py
```

---

## 4. 版本与文档

- `VERSION` 与 `CHANGELOG.md` 升到 `49.14.0`，一句话概括 R7 八项。
- `docs/系统评估升级/阶段性验收报告-49.13.0.md` 用 `git mv` 改为 `阶段性验收报告-49.14.0.md`。
- 报告第七节勾选 P1 相关三项，第八节 R7 完成度改 `8 / 8`，并补一段 49.14.0 复核说明。
- 同步 README 中验收报告链接与 `docs/系统评估升级/R11-第二阶段/spec.md` 第 4 行引用。
- 不新增其他 md。

---

## 5. 风险与边界

- 老回测缺 `data_source/slippage_ticks/...` 时会显示「仅供参考」，且不再累计 `watch_days`；这是 P1-2 的预期行为。
- 摘要只覆盖证据四节，不含签字节，避免签字动作本身使摘要失效。
- 旧滑点文件迁移只补列、不改历史行的语义；旧行 `account` 为空，评审单仍会提示「未分账」。
- 不触碰开平仓、风控、报单路径。

---

## 6. 待确认

请确认按本规格执行；批准后先建任务清单，再逐项改代码与测试，最后统一升版与文档同步。
