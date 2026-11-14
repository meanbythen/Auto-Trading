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
        规律候选：在历史 K 线上扫因子和后面涨跌的关系，过关的写进候选池。

        这里不下单。勾选只表示你觉得这条逻辑说得通，不会改现在哪根 K 线开仓。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="规律候选", font=T.FONT_TITLE, text_color=T.TEXT).pack(
            anchor="w", padx=4, pady=(4, 2)
        )
        ctk.CTkLabel(
            left,
            text="程序在一份写清经济学逻辑的因子清单上，看它们和后面几根涨跌是否稳。两段时间都显著才进池，再看分段里大部分时间是否同向。说明书开头会写更像趋势、突破、震荡还是波段，只是倾向，不会自动换策略，也不会下单。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

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
        self._date_row(box, "结束日期", self.rs_end_var, hint="点日历；清空=到最新")
        ctk.CTkCheckBox(
            box,
            text="多个品种时，至少两个合约都过关才进池",
            variable=self.rs_cross_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(6, 4))

        ctk.CTkButton(
            left,
            text="开始扫描规律",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_research_scan,
        ).pack(fill="x", padx=4, pady=(12, 6))
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
        ctk.CTkLabel(right, text="规律说明书", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.rs_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.rs_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._refresh_research_picks()


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
                text="还没有过关的规律。点上面「开始扫描规律」。",
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
        self._append_log(f"已保存规律勾选 {len(chosen)} 条（仍不下单）")
        messagebox.showinfo("已保存勾选", "勾选已写入候选池。这不会改开平仓，也不会下单。")


    def _on_research_scan(self) -> None:
        """后台拉 K 线、扫规律、写入候选池。主线程只更新说明和勾选框。"""
        from tkinter import messagebox

        if self._rs_busy:
            messagebox.showinfo("正在扫描", "上一轮规律扫描还没结束。")
            return
        symbols = self._research_symbols()
        if not symbols:
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约。")
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
        period = self._period_code(self.rs_tf_var, self.rs_tf_custom_var)
        from src.data.quote_check import normalize_source

        source = normalize_source(self.rs_source_var.get())
        require_cross = bool(self.rs_cross_var.get())
        cfg = self._clone_cfg()
        cfg.setdefault("data", {})["source"] = source
        self._rs_busy = True
        self._set_result_text(self.rs_result_box, "正在扫描规律，请稍候。这不会下单。")
        self._append_log(f"开始扫描规律 品种={','.join(symbols)} 周期={period}")

        def _run() -> None:
            import pandas as pd

            from src.data import KlineProvider
            from src.data.periods import drop_forming_bar
            from src.research.pool import load_pool, save_pool
            from src.research.scan import format_scan_report, scan_regularities
            from src.research.styles import advise_styles, format_style_section

            try:
                skipped: list[str] = []
                bars_by_symbol: dict[str, Any] = {}
                counts: dict[str, int] = {}
                kp = KlineProvider(cfg)
                for symbol in symbols:
                    try:
                        df = kp.fetch(symbol, period, keep_history=True, start=start, end=end)
                        df = drop_forming_bar(df)
                        if df is None or len(df) == 0:
                            skipped.append(f"{symbol}：没有 K 线")
                            continue
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
                        logger.warning("规律扫描拉行情失败 {}: {}", symbol, exc)
                        skipped.append(f"{symbol}：{exc}")
                previous = load_pool(self.root_dir).get("items") or []
                items = scan_regularities(
                    bars_by_symbol,
                    require_cross_symbol=require_cross,
                    previous=previous,
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
                )
                text += f"\n候选池  {path}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("规律扫描失败")
                err = str(exc)

                def _fail() -> None:
                    self._rs_busy = False
                    self._set_result_text(self.rs_result_box, f"规律扫描失败:\n{err}")
                    self._append_log(f"规律扫描失败: {err}")

                self._post_to_main(_fail)
                return

            def _done() -> None:
                self._rs_busy = False
                self._set_result_text(self.rs_result_box, text)
                self._refresh_research_picks()
                self._append_log(f"规律扫描完成 过关{len(items)}条")

            self._post_to_main(_done)

        threading.Thread(target=_run, name="research-scan", daemon=True).start()

