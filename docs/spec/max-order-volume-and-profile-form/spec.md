# 单笔最大手数回填异常与档案表单首次保存被冲掉

## 1. 问题现象

配置页「单笔最大手数」填 3 → 点「保存交易配置」→ 界面刷回 20；再填一次 3 保存才保持 3；重启程序又变回 20。

## 2. 已确认的根因（代码证据）

### 缺陷 A：保存时先触发重灌，再读表单

[config_collect.py](file:///d:/Auto-Trading/src/ui/config_collect.py#L641-L693)：

1. 先从 `ctp_max_volume_var` 取出 3，写入公共值 `ctp_box["max_order_volume"]`；
2. 第 648 行 `self.ctp_kind_var.set(...)`。Tk 的 `write` trace **即使值没变也会触发**，进入
   [config_vars.py](file:///d:/Auto-Trading/src/ui/config_vars.py#L778-L807) 的 `_on_ctp_kind_changed` → `_fill_ctp_profile_vars()`；
3. 此时当前档案（磁盘/内存中仍是 20）尚未被写入，`_fill_ctp_profile_vars()` 把 20 写回 `ctp_max_volume_var`，界面表现为“刷回 20”；
4. 第 677 行才把 `item["max_order_volume"] = 3` 写进档案，但界面已是 20。第二次保存时档案已是 3，所以看起来“第二次才生效”。

同一顺序问题**不止手数**：第 668-675 行读取的 `td_front`、`md_front`、`broker_id`、`user_id`、`password_env`、`auth_code_env` 都在第 648 行的重灌之后才读，因此这 6 个档案输入框的新输入也会在首次保存时被旧值覆盖（用户改动会丢）。这是“其他设置项是否有同样问题”的答案。

### 缺陷 B：界面回填口径与运行时不一致

[config_vars.py](file:///d:/Auto-Trading/src/ui/config_vars.py#L775-L776) 直接用 `resolve_ctp_profile` 的结果显示，而该函数是“档案覆盖公共默认”，不是“取小”。
运行时 [ctp_gateway.py](file:///d:/Auto-Trading/src/trader/ctp_gateway.py#L182-L194) 与 [session.py](file:///d:/Auto-Trading/src/account/session.py#L129-L138) 都是 `min(公共, 档案)`。
于是公共 3 / 档案 20 时：界面显示 20，实际拦单 3。

### 缺陷 C：重启后档案值被注册表物化改写为 20

[config_loader.py](file:///d:/Auto-Trading/src/utils/config_loader.py#L450-L457) 启动时 `apply_account_registry` →
[registry.py](file:///d:/Auto-Trading/src/account/registry.py#L443)：
`"max_order_volume": account.max_order_volume or profile.max_order_volume`。

真实数据（`config/accounts.yaml`）：账号 `acc_program_selftest` 的 `max_order_volume: 0`，连接 `program_selftest` 的 `max_order_volume: 20`。
所以每次启动内存里的档案都被改写为 20；`config/config.yaml` 里的 3 仍在（保存本身没失败），但界面按档案显示 20。
另有 [registry.py](file:///d:/Auto-Trading/src/account/registry.py#L487-L488) `sync_from_cfg` 直接跳过自测账号，自测账号的账号级上限永远不会落库。

## 3. 语义决定

「单笔最大手数」是**本机硬上限**，不是柜台上限：

```
实际生效 = 公共 trade.ctp.max_order_volume 与 券商档案 max_order_volume 都 >0 时取小；
           只有一个 >0 时取那个；都为 0 时回退 20。
```

界面必须显示这个“实际生效值”，与 `ctp_gateway` / `session` 完全一致。不改变持久化归属（公共值存 `trade.ctp`，档案值存 `ctp_profiles.<name>`，账号级存注册表），只统一口径。

## 4. 变更清单

### 4.1 `src/trader/ctp_limits.py`（新增）与 `src/trader/ctp_profile.py`

新增公开函数（唯一口径来源）：

```python
def effective_max_order_volume(configured_max: Any, profile_max: Any, default: int = 20) -> int
```

- 非法/空 → 0；两值都 >0 取小；单值 >0 取该值；都 0 回退 `default`。
- 放在独立的叶子模块 `ctp_limits.py`：`account.session` 也要用，但静态依赖 `ctp_profile` 会与 `utils.config_loader` 形成导入环（`tests/test_architecture.py` 已拦截）。`ctp_profile` 顶部 re-export 该函数，保持原有公开入口。

### 4.2 `src/ui/config_vars.py`

`_fill_ctp_profile_vars()` 最后一行改为：

```python
self.ctp_max_volume_var.set(
    str(effective_max_order_volume(ctp_box.get("max_order_volume"), resolved.get("max_order_volume")))
)
```

效果：公共 3 / 档案 20 → 显示 3（= 实际拦单值），重启后也显示 3。

### 4.3 `src/ui/config_collect.py`

重排 `_apply_ui_to_cfg()` 中 CTP 段（第 641-693 行），顺序改为：

1. 先取 `max_order_volume`；
2. 先取 6 个档案表单字段到局部变量（`split_fronts` / `dump_front_field` 导入提前）；
3. 写公共 `ctp_box["max_order_volume"]`；
4. 写当前档案 `profiles[profile_name]`（含 6 个字段与手数）；
5. 仅在 `self.ctp_kind_var.get() != 目标标签` 时才 `set`（避免无变化也触发重灌）；
6. 之后再处理 `matched` 档案切换与必要的 `_fill_ctp_profile_vars()`。

不变式：保存过程中只有当“操作账种类真的变了 / 档案真的换了”才允许重灌表单。

### 4.4 `src/trader/ctp_gateway.py`

第 190-194 行改用 `effective_max_order_volume(configured_max, ctp.get("max_order_volume"))`，行为等价，去掉重复实现。

### 4.5 `src/account/session.py`

第 129-138 行当前是 `min(configured_max, ctp.get("max_order_volume"))`——两值读的是同一个公共字典，等于没有取小（注释与实现不符）。
改为从 `ctp["ctp_profiles"][profile_name]["max_order_volume"]` 取档案值，再用 `effective_max_order_volume` 计算。
注意必须保留“materialize 之前读取 `configured_max`”的现有顺序。

### 4.6 不做的事

- 不修改 `registry.materialize` 的覆盖策略（缺陷 C 由统一口径消除，不影响实际拦单）。
- 不修改账号页 / 连接模板的 `max_order_volume` 编辑语义。
- 不新增配置键，不迁移数据。

## 5. 其他设置项排查结论

| 类别 | 是否存在同类问题 | 说明 |
| --- | --- | --- |
| 6 个档案表单字段（前置/BrokerID/资金账号/两个环境变量名） | 是（缺陷 A） | 4.3 的顺序重排一并修复 |
| 单笔最大手数 | 是（A+B） | 4.1/4.2/4.3 修复 |
| 其他由注册表物化的字段（前置、账号、间隔、告警数） | 否 | 界面回填与运行时读的是同一份 resolved 结果，且无“取小”逻辑 |
| `generic_stop_var`、`kelly_*_var`、`engine_var` 等 trace | 否 | 只刷新提示/面板，不重写用户输入控件 |

## 6. 验证步骤

1. 新增单元测试：
   - `effective_max_order_volume`：(3,20)→3、(20,5)→5、(20,0)→20、(0,5)→5、(0,0)→20。
   - 用“注册表物化后档案 20 + 公共 3”的配置，断言 `_fill_ctp_profile_vars` 回填 `"3"`（若现有 UI 测试桩不可用，则用最小伪对象承载 StringVar）。
   - 保存路径回归：首次保存后 `ctp_max_volume_var` 仍为刚输入的 3，且 `ctp_profiles.<name>.max_order_volume == 3`、公共值 == 3。
2. `python -m pytest tests/test_ctp_channel.py tests/test_ui_regression.py tests/test_risk.py -q`
3. `python -m pytest -q`
4. `git diff --check`
5. 手工：仿真账号下改 3 → 保存一次即保持 3 → 重启仍显示 3；启动日志中的实际 `max_order_volume` 为 3。

## 7. 约束

- 不提交 git；不触碰未跟踪 DOCX 与无关改动。
- 不虚构结论：上述根因均有行号与真实 `config/accounts.yaml`、`config/config.yaml` 数据支撑。
