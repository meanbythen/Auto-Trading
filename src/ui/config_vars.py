"""配置到界面的映射：建控件变量、把配置填进表单、只刷新数值。"""

from __future__ import annotations

from typing import Any

from src.data.periods import ui_parts
from src.ui.app_common import _eth_minutes_from_strategy
from src.ui.app_common import _ui_engine
from src.utils import load_config
from src.utils import load_secrets
from src.utils.trading_day import futures_trading_day
import customtkinter as ctk
import threading
from datetime import datetime, timedelta


class ConfigVarsMixin:
    """配置到界面的映射：建控件变量、把配置填进表单、只刷新数值。"""

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
        kb_box = st.get("kelly_break") or {}
        self.kb_lookback_var = ctk.StringVar(value=str(kb_box.get("lookback", 20)))
        self.kb_atr_period_var = ctk.StringVar(value=str(kb_box.get("atr_period", 14)))
        self.kb_stop_atr_var = ctk.StringVar(value=str(kb_box.get("stop_atr", 1.2)))
        self.kb_take_profit_var = ctk.BooleanVar(value=bool(kb_box.get("take_profit", True)))
        self.kb_reward_risk_var = ctk.StringVar(value=str(kb_box.get("reward_risk", 2.5)))
        self.kb_min_atr_ratio_var = ctk.StringVar(value=str(kb_box.get("min_atr_ratio", 0.0)))
        ls_box = st.get("liq_stop") or {}
        self.ls_zone_lookback_var = ctk.StringVar(value=str(ls_box.get("zone_lookback", 20)))
        self.ls_zone_tol_var = ctk.StringVar(value=str(ls_box.get("zone_tol", 0.005)))
        self.ls_atr_period_var = ctk.StringVar(value=str(ls_box.get("atr_period", 20)))
        self.ls_adx_period_var = ctk.StringVar(value=str(ls_box.get("adx_period", 14)))
        self.ls_adx_min_var = ctk.StringVar(value=str(ls_box.get("adx_min", 20.0)))
        self.ls_break_window_var = ctk.StringVar(value=str(ls_box.get("break_window", 3)))
        self.ls_entry_offset_var = ctk.StringVar(value=str(ls_box.get("entry_offset", 0.5)))
        self.ls_stop_atr_var = ctk.StringVar(value=str(ls_box.get("stop_atr", 1.0)))
        self.ls_take_profit_var = ctk.BooleanVar(value=bool(ls_box.get("take_profit", True)))
        self.ls_reward_risk_var = ctk.StringVar(value=str(ls_box.get("reward_risk", 2.0)))
        self.ls_vol_mult_var = ctk.StringVar(value=str(ls_box.get("vol_mult", 0.0)))
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
        # —— 数据管理页：本地K线库的建立与管理 ——
        now = datetime.now().replace(second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        self.dm_symbol_var = ctk.StringVar(value=str(default_sym))
        self.dm_period_var = ctk.StringVar(value=tf_label)
        self.dm_period_custom_var = ctk.StringVar(value="")
        self.dm_start_date_var = ctk.StringVar(value=week_ago.strftime("%Y-%m-%d"))
        self.dm_start_time_var = ctk.StringVar(value=week_ago.strftime("%H:%M"))
        self.dm_end_date_var = ctk.StringVar(value=now.strftime("%Y-%m-%d"))
        self.dm_end_time_var = ctk.StringVar(value=now.strftime("%H:%M"))
        # 库存检查/删除弹窗的默认数据源，跟配置页当前行情源一致。
        self.dm_source_var = ctk.StringVar(value=quote_label)
        # 成交近似：下一根开盘成交与滑点跳数，只影响回测。配置页和历史回测页共用这一组。
        self.bt_fill_next_open_var = ctk.BooleanVar(value=bool(bt.get("fill_on_next_open", True)))
        self.bt_slippage_var = ctk.StringVar(value=str(bt.get("slippage_ticks", 1)))
        # 策略评估门槛。配置页改这里，回测/优化/交叉测试共用。
        ev = self.cfg.get("evaluation") if isinstance(self.cfg.get("evaluation"), dict) else {}
        self.ev_enabled_var = ctk.BooleanVar(value=bool(ev.get("enabled", True)))
        self.ev_stress_var = ctk.BooleanVar(value=bool(ev.get("stress_on_backtest", True)))
        self.ev_gate_var = ctk.BooleanVar(value=bool(ev.get("gate_on_optimize", True)))
        self.ev_pf_var = ctk.StringVar(value=str(ev.get("profit_factor_min", 1.3)))
        self.ev_r_var = ctk.StringVar(value=str(ev.get("r_expect_min", 0.12)))
        self.ev_calmar_var = ctk.StringVar(value=str(ev.get("calmar_min", 1.0)))
        self.ev_dd_var = ctk.StringVar(value=str(ev.get("max_drawdown_pct", 0.15)))
        self.ev_trades_var = ctk.StringVar(value=str(ev.get("trades_min", 150)))
        self.ev_observe_var = ctk.StringVar(value=str(ev.get("trades_observe", 100)))
        self.ev_streak_var = ctk.StringVar(value=str(ev.get("max_consecutive_losses", 8)))
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
        kb_box = st.get("kelly_break") or {}
        self.kb_lookback_var.set(str(kb_box.get("lookback", 20)))
        self.kb_atr_period_var.set(str(kb_box.get("atr_period", 14)))
        self.kb_stop_atr_var.set(str(kb_box.get("stop_atr", 1.2)))
        self.kb_take_profit_var.set(bool(kb_box.get("take_profit", True)))
        self.kb_reward_risk_var.set(str(kb_box.get("reward_risk", 2.5)))
        self.kb_min_atr_ratio_var.set(str(kb_box.get("min_atr_ratio", 0.0)))
        ls_box = st.get("liq_stop") or {}
        self.ls_zone_lookback_var.set(str(ls_box.get("zone_lookback", 20)))
        self.ls_zone_tol_var.set(str(ls_box.get("zone_tol", 0.005)))
        self.ls_atr_period_var.set(str(ls_box.get("atr_period", 20)))
        self.ls_adx_period_var.set(str(ls_box.get("adx_period", 14)))
        self.ls_adx_min_var.set(str(ls_box.get("adx_min", 20.0)))
        self.ls_break_window_var.set(str(ls_box.get("break_window", 3)))
        self.ls_entry_offset_var.set(str(ls_box.get("entry_offset", 0.5)))
        self.ls_stop_atr_var.set(str(ls_box.get("stop_atr", 1.0)))
        self.ls_take_profit_var.set(bool(ls_box.get("take_profit", True)))
        self.ls_reward_risk_var.set(str(ls_box.get("reward_risk", 2.0)))
        self.ls_vol_mult_var.set(str(ls_box.get("vol_mult", 0.0)))
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
