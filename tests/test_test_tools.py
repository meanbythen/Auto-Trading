"""测试工具入口：四项选项与文案齐全。"""

from src.ui.test_tools import (
    OPTION_COUNTER,
    OPTION_FULL,
    OPTION_ONCE,
    OPTION_ORDER,
    _DETAILS,
    _LABELS,
)


def test_four_options_have_labels_and_tips() -> None:
    assert set(_LABELS) == {OPTION_ONCE, OPTION_ORDER, OPTION_FULL, OPTION_COUNTER}
    assert "试跑一轮" in _LABELS[OPTION_ONCE]
    assert "点击下单" in _LABELS[OPTION_ORDER]
    assert "完整自检" in _LABELS[OPTION_FULL]
    assert "仿真柜台" in _LABELS[OPTION_COUNTER]
    for key in (OPTION_ONCE, OPTION_ORDER, OPTION_FULL, OPTION_COUNTER):
        assert len(_DETAILS[key]) > 20
    assert "独立本机" in _DETAILS[OPTION_COUNTER]
    assert "无回报" in _DETAILS[OPTION_COUNTER]
