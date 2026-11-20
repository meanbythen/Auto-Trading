"""Strategy optimization, parameter versions, and optimized-parameter writes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger
from src.ui import theme as T
from src.ui.app_common import ENGINE_CHOICES
from src.ui.app_common import _duration_text
from src.utils import save_config
import customtkinter as ctk
import os
import threading
import time


class OptimizeTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_optimize_tab(self, tab: ctk.CTkFrame) -> None:
        """策略优化：在选定时间段上对策略参数做网格搜索，可写回配置。"""
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="策略优化", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="训练段挑参数，检验段只验收。第一次选旋钮：直接开始优化。已经在用、后来衰减了：打开「对照正在用的参数」再搜，默认不改实盘。写入前会备份这一套策略的参数版本；需要整页还原时再留配置存档。两份不是同一本账。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        engines = list(ENGINE_CHOICES)
        opt_engine_menu = self._option_row(box, "策略引擎", self.opt_engine_var, engines, width=240)
        # 换了策略，下面的版本列表只显示这一套，出厂默认也只重置这一套。
        opt_engine_menu.configure(command=lambda _choice: self._refresh_opt_history())
        from src.data.quote_check import SOURCE_LABELS

        self._option_row(box, "行情数据源", self.opt_source_var, list(SOURCE_LABELS), width=160)
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.opt_symbol_var.get() not in symbol_values:
            self.opt_symbol_var.set(symbol_values[0])
        self.opt_symbol_menu = self._option_row(box, "策略优化品种", self.opt_symbol_var, symbol_values, width=160)
        ctk.CTkLabel(
            box,
            text="和配置页的关注品种是同一份。要改成配置页当前选中的合约，点下面「按配置页重新填入」，或在历史回测页点「把条件同步到策略优化页」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._timeframe_row(box, "信号周期", self.opt_tf_var, self.opt_tf_custom_var, width=140)
        self._timeframe_row(box, "止损周期", self.opt_trail_var, self.opt_trail_custom_var, width=140)
        self._option_row(
            box,
            "时间段",
            self.opt_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.opt_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.opt_end_var, hint="点日历；清空=到最新。只算到当天 20:59")
        self._pick_switch_row(box, "含当晚夜盘", self.opt_include_night_var)
        self._option_row(
            box,
            "搜索深度",
            self.opt_depth_var,
            ["精简", "标准", "完整"],
            width=160,
        )
        file_row = ctk.CTkFrame(box, fg_color="transparent")
        file_row.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkButton(
            file_row,
            text="打开优化参数表",
            width=140,
            height=28,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT_DIM,
            command=self._open_optimizer_file,
        ).pack(side="left")
        ctk.CTkLabel(
            file_row,
            text="config/优化参数.xlsx：改网格档位和中文名，保存后下次优化生效",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left", padx=(8, 0))
        self._entry_row(
            box,
            "最多试几组",
            self.opt_trials_var,
            hint="理论组合超过这个数就随机抽；没超过就全跑",
            width=100,
        )
        self._entry_row(
            box,
            "检验比例%",
            self.opt_holdout_pct_var,
            hint="默认30，可填10～50。剩下的才用来挑参数",
            width=100,
        )
        self._option_row(
            box,
            "检验方式",
            self.opt_mode_var,
            ["一次切断", "滚动前进"],
            width=160,
        )
        self._entry_row(
            box,
            "滚动段数",
            self.opt_walk_steps_var,
            hint="仅滚动前进用，默认3。段数越多越久",
            width=100,
        )
        self._pick_switch_row(box, "冻结最后半年", self.opt_freeze_var)
        self._entry_row(
            box,
            "冻结天数",
            self.opt_freeze_days_var,
            hint="默认180天。这段只验收一次，搜索碰不到；看过就不能再当验收",
            width=100,
        )
        ctk.CTkLabel(
            box,
            text="一次切断：按比例切一刀。滚动前进：把检验段拆成多段，走一步再验一步。打开「冻结最后半年」时，最后这段永远不参与挑参数；过关后再验收一次，看过就不能用同一段再调参。优化结束后还会把检验成交打乱，分不清规则还是运气时不写入。其它关注品种会用同一组参数再跑一遍（不再重搜）。开仓限制跟下面这一块（和历史回测共用），点开始不会被交易配置改回去。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        self._pack_open_limits(left)

        run = self._card(left)
        ctk.CTkLabel(run, text="开始优化", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w", pady=(0, 4))
        self._pick_switch_row(run, "对照正在用的参数", self.opt_compare_var)
        ctk.CTkLabel(
            run,
            text="关掉：第一次选旋钮，只出搜索成绩。打开：衰减后重跑，多和现在这套对照，跑完不自动写。两种都用同一套搜索。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(
            run,
            text="开始策略优化",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_optimize,
        ).pack(fill="x")

        write = self._card(left)
        ctk.CTkLabel(write, text="写入配置", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w", pady=(0, 4))
        pick = ctk.CTkFrame(write, fg_color="transparent")
        pick.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(pick, text="写入第几组", font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(side="left")
        ctk.CTkEntry(
            pick,
            textvariable=self.opt_write_index_var,
            width=54,
            height=28,
            justify="center",
        ).pack(side="left", padx=(6, 6))
        ctk.CTkLabel(
            pick,
            text="1=训练最优；其它组会先复核检验",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left")
        self._pick_switch_row(write, "同时留下整页配置存档", self.opt_save_preset_var)
        ctk.CTkLabel(
            write,
            text="整页存档在配置页「配置存档」里，可一键换回策略、品种、模拟开关。对照全面更好时，按钮会写成「写入并切实盘」，并且一定会留这份存档。没更好仍可写入，会多问一次。常驻在跑时不能写。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        self.opt_write_btn = ctk.CTkButton(
            write,
            text="把所选参数写入配置",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_apply_opt_params,
        )
        self.opt_write_btn.pack(fill="x")

        # —— 参数版本回溯：只还原这一套策略的旋钮，不是整页配置存档 ——
        hist = self._card(left)
        ctk.CTkLabel(
            hist,
            text="参数版本历史（只还原这一套旋钮）",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            hist,
            text="和上面的整页配置存档不是同一份。这里只改当前选中的这一套策略参数，不改正在用的策略名、品种、周期，也不改另外几套。写入、切实盘、回溯前都会自动加一条。出厂默认不能删，用来重置初始。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        self._opt_hist_id_by_label: dict[str, str] = {}
        self.opt_hist_var = ctk.StringVar(value="★ 出厂默认（只重置当前选中的策略）")
        self.opt_hist_menu = ctk.CTkOptionMenu(
            hist,
            variable=self.opt_hist_var,
            values=["★ 出厂默认（只重置当前选中的策略）"],
            width=300,
            fg_color=T.BG_ELEVATED,
            button_color=T.ACCENT,
            button_hover_color=T.ACCENT_HOVER,
            dropdown_fg_color=T.BG_PANEL,
            text_color=T.TEXT,
            dropdown_text_color=T.TEXT,
        )
        self.opt_hist_menu.pack(anchor="w", pady=(0, 8))
        self.opt_hist_menu.configure(command=self._sync_hist_name_box)
        name_row = ctk.CTkFrame(hist, fg_color="transparent")
        name_row.pack(fill="x", pady=(0, 8))
        self.opt_hist_name_var = ctk.StringVar(value="出厂默认")
        self.opt_hist_name_entry = ctk.CTkEntry(
            name_row,
            textvariable=self.opt_hist_name_var,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
        )
        self.opt_hist_name_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.opt_hist_rename_btn = ctk.CTkButton(
            name_row,
            text="改名",
            height=32,
            width=64,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_rename_opt_history,
        )
        self.opt_hist_rename_btn.pack(side="left", padx=(0, 6))
        self.opt_hist_delete_btn = ctk.CTkButton(
            name_row,
            text="删除",
            height=32,
            width=64,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_delete_opt_history,
        )
        self.opt_hist_delete_btn.pack(side="left")
        hist_btns = ctk.CTkFrame(hist, fg_color="transparent")
        hist_btns.pack(fill="x")
        ctk.CTkButton(
            hist_btns,
            text="刷新列表",
            height=32,
            width=90,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._refresh_opt_history,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            hist_btns,
            text="回溯到选中版本",
            height=32,
            width=120,
            fg_color=T.DANGER_MUTED,
            hover_color=T.DANGER,
            text_color=T.DANGER_TEXT,
            command=self._on_restore_opt_history,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            hist,
            text="重置当前策略为出厂默认",
            height=34,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_reset_factory_params,
        ).pack(fill="x", pady=(8, 4))
        excel_row = ctk.CTkFrame(hist, fg_color="transparent")
        excel_row.pack(fill="x", pady=(4, 0))
        ctk.CTkButton(
            excel_row,
            text="导出追加到Excel",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_params_excel,
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            excel_row,
            text="从Excel导入",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_import_params_excel,
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))
        ctk.CTkLabel(
            hist,
            text="程序内只留最近30条。导出会同时写「优化参数」表（参数版本）和「优化结果」表（最近一次的完整成绩，第一列测试时间分清是哪一次），都在 data/Auto-Trading导出.xlsx。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(3, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="策略优化结果", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        # 进度条 + 一行文字：第几组/共几组、已用时间、预计还要多久。
        self.opt_progress_label = ctk.CTkLabel(
            right,
            text="尚未开始",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
            justify="left",
        )
        self.opt_progress_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.opt_progress = ctk.CTkProgressBar(right, height=10, progress_color=T.ACCENT)
        self.opt_progress.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.opt_progress.set(0)
        self._opt_started_at: float | None = None
        self._opt_progress_at = 0.0
        self.opt_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.opt_result_box.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.opt_result_box.insert(
            "end",
            "点左侧「开始策略优化」。第一次选旋钮：开关关掉即可。\n"
            "衰减后打开「对照正在用的参数」再搜，默认不改实盘。\n"
            "全面更好时，写入按钮会变成「写入并切实盘」。切坏了用下方回溯或重置出厂。\n",
        )
        # 打开界面时刷新一次历史下拉
        self.after(200, self._refresh_opt_history)


    def _show_opt_progress(self, done: int, total: int, phase: str = "", *, force: bool = False) -> None:
        """
        策略优化进度：第几组 / 共几组、百分比、已用时间、预计还要多久。

        进度只是提示，不能影响优化。默认 0.4 秒最多刷一次，避免几百组时把界面刷爆。
        """
        if not hasattr(self, "opt_progress"):
            return
        total = max(1, int(total))
        done = max(0, min(int(done), total))
        now = time.monotonic()
        if not force and now - self._opt_progress_at < 0.4:
            return
        self._opt_progress_at = now
        started = self._opt_started_at or now
        elapsed = max(0.0, now - started)
        self.opt_progress.set(done / total)
        if done <= 0:
            self.opt_progress_label.configure(text=f"{phase}… 已用 {_duration_text(elapsed)}")
            return
        percent = done / total * 100.0
        eta = ""
        if done < total:
            eta = f"，预计还要 {_duration_text(elapsed / done * (total - done))}"
        self.opt_progress_label.configure(
            text=f"{phase} {done}/{total}（{percent:.0f}%），已用 {_duration_text(elapsed)}{eta}"
        )


    def _finish_opt_progress(self, *, ok: bool) -> None:
        """优化结束：进度条收尾，并写一句总用时。"""
        if not hasattr(self, "opt_progress"):
            return
        elapsed = time.monotonic() - (self._opt_started_at or time.monotonic())
        self.opt_progress.set(1.0 if ok else 0.0)
        self.opt_progress_label.configure(
            text=(f"完成，用时 {_duration_text(elapsed)}" if ok else f"失败，用时 {_duration_text(elapsed)}")
        )
        self._opt_started_at = None
        self._opt_progress_at = 0.0


    def _sync_opt_write_button(self) -> None:
        """对照全面更好时，写入按钮改成切实盘；否则仍是原来的写入。"""
        btn = getattr(self, "opt_write_btn", None)
        if btn is None:
            return
        if getattr(self, "_last_evolve_better", False):
            if hasattr(self, "opt_save_preset_var"):
                self.opt_save_preset_var.set(True)
            btn.configure(
                text="写入并切实盘（先存档）",
                fg_color=T.ACCENT,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON,
                border_width=0,
            )
            return
        btn.configure(
            text="把所选参数写入配置",
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.ACCENT,
            border_width=1,
            border_color=T.ACCENT,
        )


    def _on_optimize(self, evolve_only: bool = False) -> None:
        """后台网格优化：对选定策略/时间段反复回测打分。

        打开「对照正在用的参数」时，搜完再和现在这套比，默认不写配置。
        """
        from tkinter import messagebox

        if self.worker.running:
            messagebox.showwarning("请先停止", "策略优化较耗资源，请先停止常驻扫描。")
            return
        evolve_only = bool(evolve_only) or bool(self.opt_compare_var.get())
        self._last_evolve_better = False
        self._last_evolve_reason = ""
        self._last_opt_compared = False
        self._sync_opt_write_button()

        symbol = self.opt_symbol_var.get().strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加要优化的合约。")
            return
        # 开仓限制、收盘清仓、测试资金都听回测/优化页上已经填好的格子。
        cfg = self._clone_cfg()
        try:
            self._apply_backtest_limits(cfg, symbol=symbol)
        except ValueError as e:
            messagebox.showerror("数字不对", str(e))
            return
        st = cfg.setdefault("strategy", {})
        engine = self.opt_engine_var.get()
        st["engine"] = engine
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.opt_source_var.get())
        st["timeframe"] = self._period_code(self.opt_tf_var, self.opt_tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.opt_trail_var, self.opt_trail_custom_var)
        st["symbols"] = [symbol]
        self._apply_backtest_switches(cfg)
        try:
            preset = self.opt_range_var.get()
            # 和历史回测同一层：日期填错时走下面的 except，不会在启动后点优化才崩。
            start, end = self._resolve_date_range(
                preset, self.opt_start_var.get(), self.opt_end_var.get()
            )
        except ValueError as e:
            messagebox.showerror("日期不对", str(e))
            return
        if preset == "全部可用" and (start or end):
            self.opt_range_var.set("自定义")
        if start:
            self.opt_start_var.set(start)
        if end:
            self.opt_end_var.set(end)
        # 勾了「含当晚夜盘」：只把这一次优化的结束日往后推一个交易日，日期框不回写。
        end = self._extend_end_for_night(end, self.opt_include_night_var.get())
        try:
            max_trials = max(5, int(float(self.opt_trials_var.get() or 80)))
        except ValueError:
            max_trials = 80
        try:
            holdout_pct = float(self.opt_holdout_pct_var.get() or 30)
        except ValueError:
            holdout_pct = 30.0
        try:
            walk_steps = max(1, int(float(self.opt_walk_steps_var.get() or 3)))
        except ValueError:
            walk_steps = 3
        try:
            freeze_days = min(730, max(30, int(float(self.opt_freeze_days_var.get() or 180))))
        except ValueError:
            freeze_days = 180
        bt_run = cfg.setdefault("backtest", {})
        bt_run["freeze_final_oos"] = bool(self.opt_freeze_var.get())
        bt_run["freeze_final_days"] = freeze_days
        depth = self.opt_depth_var.get().strip() or "标准"
        mode = self.opt_mode_var.get().strip() or "一次切断"
        from src.backtest.optimizer import describe_run_switches

        switch_text = describe_run_switches(
            engine,
            flatten_at_close=bool(self.bt_flatten_var.get()),
        )
        from src.backtest.engine import FillModel

        fill_text = FillModel.from_cfg(cfg, symbol).describe()

        risk_now = cfg.get("risk") or {}
        trade_now = cfg.get("trade") or {}
        # 先记下这次优化用的限制。最优一组跑完后，用那次回测的快照覆盖，里面带跳过次数。
        limit_snap = {
            "apply_limits": bool(risk_now.get("apply_limits", True)),
            "sizing_mode": risk_now.get("sizing_mode"),
            "capital": risk_now.get("capital"),
            "risk_per_trade_pct": risk_now.get("risk_per_trade_pct"),
            "fixed_volume": trade_now.get("volume"),
            "min_volume": risk_now.get("min_volume"),
            "max_daily_loss": risk_now.get("max_daily_loss"),
            "min_order_interval_sec": risk_now.get("min_order_interval_sec"),
            "max_orders_per_day": risk_now.get("max_orders_per_day"),
            "margin_per_lot": risk_now.get("margin_per_lot", 0),
            "skipped_opens": {},
        }
        cross_symbols = list(self._watch_symbols)
        job_name = "模拟重跑优化（只出报告）" if evolve_only else "开始策略优化"
        self._append_log(
            f"{job_name}… engine={engine} depth={depth} mode={mode} "
            f"检验比例={holdout_pct}% "
            f"冻结={'开'+str(freeze_days)+'天' if self.opt_freeze_var.get() else '关'} "
            f"symbol={symbol} "
            f"行情={self.opt_source_var.get()} "
            f"限制={'开' if limit_snap['apply_limits'] else '关'} "
            f"trials<={max_trials} 区间={start or '起'}~{end or '止'}"
        )
        self.tabs.set("策略优化")
        self._opt_started_at = time.monotonic()
        self._opt_progress_at = 0.0
        self._show_opt_progress(0, 1, "准备行情", force=True)
        if hasattr(self, "opt_result_box"):
            try:
                from src.backtest.optimizer import describe_grid, grid_for_engine

                preview = describe_grid(grid_for_engine(engine, depth, cfg))
            except Exception:  # noqa: BLE001
                preview = ""
            mode_hint = (
                f"滚动前进 {walk_steps} 段"
                if mode == "滚动前进"
                else f"一次切断，检验比例约 {holdout_pct}%"
            )
            evolve_hint = "这轮只出报告，不会改正在用的参数。\n" if evolve_only else ""
            self._set_result_text(
                self.opt_result_box,
                f"策略优化进行中（{mode_hint}，depth={depth}，最多{max_trials}组）…\n"
                f"{evolve_hint}"
                f"请不要关闭窗口。\n\n本次搜索旋钮：\n{preview}\n",
            )

        def _run() -> None:
            try:
                from src.backtest.optimizer import (
                    describe_params,
                    format_perf_block,
                    optimize_strategy,
                    save_optimization_report,
                )

                result = optimize_strategy(
                    cfg,
                    symbol=symbol,
                    max_trials=max_trials,
                    start_date=start,
                    end_date=end,
                    engine=engine,
                    depth=depth,
                    holdout_pct=holdout_pct,
                    mode=mode,
                    walk_steps=walk_steps,
                    cross_symbols=cross_symbols,
                    progress=lambda done, total, phase: self._post_to_main(
                        lambda d=done, t=total, p=phase: self._show_opt_progress(d, t, p)
                    ),
                )
                path = self.root_dir / "data" / "optimize_last.json"
                save_optimization_report(result, path)
                evolve_block: list[str] = []
                evolve_can_apply = False
                evolve_reason = ""
                if evolve_only:
                    from src.backtest.optimizer import (
                        current_params_from_config,
                        check_params_on_holdout,
                        judge_better_than_current,
                        save_evolve_report,
                    )

                    old_params = current_params_from_config(
                        cfg, engine, list((result.best_params or {}).keys())
                    )
                    self._post_to_main(
                        lambda: self._show_opt_progress(1, 1, "对照现在的参数", force=True)
                    )
                    old_checked = check_params_on_holdout(
                        cfg,
                        params=old_params,
                        symbol=symbol,
                        engine=engine,
                        depth=depth,
                        holdout_pct=holdout_pct,
                        start_date=start,
                        end_date=end,
                        cross_symbols=cross_symbols,
                    )
                    verdict = judge_better_than_current(
                        result.holdout_performance or {},
                        old_checked.get("holdout_performance") or {},
                        accepted=bool(result.accepted),
                        new_params=dict(result.best_params or {}),
                        old_params=old_params,
                    )
                    evolve_can_apply = bool(verdict.get("can_apply_live"))
                    evolve_reason = str(verdict.get("reason") or "")
                    report_path = self.root_dir / "data" / "evolve_last.json"
                    save_evolve_report(
                        {
                            "symbol": result.symbol,
                            "engine": result.engine,
                            "new_params": dict(result.best_params or {}),
                            "old_params": old_params,
                            "new_accepted": bool(result.accepted),
                            "new_accept_reason": str(result.accept_reason or ""),
                            "new_holdout": dict(result.holdout_performance or {}),
                            "old_holdout": dict(old_checked.get("holdout_performance") or {}),
                            "old_accepted": bool(old_checked.get("accepted")),
                            "verdict": verdict,
                            "optimize_report": str(path),
                        },
                        report_path,
                    )
                    evolve_block = [
                        "",
                        "—— 和现在这套对照（模拟重跑，没有改实盘）——",
                        f"现在的参数  {describe_params(old_params)}",
                        f"现在检验净盈亏  {float((old_checked.get('holdout_performance') or {}).get('net_pnl') or 0):.0f}"
                        f"  回撤 {float((old_checked.get('holdout_performance') or {}).get('max_drawdown') or 0):.0f}"
                        f"  盈利因子 {float((old_checked.get('holdout_performance') or {}).get('profit_factor') or 0):.2f}",
                        f"新的检验净盈亏  {float((result.holdout_performance or {}).get('net_pnl') or 0):.0f}"
                        f"  回撤 {float((result.holdout_performance or {}).get('max_drawdown') or 0):.0f}"
                        f"  盈利因子 {float((result.holdout_performance or {}).get('profit_factor') or 0):.2f}",
                        *(str(x) for x in (verdict.get("checks") or [])),
                        str(verdict.get("reason") or ""),
                        f"对照报告  {report_path}",
                    ]
                    if evolve_can_apply:
                        evolve_block.append(
                            "左侧写入按钮会改成「写入并切实盘（先存档）」。切之前会留下整页配置存档和参数版本。"
                        )
                    else:
                        evolve_block.append(
                            "默认不建议切实盘。仍可点「把所选参数写入配置」，程序会再问一次。"
                        )
                self._last_opt_params = dict(result.best_params or {})
                # 同时记住引擎，写入配置时用。没通过后一段时，下面的写入按钮会拒绝保存。
                self._last_opt_engine = engine
                self._last_opt_accepted = bool(result.accepted)
                self._last_opt_reason = str(result.accept_reason or "")
                self._last_opt_start = str(start or "")
                self._last_opt_end = str(end or "")
                # 记下全部打分行和本轮设置，供「写入第几组」复核非最优组。
                self._last_opt_trials = list(result.trials or [])
                self._last_opt_ctx = {
                    "symbol": result.symbol,
                    "engine": result.engine,
                    "depth": result.depth,
                    "holdout_pct": result.holdout_pct,
                    "start_date": str(result.start_date or ""),
                    "end_date": str(result.end_date or ""),
                    "cross_symbols": list(cross_symbols or []),
                }

                train = result.train_performance or result.best_performance or {}
                hold = result.holdout_performance or {}
                top = result.trials[:8]
                mode_line = f"检验方式  {result.mode}，检验比例 {result.holdout_pct:.0f}%"
                if result.mode == "滚动前进":
                    mode_line += f"，实际 {result.walk_steps} 段"
                lines = [
                    f"策略优化完成  {result.symbol}",
                    f"策略      {result.engine}（搜索深度 {result.depth}）",
                    mode_line,
                    switch_text,
                    fill_text,
                    self._format_limit_lines(getattr(result, "best_limits", None) or limit_snap),
                    f"K线根数   一共{result.bars}根",
                    f"训练段    {result.train_bars}根，到 {result.train_end or '切断处'}，用来挑参数",
                    f"检验段    {result.holdout_bars}根，从 {result.holdout_start or '切断处'} 起，不参与挑参数",
                    f"挑出的参数  {describe_params(result.best_params)}",
                    "",
                    *format_perf_block(
                        "训练成绩",
                        train,
                        bars=result.train_bars,
                        span=f"到 {result.train_end or '切断处'}",
                        data_from=str(result.start_date or ""),
                        data_to=str(result.train_end or result.end_date or ""),
                    ),
                    "",
                    *format_perf_block(
                        "检验成绩",
                        hold,
                        bars=result.holdout_bars,
                        span=f"从 {result.holdout_start or '切断处'} 起",
                        data_from=str(result.holdout_start or result.start_date or ""),
                        data_to=str(result.end_date or ""),
                    ),
                ]
                lines.append("")
                lines.append("—— 稳健性（公开渠道常用）——")
                lines.append(
                    f"检验效率  {float(getattr(result, 'walk_efficiency', 0) or 0):.2f}"
                    "（检验净盈亏÷训练净盈亏；大约 0.50 以上较稳，低于 0.30 很虚）"
                )
                if result.mode == "滚动前进":
                    lines.append(
                        f"检验段赚钱比例  {float(getattr(result, 'oos_win_rate', 0) or 0):.0%}"
                        "（赚钱的滚动段÷有成绩的段；不到一半不能写入）"
                    )
                    stab = str(getattr(result, "param_stability", "") or "")
                    if stab:
                        lines.append(f"参数稳定  {stab}")
                if result.walk_folds:
                    lines.append("")
                    lines.append("—— 滚动每一段 ——")
                    for fold in result.walk_folds:
                        tp = fold.get("train_performance") or {}
                        hp = fold.get("holdout_performance") or {}
                        lines.append(
                            f"第{fold.get('fold')}段 训练到{fold.get('train_end')} "
                            f"净盈亏{float(tp.get('net_pnl') or 0):.0f}；"
                            f"检验{fold.get('holdout_start')}~{fold.get('holdout_end')} "
                            f"净盈亏{float(hp.get('net_pnl') or 0):.0f} "
                            f"{'过关' if fold.get('accepted') else '未过'}"
                        )
                if result.cross_symbol_rows:
                    lines.append("")
                    lines.append("—— 多品种验收（参数原样，不再重搜）——")
                    for row in result.cross_symbol_rows:
                        if row.get("error"):
                            lines.append(f"{row.get('symbol')}  失败：{row.get('error')}")
                            continue
                        cover = float(row.get("net_pnl") or 0) - float(row.get("max_drawdown") or 0)
                        lines.append(
                            f"{row.get('symbol')}  成交{int(row.get('trades') or 0)}笔 "
                            f"胜率{float(row.get('win_rate') or 0):.0%} "
                            f"净盈亏{float(row.get('net_pnl') or 0):.0f} "
                            f"盈利因子{float(row.get('profit_factor') or 0):.2f} "
                            f"最大回撤{float(row.get('max_drawdown') or 0):.0f} "
                            f"覆盖{cover:.0f}"
                        )
                from src.backtest.evaluation import format_evaluation_lines
                from src.backtest.frozen_oos import format_freeze_lines
                from src.backtest.monte_carlo import format_monte_carlo_lines

                lines.extend(
                    [
                        "",
                        *format_freeze_lines(getattr(result, "freeze", None)),
                        "",
                        *format_monte_carlo_lines(getattr(result, "monte_carlo", None)),
                        *(
                            format_evaluation_lines(result.evaluation, title="策略评估")
                            if result.evaluation
                            else []
                        ),
                        "",
                        self._last_opt_reason,
                    f"报告      {path}",
                    "",
                        "—— 本次搜索的旋钮与取值范围 ——",
                    result.grid_describe or "",
                    "",
                        "—— 训练段得分靠前的几组 ——",
                ]
                )
                for i, row in enumerate(top, 1):
                    pr = row.get("performance") or {}
                    cover = float(pr.get("net_pnl") or 0) - float(pr.get("max_drawdown") or 0)
                    lines.append(
                        f"#{i} score={float(row.get('score', 0)):.3f} "
                        f"trades={pr.get('trades', 0)} "
                        f"wr={float(pr.get('win_rate', 0)):.0%} "
                        f"net={float(pr.get('net_pnl', 0)):.0f} "
                        f"cover={cover:.0f} | {describe_params(row.get('params'))}"
                    )
                lines.append("")
                if evolve_only:
                    lines.extend(evolve_block)
                elif result.accepted:
                    lines.append(
                        "可以在左侧「写入第几组」填 #编号（默认 1=训练最优），再点「把所选参数写入配置」。"
                        "选其它组时会先按检验段重新复核，复核没过就不写。"
                    )
                    lines.append(
                        "策略、品种、周期和开仓限制要另外点历史回测页「把本页参数写入配置页」，这次不会自动写进去。"
                    )
                else:
                    lines.append(
                        "第 1 组没有通过检验，不能直接写；可以换「写入第几组」选其它组，"
                        "程序会按检验段重新复核，过了才允许写入。"
                    )
                text = "\n".join(lines)
                verdict = "可以写入" if result.accepted else "不能写入"
                if evolve_only:
                    verdict = "可以切实盘" if evolve_can_apply else "不切实盘"
                self._last_evolve_reason = evolve_reason
                self._post_to_main(lambda: self._append_log(
                    f"{'模拟重跑' if evolve_only else '策略优化'}完成 {result.symbol} | "
                    f"训练得分={result.best_score:.3f} | {verdict} | "
                    f"{describe_params(result.best_params)}"
                ))
                if hasattr(self, "opt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.opt_result_box, t))
                self._post_to_main(lambda: self._finish_opt_progress(ok=True))
                self._post_to_main(
                    lambda compared=bool(evolve_only), ok=evolve_can_apply, why=evolve_reason: (
                        setattr(self, "_last_opt_compared", compared),
                        setattr(self, "_last_evolve_better", bool(ok)),
                        setattr(self, "_last_evolve_reason", why),
                        self._sync_opt_write_button(),
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("策略优化失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"策略优化失败: {err}"))
                if hasattr(self, "opt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.opt_result_box, f"策略优化失败:\n{m}"))
                self._post_to_main(lambda: self._finish_opt_progress(ok=False))
                self._post_to_main(lambda m=err: messagebox.showerror("策略优化失败", m))

        threading.Thread(target=_run, daemon=True).start()


    def _refresh_opt_history(self, select_id: str | None = None) -> None:
        """刷新优化页的「参数版本」下拉列表。select_id 有值时，刷新后仍停在这一条。"""
        try:
            from src.backtest.param_history import list_version_labels

            engine = self.opt_engine_var.get() if hasattr(self, "opt_engine_var") else None
            pairs = list_version_labels(self.root_dir, engine)
            self._opt_hist_id_by_label = {label: vid for label, vid in pairs}
            labels = [p[0] for p in pairs] or ["（暂无历史）"]
            chosen = labels[0]
            if select_id:
                for label, vid in pairs:
                    if vid == select_id:
                        chosen = label
                        break
            if hasattr(self, "opt_hist_menu"):
                self.opt_hist_menu.configure(values=labels)
                self.opt_hist_var.set(chosen)
            self._sync_hist_name_box()
        except Exception as e:  # noqa: BLE001
            logger.warning("刷新参数历史失败: {}", e)


    def _sync_hist_name_box(self, _label: str | None = None) -> None:
        """下拉换了一条时，名字框跟着换成这一条现在的名字。出厂默认不能改。"""
        if not hasattr(self, "opt_hist_name_var"):
            return
        from src.backtest.param_history import FACTORY_DEFAULT_ID, get_version, version_display_name

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label, "")
        if not vid or vid == FACTORY_DEFAULT_ID:
            self.opt_hist_name_var.set("出厂默认")
            if hasattr(self, "opt_hist_name_entry"):
                self.opt_hist_name_entry.configure(state="disabled")
                self.opt_hist_rename_btn.configure(state="disabled")
            if hasattr(self, "opt_hist_delete_btn"):
                self.opt_hist_delete_btn.configure(state="disabled")
            return
        entry = get_version(self.root_dir, vid) or {}
        self.opt_hist_name_var.set(version_display_name(entry))
        if hasattr(self, "opt_hist_name_entry"):
            self.opt_hist_name_entry.configure(state="normal")
            self.opt_hist_rename_btn.configure(state="normal")
        if hasattr(self, "opt_hist_delete_btn"):
            self.opt_hist_delete_btn.configure(state="normal")


    def _on_rename_opt_history(self) -> None:
        """把当前选中的参数版本改成输入框里的名字。"""
        from tkinter import messagebox

        from src.backtest.param_history import FACTORY_DEFAULT_ID, rename_version

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label)
        if not vid or vid == FACTORY_DEFAULT_ID:
            messagebox.showwarning("不能改名", "出厂默认要留着当重置锚点，不能改名。")
            return
        new_name = self.opt_hist_name_var.get()
        try:
            rename_version(self.root_dir, vid, new_name)
        except ValueError as e:
            messagebox.showwarning("改名没有成功", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("参数版本改名失败")
            messagebox.showerror("改名失败", str(e))
            return
        self._refresh_opt_history(select_id=vid)
        self._append_log(f"参数版本已改名: {new_name.strip()}")


    def _on_delete_opt_history(self) -> None:
        """删掉优化页当前选中的那一条历史。不改正在使用的参数，出厂默认也不能删。"""
        from tkinter import messagebox

        from src.backtest.param_history import FACTORY_DEFAULT_ID, delete_version

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label)
        if not vid or vid == FACTORY_DEFAULT_ID:
            messagebox.showwarning("不能删除", "出厂默认要留着当重置锚点，不能删除。")
            return
        if not messagebox.askyesno("删除历史版本", f"只从历史列表里删掉这一条，当前参数不会变：\n\n{label}\n\n确定删除？"):
            return
        try:
            removed = delete_version(self.root_dir, vid)
        except ValueError as exc:
            messagebox.showwarning("没有删掉", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除参数版本失败")
            messagebox.showerror("删除失败", str(exc))
            return
        self._refresh_opt_history()
        self._append_log(f"已删除参数版本: {removed}")


    def _open_optimizer_file(self) -> None:
        """用系统默认程序打开优化参数表，不装 IDE 也能直接改网格档位和中文名。"""
        from tkinter import messagebox

        from src.backtest.param_table import ensure_param_table, resolve_param_table_path

        path = resolve_param_table_path(self.cfg)
        try:
            ensure_param_table(path)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            logger.exception("打开优化参数表失败")
            messagebox.showerror("打不开优化参数表", f"{e}\n\n路径：{path}")


    def _on_apply_opt_params(self) -> None:
        """按「写入第几组」写入：第 1 组直接写；其它组先重新复核检验，过了再写。

        对照全面更好时走切实盘（强制整页存档）。对照过但没更好时仍允许写入，会多问一次。
        """
        from tkinter import messagebox

        if self.worker.running:
            messagebox.showwarning(
                "请先停止",
                "常驻正在跑，不会自己换成新参数。\n请先点「停止常驻」，再写入，然后重新启动。",
            )
            return
        if not self._last_opt_params:
            messagebox.showwarning("还没有策略优化结果", "请先在本页点「开始策略优化」，完成后再写入。")
            return
        raw = (self.opt_write_index_var.get() or "").strip() or "1"
        try:
            idx = int(raw)
        except ValueError:
            messagebox.showwarning("组号填错了", "请填结果框里 #1、#2 这样的整数。")
            return
        trials = list(getattr(self, "_last_opt_trials", []) or [])
        live_switch = bool(getattr(self, "_last_evolve_better", False))
        save_page_preset = bool(self.opt_save_preset_var.get()) or live_switch
        if not trials:
            self._write_optimized_params(
                None, group_index=1, live_switch=live_switch, save_page_preset=save_page_preset
            )
            return
        if idx < 1 or idx > len(trials):
            messagebox.showwarning(
                "组号超出范围", f"这次优化共 {len(trials)} 组，只能填 1～{len(trials)}。"
            )
            return
        if idx == 1:
            self._write_optimized_params(
                None, group_index=1, live_switch=live_switch, save_page_preset=save_page_preset
            )
            return
        trial = trials[idx - 1]
        params = dict(trial.get("params") or {})
        if not params:
            messagebox.showwarning("这组没有参数", "换个组号再试。")
            return
        self._recheck_and_write_optimized(idx, params, trial)


    def _recheck_and_write_optimized(
        self, idx: int, params: dict[str, Any], trial: dict[str, Any]
    ) -> None:
        """非最优组：后台按检验段重新验收 + 多品种，通过了才让写入。"""
        from tkinter import messagebox

        ctx = dict(getattr(self, "_last_opt_ctx", {}) or {})
        if not ctx.get("symbol"):
            messagebox.showwarning("缺少复核信息", "请重新跑一次策略优化，再选组写入。")
            return
        self._append_log(f"复核第 {idx} 组参数（检验段 + 多品种）…")
        self._opt_started_at = time.monotonic()
        self._opt_progress_at = 0.0
        self._show_opt_progress(0, 1, f"复核第 {idx} 组", force=True)
        cfg = self._clone_cfg()
        bt_run = cfg.setdefault("backtest", {})
        bt_run["freeze_final_oos"] = bool(self.opt_freeze_var.get())
        try:
            bt_run["freeze_final_days"] = min(
                730, max(30, int(float(self.opt_freeze_days_var.get() or 180)))
            )
        except (TypeError, ValueError):
            bt_run["freeze_final_days"] = 180

        def _run() -> None:
            try:
                from src.backtest.optimizer import check_params_on_holdout

                checked = check_params_on_holdout(
                    cfg,
                    params=params,
                    symbol=str(ctx.get("symbol") or ""),
                    engine=str(ctx.get("engine") or ""),
                    depth=str(ctx.get("depth") or "标准"),
                    holdout_pct=ctx.get("holdout_pct"),
                    start_date=str(ctx.get("start_date") or "") or None,
                    end_date=str(ctx.get("end_date") or "") or None,
                    cross_symbols=list(ctx.get("cross_symbols") or []),
                    progress=lambda done, total, phase: self._post_to_main(
                        lambda d=done, t=total, p=phase: self._show_opt_progress(d, t, p)
                    ),
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("复核第 {} 组失败", idx)
                self._post_to_main(lambda: self._finish_opt_progress(ok=False))
                self._post_to_main(lambda m=str(e): messagebox.showerror("复核失败", m))
                return
            accepted = bool(checked.get("accepted"))
            self._post_to_main(lambda: self._finish_opt_progress(ok=accepted))
            if not accepted:
                reason = str(checked.get("reason") or "检验段没过")
                self._post_to_main(lambda: self._append_log(f"第 {idx} 组复核没过: {reason}"))
                self._post_to_main(
                    lambda r=reason: messagebox.showwarning(
                        f"第 {idx} 组不能写入",
                        r + "\n\n可以换一组，或到历史回测页手动验证。配置没有改。",
                    )
                )
                return
            self._post_to_main(lambda: self._append_log(f"第 {idx} 组复核通过，准备写入。"))
            live_switch = bool(getattr(self, "_last_evolve_better", False))
            save_page_preset = bool(self.opt_save_preset_var.get()) or live_switch
            self._post_to_main(
                lambda: self._write_optimized_params(
                    params,
                    group_index=idx,
                    score=trial.get("score"),
                    performance=trial.get("performance") or {},
                    confirm_extra=checked,
                    live_switch=live_switch,
                    save_page_preset=save_page_preset,
                )
            )

        threading.Thread(target=_run, daemon=True, name="opt-recheck").start()


    def _write_optimized_params(
        self,
        params: dict[str, Any] | None = None,
        *,
        group_index: int = 1,
        score: float | None = None,
        performance: dict[str, Any] | None = None,
        confirm_extra: dict[str, Any] | None = None,
        live_switch: bool = False,
        save_page_preset: bool = False,
    ) -> None:
        """
        把选中的参数写入磁盘配置，并先备份当前版本便于回溯。

        params 为空时用刚才优化的第 1 组（训练最优），此时必须后一段验收通过；
        选中其它组时，调用方已先复核通过，这里不再重复门槛。
        """
        from tkinter import messagebox

        use = dict(params or self._last_opt_params or {})
        if not use:
            messagebox.showwarning("还没有策略优化结果", "请先在本页点「开始策略优化」，完成后再写入。")
            return
        if params is None and not getattr(self, "_last_opt_accepted", False):
            reason = getattr(self, "_last_opt_reason", "") or "后一段验收没过。"
            messagebox.showwarning("这次不能写入", reason + "\n\n配置里的参数没有改。")
            return
        compared = bool(getattr(self, "_last_opt_compared", False))
        save_page_preset = bool(save_page_preset) or live_switch or bool(
            getattr(self, "opt_save_preset_var", None) and self.opt_save_preset_var.get()
        )
        if compared and not live_switch:
            extra = getattr(self, "_last_evolve_reason", "") or "对照没有全面优于现在这套。"
            if not messagebox.askyesno(
                "对照没有更好",
                extra
                + "\n\n仍可以按原来的方式写入，但建议先不要切实盘。\n确定继续写入？",
            ):
                return
        from src.backtest.optimizer import describe_params, describe_run_switches
        from src.backtest.param_history import strategy_display_name

        engine = getattr(self, "_last_opt_engine", None) or self.opt_engine_var.get()
        name = strategy_display_name(engine)
        switch_text = describe_run_switches(
            engine,
            flatten_at_close=bool(self.bt_flatten_var.get()),
        )
        picked = self.opt_engine_var.get()
        mismatch = ""
        if picked and picked != engine:
            mismatch = (
                f"下拉框现在选的是{strategy_display_name(picked)}，"
                f"这次写入的是刚才策略优化的{name}。\n\n"
            )
        # 日内实体优化可能带 timeframe / eth_minutes，写入后信号周期和 ETH 分钟数会跟着变。
        period_note = (
            "若参数里带有信号周期或 ETH 分钟数，这两项会一并写入；正在用的策略名和关注品种仍不改。\n"
            if engine in ("daily_body",)
            else "正在用的策略、品种、周期不会改。另外几套的参数也不会改。\n"
        )
        group_note = "" if group_index == 1 else f"这次写入的是第 {group_index} 组（不是训练最优那组）。\n"
        recheck_text = ""
        if confirm_extra:
            train = confirm_extra.get("train_performance") or {}
            hold = confirm_extra.get("holdout_performance") or {}
            recheck_text = (
                f"复核成绩  训练净赚 {float(train.get('net_pnl') or 0):.0f}，"
                f"检验净赚 {float(hold.get('net_pnl') or 0):.0f}"
                f"（{int(hold.get('trades') or 0)} 笔）\n"
                f"复核结论  {confirm_extra.get('reason') or '通过'}\n\n"
            )
        title = "写入并切实盘" if live_switch else "写入配置"
        archive_line = (
            "切之前会留下整页配置存档（配置页可一键应用），并备份这一套策略的参数版本。\n"
            if save_page_preset
            else "写入前会备份这一套策略的参数版本（下面「参数版本历史」）。这次不留整页配置存档。\n"
        )
        head = (
            f"把对照后挑出的{name}写入正在用的配置。\n"
            "默认本来不切；这次是因为对照全面更好，并且你点了确认。\n"
            if live_switch
            else f"写入刚才策略优化得到的{name}。\n"
        ) + archive_line
        if not messagebox.askyesno(
            title,
            f"{head}"
            f"{group_note}"
            f"{period_note}"
            "写入前会自动备份。\n\n"
            f"{mismatch}"
            f"{recheck_text}"
            f"这次打分用的开关会一起留下：\n{switch_text}\n\n"
            "开仓限制（权益、手数、保证金）这次不写。"
            "若历史回测页和配置页的数字不同，写入后历史回测页会变回配置页的数字。"
            "要留下历史回测页的策略、品种、周期和开仓限制，请先点「把本页参数写入配置页」。\n\n"
            f"即将写入的参数：\n{describe_params(use)}\n\n确定？"
            + (
                "\n\n切坏了：下面选旧版本点「回溯」（只还原这一套旋钮）；"
                "配置页可一键应用整页存档；要出厂点「重置当前策略为出厂默认」。"
                if save_page_preset or live_switch
                else "\n\n若效果不好：下面选旧版本点「回溯」，或重置为出厂默认。"
            ),
        ):
            return
        try:
            from src.backtest.optimizer import apply_params_to_config, apply_run_switches
            from src.backtest.param_history import add_version

            cfg = self._clone_cfg()
            sym = self.opt_symbol_var.get().strip()
            if save_page_preset:
                from src.ui.config_presets import add_preset

                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prefix = "切实盘前" if live_switch else "优化写入前"
                entry = add_preset(
                    self.root_dir,
                    cfg,
                    name=f"{prefix}-{sym or '未填'}-{name}-{stamp}",
                )
                self._refresh_config_presets(select_id=str(entry.get("id") or ""))
                self._append_log(f"已留下整页配置存档: {entry.get('name')}")
            # 1) 先备份写入前的配置。记在被优化的那一套下面。
            add_version(
                self.root_dir,
                cfg,
                note="写入优化前自动备份",
                source="backup",
                engine=engine,
                symbol=sym or None,
            )
            cfg = apply_run_switches(
                cfg,
                engine,
                flatten_at_close=bool(self.bt_flatten_var.get()),
            )

            new_cfg = apply_params_to_config(cfg, use, engine=engine)
            save_config(new_cfg)
            self.cfg = new_cfg
            self._reload_values_only()

            # 2) 再存「优化写入后」版本，方便对比
            perf = dict(performance or {})
            if score is None:
                try:
                    # 从结果框无法可靠解析；若刚优化完，score 可从文件读
                    import json
                    from pathlib import Path

                    rep = Path(self.root_dir) / "data" / "optimize_last.json"
                    if rep.exists():
                        payload = json.loads(rep.read_text(encoding="utf-8"))
                        score = payload.get("best_score")
                        perf = payload.get("best_performance") or {}
                except Exception:  # noqa: BLE001
                    pass
            add_version(
                self.root_dir,
                new_cfg,
                note=f"写入优化参数（第 {group_index} 组） {describe_params(use)}",
                source="optimize",
                score=float(score) if score is not None else None,
                performance=perf,
                opt_params=dict(use),
                engine=engine,
                symbol=sym or None,
            )
            # 历史列表跟着刚才优化的那一套走，避免写入后还停在另一个策略的名单上。
            self.opt_engine_var.set(engine)
            self._refresh_opt_history()
            self._append_log(
                f"{'已切实盘' if live_switch else '已写入优化参数'}（第 {group_index} 组，只改了 {name}，已备份历史）: {describe_params(use)}"
            )
            self._last_evolve_better = False
            self._sync_opt_write_button()
            preset_msg = (
                "配置页多了一份整页存档，可一键应用回去。\n"
                if save_page_preset
                else ""
            )
            messagebox.showinfo(
                "已切实盘" if live_switch else "已写入",
                f"已保存{name}的参数，以及这次打分用到的开关。\n"
                "正在用的策略、品种和周期没有改。开仓限制也没有改。\n"
                f"{preset_msg}"
                "这一套旋钮的备份在下方「参数版本历史」。\n"
                "若效果不好：选旧版本 →「回溯到选中版本」。"
                "要出厂：点「重置当前策略为出厂默认」。",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("写入优化参数失败")
            messagebox.showerror("写入失败", str(e))


    def _apply_history_version(self, vid: str, *, confirm_title: str, confirm_body: str) -> bool:
        """
        共用：把指定历史版本（含出厂默认）写回配置。
        返回是否真正执行了回溯。
        """
        from tkinter import messagebox

        if not messagebox.askyesno(confirm_title, confirm_body):
            return False
        try:
            from src.backtest.param_history import (
                FACTORY_DEFAULT_ID,
                add_version,
                apply_engine_snapshot,
                get_version,
                strategy_display_name,
            )

            entry = get_version(self.root_dir, vid)
            if not entry or not entry.get("snapshot"):
                messagebox.showerror("回溯失败", f"找不到版本 {vid}")
                return False

            engine = self.opt_engine_var.get()
            name = strategy_display_name(engine)
            cfg = self._clone_cfg()
            sym = self.opt_symbol_var.get().strip()
            add_version(
                self.root_dir,
                cfg,
                note="回溯/重置前自动备份",
                source="backup",
                engine=engine,
                symbol=sym or None,
            )
            new_cfg = apply_engine_snapshot(cfg, entry["snapshot"], engine)
            save_config(new_cfg)
            self.cfg = new_cfg
            self._reload_values_only()

            note = (
                f"已把{name}重置为出厂默认"
                if vid == FACTORY_DEFAULT_ID
                else f"已把{name}回溯到 {vid}"
            )
            add_version(
                self.root_dir,
                new_cfg,
                note=note,
                source="restore",
                score=entry.get("score"),
                performance=entry.get("performance") or {},
                opt_params=entry.get("opt_params") or {},
                engine=engine,
                symbol=sym or None,
            )
            self._refresh_opt_history()
            self._append_log(note)
            section = {
                "daily_body": "daily_body",
                "trend_follow": "trend_follow",
                "supertrend": "supertrend",
                "dual_ma": "dual_ma",
                "ma_cross": "dual_ma",
                "ema_swing": "ema_swing",
                "macd_swing": "ema_swing",
                "swing_trend": "swing_trend",
                "swing_box": "swing_box",
                "dual_donchian": "dual_donchian",
                "dual_fractal": "dual_fractal",
            }.get(engine, engine)
            shown = (new_cfg.get("strategy") or {}).get(section)
            if hasattr(self, "opt_result_box"):
                self._set_result_text(
                    self.opt_result_box,
                    f"{note}\n时间: {entry.get('time')}\n"
                    f"说明: {entry.get('note')}\n"
                    f"只改了: {name}\n\n"
                    f"这一套现在的参数:\n{shown}\n\n"
                    "正在用的策略、品种、周期，以及另外几套的参数都没有改。",
                )
            extra = ""
            if engine in (
                "daily_body",
                "dual_ma",
                "ma_cross",
            ):
                extra = "\n收盘清仓也会回到这一版。听这个总开关的策略会一起变。"
            messagebox.showinfo(
                "完成",
                f"只恢复了{name}的参数。\n"
                "正在用的策略、品种和周期没有改。"
                f"{extra}",
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception("回溯/重置失败")
            messagebox.showerror("失败", str(e))
            return False


    def _on_restore_opt_history(self) -> None:
        """把策略参数回溯到历史里选中的版本（含出厂默认）。"""
        from tkinter import messagebox

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label)
        if not vid:
            messagebox.showwarning("无法回溯", "请先选择一个有效的历史版本。")
            return
        self._apply_history_version(
            vid,
            confirm_title="确认回溯",
            confirm_body=(
                f"只把策略优化页当前选中的策略，改成这个版本里记下的参数：\n\n{label}\n\n"
                "正在用的策略、品种、周期，以及另外几套的参数都不会动。\n"
                "回溯前会再自动备份一次。确定？"
            ),
        )


    def _on_reset_factory_params(self) -> None:
        """一键重置为出厂默认（优化怎么都不好时用）。"""
        from src.backtest.param_history import FACTORY_DEFAULT_ID

        self._apply_history_version(
            FACTORY_DEFAULT_ID,
            confirm_title="重置当前策略为出厂默认",
            confirm_body=(
                "只把策略优化页当前选中的这一套策略，恢复成程序最初的参数。\n"
                "另外几套策略保持现在的数。\n"
                "正在用的策略、品种、周期、账号和同花顺坐标都不会改。\n\n"
                "重置前会自动备份当前参数。确定？"
            ),
        )


    def _on_export_params_excel(self) -> None:
        """把程序内最近最多30条参数历史追加到「优化参数」表，并把最近一次优化追加到「优化结果」表。"""
        from tkinter import filedialog, messagebox

        try:
            from src.backtest.param_excel import default_excel_path, export_append_excel
            from src.export.bundle import export_optimize

            default_path = default_excel_path(self.root_dir)
            path = filedialog.asksaveasfilename(
                title="导出/追加策略优化参数与优化结果到 Excel",
                initialdir=str(default_path.parent),
                initialfile=default_path.name,
                defaultextension=".xlsx",
                filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
            )
            if not path:
                return
            out, added, total = export_append_excel(self.root_dir, path, include_factory=True)
            opt_msg = ""
            try:
                # 参数表若因 Excel 占用改写到另一份，优化结果必须跟过去。
                out, opt_added, opt_total = export_optimize(self.root_dir, out)
                opt_msg = f"\n「优化结果」表：本次新追加 {opt_added} 行（含全部候选组），合计 {opt_total} 行。"
            except ValueError:
                opt_msg = "\n「优化结果」表：还没有跑过策略优化，这次没写。"
            msg = (
                f"已写入「优化参数」表{opt_msg}\n\n"
                f"文件：{out}\n"
                f"参数版本本次新追加：{added} 条\n"
                f"该表合计：{total} 条\n\n"
                f"「优化结果」表第一列「测试时间」是这次优化跑完的时刻，用来分清是哪一次。\n"
                f"同一个文件里还有交易记录、资金变动记录、复盘、回测摘要、回测成交、回测信号等表。\n"
                f"已存在的版本ID / 同一次优化不会重复写入。"
                f"{self._export_saved_note(path, out)}"
            )
            self._append_log(f"参数与优化结果已导出追加到Excel: {out} (参数+{added}, 共{total})")
            if hasattr(self, "opt_result_box"):
                self._set_result_text(self.opt_result_box, msg)
            messagebox.showinfo("导出完成", msg)
        except ImportError:
            messagebox.showerror(
                "缺少组件",
                "需要 openpyxl 才能写 Excel。\n请在项目目录执行：\n  pip install openpyxl",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("导出Excel失败")
            chosen = locals().get("path", "")
            messagebox.showerror("导出失败", self._export_fail_text(str(chosen), e))


    def _on_import_params_excel(self) -> None:
        """从本地 Excel 导入参数版本，合并进程序历史（仍只留最近30条）。"""
        from tkinter import filedialog, messagebox

        try:
            from src.backtest.param_excel import default_excel_path, import_from_excel

            default_path = default_excel_path(self.root_dir)
            path = filedialog.askopenfilename(
                title="从 Excel 导入策略优化参数",
                initialdir=str(default_path.parent),
                initialfile=default_path.name if default_path.exists() else "",
                filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
            )
            if not path:
                return
            if not messagebox.askyesno(
                "确认导入",
                "将从 Excel 读入参数版本并合并到程序历史。\n"
                "程序内仍只保留最近 30 条（多的请继续用 Excel 存档）。\n\n"
                "导入不会自动改当前正在用的配置；需要的话再选版本「回溯」。\n\n确定导入？",
            ):
                return
            n_in, n_keep = import_from_excel(self.root_dir, path)
            self._refresh_opt_history()
            msg = (
                f"导入完成。\n\n"
                f"Excel 有效行：{n_in}\n"
                f"程序内现保留：{n_keep} 条\n\n"
                f"请在上方历史列表选择版本，点「回溯到选中版本」才会改当前配置。"
            )
            self._append_log(f"已从Excel导入参数: {path} | 有效{n_in} | 程序内{n_keep}")
            if hasattr(self, "opt_result_box"):
                self._set_result_text(self.opt_result_box, msg)
            messagebox.showinfo("导入完成", msg)
        except ImportError:
            messagebox.showerror(
                "缺少组件",
                "需要 openpyxl 才能读 Excel。\n请在项目目录执行：\n  pip install openpyxl",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("导入Excel失败")
            messagebox.showerror("导入失败", str(e))

