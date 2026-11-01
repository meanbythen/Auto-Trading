"""复盘推荐手动来源：按配置里的煤焦/沥青/化工品种能认出并给出清单。"""

from __future__ import annotations

from src.review.fundamental import product_of
from src.review.sources import sources_for, sources_for_watch_list, sources_hint_lines


def test_current_watch_products_are_recognized() -> None:
    """配置里这 6 个合约要认成正确品种，不能互相吃掉。"""
    assert product_of("j2701")[0] == "j"
    assert product_of("jm2701")[0] == "jm"
    assert product_of("bu2612")[0] == "bu"
    assert product_of("fu2701")[0] == "fu"
    assert product_of("TA2701")[0] == "ta"
    assert product_of("SH2701")[0] == "sh"


def test_sources_cover_current_watch_list() -> None:
    symbols = ["j2701", "bu2612", "fu2701", "TA2701", "SH2701", "jm2701"]
    for symbol in symbols:
        info = sources_for(symbol)
        assert info["links"]
        assert "仓单" in "、".join(str(x) for x in info["watch"]) or "库存" in "、".join(
            str(x) for x in info["watch"]
        )
        hint = "\n".join(sources_hint_lines(symbol))
        assert "推荐手动来源" in hint

    text = sources_for_watch_list(symbols)
    assert "焦炭" in text and "焦煤" in text
    assert "沥青" in text and "燃料油" in text
    assert "PTA" in text and "烧碱" in text
