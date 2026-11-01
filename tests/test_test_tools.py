"""测试工具入口：三项选项与文案齐全。"""

from src.ui.test_tools import OPTION_FULL, OPTION_ONCE, OPTION_ORDER, _DETAILS, _LABELS


def test_three_options_have_labels_and_tips() -> None:
    assert set(_LABELS) == {OPTION_ONCE, OPTION_ORDER, OPTION_FULL}
    assert "试跑一轮" in _LABELS[OPTION_ONCE]
    assert "点击下单" in _LABELS[OPTION_ORDER]
    assert "完整自检" in _LABELS[OPTION_FULL]
    for key in (OPTION_ONCE, OPTION_ORDER, OPTION_FULL):
        assert len(_DETAILS[key]) > 20
