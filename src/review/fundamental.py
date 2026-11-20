"""
当日基本面
功能：尽量用公开数据补充「今天这个品种外面发生了什么」，并留一块给你自己贴新闻。
原理：
  - 库存：交易所仓单数量变多，通常供给更松；变少则更紧。只描述最近一期，不假装就是复盘当天。
  - 基差：期货价 − 现货价。大于 0 叫升水（期货更贵），小于 0 叫贴水（期货更便宜）。
  - 快讯：上海金属网的短讯里，用品种关键词筛当天内容。沥青、玉米经常筛不到，所以左侧可以自己补一句。
  任何一项失败都不影响技术面和策略对照，只在报告里写明没拉到。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from loguru import logger

from src.domain.products import product_of as product_of


@dataclass
class FundamentalView:
    """基本面段落。bias: up / down / unknown，只由库存增减决定。"""

    name: str
    lines: list[str] = field(default_factory=list)
    bias: str = "unknown"


def collect_fundamentals(
    symbol: str,
    trade_date: date,
    user_note: str = "",
    *,
    fetch_remote: bool = True,
) -> FundamentalView:
    """
    汇总当日基本面文字。

    参数:
        user_note: 你在界面里手写的消息（新闻、库存解读、政策）。程序不会改写。
        fetch_remote: 测试时关掉，避免访问外网。
    """
    code, name, keys = product_of(symbol)
    view = FundamentalView(name=name)
    note = (user_note or "").strip()
    if note:
        view.lines.append("你补充的消息：")
        for line in note.splitlines():
            text = line.strip()
            if text:
                view.lines.append(f"  · {text}")
    else:
        view.lines.append("你没有手写补充。公开网站没有的消息（比如开工率、拍卖、天气），可以写在左侧再生成一次。")

    if not fetch_remote:
        view.lines.append("本次没有去网上拉库存和快讯。")
        from src.review.sources import sources_report_lines

        view.lines.extend(sources_report_lines(symbol))
        return view

    view.lines.extend(_inventory_lines(code, name, trade_date, view))
    view.lines.extend(_basis_lines(code, name, trade_date))
    view.lines.extend(_news_lines(name, keys, trade_date))
    # 自动源经常不全：报告末尾带上该品种推荐手动渠道，方便你下次知道去哪查。
    from src.review.sources import sources_report_lines

    view.lines.extend(sources_report_lines(symbol))
    return view


def _inventory_lines(code: str, name: str, trade_date: date, view: FundamentalView) -> list[str]:
    """仓单库存。成功时会改 view.bias。"""
    try:
        import akshare as ak

        df = None
        last_err: Exception | None = None
        for guess in (code, code.lower(), code.upper(), name):
            try:
                df = ak.futures_inventory_em(symbol=guess)
                if df is not None and len(df) > 0:
                    break
            except Exception as e:  # noqa: BLE001
                last_err = e
                df = None
        if df is None or len(df) == 0:
            logger.info("复盘库存未取到 {} : {}", name, last_err)
            return [f"库存：公开仓单没有 {name} 的数据，或网站暂时打不开。"]

        df = df.copy()
        df["日期"] = pd_to_datetime_date(df["日期"])
        df = df.dropna(subset=["日期"]).sort_values("日期")
        if len(df) == 0:
            return ["库存：返回了空表。"]
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else None
        last_day = last["日期"]
        stock = float(last["库存"])
        delta = None if prev is None else stock - float(prev["库存"])
        age = (trade_date - last_day).days if isinstance(last_day, date) else None
        if delta is None:
            bias_txt = "只有一期，看不出增减。"
        elif delta > 0:
            view.bias = "down"
            bias_txt = f"比上一期增加 {delta:.0f}，仓单变多，供给偏松，对价格偏空。"
        elif delta < 0:
            view.bias = "up"
            bias_txt = f"比上一期减少 {abs(delta):.0f}，仓单变少，供给偏紧，对价格偏多。"
        else:
            bias_txt = "和上一期一样，库存没有变化。"
        fresh = ""
        if age is not None and age > 3:
            fresh = f"（这份库存停在 {last_day}，不是复盘日当天，只能当背景。）"
        elif age is not None and age < 0:
            fresh = "（库存日期比复盘日还新，请以日期为准。）"
        return [f"库存：{last_day} {name}仓单约 {stock:.0f}。{bias_txt}{fresh}"]
    except Exception as e:  # noqa: BLE001
        logger.warning("复盘读取库存失败: {}", e)
        return [f"库存：读取失败（{e}）。不影响后面的技术面和策略对照。"]


def _basis_lines(code: str, name: str, trade_date: date) -> list[str]:
    """现货与期货基差。生意社偶发失败，失败就跳过，不重试太久以外的逻辑。"""
    variety = code.upper()
    # 玉米淀粉等和生意社代码不一定一致，失败就说明原因
    try:
        import akshare as ak

        day = trade_date
        df = ak.futures_spot_price(date=day.strftime("%Y%m%d"), vars_list=[variety])
        # 当天还没公布时，往前找最近一个有数的交易日，最多 5 天
        step = 0
        while (df is None or len(df) == 0) and step < 5:
            step += 1
            day = trade_date - timedelta(days=step)
            df = ak.futures_spot_price(date=day.strftime("%Y%m%d"), vars_list=[variety])
        if df is None or len(df) == 0:
            return [f"基差：没有找到 {name} 的现货/基差（生意社可能没收录这个品种）。"]
        row = df.iloc[0]
        spot = float(row.get("spot_price"))
        fut = float(row.get("dominant_contract_price"))
        basis = float(row.get("dom_basis"))
        if basis > 0:
            tone = "期货比现货贵，叫升水。"
        elif basis < 0:
            tone = "期货比现货便宜，叫贴水。"
        else:
            tone = "期货和现货差不多。"
        when = str(row.get("date") or day.strftime("%Y%m%d"))
        return [
            f"基差：{when} {name}现货约 {_fmt(spot)}，主力期货约 {_fmt(fut)}，"
            f"基差（期货−现货）{_fmt(basis)}。{tone}"
        ]
    except Exception as e:  # noqa: BLE001
        logger.info("复盘基差未取到 {}: {}", name, e)
        return [f"基差：暂时拉不到（{e}）。"]


def _news_lines(name: str, keys: tuple[str, ...], trade_date: date) -> list[str]:
    """当天快讯里和本品种有关的几条。金属网对化工、农产品覆盖有限。"""
    try:
        import akshare as ak

        df = ak.futures_news_shmet(symbol="全部")
        if df is None or len(df) == 0:
            return ["快讯：今天没有拉到公开短讯。"]
        hits: list[str] = []
        for _, row in df.iterrows():
            published = row.get("发布时间")
            stamp = pd_to_datetime(published)
            if stamp is None or stamp.date() != trade_date:
                continue
            text = str(row.get("内容") or "").replace("\n", " ").strip()
            if not text or not any(k and k in text for k in keys):
                continue
            hits.append(f"  · {stamp:%H:%M} {text[:90]}")
            if len(hits) >= 5:
                break
        if not hits:
            joined = "、".join(keys)
            return [
                f"快讯：公开短讯里没有筛到「{joined}」。"
                f"{name}的行业消息经常不在这个源里，请用左侧补充框自己贴上你看到的内容。"
            ]
        return [f"快讯（只保留和{name}相关的，最多 5 条）：", *hits]
    except Exception as e:  # noqa: BLE001
        logger.info("复盘快讯未取到: {}", e)
        return [f"快讯：暂时拉不到（{e}）。"]


def pd_to_datetime(value: object) -> datetime | None:
    """各种日期格式转成 datetime；转不了就返回空。"""
    if value is None:
        return None
    try:
        import pandas as pd

        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
        if pd.isna(stamp):
            return None
        return stamp.to_pydatetime()
    except Exception:  # noqa: BLE001
        return None


def pd_to_datetime_date(series: object) -> object:
    """库存表的日期列转成 date，非法值变成空。"""
    import pandas as pd

    out = []
    for value in series:
        stamp = pd_to_datetime(value)
        out.append(None if stamp is None else stamp.date())
    return pd.Series(out, index=getattr(series, "index", None))


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}"
    return f"{value:.2f}"
