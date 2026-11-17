"""Historical-backtest configuration and shared-state helpers."""

from __future__ import annotations

from loguru import logger
from src.data.periods import period_label
from src.ui.app_common import ENGINE_LABELS
from src.ui.app_common import _ui_engine
from src.utils import save_config
from src.utils.trading_day import futures_trading_day


class BacktestConfigMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _resolve_date_range(self, preset: str, start_raw: str, end_raw: str) -> tuple[str | None, str | None]:
        """
        把界面上的「时间段」选项换成具体起止日期。
        返回 (start, end)，None 表示不限制（用公开源能给的全部）。
        """
        from datetime import timedelta

        preset = (preset or "全部可用").strip()
        # 最近N天以期货交易日为准：夜盘 21:00 之后已经算今天。
        today = futures_trading_day()
        # 「最近N天」以今天为准，不看下面两个日期框
        if preset == "最近3天":
            return (today - timedelta(days=3)).isoformat(), today.isoformat()
        if preset == "最近7天":
            return (today - timedelta(days=7)).isoformat(), today.isoformat()
        if preset == "最近14天":
            return (today - timedelta(days=14)).isoformat(), today.isoformat()
        # 「全部可用」只有两个日期都空着才不截。
        # 只要填了开始或结束日期，就按填写的截，避免改了日期却仍跑完全部K线。
        s = self._normalize_date(start_raw)
        e = self._normalize_date(end_raw)
        if preset == "全部可用" and not s and not e:
            return None, None
        return s, e


    @staticmethod
    def _initial_start_date(saved: object) -> str:
        """
        开始日期的初始值。

        配置里已经写了日期就用那个。空着时填当年1月1日，例如今年是 2026-01-01。
        这样每次打开回测，不用再手填从哪一天开始。
        """
        text = str(saved or "").strip()
        if text:
            return text
        from datetime import date

        return f"{date.today().year}-01-01"


    @staticmethod
    def _normalize_date(raw: str | None) -> str | None:
        """把 2026-1-1、2026/1/1 收成 2026-01-01。空字符串表示不限制。"""
        text = str(raw or "").strip().replace("/", "-").replace(".", "-")
        if not text:
            return None
        try:
            import pandas as pd

            return pd.Timestamp(text).strftime("%Y-%m-%d")
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"日期「{raw}」看不懂，请写成 2026-10-01") from e


    def _sync_bt_to_opt(self) -> None:
        """把历史回测页的条件抄到策略优化页，保证两边看的是同一段行情。"""
        self.opt_engine_var.set(self.bt_engine_var.get())
        symbol = self.bt_symbol_var.get().strip()
        # 优化页品种是下拉，先保证名单和配置一致，再改成回测页当前这一项。
        self._sync_symbol_choice("opt_symbol_menu", self.opt_symbol_var, prefer=symbol, force=True)
        self.opt_tf_var.set(self.bt_tf_var.get())
        self.opt_tf_custom_var.set(self.bt_tf_custom_var.get())
        self.opt_trail_var.set(self.bt_trail_var.get())
        self.opt_trail_custom_var.set(self.bt_trail_custom_var.get())
        self.opt_range_var.set(self.bt_range_var.get())
        self.opt_start_var.set(self.bt_start_var.get())
        self.opt_end_var.set(self.bt_end_var.get())
        from src.data.quote_check import is_ths_source

        if is_ths_source(self.bt_source_var.get()):
            self.opt_source_var.set(self.source_var.get())
        else:
            self.opt_source_var.set(self.bt_source_var.get())
        # 下拉框用代码改策略名时，不会自己刷新历史列表，这里补一次。
        if hasattr(self, "opt_hist_menu"):
            self._refresh_opt_history()
        self._append_log("已把历史回测条件同步到「策略优化」页。")
        from tkinter import messagebox

        messagebox.showinfo(
            "已同步",
            "策略、品种、周期、行情来源和时间段已复制到「策略优化」页。\n"
            "收盘清仓以及开仓限制，两边本来就是同一组。",
        )


    def _copy_risk_to_backtest(self, full: bool = False) -> None:
        """
        把配置页上会影响回测的内容抄到回测页。不改磁盘。

        full=True 是「按配置页重新填入」：连策略、品种、周期和行情来源一起抄，优化页同一组条件也跟着变。
        平时重新加载只抄资金数字和三个开关，不改回测页正在看的策略和品种。
        「启用限制」不抄。配置页没有这个开关，实盘一直按这些数字算手数。
        清仓时刻、ETH 分钟、实体 ATR 倍数、趋势进场根数、SuperTrend 倍数等没有单独的回测框，开始回测时直接用配置页当时的值。
        """
        from src.ui.backtest_fill import first_symbol

        if full:
            engine = _ui_engine(self.engine_var.get())
            # 用配置页下拉框里当前选中的合约，不是列表里随便第一个旧名字。
            symbol = self._config_symbol_now() or first_symbol(self.symbols_var.get())
            tf_code = self._period_code(self.tf_var, self.tf_custom_var)
            trail_code = self._period_code(self.trail_var, self.trail_custom_var)
            self.bt_engine_var.set(engine)
            self.opt_engine_var.set(engine)
            self._sync_bt_symbol_menu(prefer=symbol, force=True)
            symbol = self.bt_symbol_var.get().strip()
            if symbol and symbol != "（还没有）":
                self._sync_symbol_choice("opt_symbol_menu", self.opt_symbol_var, prefer=symbol, force=True)
                self._sync_symbol_choice("rv_symbol_menu", self.rv_symbol_var, prefer=symbol, force=True)
            self._set_period_vars(tf_code, self.bt_tf_var, self.bt_tf_custom_var)
            self._set_period_vars(tf_code, self.opt_tf_var, self.opt_tf_custom_var)
            self._set_period_vars(trail_code, self.bt_trail_var, self.bt_trail_custom_var)
            self._set_period_vars(trail_code, self.opt_trail_var, self.opt_trail_custom_var)
            self.bt_source_var.set(self.source_var.get())
            self.opt_source_var.set(self.source_var.get())
        else:
            symbol = self.bt_symbol_var.get().strip()
            if symbol == "（还没有）":
                symbol = ""
            symbol = symbol or self._config_symbol_now() or first_symbol(self.symbols_var.get())
            engine = self.bt_engine_var.get()
            tf_code = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
            trail_code = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)

        # 收盘清仓要一起抄。以前只抄数字，回测仍用页面上旧的关。
        self.bt_flatten_var.set(bool(self.flatten_at_close_var.get()))

        self.bt_sizing_var.set(self.sizing_mode_var.get())
        self.bt_capital_var.set(self.capital_var.get())
        if hasattr(self, "pk_capital_var"):
            self.pk_capital_var.set(self.capital_var.get())
        self.bt_risk_pct_var.set(self.risk_pct_var.get())
        self.bt_margin_usage_pct_var.set(self.margin_usage_pct_var.get())
        self.bt_volume_var.set(self.volume_var.get())
        self.bt_max_loss_var.set(self.max_loss_var.get())
        self.bt_interval_var.set(self.min_interval_var.get())
        self.bt_max_orders_var.set(self.max_orders_var.get())
        risk = self.cfg.get("risk", {}) if isinstance(self.cfg, dict) else {}
        self.bt_min_vol_var.set(str(risk.get("min_volume", self.bt_min_vol_var.get() or 1)))
        self._sync_bt_margin_from_table(symbol)
        if not full:
            return
        if hasattr(self, "opt_hist_menu"):
            self._refresh_opt_history()
        def on_off(flag: object) -> str:
            return "开" if flag else "关"
        self._append_log(
            "已按配置页填入历史回测："
            f"策略 {ENGINE_LABELS.get(engine, engine)}，品种 {symbol}，"
            f"周期 {period_label(tf_code)}/{period_label(trail_code)}，"
            f"行情 {self.bt_source_var.get()}，"
            f"收盘清仓{on_off(self.bt_flatten_var.get())}"
        )


    def _write_backtest_to_config_page(self) -> None:
        """
        把历史回测页测完的参数完整写进配置页并保存。

        包括：策略、品种、信号/止损周期、行情来源、收盘清仓，以及开仓手数等限制。
        不写：账户权益（仍是实盘余额）、回测日期、「启用限制」开关（配置页没有这个开关）。
        """
        from tkinter import messagebox

        from src.data.quote_check import normalize_source

        symbol = (self.bt_symbol_var.get() or "").strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有品种", "请先在历史回测页选一个合约，再写入配置页。")
            return
        engine = _ui_engine(self.bt_engine_var.get())
        tf_code = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        trail_code = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)
        from src.data.quote_check import is_ths_source

        source_label = (self.bt_source_var.get() or "").strip()
        live_keeps_source = is_ths_source(source_label)
        flatten_on = bool(self.bt_flatten_var.get())

        try:
            sizing = self.bt_sizing_var.get() or "risk_pct"
            float(self.bt_capital_var.get())
            risk_pct = float(self.bt_risk_pct_var.get())
            margin_usage = max(0.0, float(self.bt_margin_usage_pct_var.get() or 0))
            volume = int(float(self.bt_volume_var.get()))
            min_vol = max(1, int(float(self.bt_min_vol_var.get())))
            max_loss = float(self.bt_max_loss_var.get())
            interval = max(0, int(float(self.bt_interval_var.get())))
            max_orders = max(1, int(float(self.bt_max_orders_var.get())))
            margin = max(0.0, float(self.bt_margin_var.get() or 0))
        except ValueError:
            messagebox.showerror("数字不对", "开仓限制里的权益、风险和手数要填数字")
            return

        def on_off(flag: object) -> str:
            return "开" if flag else "关"
        if not messagebox.askyesno(
            "写入配置页",
            "下面内容会写到配置页，并保存到配置文件。实盘以后按这套策略和手数跑。\n"
            "账户权益仍是当前余额，不会改成历史回测里手填的数。回测日期不写入。\n\n"
            f"策略  {ENGINE_LABELS.get(engine, engine)}\n"
            f"品种  {symbol}\n"
            f"信号周期  {period_label(tf_code)}    止损周期  {period_label(trail_code)}\n"
            f"行情来源  {'不同花顺图写入实盘（配置页仍用现在的那一家）' if live_keeps_source else (source_label or '（空）')}\n"
            f"收盘清仓  {on_off(flatten_on)}\n\n"
            f"仓位模式  {sizing}\n"
            f"每笔风险  {risk_pct}%\n"
            f"仓位上限  {margin_usage:g}%\n"
            f"固定手数  {volume}\n"
            f"最少手数  {min_vol}\n"
            f"日最大亏损  {max_loss:.0f} 元\n"
            f"开仓间隔  {interval} 秒\n"
            f"每天最多开仓  {max_orders} 次\n"
            f"兜底每手保证金  {margin:.0f} 元（表里有比例时按价格现算）\n\n"
            "确定写入？",
        ):
            return

        # —— 策略 / 周期 / 来源 / 开关 ——
        self.engine_var.set(engine)
        self._set_period_vars(tf_code, self.tf_var, self.tf_custom_var)
        self._set_period_vars(trail_code, self.trail_var, self.trail_custom_var)
        if source_label and not live_keeps_source:
            self.source_var.set(source_label)
        self.flatten_at_close_var.set(flatten_on)

        # 关注名单：没有这个合约就加上，并选为当前合约（排到最前）
        codes = list(self._watch_symbols)
        if not any(c.lower() == symbol.lower() for c in codes):
            codes.append(symbol)
        self._set_watch_codes(codes, select=symbol, save=False)

        # —— 开仓限制（不含权益）——
        self.sizing_mode_var.set(sizing)
        self.risk_pct_var.set(str(risk_pct))
        self.margin_usage_pct_var.set(str(margin_usage))
        self.volume_var.set(str(volume))
        self.max_loss_var.set(str(max_loss))
        self.min_interval_var.set(str(interval))
        self.max_orders_var.set(str(max_orders))
        self.margin_var.set(str(margin))
        self.cfg.setdefault("risk", {})["min_volume"] = min_vol
        self.cfg.setdefault("risk", {})["margin_per_lot"] = margin
        self.cfg.setdefault("risk", {})["margin_usage_pct"] = margin_usage
        self.cfg.setdefault("risk", {}).pop("max_position", None)

        cfg = self._merge_disk_gui_coords(self._apply_ui_to_cfg())
        cfg.setdefault("risk", {})["min_volume"] = min_vol
        cfg.setdefault("risk", {})["margin_per_lot"] = margin
        cfg.setdefault("risk", {})["margin_usage_pct"] = margin_usage
        cfg.setdefault("risk", {}).pop("max_position", None)
        # 回测页的新浪/东财/天勤可以写入实盘。同花顺图不能写进 data.source，
        # 否则下次启动扫描会去抢鼠标，整轮拉K失败，一单也开不出。
        if source_label and not live_keeps_source:
            cfg.setdefault("data", {})["source"] = normalize_source(source_label)
        path = save_config(cfg)
        self.cfg = cfg
        self._status = "已保存"
        self._append_log(
            "历史回测本页参数已写入配置页并保存："
            f"策略 {ENGINE_LABELS.get(engine, engine)}，品种 {symbol}，"
            f"周期 {period_label(tf_code)}/{period_label(trail_code)}，"
            f"来源 {'仍用配置页当前这家（同花顺图不写实盘）' if live_keeps_source else (source_label or '-')}，"
            f"清仓{on_off(flatten_on)} | {path}"
        )
        self._refresh_status_cards()
        self.tabs.set("配置")
        messagebox.showinfo(
            "已写入",
            "配置页已换成历史回测本页的策略、品种、周期、开关和开仓限制，并保存到配置文件。",
        )


    def _write_limits_to_config_page(self) -> None:
        """旧名字保留：别处若还调用，转到完整写入。"""
        self._write_backtest_to_config_page()


    def _apply_backtest_switches(self, cfg: dict) -> None:
        """
        收盘清仓跟实盘配置页总开关一致，写进这一次回测副本。

        趋势跟随看的是自己那一节里的开关，这里不动。
        """
        st = cfg.setdefault("strategy", {})
        live_flat = bool(self.flatten_at_close_var.get())
        st["flatten_at_close"] = live_flat
        if hasattr(self, "bt_flatten_var"):
            self.bt_flatten_var.set(live_flat)
        # 成交近似：下一根开盘 + 滑点跳数，只写进这一次回测副本。
        bt = cfg.setdefault("backtest", {})
        # 真实账户口径只这一处写入副本，换月分段和单合约都读它，不再另设手数规则。
        if hasattr(self, "bt_rollover_chain_var"):
            bt["chain_capital"] = bool(self.bt_rollover_chain_var.get())
        if hasattr(self, "bt_fill_next_open_var"):
            bt["fill_on_next_open"] = bool(self.bt_fill_next_open_var.get())
        if hasattr(self, "bt_slippage_var"):
            try:
                bt["slippage_ticks"] = max(0.0, float(self.bt_slippage_var.get() or 0))
            except (TypeError, ValueError):
                logger.warning("滑点跳数不是数字，沿用配置里的 {}", bt.get("slippage_ticks", 1))


    def _table_margin_for_symbol(self, symbol: str | None = None) -> float:
        """从品种与合约表（已载入的 risk.contracts）读该品种每手保证金。没有或为 0 则返回 0。"""
        from src.risk.universe_table import product_key

        code = (symbol or self.bt_symbol_var.get() or "").strip()
        key = product_key(code)
        if not key:
            return 0.0
        risk = self.cfg.get("risk") or {}
        box = (risk.get("contracts") or {}).get(key)
        if not isinstance(box, dict):
            return 0.0
        try:
            return max(0.0, float(box.get("margin") or 0))
        except (TypeError, ValueError):
            return 0.0


    def _sync_bt_margin_from_table(self, symbol: str | None = None) -> None:
        """
        把回测页「每手保证金」改成品种表里的数。

        表里没有或为 0 时，改用配置页统一的每手保证金。
        """
        if not hasattr(self, "bt_margin_var"):
            return
        margin = self._table_margin_for_symbol(symbol)
        if margin <= 0:
            try:
                margin = max(0.0, float(self.margin_var.get() or 0))
            except (TypeError, ValueError):
                margin = 0.0
        text = str(int(margin)) if margin == int(margin) else str(margin)
        try:
            if str(self.bt_margin_var.get() or "").strip() == text:
                return
        except Exception:  # noqa: BLE001
            pass
        self.bt_margin_var.set(text)


    def _align_backtest_trading_params_with_live(self) -> None:
        """
        仓位、风险、保证金、收盘清仓与实盘配置对齐，并刷新回测页显示。

        策略引擎 / 周期 / 品种仍可由回测页单独选。账户权益不在这里覆盖：
        测试页可以填准备投入的资金，只有点「按配置页重新填入」才抄回当前余额。
        """
        if not hasattr(self, "bt_capital_var"):
            return
        risk = self.cfg.get("risk") or {}
        self.bt_sizing_var.set(self.sizing_mode_var.get() or "risk_pct")
        self.bt_risk_pct_var.set(self.risk_pct_var.get())
        self.bt_margin_usage_pct_var.set(self.margin_usage_pct_var.get())
        self.bt_volume_var.set(self.volume_var.get())
        self.bt_max_loss_var.set(self.max_loss_var.get())
        self.bt_interval_var.set(self.min_interval_var.get())
        self.bt_max_orders_var.set(self.max_orders_var.get())
        self.bt_min_vol_var.set(str(risk.get("min_volume", 1)))
        if hasattr(self, "bt_flatten_var") and hasattr(self, "flatten_at_close_var"):
            self.bt_flatten_var.set(bool(self.flatten_at_close_var.get()))
        self._sync_bt_margin_from_table()


    def _apply_backtest_limits(self, cfg: dict, *, symbol: str | None = None) -> None:
        """
        把实盘配置里的开仓限制写进这一次回测/优化副本（保证金优先品种表）。

        不写磁盘。回测页「启用限制」关掉时仍固定 1 手，便于只看信号。
        账户权益读本页填的数（准备投入的资金），其它手数规则仍跟配置页。
        symbol：算哪一个合约的表内保证金；空则用历史回测页当前品种。
        """
        self._align_backtest_trading_params_with_live()
        risk = cfg.setdefault("risk", {})
        trade = cfg.setdefault("trade", {})
        # 开关关掉：固定 1 手，后面的数字先留着，下次打开还在。
        risk["apply_limits"] = bool(self.bt_limits_on_var.get())
        if not risk["apply_limits"]:
            trade["volume"] = 1
            risk["sizing_mode"] = "fixed"
            try:
                risk["capital"] = float(self.bt_capital_var.get())
            except (TypeError, ValueError):
                pass
            return
        try:
            risk["sizing_mode"] = self.sizing_mode_var.get() or "risk_pct"
            risk["capital"] = float(self.bt_capital_var.get())
            risk["risk_per_trade_pct"] = float(self.risk_pct_var.get())
            risk["margin_usage_pct"] = max(0.0, float(self.margin_usage_pct_var.get() or 0))
            trade["volume"] = int(float(self.volume_var.get()))
            risk["min_volume"] = max(1, int(float(risk.get("min_volume", 1) or 1)))
            risk.pop("max_position", None)
            risk["max_daily_loss"] = float(self.max_loss_var.get())
            risk["min_order_interval_sec"] = max(0, int(float(self.min_interval_var.get())))
            risk["max_orders_per_day"] = max(1, int(float(self.max_orders_var.get())))
            generic_margin = max(0.0, float(self.margin_var.get() or 0))
        except ValueError as e:
            raise ValueError("回测页的账户权益、以及配置页的风险和手数要填数字") from e
        code = (symbol or self.bt_symbol_var.get() or "").strip()
        # 有保证金比例时按每笔开仓价现算，不要绑死一个金额把比例清掉。
        risk["margin_per_lot"] = generic_margin
        logger.info(
            "开仓限制已写入本次回测 | 测试权益={:g} 风险%={} 模式={} 品种={} 兜底保证金{:g}",
            risk["capital"],
            risk["risk_per_trade_pct"],
            risk["sizing_mode"],
            code or "?",
            generic_margin,
        )


    def _prepare_backtest_run(self) -> tuple[dict, str, str | None, str | None] | None:
        """把回测页的选项收成一份可跑的回测配置。参数不对时弹窗并返回 None。"""
        from tkinter import messagebox

        # 先对齐手数规则（账户权益不覆盖），再克隆成这一次回测副本。
        self._align_backtest_trading_params_with_live()
        cfg = self._clone_cfg()
        try:
            self._apply_backtest_limits(cfg)
        except ValueError as e:
            messagebox.showerror("数字不对", str(e))
            return None
        # 用回测页选择的策略/周期，只改这份副本
        st = cfg.setdefault("strategy", {})
        st["engine"] = self.bt_engine_var.get()
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.bt_source_var.get())
        st["timeframe"] = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)
        symbol = self.bt_symbol_var.get().strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约，例如 bu2612。")
            return None
        st["symbols"] = [symbol]
        # 收盘清仓跟实盘配置页总开关一致。
        self._apply_backtest_switches(cfg)
        try:
            preset = self.bt_range_var.get()
            # 日期解析和上面的下拉必须在同一层 try 里，填错才能弹出「日期不对」。
            start, end = self._resolve_date_range(
                preset, self.bt_start_var.get(), self.bt_end_var.get()
            )
        except ValueError as e:
            messagebox.showerror("日期不对", str(e))
            return None
        # 填了日期就按日期走，并把下拉改成「自定义」，免得下次又被「全部可用」忽略
        if preset == "全部可用" and (start or end):
            self.bt_range_var.set("自定义")
        if start:
            self.bt_start_var.set(start)
        if end:
            self.bt_end_var.set(end)
        return cfg, symbol, start, end
