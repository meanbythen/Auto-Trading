# 核心原则
✅ **优先用 vnpy 官方 `database.save_tick_data()` 接口入库（推荐），不要自己裸写SQL插表**
原因：vnpy 的 `TickData` 对象自带校验、时区、ORM映射，自动适配 `database.db`，入库后在 DataManager、回测引擎都能正常识别；自己直接sqlite3写表容易字段不匹配、时区错误、新版本字段（turnover）缺失导致报错。

> 天勤Tick字段：`datetime,last_price,average,highest,lowest,ask_price1,ask_volume1,bid_price1,bid_volume1,volume,open_interest`
> 天勤只有一档盘口，2~5档盘口填0；turnover（成交金额）天勤不提供，填0。
> ⚠️ 时区：vnpy TickData 的 datetime **必须带时区（Asia/Shanghai UTC+8）**，不带时区会回测异常。

## 前置依赖
```bash
pip install pandas python-dotenv tqsdk
```

## 完整脚本：天勤清洗后Tick CSV → 写入vnpy sqlite数据库
```python
import pandas as pd
from datetime import datetime, timezone, timedelta
from vnpy.trader.constant import Exchange
from vnpy.trader.object import TickData
from vnpy.trader.database import get_database

# 中国时区 UTC+8
UTC8 = timezone(timedelta(hours=8))

# ===================== 配置区（重点修改） =====================
SRC_CSV = "./tick_batch_main/SHFE_fu_concat_tick_clean.csv"
# 交易所：Exchange.SHFE / Exchange.DCE / Exchange.CZCE
TARGET_EXCHANGE = Exchange.SHFE
# 合约代码，例如 fu2601、c2601
TARGET_SYMBOL = "fu2601"
# 批量写入批次大小，tick量大建议 1000~5000
BATCH_SIZE = 2000
# ============================================================

def tick_csv_to_vnpy_db(src_csv, exchange, symbol):
    db = get_database()
    print(f"开始读取：{src_csv}")
    df = pd.read_csv(src_csv, parse_dates=["datetime"])
    total = len(df)
    print(f"总行数：{total}")

    tick_list = []
    for idx, row in df.iterrows():
        # 时间增加时区
        dt: datetime = row["datetime"]
        dt = dt.replace(tzinfo=UTC8)

        tick = TickData(
            symbol=symbol,
            exchange=exchange,
            datetime=dt,
            last_price=float(row["last_price"]),
            volume=float(row["volume"]),
            open_interest=float(row["open_interest"]),
            bid_price_1=float(row["bid_price1"]),
            bid_volume_1=float(row["bid_volume1"]),
            ask_price_1=float(row["ask_price1"]),
            ask_volume_1=float(row["ask_volume1"]),
            # 天勤tick没有下面这些字段，填0
            turnover=0,
            last_volume=0,
            limit_up=0,
            limit_down=0,
            open_price=0,
            high_price=0,
            low_price=0,
            pre_close=0,
            # 2~5档盘口，天勤没有，全部0
            bid_price_2=0, bid_volume_2=0,
            bid_price_3=0, bid_volume_3=0,
            bid_price_4=0, bid_volume_4=0,
            bid_price_5=0, bid_volume_5=0,
            ask_price_2=0, ask_volume_2=0,
            ask_price_3=0, ask_volume_3=0,
            ask_price_4=0, ask_volume_4=0,
            ask_price_5=0, ask_volume_5=0,
            gateway_name="DB"
        )
        tick_list.append(tick)

        # 分批入库，防止内存爆炸
        if len(tick_list) >= BATCH_SIZE:
            db.save_tick_data(tick_list)
            print(f"已写入 {idx+1}/{total}")
            tick_list.clear()

    # 剩余不足一批的数据
    if tick_list:
        db.save_tick_data(tick_list)
        print(f"✅ 全部入库完成，共 {total} 条Tick")

if __name__ == "__main__":
    tick_csv_to_vnpy_db(
        src_csv=SRC_CSV,
        exchange=TARGET_EXCHANGE,
        symbol=TARGET_SYMBOL
    )
```

## 数据库文件位置说明（vnpy默认sqlite）
- Windows：`C:\Users\你的用户名\.vnpy\database.db`
- Mac / Linux：`~/.vnpy/database.db`
> 不要手动用sqlite3直接打开同时vnpy也在运行，会锁库。
> 首次运行vnpy，会自动创建数据表，不需要手动建表。

## 配套流水线（你整套链路）
天勤专业版下载 → 原始csv → **tick清洗脚本（去重、bid>ask、无效价格）** → 本脚本入库 → 在vnpy CtaBacktester回测

## 重要坑点（必看）
1. **时区问题**：`datetime` 不带UTC+8时区，回测引擎读取时间错乱，这是高频踩坑点
2. **turnover字段**：新版vnpy tick表有turnover，天勤tick没有成交金额，固定填0，不能缺失，否则报错
3. **合约是真实合约，不是主连**：vnpy回测tick必须指定真实合约（fu2601），不能直接存KQ.m@SHFE.fu主连；主连是拼接出来的，回测时自己切换合约
4. **重复数据**：重复时间戳会重复入库，**入库前必须先跑清洗脚本去重**
5. **内存问题**：Tick数量极大（几十万、上百万），不要一次性全部读到列表，脚本里做了分批写入 `BATCH_SIZE`
6. **DataManager查看**：vnpy的DataManager图形界面**不支持预览tick**，只能在回测引擎里加载tick数据回测，Bar可以预览

## 备选：批量入库（一个文件夹下多个品种tick）
如果你需要遍历整个tick文件夹批量入库，我可以给批量版本。

## 测试入库是否成功（vnpy读取验证代码）
```python
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange
from datetime import datetime, timezone, timedelta

UTC8 = timezone(timedelta(hours=8))
db = get_database()

# 读取一段tick
start = datetime(2025,1,1, tzinfo=UTC8)
end = datetime(2025,1,2, tzinfo=UTC8)
tick_list = db.load_tick_data(symbol="fu2601", exchange=Exchange.SHFE, start=start, end=end)
print(f"读取到tick数量：{len(tick_list)}")
if tick_list:
    print(tick_list[0])
```

如果你需要，我再给一份：**入库后，用tick批量合成bar并写入vnpy数据库的脚本**。