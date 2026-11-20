"""策略研究一级页签：五个二级页签的构建、跳页路由与切换刷新。"""

from __future__ import annotations

import pytest

from src.strategy.factory import ENGINE_LABELS


RESEARCH_TABS = ["历史回测", "交叉测试", "策略优化", "复盘", "规律候选"]


def _inside(widget, tab) -> bool:
    """控件是否构建在这个页签里（中间隔着卡片、滚动框也算）。"""
    parent = widget.master
    while parent is not None:
        if parent == tab:
            return True
        parent = parent.master
    return False


def test_each_research_page_is_built_in_its_own_sub_tab(real_app) -> None:
    assert real_app.research_tabs.get() == "历史回测"
    assert all(real_app.research_tabs.tab(name).winfo_children() for name in RESEARCH_TABS)

    pages = {
        "历史回测": real_app.bt_result_box,
        "交叉测试": real_app.pk_engine_list,
        "策略优化": real_app.opt_result_box,
        "复盘": real_app.rv_result_box,
        "规律候选": real_app.rs_result_box,
    }
    for name, widget in pages.items():
        assert _inside(widget, real_app.research_tabs.tab(name))


def test_open_tab_routes_every_research_page(real_app) -> None:
    for name in RESEARCH_TABS:
        real_app._open_tab(name)
        assert real_app.tabs.get() == "策略研究"
        assert real_app.research_tabs.get() == name

    # 非研究页只切一级，不改内层停在哪一页。
    real_app.research_tabs.set("复盘")
    real_app._open_tab("数据管理")
    assert real_app.tabs.get() == "数据管理"
    assert real_app.research_tabs.get() == "复盘"


def test_cross_test_tab_change_refreshes_lists(real_app, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(real_app, "_refresh_cross_lists", lambda *a, **k: calls.append(1))

    real_app.research_tabs.set("交叉测试")
    real_app._on_research_tab_change()
    assert calls

    calls.clear()
    real_app.research_tabs.set("策略优化")
    real_app._on_research_tab_change()
    assert not calls

    # 正在跑交叉测试时不重画名单，避免把勾选状态刷掉。
    calls.clear()
    monkeypatch.setattr(real_app, "_pick_running", True, raising=False)
    real_app.research_tabs.set("交叉测试")
    real_app._on_research_tab_change()
    assert not calls


def test_main_tab_switch_delegates_to_research_hub(real_app, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(real_app, "_refresh_cross_lists", lambda *a, **k: calls.append(1))
    real_app.tabs.set("策略研究")
    real_app.research_tabs.set("交叉测试")

    real_app._on_main_tab_change()
    assert calls

    calls.clear()
    real_app.tabs.set("运行")
    real_app._on_main_tab_change()
    assert not calls


def test_page_state_survives_research_tab_switches(real_app) -> None:
    engine = next(key for key in ENGINE_LABELS if key != real_app.bt_engine_var.get())
    real_app.bt_engine_var.set(engine)

    real_app._open_tab("策略优化")
    real_app._open_tab("复盘")
    assert real_app.research_tabs.get() == "复盘"

    real_app._open_tab("运行")
    real_app._open_tab("历史回测")
    assert real_app.tabs.get() == "策略研究"
    assert real_app.research_tabs.get() == "历史回测"
    assert real_app.bt_engine_var.get() == engine


def test_research_pages_share_the_same_watch_symbols(real_app) -> None:
    symbols = set(real_app._watch_symbols)
    assert real_app.bt_symbol_var.get() in symbols
    assert real_app.opt_symbol_var.get() in symbols
    assert real_app.rv_symbol_var.get() in symbols
    assert real_app.rs_symbol_var.get() in symbols


@pytest.mark.parametrize("name", RESEARCH_TABS)
def test_research_page_tabs_are_hidden_or_shown_without_errors(real_app, name) -> None:
    real_app._open_tab(name)
    real_app.update_idletasks()
    assert real_app.research_tabs.tab(name).winfo_manager() == "grid"
