"""CTP 单笔最大手数口径的单元测试（白盒，纯函数）。"""

from __future__ import annotations

import pytest

from src.trader.ctp_limits import effective_max_order_volume


@pytest.mark.parametrize(
    ("configured", "profile", "expected"),
    [
        (None, None, 20),
        (0, 0, 20),
        (10, 0, 10),
        (0, 10, 10),
        (10, 5, 5),
        (5, 10, 5),
        ("8", "6.9", 6),
        (10.9, 3.2, 3),
        (-5, 10, 10),
        (10, -5, 10),
        (-5, -10, 20),
        ("abc", "7", 7),
        ("abc", "xyz", 20),
        (float("nan"), 5, 5),
        (5, float("nan"), 5),
        (True, 5, 5),
        (999999, 100000, 100000),
    ],
)
def test_effective_max_order_volume_takes_min_of_positive_values(
    configured, profile, expected
):
    assert effective_max_order_volume(configured, profile) == expected


def test_effective_max_order_volume_uses_custom_default():
    assert effective_max_order_volume(0, 0, default=30) == 30


def test_effective_max_order_volume_does_not_mutate_inputs():
    configured = {"max_order_volume": 10}
    profile = "5"
    assert effective_max_order_volume(configured, profile) == 5
    assert configured == {"max_order_volume": 10}
