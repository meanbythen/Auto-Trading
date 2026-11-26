from __future__ import annotations

import copy
import os
import sys
import tkinter as tk

import pytest


class _MemoryCipher:
    """测试用：不碰真实 DPAPI，密文就是带前缀的明文。"""

    def protect(self, text: str) -> str:
        return "mem:" + text

    def unprotect(self, token: str) -> str:
        from src.utils.secret_store import SecretStoreUnavailable

        if not token.startswith("mem:"):
            raise SecretStoreUnavailable("测试密钥库解不开这条密文")
        return token[4:]


@pytest.fixture(scope="session", autouse=True)
def _no_live_tq_login():
    """测试进程不登录真实天勤账号：同一账号并发登录会顶掉实盘行情连接。"""
    old = os.environ.get("AUTO_TRADING_NO_TQ_LOGIN")
    os.environ["AUTO_TRADING_NO_TQ_LOGIN"] = "1"
    yield
    if old is None:
        os.environ.pop("AUTO_TRADING_NO_TQ_LOGIN", None)
    else:
        os.environ["AUTO_TRADING_NO_TQ_LOGIN"] = old


@pytest.fixture(autouse=True)
def _isolated_alert_store(tmp_path, monkeypatch):
    """每个用例的告警写自己的临时目录，别污染正在使用的实盘 alerts.jsonl。"""
    from src.ops import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "_default_root", tmp_path)
    monkeypatch.setattr(alerts_module, "_last_emit", {})
    monkeypatch.setattr(alerts_module, "_run_id", "")
    yield tmp_path


@pytest.fixture(autouse=True)
def _isolated_secret_store(tmp_path, monkeypatch):
    """每个用例用自己的空密钥库，别把开发机上的真实密码读进断言。"""
    from src.account import registry as registry_module
    from src.account import session as session_module
    from src.utils import secret_store as ss

    store = ss.SecretStore(tmp_path / "secrets.dpapi.json", cipher=_MemoryCipher())
    monkeypatch.setattr(ss, "_DEFAULT", store)
    monkeypatch.setattr(ss, "default_store", lambda: store)
    monkeypatch.setattr(session_module, "default_store", lambda: store)
    monkeypatch.setattr(registry_module, "default_store", lambda: store)
    yield store


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
        "research_tab": app.research_tabs.get(),
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
        # 兜底销毁：同一进程里下一个 UI 测试模块还要新建窗口。若这里没真正销毁，
        # Windows 的菜单/窗口句柄会一路累积，最后报「No more menus can be allocated」。
        try:
            app.destroy()
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
    app.research_tabs.set(_real_app_module["research_tab"])
    app._set_result_text(app.bt_result_box, _real_app_module["result"])
    app.update_idletasks()
    return app
