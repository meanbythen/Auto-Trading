from __future__ import annotations

import copy
import threading
from datetime import date
from tkinter import messagebox

import pytest

from src.strategy.generic_stop import GENERIC_STOP_LABELS
from src.utils import load_config, save_config


TAB_NAMES = ["运行", "配置", "历史回测", "数据管理", "交叉测试", "策略优化", "复盘", "账户分析", "规律候选"]


def test_real_app_builds_all_tabs_and_backtest_initial_state(real_app) -> None:
    assert list(real_app.tabs._tab_dict) == TAB_NAMES
    assert real_app.bt_result_box.get("1.0", "end").strip() == (
        "点左侧「开始历史回测」后，这里显示胜率、盈亏和成交明细摘要。"
    )
    assert real_app.bt_engine_var.get()
    assert real_app.bt_symbol_var.get()
    assert real_app.bt_start_var.get()


def test_strategy_and_generic_stop_panels_switch_without_recreation(real_app) -> None:
    strategy_panels = real_app._strategy_detail_panels
    stop_panels = real_app._generic_stop_detail_panels
    assert len(strategy_panels) == 13
    assert len(stop_panels) == 5
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


def test_backtest_source_hint_margin_trace_and_opt_sync(real_app, monkeypatch) -> None:
    notices = []
    monkeypatch.setattr(messagebox, "showinfo", lambda *args: notices.append(args))
    real_app._set_watch_codes(["bu2612", "bu2613"], select="bu2612", save=False)
    real_app.source_var.set("东方财富")
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
    assert real_app.opt_source_var.get() == "东方财富"
    assert notices


def test_config_mapping_save_and_reload_round_trip(real_app) -> None:
    real_app._set_watch_codes(["bu2612", "rb2610"], select="bu2612", save=False)
    real_app.engine_var.set("daily_body")
    real_app.source_var.set("东方财富")
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
    assert cfg["data"]["source"] == "eastmoney"
    assert cfg["trade"]["volume"] == 3
    assert cfg["risk"]["capital"] == 234567
    assert cfg["risk"]["risk_per_trade_pct"] == 1.25
    assert cfg["risk"]["margin_usage_pct"] == 42.5
    assert cfg["risk"]["max_daily_loss"] == 4321
    assert cfg["risk"]["min_order_interval_sec"] == 65
    assert cfg["risk"]["max_orders_per_day"] == 7
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
    assert real_app.source_var.get() == "东方财富"
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


def test_copy_risk_to_backtest_full_does_not_save(real_app) -> None:
    real_app._set_watch_codes(["bu2612"], select="bu2612", save=False)
    real_app.engine_var.set("daily_body")
    real_app.source_var.set("东方财富")
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
    assert real_app.bt_source_var.get() == "东方财富"
    assert real_app.opt_source_var.get() == "东方财富"
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
    real_app.bt_source_var.set("东方财富")
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
    assert cfg["data"]["source"] == "eastmoney"
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


@pytest.mark.parametrize(
    ("bt_source", "expected_source"),
    [("东方财富", "eastmoney"), ("同花顺图", "akshare")],
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
    assert real_app.tabs.get() == "配置"


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
