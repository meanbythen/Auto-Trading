"""
封闭的因子目录。

每一条都先写清「在量什么、为什么价格可能跟它有关」。
扫描只许用这里列出的因子和预先配对的组合，禁止临时发明上千个无逻辑指标。
因子只用当根及以前的开高低收和成交量，不用后面的价格，避免偷看未来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from src.strategy.indicators import average_true_range, ema_series


def _range(df: pd.DataFrame) -> pd.Series:
    """单根振幅，避免除零。"""
    return (df["high"].astype(float) - df["low"].astype(float)).clip(lower=1e-9)


def _ret_n(close: pd.Series, n: int) -> pd.Series:
    """最近 n 根的累计涨跌幅（相对 n 根前的收盘）。"""
    c = close.astype(float)
    return c / c.shift(int(n)) - 1.0


def factor_ret_1(df: pd.DataFrame) -> pd.Series:
    """上一根涨跌：短动量可能延续，也可能立刻反转。"""
    return _ret_n(df["close"], 1)


def factor_ret_5(df: pd.DataFrame) -> pd.Series:
    """近 5 根累计涨跌：短趋势惯性。"""
    return _ret_n(df["close"], 5)


def factor_volume_ratio(df: pd.DataFrame) -> pd.Series:
    """这一根量相对近 20 根均量：参与的人变多时，方向更容易走完。"""
    vol = df["volume"].astype(float)
    mean = vol.rolling(20, min_periods=20).mean()
    return vol / mean.replace(0, pd.NA)


def factor_atr_pct(df: pd.DataFrame) -> pd.Series:
    """ATR 占收盘价的比例：波动大时后面的振幅通常也大。"""
    atr = average_true_range(df["high"], df["low"], df["close"], period=14)
    close = df["close"].astype(float).replace(0, pd.NA)
    return atr / close


def factor_range_pos(df: pd.DataFrame) -> pd.Series:
    """收盘落在本根最高最低之间的位置：靠近最高=多方占优。"""
    low = df["low"].astype(float)
    return (df["close"].astype(float) - low) / _range(df)


def factor_ma_gap(df: pd.DataFrame) -> pd.Series:
    """收盘相对 20 根指数均线的偏离：趋势里会继续偏离，震荡里会缩回去。"""
    close = df["close"].astype(float)
    ma = ema_series(close, 20)
    # 前 19 根还不够一根完整窗口，和以前简单均线一样先空着，避免扫描开头乱出信号。
    if len(ma) >= 20:
        ma = ma.copy()
        ma.iloc[:19] = pd.NA
    return close / ma.replace(0, pd.NA) - 1.0


def factor_body_ratio(df: pd.DataFrame) -> pd.Series:
    """实体占振幅（带正负）：主动买/卖的力度。"""
    body = df["close"].astype(float) - df["open"].astype(float)
    return body / _range(df)


def factor_upper_wick(df: pd.DataFrame) -> pd.Series:
    """上影占振幅：上方抛压。"""
    high = df["high"].astype(float)
    top = df[["open", "close"]].astype(float).max(axis=1)
    return (high - top) / _range(df)


def factor_lower_wick(df: pd.DataFrame) -> pd.Series:
    """下影占振幅：下方承接。"""
    low = df["low"].astype(float)
    bot = df[["open", "close"]].astype(float).min(axis=1)
    return (bot - low) / _range(df)


def factor_consec_up(df: pd.DataFrame) -> pd.Series:
    """连续上涨根数（最多 8）：连涨后可能加速，也可能耗尽。"""
    close = df["close"].astype(float)
    up = close > close.shift(1)
    run = 0
    values: list[float] = []
    for flag in up.fillna(False).tolist():
        run = run + 1 if flag else 0
        values.append(float(min(run, 8)))
    return pd.Series(values, index=df.index)


def factor_gap(df: pd.DataFrame) -> pd.Series:
    """相对上一根收盘的跳空：隔夜信息往往一次定价。"""
    close = df["close"].astype(float)
    prev = close.shift(1)
    return df["open"].astype(float) / prev.replace(0, pd.NA) - 1.0


@dataclass(frozen=True)
class FactorSpec:
    """一条允许扫描的因子：编号、中文名、经济学逻辑、怎么算。"""

    id: str
    title: str
    logic: str
    compute: Callable[[pd.DataFrame], pd.Series]


# 只许扫这一份清单。新增因子必须先写清逻辑，不能在扫描时临时拼指标。
FACTORS: tuple[FactorSpec, ...] = (
    FactorSpec("ret_1", "上一根涨跌", "刚走完的一步，后面可能顺着走（动量）或立刻往回（反转）。", factor_ret_1),
    FactorSpec("ret_5", "近5根涨跌", "很短的一段趋势。若后面仍同向，说明短动量在延续。", factor_ret_5),
    FactorSpec("volume_ratio", "量比均量", "成交变多，说明有人在认真参与，方向更容易走完而不是假突破。", factor_volume_ratio),
    FactorSpec("atr_pct", "波动占价格", "波动本身会聚集：已经很晃的时候，后面几根往往也晃。", factor_atr_pct),
    FactorSpec("range_pos", "收盘在振幅里的位置", "收在最高附近=多方当天占优；收在最低附近=空方占优。", factor_range_pos),
    FactorSpec("ma_gap", "偏离20均线", "价格离开均线太远时，趋势市会继续离开，震荡市会缩回。", factor_ma_gap),
    FactorSpec("body_ratio", "实体占振幅", "实体大说明这一根主动成交多；后面是否顺着走，要看统计。", factor_body_ratio),
    FactorSpec("upper_wick", "上影占比", "长上影常被看成上方有人砸，后面可能偏弱。", factor_upper_wick),
    FactorSpec("lower_wick", "下影占比", "长下影常被看成下方有人接，后面可能偏强。", factor_lower_wick),
    FactorSpec("consec_up", "连涨根数", "连涨可能吸引跟风，也可能把买盘用尽。", factor_consec_up),
    FactorSpec("gap", "开盘相对昨收", "跳空往往是隔夜消息一次定价，后面可能回补或顺着缺口走。", factor_gap),
)


@dataclass(frozen=True)
class PairSpec:
    """预先指定的双因子组合：只测说得通的搭配，不做全排列乱挖。"""

    id: str
    left: str
    right: str
    title: str
    logic: str


PAIRS: tuple[PairSpec, ...] = (
    PairSpec("ret_5|volume_ratio", "ret_5", "volume_ratio", "短动量×放量", "短趋势如果有量跟上，惯性往往更稳；没量的涨跌更容易是噪声。"),
    PairSpec("range_pos|volume_ratio", "range_pos", "volume_ratio", "收盘位置×放量", "收在高位又放量，更像主动买完；无量收高可能只是空盘口。"),
    PairSpec("ma_gap|atr_pct", "ma_gap", "atr_pct", "偏离均线×波动", "又偏又晃时，后面的回归或延续会比平时更剧烈。"),
    PairSpec("body_ratio|ret_1", "body_ratio", "ret_1", "实体方向×上一根", "连续两根同向主动成交，短动量可能叠在一起。"),
    PairSpec("upper_wick|volume_ratio", "upper_wick", "volume_ratio", "上影×放量", "放量长上影更像有人在高位出货。"),
    PairSpec("lower_wick|volume_ratio", "lower_wick", "volume_ratio", "下影×放量", "放量长下影更像有人在低位吸筹。"),
    PairSpec("ret_5|ma_gap", "ret_5", "ma_gap", "短动量×均线位置", "已经在均线上方还继续涨，和在均线下方反弹，含义不同。"),
)

FACTOR_BY_ID = {item.id: item for item in FACTORS}
PAIR_BY_ID = {item.id: item for item in PAIRS}


def compute_factor_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    把目录里每个因子算成一列。

    缺列时对应因子会是空的，扫描那边会跳过，不会硬算错。
    """
    need = {"open", "high", "low", "close"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"K线缺少列: {sorted(missing)}")
    work = df.copy()
    if "volume" not in work.columns:
        work["volume"] = 0.0
    out = pd.DataFrame(index=work.index)
    for spec in FACTORS:
        try:
            out[spec.id] = spec.compute(work)
        except Exception:  # noqa: BLE001
            out[spec.id] = pd.NA
    return out
