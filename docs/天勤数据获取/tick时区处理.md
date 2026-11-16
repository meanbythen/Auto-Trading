# 核心结论
1. 天勤 TqSdk 返回的 tick `datetime`：**已经是北京时间（UTC+8），但这个时间戳不带时区信息（naive datetime）**
2. vnpy 的 TickData 要求：**datetime 必须是带时区的 `datetime[Asia/Shanghai]`**；如果直接丢 naive 时间进去，会出现时间偏移、回测K线错乱、跨交易日逻辑异常，是最常见坑。
3. 不要手动加减小时去改时间戳，**只给 naive 时间附加时区标记，不修改时间本身**。

> 天勤原始样例：`2025-01-02 09:00:00.123456`（北京时间，无时区）
> 给它打上 `Asia/Shanghai` 时区标签，变成 aware datetime，**时间数字不变，只是附加时区元信息**

## 两种标准写法（推荐用 pytz / zoneinfo）
Python3.9+ 内置 `zoneinfo`，不用额外装包；低版本用 `pytz`。
```python
from datetime import datetime
from zoneinfo import ZoneInfo          # python >=3.9
# import pytz                          # python <3.9 用这个
# tz = pytz.timezone("Asia/Shanghai")

TZ = ZoneInfo("Asia/Shanghai")
```

### ✅ 正确方式（只附加时区，时间数值不变）
```python
# row["datetime"] 是pd.read_csv读出来的 naive datetime（天勤原始时间）
dt_naive = row["datetime"]
dt_aware = dt_naive.replace(tzinfo=TZ)
```
> ❌ 错误写法：`dt_naive + timedelta(hours=8)`，这会把时间多加上8小时，直接时间错。

### 为什么不能用 `timezone(timedelta(hours=8))`？
`timezone(timedelta(hours=8))` 是固定UTC+8，**不支持夏令时（国内已经取消夏令时，期货场景也能用）**，但行业规范和vnpy源码里统一用 `Asia/Shanghai`，兼容性更好，和vnpy内置时区逻辑对齐。

---

## 改造之前的入库脚本（时区部分重点）
```python
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from vnpy.trader.constant import Exchange
from vnpy.trader.object import TickData
from vnpy.trader.database import get_database

TZ = ZoneInfo("Asia/Shanghai")

# ===================== 配置区 =====================
SRC_CSV = "./tick_batch_main/SHFE_fu_concat_tick_clean.csv"
TARGET_EXCHANGE = Exchange.SHFE
TARGET_SYMBOL = "fu2601"
BATCH_SIZE = 2000
# ==================================================

def tick_csv_to_vnpy_db(src_csv, exchange, symbol):
    db = get_database()
    print(f"读取文件：{src_csv}")
    # parse_dates 读取出来是 naive datetime
    df = pd.read_csv(src_csv, parse_dates=["datetime"])
    total = len(df)
    print(f"总行数：{total}")

    tick_list = []
    for idx, row in df.iterrows():
        dt_naive: datetime = row["datetime"]
        # 关键：附加上海时区，不改动时间
        dt_aware = dt_naive.replace(tzinfo=TZ)

        tick = TickData(
            symbol=symbol,
            exchange=exchange,
            datetime=dt_aware,
            last_price=float(row["last_price"]),
            volume=float(row["volume"]),
            open_interest=float(row["open_interest"]),
            bid_price_1=float(row["bid_price1"]),
            bid_volume_1=float(row["bid_volume1"]),
            ask_price_1=float(row["ask_price1"]),
            ask_volume_1=float(row["ask_volume1"]),
            turnover=0,
            last_volume=0,
            limit_up=0,
            limit_down=0,
            open_price=0,
            high_price=0,
            low_price=0,
            pre_close=0,
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

        if len(tick_list) >= BATCH_SIZE:
            db.save_tick_data(tick_list)
            print(f"已写入 {idx+1}/{total}")
            tick_list.clear()

    if tick_list:
        db.save_tick_data(tick_list)
    print(f"✅ 入库完成，共 {total} 条Tick")

if __name__ == "__main__":
    tick_csv_to_vnpy_db(
        src_csv=SRC_CSV,
        exchange=TARGET_EXCHANGE,
        symbol=TARGET_SYMBOL
    )
```

---

## 额外两个容易踩坑的场景
### 场景1：pandas读取csv时，自动解析成带时区的时间（极少出现）
判断是否是naive：
```python
dt = row["datetime"]
print(dt.tzinfo)
# None → naive，需要 attach Asia/Shanghai
# 不为None → 已经带时区，要先转成Asia/Shanghai，防止时区错乱
```
如果已经带了别的时区：
```python
dt_aware = dt.astimezone(TZ)
```

### 场景2：从tqsdk直接读tick对象、不落地csv直接入库（流式）
tqsdk tick 的 `tick["datetime"]` 本身是 **naive datetime（北京时间）**，处理逻辑一模一样：
```python
tick_api = api.get_tick_serial("SHFE.fu2601")
dt_naive = tick_api.iloc[-1]["datetime"]
dt_aware = dt_naive.replace(tzinfo=TZ)
```

---

## vnpy底层存储原理（理解就不会乱）
vnpy 存入 sqlite 数据库时：
- 会把 aware datetime 自动转成 UTC 时间戳（数字存库）
- **读取的时候，再自动转回 Asia/Shanghai**
如果你传入 naive 时间，vnpy 会默认当成本地机器时区；一旦你的Windows系统时区不是北京时间，时间直接跑偏。

> 验证脚本：读取入库后的数据，打印时区
```python
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
db = get_database()

start = datetime(2025,1,1, tzinfo=TZ)
end = datetime(2025,1,2, tzinfo=TZ)
ticks = db.load_tick_data("fu2601", Exchange.SHFE, start, end)
if ticks:
    print(ticks[0].datetime, ticks[0].datetime.tzinfo)
```
输出必须带 `Asia/Shanghai`，才算正确。

## 一句话总结规则
天勤tick的datetime数字 = 北京时间；**只打Asia/Shanghai时区标记，不修改时间数值**，再传给TickData。

如果你需要，我给你一个**时区校验函数**，批量检查csv里的时间是否有异常跨日、时间越界。