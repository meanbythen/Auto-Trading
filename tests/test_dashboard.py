"""状态栏对账按钮：只在真要人处理时出现。"""

from src.ui.dashboard import reconcile_needs_human


def test_stale_mismatch_does_not_need_human() -> None:
    """刚平仓、柜台查询还慢，认领/拒绝保持灰色。"""
    assert reconcile_needs_human({"ok": False, "diffs": ["lh2701 柜台 -1 手，账本 0 手"]}) is False
    assert reconcile_needs_human({"ok": True, "unclaimed": [], "phantom": []}) is False


def test_real_unclaimed_needs_human() -> None:
    """柜台多出来、又不是本程序刚成交的仓，才需要人看一眼。"""
    assert reconcile_needs_human({"unclaimed": ["lh2701"]}) is True
    assert reconcile_needs_human({"phantom": ["bu2612"]}) is True
