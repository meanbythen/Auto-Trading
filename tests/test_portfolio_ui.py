"""批次4 Task 4：组合回测 UI 入口（交叉测试页）。

只做静态断言：不额外创建 Tk 窗口（Windows 上菜单句柄有上限，
多建一个 App 会让后面的 UI 回归用例报 "No more menus can be allocated"）。
运行时的 capital_mode 传递已在 tests/test_ui_regression.py 覆盖。
"""

from __future__ import annotations

import importlib
import inspect

from src.ui import picker_tab
from src.ui.picker_tab import PickerTabMixin


def test_ui_module_imports_without_display() -> None:
    importlib.reload(picker_tab)


def test_picker_mixin_exposes_portfolio_actions() -> None:
    for name in ("_on_portfolio_run", "_portfolio_done"):
        assert hasattr(PickerTabMixin, name)
        assert callable(getattr(PickerTabMixin, name))


def test_portfolio_handler_reads_weight_controls_and_calls_data_layer() -> None:
    source = inspect.getsource(PickerTabMixin._on_portfolio_run)
    assert "pk_weight_var" in source
    assert "pk_cap_var" in source
    assert "pk_legs_var" in source
    assert "parse_legs" in source
    assert "run_portfolio_backtest" in source
    assert "format_portfolio_lines" in source


def test_portfolio_done_resets_running_flag() -> None:
    source = inspect.getsource(PickerTabMixin._portfolio_done)
    assert "_pick_running = False" in source
    assert "_render_pick_board" in source
