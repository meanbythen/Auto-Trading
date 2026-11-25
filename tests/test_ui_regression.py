from __future__ import annotations

import copy
import threading
from datetime import date
from pathlib import Path
from tkinter import messagebox

import pytest

from src.strategy.factory import ENGINE_LABELS
from src.strategy.generic_stop import GENERIC_STOP_LABELS
from src.utils import load_config, save_config


TAB_NAMES = [
    "运行",
    "交易账号",
    "交易配置",
    "策略编辑器",
    "策略研究",
    "数据管理",
    "账户分析",
]

RESEARCH_TAB_NAMES = ["历史回测", "交叉测试", "策略优化", "复盘", "因子挖掘", "研究治理"]


def test_real_app_builds_all_tabs_and_backtest_initial_state(real_app) -> None:
    assert list(real_app.tabs._tab_dict) == TAB_NAMES
    assert list(real_app.research_tabs._tab_dict) == RESEARCH_TAB_NAMES
    assert real_app.bt_result_box.get("1.0", "end").strip() == (
        "点左侧「开始历史回测」后，这里显示胜率、盈亏和成交明细摘要。"
    )
    assert real_app.bt_engine_var.get()
    assert real_app.bt_symbol_var.get()
    assert real_app.bt_start_var.get()


def test_post_to_main_only_enqueues(tmp_path) -> None:
    """后台线程回调只入队，不能直接调 Tk：关窗 join 时互相等会拖到 15 秒超时。"""
    import queue as queue_module
    from types import SimpleNamespace

    from src.ui.run_tab import RunTabMixin

    stub = SimpleNamespace(_ui_queue=queue_module.Queue())
    seen: list[str] = []
    RunTabMixin._post_to_main(stub, lambda: seen.append("done"))
    assert seen == []

    RunTabMixin._drain_ui_callbacks(stub)
    assert seen == ["done"]


def test_post_to_main_swallows_destroyed_widget_errors() -> None:
    """控件已销毁（弹窗先关、后台慢半拍）时 TclError 要被吞掉，不能打断泵。"""
    import queue as queue_module
    import tkinter as tk
    from types import SimpleNamespace

    from src.ui.run_tab import RunTabMixin

    stub = SimpleNamespace(_ui_queue=queue_module.Queue())

    def boom() -> None:
        raise tk.TclError("invalid command name")

    RunTabMixin._post_to_main(stub, boom)
    RunTabMixin._drain_ui_callbacks(stub)  # 不抛异常即通过


def test_open_tab_routes_research_pages_through_the_hub(real_app) -> None:
    real_app._open_tab("复盘")
    assert real_app.tabs.get() == "策略研究"
    assert real_app.research_tabs.get() == "复盘"

    real_app._open_tab("交易配置")
    assert real_app.tabs.get() == "交易配置"


def test_strategy_and_generic_stop_panels_switch_without_recreation(real_app) -> None:
    strategy_panels = real_app._strategy_detail_panels
    stop_panels = real_app._generic_stop_detail_panels
    assert len(strategy_panels) == len(ENGINE_LABELS)
    assert len(stop_panels) == len(GENERIC_STOP_LABELS)
    assert "rule_dsl" in strategy_panels
    strategy_ids = {key: id(panel) for key, panel in strategy_panels.items()}
    stop_ids = {key: id(panel) for key, panel in stop_panels.items()}
    real_app.risk_pct_var.set("1.25")

    for engine, expected in strategy_panels.items():
        real_app.engine_var.set(engine)
        real_app._refresh_strategy_param_panel()
        real_app.update_idletasks()
        assert expected.winfo_manager() == "pack"
        assert all(panel.winfo_manager() == "" for key, panel in strategy_panels.items() if key != engine)

    for kind, expected in stop_panels.items():
        real_app.generic_stop_var.set(GENERIC_STOP_LABELS[kind])
        real_app._refresh_generic_stop_param_panel()
        real_app.update_idletasks()
        assert expected.winfo_manager() == "pack"
        assert all(panel.winfo_manager() == "" for key, panel in stop_panels.items() if key != kind)

    assert {key: id(panel) for key, panel in strategy_panels.items()} == strategy_ids
    assert {key: id(panel) for key, panel in stop_panels.items()} == stop_ids
    assert real_app.risk_pct_var.get() == "1.25"


def test_rule_engine_panel_loads_yaml_into_editor(real_app) -> None:
    """选中 rule_dsl:<文件名> 时仍显示自定义规则面板，并把对应 YAML 读进编辑框。"""
    real_app.engine_var.set("rule_dsl:turtle_adx")
    real_app._refresh_strategy_param_panel()
    real_app.update_idletasks()
    assert real_app._strategy_detail_panels["rule_dsl"].winfo_manager() == "pack"
    assert real_app.rule_name_var.get() == "turtle_adx"
    assert "海龟唐奇安" in real_app.rule_editor.get("1.0", "end")


def test_rule_editor_save_writes_yaml_without_switching_engine(real_app, tmp_path, monkeypatch) -> None:
    """编辑器保存 → 先校验再写 yaml；保存本身不切换运行策略。"""
    import src.strategy.rule_dsl as rule_dsl
    import src.ui.app_common as app_common

    monkeypatch.setattr(rule_dsl, "rules_dir", lambda: tmp_path)
    monkeypatch.setattr(
        rule_dsl,
        "list_rule_files",
        lambda directory=None: [
            {"stem": path.stem, "name": "临时规则", "builtin": False, "path": str(path)} for path in tmp_path.glob("*.yaml")
        ],
    )
    monkeypatch.setattr(app_common, "register_rule_engines", lambda directory=None: [])

    text = (
        'schema: 1\nname: "临时规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n"
    )
    before_engine = real_app.engine_var.get()
    real_app.rule_name_var.set("tmp_rule")
    real_app.rule_editor.delete("1.0", "end")
    real_app.rule_editor.insert("1.0", text)
    real_app._on_rule_save()

    assert (tmp_path / "tmp_rule.yaml").read_text(encoding="utf-8").startswith("schema: 1")
    assert real_app.engine_var.get() == before_engine
    assert "已保存" in real_app.rule_status_var.get()

    # 校验失败时不写盘、不切引擎
    real_app.rule_name_var.set("bad_rule")
    real_app.rule_editor.delete("1.0", "end")
    real_app.rule_editor.insert("1.0", "schema: 1\nentry: {}\nstop: {kind: none}\n")
    real_app._on_rule_save()
    assert not (tmp_path / "bad_rule.yaml").exists()
    assert real_app.engine_var.get() == before_engine


def test_rule_editor_page_lists_files_and_marks_dirty(real_app, tmp_path, monkeypatch) -> None:
    """独立页签：列表分组显示规则文件，改编辑框后标题打上未保存星号。"""
    import src.strategy.rule_dsl as rule_dsl

    (tmp_path / "mine.yaml").write_text(
        'schema: 1\nname: "我的规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rule_dsl, "rules_dir", lambda: tmp_path)
    monkeypatch.setattr(
        rule_dsl,
        "list_rule_files",
        lambda directory=None: [
            {"stem": path.stem, "name": path.stem, "builtin": False, "path": str(path)}
            for path in tmp_path.glob("*.yaml")
        ],
    )

    page = getattr(real_app, "rule_page", None)
    assert page is not None and page.list_host is not None
    real_app._refresh_rule_list()
    labels = [
        str(widget.cget("text"))
        for widget in page.list_host.winfo_children()
        if hasattr(widget, "cget")
    ]
    assert any("mine.yaml" in text for text in labels)
    assert any("我的规则" in text for text in labels)

    real_app._load_rule_text("mine")
    assert real_app._rule_loaded_stem == "mine"
    assert real_app.rule_title_var.get() == "当前：mine.yaml"

    real_app.rule_editor.insert("end", "\n# 改一笔\n")
    real_app._on_rule_modified()
    assert real_app._rule_dirty is True
    assert real_app.rule_title_var.get().endswith("*")

    # 切到别的文件/新建时先确认；这里选「不放弃」，编辑内容原地保留
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: False)
    real_app._on_rule_new()
    assert real_app._rule_loaded_stem == "mine"
    assert real_app._rule_dirty is True


def test_rule_editor_check_only_does_not_write(real_app, tmp_path, monkeypatch) -> None:
    """「仅校验」只编译不落盘。"""
    import src.strategy.rule_dsl as rule_dsl

    monkeypatch.setattr(rule_dsl, "rules_dir", lambda: tmp_path)
    real_app.rule_name_var.set("only_check")
    real_app.rule_editor.delete("1.0", "end")
    real_app.rule_editor.insert(
        "1.0",
        'schema: 1\nname: "仅校验"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n",
    )
    real_app._on_rule_check()
    assert "校验通过" in real_app.rule_status_var.get()
    assert not (tmp_path / "only_check.yaml").exists()

    real_app.rule_editor.delete("1.0", "end")
    real_app.rule_editor.insert("1.0", "schema: 1\nentry: {}\nstop: {kind: none}\n")
    real_app._on_rule_check()
    assert "校验没通过" in real_app.rule_status_var.get()


def test_rule_editor_delete_user_rule_removes_file(real_app, tmp_path, monkeypatch) -> None:
    """删除用户规则：文件被删，引擎回退到日内实体。"""
    import src.strategy.rule_dsl as rule_dsl
    import src.ui.app_common as app_common

    user_text = (
        'schema: 1\nname: "用户规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n"
    )
    (tmp_path / "mine.yaml").write_text(user_text, encoding="utf-8")

    def _list(directory=None):
        root = Path(directory) if directory is not None else tmp_path
        return [
            {"stem": p.stem, "name": "用户规则", "builtin": False, "path": str(p)}
            for p in root.glob("*.yaml")
        ]

    monkeypatch.setattr(rule_dsl, "rules_dir", lambda: tmp_path)
    monkeypatch.setattr(rule_dsl, "list_rule_files", _list)
    monkeypatch.setattr(app_common, "register_rule_engines", lambda directory=None: [])

    real_app._load_rule_text("mine")
    real_app.engine_var.set("rule_dsl:mine")
    real_app._on_rule_delete()
    assert not (tmp_path / "mine.yaml").exists()
    assert real_app.engine_var.get() == "daily_body"
    assert "已删除" in real_app.rule_status_var.get()


def test_rule_editor_delete_builtin_rule_rejected(real_app, tmp_path, monkeypatch) -> None:
    """内置规则点删除：拒绝，文件保留。"""
    import src.strategy.rule_dsl as rule_dsl
    import src.ui.app_common as app_common

    builtin_text = (
        'schema: 1\nbuiltin: true\nname: "内置规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n"
    )
    (tmp_path / "builtin_rule.yaml").write_text(builtin_text, encoding="utf-8")

    def _list(directory=None):
        root = Path(directory) if directory is not None else tmp_path
        return [
            {"stem": p.stem, "name": "内置规则", "builtin": True, "path": str(p)}
            for p in root.glob("*.yaml")
        ]

    monkeypatch.setattr(rule_dsl, "rules_dir", lambda: tmp_path)
    monkeypatch.setattr(rule_dsl, "list_rule_files", _list)
    monkeypatch.setattr(app_common, "register_rule_engines", lambda directory=None: [])

    real_app._load_rule_text("builtin_rule")
    real_app._on_rule_delete()
    assert (tmp_path / "builtin_rule.yaml").exists()
    assert "程序自带规则" in real_app.rule_status_var.get()


def test_rule_editor_rename_user_rule(real_app, tmp_path, monkeypatch) -> None:
    """重命名用户规则：旧文件消失，新文件出现，引擎切到新名字。"""
    import src.strategy.rule_dsl as rule_dsl
    import src.ui.app_common as app_common

    (tmp_path / "old.yaml").write_text(
        'schema: 1\nname: "旧规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n",
        encoding="utf-8",
    )

    def _list(directory=None):
        root = Path(directory) if directory is not None else tmp_path
        return [
            {"stem": p.stem, "name": p.stem, "builtin": False, "path": str(p)}
            for p in root.glob("*.yaml")
        ]

    monkeypatch.setattr(rule_dsl, "rules_dir", lambda: tmp_path)
    monkeypatch.setattr(rule_dsl, "list_rule_files", _list)
    monkeypatch.setattr(app_common, "register_rule_engines", lambda directory=None: [])

    real_app._load_rule_text("old")
    real_app.rule_name_var.set("new")
    real_app.engine_var.set("rule_dsl:old")
    real_app._on_rule_rename()
    assert not (tmp_path / "old.yaml").exists()
    assert (tmp_path / "new.yaml").exists()
    assert real_app.engine_var.get() == "rule_dsl:new"
    assert "改名为" in real_app.rule_status_var.get()


def test_rule_editor_backtest_saves_rule_and_reuses_history_backtest(real_app, tmp_path, monkeypatch) -> None:
    """编辑器「回测当前规则」：先存盘，再走历史回测同一套入口，且不跳到回测页。"""
    import src.strategy.rule_dsl as rule_dsl

    monkeypatch.setattr(rule_dsl, "rules_dir", lambda: tmp_path)
    monkeypatch.setattr(
        rule_dsl,
        "list_rule_files",
        lambda directory=None: [
            {"stem": path.stem, "name": path.stem, "builtin": False, "path": str(path)}
            for path in tmp_path.glob("*.yaml")
        ],
    )
    monkeypatch.setattr(rule_dsl, "is_builtin_rule", lambda stem, directory=None: False)
    monkeypatch.setattr("src.ui.app_common.register_rule_engines", lambda directory=None: [])

    real_app._rule_loaded_stem = ""
    real_app._rule_saved_text = ""
    real_app.rule_name_var.set("bt_rule")
    real_app.rule_editor.delete("1.0", "end")
    real_app.rule_editor.insert(
        "1.0",
        'schema: 1\nname: "回测规则"\n'
        "indicators:\n  ma3: {kind: sma, period: 3}\n"
        "entry:\n  long: {gt: {left: close, right: ma3}}\n"
        "stop: {kind: percent, pct: 2}\n",
    )
    recorded: dict = {}
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    monkeypatch.setattr(real_app, "_start_backtest", lambda **kwargs: recorded.update(kwargs))

    real_app._on_rule_backtest()

    assert (tmp_path / "bt_rule.yaml").exists()
    assert recorded["engine_override"] == "rule_dsl:bt_rule"
    assert recorded["switch_tab"] is False
    assert recorded["result_box"] is real_app.rule_bt_result_box


def test_indicator_builder_inserts_configured_indicator(real_app) -> None:
    """在可视化面板里选类型、填参数，点插入后编辑框的 indicators 段会多一行。"""
    builder = getattr(real_app, "rule_indicator_builder", None)
    assert builder is not None, "策略编辑器应挂上指标库面板"

    real_app.rule_editor.delete("1.0", "end")
    real_app.rule_editor.insert(
        "1.0",
        'schema: 1\nname: "面板测试"\n'
        "indicators:\n  ma20: { kind: ema, period: 20 }\n\n"
        "entry:\n  long: { gt: { left: close, right: ma20 } }\n"
        "stop: { kind: none }\n",
    )
    # 默认选中的是 EMA，改周期与名字后预览与插入内容都要跟着变
    builder.param_vars["period"].set("60")
    builder.name_var.set("ema60")
    assert "kind: ema, period: 60" in builder.snippet_var.get()
    assert "EMA(60)" in builder.formula_var.get()

    builder.insert()
    text = real_app.rule_editor.get("1.0", "end")
    assert "  ema60: { kind: ema, period: 60 }" in text
    assert "已插入 ema60" in builder.status_var.get()

    # 同名再点一次会被拦住，不会写出重复的指标名
    builder.insert()
    assert "已经有叫 ema60" in builder.status_var.get()


def test_indicator_builder_rejects_bad_params(real_app) -> None:
    """参数越界时面板给出校验提示，且不会写进编辑框。"""
    builder = real_app.rule_indicator_builder
    real_app.rule_editor.delete("1.0", "end")
    real_app.rule_editor.insert("1.0", 'schema: 1\nname: "t"\nentry: {}\nstop: { kind: none }\n')
    before = real_app.rule_editor.get("1.0", "end")
    builder.name_var.set("ma20")
    builder.param_vars["period"].set("1")
    builder.insert()
    assert "校验没通过" in builder.status_var.get()
    assert real_app.rule_editor.get("1.0", "end") == before


def test_indicator_builder_exposes_every_library_indicator(real_app) -> None:
    """指标库里的 kind 必须都能在下拉里选到，并能带出各自的参数。"""
    from src.strategy.rule_dsl import INDICATOR_LIBRARY
    from src.ui.rule_indicator_builder import _KIND_LABELS

    kinds = {str(item["kind"]) for item in INDICATOR_LIBRARY}
    assert kinds == set(_KIND_LABELS)

    builder = real_app.rule_indicator_builder
    builder.kind_var.set(_KIND_LABELS["supertrend"])
    assert set(builder.param_vars) == {"period", "mult"}
    assert "kind: supertrend" in builder.snippet_var.get()

    builder.kind_var.set(_KIND_LABELS["expr"])
    assert "formula" in builder.param_vars
    assert builder.formula_entry is not None


def test_backtest_source_hint_margin_trace_and_opt_sync(real_app, monkeypatch) -> None:
    notices = []
    monkeypatch.setattr(messagebox, "showinfo", lambda *args: notices.append(args))
    real_app._set_watch_codes(["bu2612", "bu2613"], select="bu2612", save=False)
    real_app.source_var.set("天勤")
    real_app.bt_source_var.set("同花顺图")
    assert "只给历史回测" in real_app.bt_source_hint.cget("text")
    real_app.bt_source_var.set("新浪")
    assert real_app.bt_source_hint.cget("text") == ""

    real_app.cfg.setdefault("risk", {}).setdefault("contracts", {})["bu"] = {"margin": 12345}
    real_app.margin_var.set("777")
    real_app.bt_symbol_var.set("bu2612")
    assert real_app.bt_margin_var.get() == "12345"
    real_app.cfg["risk"]["contracts"]["bu"]["margin"] = 0
    real_app.bt_symbol_var.set("bu2613")
    assert real_app.bt_margin_var.get() == "777"

    real_app.bt_engine_var.set("daily_body")
    real_app._set_period_vars("60", real_app.bt_tf_var, real_app.bt_tf_custom_var)
    real_app._set_period_vars("D", real_app.bt_trail_var, real_app.bt_trail_custom_var)
    real_app.bt_start_var.set("2026-01-02")
    real_app.bt_end_var.set("2026-02-03")
    real_app.bt_source_var.set("同花顺图")
    real_app._sync_bt_to_opt()
    assert real_app.opt_engine_var.get() == "daily_body"
    assert real_app.opt_symbol_var.get() == "bu2613"
    assert real_app._period_code(real_app.opt_tf_var, real_app.opt_tf_custom_var) == "60"
    assert real_app._period_code(real_app.opt_trail_var, real_app.opt_trail_custom_var) == "D"
    assert real_app.opt_start_var.get() == "2026-01-02"
    assert real_app.opt_end_var.get() == "2026-02-03"
    assert real_app.opt_source_var.get() == "天勤"
    assert notices


def test_config_mapping_save_and_reload_round_trip(real_app) -> None:
    real_app._set_watch_codes(["bu2612", "rb2610"], select="bu2612", save=False)
    real_app.engine_var.set("daily_body")
    real_app.source_var.set("天勤")
    real_app._set_period_vars("60", real_app.tf_var, real_app.tf_custom_var)
    real_app._set_period_vars("D", real_app.trail_var, real_app.trail_custom_var)
    real_app.flatten_at_close_var.set(False)
    real_app.sizing_mode_var.set("fixed")
    real_app.capital_var.set("234567")
    real_app.risk_pct_var.set("1.25")
    real_app.margin_usage_pct_var.set("42.5")
    real_app.volume_var.set("3")
    real_app.max_loss_var.set("4321")
    real_app.min_interval_var.set("65")
    real_app.max_orders_var.set("7")
    real_app.ctp_max_volume_var.set("4")
    real_app.margin_var.set("8888")

    strategy = real_app.cfg.setdefault("strategy", {})
    body = strategy.setdefault("daily_body", {})
    body.update(
        skip_gap_ticks=1,
        max_box_ticks=2,
        max_chase_ticks=3,
        initial_atr_mult=4,
        reverse_atr_mult=5,
        stop_mode="close",
    )
    strategy["market_filter"] = {"enabled": True}
    real_app.cfg.setdefault("data", {})["quote_check"] = {"enabled": True}
    real_app.cfg.setdefault("risk", {})["max_position"] = 9
    real_app.cfg["risk"]["apply_limits"] = True

    cfg = real_app._apply_ui_to_cfg()
    assert cfg["strategy"]["engine"] == "daily_body"
    assert cfg["strategy"]["timeframe"] == "60"
    assert cfg["strategy"]["trail_timeframe"] == "D"
    assert cfg["strategy"]["symbols"] == list(real_app._watch_symbols)
    assert "bu2612" in cfg["strategy"]["symbols"]
    assert cfg["strategy"]["flatten_at_close"] is False
    assert cfg["data"]["source"] == "tq"
    assert cfg["trade"]["volume"] == 3
    assert cfg["risk"]["capital"] == 234567
    assert cfg["risk"]["risk_per_trade_pct"] == 1.25
    assert cfg["risk"]["margin_usage_pct"] == 42.5
    assert cfg["risk"]["max_daily_loss"] == 4321
    assert cfg["risk"]["min_order_interval_sec"] == 65
    assert cfg["risk"]["max_orders_per_day"] == 7
    # 单笔最大手数是本机保护上限，配置页改完要落到 trade.ctp。
    assert cfg["trade"]["ctp"]["max_order_volume"] == 4
    for key in ("skip_gap_ticks", "max_box_ticks", "max_chase_ticks", "initial_atr_mult", "reverse_atr_mult", "stop_mode"):
        assert key not in cfg["strategy"]["daily_body"]
    assert "market_filter" not in cfg["strategy"]
    assert "quote_check" not in cfg["data"]
    assert "max_position" not in cfg["risk"]
    assert "apply_limits" not in cfg["risk"]

    save_config(cfg)
    real_app.engine_var.set("multi_factor")
    real_app.source_var.set("新浪")
    real_app.volume_var.set("9")
    real_app._reload_values_only()
    assert real_app.engine_var.get() == "daily_body"
    assert real_app.source_var.get() == "天勤"
    assert real_app._period_code(real_app.tf_var, real_app.tf_custom_var) == "60"
    assert real_app._period_code(real_app.trail_var, real_app.trail_custom_var) == "D"
    assert real_app.flatten_at_close_var.get() is False
    assert real_app.volume_var.get() == "3"
    assert float(real_app.capital_var.get()) == 234567
    assert float(real_app.risk_pct_var.get()) == 1.25
    assert float(real_app.margin_usage_pct_var.get()) == 42.5
    loaded = load_config()
    assert "max_position" not in loaded["risk"]
    assert "apply_limits" not in loaded["risk"]


def test_config_page_edits_multi_symbol_legs(real_app) -> None:
    """配置页能改多品种开关、扫描间隔和每条腿的策略/周期。"""
    real_app._set_watch_codes(["fu2701", "lh2701"], select="fu2701", save=False)
    strategy = real_app.cfg.setdefault("strategy", {})
    strategy.pop("legs", None)
    strategy["symbols"] = ["fu2701", "lh2701"]
    real_app._rebuild_leg_rows()
    assert [row["symbol_var"].get() for row in real_app._leg_rows] == ["fu2701", "lh2701"]

    real_app.ms_enabled_var.set(True)
    real_app.ms_allow_live_var.set(False)
    real_app.ms_interval_var.set("20")
    real_app.legs_enabled_var.set(True)
    first = real_app._leg_rows[0]
    first["engine_var"].set("daily_body")
    real_app._set_period_vars("60", first["tf_var"], first["tf_custom_var"])
    real_app._set_period_vars("D", first["trail_var"], first["trail_custom_var"])

    cfg = real_app._apply_ui_to_cfg()
    live = cfg["live"]["multi_symbol"]
    assert live["enabled"] is True
    assert live["allow_live"] is False
    assert live["scan_interval_sec"] == 20
    assert live["symbols"] == ["fu2701", "lh2701"]
    legs = cfg["strategy"]["legs"]
    assert [leg["symbol"] for leg in legs] == ["fu2701", "lh2701"]
    assert legs[0]["engine"] == "daily_body"
    assert legs[0]["timeframe"] == "60"
    assert legs[0]["trail_timeframe"] == "D"
    assert real_app._legs_ui_problem() == ""

    real_app.legs_enabled_var.set(False)
    cfg = real_app._apply_ui_to_cfg()
    assert "legs" not in cfg["strategy"]


def test_legs_editor_blocks_duplicate_and_unwatched_symbols(real_app) -> None:
    """腿里的合约不在关注名单，或同一合约重复时，保存前给出提示。"""
    from src.ui.config_tab_sections import add_leg_row

    real_app._set_watch_codes(["fu2701"], select="fu2701", save=False)
    strategy = real_app.cfg.setdefault("strategy", {})
    strategy.pop("legs", None)
    strategy["symbols"] = ["fu2701"]
    real_app._rebuild_leg_rows()
    real_app.legs_enabled_var.set(False)
    add_leg_row(
        real_app,
        symbol="lh2701",
        engine=real_app.engine_var.get(),
        timeframe="15",
        trail_timeframe="15",
    )
    assert "关注品种" in real_app._legs_ui_problem()

    real_app._leg_rows[-1]["symbol_var"].set("fu2701")
    assert "只能出现一次" in real_app._legs_ui_problem()


def test_live_scan_cfg_keeps_whitelist_when_multi_enabled(real_app) -> None:
    """常驻启动时不能把多品种白名单裁成当前品种一个。"""
    real_app._set_watch_codes(["fu2701", "lh2701"], select="lh2701", save=False)
    strategy = real_app.cfg.setdefault("strategy", {})
    strategy.pop("legs", None)
    strategy["symbols"] = ["fu2701", "lh2701"]
    real_app._rebuild_leg_rows()
    real_app.ms_enabled_var.set(True)
    real_app.legs_enabled_var.set(True)

    cfg = real_app._cfg_for_live_scan()
    assert cfg["strategy"]["symbols"] == ["lh2701", "fu2701"]
    assert cfg["trade"]["symbol"] == "lh2701"

    real_app.ms_enabled_var.set(False)
    cfg = real_app._cfg_for_live_scan()
    assert cfg["strategy"]["symbols"] == ["lh2701"]


def test_ctp_max_volume_display_uses_effective_min(real_app) -> None:
    """公共 3 / 档案 20 时，界面显示实际拦单值 3，而不是档案的 20。"""
    from src.trader.ctp_profile import selected_profile_name

    ctp = real_app.cfg.setdefault("trade", {}).setdefault("ctp", {})
    profiles = ctp.setdefault("ctp_profiles", {})
    name = selected_profile_name(ctp)
    profiles.setdefault(name, {})["kind"] = ctp.get("account_kind") or "sim"
    profiles[name]["max_order_volume"] = 20
    ctp["max_order_volume"] = 3

    real_app.ctp_profile_var.set(name)
    real_app._fill_ctp_profile_vars()
    assert real_app.ctp_max_volume_var.get() == "3"


def test_ctp_profile_form_survives_first_save(real_app) -> None:
    """首次保存不能把刚输入的手数与档案字段刷回旧值。"""
    from src.trader.ctp_profile import selected_profile_name

    ctp = real_app.cfg.setdefault("trade", {}).setdefault("ctp", {})
    profiles = ctp.setdefault("ctp_profiles", {})
    name = selected_profile_name(ctp)
    item = profiles.setdefault(name, {})
    item["kind"] = ctp.get("account_kind") or "sim"
    item["max_order_volume"] = 20
    item["broker_id"] = "8080"

    real_app.ctp_profile_var.set(name)
    real_app.ctp_max_volume_var.set("3")
    real_app.ctp_profile_broker_var.set("9999")
    cfg = real_app._apply_ui_to_cfg()

    saved = cfg["trade"]["ctp"]
    assert saved["max_order_volume"] == 3
    assert saved["ctp_profiles"][name]["max_order_volume"] == 3
    assert saved["ctp_profiles"][name]["broker_id"] == "9999"
    assert real_app.ctp_max_volume_var.get() == "3"


def test_copy_risk_to_backtest_full_does_not_save(real_app) -> None:
    real_app._set_watch_codes(["bu2612"], select="bu2612", save=False)
    real_app.engine_var.set("daily_body")
    real_app.source_var.set("天勤")
    real_app._set_period_vars("60", real_app.tf_var, real_app.tf_custom_var)
    real_app._set_period_vars("D", real_app.trail_var, real_app.trail_custom_var)
    real_app.flatten_at_close_var.set(False)
    real_app.sizing_mode_var.set("fixed")
    real_app.capital_var.set("246800")
    real_app.risk_pct_var.set("1.5")
    real_app.margin_usage_pct_var.set("45")
    real_app.volume_var.set("4")
    real_app.max_loss_var.set("3456")
    real_app.min_interval_var.set("70")
    real_app.max_orders_var.set("8")
    real_app.cfg.setdefault("risk", {})["min_volume"] = 2
    config_path = real_app.root_dir / "config" / "config.yaml"
    before = config_path.read_bytes()

    real_app._copy_risk_to_backtest(full=True)

    assert real_app.bt_engine_var.get() == "daily_body"
    assert real_app.opt_engine_var.get() == "daily_body"
    assert real_app.bt_symbol_var.get() == "bu2612"
    assert real_app.rv_symbol_var.get() == "bu2612"
    assert real_app._period_code(real_app.bt_tf_var, real_app.bt_tf_custom_var) == "60"
    assert real_app._period_code(real_app.bt_trail_var, real_app.bt_trail_custom_var) == "D"
    assert real_app.bt_source_var.get() == "天勤"
    assert real_app.opt_source_var.get() == "天勤"
    assert real_app.bt_flatten_var.get() is False
    assert real_app.bt_sizing_var.get() == "fixed"
    assert real_app.bt_capital_var.get() == "246800"
    assert real_app.bt_risk_pct_var.get() == "1.5"
    assert real_app.bt_margin_usage_pct_var.get() == "45"
    assert real_app.bt_volume_var.get() == "4"
    assert real_app.bt_min_vol_var.get() == "2"
    assert real_app.bt_max_loss_var.get() == "3456"
    assert real_app.bt_interval_var.get() == "70"
    assert real_app.bt_max_orders_var.get() == "8"
    assert config_path.read_bytes() == before


def test_prepare_backtest_run_clones_and_normalizes_without_live_leak(real_app) -> None:
    real_app._set_watch_codes(["bu2612"], select="bu2612", save=False)
    real_app.engine_var.set("multi_factor")
    real_app.source_var.set("新浪")
    real_app.bt_engine_var.set("daily_body")
    real_app.bt_source_var.set("天勤")
    real_app.bt_symbol_var.set("bu2612")
    real_app._set_period_vars("30", real_app.bt_tf_var, real_app.bt_tf_custom_var)
    real_app._set_period_vars("60", real_app.bt_trail_var, real_app.bt_trail_custom_var)
    real_app.bt_range_var.set("自定义")
    real_app.bt_start_var.set("2026/1/2")
    real_app.bt_end_var.set("2026.02.03")
    real_app.bt_limits_on_var.set(False)
    config_path = real_app.root_dir / "config" / "config.yaml"
    before = config_path.read_bytes()

    prepared = real_app._prepare_backtest_run()

    assert prepared is not None
    cfg, symbol, start, end = prepared
    assert symbol == "bu2612"
    assert start == "2026-01-02"
    assert end == "2026-02-03"
    assert cfg["strategy"]["engine"] == "daily_body"
    assert cfg["strategy"]["symbols"] == ["bu2612"]
    assert cfg["data"]["source"] == "tq"
    assert cfg["risk"]["apply_limits"] is False
    assert cfg["risk"]["sizing_mode"] == "fixed"
    assert cfg["trade"]["volume"] == 1
    assert real_app.cfg["strategy"]["engine"] == "multi_factor"
    assert real_app.cfg["data"]["source"] == "akshare"
    assert "apply_limits" not in real_app.cfg["risk"]
    assert config_path.read_bytes() == before
    live_symbols = list(real_app.cfg["strategy"]["symbols"])
    cfg["strategy"]["symbols"].append("rb2610")
    assert real_app.cfg["strategy"]["symbols"] == live_symbols


def test_prepare_backtest_uses_page_capital_not_live(real_app) -> None:
    """历史回测用本页填的测试资金开仓，不改配置页账户余额。"""
    real_app._set_watch_codes(["bu2612"], select="bu2612", save=False)
    real_app.capital_var.set("32790")
    real_app.bt_capital_var.set("100000")
    real_app.bt_engine_var.set("daily_body")
    real_app.bt_source_var.set("天勤")
    real_app.bt_symbol_var.set("bu2612")
    real_app.bt_limits_on_var.set(True)
    real_app.bt_range_var.set("自定义")
    real_app.bt_start_var.set("2026-01-02")
    real_app.bt_end_var.set("2026-02-03")
    config_path = real_app.root_dir / "config" / "config.yaml"
    before = config_path.read_bytes()

    prepared = real_app._prepare_backtest_run()

    assert prepared is not None
    cfg, _symbol, _start, _end = prepared
    assert cfg["risk"]["capital"] == 100000
    assert real_app.capital_var.get() == "32790"
    assert config_path.read_bytes() == before


def test_prepare_backtest_keeps_page_flatten_switch(real_app) -> None:
    """回测页关掉收盘清仓后点开始，不能被交易配置再打开。"""
    real_app._set_watch_codes(["bu2612"], select="bu2612", save=False)
    real_app.flatten_at_close_var.set(True)
    real_app.bt_flatten_var.set(False)
    real_app.engine_var.set("multi_factor")
    real_app.source_var.set("新浪")
    real_app.bt_engine_var.set("daily_body")
    real_app.bt_source_var.set("天勤")
    real_app.bt_symbol_var.set("bu2612")
    real_app._set_period_vars("30", real_app.bt_tf_var, real_app.bt_tf_custom_var)
    real_app._set_period_vars("60", real_app.bt_trail_var, real_app.bt_trail_custom_var)
    real_app.bt_capital_var.set("100000")
    real_app.capital_var.set("32790")
    real_app.bt_range_var.set("自定义")
    real_app.bt_start_var.set("2026-01-02")
    real_app.bt_end_var.set("2026-02-03")

    prepared = real_app._prepare_backtest_run()

    assert prepared is not None
    cfg, _symbol, _start, _end = prepared
    assert real_app.bt_flatten_var.get() is False
    assert cfg["strategy"]["flatten_at_close"] is False
    assert real_app.flatten_at_close_var.get() is True
    assert real_app.bt_engine_var.get() == "daily_body"
    assert real_app.bt_source_var.get() == "天勤"
    assert real_app._period_code(real_app.bt_tf_var, real_app.bt_tf_custom_var) == "30"
    assert real_app._period_code(real_app.bt_trail_var, real_app.bt_trail_custom_var) == "60"
    assert real_app.bt_capital_var.get() == "100000"
    assert cfg["strategy"]["engine"] == "daily_body"
    assert cfg["data"]["source"] == "tq"


def test_picker_settings_keeps_page_values(real_app) -> None:
    """交叉测试点开始不能把本页收盘清仓、手数改回交易配置。"""
    real_app.flatten_at_close_var.set(True)
    real_app.volume_var.set("1")
    real_app.risk_pct_var.set("2")
    real_app.min_interval_var.set("60")
    real_app.max_orders_var.set("5")
    real_app.capital_var.set("1000")
    real_app.pk_flatten_var.set(False)
    real_app.pk_volume_var.set("9")
    real_app.pk_risk_pct_var.set("1.5")
    real_app.pk_interval_var.set("10")
    real_app.pk_max_orders_var.set("8")
    real_app.pk_capital_var.set("50000")
    real_app.pk_range_var.set("自定义")
    real_app.pk_start_var.set("2026-01-02")
    real_app.pk_end_var.set("2026-02-03")
    real_app.pk_source_var.set("天勤")
    real_app.pk_capital_mode_var.set(True)

    settings = real_app._picker_settings()

    assert real_app.pk_flatten_var.get() is False
    assert real_app.pk_volume_var.get() == "9"
    assert real_app.pk_capital_var.get() == "50000"
    assert settings["flatten_at_close"] is False
    assert settings["fixed_volume"] == 9
    assert settings["risk_per_trade_pct"] == 1.5
    assert settings["min_order_interval_sec"] == 10
    assert settings["max_orders_per_day"] == 8
    assert settings["capital"] == 50000
    assert settings["capital_mode"] is True


def test_copy_risk_without_button_does_not_touch_backtest_page(real_app) -> None:
    """没点「按配置页重新填入」时，重新加载那条抄写路径不能改回测页。"""
    real_app.bt_flatten_var.set(False)
    real_app.bt_engine_var.set("ema_swing")
    real_app.bt_capital_var.set("88888")
    real_app.flatten_at_close_var.set(True)
    real_app.engine_var.set("daily_body")
    real_app.capital_var.set("1000")

    real_app._copy_risk_to_backtest()

    assert real_app.bt_flatten_var.get() is False
    assert real_app.bt_engine_var.get() == "ema_swing"
    assert real_app.bt_capital_var.get() == "88888"


@pytest.mark.parametrize(
    ("bt_source", "expected_source"),
    [("天勤", "tq"), ("同花顺图", "akshare")],
)
def test_write_backtest_to_config_page_preserves_live_only_values(real_app, monkeypatch, bt_source, expected_source) -> None:
    real_app._set_watch_codes(["bu2612"], select="bu2612", save=False)
    real_app.engine_var.set("multi_factor")
    real_app.source_var.set("新浪")
    real_app.capital_var.set("333333")
    real_app.bt_engine_var.set("daily_body")
    real_app.bt_source_var.set(bt_source)
    real_app.bt_symbol_var.set("bu2612")
    real_app._set_period_vars("60", real_app.bt_tf_var, real_app.bt_tf_custom_var)
    real_app._set_period_vars("D", real_app.bt_trail_var, real_app.bt_trail_custom_var)
    real_app.bt_flatten_var.set(False)
    real_app.bt_sizing_var.set("fixed")
    real_app.bt_capital_var.set("999999")
    real_app.bt_risk_pct_var.set("1.5")
    real_app.bt_margin_usage_pct_var.set("40")
    real_app.bt_volume_var.set("4")
    real_app.bt_min_vol_var.set("2")
    real_app.bt_max_loss_var.set("1234")
    real_app.bt_interval_var.set("66")
    real_app.bt_max_orders_var.set("8")
    real_app.bt_margin_var.set("7777")
    real_app.bt_start_var.set("2030-01-02")
    real_app.bt_end_var.set("2030-02-03")
    real_app.bt_limits_on_var.set(False)
    monkeypatch.setattr(messagebox, "askyesno", lambda *args: True)
    monkeypatch.setattr(messagebox, "showinfo", lambda *args: None)

    real_app._write_backtest_to_config_page()

    saved = load_config()
    assert saved["strategy"]["engine"] == "daily_body"
    assert saved["strategy"]["timeframe"] == "60"
    assert saved["strategy"]["trail_timeframe"] == "D"
    assert saved["strategy"]["flatten_at_close"] is False
    assert saved["data"]["source"] == expected_source
    assert saved["trade"]["volume"] == 4
    assert saved["risk"]["capital"] == 333333
    assert saved["risk"]["risk_per_trade_pct"] == 1.5
    assert saved["risk"]["margin_usage_pct"] == 40
    assert saved["risk"]["min_volume"] == 2
    assert saved["risk"]["max_daily_loss"] == 1234
    assert saved["risk"]["min_order_interval_sec"] == 66
    assert saved["risk"]["max_orders_per_day"] == 8
    assert saved["risk"]["margin_per_lot"] == 7777
    assert "apply_limits" not in saved["risk"]
    assert saved.get("backtest", {}).get("start_date") != "2030-01-02"
    assert saved.get("backtest", {}).get("end_date") != "2030-02-03"
    assert real_app.tabs.get() == "交易配置"


def test_backtest_validation_failure_does_not_start_thread(real_app, monkeypatch) -> None:
    errors = []
    monkeypatch.setattr(messagebox, "showerror", lambda *args: errors.append(args))
    monkeypatch.setattr(real_app, "_prepare_backtest_run", lambda: None)

    class UnexpectedThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("准备失败时不应创建线程")

    monkeypatch.setattr(threading, "Thread", UnexpectedThread)
    real_app._on_backtest()
    assert not errors


def test_prepare_backtest_run_rejects_bad_date_and_empty_symbol(real_app, monkeypatch) -> None:
    errors = []
    warnings = []
    monkeypatch.setattr(messagebox, "showerror", lambda *args: errors.append(args))
    monkeypatch.setattr(messagebox, "showwarning", lambda *args: warnings.append(args))
    monkeypatch.setattr(real_app, "_clone_cfg", lambda: copy.deepcopy(real_app.cfg))
    real_app.bt_symbol_var.set("bu2612")
    real_app.bt_range_var.set("自定义")
    real_app.bt_start_var.set("不是日期")
    assert real_app._prepare_backtest_run() is None
    assert errors

    real_app.bt_start_var.set("2026-01-02")
    real_app.bt_symbol_var.set("")
    assert real_app._prepare_backtest_run() is None
    assert warnings


def test_main_table_action_uses_controlled_thread_and_main_thread_callbacks(real_app, monkeypatch) -> None:
    from src.backtest import main_table

    calls = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *args: True)
    monkeypatch.setattr(
        main_table,
        "update_table",
        lambda root, **kwargs: calls.append((root, kwargs)) or {
            "products": 2,
            "periods": 3,
            "start": "2026-01-01",
            "end": "2026-11-15",
        },
    )

    class ImmediateThread:
        def __init__(self, *, target, daemon=False, **kwargs):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    real_app._on_update_main_table()
    assert real_app._main_table_busy is True
    real_app.update()

    assert len(calls) == 1
    assert calls[0][0] == real_app.root_dir
    assert calls[0][1]["start"] == f"{date.today().year}-01-01"
    assert calls[0][1]["out"] == real_app._main_table_path()
    assert real_app._main_table_busy is False
    assert real_app.bt_table_btn.cget("state") == "normal"
    assert real_app.bt_table_btn.cget("text") == "更新主力合约表"
    expected_state = "更新于" if real_app._main_table_path().exists() else "还没有表"
    assert expected_state in real_app.bt_table_state.cget("text")
