说明
两个都给你：
1. vnpy 简化Casper（Jesse Rogers）策略代码：仅K线+成交量，无订单流、无足迹，适配国内CTP，5分钟周期，燃油/纯碱/玉米日内，收盘强制平仓；是阉割可实盘版本，去掉Absorption、CVD，改用成交量、波动替代过滤。
风险提示：简化版缺少原版订单流吸收确认，假扫流动性无法有效识别，胜率低于原版，仅适合仿真先跑，小资金实盘严格风控。
2. ATAS 标记清单+指标配置模板（原版Jesse Casper），可以直接在ATAS照着搭建，带VA、CVD、流动性池、FVG、OB标记。

一、vnpy 简化Casper策略（vnpy2.x，CTP可用）
逻辑对齐Jesse核心骨架：
● HTF偏见：15分钟周期判断方向（替代人工高框架）
● 流动性扫荡（Sweep）：价格刺破高低点，影线刺穿
● 过滤：放量（当前K成交量>近期均值）替代订单流吸收
● ChoCH结构转变 + Displacement位移（大实体K线）
● 回踩OB/FVG区域限价入场
● 止损：流动性外侧；止盈：下一个流动性高低点；盈亏比≥3:1
● 日内，收盘前平仓，不隔夜
● 仅5min执行K线，15min做偏见
from vnpy.trader.constant import Direction, Offset, Interval
from vnpy.trader.object import BarData, TickData
from vnpy.trader.utility import BarGenerator, ArrayManager
from vnpy.trader.strategy import Strategy, StopOrder
import numpy as np


class CasperSimpleStrategy(Strategy):
    """"
    Jesse Rogers Casper SMC 简化版
    无订单流，K线+成交量过滤，5min执行，15min定偏见
    适用：国内期货日内，燃油/纯碱/玉米，CTP
    """
    author = "CasperSimplify"
    parameters = [
        ("htf_period", 15),          # 高时间框架分钟
        ("lookback_liq", 20),        # 流动性高低点回溯K线数量
        ("vol_ma_len", 20),          # 成交量均值周期
        ("vol_multiplier", 1.3),     # 放量阈值：当前量>均值*倍数
        ("displacement_body_ratio", 0.6), # 位移K实体占比
        ("min_rr", 3.0),             # 最低盈亏比
        ("close_offset", 10),        # 收盘提前平仓分钟
    ]
    variables = [
        ("bias", 0), # 1多头偏见 -1空头偏见 0无偏见
    ]

    def __init__(self, engine, setting):
        super().__init__(engine, setting)
        self.bg5 = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am5 = ArrayManager(100)
        self.bg15 = BarGenerator(self.on_bar, 15, self.on_15min_bar)
        self.am15 = ArrayManager(100)

        self.long_liq = None
        self.short_liq = None
        self.entry_zone_top = None
        self.entry_zone_btm = None
        self.stop_price = None
        self.target_price = None
        self.has_signal = False
        self.trade_direction = 0

    def on_tick(self, tick: TickData):
        self.bg5.update_tick(tick)
        self.bg15.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg5.update_bar(bar)
        self.bg15.update_bar(bar)

    def on_15min_bar(self, bar: BarData):
        self.am15.update_bar(bar)
        if not self.am15.inited:
            return
        # 高时间框架偏见：简单结构判断，高点抬高=多头偏见，低点降低=空头
        highs = self.am15.high[-20:]
        lows = self.am15.low[-20:]
        hh = np.max(highs[-10:])
        lh = np.max(highs[-20:-10])
        ll = np.min(lows[-10:])
        hl = np.min(lows[-20:-10])
        if hh > lh and ll > hl:
            self.bias = 1
        elif ll < hl and hh < lh:
            self.bias = -1
        else:
            self.bias = 0

    def on_5min_bar(self, bar: BarData):
        self.am5.update_bar(bar)
        if not self.am5.inited:
            return
        # 收盘前平仓逻辑
        if self.check_close_time(bar):
            self.close_all()
            return

        vol_ma = self.am5.volume.mean(self.vol_ma_len)
        vol_condition = bar.volume > vol_ma * self.vol_multiplier

        high_array = self.am5.high[-self.lookback_liq:]
        low_array = self.am5.low[-self.lookback_liq:]
        swing_high = np.max(high_array[:-1])
        swing_low = np.min(low_array[:-1])

        # ========== 多头流程：偏见向上，扫下方流动性，放量位移 ==========
        if self.bias == 1:
            # Sweep：K下影刺破swing_low
            if bar.low < swing_low and bar.close > swing_low and vol_condition:
                self.long_liq = swing_low
                # Displacement：实体占比大的阳线
                body = bar.close - bar.open
                total_range = bar.high - bar.low
                if total_range > 0 and body / total_range > self.displacement_body_ratio and body>0:
                    # 简单OB：这根K的开盘-低点作为回踩入场区
                    self.entry_zone_btm = bar.low
                    self.entry_zone_top = bar.open
                    self.stop_price = self.long_liq - (bar.high - bar.low)*0.3
                    self.target_price = swing_high
                    rr = (self.target_price - self.entry_zone_top) / (self.entry_zone_top - self.stop_price)
                    if rr >= self.min_rr:
                        self.has_signal = True
                        self.trade_direction = 1
        # ========== 空头流程：偏见向下，扫上方流动性，放量位移 ==========
        elif self.bias == -1:
            if bar.high > swing_high and bar.close < swing_high and vol_condition:
                self.short_liq = swing_high
                body = bar.open - bar.close
                total_range = bar.high - bar.low
                if total_range>0 and body/total_range>self.displacement_body_ratio and body>0:
                    self.entry_zone_top = bar.high
                    self.entry_zone_btm = bar.open
                    self.stop_price = self.short_liq + (bar.high - bar.low)*0.3
                    self.target_price = swing_low
                    rr = (self.entry_zone_btm - self.target_price) / (self.stop_price - self.entry_zone_btm)
                    if rr >= self.min_rr:
                        self.has_signal = True
                        self.trade_direction = -1

        # 限价挂单：回踩入场区
        if self.has_signal:
            if self.trade_direction == 1:
                if self.entry_zone_btm <= bar.close <= self.entry_zone_top:
                    self.buy(bar.close, 1, stop=False)
                    self.has_signal = False
            elif self.trade_direction == -1:
                if self.entry_zone_btm <= bar.close <= self.entry_zone_top:
                    self.short(bar.close,1, stop=False)
                    self.has_signal = False

        # 持仓管理：止盈止损
        if self.pos > 0:
            if bar.low <= self.stop_price:
                self.sell(bar.low, self.pos, stop=True)
            if bar.high >= self.target_price:
                self.sell(bar.high, self.pos, stop=True)
        elif self.pos <0:
            if bar.high >= self.stop_price:
                self.cover(bar.high, abs(self.pos), stop=True)
            if bar.low <= self.target_price:
                self.cover(bar.low, abs(self.pos), stop=True)

    def check_close_time(self, bar:BarData):
        """国内期货收盘前提前平仓"""
        # 日盘 14:55，夜盘22:55（燃油等），你可按需修改
        h, m = bar.datetime.hour, bar.datetime.minute
        if (h == 14 and m >= 60 - self.close_offset) or (h == 22 and m >= 60 - self.close_offset):
            return True
        return False

    def close_all(self):
        if self.pos > 0:
            self.sell(self.last_price, self.pos)
        elif self.pos <0:
            self.cover(self.last_price, abs(self.pos))
        self.has_signal = False
使用要点（vnpy）
1. 周期：订阅5分钟K线，引擎自动合成15min偏见
2. 先在SimNow仿真跑至少2周，优先燃油、纯碱
3. 参数微调：
  ○ vol_multiplier：震荡行情调到1.5，趋势行情1.2
  ○ lookback_liq：20是默认，波动大品种改成25
4. 缺陷：没有CVD、没有吸收判断，震荡区间假扫会频繁触发信号，必须搭配人工过滤：如果价格在VA区间内，直接不做（这个版本代码没有自动VA，需要你人工标记VA）
如果你用vnpy3，我可以再给一份适配vnpy3的改写版本。

二、ATAS Jesse Casper 全套指标模板 + 标记清单（原版Jesse）
这是抖音视频里那套完整版本，你在ATAS直接照着新建图表模板，5min图执行，15min/日线看偏见
图表基础设置
● 主图周期：5min
● 更高时间框架：15min（偏见）、D（日VA）
● 数据源：CTP逐笔行情（必须开通逐笔，只有K线不行）
● 模板保存名称：Casper-Jesse-Rogers
1. 必须添加指标列表
指标	参数	用途
Volume Profile（成交量分布）	Session = Previous Session（前一个交易时段）	VA价值区、VAH、VAL、VPOC
Cumulative Delta Volume（CVD累计Delta）	默认，和主图对齐	资金方向、CVD背离过滤
Footprint（足迹图）	Cluster Footprint	看Absorption吸收、扫流动性时承接
Market Profile（可选）	和VP配套	拍卖结构辅助
FVG 公允价值缺口（ATAS自带FVG指标）	MinGap=3ticks	自动标记失衡缺口
Order Block（OB指标）	Jesse标准：位移前库存K线	回踩入场区标记
2. 绘图标记清单（手动标记，Jesse习惯，视频里的标记逻辑）
① 流动性池 Liquidity Pool（黄色虚线）
● Swing High 上方流动性：多头止损挂单区
● Swing Low 下方流动性：空头止损挂单区
✅标记规则：只标记清晰、多次测试的高低点，小毛刺忽略
标注简写：LIQ-H / LIQ-L
② VA价值区（前一日VP）
● VAH：Value Area High 红色实线
● VAL：Value Area Low 绿色实线
● VPOC：成交量控制点，加粗横线
规则：价格在VAH~VAL之间 = 平衡市场，不交易趋势单；价格跑出VA才是失衡行情，才允许找机会
③ ChoCH 结构转变（蓝色箭头）
● 上涨ChoCH：下跌打破前低 → 箭头向下
● 下跌ChoCH：上涨打破前高 → 箭头向上
只有ChoCH之后出现干净Displacement才有效，震荡里的ChoCH直接忽略
④ Displacement 位移K（红/绿粗框K线）
● 实体大、快速离开、伴随放量Delta同向
● 位移之后拉出FVG
⑤ FVG 公允缺口（浅色块填充）
● 看涨FVG：上方缺口（做多回踩区）
● 看跌FVG：下方缺口（做空回踩区）
⑥ OB订单块（色块填充）
● 看涨OB：位移启动前那根阴线库存K
● 看跌OB：位移启动前那根阳线库存K
Jesse优先OB，其次FVG作为入场位置
3. ATAS 交易检查清单（开单前逐条核对，对应视频）
✅ 高时间框架偏见确定方向
✅ 价格脱离VA（失衡，不是平衡）
✅ 价格扫对应方向流动性池（Sweep）
✅ 足迹图看到Absorption承接（扫影线位置大单吃单）
✅ 出现ChoCH，紧接着干净Displacement
✅ CVD和价格同向，无CVD背离
✅ 回踩OB/FVG
✅ 盈亏比≥3:1
✅ 在交易窗口（国内：夜盘21:00–22:30 / 日盘09:00–10:00）
❌ 任意一条不满足 → 放弃
4. ATAS 颜色配色建议（和Jesse原版风格对齐）
● LIQ流动性：黄色虚线
● VAH：红色；VAL：绿色；VPOC：白色粗线
● 看涨OB：淡绿色填充；看跌OB：淡红色填充
● FVG：浅灰半透明填充
● CVD：绿色（正Delta）红色（负Delta）
5. 导出提示
ATAS不支持直接导入外部指标模板文件，只能按上面清单手动新建一套，保存为Chart Template，以后打开品种一键加载这套标记和指标。

下一步可选
1. 我给你 vnpy版本增加VA自动计算（成交量分布）模块，让简化版自动识别平衡/失衡，补上最大短板