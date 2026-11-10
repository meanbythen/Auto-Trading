# 如何在策略中设置ATR动态仓位、移动跟踪止损、因子分歧校验

# 一、整体说明

下面三块：**ATR动态仓位、移动跟踪止损、因子分歧校验**，全部承接上面这套【日线方向 + 5分钟三因子】vnpy策略，讲清楚原理、公式、代码位置、坑点；

前提：ATR用 `ATR(14)`，跟踪止损倍数 `2*ATR`，单笔风险控制为账户资金的1%（你之前的规则）  
风险提示：仅学习代码逻辑，不构成交易建议

---

# 二、ATR动态仓位（开仓阶段，只在无持仓时计算）

## 原理

核心是海龟式单位风险仓位：**每一笔单子，止损被打穿时，亏损≈账户总资金 × risk_ratio（这里0.01）**

![](https://cdn.nlark.com/yuque/__latex/bd1d308e3d7162ec74c0a3ec7e19e777.svg)

- ATR：当前5分钟ATR(14)，代表当前波动大小
- 止损倍数：2，也就是止损距离 = `2 * ATR`
- 合约乘数：品种自带（燃油FU=10吨，纯碱SA=20吨，玉米C=10吨）
- 最后向下取整，最小手数1手

特点：波动大的时候ATR变大 → 手数自动变小；波动压缩ATR变小 → 手数适度放大；天然波动率仓位。

## 代码片段（放在 `try_open_order` 内部）

```
def try_open_order(self):
    atr = self.am_m5.atr(self.atr_len)
    close = self.am_m5.close[-1]
    symbol_info = self.get_contract(self.vt_symbol)
    if not symbol_info:
        self.write_log("获取合约信息失败")
        return
    multiplier = symbol_info.size

    capital = self.cta_engine.get_capital()
    risk_value = capital * self.risk_ratio   # 这笔最多亏多少钱
    stop_distance = self.atr_stop_multiple * atr
    risk_per_lot = stop_distance * multiplier  # 每一手止损打掉的亏损

    if risk_per_lot <= 0:
        return

    # 计算手数，向下取整，最少1手
    volume = int(risk_value / risk_per_lot)
    volume = max(1, volume)

    # 开仓逻辑
    if self.daily_direction == 1 and self.total_score > self.open_threshold:
        self.buy(close, volume)
        self.entry_price = close
        self.stop_price = self.entry_price - stop_distance
        self.write_log(f"开多｜总分:{self.total_score:.2f},手数:{volume},止损价:{self.stop_price:.2f}")
    elif self.daily_direction == -1 and self.total_score < -self.open_threshold:
        self.short(close, volume)
        self.entry_price = close
        self.stop_price = self.entry_price + stop_distance
        self.write_log(f"开空｜总分:{self.total_score:.2f},手数:{volume},止损价:{self.stop_price:.2f}")
```

## 坑点

1. `get_capital()` 部分vnpy回测引擎不支持，回测时可以替换成固定本金：`capital = 30000`（适合你的3万本金）
2. ATR是bar收盘的值，**不能盘中实时刷新重新加仓改手数**；手数只在开仓那一刻确定
3. 要加总风险上限：所有持仓合计风险不超过账户3%（进阶硬约束，防止多品种同时重仓）

---

# 三、移动跟踪止损（持仓阶段，每一根5分钟K更新一次）

## 规则

- 多头：止损价**只上移、不下移**。每根新K，新止损 = max(旧止损, 最新收盘价 - 2*ATR)
- 空头：止损价**只下移、不上移**。每根新K，新止损 = min(旧止损, 最新收盘价 + 2*ATR)
- 触发条件：收盘价击穿止损价 → 平仓

和固定止损区别：盈利后止损跟着行情移动，把浮盈锁住；回撤才离场，符合趋势跟踪。

出场是双条件：【跟踪止损触发】OR【信号反转】，任意一个满足就平仓

## 代码片段（放在 `check_exit`）

```
def check_exit(self, bar: BarData):
    atr = self.am_m5.atr(self.atr_len)
    stop_dist = self.atr_stop_multiple * atr
    new_stop = self.stop_price

    if self.pos > 0:
        # 多头：止损向上移动，不后退
        new_stop = max(self.stop_price, bar.close_price - stop_dist)
        # 止损触发
        if bar.close_price <= new_stop:
            self.sell(bar.close_price, abs(self.pos))
            self.write_log("多头：跟踪止损触发离场")
        # 信号反转离场（总分反向）
        elif self.total_score < -self.close_threshold:
            self.sell(bar.close_price, abs(self.pos))
            self.write_log("多头：信号反转离场")

    elif self.pos < 0:
        # 空头：止损向下移动，不后退
        new_stop = min(self.stop_price, bar.close_price + stop_dist)
        # 止损触发
        if bar.close_price >= new_stop:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log("空头：跟踪止损触发离场")
        # 信号反转离场
        elif self.total_score > self.close_threshold:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log("空头：信号反转离场")

    self.stop_price = new_stop
```

## 坑点

1. 跟踪止损是**收盘K判定**，不是盘中tick；如果想要盘中tick止损，要挪到on_tick，复杂度上升
2. 跳空会直接击穿止损，没有滑点保护，这是CTA天然风险
3. 收盘强制平仓优先级最高，高于跟踪止损

---

# 四、因子分歧校验（市场过滤器，开仓前一票否决）

## 原理

三个因子各自输出分数：`score_ma, score_don, score_boll`  
把三个分数放入数组，计算**标准差std**：

- 标准差越大，代表因子之间方向打架（一个+1、一个-1、一个0 → 分歧巨大）
- 阈值：`factor_std_limit = 0.7`（你之前设定）
- 规则：`std > 0.7` → 分歧过大，禁止开新仓；已有持仓继续跑跟踪止损，不强行平仓

它是【开仓过滤器】，不是平仓过滤器！  
举例：均线看多+1，唐奇安看空-1，布林中性0.5 → 标准差很大，直接放弃开仓，避免假信号。

## 代码片段（放在 `check_market_env` 函数里）

```
def check_market_env(self) -> bool:
    # 1. ADX过滤
    adx = self.am_m5.adx(self.adx_m5_len)
    if adx < self.adx_m5_min:
        return False

    # 2. ATR低波动过滤
    atr_array = self.am_m5.atr(self.atr_len, array=True)
    atr_now = atr_array[-1]
    atr_p20 = np.percentile(atr_array, self.atr_low_percentile * 100)
    if atr_now < atr_p20:
        return False

    # 3. 因子分歧校验【核心】
    score_list = np.array([self.score_ma, self.score_don, self.score_boll])
    score_std = np.std(score_list)
    if score_std > self.factor_std_limit:
        self.write_log(f"因子分歧过大，std={score_std:.3f}")
        return False

    return True
```

## 阈值调试参考

- 0.6：严格，信号很少，胜率高
- 0.7：中等（推荐）
- 0.8：宽松，信号变多，杂信号增加

注意：分歧校验只拦**新开仓**，已经持仓的单子，哪怕后面因子开始打架，也继续持有，交给跟踪止损/信号反转离场。

---

# 五、三者执行顺序（非常重要，决定策略逻辑）

```
收到5分钟K
    ├─ 更新指标 → 计算三个因子得分 → 计算总分
    ├─ 先判断：是否收盘时间 → 强制平仓（最高优先级）
    ├─ 如果当前有持仓：
    │      └─ 执行【移动跟踪止损 + 信号反转离场】
    └─ 如果当前无持仓：
           ├─ 日线方向是否为0？是 → 跳过
           ├─ 执行市场过滤器：ADX + ATR分位 +【因子分歧校验】
           └─ 过滤器通过 → 执行【ATR动态仓位】计算手数，尝试开仓
```

# 六、常见进阶优化（适合你后续迭代）

1. **总风险上限**：全局变量记录当前所有持仓风险，合计超过3%，禁止新开仓
2. **信号强度加权仓位**：总分越高，在基础仓位上再加仓（>0.8用1.0N，0.65~0.8用0.6N）
3. **分歧度日志输出**：每次分歧拦截的时候打印三个因子分数，方便复盘哪两个因子经常打架
4. 注意：`np.percentile` 是滚动窗口，实盘如果不想有未来隐患，可以用deque滚动队列维护ATR序列，自己算分位

# 七、整合提示

你上一版完整框架已经包含这三块，上面是单独抽离讲解；如果你想要，我可以输出**修复完边界bug、把这三块逻辑加固后的完整新版本代码**（增加总风险限制、固定本金兼容回测、分歧日志打印）。
