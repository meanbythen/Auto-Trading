```
```python type="interpreter"
"""
天勤TqSdk 可自定义周期K线拉取代码
支持：tick / 秒线 / 分钟线 / 小时线 / 日线
周/月线：底层拿日线，本地聚合（内置聚合函数）
依赖：pip install tqsdk
"""
from tqsdk import TqApi, TqAuth
import pandas as pd
from datetime import datetime


def resample_week(df_day: pd.DataFrame) -> pd.DataFrame:
    """日线聚合周线，周一为一周起始"""
    if df_day.empty:
        return pd.DataFrame()
    df = df_day.copy()
    df["date"] = pd.to_datetime(df["datetime"])
    df["week"] = df["date"].dt.isocalendar().week
    df["year"] = df["date"].dt.isocalendar().year

    week_df = df.groupby(["year", "week"]).agg(
        datetime=("datetime", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        open_interest=("open_interest", "last")
    ).reset_index(drop=True)
    return week_df


def resample_month(df_day: pd.DataFrame) -> pd.DataFrame:
    """日线聚合月线"""
    if df_day.empty:
        return pd.DataFrame()
    df = df_day.copy()
    df["date"] = pd.to_datetime(df["datetime"])
    df["month"] = df["date"].dt.month
    df["year"] = df["date"].dt.year

    month_df = df.groupby(["year", "month"]).agg(
        datetime=("datetime", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        open_interest=("open_interest", "last")
    ).reset_index(drop=True)
    return month_df


def get_tq_kline(api, symbol: str, period_seconds: int, data_length: int = 2000):
    """
    :param api: TqApi实例
    :param symbol: 合约代码，如 "SHFE.fu2601"
    :param period_seconds: K线周期秒数
        tick: 0
        1min:60, 5min:300,15min:900,30min:1800,60min:3600
        日线:86400
        周线/月线：这里填特殊标记 999 / 998，底层自动拉日线再聚合
    :param data_length: 获取K线数量
    :return: DataFrame
    """
    # 周线标记：999，月线标记：998
    if period_seconds == 999:
        df_day = api.get_kline_serial(symbol, duration_seconds=86400, data_length=data_length + 20)
        df = resample_week(df_day)
    elif period_seconds == 998:
        df_day = api.get_kline_serial(symbol, duration_seconds=86400, data_length=data_length + 20)
        df = resample_month(df_day)
    else:
        # 原生支持周期 tick/秒/分/时/日
        df = api.get_kline_serial(symbol, duration_seconds=period_seconds, data_length=data_length)

    # 清理tqsdk自带的额外列
    drop_cols = [c for c in df.columns if c.startswith("&")]
    df = df.drop(columns=drop_cols, errors="ignore")
    return df


if __name__ == "__main__":
    # ========== 配置区，在这里改 ==========
    USER = "你的天勤账号"
    PWD = "你的天勤密码"
    SYMBOL = "SHFE.fu2601"
    # 周期选择示例：
    # 60=1分钟, 300=5分钟, 900=15min, 3600=1小时, 86400=日线
    # 999=周线(日线聚合), 998=月线(日线聚合)
    PERIOD = 300
    KLINE_NUM = 1500
    # =====================================

    api = TqApi(auth=TqAuth(USER, PWD))
    try:
        df_k = get_tq_kline(api, SYMBOL, PERIOD, KLINE_NUM)
        print(f"==== {SYMBOL} 周期:{PERIOD} 数据共 {len(df_k)} 条 ====")
        print(df_k.head())
        print(df_k.tail())

        # 可选：保存csv
        df_k.to_csv(f"{SYMBOL}_{PERIOD}.csv", index=False, encoding="utf-8-sig")
        print("已保存csv")
    finally:
        api.close()
```
```

# TqSdk 灵活周期K线拉取完整代码
> 说明：
> - 原生周期：tick、秒、分钟、小时、日线（直接传秒数）
> - 周/月线：**天勤没有原生接口，代码内部拿日线聚合**
> - 安装依赖：`pip install tqsdk pandas`
> - 免费版限制：`get_kline_serial` 最大原生周期 86400（日线）

```python
"""
天勤TqSdk 自定义周期K线拉取
周期参数对照表：
0       tick
60      1分钟
300     5分钟
900     15分钟
1800    30分钟
3600    1小时
86400   日线
999     周线（日线聚合）
998     月线（日线聚合）
"""
from tqsdk import TqApi, TqAuth
import pandas as pd


def agg_week_from_day(df_day: pd.DataFrame) -> pd.DataFrame:
    """日线聚合周K（周一为一周起点）"""
    if df_day.empty:
        return pd.DataFrame()
    df = df_day.copy()
    df["dt"] = pd.to_datetime(df["datetime"])
    df["year"] = df["dt"].dt.isocalendar().year
    df["week"] = df["dt"].dt.isocalendar().week

    week_df = df.groupby(["year", "week"], as_index=False).agg({
        "datetime": "last",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "open_interest": "last"
    })
    return week_df


def agg_month_from_day(df_day: pd.DataFrame) -> pd.DataFrame:
    """日线聚合月K"""
    if df_day.empty:
        return pd.DataFrame()
    df = df_day.copy()
    df["dt"] = pd.to_datetime(df["datetime"])
    df["year"] = df["dt"].dt.year
    df["month"] = df["dt"].dt.month

    mon_df = df.groupby(["year", "month"], as_index=False).agg({
        "datetime": "last",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "open_interest": "last"
    })
    return mon_df


def fetch_kline(api, symbol: str, period: int, count: int = 2000) -> pd.DataFrame:
    """
    :param api: TqApi实例
    :param symbol: 合约代码，如 "SHFE.fu2601", "CZCE.CF601"
    :param period: 周期秒数 / 特殊标记999周、998月
    :param count: 需要K线数量
    :return: dataframe
    """
    if period == 999:
        # 周线，底层拿日线再多拿一点防止截断
        df_d = api.get_kline_serial(symbol, 86400, data_length=count + 30)
        df_out = agg_week_from_day(df_d)
    elif period == 998:
        # 月线
        df_d = api.get_kline_serial(symbol, 86400, data_length=count + 60)
        df_out = agg_month_from_day(df_d)
    else:
        # 原生周期：tick/秒/分/时/日线
        df_out = api.get_kline_serial(symbol, duration_seconds=period, data_length=count)

    # 删除tqsdk自带的&标记列
    drop_cols = [c for c in df_out.columns if c.startswith("&")]
    df_out = df_out.drop(columns=drop_cols, errors="ignore")
    return df_out


if __name__ == "__main__":
    # ====================== 在这里修改配置 ======================
    TQ_USER = "你的天勤账号"
    TQ_PASS = "你的天勤密码"
    TARGET_SYMBOL = "SHFE.fu2601"
    SELECT_PERIOD = 300      # 5分钟，改成999就是周线
    FETCH_NUM = 1200
    SAVE_CSV = True
    # ==========================================================

    api = TqApi(auth=TqAuth(TQ_USER, TQ_PASS))
    try:
        df = fetch_kline(api, TARGET_SYMBOL, SELECT_PERIOD, FETCH_NUM)
        print(f"【{TARGET_SYMBOL}】周期={SELECT_PERIOD}，共 {len(df)} 根K线")
        print(df.head())

        if SAVE_CSV:
            fn = f"{TARGET_SYMBOL}_{SELECT_PERIOD}.csv"
            df.to_csv(fn, index=False, encoding="utf-8-sig")
            print(f"已保存至 {fn}")
    finally:
        api.close()
```

## 重要注意点（适配你日内交易）
1. **免费版限制**：`duration_seconds` 不能直接填 >86400 的数字，所以周月必须本地聚合
2. **tick 数据**：period=0 拿tick，返回字段略有区别，不要直接用在K线指标计算
3. **历史长度上限**：免费版单次 `get_kline_serial` 最多 8000根
4. **实盘注意**：TqApi是同步阻塞模式，如果你要放到vnpy策略里，要改用 `TqApi.create_kline_serial` 异步订阅

## 扩展选项
如果你想要：
- 批量拉多个品种
- 自动对接vnpy导入K线
- 增加复权、夜盘过滤、跳空标记
告诉我，我给你改对应版本。