"""
复盘「当日基本面」推荐手动来源
功能：按合约品种给出可手查的公开渠道，填进左侧补充框时有据可依。
原理：
  程序自动拉的仓单/基差/快讯覆盖不全（尤其沥青、化工、煤焦）。
  这里不爬付费站，只列你能打开的官网和常见资讯入口；自己核对后再贴进补充框。
  键是品种字母（小写），和 fundamental.product_of 对齐。
"""

from __future__ import annotations

from src.review.fundamental import product_of

# 每个品种：常见看什么 + 推荐去哪看（公开渠道为主，不保证永远能打开）
_SOURCES: dict[str, dict[str, object]] = {
    "j": {
        "name": "焦炭",
        "watch": ("仓单与库存", "钢厂开工/利润", "焦炭现货与基差", "港口与主产地报价"),
        "links": (
            "大商所官网 → 数据 → 仓单日报（焦炭）",
            "我的钢铁网 Mysteel → 煤焦频道（现货、库存、钢厂开工）",
            "同花顺期货通 / 东财期货 → 焦炭资讯与仓单",
            "主流期货公司早评（焦炭专题，注意区分广告与数据）",
        ),
    },
    "jm": {
        "name": "焦煤",
        "watch": ("仓单", "煤矿开工与安检", "进口煤到港", "焦煤现货与基差"),
        "links": (
            "大商所官网 → 仓单日报（焦煤）",
            "我的钢铁网 Mysteel → 炼焦煤频道",
            "同花顺期货通 → 焦煤资讯",
            "海关/港口到港传闻需交叉验证，勿单信自媒体",
        ),
    },
    "bu": {
        "name": "沥青",
        "watch": ("仓单", "炼厂开工与库存", "原油走势", "道路需求/天气施工"),
        "links": (
            "上期所官网 → 仓单日报（沥青）",
            "隆众资讯 / 金联创 → 沥青开工、社会库存（多为订阅，可看公开摘要）",
            "同花顺期货通 → 沥青 + 原油相关资讯",
            "EIA 周度原油库存（外围背景，注意时差）",
        ),
    },
    "fu": {
        "name": "燃料油",
        "watch": ("仓单", "裂解价差", "高硫/低硫价差", "船燃与进口"),
        "links": (
            "上期能源（INE）官网 → 仓单与交割相关公告",
            "隆众 / 金联创 → 燃料油与船燃现货",
            "同花顺期货通 → 燃料油、原油资讯",
            "关注原油与裂解价差，不要只看单一合约涨跌",
        ),
    },
    "ta": {
        "name": "PTA",
        "watch": ("仓单", "PX-PTA 加工费", "聚酯开工", "装置检修"),
        "links": (
            "郑商所官网 → 仓单日报（PTA）",
            "隆众资讯 → PTA / 聚酯开工与库存",
            "同花顺期货通 → PTA、乙二醇相关资讯",
            "关注成本端原油/PX 与下游聚酯负荷，避免只看盘面",
        ),
    },
    "sh": {
        "name": "烧碱",
        "watch": ("仓单", "氯碱开工", "下游氧化铝/造纸需求", "液碱现货"),
        "links": (
            "郑商所官网 → 仓单日报（烧碱）",
            "隆众 / 百川盈孚 → 烧碱、液碱现货与开工（公开摘要或订阅）",
            "同花顺期货通 → 烧碱资讯",
            "与氧化铝、造纸需求一起看，单看仓单容易片面",
        ),
    },
    "rb": {
        "name": "螺纹钢",
        "watch": ("仓单", "钢厂库存与产量", "建材需求", "基差"),
        "links": (
            "上期所仓单日报",
            "我的钢铁网 Mysteel → 钢材库存与产量",
            "同花顺期货通 → 黑色系资讯",
        ),
    },
    "c": {
        "name": "玉米",
        "watch": ("仓单", "到港与拍卖", "饲料需求", "天气"),
        "links": (
            "大商所仓单日报",
            "天下粮仓 / 饲料行业资讯（交叉验证）",
            "同花顺期货通 → 玉米资讯",
        ),
    },
}


def sources_for(symbol: str) -> dict[str, object]:
    """
    按合约返回该品种的推荐来源。
    不认识的品种返回通用提示，避免界面空白。
    """
    code, name, _keys = product_of(symbol)
    preset = _SOURCES.get(code.lower())
    if preset:
        return {
            "code": code.lower(),
            "name": str(preset["name"]),
            "watch": list(preset["watch"]),
            "links": list(preset["links"]),
        }
    return {
        "code": code.lower(),
        "name": name,
        "watch": ["仓单增减", "现货与基差", "装置/开工或供需新闻"],
        "links": [
            "对应交易所官网 → 仓单日报 / 公告",
            "同花顺期货通 → 该品种资讯",
            "把你核对过的一两句贴进左侧补充框即可",
        ],
    }


def sources_hint_lines(symbol: str, *, max_links: int = 4) -> list[str]:
    """界面小字：这个品种建议看什么、去哪看。"""
    info = sources_for(symbol)
    watch = "、".join(str(x) for x in info["watch"])
    lines = [
        f"【{info['name']}】建议关注：{watch}",
        "推荐手动来源（打开核对后再贴进上面的框）：",
    ]
    for i, link in enumerate(list(info["links"])[:max_links], 1):
        lines.append(f"  {i}. {link}")
    return lines


def sources_report_lines(symbol: str) -> list[str]:
    """写进复盘报告「三、基本面」末尾，方便以后翻记录。"""
    info = sources_for(symbol)
    watch = "、".join(str(x) for x in info["watch"])
    lines = [
        "推荐手动来源（程序不会自动打开这些网站，请你自己核对后贴到补充框）：",
        f"  关注要点：{watch}",
    ]
    for link in info["links"]:
        lines.append(f"  · {link}")
    return lines


def sources_for_watch_list(symbols: list[str]) -> str:
    """按关注名单生成一份可读清单（说明书或导出用）。"""
    seen: set[str] = set()
    blocks: list[str] = []
    for symbol in symbols:
        code, _name, _ = product_of(symbol)
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        info = sources_for(symbol)
        watch = "、".join(str(x) for x in info["watch"])
        blocks.append(f"### {info['name']}（例：{symbol}）")
        blocks.append(f"建议关注：{watch}")
        for link in info["links"]:
            blocks.append(f"- {link}")
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"
