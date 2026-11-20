"""品种代码识别：合约代码 → 品种字母、中文名、快讯关键词。

基本面、资讯源和评审单共用这一份表，避免各写一套前缀规则。
"""

from __future__ import annotations

# 代码 → (中文名, 快讯关键词)。长代码放前面，避免 c 把 cs 吃掉。
# 长代码放前面，避免 j 把 jm、c 把 cs、ta 把别的吃掉。
_PRODUCTS: list[tuple[str, str, tuple[str, ...]]] = [
    ("cs", "玉米淀粉", ("玉米淀粉", "淀粉")),
    ("bu", "沥青", ("沥青", "原油")),
    ("fu", "燃料油", ("燃料油", "船燃", "原油")),
    ("ru", "橡胶", ("橡胶",)),
    ("rb", "螺纹钢", ("螺纹", "钢材")),
    ("hc", "热卷", ("热卷", "钢材")),
    ("cu", "沪铜", ("铜",)),
    ("al", "沪铝", ("铝",)),
    ("zn", "沪锌", ("锌",)),
    ("ni", "沪镍", ("镍",)),
    ("ag", "沪银", ("白银", "银")),
    ("au", "沪金", ("黄金", "金价")),
    ("m", "豆粕", ("豆粕", "大豆")),
    ("y", "豆油", ("豆油", "大豆")),
    ("p", "棕榈油", ("棕榈",)),
    ("i", "铁矿石", ("铁矿",)),
    ("jm", "焦煤", ("焦煤",)),
    ("j", "焦炭", ("焦炭",)),
    ("ma", "甲醇", ("甲醇",)),
    ("ta", "PTA", ("PTA", "化工")),
    ("sh", "烧碱", ("烧碱", "液碱", "氯碱")),
    ("sr", "白糖", ("白糖", "糖")),
    ("cf", "棉花", ("棉花",)),
    ("c", "玉米", ("玉米", "饲料")),
]


def product_of(symbol: str) -> tuple[str, str, tuple[str, ...]]:
    """
    从合约代码认出品种。

    例：bu2612 → bu / 沥青；c2511 → c / 玉米（不会误当成 cs）。
    """
    raw = "".join(ch for ch in str(symbol).strip().lower() if ch.isalpha() or ch.isdigit())
    for code, name, keys in _PRODUCTS:
        if raw == code or raw.startswith(code) and (len(raw) == len(code) or raw[len(code)].isdigit()):
            return code, name, keys
    letters = "".join(ch for ch in raw if ch.isalpha()) or raw or symbol
    return letters, letters.upper(), (letters,)
