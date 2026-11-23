"""多品种报单权限探针：价格必须远离现价，真实账户必须有环境变量才发单。"""

from __future__ import annotations

import sys


def test_far_sell_price_keeps_distance_and_stays_below_limit() -> None:
    """卖开挂在现价上方至少 2%，并且低于涨停价。"""
    from tools.ctp_order_probe import _far_sell_price

    price, skip = _far_sell_price(last=3700.0, upper=4000.0, tick=1.0, gap_pct=2.0)

    assert skip == ""
    assert price == 3775.0
    assert (price - 3700.0) / 3700.0 * 100 >= 2.0
    assert price < 4000.0


def test_far_sell_price_skips_when_limit_is_too_close() -> None:
    """涨跌停离现价太近时宁可跳过，不挂可能成交的单。"""
    from tools.ctp_order_probe import _far_sell_price

    price, skip = _far_sell_price(last=3990.0, upper=4000.0, tick=1.0, gap_pct=2.0)

    assert price == 0.0
    assert "不足" in skip


def test_far_sell_price_aligns_to_product_tick() -> None:
    """生猪最小变动 5 元：挂单价必须是 5 的整数倍。"""
    from tools.ctp_order_probe import _align, _far_sell_price

    assert _align(3774.9, 5.0) == 3770.0
    price, skip = _far_sell_price(last=9300.0, upper=10000.0, tick=5.0, gap_pct=2.0)

    assert skip == ""
    assert price % 5 == 0


def test_live_probe_without_env_does_not_connect(monkeypatch) -> None:
    """真实账户少一个环境变量就不能发单，直接退出。"""
    from tools import ctp_order_probe

    monkeypatch.delenv(ctp_order_probe.PROBE_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["ctp_order_probe.py", "--kind", "live", "--yes"])

    assert ctp_order_probe.main() == 2
