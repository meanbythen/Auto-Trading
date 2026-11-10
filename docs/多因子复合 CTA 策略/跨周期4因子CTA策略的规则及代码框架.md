# 跨周期4因子CTA策略的规则及代码框架

说明：  
4因子：均线趋势因子、唐奇安突破因子、布林趋势因子、SAR因子  
跨周期设计：**大周期定方向（过滤），小周期抓入场信号**  
约定：

- 大周期：日线（判断趋势方向，一票否决）
- 小周期：5分钟K（生成4因子打分、开平仓）
- 框架基于 vnpy，Python，vnpy CtaStrategy 标准结构
- 所有因子输出 `[-1, 0, 0.5, 1]`，固定权重：ma=0.3, donchian=0.25, boll=0.25, sar=0.2
- 总分阈值：>0.6做多，<-0.6做空；中间区间不新开仓
- 顶层过滤器：ADX、ATR分位、因子分歧度
- 日内规则：收盘强制清仓

---

# 第一部分：4因子完整打分规则（文档版，可直接对照代码）

## 公共前置

输入：小周期Bar（5min）、大周期Bar（日线）  
大周期方向过滤（强过滤）

- long_bias：日线 close > ma60 → 允许做多；否则做多信号直接置0
- short_bias：日线 close < ma60 → 允许做空；否则做空信号直接置0

### 因子1：均线趋势因子（MA20/MA60，权重0.3）

参数：ma_short=20，ma_long=60（小周期）

```
if close > ma_long and ma_short > ma_long:
    if 金叉：score = +1
    else：score = +0.5
elif close < ma_long and ma_short < ma_long:
    if 死叉：score = -1
    else：score = -0.5
else:
    # 均线缠绕：|ma_short - ma_long| < 0.5*atr14
    score = 0
```

再叠加大周期bias：不满足大周期bias，本因子得分强制=0

### 因子2：唐奇安突破因子（20周期突破，10周期离场，权重0.25）

参数：don_high=20, don_low=20

```
h20 = 过去20根K最高
l20 = 过去20根K最低
if close > h20:
    score = +1
elif close < l20:
    score = -1
elif close > (h20+l20)/2:
    score = +0.5
elif close < (h20+l20)/2:
    score = -0.5
else:
    score = 0
```

叠加大周期bias校验，方向不一致 → score=0

### 因子3：布林趋势因子（MA20，2倍std，权重0.25）

参数：boll_len=20, boll_std=2

```
mid = ma20
upper = mid + 2*std(20)
lower = mid - 2*std(20)
slope = mid - mid[1]  # 中轨斜率
if slope > 0 and close > mid:
    if close > upper:
        score = +1
    else:
        score = +0.5
elif slope < 0 and close < mid:
    if close < lower:
        score = -1
    else:
        score = -0.5
else:
    # 中轨走平/反向
    score = 0
```

叠加大周期bias校验，方向不一致 → score=0

### 因子4：SAR转向因子（AF 0.02，max 0.2，权重0.2）

```
if sar < close:
    # SAR多头
    score = +1
elif sar > close:
    # SAR空头
    score = -1
else:
    score = 0
```

附加约束：

- 窗口内翻转次数>3次（最近20根K反复翻转）→ score=0
- 和大周期bias不一致 → score=0

### 总分合成

```
total_score = ma_score*0.3 + don_score*0.25 + boll_score*0.25 + sar_score*0.2
```

### 顶层市场状态过滤器（全局开关，is_tradeable）

```
1. ADX(14) < 20 → is_tradeable = False
2. ATR14 处于近60日ATR分位 < 0.2 → is_tradeable = False
3. 因子分歧度：4个因子得分标准差 > 0.7 → 因子打架，is_tradeable=False
```

分歧度：np.std([ma_score, don_score, boll_score, sar_score])

### 开仓条件

- is_tradeable == True
- total_score > 0.6 且 无多头持仓 → 开多
- total_score < -0.6 且 无空头持仓 → 开空

### 平仓条件

1. 信号反转平仓：多单 total_score < -0.6；空单 total_score > 0.6
2. ATR移动止损：2*ATR(14)跟踪止损
3. 日内强制平仓：收盘前N分钟全部平仓
4. 单笔风险：单笔风险控制总资金1%，ATR动态手数

---

# 第二部分：vnpy 跨周期4因子CTA 完整代码框架

依赖：vnpy 官方库，vnpy_ctastrategy  
特点：

- 独立日线数据缓存（跨周期）
- 4因子打分
- 市场状态过滤器 ADX+ATR分位+因子分歧
- ATR动态仓位
- 收盘强制清仓
- 移动止损

```
from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
)
import numpy as np


class MultiFactor4CrossCycleStrategy(CtaTemplate):
    """
    跨周期4因子CTA策略
    大周期：日线，MA60方向过滤
    小周期：5分钟，4因子打分
    因子：均线 / 唐奇安 / 布林 / SAR
    """
    author = "study"
    # ------------ 参数 ------------
    # 均线因子
    ma_short = 20
    ma_long = 60
    # 唐奇安
    donchian_period = 20
    # 布林
    boll_len = 20
    boll_std = 2
    # SAR
    sar_af = 0.02
    sar_maxaf = 0.2
    # ATR
    atr_len = 14
    # ADX
    adx_len = 14
    # ATR分位窗口
    atr_window = 60
    # 打分阈值
    score_long_threshold = 0.6
    score_short_threshold = -0.6
    # 止损倍数
    atr_stop_multiple = 2.0
    # 单笔风险占总资金比例
    risk_per_trade = 0.01
    # 收盘清仓分钟
    close_minute = 15

    # 权重固定
    w_ma = 0.3
    w_don = 0.25
    w_boll = 0.25
    w_sar = 0.2

    # 变量
    total_score = 0.0
    ma_score = 0.0
    don_score = 0.0
    boll_score = 0.0
    sar_score = 0.0
    is_tradeable = False
    long_bias = False
    short_bias = False

    # 止损价格
    stop_loss_price = 0.0

    parameters = [
        "ma_short", "ma_long", "donchian_period",
        "boll_len", "boll_std", "sar_af", "sar_maxaf",
        "atr_len", "adx_len", "atr_window",
        "score_long_threshold", "score_short_threshold",
        "atr_stop_multiple", "risk_per_trade", "close_minute"
    ]
    variables = [
        "total_score", "ma_score", "don_score", "boll_score", "sar_score",
        "is_tradeable", "long_bias", "short_bias", "stop_loss_price"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # 5min K线
        self.bg5 = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am5 = ArrayManager(size=100)
        # 日线 K线
        self.bg_day = BarGenerator(self.on_bar, 1440, self.on_day_bar)
        self.am_day = ArrayManager(size=100)

        # ATR历史序列，用来算分位
        self.atr_list = []

    def on_init(self):
        self.write_log("策略初始化")
        self.load_bar(10)

    def on_start(self):
        self.write_log("策略启动")

    def on_stop(self):
        self.write_log("策略停止")

    def on_tick(self, tick: TickData):
        self.bg5.update_tick(tick)
        self.bg_day.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg5.update_bar(bar)
        self.bg_day.update_bar(bar)

    def on_day_bar(self, bar: BarData):
        """日线大周期：只用来判断多空bias"""
        self.am_day.update_bar(bar)
        if not self.am_day.inited:
            return
        ma60_day = self.am_day.sma(self.ma_long)
        close_day = self.am_day.close[-1]
        self.long_bias = close_day > ma60_day
        self.short_bias = close_day < ma60_day

    def on_5min_bar(self, bar: BarData):
        self.cancel_all()
        self.am5.update_bar(bar)
        if not self.am5.inited or not self.am_day.inited:
            return

        # ========== 1. 计算基础指标 ==========
        close = self.am5.close[-1]
        atr = self.am5.atr(self.atr_len)
        adx = self.am5.adx(self.adx_len)
        ma_s = self.am5.sma(self.ma_short)
        ma_l = self.am5.sma(self.ma_long)

        # 唐奇安
        h20 = self.am5.high[-self.donchian_period:].max()
        l20 = self.am5.low[-self.donchian_period:].min()
        mid_don = (h20 + l20) / 2

        # 布林
        boll_mid = self.am5.sma(self.boll_len)
        boll_std = self.am5.std(self.boll_len)
        boll_upper = boll_mid + self.boll_std * boll_std
        boll_lower = boll_mid - self.boll_std * boll_std
        boll_slope = boll_mid - self.am5.sma(self.boll_len, 1)[-1]

        # SAR
        sar = self.am5.sar(self.sar_af, self.sar_maxaf)

        # ATR分位缓存
        self.atr_list.append(atr)
        if len(self.atr_list) > self.atr_window:
            self.atr_list.pop(0)
        atr_quantile = np.percentile(self.atr_list, 20)

        # ========== 2. 4因子打分 ==========
        # 因子1：均线
        self.ma_score = 0.0
        if close > ma_l and ma_s > ma_l:
            # 金叉判断
            cross = (self.am5.sma(self.ma_short, 1)[-2] <= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s > ma_l)
            if cross:
                self.ma_score = 1.0
            else:
                self.ma_score = 0.5
        elif close < ma_l and ma_s < ma_l:
            cross = (self.am5.sma(self.ma_short, 1)[-2] >= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s < ma_l)
            if cross:
                self.ma_score = -1.0
            else:
                self.ma_score = -0.5
        else:
            self.ma_score = 0.0
        # 大周期过滤
        if self.ma_score > 0 and not self.long_bias:
            self.ma_score = 0
        if self.ma_score < 0 and not self.short_bias:
            self.ma_score = 0

        # 因子2：唐奇安
        self.don_score = 0.0
        if close > h20:
            self.don_score = 1.0
        elif close < l20:
            self.don_score = -1.0
        elif close > mid_don:
            self.don_score = 0.5
        elif close < mid_don:
            self.don_score = -0.5
        if self.don_score > 0 and not self.long_bias:
            self.don_score = 0
        if self.don_score < 0 and not self.short_bias:
            self.don_score = 0

        # 因子3：布林
        self.boll_score = 0.0
        if boll_slope > 0 and close > boll_mid:
            if close > boll_upper:
                self.boll_score = 1.0
            else:
                self.boll_score = 0.5
        elif boll_slope < 0 and close < boll_mid:
            if close < boll_lower:
                self.boll_score = -1.0
            else:
                self.boll_score = -0.5
        if self.boll_score > 0 and not self.long_bias:
            self.boll_score = 0
        if self.boll_score < 0 and not self.short_bias:
            self.boll_score = 0

        # 因子4：SAR
        self.sar_score = 0.0
        if sar < close:
            self.sar_score = 1.0
        elif sar > close:
            self.sar_score = -1.0
        if self.sar_score > 0 and not self.long_bias:
            self.sar_score = 0
        if self.sar_score < 0 and not self.short_bias:
            self.sar_score = 0

        # ========== 3. 总分 & 因子分歧 ==========
        self.total_score = (self.ma_score * self.w_ma +
                            self.don_score * self.w_don +
                            self.boll_score * self.w_boll +
                            self.sar_score * self.w_sar)
        scores_arr = np.array([self.ma_score, self.don_score, self.boll_score, self.sar_score])
        divergence = np.std(scores_arr)

        # ========== 4. 全局市场状态过滤器 ==========
        self.is_tradeable = True
        if adx < 20:
            self.is_tradeable = False
        if atr < atr_quantile:
            self.is_tradeable = False
        if divergence > 0.7:
            self.is_tradeable = False

        # ========== 5. 收盘强制清仓判断 ==========
        is_close_time = (bar.datetime.hour == 14 and bar.datetime.minute >= 60 - self.close_minute) or \
                        (bar.datetime.hour == 22 and bar.datetime.minute >= 60 - self.close_minute)
        if is_close_time:
            if self.pos > 0:
                self.sell(close, self.pos)
            elif self.pos < 0:
                self.cover(close, abs(self.pos))
            return

        # ========== 6. ATR动态仓位计算 ==========
        # 注意：vt_symbol 合约乘数需要你自己核对，示例这里预留，你要替换
        multiplier = self.get_contract().size
        risk_volume = (self.risk_per_trade * self.cta_engine.capital) / (atr * self.atr_stop_multiple * multiplier)
        volume = max(1, int(risk_volume))

        # ========== 7. 开仓逻辑 ==========
        if self.is_tradeable:
            # 开多
            if self.total_score > self.score_long_threshold and self.pos == 0:
                self.buy(close, volume)
                self.stop_loss_price = close - self.atr_stop_multiple * atr
            # 开空
            elif self.total_score < self.score_short_threshold and self.pos == 0:
                self.short(close, volume)
                self.stop_loss_price = close + self.atr_stop_multiple * atr

        # ========== 8. 移动止损跟踪 ==========
        if self.pos > 0:
            new_sl = close - self.atr_stop_multiple * atr
            if new_sl > self.stop_loss_price:
                self.stop_loss_price = new_sl
            if close <= self.stop_loss_price:
                self.sell(close, self.pos)
        elif self.pos < 0:
            new_sl = close + self.atr_stop_multiple * atr
            if new_sl < self.stop_loss_price:
                self.stop_loss_price = new_sl
            if close >= self.stop_loss_price:
                self.cover(close, abs(self.pos))

        # 信号反转平仓
        if self.pos > 0 and self.total_score < self.score_short_threshold:
            self.sell(close, self.pos)
        if self.pos < 0 and self.total_score > self.score_long_threshold:
            self.cover(close, abs(self.pos))

        self.put_event()

    def on_order(self, order: OrderData):
        pass

    def on_trade(self, trade: TradeData):
        self.put_event()

    def on_stop_order(self, so: StopOrder):
        pass
```

## 重要修改点（你必须自己调整）

1. `multiplier`：合约乘数，燃油FU是10，纯碱SA是20，玉米C是10；
2. `self.cta_engine.capital`：vnpy里默认资金读取，如果你用仿真，确认资金配置；
3. 收盘时间：国内期货日盘14:57、夜盘22:57，`close_minute=15`代表14:45、22:45开始清仓；
4. 跳空场景：这个框架没有额外写跳空过滤，可以在`on_5min_bar`开头增加开盘缺口判断；
5. 加仓：当前版本**不加仓**，如需加仓可以在总分>0.8时增加加仓分支。

## 回测建议

1. 先用燃油FU、纯碱SA，5分钟K，样本切分：前70%训练、后30%样本外；
2. 滑点：商品建议0.5~1个最小变动价位；手续费按期货公司标准；
3. 监控指标：每个因子单独输出得分，单独统计每个因子的盈亏，用来判断因子衰减；
4. 绩效监控：ADX、ATR分位、因子分歧三个过滤器的触发次数，统计过滤掉的信号胜率。

如果你需要，我可以再给你配套：

1. 回测绩效监控脚本（输出每个因子独立盈亏）
2. 增加自适应权重版本（根据最近N笔盈亏自动调整4因子权重）

本回答由AI生成，仅供参考，请仔细甄别，谨慎投资。

# 回测绩效监控脚本和自适应权重版本

# 前置说明

免责：仅学习研究，不构成实盘交易建议；自适应权重极易过拟合，优先在SimNow仿真跑，不要直接上实盘。  
两部分：  
① 因子独立盈亏监控模块（嵌入策略，每一笔成交记录是哪个因子贡献信号，回测后输出每个因子单独盈亏）  
② 自适应权重版本完整策略代码（基于最近N笔信号绩效动态调整4因子权重）  
底层沿用前面跨周期4因子框架：日线定方向、5min入场；因子：均线/唐奇安/布林/SAR

核心自适应逻辑：  
窗口长度 `lookback_trades = 20`（最近20笔该因子发出并成交的交易）  
对每个因子单独维护交易队列，计算**因子净盈亏**，归一化后分配权重；  
基础权重保底，设置权重上下限，防止单个因子权重吞噬全部；  
因子近期持续亏损 → 权重压低甚至归零；近期盈利好 → 权重抬升。

因子盈亏监控原理：  
开仓瞬间，记录当前4个因子各自得分，标记这笔交易的**主导因子集合**；平仓时，把这笔盈亏按因子得分加权分摊到4个因子上；每一笔盈亏都分摊到4个因子，最后汇总每个因子累计盈亏、胜率、交易次数。

---

## 一、公共数据结构说明（两个版本共用）

每一笔交易记录对象：

```
# 单条交易记录
class FactorTradeRecord:
    def __init__(self):
        self.trade_id = ""
        self.datetime = None
        self.direction = 0  # 1多 -1空
        self.ma_score = 0.0
        self.don_score = 0.0
        self.boll_score = 0.0
        self.sar_score = 0.0
        self.entry_price = 0.0
        self.exit_price = 0.0
        self.volume = 0
        self.pnl = 0.0  # 这笔总盈亏（不含手续费）
```

因子绩效容器：

```
# 因子绩效统计
factor_performance = {
    "ma": {"pnl":0, "count":0, "win":0, "trade_list":[]},
    "don": {"pnl":0, "count":0, "win":0, "trade_list":[]},
    "boll": {"pnl":0, "count":0, "win":0, "trade_list":[]},
    "sar": {"pnl":0, "count":0, "win":0, "trade_list":[]},
}
```

分摊规则：

![](https://cdn.nlark.com/yuque/__latex/3ee9c5462c83cf5656b5a0d996bbd06f.svg)

分母为0（全部0分）→ 不分摊，不计入因子盈亏。

---

# 二、版本A：【带因子独立盈亏监控】固定权重版（可直接回测导出因子盈亏）

```
from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
)
import numpy as np
from dataclasses import dataclass
from typing import List, Dict


@dataclass
class FactorTradeRecord:
    trade_id: str = ""
    datetime = None
    direction: int = 0
    ma_score: float = 0.0
    don_score: float = 0.0
    boll_score: float = 0.0
    sar_score: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    volume: int = 0
    pnl: float = 0.0


class MultiFactor4CrossCycleMonitorStrategy(CtaTemplate):
    """
    跨周期4因子CTA - 固定权重 + 因子独立盈亏监控
    大周期日线MA60过滤，小周期5min打分
    回测结束自动打印每个因子独立盈亏、胜率、交易次数
    """
    author = "study"
    # ========== 参数 ==========
    ma_short = 20
    ma_long = 60
    donchian_period = 20
    boll_len = 20
    boll_std = 2
    sar_af = 0.02
    sar_maxaf = 0.2
    atr_len = 14
    adx_len = 14
    atr_window = 60
    score_long_threshold = 0.6
    score_short_threshold = -0.6
    atr_stop_multiple = 2.0
    risk_per_trade = 0.01
    close_minute = 15

    # 固定权重
    w_ma = 0.3
    w_don = 0.25
    w_boll = 0.25
    w_sar = 0.2

    # 变量
    total_score = 0.0
    ma_score = 0.0
    don_score = 0.0
    boll_score = 0.0
    sar_score = 0.0
    is_tradeable = False
    long_bias = False
    short_bias = False
    stop_loss_price = 0.0

    # 因子监控
    trade_records: List[FactorTradeRecord]
    open_record: FactorTradeRecord = None  # 当前持仓对应的开仓记录

    parameters = [
        "ma_short", "ma_long", "donchian_period",
        "boll_len", "boll_std", "sar_af", "sar_maxaf",
        "atr_len", "adx_len", "atr_window",
        "score_long_threshold", "score_short_threshold",
        "atr_stop_multiple", "risk_per_trade", "close_minute"
    ]
    variables = [
        "total_score", "ma_score", "don_score", "boll_score", "sar_score",
        "is_tradeable", "long_bias", "short_bias", "stop_loss_price"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg5 = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am5 = ArrayManager(size=120)
        self.bg_day = BarGenerator(self.on_bar, 1440, self.on_day_bar)
        self.am_day = ArrayManager(size=100)
        self.atr_list = []
        self.trade_records = []
        self.open_record = None

    def on_init(self):
        self.write_log("策略初始化【带因子盈亏监控】")
        self.load_bar(10)

    def on_start(self):
        self.write_log("策略启动")

    def on_stop(self):
        # 策略停止时，输出因子绩效报表
        self.print_factor_report()
        self.write_log("策略停止")

    def on_tick(self, tick: TickData):
        self.bg5.update_tick(tick)
        self.bg_day.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg5.update_bar(bar)
        self.bg_day.update_bar(bar)

    def on_day_bar(self, bar: BarData):
        self.am_day.update_bar(bar)
        if not self.am_day.inited:
            return
        ma60_day = self.am_day.sma(self.ma_long)
        close_day = self.am_day.close[-1]
        self.long_bias = close_day > ma60_day
        self.short_bias = close_day < ma60_day

    def on_5min_bar(self, bar: BarData):
        self.cancel_all()
        self.am5.update_bar(bar)
        if not self.am5.inited or not self.am_day.inited:
            return

        close = self.am5.close[-1]
        atr = self.am5.atr(self.atr_len)
        adx = self.am5.adx(self.adx_len)
        ma_s = self.am5.sma(self.ma_short)
        ma_l = self.am5.sma(self.ma_long)

        # 唐奇安
        h20 = self.am5.high[-self.donchian_period:].max()
        l20 = self.am5.low[-self.donchian_period:].min()
        mid_don = (h20 + l20) / 2

        # 布林
        boll_mid = self.am5.sma(self.boll_len)
        boll_std_val = self.am5.std(self.boll_len)
        boll_upper = boll_mid + self.boll_std * boll_std_val
        boll_lower = boll_mid - self.boll_std * boll_std_val
        boll_slope = boll_mid - self.am5.sma(self.boll_len, 1)[-1]

        # SAR
        sar = self.am5.sar(self.sar_af, self.sar_maxaf)

        # ATR分位
        self.atr_list.append(atr)
        if len(self.atr_list) > self.atr_window:
            self.atr_list.pop(0)
        atr_quantile = np.percentile(self.atr_list, 20)

        # ========== 因子打分 ==========
        self.ma_score = 0.0
        if close > ma_l and ma_s > ma_l:
            cross = (self.am5.sma(self.ma_short, 1)[-2] <= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s > ma_l)
            self.ma_score = 1.0 if cross else 0.5
        elif close < ma_l and ma_s < ma_l:
            cross = (self.am5.sma(self.ma_short, 1)[-2] >= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s < ma_l)
            self.ma_score = -1.0 if cross else -0.5
        if self.ma_score > 0 and not self.long_bias: self.ma_score = 0
        if self.ma_score < 0 and not self.short_bias: self.ma_score = 0

        self.don_score = 0.0
        if close > h20: self.don_score = 1.0
        elif close < l20: self.don_score = -1.0
        elif close > mid_don: self.don_score = 0.5
        elif close < mid_don: self.don_score = -0.5
        if self.don_score > 0 and not self.long_bias: self.don_score = 0
        if self.don_score < 0 and not self.short_bias: self.don_score = 0

        self.boll_score = 0.0
        if boll_slope > 0 and close > boll_mid:
            self.boll_score = 1.0 if close > boll_upper else 0.5
        elif boll_slope < 0 and close < boll_mid:
            self.boll_score = -1.0 if close < boll_lower else -0.5
        if self.boll_score > 0 and not self.long_bias: self.boll_score = 0
        if self.boll_score < 0 and not self.short_bias: self.boll_score = 0

        self.sar_score = 0.0
        if sar < close: self.sar_score = 1.0
        elif sar > close: self.sar_score = -1.0
        if self.sar_score > 0 and not self.long_bias: self.sar_score = 0
        if self.sar_score < 0 and not self.short_bias: self.sar_score = 0

        # 总分 & 分歧
        self.total_score = self.ma_score*self.w_ma + self.don_score*self.w_don + self.boll_score*self.w_boll + self.sar_score*self.w_sar
        scores_arr = np.array([self.ma_score, self.don_score, self.boll_score, self.sar_score])
        divergence = np.std(scores_arr)

        # 全局过滤器
        self.is_tradeable = True
        if adx < 20: self.is_tradeable = False
        if atr < atr_quantile: self.is_tradeable = False
        if divergence > 0.7: self.is_tradeable = False

        # 收盘清仓
        is_close_time = (bar.datetime.hour == 14 and bar.datetime.minute >= 60 - self.close_minute) or \
                        (bar.datetime.hour == 22 and bar.datetime.minute >= 60 - self.close_minute)
        if is_close_time:
            if self.pos > 0:
                self.sell(close, self.pos)
            elif self.pos < 0:
                self.cover(close, abs(self.pos))
            return

        # 仓位计算
        contract = self.get_contract()
        if not contract:
            return
        multiplier = contract.size
        risk_volume = (self.risk_per_trade * self.cta_engine.capital) / (atr * self.atr_stop_multiple * multiplier)
        volume = max(1, int(risk_volume))

        # 开仓
        if self.is_tradeable and self.pos == 0:
            if self.total_score > self.score_long_threshold:
                self.buy(close, volume)
                self.stop_loss_price = close - self.atr_stop_multiple * atr
                # 记录开仓因子得分
                self.open_record = FactorTradeRecord(
                    trade_id=bar.datetime.strftime("%Y%m%d%H%M"),
                    datetime=bar.datetime,
                    direction=1,
                    ma_score=self.ma_score,
                    don_score=self.don_score,
                    boll_score=self.boll_score,
                    sar_score=self.sar_score,
                    entry_price=close,
                    volume=volume
                )
            elif self.total_score < self.score_short_threshold:
                self.short(close, volume)
                self.stop_loss_price = close + self.atr_stop_multiple * atr
                self.open_record = FactorTradeRecord(
                    trade_id=bar.datetime.strftime("%Y%m%d%H%M"),
                    datetime=bar.datetime,
                    direction=-1,
                    ma_score=self.ma_score,
                    don_score=self.don_score,
                    boll_score=self.boll_score,
                    sar_score=self.sar_score,
                    entry_price=close,
                    volume=volume
                )

        # 移动止损
        if self.pos > 0:
            new_sl = close - self.atr_stop_multiple * atr
            if new_sl > self.stop_loss_price:
                self.stop_loss_price = new_sl
            if close <= self.stop_loss_price:
                self.sell(close, self.pos)
        elif self.pos < 0:
            new_sl = close + self.atr_stop_multiple * atr
            if new_sl < self.stop_loss_price:
                self.stop_loss_price = new_sl
            if close >= self.stop_loss_price:
                self.cover(close, abs(self.pos))

        # 信号反转平仓
        if self.pos > 0 and self.total_score < self.score_short_threshold:
            self.sell(close, self.pos)
        if self.pos < 0 and self.total_score > self.score_long_threshold:
            self.cover(close, abs(self.pos))

        self.put_event()

    def on_trade(self, trade: TradeData):
        """成交回调：平仓时计算盈亏，分摊到因子"""
        if self.open_record is None:
            return
        rec = self.open_record
        contract = self.get_contract()
        if not contract:
            return
        mult = contract.size
        if trade.direction.value == "多平" or trade.direction.value == "空平":
            # 平仓成交
            rec.exit_price = trade.price
            if rec.direction == 1:
                pnl = (rec.exit_price - rec.entry_price) * rec.volume * mult
            else:
                pnl = (rec.entry_price - rec.exit_price) * rec.volume * mult
            rec.pnl = pnl
            self.trade_records.append(rec)
            self.open_record = None
        self.put_event()

    def print_factor_report(self):
        """输出因子独立盈亏报表"""
        factor_stats = {
            "ma": {"pnl":0.0, "count":0, "win":0},
            "don": {"pnl":0.0, "count":0, "win":0},
            "boll": {"pnl":0.0, "count":0, "win":0},
            "sar": {"pnl":0.0, "count":0, "win":0},
        }
        for r in self.trade_records:
            s_ma = abs(r.ma_score)
            s_don = abs(r.don_score)
            s_boll = abs(r.boll_score)
            s_sar = abs(r.sar_score)
            total_s = s_ma + s_don + s_boll + s_sar
            if total_s < 1e-6:
                continue
            p_ma = r.pnl * s_ma / total_s
            p_don = r.pnl * s_don / total_s
            p_boll = r.pnl * s_boll / total_s
            p_sar = r.pnl * s_sar / total_s

            factor_stats["ma"]["pnl"] += p_ma
            factor_stats["don"]["pnl"] += p_don
            factor_stats["boll"]["pnl"] += p_boll
            factor_stats["sar"]["pnl"] += p_sar

            factor_stats["ma"]["count"] +=1
            factor_stats["don"]["count"] +=1
            factor_stats["boll"]["count"] +=1
            factor_stats["sar"]["count"] +=1

            if p_ma > 0: factor_stats["ma"]["win"] +=1
            if p_don > 0: factor_stats["don"]["win"] +=1
            if p_boll > 0: factor_stats["boll"]["win"] +=1
            if p_sar > 0: factor_stats["sar"]["win"] +=1

        self.write_log("===== 因子独立绩效报告 =====")
        for name, st in factor_stats.items():
            if st["count"] == 0:
                wr = 0
            else:
                wr = st["win"] / st["count"]
            self.write_log(f"{name:4s} | 总盈亏:{st['pnl']:.2f} | 交易次数:{st['count']} | 盈利次数:{st['win']} | 胜率:{wr:.2%}")
```

---

# 三、版本B：自适应权重版本（带因子盈亏监控 + 动态权重）

自适应规则：

1. 每个因子单独维护最近 `lookback_trades=20` 笔分摊盈亏队列
2. 计算每个因子窗口内净盈亏 `score_p = sum(window_pnl)`
3. 对4个因子的 `score_p` 做归一化，得到原始权重
4. 保底最小权重 `min_weight=0.05`，上限 `max_weight=0.4`，总和强制归一到1
5. 因子近期持续亏损，窗口盈亏为负 → 权重压到min_weight
6. 权重每成交一笔才更新（不是每根K乱跳，防止权重震荡）

```
from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
)
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Deque
from collections import deque


@dataclass
class FactorTradeRecord:
    trade_id: str = ""
    datetime = None
    direction: int = 0
    ma_score: float = 0.0
    don_score: float = 0.0
    boll_score: float = 0.0
    sar_score: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    volume: int = 0
    pnl: float = 0.0


class MultiFactor4AdaptiveWeightStrategy(CtaTemplate):
    """
    跨周期4因子CTA：自适应权重 + 因子盈亏监控
    权重基于每个因子最近N笔分摊盈亏动态调整
    """
    author = "study"
    # 基础参数
    ma_short = 20
    ma_long = 60
    donchian_period = 20
    boll_len = 20
    boll_std = 2
    sar_af = 0.02
    sar_maxaf = 0.2
    atr_len = 14
    adx_len = 14
    atr_window = 60
    score_long_threshold = 0.6
    score_short_threshold = -0.6
    atr_stop_multiple = 2.0
    risk_per_trade = 0.01
    close_minute = 15

    # 自适应权重参数
    lookback_trades = 20    # 回看最近N笔交易
    min_weight = 0.05
    max_weight = 0.40

    # 当前权重
    w_ma = 0.25
    w_don = 0.25
    w_boll = 0.25
    w_sar = 0.25

    # 因子盈亏队列（每个因子独立）
    pnl_queue: Dict[str, Deque]
    trade_records: List[FactorTradeRecord]
    open_record: FactorTradeRecord = None

    # 打分变量
    total_score = 0.0
    ma_score = 0.0
    don_score = 0.0
    boll_score = 0.0
    sar_score = 0.0
    is_tradeable = False
    long_bias = False
    short_bias = False
    stop_loss_price = 0.0

    parameters = [
        "ma_short", "ma_long", "donchian_period",
        "boll_len", "boll_std", "sar_af", "sar_maxaf",
        "atr_len", "adx_len", "atr_window",
        "score_long_threshold", "score_short_threshold",
        "atr_stop_multiple", "risk_per_trade", "close_minute",
        "lookback_trades", "min_weight", "max_weight"
    ]
    variables = [
        "total_score", "ma_score", "don_score", "boll_score", "sar_score",
        "is_tradeable", "long_bias", "short_bias", "stop_loss_price",
        "w_ma", "w_don", "w_boll", "w_sar"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg5 = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am5 = ArrayManager(size=120)
        self.bg_day = BarGenerator(self.on_bar, 1440, self.on_day_bar)
        self.am_day = ArrayManager(size=100)
        self.atr_list = []
        self.trade_records = []
        self.open_record = None
        self.pnl_queue = {
            "ma": deque(maxlen=self.lookback_trades),
            "don": deque(maxlen=self.lookback_trades),
            "boll": deque(maxlen=self.lookback_trades),
            "sar": deque(maxlen=self.lookback_trades),
        }

    def on_init(self):
        self.write_log("策略初始化【自适应权重版本】")
        self.load_bar(10)

    def on_start(self):
        self.write_log("策略启动")

    def on_stop(self):
        self.print_factor_report()
        self.write_log("策略停止")

    def on_tick(self, tick: TickData):
        self.bg5.update_tick(tick)
        self.bg_day.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg5.update_bar(bar)
        self.bg_day.update_bar(bar)

    def on_day_bar(self, bar: BarData):
        self.am_day.update_bar(bar)
        if not self.am_day.inited:
            return
        ma60_day = self.am_day.sma(self.ma_long)
        close_day = self.am_day.close[-1]
        self.long_bias = close_day > ma60_day
        self.short_bias = close_day < ma60_day

    def update_adaptive_weight(self):
        """根据队列盈亏，更新自适应权重"""
        p_ma = sum(self.pnl_queue["ma"])
        p_don = sum(self.pnl_queue["don"])
        p_boll = sum(self.pnl_queue["boll"])
        p_sar = sum(self.pnl_queue["sar"])
        arr = np.array([p_ma, p_don, p_boll, p_sar])
        # 把负盈亏置0，亏损因子不加分
        arr = np.clip(arr, 0, None)
        s = arr.sum()
        if s < 1e-6:
            # 全部亏损，均等权重
            self.w_ma = self.w_don = self.w_boll = self.w_sar = 0.25
            return
        raw = arr / s
        # 截断上下限
        raw = np.clip(raw, self.min_weight, self.max_weight)
        # 再次归一化，保证总和=1
        raw = raw / raw.sum()
        self.w_ma, self.w_don, self.w_boll, self.w_sar = raw[0], raw[1], raw[2], raw[3]

    def on_5min_bar(self, bar: BarData):
        self.cancel_all()
        self.am5.update_bar(bar)
        if not self.am5.inited or not self.am_day.inited:
            return

        close = self.am5.close[-1]
        atr = self.am5.atr(self.atr_len)
        adx = self.am5.adx(self.adx_len)
        ma_s = self.am5.sma(self.ma_short)
        ma_l = self.am5.sma(self.ma_long)

        h20 = self.am5.high[-self.donchian_period:].max()
        l20 = self.am5.low[-self.donchian_period:].min()
        mid_don = (h20 + l20) / 2

        boll_mid = self.am5.sma(self.boll_len)
        boll_std_val = self.am5.std(self.boll_len)
        boll_upper = boll_mid + self.boll_std * boll_std_val
        boll_lower = boll_mid - self.boll_std * boll_std_val
        boll_slope = boll_mid - self.am5.sma(self.boll_len, 1)[-1]
        sar = self.am5.sar(self.sar_af, self.sar_maxaf)

        self.atr_list.append(atr)
        if len(self.atr_list) > self.atr_window:
            self.atr_list.pop(0)
        atr_quantile = np.percentile(self.atr_list, 20)

        # ========== 因子打分 ==========
        self.ma_score = 0.0
        if close > ma_l and ma_s > ma_l:
            cross = (self.am5.sma(self.ma_short, 1)[-2] <= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s > ma_l)
            self.ma_score = 1.0 if cross else 0.5
        elif close < ma_l and ma_s < ma_l:
            cross = (self.am5.sma(self.ma_short, 1)[-2] >= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s < ma_l)
            self.ma_score = -1.0 if cross else -0.5
        if self.ma_score > 0 and not self.long_bias: self.ma_score = 0
        if self.ma_score < 0 and not self.short_bias: self.ma_score = 0

        self.don_score = 0.0
        if close > h20: self.don_score = 1.0
        elif close < l20: self.don_score = -1.0
        elif close > mid_don: self.don_score = 0.5
        elif close < mid_don: self.don_score = -0.5
        if self.don_score > 0 and not self.long_bias: self.don_score = 0
        if self.don_score < 0 and not self.short_bias: self.don_score = 0

        self.boll_score = 0.0
        if boll_slope > 0 and close > boll_mid:
            self.boll_score = 1.0 if close > boll_upper else 0.5
        elif boll_slope < 0 and close < boll_mid:
            self.boll_score = -1.0 if close < boll_lower else -0.5
        if self.boll_score > 0 and not self.long_bias: self.boll_score = 0
        if self.boll_score < 0 and not self.short_bias: self.boll_score = 0

        self.sar_score = 0.0
        if sar < close: self.sar_score = 1.0
        elif sar > close: self.sar_score = -1.0
        if self.sar_score > 0 and not self.long_bias: self.sar_score = 0
        if self.sar_score < 0 and not self.short_bias: self.sar_score = 0

        # 总分
        self.total_score = self.ma_score*self.w_ma + self.don_score*self.w_don + self.boll_score*self.w_boll + self.sar_score*self.w_sar
        scores_arr = np.array([self.ma_score, self.don_score, self.boll_score, self.sar_score])
        divergence = np.std(scores_arr)

        self.is_tradeable = True
        if adx < 20: self.is_tradeable = False
        if atr < atr_quantile: self.is_tradeable = False
        if divergence > 0.7: self.is_tradeable = False

        # 收盘清仓
        is_close_time = (bar.datetime.hour == 14 and bar.datetime.minute >= 60 - self.close_minute) or \
                        (bar.datetime.hour == 22 and bar.datetime.minute >= 60 - self.close_minute)
        if is_close_time:
            if self.pos > 0:
                self.sell(close, self.pos)
            elif self.pos < 0:
                self.cover(close, abs(self.pos))
            return

        contract = self.get_contract()
        if not contract:
            return
        multiplier = contract.size
        risk_volume = (self.risk_per_trade * self.cta_engine.capital) / (atr * self.atr_stop_multiple * multiplier)
        volume = max(1, int(risk_volume))

        if self.is_tradeable and self.pos == 0:
            if self.total_score > self.score_long_threshold:
                self.buy(close, volume)
                self.stop_loss_price = close - self.atr_stop_multiple * atr
                self.open_record = FactorTradeRecord(
                    trade_id=bar.datetime.strftime("%Y%m%d%H%M"),
                    datetime=bar.datetime,
                    direction=1,
                    ma_score=self.ma_score,
                    don_score=self.don_score,
                    boll_score=self.boll_score,
                    sar_score=self.sar_score,
                    entry_price=close,
                    volume=volume
                )
            elif self.total_score < self.score_short_threshold:
                self.short(close, volume)
                self.stop_loss_price = close + self.atr_stop_multiple * atr
                self.open_record = FactorTradeRecord(
                    trade_id=bar.datetime.strftime("%Y%m%d%H%M"),
                    datetime=bar.datetime,
                    direction=-1,
                    ma_score=self.ma_score,
                    don_score=self.don_score,
                    boll_score=self.boll_score,
                    sar_score=self.sar_score,
                    entry_price=close,
                    volume=volume
                )

        if self.pos > 0:
            new_sl = close - self.atr_stop_multiple * atr
            if new_sl > self.stop_loss_price:
                self.stop_loss_price = new_sl
            if close <= self.stop_loss_price:
                self.sell(close, self.pos)
        elif self.pos < 0:
            new_sl = close + self.atr_stop_multiple * atr
            if new_sl < self.stop_loss_price:
                self.stop_loss_price = new_sl
            if close >= self.stop_loss_price:
                self.cover(close, abs(self.pos))

        if self.pos > 0 and self.total_score < self.score_short_threshold:
            self.sell(close, self.pos)
        if self.pos < 0 and self.total_score > self.score_long_threshold:
            self.cover(close, abs(self.pos))

        self.put_event()

    def on_trade(self, trade: TradeData):
        if self.open_record is None:
            return
        rec = self.open_record
        contract = self.get_contract()
        if not contract:
            return
        mult = contract.size
        if trade.direction.value in ("多平", "空平"):
            rec.exit_price = trade.price
            if rec.direction == 1:
                pnl = (rec.exit_price - rec.entry_price) * rec.volume * mult
            else:
                pnl = (rec.entry_price - rec.exit_price) * rec.volume * mult
            rec.pnl = pnl
            self.trade_records.append(rec)
            # 分摊盈亏，压入因子队列
            s_ma = abs(rec.ma_score)
            s_don = abs(rec.don_score)
            s_boll = abs(rec.boll_score)
            s_sar = abs(rec.sar_score)
            total_s = s_ma + s_don + s_boll + s_sar
            if total_s > 1e-6:
                self.pnl_queue["ma"].append(pnl * s_ma / total_s)
                self.pnl_queue["don"].append(pnl * s_don / total_s)
                self.pnl_queue["boll"].append(pnl * s_boll / total_s)
                self.pnl_queue["sar"].append(pnl * s_sar / total_s)
                # 平仓成交后更新权重
                self.update_adaptive_weight()
            self.open_record = None
        self.put_event()

    def print_factor_report(self):
        factor_stats = {
            "ma": {"pnl":0.0, "count":0, "win":0},
            "don": {"pnl":0.0, "count":0, "win":0},
            "boll": {"pnl":0.0, "count":0, "win":0},
            "sar": {"pnl":0.0, "count":0, "win":0},
        }
        for r in self.trade_records:
            s_ma = abs(r.ma_score)
            s_don = abs(r.don_score)
            s_boll = abs(r.boll_score)
            s_sar = abs(r.sar_score)
            total_s = s_ma + s_don + s_boll + s_sar
            if total_s < 1e-6:
                continue
            p_ma = r.pnl * s_ma / total_s
            p_don = r.pnl * s_don / total_s
            p_boll = r.pnl * s_boll / total_s
            p_sar = r.pnl * s_sar / total_s

            factor_stats["ma"]["pnl"] += p_ma
            factor_stats["don"]["pnl"] += p_don
            factor_stats["boll"]["pnl"] += p_boll
            factor_stats["sar"]["pnl"] += p_sar

            factor_stats["ma"]["count"] +=1
            factor_stats["don"]["count"] +=1
            factor_stats["boll"]["count"] +=1
            factor_stats["sar"]["count"] +=1

            if p_ma > 0: factor_stats["ma"]["win"] +=1
            if p_don > 0: factor_stats["don"]["win"] +=1
            if p_boll > 0: factor_stats["boll"]["win"] +=1
            if p_sar > 0: factor_stats["sar"]["win"] +=1

        self.write_log("===== 自适应权重 - 因子独立绩效报告 =====")
        self.write_log(f"当前权重：MA={self.w_ma:.3f} DON={self.w_don:.3f} BOLL={self.w_boll:.3f} SAR={self.w_sar:.3f}")
        for name, st in factor_stats.items():
            if st["count"] == 0:
                wr = 0
            else:
                wr = st["win"] / st["count"]
            self.write_log(f"{name:4s} | 总盈亏:{st['pnl']:.2f} | 交易次数:{st['count']} | 盈利次数:{st['win']} | 胜率:{wr:.2%}")
```

## 使用&调参建议

1. 固定权重版本优先用来做基准回测，看4个因子本身的原生能力；
2. 自适应版本：`lookback_trades=20` 不要设太小（比如5），极易权重来回震荡、过拟合；
3. 每次平仓才更新权重，而不是每根K，避免权重噪声；
4. 监控重点：日志里打印的**当前权重**，观察市场风格切换时权重是否合理迁移；
5. 回测对比：同一份K线，跑固定权重、自适应权重两组，对比夏普、最大回撤、因子衰减曲线；
6. 缺陷提醒：自适应是“后视镜”，因子刚盈利就拉高权重，风格突然反转会连续亏损，一定要搭配ADX+ATR全局过滤器。

如果你下一步需要，我可以再加：

- 日志输出csv导出（把每一笔交易、因子得分、权重写入csv，方便用pandas画图分析因子衰减曲线）
- 增加单品种最大回撤熔断开关（回撤到阈值直接锁仓停信号）
