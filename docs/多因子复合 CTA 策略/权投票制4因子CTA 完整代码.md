# 权投票制4因子CTA 完整代码

说明：

1. 完全对齐你之前的跨周期架构：**日线MA60大周期过滤 + 5分钟小周期信号**，4个因子与打分版完全对应，方便你控制变量对比两种合成方式
2. 纯等权投票：每个因子独立输出 `1=投多 / -1=投空 / 0=弃权`，最低2票同向且票数占优才开仓
3. 保留全套风控：ATR动态仓位、2ATR移动止损、收盘强制清仓、ADX+ATR分位+因子分歧三层全局过滤
4. 内置投票制专属归因：按「投票方向与交易盈亏匹配度」统计每个因子的独立绩效，配套pandas衰减绘图

---

# 一、vnpy 等权投票制4因子CTA 完整代码

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
from vnpy.trader.constant import Offset
import numpy as np
from dataclasses import dataclass
from typing import List
from collections import deque


@dataclass
class VoteTradeRecord:
    """投票制交易记录，记录开仓时每个因子的投票结果"""
    trade_id: str = ""
    datetime = None
    direction: int = 0  # 1多 -1空
    v_ma: int = 0
    v_don: int = 0
    v_boll: int = 0
    v_sar: int = 0
    entry_price: float = 0.0
    exit_price: float = 0.0
    volume: int = 0
    pnl: float = 0.0


class MultiFactor4VotingStrategy(CtaTemplate):
    """
    4因子等权投票制CTA策略（跨周期）
    因子：均线趋势 / 唐奇安突破 / 布林突破 / SAR转向
    规则：每个因子独立投票(多/空/弃权)，满足最低票数门槛才开仓
    """
    author = "study"

    # ========== 策略参数 ==========
    # 均线因子
    ma_short = 20
    ma_long = 60
    # 唐奇安突破
    donchian_period = 20
    # 布林带
    boll_len = 20
    boll_std = 2
    # SAR
    sar_af = 0.02
    sar_maxaf = 0.2
    # 基础指标
    atr_len = 14
    adx_len = 14
    atr_window = 60
    # 投票规则
    min_votes = 2  # 最低同向票数门槛
    # 风控
    atr_stop_multiple = 2.0
    risk_per_trade = 0.01
    close_minute = 15

    # ========== 策略变量 ==========
    # 单因子投票结果
    v_ma = 0
    v_don = 0
    v_boll = 0
    v_sar = 0
    # 汇总票数
    long_votes = 0
    short_votes = 0
    # 全局开关
    is_tradeable = False
    long_bias = False
    short_bias = False
    # 止损价
    stop_loss_price = 0.0

    # 交易记录与归因
    trade_records: List[VoteTradeRecord]
    open_record: VoteTradeRecord = None

    parameters = [
        "ma_short", "ma_long", "donchian_period",
        "boll_len", "boll_std", "sar_af", "sar_maxaf",
        "atr_len", "adx_len", "atr_window",
        "min_votes", "atr_stop_multiple", "risk_per_trade", "close_minute"
    ]
    variables = [
        "v_ma", "v_don", "v_boll", "v_sar",
        "long_votes", "short_votes",
        "is_tradeable", "long_bias", "short_bias",
        "stop_loss_price"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # 5分钟K线 + 日线K线 双周期
        self.bg5 = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am5 = ArrayManager(size=120)
        self.bg_day = BarGenerator(self.on_bar, 1440, self.on_day_bar)
        self.am_day = ArrayManager(size=100)

        self.atr_list = []
        self.trade_records = []
        self.open_record = None

    def on_init(self):
        self.write_log("策略初始化：4因子等权投票制CTA")
        self.load_bar(10)

    def on_start(self):
        self.write_log("策略启动")

    def on_stop(self):
        # 停止时输出因子绩效报告
        self.print_vote_factor_report()
        self.export_trade_csv()
        self.write_log("策略停止")

    def on_tick(self, tick: TickData):
        self.bg5.update_tick(tick)
        self.bg_day.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg5.update_bar(bar)
        self.bg_day.update_bar(bar)

    def on_day_bar(self, bar: BarData):
        """日线大周期：多空方向过滤，一票否决反向信号"""
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

        # 唐奇安通道
        h20 = self.am5.high[-self.donchian_period:].max()
        l20 = self.am5.low[-self.donchian_period:].min()

        # 布林带
        boll_mid = self.am5.sma(self.boll_len)
        boll_std_val = self.am5.std(self.boll_len)
        boll_upper = boll_mid + self.boll_std * boll_std_val
        boll_lower = boll_mid - self.boll_std * boll_std_val
        boll_slope = boll_mid - self.am5.sma(self.boll_len, 1)[-1]

        # SAR
        sar = self.am5.sar(self.sar_af, self.sar_maxaf)

        # ATR历史分位
        self.atr_list.append(atr)
        if len(self.atr_list) > self.atr_window:
            self.atr_list.pop(0)
        atr_quantile = np.percentile(self.atr_list, 20)

        # ========== 1. 4因子独立投票 ==========
        # 因子1：均线趋势（金叉/死叉投票，缠绕弃权）
        self.v_ma = 0
        ma_cross_up = (self.am5.sma(self.ma_short, 1)[-2] <= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s > ma_l)
        ma_cross_down = (self.am5.sma(self.ma_short, 1)[-2] >= self.am5.sma(self.ma_long, 1)[-2]) and (ma_s < ma_l)
        if ma_cross_up and close > ma_l and self.long_bias:
            self.v_ma = 1
        elif ma_cross_down and close < ma_l and self.short_bias:
            self.v_ma = -1

        # 因子2：唐奇安突破
        self.v_don = 0
        if close > h20 and self.long_bias:
            self.v_don = 1
        elif close < l20 and self.short_bias:
            self.v_don = -1

        # 因子3：布林带突破（中轨同向才有效）
        self.v_boll = 0
        if boll_slope > 0 and close > boll_upper and self.long_bias:
            self.v_boll = 1
        elif boll_slope < 0 and close < boll_lower and self.short_bias:
            self.v_boll = -1

        # 因子4：SAR转向
        self.v_sar = 0
        if sar < close and self.long_bias:
            self.v_sar = 1
        elif sar > close and self.short_bias:
            self.v_sar = -1

        # ========== 2. 票数统计 ==========
        vote_list = [self.v_ma, self.v_don, self.v_boll, self.v_sar]
        self.long_votes = sum(1 for v in vote_list if v == 1)
        self.short_votes = sum(1 for v in vote_list if v == -1)

        # 因子分歧度（票数差越小，分歧越大）
        vote_diff = abs(self.long_votes - self.short_votes)

        # ========== 3. 全局市场状态过滤 ==========
        self.is_tradeable = True
        if adx < 20:          # 趋势强度不足
            self.is_tradeable = False
        if atr < atr_quantile:  # 波动率过低
            self.is_tradeable = False
        if vote_diff < 2:     # 因子分歧过大（票数差<2）
            self.is_tradeable = False

        # ========== 4. 收盘强制清仓 ==========
        is_close_time = (
            (bar.datetime.hour == 14 and bar.datetime.minute >= 60 - self.close_minute)
            or (bar.datetime.hour == 22 and bar.datetime.minute >= 60 - self.close_minute)
        )
        if is_close_time:
            if self.pos > 0:
                self.sell(close, self.pos)
            elif self.pos < 0:
                self.cover(close, abs(self.pos))
            return

        # ========== 5. ATR动态仓位计算 ==========
        contract = self.get_contract()
        if not contract:
            return
        multiplier = contract.size
        risk_volume = (self.risk_per_trade * self.cta_engine.capital) / (atr * self.atr_stop_multiple * multiplier)
        volume = max(1, int(risk_volume))

        # ========== 6. 开仓逻辑（投票制） ==========
        if self.is_tradeable and self.pos == 0:
            # 多头：满足最低票数，且多票数 > 空票数
            if self.long_votes >= self.min_votes and self.long_votes > self.short_votes:
                self.buy(close, volume)
                self.stop_loss_price = close - self.atr_stop_multiple * atr
                self.open_record = VoteTradeRecord(
                    trade_id=bar.datetime.strftime("%Y%m%d%H%M"),
                    datetime=bar.datetime,
                    direction=1,
                    v_ma=self.v_ma,
                    v_don=self.v_don,
                    v_boll=self.v_boll,
                    v_sar=self.v_sar,
                    entry_price=close,
                    volume=volume
                )
            # 空头：满足最低票数，且空票数 > 多票数
            elif self.short_votes >= self.min_votes and self.short_votes > self.long_votes:
                self.short(close, volume)
                self.stop_loss_price = close + self.atr_stop_multiple * atr
                self.open_record = VoteTradeRecord(
                    trade_id=bar.datetime.strftime("%Y%m%d%H%M"),
                    datetime=bar.datetime,
                    direction=-1,
                    v_ma=self.v_ma,
                    v_don=self.v_don,
                    v_boll=self.v_boll,
                    v_sar=self.v_sar,
                    entry_price=close,
                    volume=volume
                )

        # ========== 7. 移动止损 ==========
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

        # ========== 8. 投票反转平仓 ==========
        if self.pos > 0 and self.short_votes >= self.min_votes and self.short_votes > self.long_votes:
            self.sell(close, self.pos)
        if self.pos < 0 and self.long_votes >= self.min_votes and self.long_votes > self.short_votes:
            self.cover(close, abs(self.pos))

        self.put_event()

    def on_trade(self, trade: TradeData):
        """成交回调：平仓时计算盈亏，计入因子绩效"""
        if self.open_record is None:
            return
        rec = self.open_record
        contract = self.get_contract()
        if not contract:
            return

        # 仅处理平仓成交
        if trade.offset == Offset.CLOSE:
            rec.exit_price = trade.price
            mult = contract.size
            if rec.direction == 1:
                pnl = (rec.exit_price - rec.entry_price) * rec.volume * mult
            else:
                pnl = (rec.entry_price - rec.exit_price) * rec.volume * mult
            rec.pnl = pnl
            self.trade_records.append(rec)
            self.open_record = None

        self.put_event()

    def print_vote_factor_report(self):
        """输出投票制因子独立绩效报告"""
        factor_stats = {
            "ma": {"pnl": 0.0, "count": 0, "win": 0},
            "don": {"pnl": 0.0, "count": 0, "win": 0},
            "boll": {"pnl": 0.0, "count": 0, "win": 0},
            "sar": {"pnl": 0.0, "count": 0, "win": 0},
        }
        factor_vote_map = {
            "ma": "v_ma",
            "don": "v_don",
            "boll": "v_boll",
            "sar": "v_sar",
        }

        for r in self.trade_records:
            for name, vote_key in factor_vote_map.items():
                vote = getattr(r, vote_key)
                if vote == 0:
                    continue  # 弃权不统计
                factor_stats[name]["count"] += 1
                # 投票方向与交易方向一致，获得该笔盈亏；相反则记亏损
                if vote == r.direction:
                    factor_stats[name]["pnl"] += r.pnl
                    if r.pnl > 0:
                        factor_stats[name]["win"] += 1
                else:
                    factor_stats[name]["pnl"] -= r.pnl

        self.write_log("===== 投票制 因子独立绩效报告 =====")
        for name, st in factor_stats.items():
            if st["count"] == 0:
                wr = 0
            else:
                wr = st["win"] / st["count"]
            self.write_log(
                f"{name:4s} | 总盈亏:{st['pnl']:>8.2f} | "
                f"投票次数:{st['count']:>3} | 正确次数:{st['win']:>3} | "
                f"投票胜率:{wr:.2%}"
            )

    def export_trade_csv(self, csv_path="vote_trade_log.csv"):
        """导出交易明细CSV，用于pandas绘图分析"""
        import csv
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "trade_id", "datetime", "direction",
                "v_ma", "v_don", "v_boll", "v_sar",
                "entry_price", "exit_price", "volume", "pnl"
            ])
            writer.writeheader()
            for r in self.trade_records:
                writer.writerow({
                    "trade_id": r.trade_id,
                    "datetime": r.datetime,
                    "direction": r.direction,
                    "v_ma": r.v_ma,
                    "v_don": r.v_don,
                    "v_boll": r.v_boll,
                    "v_sar": r.v_sar,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "volume": r.volume,
                    "pnl": r.pnl,
                })
        self.write_log(f"交易明细已导出至 {csv_path}")

    def on_order(self, order: OrderData):
        pass

    def on_stop_order(self, so: StopOrder):
        pass
```

---

# 二、配套 Pandas 因子衰减绘图代码

适配投票制CSV，绘制每个因子的**滚动投票盈亏、累计盈亏、滚动胜率**，直观判断因子是否衰减。

```
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

# ====================== 配置 ======================
CSV_PATH = "vote_trade_log.csv"
ROLL_WINDOW = 20  # 滚动窗口，与策略逻辑对齐
# ==================================================


def load_vote_data(csv_path):
    df = pd.read_csv(csv_path, parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)

    # 计算每个因子每笔交易的盈亏（投票同向得正收益，反向得负收益，弃权0）
    factors = ["ma", "don", "boll", "sar"]
    for f in factors:
        col = f"v_{f}"
        # 投票方向 == 交易方向 → 盈亏正；相反 → 盈亏负；弃权 → 0
        df[f"pnl_{f}"] = np.where(
            df[col] == 0,
            0,
            np.where(df[col] == df["direction"], df["pnl"], -df["pnl"])
        )
    return df


def plot_vote_factor_decay(df):
    factors = ["ma", "don", "boll", "sar"]
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]

    fig = plt.figure(figsize=(14, 10))
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1])

    # 上图：因子累计盈亏 + 滚动盈亏
    ax1 = fig.add_subplot(gs[0])
    ax1.set_title(f"投票制4因子 盈亏衰减曲线（滚动窗口={ROLL_WINDOW}笔）", fontsize=14)
    for f, c in zip(factors, colors):
        cum_pnl = df[f"pnl_{f}"].cumsum()
        roll_pnl = df[f"pnl_{f}"].rolling(ROLL_WINDOW).sum()
        ax1.plot(df["datetime"], cum_pnl, label=f"{f} 累计盈亏", color=c, lw=1.8)
        ax1.plot(df["datetime"], roll_pnl, label=f"{f} 滚动{ROLL_WINDOW}笔盈亏", color=c, lw=1.2, ls="--")

    ax1.axhline(y=0, color="k", lw=0.8, alpha=0.6)
    ax1.set_ylabel("盈亏金额")
    ax1.legend(loc="upper left", ncol=2)
    ax1.grid(alpha=0.3)

    # 下图：滚动胜率
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    for f, c in zip(factors, colors):
        # 滚动胜率：窗口内投票正确次数 / 投票总次数（弃权不算）
        win_flag = df[f"pnl_{f}"] > 0
        vote_flag = df[f"pnl_{f}"] != 0
        roll_win = win_flag.rolling(ROLL_WINDOW).sum()
        roll_count = vote_flag.rolling(ROLL_WINDOW).sum()
        roll_winrate = roll_win / roll_count.replace(0, np.nan)
        ax2.plot(df["datetime"], roll_winrate, label=f"{f} 滚动胜率", color=c, lw=1.5)

    ax2.axhline(y=0.5, color="k", lw=0.8, alpha=0.6, ls="--")
    ax2.set_ylabel("滚动胜率")
    ax2.set_ylim(0, 1)
    ax2.legend(loc="lower left", ncol=2)
    ax2.grid(alpha=0.3)
    ax2.set_xlabel("时间")

    plt.tight_layout()
    plt.savefig("vote_factor_decay.png", dpi=300, bbox_inches="tight")
    plt.show()

    # 打印汇总表
    summary = pd.DataFrame(index=factors)
    for f in factors:
        pnl_col = f"pnl_{f}"
        summary.loc[f, "总盈亏"] = df[pnl_col].sum()
        summary.loc[f, "投票次数"] = (df[pnl_col] != 0).sum()
        summary.loc[f, "正确次数"] = (df[pnl_col] > 0).sum()
    summary["投票胜率"] = summary["正确次数"] / summary["投票次数"]
    print("===== 投票制因子绩效汇总 =====")
    print(summary.round(2))


if __name__ == "__main__":
    df = load_vote_data(CSV_PATH)
    plot_vote_factor_decay(df)
```

---

# 三、使用说明与调参建议

1. **对比测试方法**  
   用完全相同的品种、周期、回测区间、风控参数，分别跑「打分加权版」和「投票制版」，重点对比：
- 交易次数：投票制通常更少，震荡市自动降频
- 最大回撤：投票制多数情况下回撤更小
- 夏普比率：趋势流畅行情打分版通常更高，震荡市投票版更稳
- 因子衰减速度：投票制单个因子失效对整体冲击更小
2. **核心可调参数**
- `min_votes`：默认2，调大（如3）信号更准但更少，调小（如1）信号多但噪声大
- `vote_diff` 分歧过滤：默认票数差≥2，可根据品种波动特性调整
- 每个因子的投票触发条件：可单独给某因子增加过滤（比如唐奇安叠加放量才投票）
3. **投票制避坑点**
- 不要给因子偷偷加“半票”，否则就退化成打分加权
- 因子之间尽量低相关，否则相当于重复投票，提升有限
- 自适应权重不适合纯投票制，如需动态调整因子话语权，建议直接切回打分加权版

如果你需要，我可以再补充一个「打分+投票混合版」：投票决定是否开仓，打分决定仓位大小，兼顾鲁棒性和信号强弱区分。

本回答由AI生成，仅供参考，请仔细甄别，谨慎投资。
