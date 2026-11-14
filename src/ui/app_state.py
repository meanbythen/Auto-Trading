"""UI variable initialization and conversion between widgets and configuration."""

from __future__ import annotations

from typing import Any

from loguru import logger
from src.data.periods import period_label
from src.data.periods import ui_parts
from src.ui.app_common import _clock_text
from src.ui.app_common import _eth_minutes_from_strategy
from src.ui.app_common import _ui_engine
from src.utils import load_config
from src.utils import load_secrets
from src.utils import save_secrets
from src.utils.trading_day import futures_trading_day
import copy
import customtkinter as ctk
import os
import threading


class AppStateMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _init_vars(self) -> None:
        st = self.cfg.get("strategy", {})
        acc = self.cfg.get("account", {})
        app = self.cfg.get("app", {})
        trade = self.cfg.get("trade", {})
        risk = self.cfg.get("risk", {})
        sch = self.cfg.get("schedule", {})
        secrets = load_secrets()

        self.engine_var = ctk.StringVar(value=_ui_engine(st.get("engine", "daily_body")))
        self.exec_var = ctk.StringVar(value=str(trade.get("executor", "sim")))
        from src.trader.ctp_logic import CTP_KIND_LABELS, ctp_kind_label

        ctp_box = trade.get("ctp") or {}
        live_box = ctp_box.get("live") if isinstance(ctp_box.get("live"), dict) else {}
        self.ctp_kind_var = ctk.StringVar(value=ctp_kind_label(ctp_box.get("account_kind")))
        self.ctp_kind_choices = list(CTP_KIND_LABELS.values())
        self.ctp_live_td_var = ctk.StringVar(value=str(live_box.get("td_front") or ""))
        self.ctp_live_md_var = ctk.StringVar(value=str(live_box.get("md_front") or ""))
        from src.trader.go_live import STAGE_LABELS, current_stage_from_cfg, load_state as load_go_live
        from src.trader.go_live import stage_hint, stage_label

        go_state = load_go_live(self.root_dir)
        yaml_stage = ((trade.get("go_live") or {}) if isinstance(trade.get("go_live"), dict) else {}).get("stage")
        stage_now = go_state.get("stage") or yaml_stage or current_stage_from_cfg(self.cfg)
        self.go_live_choices = list(STAGE_LABELS.values())
        self.go_live_var = ctk.StringVar(value=stage_label(stage_now))
        self.go_live_hint_var = ctk.StringVar(value=stage_hint(stage_now))
        from src.trader.go_live import allows_strategy_orders, apply_stage_to_cfg, account_kind_for_stage

        apply_stage_to_cfg(self.cfg, stage_now)
        if allows_strategy_orders(stage_now):
            self.exec_var.set("ctp")
        self.ctp_kind_var.set(ctp_kind_label(account_kind_for_stage(stage_now)))
        from src.data.quote_check import source_label

        quote_label = source_label((self.cfg.get("data") or {}).get("source"))
        self.source_var = ctk.StringVar(value=quote_label)
        self.dry_var = ctk.BooleanVar(value=bool(self.cfg.get("dry_run", True)))
        tf_label, tf_custom = ui_parts(str(st.get("timeframe", "15")))
        trail_label, trail_custom = ui_parts(str(st.get("trail_timeframe", "30")))
        self.tf_var = ctk.StringVar(value=tf_label)
        self.tf_custom_var = ctk.StringVar(value=tf_custom)
        self.trail_var = ctk.StringVar(value=trail_label)
        self.trail_custom_var = ctk.StringVar(value=trail_custom)
        self.symbols_var = ctk.StringVar(value=",".join(str(x) for x in st.get("symbols", ["bu", "c"])))
        # 关注列表。下拉框选中的那一个，就是运行页顶部显示的当前合约。
        self._watch_symbols = self._parse_list(self.symbols_var.get())
        self.watch_choice_var = ctk.StringVar(
            value=self._watch_symbols[0] if self._watch_symbols else "（还没有）"
        )
        self.watch_add_var = ctk.StringVar(value="")
        self._pick_baseline: dict[str, Any] | None = None
        self._pick_running = False
        self._pick_cancel = threading.Event()
        self.volume_var = ctk.StringVar(value=str(trade.get("volume", 1)))
        self.max_slip_var = ctk.StringVar(value=str(trade.get("max_entry_slip_atr", 0.5)))
        self.max_slip_points_var = ctk.StringVar(value=str(trade.get("max_entry_slip_points", 0)))
        self.sizing_mode_var = ctk.StringVar(value=str(risk.get("sizing_mode", "risk_pct")))
        self.capital_var = ctk.StringVar(value=str(risk.get("capital", 1000000)))
        self.risk_pct_var = ctk.StringVar(value=str(risk.get("risk_per_trade_pct", 0.5)))
        from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

        self.margin_usage_pct_var = ctk.StringVar(
            value=str(risk.get("margin_usage_pct", DEFAULT_MARGIN_USAGE_PCT))
        )
        self.margin_var = ctk.StringVar(value=str(risk.get("margin_per_lot", 0) or 0))
        from src.risk.dynamic import DynamicExposure

        _dyn = DynamicExposure.from_config(self.cfg)
        self.dyn_enabled_var = ctk.BooleanVar(value=bool(_dyn.enabled))
        self.dyn_dd_half_var = ctk.StringVar(value=str(_dyn.dd_half_pct))
        self.dyn_dd_pause_var = ctk.StringVar(value=str(_dyn.dd_pause_pct))
        self.dyn_atr_half_var = ctk.StringVar(value=str(_dyn.atr_half_mult))
        self.dyn_atr_pause_var = ctk.StringVar(value=str(_dyn.atr_pause_mult))
        from src.risk.kelly import (
            DEFAULT_FRACTION,
            DEFAULT_MAX_F_PCT,
            kelly_box,
            label_from_fraction,
            read_manual_factors,
            status_text,
            win_rate_pct_display,
        )

        _kelly = kelly_box(self.cfg)
        self.kelly_enabled_var = ctk.BooleanVar(value=bool(_kelly.get("enabled")))
        self.kelly_frac_var = ctk.StringVar(
            value=label_from_fraction(_kelly.get("fraction", DEFAULT_FRACTION))
        )
        self.kelly_max_var = ctk.StringVar(
            value=str(_kelly.get("max_f_pct", DEFAULT_MAX_F_PCT))
        )
        _kr, _kp = read_manual_factors(_kelly)
        self.kelly_win_var = ctk.StringVar(value=win_rate_pct_display(_kr))
        self.kelly_payoff_var = ctk.StringVar(value="" if _kp is None else f"{_kp:g}")
        self.kelly_status_var = ctk.StringVar(value=status_text(self.cfg))
        # 关注合约+保证金只认总表；margin_map_var 给回测页抄「当前品种保证金」用。
        from src.risk.universe_table import DEFAULT_REL_PATH, summarize_universe

        self.margin_table_path_var = ctk.StringVar(
            value=str(risk.get("universe_table") or risk.get("margin_table") or DEFAULT_REL_PATH)
        )
        self.margin_map_var = ctk.StringVar(
            value=summarize_universe(
                list(st.get("symbols") or []),
                risk.get("contracts") or {},
            )
        )
        self.scan_var = ctk.StringVar(value=str(sch.get("scan_interval_sec", 30)))
        # 公共交易时段：界面显示中文，保存时换回 all/day/night。只拦开新仓。
        from src.strategy.session_scope import scope_from_cfg, scope_label

        self.session_scope_var = ctk.StringVar(value=scope_label(scope_from_cfg(self.cfg)))
        self.eth_min_var = ctk.StringVar(value=str(_eth_minutes_from_strategy(st)))
        # 各策略必要旋钮。只在选对应策略时真正用到，保存进各自小节。
        body_box = st.get("daily_body") or {}
        trend_box = st.get("trend_follow") or {}
        st_box = st.get("supertrend") or {}
        dual_box = st.get("dual_ma") or {}
        # 箱沿外再让几跳：多单箱底外、空单箱顶外。默认 1。
        self.body_stop_ticks_var = ctk.StringVar(value=str(body_box.get("stop_extra_ticks", 1)))
        from src.strategy.factory import format_atr_mult

        # 跳空/箱宽/急破：开盘前 ATR 的倍数，0=关闭。换品种时跟着波动走。
        self.body_skip_gap_var = ctk.StringVar(
            value=format_atr_mult(body_box.get("skip_gap_atr_mult"), 2.5)
        )
        self.body_max_box_var = ctk.StringVar(
            value=format_atr_mult(body_box.get("max_box_atr_mult"), 5.0)
        )
        self.body_max_chase_var = ctk.StringVar(
            value=format_atr_mult(body_box.get("max_chase_atr_mult"), 1.0)
        )
        # 回抽时看哪条 EMA；以及收盘超过它几跳才算有效打穿。
        self.body_ema_period_var = ctk.StringVar(value=str(body_box.get("ema_period", 20)))
        self.body_ema_buffer_var = ctk.StringVar(value=str(body_box.get("ema_buffer_ticks", 2)))
        # 开仓截止钟点：过了这个钟点，日盘/夜盘这一段不再开新仓。默认沿用旧版 10:30 / 22:15。
        self.body_day_cutoff_var = ctk.StringVar(value=str(body_box.get("day_cutoff", "10:30")))
        self.body_night_cutoff_var = ctk.StringVar(value=str(body_box.get("night_cutoff", "22:15")))
        self.body_eth2_var = ctk.StringVar(value=str(body_box.get("eth_minutes2", 30)))
        hold_box = st.get("float_hold") or {}
        from src.strategy.float_hold import (
            DIRECTION_LABELS,
            EMA_PAIR_LABELS,
            MACD_COMBO_LABELS,
            MACD_TF_LABELS,
            direction_label,
            ema_pair_label,
            macd_combo_label,
            macd_timeframe_label,
        )

        self.float_direction_var = ctk.StringVar(
            value=direction_label(hold_box.get("direction"))
        )
        self._float_direction_labels = list(DIRECTION_LABELS.values())
        self.float_macd_tf_var = ctk.StringVar(
            value=macd_timeframe_label(hold_box.get("macd_timeframe", "5"))
        )
        self._float_macd_tf_labels = list(MACD_TF_LABELS.values())
        self.float_macd_combo_var = ctk.StringVar(
            value=macd_combo_label(
                hold_box.get("macd_combo")
                or f"{hold_box.get('macd_fast', 12)}/{hold_box.get('macd_slow', 26)}/{hold_box.get('macd_signal', 9)}"
            )
        )
        self._float_macd_combo_labels = list(MACD_COMBO_LABELS)
        self.float_macd_fast_var = ctk.StringVar(value=str(hold_box.get("macd_fast", 12)))
        self.float_macd_slow_var = ctk.StringVar(value=str(hold_box.get("macd_slow", 26)))
        self.float_macd_signal_var = ctk.StringVar(value=str(hold_box.get("macd_signal", 9)))
        self.float_ema_pair_var = ctk.StringVar(
            value=ema_pair_label(
                hold_box.get("dir_ema_fast", 5), hold_box.get("dir_ema_slow", 20)
            )
        )
        self._float_ema_pair_labels = list(EMA_PAIR_LABELS)
        self.float_trend_align_var = ctk.BooleanVar(
            value=bool(hold_box.get("trend_align_enabled", False))
        )
        self.float_trend_slope_var = ctk.StringVar(
            value=str(hold_box.get("trend_slope_min_deg", 0))
        )
        self.float_trend_lookback_var = ctk.StringVar(
            value=str(hold_box.get("trend_slope_lookback", 5))
        )
        self.float_eth_var = ctk.StringVar(value=str(hold_box.get("eth_minutes", 60)))
        self.float_cluster_width_var = ctk.StringVar(
            value=str(hold_box.get("cluster_max_width", 10))
        )
        self.float_stop_atr_var = ctk.StringVar(
            value=format_atr_mult(hold_box.get("stop_atr_mult"), 1.5)
        )
        self.float_skip_gap_var = ctk.StringVar(
            value=format_atr_mult(hold_box.get("skip_gap_atr_mult"), 0.0)
        )
        self.float_max_box_var = ctk.StringVar(
            value=format_atr_mult(hold_box.get("max_box_atr_mult"), 0.0)
        )
        self.float_max_chase_var = ctk.StringVar(
            value=format_atr_mult(hold_box.get("max_chase_atr_mult"), 0.0)
        )
        self.float_gap_align_var = ctk.StringVar(
            value=format_atr_mult(hold_box.get("gap_align_atr_mult"), 0.0)
        )
        self.float_be_atr_var = ctk.StringVar(
            value=format_atr_mult(hold_box.get("be_atr_mult"), 2.0)
        )
        self.float_day_cutoff_var = ctk.StringVar(value=str(hold_box.get("day_cutoff", "14:45")))
        self.float_night_cutoff_var = ctk.StringVar(value=str(hold_box.get("night_cutoff", "22:50")))
        from src.strategy.market_state import range_params_for_daily_body

        body_rp = range_params_for_daily_body(self.cfg)
        self.body_filter_enabled_var = ctk.BooleanVar(value=bool(body_rp.enabled))
        self.body_filter_adx_period_var = ctk.StringVar(value=str(body_rp.adx_period))
        self.body_filter_atr_period_var = ctk.StringVar(value=str(body_rp.atr_period))
        self.body_filter_lookback_var = ctk.StringVar(value=str(body_rp.lookback))
        self.body_filter_adx_upper_var = ctk.StringVar(value=f"{body_rp.adx_upper:g}")
        body_atr_high_ui = 0 if body_rp.atr_high_pct >= 0.999 else body_rp.atr_high_pct * 100
        self.body_filter_atr_high_var = ctk.StringVar(value=f"{body_atr_high_ui:g}")
        # 止损触发方式（全策略共用）：触及即止损（实时看影线）/ 收盘价确认（等这根K线收完）。
        from src.strategy.types import (
            entry_mode_from_cfg,
            entry_mode_label,
            stop_mode_from_cfg,
            stop_mode_label,
        )

        self.stop_mode_var = ctk.StringVar(value=stop_mode_label(stop_mode_from_cfg(self.cfg)))
        self.entry_mode_var = ctk.StringVar(value=entry_mode_label(entry_mode_from_cfg(self.cfg)))
        # 通用止损（独立于策略自身止损）：关闭 / SAR / 前高低点 / ATR 吊灯。
        from src.strategy.generic_stop import generic_stop_from_cfg, generic_stop_label

        gs_cfg = generic_stop_from_cfg(self.cfg)
        self.generic_stop_var = ctk.StringVar(value=generic_stop_label(gs_cfg.kind))
        self.generic_stop_lookback_var = ctk.StringVar(value=str(gs_cfg.lookback))
        self.generic_stop_sar_start_var = ctk.StringVar(value=f"{gs_cfg.sar_af_start:g}")
        self.generic_stop_sar_step_var = ctk.StringVar(value=f"{gs_cfg.sar_af_step:g}")
        self.generic_stop_sar_max_var = ctk.StringVar(value=f"{gs_cfg.sar_af_max:g}")
        self.generic_stop_atr_period_var = ctk.StringVar(value=str(gs_cfg.atr_period))
        self.generic_stop_atr_mult_var = ctk.StringVar(value=f"{gs_cfg.atr_mult:g}")
        self.generic_stop_be_atr_var = ctk.StringVar(value=f"{gs_cfg.be_atr_mult:g}")
        # 趋势策略各自的行情过滤（ADX 没趋势或 ATR 波动压缩时不开新仓）。
        self._init_trend_filter_vars()
        self.trend_entry_bars_var = ctk.StringVar(value=str(trend_box.get("entry_bars", 36)))
        self.trend_atr_mult_var = ctk.StringVar(value=str(trend_box.get("atr_mult", 2.0)))
        self.st_atr_period_var = ctk.StringVar(value=str(st_box.get("atr_period", 10)))
        self.st_multiplier_var = ctk.StringVar(value=str(st_box.get("multiplier", 3.0)))
        self.dual_short_var = ctk.StringVar(value=str(dual_box.get("short_period", 20)))
        self.dual_long_var = ctk.StringVar(value=str(dual_box.get("long_period", 60)))
        self.dual_reverse_var = ctk.BooleanVar(value=bool(dual_box.get("reverse_on_cross", True)))
        macd_box = st.get("ema_swing") or st.get("macd_swing") or {}
        from src.strategy.macd_swing import (
            parse_close_guard_min,
            parse_dir_pair,
            parse_ema_len,
            parse_sig_triple,
        )

        d_fast, d_slow = parse_dir_pair(
            macd_box.get("dir_ema_pair"),
            macd_box.get("dir_ema_fast", macd_box.get("ema_fast", 60)),
            macd_box.get("dir_ema_slow", macd_box.get("ema_slow", 120)),
        )
        self.macd_swing_dir_fast_var = ctk.StringVar(value=str(d_fast))
        self.macd_swing_dir_slow_var = ctk.StringVar(value=str(d_slow))
        sa, sb, sc = parse_sig_triple(
            macd_box.get("sig_ema_triple"),
            macd_box.get("sig_ema_a", 5),
            macd_box.get("sig_ema_b", 10),
            macd_box.get("sig_ema_c", 30),
        )
        self.macd_swing_sig_a_var = ctk.StringVar(value=str(sa))
        self.macd_swing_sig_b_var = ctk.StringVar(value=str(sb))
        self.macd_swing_sig_c_var = ctk.StringVar(value=str(sc))
        stop_n = parse_ema_len(macd_box.get("stop_ema", 120), 120)
        side_n = parse_ema_len(macd_box.get("side_ema", stop_n), stop_n)
        slope_n = parse_ema_len(macd_box.get("slope_ema", stop_n), stop_n)
        self.macd_swing_side_ema_var = ctk.StringVar(value=str(side_n))
        self.macd_swing_stop_ema_var = ctk.StringVar(value=str(stop_n))
        self.macd_swing_slope_ema_var = ctk.StringVar(value=str(slope_n))
        self.macd_swing_slope_var = ctk.StringVar(value=str(macd_box.get("slope_min_deg", 3)))
        self.macd_swing_slope_lookback_var = ctk.StringVar(value=str(macd_box.get("slope_lookback", 20)))
        self.macd_swing_slope_exit_var = ctk.BooleanVar(value=bool(macd_box.get("slope_exit", False)))
        self.macd_swing_close_guard_var = ctk.StringVar(
            value=str(parse_close_guard_min(macd_box.get("no_open_before_close_min", 30), 30))
        )
        boll_box = st.get("bollinger") or {}
        self.boll_window_var = ctk.StringVar(value=str(boll_box.get("boll_window", 20)))
        self.boll_dev_var = ctk.StringVar(value=str(boll_box.get("boll_dev", 2.0)))
        self.boll_atr_mult_var = ctk.StringVar(value=str(boll_box.get("atr_stop_mult", 1.5)))
        sar_box = st.get("sar_trend") or {}
        self.sar_ma_filter_var = ctk.StringVar(value=str(sar_box.get("ma_filter", 20)))
        self.sar_af_step_var = ctk.StringVar(value=str(sar_box.get("af_step", 0.02)))
        multi_box = st.get("multi_factor") or {}
        self.multi_threshold_var = ctk.StringVar(value=str(multi_box.get("threshold", 0.6)))
        self.multi_ma_short_var = ctk.StringVar(value=str(multi_box.get("ma_short", 20)))
        self.multi_ma_long_var = ctk.StringVar(value=str(multi_box.get("ma_long", 60)))
        self.multi_channel_var = ctk.StringVar(value=str(multi_box.get("channel", 20)))
        self.multi_atr_mult_var = ctk.StringVar(value=str(multi_box.get("atr_stop_mult", 2.0)))
        self.multi_disagree_var = ctk.StringVar(value=str(multi_box.get("disagree_max", 0.7)))
        self.multi_use_sar_var = ctk.BooleanVar(value=bool(multi_box.get("use_sar", True)))
        self._init_cta_filter_vars(multi_box)
        swing_box = st.get("swing_trend") or {}
        from src.strategy.swing_trend import DEFAULT_SCALE, SCALE_LABELS, resolve_scale_pair, scale_label

        scale_code, _trend_tf, _sig_tf = resolve_scale_pair(swing_box if isinstance(swing_box, dict) else {})
        self._swing_scale_by_label = dict(SCALE_LABELS)
        self._swing_label_by_scale = {code: label for code, label in SCALE_LABELS.items()}
        self.swing_scale_var = ctk.StringVar(
            value=self._swing_label_by_scale.get(scale_code, scale_label(DEFAULT_SCALE))
        )
        self.swing_daily_ma_var = ctk.StringVar(value=str(swing_box.get("daily_ma", 20)))
        self.swing_unit_var = ctk.StringVar(value=str(swing_box.get("unit_volume", 1)))
        self.swing_max_adds_var = ctk.StringVar(value=str(swing_box.get("max_adds", 2)))
        self.swing_add_atr_var = ctk.StringVar(value=str(swing_box.get("add_min_atr", 1.0)))
        self.swing_reduce_atr_var = ctk.StringVar(value=str(swing_box.get("reduce_extend_atr", 2.5)))
        sbox = st.get("swing_box") or {}
        self.sbox_lookback_var = ctk.StringVar(value=str(sbox.get("lookback", 40)))
        self.sbox_height_atr_var = ctk.StringVar(value=str(sbox.get("height_atr", 1.2)))
        self.sbox_adx_min_var = ctk.StringVar(value=str(sbox.get("adx_min", 20)))
        self.sbox_stop_atr_var = ctk.StringVar(value=str(sbox.get("stop_atr", 0.5)))
        self.sbox_trail_atr_var = ctk.StringVar(value=str(sbox.get("trail_atr", 2.0)))
        self.sbox_trail_h_var = ctk.StringVar(value=str(sbox.get("trail_start_h", 1.0)))
        self.flatten_var = ctk.StringVar(
            value=",".join(str(x) for x in st.get("flatten_times", ["14:55", "22:55"]))
        )
        # 默认开：到点强平。关掉后仓可以留过收盘，用来对比隔夜表现。
        self.flatten_at_close_var = ctk.BooleanVar(value=bool(st.get("flatten_at_close", True)))
        # 收盘后关机（默认关；有夜盘会等到夜盘结束）
        rt = self.cfg.get("runtime") or {}
        self.auto_shutdown_var = ctk.BooleanVar(value=bool(rt.get("auto_shutdown_after_close", False)))

        self.broker_var = ctk.StringVar(value=str(acc.get("broker", "")))
        self.channel_var = ctk.StringVar(value=str(acc.get("channel", "")))
        self.user_var = ctk.StringVar(value=str(acc.get("user_id", "")))
        self.pwd_env_var = ctk.StringVar(value=str(acc.get("password_env", "FUTURES_PASSWORD")))
        self.password_var = ctk.StringVar(value=str(secrets.get("password", "") or acc.get("resolved_password", "")))
        # 天勤行情要单独登录。账号写进 config.yaml 的 data.tq.user，密码写进 secrets.local.yaml。
        tq_cfg = (self.cfg.get("data") or {}).get("tq") or {}
        self.tq_user_var = ctk.StringVar(value=str(tq_cfg.get("user") or secrets.get("tq_user", "") or ""))
        self.tq_password_var = ctk.StringVar(
            value=str(secrets.get("tq_password", "") or tq_cfg.get("password", "") or "")
        )
        self.captcha_var = ctk.StringVar(value=str(acc.get("captcha_mode", "pause")))
        # 配置里 skip_auto_login=true 表示不自动填密码。界面用「自动登录」开关，打开才去点登录。
        self.auto_login_var = ctk.BooleanVar(value=not bool(acc.get("skip_auto_login", True)))

        self.app_path_var = ctk.StringVar(value=str(app.get("path", "")))
        self.app_title_var = ctk.StringVar(value=str(app.get("window_title", "期货通")))
        self.startup_wait_var = ctk.StringVar(value=str(app.get("startup_wait_sec", 15)))

        self.max_loss_var = ctk.StringVar(value=str(risk.get("max_daily_loss", 1000)))
        self.min_interval_var = ctk.StringVar(value=str(risk.get("min_order_interval_sec", 60)))
        self.max_orders_var = ctk.StringVar(value=str(risk.get("max_orders_per_day", 20)))

        # —— 回测 / 优化页专用（与实盘配置分开选，避免改乱挂机参数）——
        bt = self.cfg.get("backtest") or {}
        default_sym = (st.get("symbols") or ["bu2612"])[0]
        self.bt_engine_var = ctk.StringVar(value=_ui_engine(st.get("engine", "daily_body")))
        self.bt_source_var = ctk.StringVar(value=quote_label)
        self.bt_symbol_var = ctk.StringVar(value=str(default_sym))
        self.bt_tf_var = ctk.StringVar(value=tf_label)
        self.bt_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.bt_trail_var = ctk.StringVar(value=trail_label)
        self.bt_trail_custom_var = ctk.StringVar(value=trail_custom)
        self.bt_range_var = ctk.StringVar(value=str(bt.get("range_preset", "全部可用")))
        # 配置里没写开始日期时，默认当年1月1日。今年打开就是 2026-01-01。
        default_start = self._initial_start_date(bt.get("start_date", ""))
        self.bt_start_var = ctk.StringVar(value=default_start)
        self.bt_end_var = ctk.StringVar(value=str(bt.get("end_date", "")))
        ths_chart = ((self.cfg.get("data") or {}).get("ths_chart") or {})
        self.bt_ths_bars_var = ctk.StringVar(value=str(ths_chart.get("max_bars", 800)))
        # 成交近似：下一根开盘成交与滑点跳数，只影响回测。配置页和历史回测页共用这一组。
        self.bt_fill_next_open_var = ctk.BooleanVar(value=bool(bt.get("fill_on_next_open", True)))
        self.bt_slippage_var = ctk.StringVar(value=str(bt.get("slippage_ticks", 1)))
        # 回测页自己的资金限制，默认抄配置页，改这里不会写回实盘配置。
        self.bt_sizing_var = ctk.StringVar(value=str(risk.get("sizing_mode", "risk_pct")))
        self.bt_capital_var = ctk.StringVar(value=str(risk.get("capital", 14000)))
        self.bt_risk_pct_var = ctk.StringVar(value=str(risk.get("risk_per_trade_pct", 0.5)))
        self.bt_volume_var = ctk.StringVar(value=str(trade.get("volume", 1)))
        self.bt_min_vol_var = ctk.StringVar(value=str(risk.get("min_volume", 1)))
        self.bt_max_loss_var = ctk.StringVar(value=str(risk.get("max_daily_loss", 500)))
        self.bt_interval_var = ctk.StringVar(value=str(risk.get("min_order_interval_sec", 60)))
        self.bt_max_orders_var = ctk.StringVar(value=str(risk.get("max_orders_per_day", 20)))
        self.bt_margin_var = ctk.StringVar(value=str(risk.get("margin_per_lot", 0) or 0))
        from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT as _DEF_MGN_PCT

        self.bt_margin_usage_pct_var = ctk.StringVar(
            value=str(risk.get("margin_usage_pct", _DEF_MGN_PCT))
        )
        # 默认开着，和实盘一致。关掉后固定 1 手，用来看策略本身。配置页没有这个开关。
        self.bt_limits_on_var = ctk.BooleanVar(value=True)
        # 打开程序时先跟配置页一致。之后可以在回测页单独改，点「按配置页重新填入」再抄回来。
        self.bt_flatten_var = ctk.BooleanVar(value=bool(st.get("flatten_at_close", True)))
        # 按主力合约表的时段自动换月。默认开，让历史回测跟实盘一样换月日仍用旧合约、次日切新主力。
        self.bt_rollover_var = ctk.BooleanVar(value=bool(bt.get("rollover_main", True)))
        # 真实账户口径。关=每段、每一笔都按同一份起始资金；开=剩余权益接到下一段，段内手数也跟着走。
        self.bt_rollover_chain_var = ctk.BooleanVar(value=False)
        # 交叉测试不再跟某一次回测比。周期和手数用本页自己的数字，策略会把每一套都测。
        self.pk_engine_var = ctk.StringVar(value=self.bt_engine_var.get())
        # 交叉测试自己的行情来源。打开时跟配置页一样，之后可以单独改。
        self.pk_source_var = ctk.StringVar(value=quote_label)
        self.pk_tf_var = ctk.StringVar(value=tf_label)
        self.pk_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.pk_trail_var = ctk.StringVar(value=trail_label)
        self.pk_trail_custom_var = ctk.StringVar(value=trail_custom)
        # 交叉测试三组勾选：策略、合约、周期。信号和止损共用周期。
        self._pk_engine_vars: dict[str, ctk.BooleanVar] = {}
        self._pk_symbol_vars: dict[str, ctk.BooleanVar] = {}
        self._pk_period_vars: dict[str, ctk.BooleanVar] = {}
        self.pk_range_var = ctk.StringVar(value=self.bt_range_var.get())
        self.pk_start_var = ctk.StringVar(value=default_start)
        self.pk_end_var = ctk.StringVar(value=self.bt_end_var.get())
        self.pk_flatten_var = ctk.BooleanVar(value=bool(self.bt_flatten_var.get()))
        self.pk_limits_on_var = ctk.BooleanVar(value=True)
        self.pk_sizing_var = ctk.StringVar(value="fixed")
        self.pk_capital_var = ctk.StringVar(value=self.bt_capital_var.get())
        self.pk_risk_pct_var = ctk.StringVar(value=self.bt_risk_pct_var.get())
        self.pk_volume_var = ctk.StringVar(value=self.bt_volume_var.get())
        self.pk_min_vol_var = ctk.StringVar(value=self.bt_min_vol_var.get())
        self.pk_max_loss_var = ctk.StringVar(value=self.bt_max_loss_var.get())
        self.pk_interval_var = ctk.StringVar(value=self.bt_interval_var.get())
        self.pk_max_orders_var = ctk.StringVar(value=self.bt_max_orders_var.get())
        self.pk_margin_var = ctk.StringVar(value="0")

        self.opt_engine_var = ctk.StringVar(value=_ui_engine(st.get("engine", "daily_body")))
        self.opt_source_var = ctk.StringVar(value=quote_label)
        self.opt_symbol_var = ctk.StringVar(value=str(default_sym))
        self.opt_tf_var = ctk.StringVar(value=tf_label)
        self.opt_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.opt_trail_var = ctk.StringVar(value=trail_label)
        self.opt_trail_custom_var = ctk.StringVar(value=trail_custom)
        self.opt_range_var = ctk.StringVar(value=str(bt.get("range_preset", "全部可用")))
        self.opt_start_var = ctk.StringVar(value=default_start)
        self.opt_end_var = ctk.StringVar(value=str(bt.get("end_date", "")))
        self.opt_trials_var = ctk.StringVar(value=str(bt.get("max_trials", 80)))
        self.opt_depth_var = ctk.StringVar(value=str(bt.get("optimize_depth", "标准")))
        # 检验比例默认 30%，和优化器里的默认切断一致。可在 10～50 之间手调。
        self.opt_holdout_pct_var = ctk.StringVar(value=str(bt.get("optimize_holdout_pct", 30)))
        self.opt_mode_var = ctk.StringVar(value=str(bt.get("optimize_mode", "一次切断")))
        self.opt_walk_steps_var = ctk.StringVar(value=str(bt.get("optimize_walk_steps", 3)))
        # 写入第几组：1=训练最优；填其它编号会先按检验段重新复核再写。
        self.opt_write_index_var = ctk.StringVar(value="1")
        # 打开＝衰减重跑：搜完对照正在用的参数，默认不写配置。
        self.opt_compare_var = ctk.BooleanVar(value=False)
        # 写入时额外留一份整页配置存档（配置页可一键应用）。和下面参数版本不是同一份。
        self.opt_save_preset_var = ctk.BooleanVar(value=True)
        self._last_opt_params: dict = {}
        self._last_opt_engine: str = ""
        # 后一段验收过了才是 True。没过时参数仍显示在结果里，但不能写入配置。
        self._last_opt_accepted: bool = False
        self._last_opt_reason: str = ""
        self._last_opt_start: str = ""
        self._last_opt_end: str = ""
        # 这次优化的全部打分行和运行设置，供「写入第几组」复核用。
        self._last_opt_trials: list[dict] = []
        self._last_opt_ctx: dict = {}
        # 对照结果：打开「对照正在用的参数」跑完后才有。全面更好时写入按钮改成切实盘。
        self._last_evolve_better: bool = False
        self._last_evolve_reason: str = ""
        self._last_opt_compared: bool = False

        # 复盘页：日期默认今天；策略对照默认跟配置里正在用的那一套，也可以改成「只看盘面」
        from datetime import date as _date

        from src.review.replay import label_for_engine

        self.rv_date_var = ctk.StringVar(value=_date.today().isoformat())
        self.rv_symbol_var = ctk.StringVar(value=str(default_sym))
        self.rv_tf_var = ctk.StringVar(value=tf_label)
        self.rv_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.rv_trail_var = ctk.StringVar(value=trail_label)
        self.rv_trail_custom_var = ctk.StringVar(value=trail_custom)
        self.rv_scope_var = ctk.StringVar(value="前夜盘+日盘")
        self.rv_engine_var = ctk.StringVar(
            value=label_for_engine(_ui_engine(st.get("engine", "daily_body")))
        )

        # 账户分析：初始资金默认跟配置里的账户权益一样。出入金和结算是你手工填的金额。
        self.ac_capital_var = ctk.StringVar(value=str(risk.get("capital", 100000)))
        self.ac_cash_var = ctk.StringVar(value="")
        self.ac_settle_var = ctk.StringVar(value="")
        self.ac_span_var = ctk.StringVar(value="全部")
        self._ac_chart = "权益"
        self._ac_cal = futures_trading_day().replace(day=1)
        self._ac_report = None
        self._signal_book_mtime = 0.0
        # 资金表控件还没建好时，刷新说明不能去读这个字典（Tk 会当成窗口属性报错）。
        self._ac_fund_values: dict[str, Any] = {}
        # 账户分析换账时，下拉框改回去不要再弹一次确认。
        self._account_switch_busy = False
        from src.account.workspace import display_label, load_active_id

        self.ac_account_var = ctk.StringVar(value=display_label(load_active_id(self.root_dir), self.root_dir))

        # 规律候选：只扫统计关系，不下单。日期默认跟回测页同一套，改这里不会改回测。
        self.rs_source_var = ctk.StringVar(value=quote_label)
        self.rs_symbol_var = ctk.StringVar(value=str(default_sym))
        self.rs_tf_var = ctk.StringVar(value=tf_label)
        self.rs_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.rs_range_var = ctk.StringVar(value=str(bt.get("range_preset", "全部可用")))
        self.rs_start_var = ctk.StringVar(value=default_start)
        self.rs_end_var = ctk.StringVar(value=str(bt.get("end_date", "")))
        self.rs_scope_var = ctk.StringVar(value="扫关注名单全部")
        self.rs_cross_var = ctk.BooleanVar(value=True)
        self._rs_busy = False
        self._rs_check_vars: dict[str, ctk.BooleanVar] = {}


    def _init_cta_filter_vars(
        self, multi_box: dict
    ) -> None:
        """多因子加权打分自己一份行情闸门控件。"""
        from src.strategy.cta_filter import CtaFilterParams

        self._cta_filter_vars = {}
        for kind, box in (
            ("multi_factor", multi_box),
        ):
            p = CtaFilterParams.from_box(box if isinstance(box, dict) else {}, kind=kind)
            self._cta_filter_vars[kind] = {
                "enabled": ctk.BooleanVar(value=p.enabled),
                "use_adx": ctk.BooleanVar(value=p.use_adx),
                "use_atr": ctk.BooleanVar(value=p.use_atr),
                "use_disagree": ctk.BooleanVar(value=p.use_disagree),
                "adx_flat": ctk.StringVar(value=f"{p.adx_flat:g}"),
                "adx_trend": ctk.StringVar(value=f"{p.adx_trend:g}"),
                "atr_lookback": ctk.StringVar(value=str(p.atr_lookback)),
                "atr_low_pct": ctk.StringVar(value=f"{p.atr_low_pct * 100:g}"),
                "weak_score": ctk.StringVar(value=f"{p.weak_score:g}"),
                "weak_size": ctk.StringVar(value=f"{p.weak_size:g}"),
                "weak_votes": ctk.StringVar(value=str(p.weak_votes)),
            }


    def _fill_cta_filter_vars(
        self, multi_box: dict
    ) -> None:
        """从配置重新灌进加权打分闸门控件。"""
        from src.strategy.cta_filter import CtaFilterParams

        boxes = {
            "multi_factor": multi_box,
        }
        for kind, box in boxes.items():
            p = CtaFilterParams.from_box(box if isinstance(box, dict) else {}, kind=kind)
            vars_ = self._cta_filter_vars[kind]
            vars_["enabled"].set(p.enabled)
            vars_["use_adx"].set(p.use_adx)
            vars_["use_atr"].set(p.use_atr)
            vars_["use_disagree"].set(p.use_disagree)
            vars_["adx_flat"].set(f"{p.adx_flat:g}")
            vars_["adx_trend"].set(f"{p.adx_trend:g}")
            vars_["atr_lookback"].set(str(p.atr_lookback))
            vars_["atr_low_pct"].set(f"{p.atr_low_pct * 100:g}")
            vars_["weak_score"].set(f"{p.weak_score:g}")
            vars_["weak_size"].set(f"{p.weak_size:g}")
            vars_["weak_votes"].set(str(p.weak_votes))


    def _write_cta_filter_vars(self, box: dict, kind: str) -> None:
        """把界面上的闸门写回这一套策略小节。"""
        from src.strategy.cta_filter import default_cta_filter

        vars_ = self._cta_filter_vars[kind]
        seed = default_cta_filter(kind)
        try:
            adx_flat = min(90.0, max(5.0, float(vars_["adx_flat"].get())))
        except ValueError:
            adx_flat = float(seed["adx_flat"])
        try:
            adx_trend = min(90.0, max(adx_flat, float(vars_["adx_trend"].get())))
        except ValueError:
            adx_trend = float(seed["adx_trend"])
        try:
            lookback = max(10, int(vars_["atr_lookback"].get()))
        except ValueError:
            lookback = int(seed["atr_lookback"])
        try:
            atr_low = min(90.0, max(1.0, float(vars_["atr_low_pct"].get()))) / 100.0
        except ValueError:
            atr_low = float(seed["atr_low_pct"])
        try:
            weak_score = min(0.95, max(0.0, float(vars_["weak_score"].get())))
        except ValueError:
            weak_score = float(seed["weak_score"])
        try:
            weak_size = min(1.0, max(0.1, float(vars_["weak_size"].get())))
        except ValueError:
            weak_size = float(seed["weak_size"])
        try:
            weak_votes = max(0, int(vars_["weak_votes"].get()))
        except ValueError:
            weak_votes = int(seed["weak_votes"])
        box["cta_filter"] = {
            "enabled": bool(vars_["enabled"].get()),
            "use_adx": bool(vars_["use_adx"].get()),
            "use_atr": bool(vars_["use_atr"].get()),
            "use_disagree": bool(vars_["use_disagree"].get()),
            "adx_period": 14,
            "adx_flat": adx_flat,
            "adx_trend": adx_trend,
            "atr_lookback": lookback,
            "atr_low_pct": atr_low,
            "weak_score": weak_score,
            "weak_size": weak_size,
            "weak_votes": weak_votes,
        }


    def _init_trend_filter_vars(self) -> None:
        """海龟、SuperTrend、双均线、布林、SAR、长线各自一份趋势过滤控件。"""
        from src.strategy.market_state import TREND_FILTER_SECTIONS, trend_params_for_section

        self._trend_filter_vars = {}
        for section in TREND_FILTER_SECTIONS:
            p = trend_params_for_section(self.cfg, section)
            self._trend_filter_vars[section] = {
                "enabled": ctk.BooleanVar(value=bool(p.enabled)),
                "adx_period": ctk.StringVar(value=str(p.adx_period)),
                "atr_period": ctk.StringVar(value=str(p.atr_period)),
                "lookback": ctk.StringVar(value=str(p.lookback)),
                "adx_flat": ctk.StringVar(value=f"{p.adx_flat:g}"),
                "atr_low_pct": ctk.StringVar(value=f"{p.atr_low_pct * 100:g}"),
            }


    def _fill_trend_filter_vars(self) -> None:
        """从配置重新灌进趋势策略各自的过滤控件。"""
        from src.strategy.market_state import TREND_FILTER_SECTIONS, trend_params_for_section

        for section in TREND_FILTER_SECTIONS:
            p = trend_params_for_section(self.cfg, section)
            vars_ = self._trend_filter_vars[section]
            vars_["enabled"].set(bool(p.enabled))
            vars_["adx_period"].set(str(p.adx_period))
            vars_["atr_period"].set(str(p.atr_period))
            vars_["lookback"].set(str(p.lookback))
            vars_["adx_flat"].set(f"{p.adx_flat:g}")
            vars_["atr_low_pct"].set(f"{p.atr_low_pct * 100:g}")


    def _write_trend_filter_vars(self, box: dict, section: str) -> None:
        """把界面上的趋势过滤写回这一套策略小节。"""
        vars_ = self._trend_filter_vars[section]
        box["filter_enabled"] = bool(vars_["enabled"].get())
        try:
            box["filter_adx_period"] = max(2, min(200, int(vars_["adx_period"].get())))
        except ValueError:
            box["filter_adx_period"] = 14
        try:
            box["filter_atr_period"] = max(2, min(200, int(vars_["atr_period"].get())))
        except ValueError:
            box["filter_atr_period"] = 14
        try:
            box["filter_lookback"] = max(10, min(500, int(vars_["lookback"].get())))
        except ValueError:
            box["filter_lookback"] = 60
        try:
            box["filter_adx_flat"] = min(60.0, max(5.0, float(vars_["adx_flat"].get())))
        except ValueError:
            box["filter_adx_flat"] = 20.0
        try:
            atr_ui = min(90.0, max(5.0, float(vars_["atr_low_pct"].get())))
        except ValueError:
            atr_ui = 20.0
        box["filter_atr_low_pct"] = atr_ui / 100.0


    def _parse_list(self, raw: str) -> list[str]:
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]


    def _reload_values_only(self) -> None:
        self.cfg = load_config()
        st = self.cfg.get("strategy", {})
        acc = self.cfg.get("account", {})
        app = self.cfg.get("app", {})
        trade = self.cfg.get("trade", {})
        risk = self.cfg.get("risk", {})
        sch = self.cfg.get("schedule", {})
        secrets = load_secrets()

        self.engine_var.set(_ui_engine(st.get("engine", "daily_body")))
        self.exec_var.set(str(trade.get("executor", "sim")))
        from src.trader.ctp_logic import ctp_kind_label

        ctp_box = trade.get("ctp") or {}
        live_box = ctp_box.get("live") if isinstance(ctp_box.get("live"), dict) else {}
        self.ctp_kind_var.set(ctp_kind_label(ctp_box.get("account_kind")))
        self.ctp_live_td_var.set(str(live_box.get("td_front") or ""))
        self.ctp_live_md_var.set(str(live_box.get("md_front") or ""))
        from src.trader.go_live import load_state as load_go_live, stage_hint, stage_label

        go_state = load_go_live(self.root_dir)
        yaml_stage = ((trade.get("go_live") or {}) if isinstance(trade.get("go_live"), dict) else {}).get("stage")
        stage_now = go_state.get("stage") or yaml_stage
        if stage_now:
            self.go_live_var.set(stage_label(stage_now))
            self.go_live_hint_var.set(stage_hint(stage_now))
        from src.data.quote_check import source_label

        self.source_var.set(source_label((self.cfg.get("data") or {}).get("source")))
        tq_cfg = (self.cfg.get("data") or {}).get("tq") or {}
        self.tq_user_var.set(str(tq_cfg.get("user") or secrets.get("tq_user", "") or ""))
        self.tq_password_var.set(
            str(secrets.get("tq_password", "") or tq_cfg.get("password", "") or "")
        )
        self._refresh_tq_hint()
        self.dry_var.set(bool(self.cfg.get("dry_run", True)))
        self._set_period_vars(str(st.get("timeframe", "15")), self.tf_var, self.tf_custom_var)
        self._set_period_vars(str(st.get("trail_timeframe", "30")), self.trail_var, self.trail_custom_var)
        self.symbols_var.set(",".join(str(x) for x in st.get("symbols", ["bu", "c"])))
        # 这是从文件读回来，不要再写一遍，免得把刚读到的名单覆盖掉。
        self._set_watch_codes(self._parse_list(self.symbols_var.get()), save=False)
        self.volume_var.set(str(trade.get("volume", 1)))
        self.max_slip_var.set(str(trade.get("max_entry_slip_atr", 0.5)))
        self.max_slip_points_var.set(str(trade.get("max_entry_slip_points", 0)))
        self.sizing_mode_var.set(str(risk.get("sizing_mode", "risk_pct")))
        self.capital_var.set(str(risk.get("capital", 1000000)))
        self.risk_pct_var.set(str(risk.get("risk_per_trade_pct", 0.5)))
        from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

        self.margin_usage_pct_var.set(str(risk.get("margin_usage_pct", DEFAULT_MARGIN_USAGE_PCT)))
        self.margin_var.set(str(risk.get("margin_per_lot", 0) or 0))
        if hasattr(self, "bt_margin_usage_pct_var"):
            self.bt_margin_usage_pct_var.set(self.margin_usage_pct_var.get())
        from src.risk.universe_table import DEFAULT_REL_PATH, summarize_universe

        self.margin_table_path_var.set(
            str(risk.get("universe_table") or risk.get("margin_table") or DEFAULT_REL_PATH)
        )
        self.margin_map_var.set(
            summarize_universe(list(st.get("symbols") or []), risk.get("contracts") or {})
        )
        try:
            self._refresh_margin_table_summary()
        except Exception:  # noqa: BLE001
            pass
        self.scan_var.set(str(sch.get("scan_interval_sec", 30)))
        from src.strategy.session_scope import scope_from_cfg, scope_label

        self.session_scope_var.set(scope_label(scope_from_cfg(self.cfg)))
        self.eth_min_var.set(str(_eth_minutes_from_strategy(st)))
        body_box = st.get("daily_body") or {}
        trend_box = st.get("trend_follow") or {}
        st_box = st.get("supertrend") or {}
        dual_box = st.get("dual_ma") or {}
        from src.strategy.factory import format_atr_mult

        self.body_stop_ticks_var.set(str(body_box.get("stop_extra_ticks", 1)))
        self.body_skip_gap_var.set(format_atr_mult(body_box.get("skip_gap_atr_mult"), 2.5))
        self.body_max_box_var.set(format_atr_mult(body_box.get("max_box_atr_mult"), 5.0))
        self.body_max_chase_var.set(format_atr_mult(body_box.get("max_chase_atr_mult"), 1.0))
        self.body_ema_period_var.set(str(body_box.get("ema_period", 20)))
        self.body_ema_buffer_var.set(str(body_box.get("ema_buffer_ticks", 2)))
        self.body_day_cutoff_var.set(str(body_box.get("day_cutoff", "10:30")))
        self.body_night_cutoff_var.set(str(body_box.get("night_cutoff", "22:15")))
        self.body_eth2_var.set(str(body_box.get("eth_minutes2", 30)))
        hold_box = st.get("float_hold") or {}
        from src.strategy.float_hold import (
            direction_label,
            ema_pair_label,
            macd_combo_label,
            macd_timeframe_label,
        )

        self.float_direction_var.set(direction_label(hold_box.get("direction")))
        self.float_macd_tf_var.set(macd_timeframe_label(hold_box.get("macd_timeframe", "5")))
        self.float_macd_fast_var.set(str(hold_box.get("macd_fast", 12)))
        self.float_macd_slow_var.set(str(hold_box.get("macd_slow", 26)))
        self.float_macd_signal_var.set(str(hold_box.get("macd_signal", 9)))
        self.float_macd_combo_var.set(
            macd_combo_label(
                hold_box.get("macd_combo")
                or f"{hold_box.get('macd_fast', 12)}/{hold_box.get('macd_slow', 26)}/{hold_box.get('macd_signal', 9)}"
            )
        )
        self.float_ema_pair_var.set(
            ema_pair_label(hold_box.get("dir_ema_fast", 5), hold_box.get("dir_ema_slow", 20))
        )
        self.float_trend_align_var.set(bool(hold_box.get("trend_align_enabled", False)))
        self.float_trend_slope_var.set(str(hold_box.get("trend_slope_min_deg", 0)))
        self.float_trend_lookback_var.set(str(hold_box.get("trend_slope_lookback", 5)))
        self.float_eth_var.set(str(hold_box.get("eth_minutes", 60)))
        self.float_cluster_width_var.set(str(hold_box.get("cluster_max_width", 10)))
        self.float_stop_atr_var.set(format_atr_mult(hold_box.get("stop_atr_mult"), 1.5))
        self.float_skip_gap_var.set(format_atr_mult(hold_box.get("skip_gap_atr_mult"), 0.0))
        self.float_max_box_var.set(format_atr_mult(hold_box.get("max_box_atr_mult"), 0.0))
        self.float_max_chase_var.set(format_atr_mult(hold_box.get("max_chase_atr_mult"), 0.0))
        self.float_gap_align_var.set(format_atr_mult(hold_box.get("gap_align_atr_mult"), 0.0))
        self.float_be_atr_var.set(format_atr_mult(hold_box.get("be_atr_mult"), 2.0))
        self.float_day_cutoff_var.set(str(hold_box.get("day_cutoff", "14:45")))
        self.float_night_cutoff_var.set(str(hold_box.get("night_cutoff", "22:50")))
        from src.strategy.market_state import range_params_for_daily_body

        body_rp = range_params_for_daily_body(self.cfg)
        self.body_filter_enabled_var.set(bool(body_rp.enabled))
        self.body_filter_adx_period_var.set(str(body_rp.adx_period))
        self.body_filter_atr_period_var.set(str(body_rp.atr_period))
        self.body_filter_lookback_var.set(str(body_rp.lookback))
        self.body_filter_adx_upper_var.set(f"{body_rp.adx_upper:g}")
        body_atr_high_ui = 0 if body_rp.atr_high_pct >= 0.999 else body_rp.atr_high_pct * 100
        self.body_filter_atr_high_var.set(f"{body_atr_high_ui:g}")
        from src.strategy.types import (
            entry_mode_from_cfg,
            entry_mode_label,
            stop_mode_from_cfg,
            stop_mode_label,
        )

        self.stop_mode_var.set(stop_mode_label(stop_mode_from_cfg(self.cfg)))
        self.entry_mode_var.set(entry_mode_label(entry_mode_from_cfg(self.cfg)))
        from src.strategy.generic_stop import generic_stop_from_cfg, generic_stop_label

        gs_cfg = generic_stop_from_cfg(self.cfg)
        self.generic_stop_var.set(generic_stop_label(gs_cfg.kind))
        self.generic_stop_lookback_var.set(str(gs_cfg.lookback))
        self.generic_stop_sar_start_var.set(f"{gs_cfg.sar_af_start:g}")
        self.generic_stop_sar_step_var.set(f"{gs_cfg.sar_af_step:g}")
        self.generic_stop_sar_max_var.set(f"{gs_cfg.sar_af_max:g}")
        self.generic_stop_atr_period_var.set(str(gs_cfg.atr_period))
        self.generic_stop_atr_mult_var.set(f"{gs_cfg.atr_mult:g}")
        self.generic_stop_be_atr_var.set(f"{gs_cfg.be_atr_mult:g}")
        self._fill_trend_filter_vars()
        self.trend_entry_bars_var.set(str(trend_box.get("entry_bars", 36)))
        self.trend_atr_mult_var.set(str(trend_box.get("atr_mult", 2.0)))
        self.st_atr_period_var.set(str(st_box.get("atr_period", 10)))
        self.st_multiplier_var.set(str(st_box.get("multiplier", 3.0)))
        self.dual_short_var.set(str(dual_box.get("short_period", 20)))
        self.dual_long_var.set(str(dual_box.get("long_period", 60)))
        self.dual_reverse_var.set(bool(dual_box.get("reverse_on_cross", True)))
        macd_box = st.get("ema_swing") or st.get("macd_swing") or {}
        from src.strategy.macd_swing import (
            parse_close_guard_min,
            parse_dir_pair,
            parse_ema_len,
            parse_sig_triple,
        )

        d_fast, d_slow = parse_dir_pair(
            macd_box.get("dir_ema_pair"),
            macd_box.get("dir_ema_fast", macd_box.get("ema_fast", 60)),
            macd_box.get("dir_ema_slow", macd_box.get("ema_slow", 120)),
        )
        self.macd_swing_dir_fast_var.set(str(d_fast))
        self.macd_swing_dir_slow_var.set(str(d_slow))
        sa, sb, sc = parse_sig_triple(
            macd_box.get("sig_ema_triple"),
            macd_box.get("sig_ema_a", 5),
            macd_box.get("sig_ema_b", 10),
            macd_box.get("sig_ema_c", 30),
        )
        self.macd_swing_sig_a_var.set(str(sa))
        self.macd_swing_sig_b_var.set(str(sb))
        self.macd_swing_sig_c_var.set(str(sc))
        stop_n = parse_ema_len(macd_box.get("stop_ema", 120), 120)
        side_n = parse_ema_len(macd_box.get("side_ema", stop_n), stop_n)
        slope_n = parse_ema_len(macd_box.get("slope_ema", stop_n), stop_n)
        self.macd_swing_side_ema_var.set(str(side_n))
        self.macd_swing_stop_ema_var.set(str(stop_n))
        self.macd_swing_slope_ema_var.set(str(slope_n))
        self.macd_swing_slope_var.set(str(macd_box.get("slope_min_deg", 3)))
        self.macd_swing_slope_lookback_var.set(str(macd_box.get("slope_lookback", 20)))
        self.macd_swing_slope_exit_var.set(bool(macd_box.get("slope_exit", False)))
        self.macd_swing_close_guard_var.set(
            str(parse_close_guard_min(macd_box.get("no_open_before_close_min", 30), 30))
        )
        boll_box = st.get("bollinger") or {}
        self.boll_window_var.set(str(boll_box.get("boll_window", 20)))
        self.boll_dev_var.set(str(boll_box.get("boll_dev", 2.0)))
        self.boll_atr_mult_var.set(str(boll_box.get("atr_stop_mult", 1.5)))
        sar_box = st.get("sar_trend") or {}
        self.sar_ma_filter_var.set(str(sar_box.get("ma_filter", 20)))
        self.sar_af_step_var.set(str(sar_box.get("af_step", 0.02)))
        multi_box = st.get("multi_factor") or {}
        self.multi_threshold_var.set(str(multi_box.get("threshold", 0.6)))
        self.multi_ma_short_var.set(str(multi_box.get("ma_short", 20)))
        self.multi_ma_long_var.set(str(multi_box.get("ma_long", 60)))
        self.multi_channel_var.set(str(multi_box.get("channel", 20)))
        self.multi_atr_mult_var.set(str(multi_box.get("atr_stop_mult", 2.0)))
        self.multi_disagree_var.set(str(multi_box.get("disagree_max", 0.7)))
        self.multi_use_sar_var.set(bool(multi_box.get("use_sar", True)))
        self._fill_cta_filter_vars(multi_box)
        swing_box = st.get("swing_trend") or {}
        from src.strategy.swing_trend import DEFAULT_SCALE, resolve_scale_pair, scale_label

        scale_code, _t, _s = resolve_scale_pair(swing_box if isinstance(swing_box, dict) else {})
        self.swing_scale_var.set(self._swing_label_by_scale.get(scale_code, scale_label(DEFAULT_SCALE)))
        self.swing_daily_ma_var.set(str(swing_box.get("daily_ma", 20)))
        self.swing_unit_var.set(str(swing_box.get("unit_volume", 1)))
        self.swing_max_adds_var.set(str(swing_box.get("max_adds", 2)))
        self.swing_add_atr_var.set(str(swing_box.get("add_min_atr", 1.0)))
        self.swing_reduce_atr_var.set(str(swing_box.get("reduce_extend_atr", 2.5)))
        sbox = st.get("swing_box") or {}
        self.sbox_lookback_var.set(str(sbox.get("lookback", 40)))
        self.sbox_height_atr_var.set(str(sbox.get("height_atr", 1.2)))
        self.sbox_adx_min_var.set(str(sbox.get("adx_min", 20)))
        self.sbox_stop_atr_var.set(str(sbox.get("stop_atr", 0.5)))
        self.sbox_trail_atr_var.set(str(sbox.get("trail_atr", 2.0)))
        self.sbox_trail_h_var.set(str(sbox.get("trail_start_h", 1.0)))
        self.flatten_var.set(",".join(str(x) for x in st.get("flatten_times", ["14:55", "22:55"])))
        self.flatten_at_close_var.set(bool(st.get("flatten_at_close", True)))
        rt = self.cfg.get("runtime") or {}
        self.auto_shutdown_var.set(bool(rt.get("auto_shutdown_after_close", False)))
        self.broker_var.set(str(acc.get("broker", "")))
        self.channel_var.set(str(acc.get("channel", "")))
        self.user_var.set(str(acc.get("user_id", "")))
        self.pwd_env_var.set(str(acc.get("password_env", "FUTURES_PASSWORD")))
        self.password_var.set(str(secrets.get("password", "") or ""))
        self.captcha_var.set(str(acc.get("captcha_mode", "pause")))
        self.auto_login_var.set(not bool(acc.get("skip_auto_login", True)))
        self.app_path_var.set(str(app.get("path", "")))
        self.app_title_var.set(str(app.get("window_title", "期货通")))
        self.startup_wait_var.set(str(app.get("startup_wait_sec", 15)))
        self.max_loss_var.set(str(risk.get("max_daily_loss", 1000)))
        self.min_interval_var.set(str(risk.get("min_order_interval_sec", 60)))
        self.max_orders_var.set(str(risk.get("max_orders_per_day", 20)))
        from src.risk.dynamic import DynamicExposure

        _dyn = DynamicExposure.from_config(self.cfg)
        self.dyn_enabled_var.set(bool(_dyn.enabled))
        self.dyn_dd_half_var.set(str(_dyn.dd_half_pct))
        self.dyn_dd_pause_var.set(str(_dyn.dd_pause_pct))
        self.dyn_atr_half_var.set(str(_dyn.atr_half_mult))
        self.dyn_atr_pause_var.set(str(_dyn.atr_pause_mult))
        from src.risk.kelly import (
            DEFAULT_FRACTION,
            DEFAULT_MAX_F_PCT,
            kelly_box,
            label_from_fraction,
            read_manual_factors,
            win_rate_pct_display,
        )

        _kelly = kelly_box(self.cfg)
        self.kelly_enabled_var.set(bool(_kelly.get("enabled")))
        self.kelly_frac_var.set(label_from_fraction(_kelly.get("fraction", DEFAULT_FRACTION)))
        self.kelly_max_var.set(str(_kelly.get("max_f_pct", DEFAULT_MAX_F_PCT)))
        _kr, _kp = read_manual_factors(_kelly)
        self.kelly_win_var.set(win_rate_pct_display(_kr))
        self.kelly_payoff_var.set("" if _kp is None else f"{_kp:g}")
        self._refresh_kelly_status()
        self._copy_risk_to_backtest()


    def _dynamic_cfg_from_ui(self) -> dict[str, Any]:
        """配置页动态减仓几个框收成一段配置。数字填错就用默认。"""
        from src.risk.dynamic import DynamicExposure

        fallback = DynamicExposure()
        try:
            dd_half = float(self.dyn_dd_half_var.get() or fallback.dd_half_pct)
        except ValueError:
            dd_half = fallback.dd_half_pct
        try:
            dd_pause = float(self.dyn_dd_pause_var.get() or fallback.dd_pause_pct)
        except ValueError:
            dd_pause = fallback.dd_pause_pct
        try:
            atr_half = max(1.01, float(self.dyn_atr_half_var.get() or fallback.atr_half_mult))
        except ValueError:
            atr_half = fallback.atr_half_mult
        try:
            atr_pause = max(1.01, float(self.dyn_atr_pause_var.get() or fallback.atr_pause_mult))
        except ValueError:
            atr_pause = fallback.atr_pause_mult
        return {
            "enabled": bool(self.dyn_enabled_var.get()),
            "dd_half_pct": dd_half,
            "dd_pause_pct": dd_pause,
            "atr_half_mult": atr_half,
            "atr_pause_mult": atr_pause,
            "atr_period": fallback.atr_period,
            "atr_lookback": fallback.atr_lookback,
            "reduce_held": True,
        }


    def _scan_interval_value(self) -> int:
        """
        扫描间隔秒。

        读界面上的值并夹在 5~600 秒：太小会把行情源打到限流（下载失败会退化成收盘判止损），
        太大则止损发现明显滞后。空着或写错按 30 秒。
        """
        try:
            value = int(str(self.scan_var.get()).strip())
        except (TypeError, ValueError):
            value = 30
        return max(5, min(600, value))


    def _kelly_cfg_from_ui(self) -> dict[str, Any]:
        """配置页凯利几个框收成一段。胜率和盈亏比由你手填。"""
        from src.risk.kelly import DEFAULT_FRACTION, DEFAULT_MAX_F_PCT, fraction_from_label

        old = (self.cfg.get("risk") or {}).get("kelly") if isinstance(self.cfg.get("risk"), dict) else {}
        old = dict(old) if isinstance(old, dict) else {}
        try:
            max_f = float(self.kelly_max_var.get() or DEFAULT_MAX_F_PCT)
        except (TypeError, ValueError):
            max_f = DEFAULT_MAX_F_PCT
        old["enabled"] = bool(self.kelly_enabled_var.get())
        old["fraction"] = fraction_from_label(self.kelly_frac_var.get())
        old["max_f_pct"] = max(0.05, min(10.0, max_f))
        win_text = str(self.kelly_win_var.get() or "").strip()
        pay_text = str(self.kelly_payoff_var.get() or "").strip()
        if win_text:
            try:
                old["win_rate_pct"] = float(win_text)
            except ValueError:
                pass
        else:
            old.pop("win_rate_pct", None)
            old.pop("win_rate", None)
        if pay_text:
            try:
                old["payoff"] = float(pay_text)
            except ValueError:
                pass
        else:
            old.pop("payoff", None)
        old.pop("snapshot", None)
        old.pop("min_trades", None)
        old.setdefault("fraction", DEFAULT_FRACTION)
        return old


    def _refresh_kelly_status(self, *_args: object) -> None:
        """开关或占用比例一改，立刻把说明那一行算新。"""
        from src.risk.kelly import status_text

        box = (self.cfg.setdefault("risk", {})).setdefault("kelly", {})
        if not isinstance(box, dict):
            box = {}
            self.cfg["risk"]["kelly"] = box
        merged = self._kelly_cfg_from_ui()
        self.cfg["risk"]["kelly"] = merged
        if hasattr(self, "kelly_status_var"):
            self.kelly_status_var.set(status_text(self.cfg))


    def _apply_ui_to_cfg(self) -> dict[str, Any]:
        """把表单写回内存 cfg，并处理密码。"""
        self.cfg["dry_run"] = bool(self.dry_var.get())
        from src.data.quote_check import normalize_source

        data = self.cfg.setdefault("data", {})
        data["source"] = normalize_source(self.source_var.get())
        if data["source"] == "ths":
            # 同花顺图不能当实盘来源。误选时退回新浪，避免扫描去抢鼠标。
            data["source"] = "akshare"
        data.pop("quote_check", None)
        # 天勤账号进主配置；密码不写这里，保存时进 secrets.local.yaml。
        tq_box = data.get("tq") if isinstance(data.get("tq"), dict) else {}
        tq_box["user"] = self.tq_user_var.get().strip()
        data["tq"] = tq_box

        st = self.cfg.setdefault("strategy", {})
        st["engine"] = self.engine_var.get()
        st["timeframe"] = self._period_code(self.tf_var, self.tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.trail_var, self.trail_custom_var)
        # 以关注下拉框里的整份名单为准。选中的排在第一个，其余合约也要留下来。
        st["symbols"] = list(self._watch_symbols)
        st["flatten_times"] = self._parse_list(self.flatten_var.get())
        st["flatten_at_close"] = bool(self.flatten_at_close_var.get())

        body = st.setdefault("daily_body", {})
        try:
            body["eth_minutes"] = max(0, int(self.eth_min_var.get()))
        except ValueError:
            body["eth_minutes"] = 20
        try:
            body["stop_extra_ticks"] = max(0, int(self.body_stop_ticks_var.get()))
        except ValueError:
            body["stop_extra_ticks"] = 1
        def _read_atr_mult(raw: str, default: float) -> float:
            try:
                number = float(str(raw).strip())
            except ValueError:
                return float(default)
            if number != number:
                return float(default)
            return max(0.0, min(20.0, number))

        body["skip_gap_atr_mult"] = _read_atr_mult(self.body_skip_gap_var.get(), 2.5)
        body["max_box_atr_mult"] = _read_atr_mult(self.body_max_box_var.get(), 5.0)
        body["max_chase_atr_mult"] = _read_atr_mult(self.body_max_chase_var.get(), 1.0)
        body.pop("skip_gap_ticks", None)
        body.pop("max_box_ticks", None)
        body.pop("max_chase_ticks", None)
        try:
            body["ema_period"] = max(0, int(self.body_ema_period_var.get()))
        except ValueError:
            body["ema_period"] = 20
        try:
            body["ema_buffer_ticks"] = max(0, int(self.body_ema_buffer_var.get()))
        except ValueError:
            body["ema_buffer_ticks"] = 2
        body["day_cutoff"] = _clock_text(self.body_day_cutoff_var.get(), "10:30")
        body["night_cutoff"] = _clock_text(self.body_night_cutoff_var.get(), "22:15")
        try:
            body["eth_minutes2"] = max(0, int(self.body_eth2_var.get()))
        except ValueError:
            body["eth_minutes2"] = 30
        from src.strategy.types import entry_mode_from_label, stop_mode_from_label

        # 止损/开仓触发方式都是全策略公共设置，写在 strategy 根。
        st["stop_mode"] = stop_mode_from_label(self.stop_mode_var.get())
        st["entry_mode"] = entry_mode_from_label(self.entry_mode_var.get())
        # 通用止损：独立于策略自身止损，写在 strategy.generic_stop。
        from src.strategy.generic_stop import generic_stop_from_label

        gs = st.setdefault("generic_stop", {})
        if not isinstance(gs, dict):
            gs = {}
            st["generic_stop"] = gs
        gs["kind"] = generic_stop_from_label(self.generic_stop_var.get())

        def _num(var, default: float, low: float, high: float) -> float:
            """界面参数统一收口：填错就用默认值，超范围夹到边界。"""
            try:
                value = float(var.get())
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        gs["lookback"] = int(_num(self.generic_stop_lookback_var, 20.0, 2.0, 200.0))
        gs["sar_af_start"] = _num(self.generic_stop_sar_start_var, 0.02, 0.001, 0.5)
        gs["sar_af_step"] = _num(self.generic_stop_sar_step_var, 0.02, 0.001, 0.5)
        gs["sar_af_max"] = _num(self.generic_stop_sar_max_var, 0.2, 0.05, 0.9)
        gs["atr_period"] = int(_num(self.generic_stop_atr_period_var, 14.0, 2.0, 200.0))
        gs["atr_mult"] = _num(self.generic_stop_atr_mult_var, 3.0, 0.1, 10.0)
        gs["be_atr_mult"] = _num(self.generic_stop_be_atr_var, 1.0, 0.1, 10.0)
        body["filter_enabled"] = bool(self.body_filter_enabled_var.get())
        body["filter_adx_period"] = int(_num(self.body_filter_adx_period_var, 14.0, 2.0, 200.0))
        body["filter_atr_period"] = int(_num(self.body_filter_atr_period_var, 14.0, 2.0, 200.0))
        body["filter_lookback"] = int(_num(self.body_filter_lookback_var, 60.0, 10.0, 500.0))
        body["filter_adx_upper"] = _num(self.body_filter_adx_upper_var, 30.0, 0.0, 90.0)
        body.pop("filter_adx_expand", None)
        body_high_raw = _num(self.body_filter_atr_high_var, 80.0, 0.0, 100.0)
        body["filter_atr_high_pct"] = 0.0 if body_high_raw <= 1 else body_high_raw / 100.0
        st.pop("market_filter", None)
        # 旧版 ATR 底线 / 回撤止损已取消，保存时清掉，避免配置里还留着误导人。
        body.pop("initial_atr_mult", None)
        body.pop("reverse_atr_mult", None)
        # 止损触发方式已挪到 strategy 根，旧键顺手清掉。
        body.pop("stop_mode", None)

        hold = st.setdefault("float_hold", {})
        from src.strategy.float_hold import (
            format_macd_combo,
            normalize_direction_key,
            normalize_macd_timeframe,
            parse_ema_pair_label,
            parse_macd_combo,
        )

        hold["direction"] = normalize_direction_key(self.float_direction_var.get())
        hold["macd_timeframe"] = normalize_macd_timeframe(self.float_macd_tf_var.get())

        try:
            macd_fast = int(float(self.float_macd_fast_var.get()))
            macd_slow = int(float(self.float_macd_slow_var.get()))
            macd_signal = int(float(self.float_macd_signal_var.get()))
        except (TypeError, ValueError):
            macd_fast, macd_slow, macd_signal = parse_macd_combo(self.float_macd_combo_var.get())
        macd_fast, macd_slow, macd_signal = parse_macd_combo(
            f"{macd_fast}/{macd_slow}/{macd_signal}"
        )
        hold["macd_fast"] = macd_fast
        hold["macd_slow"] = macd_slow
        hold["macd_signal"] = macd_signal
        hold["macd_combo"] = format_macd_combo(macd_fast, macd_slow, macd_signal)
        ema_fast, ema_slow = parse_ema_pair_label(self.float_ema_pair_var.get())
        hold["dir_ema_fast"] = ema_fast
        hold["dir_ema_slow"] = ema_slow
        hold["dir_ema_pair"] = f"{ema_fast}/{ema_slow}"
        hold["trend_align_enabled"] = bool(self.float_trend_align_var.get())
        hold["trend_align_fast"] = 60
        hold["trend_align_slow"] = 120
        try:
            hold["trend_slope_min_deg"] = max(0.0, min(80.0, float(self.float_trend_slope_var.get())))
        except (TypeError, ValueError):
            hold["trend_slope_min_deg"] = 0.0
        try:
            hold["trend_slope_lookback"] = max(2, min(60, int(float(self.float_trend_lookback_var.get()))))
        except (TypeError, ValueError):
            hold["trend_slope_lookback"] = 5
        try:
            hold["eth_minutes"] = max(0, int(self.float_eth_var.get()))
        except ValueError:
            hold["eth_minutes"] = 60
        try:
            hold["cluster_max_width"] = max(1, min(80, int(float(self.float_cluster_width_var.get()))))
        except (TypeError, ValueError):
            hold["cluster_max_width"] = 10
        hold["stop_atr_mult"] = max(0.1, _read_atr_mult(self.float_stop_atr_var.get(), 1.5))
        hold["skip_gap_atr_mult"] = _read_atr_mult(self.float_skip_gap_var.get(), 0.0)
        hold["max_box_atr_mult"] = _read_atr_mult(self.float_max_box_var.get(), 0.0)
        hold["max_chase_atr_mult"] = _read_atr_mult(self.float_max_chase_var.get(), 0.0)
        hold["gap_align_atr_mult"] = _read_atr_mult(self.float_gap_align_var.get(), 0.0)
        hold["be_atr_mult"] = _read_atr_mult(self.float_be_atr_var.get(), 2.0)
        hold["day_cutoff"] = _clock_text(self.float_day_cutoff_var.get(), "14:45")
        hold["night_cutoff"] = _clock_text(self.float_night_cutoff_var.get(), "22:50")
        hold["eth_minutes2"] = 0
        hold.pop("skip_gap_ticks", None)
        hold.pop("max_box_ticks", None)
        hold.pop("max_chase_ticks", None)
        hold.pop("stop_extra_ticks", None)
        hold.pop("be_r_mult", None)
        hold.pop("stop_mode", None)

        trend_box = st.setdefault("trend_follow", {})
        try:
            trend_box["entry_bars"] = max(5, int(self.trend_entry_bars_var.get()))
        except ValueError:
            trend_box["entry_bars"] = 36
        try:
            trend_box["atr_mult"] = max(0.1, float(self.trend_atr_mult_var.get()))
        except ValueError:
            trend_box["atr_mult"] = 2.0

        st_box = st.setdefault("supertrend", {})
        try:
            st_box["atr_period"] = max(2, int(self.st_atr_period_var.get()))
        except ValueError:
            st_box["atr_period"] = 10
        try:
            st_box["multiplier"] = max(0.1, float(self.st_multiplier_var.get()))
        except ValueError:
            st_box["multiplier"] = 3.0

        dual_box = st.setdefault("dual_ma", {})
        try:
            dual_box["short_period"] = max(2, int(self.dual_short_var.get()))
        except ValueError:
            dual_box["short_period"] = 20
        try:
            dual_box["long_period"] = max(3, int(self.dual_long_var.get()))
        except ValueError:
            dual_box["long_period"] = 60
        dual_box["reverse_on_cross"] = bool(self.dual_reverse_var.get())
        macd_box = st.setdefault("ema_swing", {})
        st.pop("macd_swing", None)
        macd_box.pop("cluster_width", None)
        for dead in ("macd_fast", "macd_slow", "macd_signal", "macd_combo"):
            macd_box.pop(dead, None)
        macd_box["flatten_at_close"] = False
        from src.strategy.macd_swing import (
            format_sig_triple,
            parse_close_guard_min,
            parse_dir_pair,
            parse_ema_len,
            parse_sig_triple,
        )

        d_fast, d_slow = parse_dir_pair(
            None,
            parse_ema_len(self.macd_swing_dir_fast_var.get(), 60),
            parse_ema_len(self.macd_swing_dir_slow_var.get(), 120),
        )
        macd_box["dir_ema_fast"] = d_fast
        macd_box["dir_ema_slow"] = d_slow
        macd_box["dir_ema_pair"] = f"{d_fast}/{d_slow}"
        macd_box["ema_fast"] = d_fast
        macd_box["ema_slow"] = d_slow
        self.macd_swing_dir_fast_var.set(str(d_fast))
        self.macd_swing_dir_slow_var.set(str(d_slow))
        sa, sb, sc = parse_sig_triple(
            None,
            parse_ema_len(self.macd_swing_sig_a_var.get(), 5),
            parse_ema_len(self.macd_swing_sig_b_var.get(), 10),
            parse_ema_len(self.macd_swing_sig_c_var.get(), 30),
        )
        macd_box["sig_ema_a"] = sa
        macd_box["sig_ema_b"] = sb
        macd_box["sig_ema_c"] = sc
        macd_box["sig_ema_triple"] = format_sig_triple(sa, sb, sc)
        self.macd_swing_sig_a_var.set(str(sa))
        self.macd_swing_sig_b_var.set(str(sb))
        self.macd_swing_sig_c_var.set(str(sc))
        macd_box["side_ema"] = parse_ema_len(self.macd_swing_side_ema_var.get(), 120)
        macd_box["stop_ema"] = parse_ema_len(self.macd_swing_stop_ema_var.get(), 120)
        macd_box["slope_ema"] = parse_ema_len(self.macd_swing_slope_ema_var.get(), 120)
        self.macd_swing_side_ema_var.set(str(macd_box["side_ema"]))
        self.macd_swing_stop_ema_var.set(str(macd_box["stop_ema"]))
        self.macd_swing_slope_ema_var.set(str(macd_box["slope_ema"]))
        try:
            macd_box["slope_min_deg"] = max(0.0, min(80.0, float(self.macd_swing_slope_var.get())))
        except (TypeError, ValueError):
            macd_box["slope_min_deg"] = 3.0
        try:
            macd_box["slope_lookback"] = max(2, min(120, int(float(self.macd_swing_slope_lookback_var.get()))))
        except (TypeError, ValueError):
            macd_box["slope_lookback"] = 20
        macd_box["slope_exit"] = bool(self.macd_swing_slope_exit_var.get())
        macd_box["no_open_before_close_min"] = parse_close_guard_min(
            self.macd_swing_close_guard_var.get(), 30
        )
        self.macd_swing_close_guard_var.set(str(macd_box["no_open_before_close_min"]))
        boll_box = st.setdefault("bollinger", {})
        try:
            boll_box["boll_window"] = max(10, int(self.boll_window_var.get()))
        except ValueError:
            boll_box["boll_window"] = 20
        try:
            boll_box["boll_dev"] = max(0.5, float(self.boll_dev_var.get()))
        except ValueError:
            boll_box["boll_dev"] = 2.0
        try:
            boll_box["atr_stop_mult"] = max(0.1, float(self.boll_atr_mult_var.get()))
        except ValueError:
            boll_box["atr_stop_mult"] = 1.5
        sar_box = st.setdefault("sar_trend", {})
        try:
            sar_box["ma_filter"] = max(2, int(self.sar_ma_filter_var.get()))
        except ValueError:
            sar_box["ma_filter"] = 20
        try:
            sar_box["af_step"] = max(0.005, float(self.sar_af_step_var.get()))
        except ValueError:
            sar_box["af_step"] = 0.02
        multi_box = st.setdefault("multi_factor", {})
        try:
            multi_box["threshold"] = min(0.95, max(0.1, float(self.multi_threshold_var.get())))
        except ValueError:
            multi_box["threshold"] = 0.6
        try:
            multi_box["ma_short"] = max(2, int(self.multi_ma_short_var.get()))
        except ValueError:
            multi_box["ma_short"] = 20
        try:
            multi_box["ma_long"] = max(3, int(self.multi_ma_long_var.get()))
        except ValueError:
            multi_box["ma_long"] = 60
        if multi_box["ma_long"] <= multi_box["ma_short"]:
            multi_box["ma_long"] = multi_box["ma_short"] * 2
        try:
            multi_box["channel"] = max(5, int(self.multi_channel_var.get()))
        except ValueError:
            multi_box["channel"] = 20
        try:
            multi_box["atr_stop_mult"] = max(0.1, float(self.multi_atr_mult_var.get()))
        except ValueError:
            multi_box["atr_stop_mult"] = 2.0
        try:
            multi_box["disagree_max"] = max(0.1, float(self.multi_disagree_var.get()))
        except ValueError:
            multi_box["disagree_max"] = 0.7
        multi_box["use_sar"] = bool(self.multi_use_sar_var.get())
        self._write_cta_filter_vars(multi_box, "multi_factor")
        swing_box = st.setdefault("swing_trend", {})
        from src.strategy.swing_trend import DEFAULT_SCALE, SCALE_PAIRS

        label_to_code = {label: code for code, label in self._swing_label_by_scale.items()}
        scale_code = label_to_code.get(str(self.swing_scale_var.get() or "").strip(), DEFAULT_SCALE)
        if scale_code not in SCALE_PAIRS:
            scale_code = DEFAULT_SCALE
        trend_tf, _scale_signal = SCALE_PAIRS[scale_code]
        swing_box["scale"] = scale_code
        swing_box["trend_timeframe"] = trend_tf
        # 回调进出跟左边信号周期，不再把搭配里的 60 分钟写回去盖掉。
        swing_box["signal_timeframe"] = str(st.get("timeframe") or "60")
        try:
            swing_box["daily_ma"] = max(2, int(self.swing_daily_ma_var.get()))
        except ValueError:
            swing_box["daily_ma"] = 20
        try:
            swing_box["unit_volume"] = max(1, int(self.swing_unit_var.get()))
        except ValueError:
            swing_box["unit_volume"] = 1
        try:
            swing_box["max_adds"] = max(0, int(self.swing_max_adds_var.get()))
        except ValueError:
            swing_box["max_adds"] = 2
        try:
            swing_box["add_min_atr"] = max(0.0, float(self.swing_add_atr_var.get()))
        except ValueError:
            swing_box["add_min_atr"] = 1.0
        try:
            swing_box["reduce_extend_atr"] = max(0.5, float(self.swing_reduce_atr_var.get()))
        except ValueError:
            swing_box["reduce_extend_atr"] = 2.5
        swing_box.setdefault("flatten_at_close", False)
        self._write_trend_filter_vars(trend_box, "trend_follow")
        self._write_trend_filter_vars(st_box, "supertrend")
        self._write_trend_filter_vars(dual_box, "dual_ma")
        self._write_trend_filter_vars(boll_box, "bollinger")
        self._write_trend_filter_vars(sar_box, "sar_trend")
        self._write_trend_filter_vars(swing_box, "swing_trend")
        sbox = st.setdefault("swing_box", {})
        try:
            sbox["lookback"] = max(20, int(self.sbox_lookback_var.get()))
        except ValueError:
            sbox["lookback"] = 40
        try:
            sbox["height_atr"] = max(0.0, float(self.sbox_height_atr_var.get()))
        except ValueError:
            sbox["height_atr"] = 1.2
        try:
            sbox["adx_min"] = max(0.0, float(self.sbox_adx_min_var.get()))
        except ValueError:
            sbox["adx_min"] = 20.0
        try:
            sbox["stop_atr"] = max(0.0, float(self.sbox_stop_atr_var.get()))
        except ValueError:
            sbox["stop_atr"] = 0.5
        try:
            sbox["trail_atr"] = max(0.5, float(self.sbox_trail_atr_var.get()))
        except ValueError:
            sbox["trail_atr"] = 2.0
        try:
            sbox["trail_start_h"] = max(0.1, float(self.sbox_trail_h_var.get()))
        except ValueError:
            sbox["trail_start_h"] = 1.0
        sbox.setdefault("flatten_at_close", False)
        # 保存前清掉已删策略残留，并补齐现有策略缺的默认键。
        from src.strategy.factory import sanitize_strategy_config

        sanitize_strategy_config(self.cfg)

        # 收盘后关机
        rt = self.cfg.setdefault("runtime", {})
        rt["auto_shutdown_after_close"] = bool(self.auto_shutdown_var.get())

        # 回测成交模拟：下一根开盘成交、滑点跳数。只影响回测/优化，配置页和历史回测页共用同一组。
        bt = self.cfg.setdefault("backtest", {})
        bt["fill_on_next_open"] = bool(self.bt_fill_next_open_var.get())
        try:
            bt["slippage_ticks"] = max(0.0, float(self.bt_slippage_var.get() or 0))
        except (TypeError, ValueError):
            bt["slippage_ticks"] = 1.0
        # 策略优化页的搜索设置：写进配置，下次打开还是上次的样子，不再关窗口就回默认。
        depth = self.opt_depth_var.get().strip()
        bt["optimize_depth"] = depth if depth in ("精简", "标准", "完整") else "标准"
        mode = self.opt_mode_var.get().strip()
        bt["optimize_mode"] = mode if mode in ("一次切断", "滚动前进") else "一次切断"
        try:
            bt["optimize_holdout_pct"] = min(80, max(0, int(float(self.opt_holdout_pct_var.get() or 30))))
        except (TypeError, ValueError):
            bt["optimize_holdout_pct"] = 30
        try:
            bt["optimize_walk_steps"] = min(10, max(1, int(float(self.opt_walk_steps_var.get() or 3))))
        except (TypeError, ValueError):
            bt["optimize_walk_steps"] = 3
        try:
            bt["max_trials"] = max(5, int(float(self.opt_trials_var.get() or 80)))
        except (TypeError, ValueError):
            bt["max_trials"] = 80

        trade = self.cfg.setdefault("trade", {})
        trade["executor"] = self.exec_var.get()
        from src.trader.go_live import apply_stage_to_cfg, normalize_stage

        apply_stage_to_cfg(self.cfg, normalize_stage(self.go_live_var.get()))

        ctp_box = trade.setdefault("ctp", {})
        if not isinstance(ctp_box, dict):
            ctp_box = {}
            trade["ctp"] = ctp_box
        from src.trader.go_live import account_kind_for_stage
        from src.trader.ctp_logic import ctp_kind_label

        # 账号种类以上线阶段为准，手工台和策略始终对同一本账。
        ctp_box["account_kind"] = account_kind_for_stage(self.go_live_var.get())
        self.ctp_kind_var.set(ctp_kind_label(ctp_box["account_kind"]))
        live_box = ctp_box.setdefault("live", {})
        if not isinstance(live_box, dict):
            live_box = {}
            ctp_box["live"] = live_box
        # 真实资金账号跟「资金账号」同一格，保存时抄一份到 live，界面不再单独填。
        live_box["user_id"] = str(self.user_var.get() or "").strip()
        live_box["td_front"] = str(self.ctp_live_td_var.get() or "").strip()
        live_box["md_front"] = str(self.ctp_live_md_var.get() or "").strip()
        if not str(live_box.get("broker_id") or "").strip():
            live_box["broker_id"] = str(ctp_box.get("broker_id") or "8080")
        sim_box = ctp_box.setdefault("sim", {})
        if not isinstance(sim_box, dict):
            sim_box = {}
            ctp_box["sim"] = sim_box
        # 顶层仍留给旧配置和仿真窗口。有值时同步进 sim 小节，避免两处对不上。
        for key in ("td_front", "md_front", "user_id", "broker_id"):
            top = str(ctp_box.get(key) or "").strip()
            if top and not str(sim_box.get(key) or "").strip():
                sim_box[key] = top
        try:
            trade["volume"] = int(self.volume_var.get())
        except ValueError:
            trade["volume"] = 1
        try:
            trade["max_entry_slip_atr"] = max(0.0, float(self.max_slip_var.get()))
        except ValueError:
            trade["max_entry_slip_atr"] = 0.5
        try:
            trade["max_entry_slip_points"] = max(0.0, float(self.max_slip_points_var.get()))
        except ValueError:
            trade["max_entry_slip_points"] = 0.0
        if st["symbols"]:
            trade["symbol"] = st["symbols"][0]

        risk_pre = self.cfg.setdefault("risk", {})
        risk_pre["sizing_mode"] = self.sizing_mode_var.get() or "risk_pct"
        try:
            risk_pre["capital"] = float(self.capital_var.get())
        except ValueError:
            risk_pre["capital"] = 1_000_000.0
        try:
            risk_pre["risk_per_trade_pct"] = float(self.risk_pct_var.get())
        except ValueError:
            risk_pre["risk_per_trade_pct"] = 0.5
        try:
            from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

            risk_pre["margin_usage_pct"] = max(
                0.0, float(self.margin_usage_pct_var.get() or DEFAULT_MARGIN_USAGE_PCT)
            )
        except ValueError:
            from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

            risk_pre["margin_usage_pct"] = DEFAULT_MARGIN_USAGE_PCT
        try:
            risk_pre["margin_per_lot"] = max(0.0, float(self.margin_var.get() or 0))
        except ValueError:
            risk_pre["margin_per_lot"] = 0.0
        # 关注合约+保证金只从总表读。
        table_rel = (
            str(self.margin_table_path_var.get() or "").strip() or "config/品种与合约.xlsx"
        )
        risk_pre["universe_table"] = table_rel
        risk_pre["margin_table"] = table_rel
        self._reload_margin_table(silent=True)
        # 「启用限制」只属于回测和优化这一次。留在这里的话，下次保存会把回测开关写进实盘配置。
        risk_pre.pop("apply_limits", None)

        sch = self.cfg.setdefault("schedule", {})
        sch["scan_interval_sec"] = self._scan_interval_value()
        from src.strategy.session_scope import scope_code

        sch["session_scope"] = scope_code(self.session_scope_var.get())

        acc = self.cfg.setdefault("account", {})
        acc["broker"] = self.broker_var.get().strip()
        acc["channel"] = self.channel_var.get().strip()
        acc["user_id"] = self.user_var.get().strip()
        acc["password_env"] = self.pwd_env_var.get().strip() or "FUTURES_PASSWORD"
        acc["captcha_mode"] = self.captcha_var.get()
        acc["skip_auto_login"] = not bool(self.auto_login_var.get())

        app = self.cfg.setdefault("app", {})
        app["path"] = self.app_path_var.get().strip()
        app["window_title"] = self.app_title_var.get().strip() or "期货通"
        try:
            app["startup_wait_sec"] = int(self.startup_wait_var.get())
        except ValueError:
            app["startup_wait_sec"] = 15

        risk = self.cfg.setdefault("risk", {})
        for key, var in (
            ("max_daily_loss", self.max_loss_var),
            ("min_order_interval_sec", self.min_interval_var),
            ("max_orders_per_day", self.max_orders_var),
        ):
            try:
                risk[key] = int(float(var.get()))
            except ValueError:
                pass
        # 已废弃「最大持仓手数」：保存时从配置里删掉，仓位只靠仓位上限%和日最大亏损。
        risk.pop("max_position", None)
        risk["dynamic"] = self._dynamic_cfg_from_ui()
        risk["kelly"] = self._kelly_cfg_from_ui()

        # 密码：写入本地密钥 + 当前进程环境。同花顺密码和天勤密码一次写进同一份文件。
        pwd = self.password_var.get()
        pwd_env = acc["password_env"]
        tq_pwd = self.tq_password_var.get()
        extra = {"tq_password": tq_pwd} if tq_pwd else None
        if pwd or extra:
            save_secrets(password=pwd if pwd else "", extra=extra)
        if pwd:
            os.environ[pwd_env] = pwd
            acc["resolved_password"] = pwd
        else:
            # 保留已有 secrets，不覆盖为空
            existing = str(load_secrets().get("password", "") or "")
            acc["resolved_password"] = existing or os.environ.get(pwd_env, "")

        return self.cfg


    def _merge_disk_gui_coords(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """
        保存前合并磁盘上的最新 trade.gui，避免界面保存用旧坐标盖掉标定结果。
        """
        try:
            disk = load_config(apply_universe=False)
            disk_gui = ((disk.get("trade") or {}).get("gui") or {})
            if disk_gui:
                cfg.setdefault("trade", {}).setdefault("gui", {}).update(disk_gui)
        except Exception as e:  # noqa: BLE001
            logger.warning("合并磁盘坐标失败: {}", e)
        return cfg


    def _clone_cfg(self) -> dict[str, Any]:
        """
        拿出一份和配置页当前内容一样的配置副本。

        启动、单次扫描、回测、优化、复盘都用这份副本。
        副本上改策略或开仓限制，不会改到界面上的配置，也不会改到已经在跑的程序。
        """
        return copy.deepcopy(self._merge_disk_gui_coords(self._apply_ui_to_cfg()))


    def _cfg_for_live_scan(self) -> dict[str, Any]:
        """
        给常驻和单次扫描用的配置。

        关注列表可以有好几个具体合约，但同花顺一次只盯下拉框里选中的那一个。
        其余合约仍保存在配置里，下次可以再选，这一次不会一起扫描。
        """
        cfg = self._clone_cfg()
        current = self.watch_choice_var.get().strip()
        symbols = list((cfg.get("strategy") or {}).get("symbols") or [])
        if not current or current == "（还没有）":
            return cfg
        st = cfg.setdefault("strategy", {})
        st["symbols"] = [current]
        cfg.setdefault("trade", {})["symbol"] = current
        extras = [item for item in symbols if item.lower() != current.lower()]
        if extras:
            self._append_log(
                f"本次只盯 {current}。列表里还有 {'、'.join(extras)}，不会一起挂机。"
            )
        return cfg


    def _sync_bt_periods_after_save(
        self, old_tf: str, old_trail: str, cur_tf: str, cur_trail: str, cfg: dict[str, Any]
    ) -> None:
        """
        配置页保存后，把回测页还跟着旧配置的信号/止损周期一起换过来。

        回测页有自己的周期选择器（故意和实盘分开）。只有它当前值还是配置页上一次
        的值——也就是没在回测页单独改过——才跟着换，免得把特意选的周期冲掉。
        """
        st = cfg.get("strategy") or {}
        pairs = (
            ("信号周期", old_tf, cur_tf, str(st.get("timeframe") or ""),
             self.bt_tf_var, self.bt_tf_custom_var),
            ("止损周期", old_trail, cur_trail, str(st.get("trail_timeframe") or ""),
             self.bt_trail_var, self.bt_trail_custom_var),
        )
        for name, old, cur, new, var, custom in pairs:
            if new and old and cur == old and new != old:
                self._set_period_vars(new, var, custom)
                self._append_log(f"回测页{name}跟着配置页改成 {period_label(new)}。")

