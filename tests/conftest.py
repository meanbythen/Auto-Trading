from __future__ import annotations

import copy
import sys
import tkinter as tk

import pytest


@pytest.fixture(scope="module")
def _real_app_module(tmp_path_factory):
    """整套 UI 用例共享窗口，避免 CustomTkinter 反复分配 Windows 菜单。"""
    from src.ui import app as app_module
    from src.utils import config_loader

    root = tmp_path_factory.mktemp("real-ui")
    patcher = pytest.MonkeyPatch()
    patcher.setattr(app_module, "project_root", lambda: root)
    patcher.setattr(config_loader, "project_root", lambda: root)
    patcher.setattr(app_module, "setup_logger", lambda *args, **kwargs: None)
    try:
        app = app_module.AutoTradingApp()
    except tk.TclError:
        patcher.undo()
        if sys.platform.startswith("win"):
            raise
        pytest.skip("当前非 Windows 环境没有可用显示服务")
    except BaseException:
        patcher.undo()
        raise
    app.withdraw()
    app.update_idletasks()
    config_path = root / "config" / "config.yaml"
    state = {
        "app": app,
        "cfg": copy.deepcopy(app.cfg),
        "config_bytes": config_path.read_bytes(),
        "variables": {
            name: copy.deepcopy(value.get())
            for name, value in vars(app).items()
            if isinstance(value, tk.Variable)
        },
        "watch_symbols": list(app._watch_symbols),
        "watch_choice": app.watch_choice_var.get(),
        "tab": app.tabs.get(),
        "result": app.bt_result_box.get("1.0", "end"),
    }
    try:
        yield state
    finally:
        app._closing = True
        try:
            for after_id in app.tk.call("after", "info"):
                try:
                    app.after_cancel(after_id)
                except tk.TclError:
                    pass
            if app.winfo_exists():
                app._on_close()
        except tk.TclError:
            pass
        patcher.undo()


@pytest.fixture
def real_app(_real_app_module):
    """每个用例恢复启动快照；窗口本身不重复创建。"""
    app = _real_app_module["app"]
    config_path = app.root_dir / "config" / "config.yaml"
    config_path.write_bytes(_real_app_module["config_bytes"])
    app.cfg = copy.deepcopy(_real_app_module["cfg"])
    app._watch_busy = False
    app._set_watch_codes(
        _real_app_module["watch_symbols"],
        select=_real_app_module["watch_choice"],
        save=False,
    )
    for name, value in _real_app_module["variables"].items():
        getattr(app, name).set(value)
    app._main_table_busy = False
    app._closing = False
    app.tabs.set(_real_app_module["tab"])
    app._set_result_text(app.bt_result_box, _real_app_module["result"])
    app.update_idletasks()
    return app
