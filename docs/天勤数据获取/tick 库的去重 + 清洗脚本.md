```python
import pandas as pd
import numpy as np
from datetime import datetime

def clean_tick_data(
    src_path: str,
    out_path: str,
    price_min: float = 0.0,
    drop_duplicate_ts: bool = True,
    sort_by_time: bool = True
):
    """
    天勤Tick清洗：去重时间戳、剔除无效价格、剔除异常0值、排序
    :param src_path: 原始tick csv路径
    :param out_path: 清洗后输出路径
    :param price_min: 价格下限，低于这个价格直接剔除（期货一般>0）
    :param drop_duplicate_ts: 是否删除重复时间戳（保留第一条）
    :param sort_by_time: 是否按时间升序重排
    """
    print(f"开始读取：{src_path}")
    df = pd.read_csv(src_path, parse_dates=["datetime"])
    raw_cnt = len(df)
    print(f"原始行数：{raw_cnt}")

    # 1. 剔除价格为0、nan的脏数据
    price_cols = ["last_price", "ask_price1", "bid_price1"]
    for col in price_cols:
        df = df[df[col].notna()]
        df = df[df[col] > price_min]

    # 2. 剔除成交量、持仓为负数的脏数据
    df = df[df["volume"] >= 0]
    df = df[df["open_interest"] >= 0]

    # 3. 剔除盘口价差异常（bid>ask，撮合不可能出现）
    df = df[df["bid_price1"] <= df["ask_price1"]]

    # 4. 重复时间戳去重：同一个datetime只保留第一条tick
    if drop_duplicate_ts:
        df = df.drop_duplicates(subset=["datetime"], keep="first")

    # 5. 按时间升序排序（防止录制时乱序）
    if sort_by_time:
        df = df.sort_values("datetime").reset_index(drop=True)

    final_cnt = len(df)
    drop_cnt = raw_cnt - final_cnt
    print(f"清洗完成，保留行数：{final_cnt}，剔除脏数据：{drop_cnt}")

    df.to_csv(out_path, index=False)
    print(f"清洗后文件已保存至：{out_path}")
    return df


def batch_clean_folder(folder: str, suffix="_tick.csv"):
    """
    批量清洗目录下所有tick文件，自动生成 xxx_clean.csv
    """
    import os
    for fname in os.listdir(folder):
        if fname.endswith(suffix) and not fname.endswith("_clean.csv"):
            src = os.path.join(folder, fname)
            out = os.path.join(folder, fname.replace(".csv", "_clean.csv"))
            clean_tick_data(src, out)


if __name__ == "__main__":
    # ========== 单文件清洗示例 ==========
    # clean_tick_data(
    #     src_path="./tick_data/SHFE_fu_tick.csv",
    #     out_path="./tick_data/SHFE_fu_tick_clean.csv",
    #     price_min=1.0
    # )

    # ========== 批量清洗整个tick文件夹（推荐） ==========
    batch_clean_folder("./tick_data")
```

### 清洗规则说明（适配天勤快照Tick）
1. **空值剔除**：last_price / bid1 / ask1 为空直接删掉
2. **零价格过滤**：剔除价格=0或者负数（网络抖动、断线会出现这类脏tick）
3. **盘口逻辑校验**：`bid_price1 > ask_price1` 属于脏数据，直接丢弃
4. **成交量/持仓校验**：volume、open_interest不能为负
5. **时间戳去重**：同一纳秒时间戳多条tick，只保留第一条（`keep="first"`）
6. **强制时间排序**：录制脚本断线重连后，偶尔会出现时序错乱，回测前必须排序

### 和前面脚本配合流程
1. 挂机录制得到 `SHFE_fu_tick.csv`
2. 运行本脚本 → `SHFE_fu_tick_clean.csv`
3. 把清洗后的文件丢进 `tick_to_vnpy.py`，转vnpy格式、合成1min bar

### 可选扩展（你按需加）
如果你想要额外过滤**跳价异常**（比如瞬间价格跳变幅度过大，网络毛刺），我可以再加一个价格波动率过滤。