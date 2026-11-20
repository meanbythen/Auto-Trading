"""界面到配置的映射：把表单读回 cfg、收动态减仓和凯利参数。"""

from __future__ import annotations

from typing import Any

from loguru import logger
from src.strategy.factory import REMOVED_ENGINES
from src.strategy.rule_dsl import rule_engine_stem
from src.ui.app_common import _clock_text
from src.utils import load_config
from src.utils import save_secrets
import copy
import os


class ConfigCollectMixin:
    """界面到配置的映射：把表单读回 cfg、收动态减仓和凯利参数。"""

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
        # 自定义规则引擎（rule_dsl:文件名）同时把文件指针写回去，兼容只读 rule_dsl.file 的旧路径。
        rule_stem = rule_engine_stem(st["engine"])
        if rule_stem:
            rule_box = st.get("rule_dsl")
            if not isinstance(rule_box, dict):
                rule_box = {}
                st["rule_dsl"] = rule_box
            rule_box["file"] = f"rules/{rule_stem}.yaml"
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
        # 已删引擎留下的整节配置，保存时统一清掉；名单只认策略工厂那一份。
        for removed in REMOVED_ENGINES:
            st.pop(removed, None)

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
        boll_box["flatten_at_close"] = bool(self.flatten_at_close_var.get())
        kb_box = st.setdefault("kelly_break", {})
        try:
            kb_box["lookback"] = max(5, int(self.kb_lookback_var.get()))
        except ValueError:
            kb_box["lookback"] = 20
        try:
            kb_box["atr_period"] = max(2, int(self.kb_atr_period_var.get()))
        except ValueError:
            kb_box["atr_period"] = 14
        try:
            kb_box["stop_atr"] = max(0.1, float(self.kb_stop_atr_var.get()))
        except ValueError:
            kb_box["stop_atr"] = 1.2
        kb_box["take_profit"] = bool(self.kb_take_profit_var.get())
        try:
            kb_box["reward_risk"] = max(0.5, float(self.kb_reward_risk_var.get()))
        except ValueError:
            kb_box["reward_risk"] = 2.5
        try:
            kb_box["min_atr_ratio"] = max(0.0, min(1.0, float(self.kb_min_atr_ratio_var.get())))
        except ValueError:
            kb_box["min_atr_ratio"] = 0.0
        ls_box = st.setdefault("liq_stop", {})
        try:
            ls_box["zone_lookback"] = max(10, int(self.ls_zone_lookback_var.get()))
        except ValueError:
            ls_box["zone_lookback"] = 20
        try:
            ls_box["zone_tol"] = max(0.0005, min(0.2, float(self.ls_zone_tol_var.get())))
        except ValueError:
            ls_box["zone_tol"] = 0.005
        try:
            ls_box["atr_period"] = max(2, int(self.ls_atr_period_var.get()))
        except ValueError:
            ls_box["atr_period"] = 20
        try:
            ls_box["adx_period"] = max(2, int(self.ls_adx_period_var.get()))
        except ValueError:
            ls_box["adx_period"] = 14
        try:
            ls_box["adx_min"] = max(0.0, float(self.ls_adx_min_var.get()))
        except ValueError:
            ls_box["adx_min"] = 20.0
        try:
            ls_box["break_window"] = max(1, int(self.ls_break_window_var.get()))
        except ValueError:
            ls_box["break_window"] = 3
        try:
            ls_box["entry_offset"] = max(0.0, float(self.ls_entry_offset_var.get()))
        except ValueError:
            ls_box["entry_offset"] = 0.5
        try:
            ls_box["stop_atr"] = max(0.1, float(self.ls_stop_atr_var.get()))
        except ValueError:
            ls_box["stop_atr"] = 1.0
        ls_box["take_profit"] = bool(self.ls_take_profit_var.get())
        try:
            ls_box["reward_risk"] = max(0.5, float(self.ls_reward_risk_var.get()))
        except ValueError:
            ls_box["reward_risk"] = 2.0
        try:
            ls_box["vol_mult"] = max(0.0, float(self.ls_vol_mult_var.get()))
        except ValueError:
            ls_box["vol_mult"] = 0.0
        sar_box = st.setdefault("sar_trend", {})
        try:
            sar_box["ma_filter"] = max(2, int(self.sar_ma_filter_var.get()))
        except ValueError:
            sar_box["ma_filter"] = 20
        try:
            sar_box["af_step"] = max(0.005, float(self.sar_af_step_var.get()))
        except ValueError:
            sar_box["af_step"] = 0.02
        sar_box["flatten_at_close"] = bool(self.flatten_at_close_var.get())
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
        # 多因子收盘清仓跟左边总开关，不再单独留一份写死的 true。
        multi_box["flatten_at_close"] = bool(self.flatten_at_close_var.get())
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
        from src.strategy.dual_cycle import parse_fractal_mode, parse_htf

        dd_box = st.setdefault("dual_donchian", {})
        dd_box["trend_timeframe"] = parse_htf(str(self.dd_htf_var.get() or "日线"), "D")
        try:
            dd_box["entry_bars"] = max(5, int(self.dd_entry_var.get()))
        except ValueError:
            dd_box["entry_bars"] = 55
        try:
            dd_box["exit_bars"] = max(2, int(self.dd_exit_var.get()))
        except ValueError:
            dd_box["exit_bars"] = 20
        if dd_box["exit_bars"] >= dd_box["entry_bars"]:
            dd_box["exit_bars"] = max(2, dd_box["entry_bars"] // 2)
        dd_box["htf_donchian"] = dd_box["entry_bars"]
        try:
            dd_box["adx_min"] = max(0.0, float(self.dd_adx_min_var.get()))
        except ValueError:
            dd_box["adx_min"] = 22.0
        try:
            dd_box["atr_mult"] = max(0.1, float(self.dd_atr_mult_var.get()))
        except ValueError:
            dd_box["atr_mult"] = 2.5
        dd_box.setdefault("flatten_at_close", False)
        df_box = st.setdefault("dual_fractal", {})
        df_box["trend_timeframe"] = parse_htf(str(self.df_htf_var.get() or "日线"), "D")
        df_box["trend_mode"] = parse_fractal_mode(str(self.df_mode_var.get() or "指数均线"))
        try:
            df_box["trend_ema"] = max(2, int(self.df_ema_var.get()))
        except ValueError:
            df_box["trend_ema"] = 20
        try:
            df_box["adx_min"] = max(0.0, float(self.df_adx_min_var.get()))
        except ValueError:
            df_box["adx_min"] = 22.0
        try:
            df_box["atr_mult"] = max(0.1, float(self.df_atr_mult_var.get()))
        except ValueError:
            df_box["atr_mult"] = 2.8
        df_box.setdefault("flatten_at_close", False)
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

        # 回测成交模拟：开关在历史回测页。保存交易配置时一并写盘，下次打开还在。
        bt = self.cfg.setdefault("backtest", {})
        if hasattr(self, "bt_fill_next_open_var"):
            bt["fill_on_next_open"] = bool(self.bt_fill_next_open_var.get())
        if hasattr(self, "bt_mirror_gates_var"):
            bt["mirror_live_gates"] = bool(self.bt_mirror_gates_var.get())
        if hasattr(self, "bt_slippage_var"):
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
        bt["freeze_final_oos"] = bool(self.opt_freeze_var.get())
        try:
            bt["freeze_final_days"] = min(730, max(30, int(float(self.opt_freeze_days_var.get() or 180))))
        except (TypeError, ValueError):
            bt["freeze_final_days"] = 180
        try:
            bt["max_trials"] = max(5, int(float(self.opt_trials_var.get() or 80)))
        except (TypeError, ValueError):
            bt["max_trials"] = 80

        # 策略评估：界面在历史回测页。保存交易配置时写盘；回测/优化经 _clone_cfg 也会带上当前值。
        ev = self.cfg.setdefault("evaluation", {})
        if not isinstance(ev, dict):
            ev = {}
            self.cfg["evaluation"] = ev

        def _ev_num(var: object, fallback: float, low: float, high: float) -> float:
            try:
                raw = float(getattr(var, "get")() or fallback)
            except (TypeError, ValueError):
                return fallback
            return min(high, max(low, raw))

        ev["enabled"] = bool(self.ev_enabled_var.get())
        ev["stress_on_backtest"] = bool(self.ev_stress_var.get())
        ev["gate_on_optimize"] = bool(self.ev_gate_var.get())
        ev["profit_factor_min"] = _ev_num(self.ev_pf_var, 1.3, 0.0, 5.0)
        ev["r_expect_min"] = _ev_num(self.ev_r_var, 0.12, -1.0, 2.0)
        ev["calmar_min"] = _ev_num(self.ev_calmar_var, 1.0, 0.0, 10.0)
        ev["max_drawdown_pct"] = _ev_num(self.ev_dd_var, 0.15, 0.0, 1.0)
        ev["trades_min"] = int(_ev_num(self.ev_trades_var, 150, 0, 10000))
        ev["trades_observe"] = int(_ev_num(self.ev_observe_var, 100, 0, 10000))
        ev["max_consecutive_losses"] = int(_ev_num(self.ev_streak_var, 8, 0, 100))

        trade = self.cfg.setdefault("trade", {})
        chosen_exec = str(self.exec_var.get() or "sim").strip() or "sim"
        trade["executor"] = chosen_exec
        from src.trader.go_live import (
            apply_stage_to_cfg,
            load_state as load_go_live,
            pair_stage_with_kind,
            save_state as save_go_live,
            stage_hint,
            stage_label,
        )
        from src.trader.ctp_logic import ctp_kind_label, normalize_ctp_kind

        # 「账号」页「CTP操作账」和交易台一样：只换仿真/真实接口，开平仓规则不动。
        kind = normalize_ctp_kind(self.ctp_kind_var.get())
        stage = pair_stage_with_kind(self.go_live_var.get(), kind)
        apply_stage_to_cfg(self.cfg, stage)
        trade["executor"] = chosen_exec
        self.go_live_var.set(stage_label(stage))
        self.go_live_hint_var.set(stage_hint(stage))
        try:
            state = load_go_live(self.root_dir)
            state["stage"] = stage
            save_go_live(self.root_dir, state)
        except Exception:  # noqa: BLE001
            pass

        ctp_box = trade.setdefault("ctp", {})
        if not isinstance(ctp_box, dict):
            ctp_box = {}
            trade["ctp"] = ctp_box
        # 单笔最大手数只在本机拦单，不是柜台上限；夹到 1~1000，非法值退回默认 20。
        # 先取值再改 ctp_kind：改种类会重灌档案控件，别把刚输入的数冲掉。
        try:
            max_order_volume = max(1, min(1000, int(float(self.ctp_max_volume_var.get()))))
        except (TypeError, ValueError):
            max_order_volume = 20
        ctp_box["max_order_volume"] = max_order_volume
        self.ctp_kind_var.set(ctp_kind_label(ctp_box.get("account_kind") or kind))
        # 券商档案：页面上改的是当前选中的那一份。旧写法不再读写。
        from src.trader.ctp_profile import profile_name_for_kind

        profile_name = str(self.ctp_profile_var.get() or "").strip()
        if profile_name:
            profiles = ctp_box.get("ctp_profiles")
            if not isinstance(profiles, dict):
                profiles = {}
            ctp_box["ctp_profiles"] = profiles
            current = profiles.get(profile_name)
            item = dict(current) if isinstance(current, dict) else {}
            # 档案自己的仿真/真实种类不要被当前操作账盖掉，避免把实盘前置写进仿真档。
            item.setdefault(
                "kind",
                (current or {}).get("kind") if isinstance(current, dict) else ctp_box["account_kind"],
            )
            item.setdefault("label", profile_name)
            from src.trader.ctp_fronts import dump_front_field, split_fronts

            item["td_front"] = dump_front_field(split_fronts(self.ctp_profile_td_var.get()))
            item["md_front"] = dump_front_field(split_fronts(self.ctp_profile_md_var.get()))
            item.pop("td_fronts", None)
            item.pop("md_fronts", None)
            item["broker_id"] = str(self.ctp_profile_broker_var.get() or "").strip()
            item["user_id"] = str(self.ctp_profile_user_var.get() or "").strip()
            item["password_env"] = str(self.ctp_profile_pwd_env_var.get() or "").strip()
            item["auth_code_env"] = str(self.ctp_profile_auth_env_var.get() or "").strip()
            # 档案里若曾单独写过上限，界面上改的数要真的生效，不能只写公共默认。
            item["max_order_volume"] = max_order_volume
            profiles[profile_name] = item
            from src.trader.ctp_profile import set_sim_profile_user_id

            # 账号页的仿真号码始终写仿真档案，避免真实资金号盖掉 28077。
            sim_user = str(self.ctp_sim_user_var.get() or "").strip()
            sim_name = set_sim_profile_user_id(ctp_box, sim_user)
            if sim_name and profile_name == sim_name:
                item["user_id"] = sim_user
                self.ctp_profile_user_var.set(sim_user)
            matched = profile_name_for_kind(ctp_box, ctp_box.get("account_kind"))
            ctp_box["ctp_profile"] = matched or profile_name
            if matched and matched != profile_name:
                self._ctp_profile_syncing = True
                self.ctp_profile_var.set(matched)
                self._ctp_profile_syncing = False
                self._fill_ctp_profile_vars()
        else:
            from src.trader.ctp_profile import set_sim_profile_user_id

            set_sim_profile_user_id(ctp_box, str(self.ctp_sim_user_var.get() or "").strip())
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
        # 保证金和手续费从总表补；关注名单以界面上这份为准，这里不去读表覆盖。
        # 在收集时重读总表会把用户刚改的名单和回测页选中的合约一起冲掉。
        table_rel = (
            str(self.margin_table_path_var.get() or "").strip() or "config/品种与合约.xlsx"
        )
        risk_pre["universe_table"] = table_rel
        risk_pre["margin_table"] = table_rel
        try:
            from src.risk.universe_table import apply_universe_table_to_cfg

            apply_universe_table_to_cfg(
                self.cfg, path=self._margin_table_abs_path(), create_if_missing=True
            )
            self.cfg.setdefault("strategy", {})["symbols"] = list(self._watch_symbols)
            if self._watch_symbols:
                self.cfg.setdefault("trade", {})["symbol"] = self._watch_symbols[0]
        except Exception as exc:  # noqa: BLE001
            # 读表失败不影响收集，保证金沿用配置里的值。
            logger.warning("读取品种与合约表失败: {}", exc)
        # 「启用限制」只属于回测和优化这一次。留在这里的话，下次保存会把回测开关写进实盘配置。
        risk_pre.pop("apply_limits", None)

        sch = self.cfg.setdefault("schedule", {})
        sch["scan_interval_sec"] = self._scan_interval_value()
        from src.strategy.session_scope import scope_code

        sch["session_scope"] = scope_code(self.session_scope_var.get())

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

        # 密码：写入本地密钥 + 当前进程环境。某一格留空表示不改已经存过的值。
        tq_pwd = self.tq_password_var.get()
        sim_pwd = str(self.ctp_sim_password_var.get() or "")
        auth_code = str(self.ctp_auth_code_var.get() or "")
        extra: dict[str, Any] = {}
        if tq_pwd:
            extra["tq_password"] = tq_pwd
        if sim_pwd:
            extra["ctp_password"] = sim_pwd
            os.environ["CTP_PASSWORD"] = sim_pwd
        if auth_code:
            extra["ctp_auth_code"] = auth_code
            os.environ["CTP_AUTH_CODE"] = auth_code
        if extra:
            save_secrets(extra=extra)

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
