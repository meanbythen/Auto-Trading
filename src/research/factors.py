"""
封闭的因子目录。

每一条都先写清「在量什么、为什么价格可能跟它有关」。
扫描只许用这里列出的因子和预先配对的组合，禁止临时发明上千个无逻辑指标。
因子只用当根及以前的开高低收、成交量和持仓量，不用后面的价格，避免偷看未来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from src.strategy.indicators import average_true_range, ema_series


def _range(df: pd.DataFrame) -> pd.Series:
    """单根振幅，避免除零。"""
    return (df["high"].astype(float) - df["low"].astype(float)).clip(lower=1e-9)


def _ret_n(close: pd.Series, n: int) -> pd.Series:
    """最近 n 根的累计涨跌幅（相对 n 根前的收盘）。"""
    c = close.astype(float)
    return c / c.shift(int(n)) - 1.0


def _rolling_z(series: pd.Series, window: int) -> pd.Series:
    """滚动 z 分：离最近一段均值几个标准差。"""
    s = series.astype(float)
    mean = s.rolling(window, min_periods=window).mean()
    std = s.rolling(window, min_periods=window).std()
    return (s - mean) / std.replace(0, pd.NA)


def factor_ret_1(df: pd.DataFrame) -> pd.Series:
    """上一根涨跌：短动量可能延续，也可能立刻反转。"""
    return _ret_n(df["close"], 1)


def factor_ret_5(df: pd.DataFrame) -> pd.Series:
    """近 5 根累计涨跌：短趋势惯性。"""
    return _ret_n(df["close"], 5)


def factor_ret_20(df: pd.DataFrame) -> pd.Series:
    """近 20 根累计涨跌：一个月的方向，看中期惯性是否还在。"""
    return _ret_n(df["close"], 20)


def factor_ret_60(df: pd.DataFrame) -> pd.Series:
    """近 60 根累计涨跌：更长一段趋势，区分「顺大势」和「逆势反弹」。"""
    return _ret_n(df["close"], 60)


def factor_volume_ratio(df: pd.DataFrame) -> pd.Series:
    """这一根量相对近 20 根均量：参与的人变多时，方向更容易走完。"""
    vol = df["volume"].astype(float)
    mean = vol.rolling(20, min_periods=20).mean()
    return vol / mean.replace(0, pd.NA)


def factor_vol_z(df: pd.DataFrame) -> pd.Series:
    """成交量相对自身近 20 根的 z 分：比量比更不受长期水位变化影响。"""
    return _rolling_z(df["volume"].astype(float), 20)


def factor_vol_trend(df: pd.DataFrame) -> pd.Series:
    """近 5 根均量相对近 20 根均量：量能是在放大还是萎缩。"""
    vol = df["volume"].astype(float)
    fast = vol.rolling(5, min_periods=5).mean()
    slow = vol.rolling(20, min_periods=20).mean()
    return fast / slow.replace(0, pd.NA) - 1.0


def factor_obv_slope(df: pd.DataFrame) -> pd.Series:
    """OBV 近 20 根的变化，除以同期总量：资金是净流入还是净流出。"""
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    direction = np.sign(close.diff()).fillna(0.0)
    obv = (direction * vol).cumsum()
    change = obv - obv.shift(20)
    total = vol.rolling(20, min_periods=20).sum()
    return change / total.replace(0, pd.NA)


def factor_price_vol_corr(df: pd.DataFrame) -> pd.Series:
    """近 20 根「涨跌」和「量变」的滚动相关：价涨量增是否同步。"""
    ret = df["close"].astype(float).pct_change()
    vol_change = df["volume"].astype(float).pct_change().replace([np.inf, -np.inf], np.nan)
    return ret.rolling(20, min_periods=20).corr(vol_change)


def factor_up_volume_share(df: pd.DataFrame) -> pd.Series:
    """近 20 根里，上涨那几根的量占总量的比例：上涨有没有量支撑。"""
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    up_vol = vol.where(close > close.shift(1), 0.0).rolling(20, min_periods=20).sum()
    total = vol.rolling(20, min_periods=20).sum()
    return up_vol / total.replace(0, pd.NA)


def factor_amihud(df: pd.DataFrame) -> pd.Series:
    """近 20 根的平均「单位成交额推动的涨跌幅」：越大越不流动。"""
    ret = df["close"].astype(float).pct_change().abs()
    vol = df["volume"].astype(float).replace(0, pd.NA)
    return (ret / vol * 1e6).rolling(20, min_periods=20).mean()


def factor_atr_pct(df: pd.DataFrame) -> pd.Series:
    """ATR 占收盘价的比例：波动大时后面的振幅通常也大。"""
    atr = average_true_range(df["high"], df["low"], df["close"], period=14)
    close = df["close"].astype(float).replace(0, pd.NA)
    return atr / close


def factor_vol_of_vol(df: pd.DataFrame) -> pd.Series:
    """波动的波动：近 20 根波动率本身是否稳定，不稳时方向更难判断。"""
    vol = df["close"].astype(float).pct_change().rolling(20, min_periods=20).std()
    mean = vol.rolling(20, min_periods=20).mean()
    std = vol.rolling(20, min_periods=20).std()
    return std / mean.replace(0, pd.NA)


def factor_range_pos(df: pd.DataFrame) -> pd.Series:
    """收盘落在本根最高最低之间的位置：靠近最高=多方占优。"""
    low = df["low"].astype(float)
    return (df["close"].astype(float) - low) / _range(df)


def factor_donchian_pos(df: pd.DataFrame) -> pd.Series:
    """收盘在近 20 根高低区间里的位置：接近上沿更像突破，接近下沿更像破位。"""
    close = df["close"].astype(float)
    high = df["high"].astype(float).rolling(20, min_periods=20).max()
    low = df["low"].astype(float).rolling(20, min_periods=20).min()
    return (close - low) / (high - low).replace(0, pd.NA)


def factor_dist_high(df: pd.DataFrame) -> pd.Series:
    """离近 60 根最高收盘还有多远：越接近前高，越容易选择方向。"""
    close = df["close"].astype(float)
    high = close.rolling(60, min_periods=60).max()
    return close / high.replace(0, pd.NA) - 1.0


def factor_ma_gap(df: pd.DataFrame) -> pd.Series:
    """收盘相对 20 根指数均线的偏离：趋势里会继续偏离，震荡里会缩回去。"""
    close = df["close"].astype(float)
    ma = ema_series(close, 20)
    # 前 19 根还不够一根完整窗口，和以前简单均线一样先空着，避免扫描开头乱出信号。
    if len(ma) >= 20:
        ma = ma.copy()
        ma.iloc[:19] = pd.NA
    return close / ma.replace(0, pd.NA) - 1.0


def factor_rsi_14(df: pd.DataFrame) -> pd.Series:
    """14 根相对强弱：超买超卖后，后面可能回归也可能继续走强。"""
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100.0 - 100.0 / (1.0 + rs)


def factor_autocorr_20(df: pd.DataFrame) -> pd.Series:
    """近 20 根一阶自相关：正=涨跌容易连着走，负=更像来回摆。"""
    close = df["close"].astype(float)
    return close.rolling(20, min_periods=20).corr(close.shift(1))


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


def factor_oi_change(df: pd.DataFrame) -> pd.Series:
    """持仓量这一根的变化率：增仓说明有新钱进场，减仓说明在离场。"""
    oi = df["oi"].astype(float).replace(0, pd.NA)
    return oi / oi.shift(1) - 1.0


def factor_oi_z(df: pd.DataFrame) -> pd.Series:
    """持仓量相对近 20 根的 z 分：仓位是不是到了异常水平。"""
    return _rolling_z(df["oi"].astype(float), 20)


def factor_oi_vol_ratio(df: pd.DataFrame) -> pd.Series:
    """持仓量÷成交量：仓位相对当日活跃度，衡量筹码是沉淀还是快进快出。"""
    oi = df["oi"].astype(float)
    vol = df["volume"].astype(float).replace(0, pd.NA)
    return oi / vol


def factor_price_oi_quad(df: pd.DataFrame) -> pd.Series:
    """价仓四象限（-3~3）：价涨增仓=主动做多，价涨减仓=空头回补，价跌增仓=主动做空。"""
    ret = df["close"].astype(float).pct_change()
    oi_change = factor_oi_change(df)
    return np.sign(ret) + 2.0 * np.sign(oi_change)


def factor_price_oi_div(df: pd.DataFrame) -> pd.Series:
    """价格涨跌减持仓涨跌：一涨一减说明是平仓推动，持续性通常更差。"""
    ret = df["close"].astype(float).pct_change()
    return ret - factor_oi_change(df)


@dataclass(frozen=True)
class FactorSpec:
    """一条允许扫描的因子：编号、中文名、经济学逻辑、怎么算、属于哪组、需要哪些列。"""

    id: str
    title: str
    logic: str
    compute: Callable[[pd.DataFrame], pd.Series]
    group: str = "price"
    requires: tuple[str, ...] = ()
    min_bars: int = 60


# 只许扫这一份清单。新增因子必须先写清逻辑，不能在扫描时临时拼指标。
FACTORS: tuple[FactorSpec, ...] = (
    FactorSpec("ret_1", "上一根涨跌", "刚走完的一步，后面可能顺着走（动量）或立刻往回（反转）。", factor_ret_1, "price", (), 10),
    FactorSpec("ret_5", "近5根涨跌", "很短的一段趋势。若后面仍同向，说明短动量在延续。", factor_ret_5, "price", (), 20),
    FactorSpec("ret_20", "近20根涨跌", "一个月左右的方向，用来区分顺中期趋势和逆势反弹。", factor_ret_20, "price", (), 40),
    FactorSpec("ret_60", "近60根涨跌", "更长一段趋势，看大方向是否仍在延续。", factor_ret_60, "price", (), 90),
    FactorSpec("volume_ratio", "量比均量", "成交变多，说明有人在认真参与，方向更容易走完而不是假突破。", factor_volume_ratio, "volume", (), 40),
    FactorSpec("vol_z", "成交量z分", "量放到近 20 根的极端位置时，后面更容易走出方向。", factor_vol_z, "volume", (), 40),
    FactorSpec("vol_trend", "量能趋势", "近 5 根均量相对 20 根均量，量在放大还是萎缩。", factor_vol_trend, "volume", (), 40),
    FactorSpec("obv_slope", "OBV斜率", "按涨跌方向累计的量，看资金是净流入还是净流出。", factor_obv_slope, "volume", (), 40),
    FactorSpec("price_vol_corr", "价量同步", "近 20 根涨跌和量变的相关，价涨量增更可信。", factor_price_vol_corr, "volume", (), 40),
    FactorSpec("up_volume_share", "上涨量占比", "上涨那几根占总量的比例，衡量上涨有没有量支撑。", factor_up_volume_share, "volume", (), 40),
    FactorSpec("amihud", "非流动性", "单位成交额推动的涨跌幅，越大越难成交，波动也更容易放大。", factor_amihud, "volume", (), 40),
    FactorSpec("atr_pct", "波动占价格", "波动本身会聚集：已经很晃的时候，后面几根往往也晃。", factor_atr_pct, "price", (), 30),
    FactorSpec("vol_of_vol", "波动的波动", "波动率本身是否稳定；不稳定时方向判断更容易失效。", factor_vol_of_vol, "price", (), 60),
    FactorSpec("range_pos", "收盘在振幅里的位置", "收在最高附近=多方当天占优；收在最低附近=空方占优。", factor_range_pos, "price", (), 10),
    FactorSpec("donchian_pos", "唐奇安位置", "收盘在近 20 根高低区间的位置，接近上沿更像突破。", factor_donchian_pos, "price", (), 40),
    FactorSpec("dist_high", "距近60根高点", "离前高还有多远，接近前高时往往要选择方向。", factor_dist_high, "price", (), 90),
    FactorSpec("ma_gap", "偏离20均线", "价格离开均线太远时，趋势市会继续离开，震荡市会缩回。", factor_ma_gap, "price", (), 40),
    FactorSpec("rsi_14", "RSI14", "相对强弱到极端后，可能回归也可能继续走强。", factor_rsi_14, "price", (), 30),
    FactorSpec("autocorr_20", "20根自相关", "正=涨跌容易连着走，负=更像来回摆。", factor_autocorr_20, "price", (), 40),
    FactorSpec("body_ratio", "实体占振幅", "实体大说明这一根主动成交多；后面是否顺着走，要看统计。", factor_body_ratio, "price", (), 10),
    FactorSpec("upper_wick", "上影占比", "长上影常被看成上方有人砸，后面可能偏弱。", factor_upper_wick, "price", (), 10),
    FactorSpec("lower_wick", "下影占比", "长下影常被看成下方有人接，后面可能偏强。", factor_lower_wick, "price", (), 10),
    FactorSpec("consec_up", "连涨根数", "连涨可能吸引跟风，也可能把买盘用尽。", factor_consec_up, "price", (), 10),
    FactorSpec("gap", "开盘相对昨收", "跳空往往是隔夜消息一次定价，后面可能回补或顺着缺口走。", factor_gap, "price", (), 10),
    FactorSpec("oi_change", "持仓量变化", "增仓说明有新钱进场，减仓说明在离场；方向要和价格一起看。", factor_oi_change, "oi", ("oi",), 10),
    FactorSpec("oi_z", "持仓量z分", "仓位到了近 20 根的异常水平，后面更容易出现趋势或挤兑。", factor_oi_z, "oi", ("oi",), 40),
    FactorSpec("oi_vol_ratio", "持仓量÷成交量", "筹码是沉淀还是快进快出，换手结构不同，延续性也不同。", factor_oi_vol_ratio, "oi", ("oi",), 10),
    FactorSpec("price_oi_quad", "价仓四象限", "价涨增仓=主动做多，价涨减仓=空头回补，价跌增仓=主动做空。", factor_price_oi_quad, "oi", ("oi",), 10),
    FactorSpec("price_oi_div", "价仓背离", "价格和持仓一涨一减，多半是平仓推动，持续性通常更差。", factor_price_oi_div, "oi", ("oi",), 10),
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
    PairSpec("ret_5|vol_trend", "ret_5", "vol_trend", "短动量×量能趋势", "趋势要延续，量能最好同步放大，缩量上涨更易夭折。"),
    PairSpec("range_pos|vol_z", "range_pos", "vol_z", "收盘位置×量z分", "收在高位且量放到极端，比单纯放量更值得关注。"),
    PairSpec("ret_5|oi_change", "ret_5", "oi_change", "短动量×增仓", "涨跌有增仓配合，说明是新钱推动；减仓上涨更像回补。"),
    PairSpec("range_pos|oi_change", "range_pos", "oi_change", "收盘位置×增仓", "收在高位又增仓，多方更主动；收高减仓要小心。"),
    PairSpec("volume_ratio|oi_z", "volume_ratio", "oi_z", "放量×高仓位", "放量且仓位已在极端，后面更容易出现趋势加速或踩踏。"),
    PairSpec("ret_1|price_oi_div", "ret_1", "price_oi_div", "上一根×价仓背离", "价格和持仓不一致时，短动量的持续性通常更差。"),
)

FACTOR_BY_ID = {item.id: item for item in FACTORS}
PAIR_BY_ID = {item.id: item for item in PAIRS}


def compute_factor_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    把目录里每个因子算成一列。

    缺列时对应因子会是空的，扫描那边会跳过，不会硬算错。
    持仓量缺失时 OI 组整列为空，不填 0。
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
        if any(name not in work.columns for name in spec.requires):
            out[spec.id] = pd.NA
            continue
        try:
            out[spec.id] = spec.compute(work)
        except Exception:  # noqa: BLE001
            out[spec.id] = pd.NA
    return out

