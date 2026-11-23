"""Regularity-research tab construction, scanning, and candidate selection."""

from __future__ import annotations

from typing import Any

from loguru import logger
from src.ui import theme as T
import customtkinter as ctk
import threading


class ResearchTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_research_tab(self, tab: ctk.CTkFrame) -> None:
        """
        因子挖掘：在历史 K 线上扫因子和后面涨跌的关系，过关的写进候选池。

        这里不下单。勾选只表示你觉得这条逻辑说得通，不会改现在哪根 K 线开仓。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="因子挖掘", font=T.FONT_TITLE, text_color=T.TEXT).pack(
            anchor="w", padx=4, pady=(4, 2)
        )
        ctk.CTkLabel(
            left,
            text="程序在一份写清经济学逻辑的因子清单上，看它们和后面几根涨跌是否稳。两段时间都显著才进池，再看分段里大部分时间是否同向。报告开头会写更像趋势、突破、震荡还是波段，只是倾向，不会自动换策略，也不会下单。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        self._build_factor_data_card(left)

        box = self._card(left)
        from src.data.quote_check import SOURCE_LABELS

        self._option_row(box, "行情数据源", self.rs_source_var, list(SOURCE_LABELS), width=160)
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.rs_symbol_var.get() not in symbol_values:
            self.rs_symbol_var.set(symbol_values[0])
        self.rs_symbol_menu = self._option_row(box, "品种", self.rs_symbol_var, symbol_values, width=160)
        self._option_row(
            box,
            "扫哪些",
            self.rs_scope_var,
            ["只扫当前这个", "扫关注名单全部"],
            width=180,
        )
        self._timeframe_row(box, "K线周期", self.rs_tf_var, self.rs_tf_custom_var, width=140)
        self._option_row(
            box,
            "时间段",
            self.rs_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.rs_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.rs_end_var, hint="点日历；清空=到最新。只算到当天 20:59")
        self._pick_switch_row(box, "含当晚夜盘", self.rs_include_night_var)
        ctk.CTkCheckBox(
            box,
            text="多个品种时，至少两个合约都过关才进池",
            variable=self.rs_cross_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(6, 4))

        self.rs_start_btn = ctk.CTkButton(
            left,
            text="开始因子挖掘",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_research_scan,
        )
        self.rs_start_btn.pack(fill="x", padx=4, pady=(12, 6))
        ctk.CTkButton(
            left,
            text="保存勾选（仍不下单）",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_save_research_picks,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="过关条目（勾选=我认可这条逻辑，还要另写成开平规则才能当策略）",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))
        self.rs_pick_frame = ctk.CTkScrollableFrame(left, fg_color=T.BG_ELEVATED, height=220)
        self.rs_pick_frame.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        right = ctk.CTkFrame(tab, fg_color=T.BG_PANEL, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="因子研究报告", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.rs_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.rs_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._refresh_research_picks()
        # 按钮建好后再点一次灯，红灯时「开始因子挖掘」保持禁用。
        self._refresh_data_lights()


    def _build_factor_data_card(self, parent: ctk.CTkFrame) -> None:
        """因子挖掘基础数据：五盏灯、模式、OI 开关、检查与补齐按钮。"""
        from src.research.data_requirements import MODE_LABELS

        card = self._card(parent)
        ctk.CTkLabel(card, text="因子数据需求", font=T.FONT_BODY, text_color=T.TEXT).pack(
            anchor="w", pady=(0, 2)
        )
        self.rs_light_summary = ctk.CTkLabel(
            card,
            text="打开后自动检查当前品种和周期。红灯不能开始扫描。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        )
        self.rs_light_summary.pack(anchor="w", pady=(0, 6))

        lights = ctk.CTkFrame(card, fg_color="transparent")
        lights.pack(fill="x", pady=(0, 4))
        self.rs_light_labels: dict[str, Any] = {}
        for key, label in (
            ("kline", "K线"),
            ("oi", "持仓量"),
            ("cross", "跨品种"),
            ("fresh", "新鲜度"),
            ("lock", "样本外"),
        ):
            chip = ctk.CTkLabel(
                lights,
                text=f"{label} —",
                font=T.FONT_SMALL,
                fg_color="#374151",
                text_color="#E5E7EB",
                corner_radius=8,
                padx=8,
                pady=2,
            )
            chip.pack(side="left", padx=(0, 5))
            self.rs_light_labels[key] = chip

        self._option_row(card, "研究目标", self.rs_goal_var, ["因子筛选", "状态识别", "全流程"], width=150)
        self._option_row(card, "研究模式", self.rs_mode_var, list(MODE_LABELS.values()), width=150)
        self._pick_switch_row(card, "研究持仓量", self.rs_oi_var)
        self._pick_switch_row(card, "补齐只在非交易时段", self.rs_offhours_var)

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", pady=(6, 2))
        self.rs_check_btn = ctk.CTkButton(
            buttons,
            text="检查数据",
            width=96,
            height=30,
            fg_color=T.BG_PANEL,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_check_factor_data,
        )
        self.rs_check_btn.pack(side="left", padx=(0, 6))
        self.rs_backfill_btn = ctk.CTkButton(
            buttons,
            text="补齐当前周期K线",
            height=30,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_backfill_kline,
        )
        self.rs_backfill_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.rs_cancel_btn = ctk.CTkButton(
            buttons,
            text="取消",
            width=56,
            height=30,
            fg_color=T.BG_PANEL,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT_DIM,
            state="disabled",
            command=self._on_cancel_backfill,
        )
        self.rs_cancel_btn.pack(side="left")

        self.rs_data_progress = ctk.CTkLabel(
            card,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        )
        self.rs_data_progress.pack(anchor="w", pady=(4, 0))

        # 模式或 OI 一改，灯和「开始」按钮立刻跟着变，不用再点一次检查。
        for var in (self.rs_mode_var, self.rs_oi_var):
            var.trace_add("write", lambda *_args: self._refresh_data_lights())
        self._refresh_data_lights()


    def _prefill_cross_engines(self, engine_ids: set[str]) -> None:
        """把状态清单里的引擎勾进交叉测试。只勾选，不会自动开始，也不改策略参数。"""
        vars_map = getattr(self, "_pk_engine_vars", {}) or {}
        if not vars_map:
            return
        hit = 0
        for code, var in vars_map.items():
            chosen = str(code) in engine_ids
            var.set(chosen)
            hit += int(chosen)
        self._append_log(f"已预填交叉测试引擎 {hit} 个（只勾选，不会自动开始）")


    def _factor_period(self) -> str:
        return self._period_code(self.rs_tf_var, self.rs_tf_custom_var)


    def _factor_readiness(self) -> dict[str, Any]:
        """按当前品种、当前扫描周期和模式查本地库存。只读，不下载。"""
        from pathlib import Path

        from src.data.bar_store import BarStore
        from src.data.quote_check import normalize_source
        from src.research.data_requirements import evaluate_readiness

        data_cfg = self.cfg.get("data") or {}
        store = BarStore(Path(self.root_dir) / data_cfg.get("cache_dir", "data/klines") / "market.sqlite")
        try:
            start, end = self._resolve_date_range(
                self.rs_range_var.get(), self.rs_start_var.get(), self.rs_end_var.get()
            )
        except ValueError:
            start, end = None, None
        return evaluate_readiness(
            store,
            self._research_symbols(),
            [self._factor_period()],
            mode=self._rs_mode_by_label.get(self.rs_mode_var.get(), "standard"),
            require_oi=bool(self.rs_oi_var.get()),
            require_cross=bool(self.rs_cross_var.get()),
            start=start,
            end=end,
            source=normalize_source(self.rs_source_var.get()),
        )


    def _paint_factor_lights(self, report: dict[str, Any]) -> None:
        colors = {
            "green": ("#14532D", "#BBF7D0"),
            "yellow": ("#713F12", "#FDE68A"),
            "red": ("#7F1D1D", "#FECACA"),
            "off": ("#374151", "#D1D5DB"),
        }
        marks = {"green": "绿", "yellow": "黄", "red": "红", "off": "—"}
        lights = report.get("lights") or {}
        for key, chip in getattr(self, "rs_light_labels", {}).items():
            item = lights.get(key) or {}
            status = str(item.get("status") or "")
            fg, text_color = colors.get(status, ("#374151", "#E5E7EB"))
            chip.configure(
                text=f"{item.get('label', key)} {marks.get(status, '?')}",
                fg_color=fg,
                text_color=text_color,
            )
        overall = str(report.get("overall") or "")
        summary = str(report.get("summary") or "数据检查没有结果")
        missing = [str(item) for item in (report.get("missing") or []) if str(item).strip()]
        if missing:
            summary = summary + "\n" + "；".join(missing[:3])
        detail = " · ".join(
            f"{item.get('label', key)}：{item.get('detail', '')}"
            for key, item in lights.items()
            if item.get("detail")
        )
        text = summary
        if detail:
            text += "\n" + detail
        color = {"green": "#4ADE80", "yellow": "#FBBF24", "red": "#F87171"}.get(
            overall, T.TEXT_DIM
        )
        if hasattr(self, "rs_light_summary"):
            self.rs_light_summary.configure(text=text, text_color=color)
        self._set_factor_start_enabled(overall != "red")


    def _set_factor_start_enabled(self, enabled: bool) -> None:
        button = getattr(self, "rs_start_btn", None)
        if button is None:
            return
        button.configure(
            state="normal" if enabled else "disabled",
            fg_color=T.ACCENT if enabled else T.ACCENT_MUTED,
            hover_color=T.ACCENT_HOVER if enabled else T.ACCENT_MUTED,
            text_color=T.ACCENT_ON if enabled else T.ACCENT_TEXT_DISABLED,
        )


    def _refresh_data_lights(self) -> dict[str, Any]:
        """重新检查并点亮五盏灯；结果落盘，下次打开页面先看旧结果。"""
        if not hasattr(self, "rs_light_labels"):
            return {}
        try:
            report = self._factor_readiness()
        except Exception as exc:  # noqa: BLE001
            logger.warning("因子数据检查失败: {}", exc)
            if hasattr(self, "rs_light_summary"):
                self.rs_light_summary.configure(
                    text=f"数据检查失败：{exc}", text_color="#F87171"
                )
            self._set_factor_start_enabled(False)
            return {}
        self._paint_factor_lights(report)
        from src.research.data_requirements import save_status

        try:
            save_status(self.root_dir, report)
        except OSError as exc:
            logger.warning("因子数据状态没写上: {}", exc)
        return report


    def _on_check_factor_data(self) -> None:
        report = self._refresh_data_lights()
        if not report:
            return
        self._append_log(f"因子数据检查 {report.get('overall')} {report.get('summary', '')}")
        if hasattr(self, "rs_data_progress"):
            self.rs_data_progress.configure(text=str(report.get("summary") or ""))


    def _on_backfill_kline(self) -> None:
        """后台补齐当前周期的 K 线。默认交易时段直接等待，不抢实盘连接。"""
        from tkinter import messagebox

        if self._rs_backfill_busy:
            messagebox.showinfo("正在补齐", "上一轮 K 线补齐还没结束。")
            return
        symbols = self._research_symbols()
        if not symbols:
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约。")
            return
        period = self._factor_period()
        mode = self._rs_mode_by_label.get(self.rs_mode_var.get(), "standard")
        require_oi = bool(self.rs_oi_var.get())
        pause = bool(self.rs_offhours_var.get())
        cfg = self._clone_cfg()
        from src.data.quote_check import normalize_source

        source = normalize_source(self.rs_source_var.get())
        cfg.setdefault("data", {})["source"] = source
        cancel = threading.Event()
        self._rs_cancel = cancel
        self._rs_backfill_busy = True
        self.rs_backfill_btn.configure(state="disabled", text="补齐中…")
        self.rs_cancel_btn.configure(state="normal")
        self.rs_data_progress.configure(text="准备补齐 K 线，先检查本地缺口。")
        self._append_log(f"开始补齐K线 品种={','.join(symbols)} 周期={period}")
        root_dir = self.root_dir

        def _progress(payload: dict[str, Any]) -> None:
            self._post_to_main(lambda: self._on_backfill_progress(payload))

        def _run() -> None:
            from src.data.factor_backfill import run_kline_backfill

            try:
                report = run_kline_backfill(
                    cfg,
                    root_dir,
                    symbols,
                    [period],
                    mode=mode,
                    require_oi=require_oi,
                    source=source,
                    pause_when_trading=pause,
                    progress=_progress,
                    cancel=cancel,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("补齐K线失败")
                err = str(exc)

                def _fail() -> None:
                    self._finish_backfill()
                    self.rs_data_progress.configure(text=f"补齐失败：{err}", text_color="#F87171")
                    self._append_log(f"补齐K线失败: {err}")

                self._post_to_main(_fail)
                return

            def _done() -> None:
                self._finish_backfill()
                self._show_backfill_report(report)

            self._post_to_main(_done)

        threading.Thread(target=_run, name="factor-backfill", daemon=True).start()


    def _on_backfill_progress(self, payload: dict[str, Any]) -> None:
        if not hasattr(self, "rs_data_progress"):
            return
        event = str(payload.get("event") or "")
        message = str(payload.get("message") or "")
        if event == "task":
            index = payload.get("index")
            total = payload.get("total")
            message = f"[{index}/{total}] {message}"
        if message:
            self.rs_data_progress.configure(text=message, text_color=T.TEXT_DIM)


    def _finish_backfill(self) -> None:
        self._rs_backfill_busy = False
        self._rs_cancel = None
        if hasattr(self, "rs_backfill_btn"):
            self.rs_backfill_btn.configure(state="normal", text="补齐当前周期K线")
        if hasattr(self, "rs_cancel_btn"):
            self.rs_cancel_btn.configure(state="disabled")


    def _on_cancel_backfill(self) -> None:
        cancel = getattr(self, "_rs_cancel", None)
        if cancel is not None:
            cancel.set()
            self._append_log("已请求取消 K 线补齐")


    def _show_backfill_report(self, report: dict[str, Any]) -> None:
        status = str(report.get("status") or "")
        message = str(report.get("message") or "")
        if status == "paused":
            color = "#FBBF24"
        elif status == "canceled":
            color = "#FBBF24"
        elif int(report.get("failed") or 0) > 0:
            color = "#F87171"
        else:
            color = "#4ADE80"
        if hasattr(self, "rs_data_progress"):
            self.rs_data_progress.configure(text=message, text_color=color)
        self._append_log(f"补齐K线结束 {status} {message}")
        self._refresh_data_lights()


    def _research_symbols(self) -> list[str]:
        """这次扫描要看哪些合约：当前一个，或关注名单全部。"""
        if self.rs_scope_var.get().strip() == "只扫当前这个":
            one = (self.rs_symbol_var.get() or "").strip()
            if not one or one == "（还没有）":
                return []
            return [one]
        return [item for item in self._watch_symbols if item and item != "（还没有）"]


    def _refresh_research_picks(self) -> None:
        """按候选池文件画出勾选框。文件没有时给一句说明。"""
        frame = getattr(self, "rs_pick_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        from src.research.pool import load_pool

        pool = load_pool(self.root_dir)
        items = pool.get("items") or []
        self._rs_check_vars = {}
        if not items:
            ctk.CTkLabel(
                frame,
                text="还没有过关的因子。点上面「开始因子挖掘」。",
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                wraplength=320,
                justify="left",
            ).pack(anchor="w", padx=6, pady=8)
            return
        for item in items:
            key = str(item.get("id") or "")
            if not key:
                continue
            var = ctk.BooleanVar(value=bool(item.get("selected")))
            self._rs_check_vars[key] = var
            title = str(item.get("title") or key)
            horizon = item.get("horizon")
            label = f"{title} · 往后{horizon}根"
            ctk.CTkCheckBox(
                frame,
                text=label,
                variable=var,
                font=T.FONT_SMALL,
                text_color=T.TEXT,
            ).pack(anchor="w", padx=6, pady=3)


    def _on_save_research_picks(self) -> None:
        """只把勾选写回候选池，不启动扫描，也不下单。"""
        from tkinter import messagebox

        from src.research.pool import mark_selected

        chosen = {key for key, var in self._rs_check_vars.items() if var.get()}
        mark_selected(self.root_dir, chosen)
        self._append_log(f"已保存因子挖掘勾选 {len(chosen)} 条（仍不下单）")
        messagebox.showinfo("已保存勾选", "勾选已写入候选池。这不会改开平仓，也不会下单。")


    def _on_research_scan(self) -> None:
        """后台拉 K 线、挖因子、写入候选池。主线程只更新报告和勾选框。"""
        from tkinter import messagebox

        if self._rs_busy:
            messagebox.showinfo("正在挖掘", "上一轮因子挖掘还没结束。")
            return
        symbols = self._research_symbols()
        if not symbols:
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约。")
            return
        data_report = self._refresh_data_lights()
        if data_report.get("overall") == "red":
            detail = "\n".join(data_report.get("missing") or [])
            messagebox.showwarning(
                "数据还不够",
                (data_report.get("summary") or "K 线数据不满足研究门槛。")
                + ("\n" + detail if detail else "")
                + "\n\n可以点「补齐当前周期K线」，或把研究模式改成快速。",
            )
            return
        try:
            start, end = self._resolve_date_range(
                self.rs_range_var.get(), self.rs_start_var.get(), self.rs_end_var.get()
            )
        except ValueError as exc:
            messagebox.showerror("日期不对", str(exc))
            return
        if self.rs_range_var.get() == "全部可用" and (start or end):
            self.rs_range_var.set("自定义")
        # 勾了「含当晚夜盘」：只把这一次扫描的结束日往后推一个交易日，日期框不回写。
        end = self._extend_end_for_night(end, self.rs_include_night_var.get())
        period = self._period_code(self.rs_tf_var, self.rs_tf_custom_var)
        from src.data.quote_check import normalize_source

        source = normalize_source(self.rs_source_var.get())
        require_cross = bool(self.rs_cross_var.get())
        require_oi = bool(self.rs_oi_var.get())
        cfg = self._clone_cfg()
        cfg.setdefault("data", {})["source"] = source
        self._rs_busy = True
        goal = self.rs_goal_var.get()
        if goal == "状态识别":
            self._set_result_text(self.rs_result_box, "正在识别市场状态，请稍候。这不会改策略。")
        else:
            self._set_result_text(self.rs_result_box, "正在做因子挖掘，请稍候。这不会下单。")
        self._append_log(f"开始因子挖掘 目标={goal} 品种={','.join(symbols)} 周期={period}")

        def _run() -> None:
            import json
            from datetime import datetime
            from pathlib import Path

            import pandas as pd

            from src.data import KlineProvider
            from src.data.periods import drop_forming_bar
            from src.research.pool import load_pool, save_pool
            from src.research.scan import format_scan_report, scan_regularities
            from src.research.styles import advise_styles, format_style_section

            try:
                skipped: list[str] = []
                notes: list[str] = []
                bars_by_symbol: dict[str, Any] = {}
                counts: dict[str, int] = {}
                kp = KlineProvider(cfg)
                from src.research.data_requirements import is_specific_contract

                for symbol in symbols:
                    try:
                        df = kp.fetch(symbol, period, keep_history=True, start=start, end=end)
                        df = drop_forming_bar(df)
                        if df is None or len(df) == 0:
                            skipped.append(f"{symbol}：没有 K 线")
                            continue
                        if require_oi and not is_specific_contract(symbol):
                            # 主力连续换月时持仓量会跳变，宁可不算 OI，也不拿错数。
                            df = df.drop(columns=["oi"], errors="ignore")
                            notes.append(f"{symbol} 不是具体合约，本次不算持仓量因子")
                        if start:
                            df = df[df.index >= pd.Timestamp(start)]
                        if end:
                            df = df[df.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
                        if df is None or len(df) == 0:
                            skipped.append(f"{symbol}：这段日期没有 K 线")
                            continue
                        bars_by_symbol[symbol] = df
                        counts[symbol] = int(len(df))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("因子挖掘拉行情失败 {}: {}", symbol, exc)
                        skipped.append(f"{symbol}：{exc}")
                text = ""
                items: list[Any] = []
                engine_ids: set[str] = set()
                diagnostics: dict[str, Any] = {}
                if goal != "状态识别":
                    previous = load_pool(self.root_dir).get("items") or []
                    items = scan_regularities(
                        bars_by_symbol,
                        require_cross_symbol=require_cross,
                        previous=previous,
                        cfg=cfg,
                        period=period,
                        record=True,
                        diagnostics=diagnostics,
                    )
                    # 只给倾向，不改开平仓。分段不稳的条不会拿来投票。
                    advice = advise_styles(items)
                    path = save_pool(self.root_dir, items, extra={"style_advice": advice.to_dict()})
                    text = format_scan_report(
                        items,
                        symbols=symbols,
                        bars=counts,
                        skipped=skipped,
                        style_text=format_style_section(advice),
                        notes=notes,
                        diagnostics=diagnostics,
                    )
                    text += f"\n候选池  {path}"
                else:
                    text = "状态识别完成（这不是买卖点，挖掘层不会下单）\n"
                    text += "品种  " + "、".join(counts) + "\n"
                    if skipped:
                        text += "没读成的品种：" + "；".join(skipped) + "\n"

                if goal in ("状态识别", "全流程"):
                    from src.research.regimes import (
                        fit_regimes,
                        format_regime_report,
                        save_regimes,
                    )

                    blocks: list[str] = []
                    for symbol, frame in bars_by_symbol.items():
                        try:
                            regime = fit_regimes(frame)
                        except Exception as exc:  # noqa: BLE001
                            blocks.append(f"\n{symbol}：状态识别跳过（{exc}）")
                            continue
                        save_regimes(self.root_dir, regime, symbol=symbol)
                        for entry in regime.engines:
                            for engine in entry.get("engines") or []:
                                engine_ids.add(str(engine.get("id") or ""))
                        blocks.append(f"\n【{symbol}】" + format_regime_report(regime))
                    text += "\n".join(blocks) if blocks else "\n状态识别没有可用的数据。"

                # 每次研究留一个可回看的目录：报告原文 + 结构化结果。
                run_dir = (
                    Path(self.root_dir)
                    / "data"
                    / "factor_mining"
                    / "runs"
                    / datetime.now().strftime("%Y%m%d_%H%M%S")
                )
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text(text, encoding="utf-8")
                payload = {
                    "goal": goal,
                    "symbols": list(counts),
                    "period": period,
                    "diagnostics": diagnostics,
                    "items": [item.to_dict() for item in items],
                }
                (run_dir / "results.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                text += f"\n研究运行目录  {run_dir}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("因子挖掘失败")
                err = str(exc)

                def _fail() -> None:
                    self._rs_busy = False
                    self._set_result_text(self.rs_result_box, f"因子挖掘失败:\n{err}")
                    self._append_log(f"因子挖掘失败: {err}")

                self._post_to_main(_fail)
                return

            def _done() -> None:
                self._rs_busy = False
                self._set_result_text(self.rs_result_box, text)
                self._refresh_research_picks()
                if engine_ids:
                    self._prefill_cross_engines(engine_ids)
                self._append_log(f"因子挖掘完成 过关{len(items)}条")

            self._post_to_main(_done)

        threading.Thread(target=_run, name="research-scan", daemon=True).start()

