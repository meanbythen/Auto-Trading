"""Account analysis, cash operations, fund views, and chart rendering."""

from __future__ import annotations

from loguru import logger

from datetime import datetime
from typing import Any

from src.ui import theme as T
from src.utils.trading_day import futures_trading_day
import customtkinter as ctk
import threading


class AccountTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_account_tab(self, tab: ctk.CTkFrame) -> None:
        """
        账户分析：左边改本金和出入金，右边看程序信号算出来的成绩。

        原理：开平仓由程序在真正记账时自动写入。这里只允许改初始资金、入金、出金和每日结算。
        """
        tab.grid_columnconfigure(0, weight=0, minsize=430)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent", width=420)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        ctk.CTkLabel(left, text="账户分析", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        self.ac_balance_label = ctk.CTkLabel(
            left, text="当前余额  —", font=T.FONT_TITLE, text_color=T.ACCENT, anchor="w"
        )
        self.ac_balance_label.pack(anchor="w", padx=4, pady=(0, 6))
        switch_box = self._card(left)
        switch_row = ctk.CTkFrame(switch_box, fg_color="transparent")
        switch_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            switch_row, text="当前账", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=80, anchor="w"
        ).pack(side="left", padx=(0, 8))
        from src.account.workspace import labels as account_labels

        account_menu = self._option(switch_row, self.ac_account_var, account_labels(self.root_dir), width=280)
        account_menu.pack(side="left")
        account_menu.configure(command=self._on_account_switch)
        self.ac_account_hint = ctk.CTkLabel(
            switch_box,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        )
        self.ac_account_hint.pack(anchor="w", pady=(4, 0))

        self.ac_ths_panel = self._card(left)
        self._account_action_row(
            self.ac_ths_panel, "初始资金", self.ac_capital_var, [("初始化", self._on_signal_initial, True)]
        )
        self._account_action_row(
            self.ac_ths_panel,
            "金额",
            self.ac_cash_var,
            [("入金", self._on_signal_deposit, False), ("出金", self._on_signal_withdraw, False)],
        )
        self._account_action_row(
            self.ac_ths_panel, "实际余额", self.ac_settle_var, [("每日结算", self._on_signal_settle, True)]
        )
        ctk.CTkLabel(
            self.ac_ths_panel,
            text="GUI 点单账可以手改。初始化会先把旧账存档，再从现在另记一本。入金、出金填正数。红字是赚，绿字是亏。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self.ac_ctp_panel = self._card(left)
        ctk.CTkLabel(
            self.ac_ctp_panel,
            text="CTP 柜台资金（与交易台「资金」页同一份回报）",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(0, 4))
        self.ac_ctp_user_label = ctk.CTkLabel(
            self.ac_ctp_panel, text="资金账号  —", font=T.FONT_BODY, text_color=T.ACCENT, anchor="w"
        )
        self.ac_ctp_user_label.pack(anchor="w")
        self.ac_ctp_time_label = ctk.CTkLabel(
            self.ac_ctp_panel, text="还没有查询记录", font=T.FONT_SMALL, text_color=T.TEXT_DIM, anchor="w"
        )
        self.ac_ctp_time_label.pack(anchor="w", pady=(0, 6))
        fund_grid = ctk.CTkFrame(self.ac_ctp_panel, fg_color="transparent")
        fund_grid.pack(fill="x")
        self._ac_fund_values: dict[str, Any] = {}
        from src.trader.ctp_display import account_pairs

        for index, (title, _value) in enumerate(account_pairs({})):
            row, col = divmod(index, 2)
            ctk.CTkLabel(
                fund_grid, text=title, font=T.FONT_SMALL, text_color=T.TEXT_DIM, width=88, anchor="e"
            ).grid(row=row, column=col * 2, sticky="e", padx=(0, 6), pady=2)
            value_label = ctk.CTkLabel(
                fund_grid, text="—", font=T.FONT_SMALL, text_color=T.TEXT, width=110, anchor="w"
            )
            value_label.grid(row=row, column=col * 2 + 1, sticky="w", pady=2)
            self._ac_fund_values[title] = value_label
        ctk.CTkButton(
            self.ac_ctp_panel,
            text="从柜台刷新资金",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_ctp_refresh_funds,
        ).pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(
            self.ac_ctp_panel,
            text="数字原样来自期货公司，本地只保存、不改。GUI 点单请换到上面的 GUI 账。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self.ac_common_refresh = ctk.CTkButton(
            left, text="刷新", height=32, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
            border_width=1, border_color=T.BORDER, text_color=T.TEXT, command=self._refresh_signal_view,
        )
        self.ac_common_refresh.pack(fill="x", padx=4, pady=(8, 4))
        # 这两张表和「导出全部」里的「交易记录」「资金变动记录」是同一份内容，只单独写出一张。
        ctk.CTkButton(
            left, text="导出交易记录", height=32, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
            border_width=1, border_color=T.BORDER, text_color=T.TEXT,
            command=lambda: self._on_export_account_part("trades"),
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            left, text="导出资金变动记录", height=32, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
            border_width=1, border_color=T.BORDER, text_color=T.TEXT,
            command=lambda: self._on_export_account_part("changes"),
        ).pack(fill="x", padx=4, pady=(0, 8))
        self.ac_ths_manual = ctk.CTkButton(
            left,
            text="手工单（只改程序账本）",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_manual_trade,
        )
        self.ac_ths_manual.pack(fill="x", padx=4, pady=(0, 8))
        self._sync_account_mode()

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(right, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        head = ctk.CTkFrame(scroll, fg_color="transparent")
        head.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(head, text="账户统计", font=T.FONT_TITLE, text_color=T.TEXT).pack(side="left")
        spans = ctk.CTkFrame(head, fg_color="transparent")
        spans.pack(side="right")
        self._ac_span_buttons = {}
        for name in ("近1周", "近1月", "近3月", "近6月", "全部"):
            button = ctk.CTkButton(
                spans,
                text=name,
                width=58,
                height=26,
                fg_color=T.ACCENT if name == "全部" else T.BG,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON if name == "全部" else T.TEXT,
                command=lambda choice=name: self._on_account_span(choice),
            )
            button.pack(side="left", padx=2)
            self._ac_span_buttons[name] = button

        stats = ctk.CTkFrame(scroll, fg_color="transparent")
        stats.pack(fill="x", pady=(0, 6))
        self.ac_win_label = self._account_stat_card(stats, "胜率")
        self.ac_factor_label = self._account_stat_card(stats, "盈利因子")
        self.ac_dd_label = self._account_stat_card(stats, "最大回撤")
        self.ac_use_label = self._account_stat_card(stats, "资金使用率")

        chart_head = ctk.CTkFrame(scroll, fg_color="transparent")
        chart_head.pack(fill="x")
        self._ac_chart_buttons = {}
        for name in ("权益", "每日盈亏"):
            button = ctk.CTkButton(
                chart_head,
                text=name,
                width=72,
                height=26,
                fg_color=T.ACCENT if name == "权益" else T.BG,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON if name == "权益" else T.TEXT,
                command=lambda choice=name: self._on_account_chart(choice),
            )
            button.pack(side="left", padx=(0, 6))
            self._ac_chart_buttons[name] = button
        import tkinter as tk

        self.ac_chart = tk.Canvas(
            scroll, height=150, bg=T.BG, highlightthickness=0, bd=0
        )
        self.ac_chart.pack(fill="x", pady=(4, 8))

        ctk.CTkLabel(scroll, text="分品种", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w")
        self.ac_symbol_label = ctk.CTkLabel(
            scroll,
            text="还没有平仓。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            justify="left",
            anchor="w",
        )
        self.ac_symbol_label.pack(anchor="w", fill="x", pady=(2, 8))

        cal_head = ctk.CTkFrame(scroll, fg_color="transparent")
        cal_head.pack(fill="x")
        ctk.CTkButton(
            cal_head, text="上月", width=52, height=26, fg_color=T.BG, hover_color=T.BORDER,
            text_color=T.TEXT, command=lambda: self._shift_account_month(-1),
        ).pack(side="left")
        self.ac_cal_label = ctk.CTkLabel(cal_head, text="", font=T.FONT_BODY, text_color=T.TEXT)
        self.ac_cal_label.pack(side="left", padx=8)
        ctk.CTkButton(
            cal_head, text="下月", width=52, height=26, fg_color=T.BG, hover_color=T.BORDER,
            text_color=T.TEXT, command=lambda: self._shift_account_month(1),
        ).pack(side="left")
        self.ac_month_pnl = ctk.CTkLabel(cal_head, text="", font=T.FONT_SMALL, text_color=T.TEXT_DIM)
        self.ac_month_pnl.pack(side="right")
        self.ac_calendar = tk.Canvas(scroll, height=210, bg=T.BG, highlightthickness=0, bd=0)
        self.ac_calendar.pack(fill="x", pady=(4, 8))

        self.ac_ledger_title = ctk.CTkLabel(
            scroll, text="资金变动记录", font=T.FONT_BODY, text_color=T.TEXT
        )
        self.ac_ledger_title.pack(anchor="w")
        self.ac_result_box = ctk.CTkTextbox(
            scroll, height=280, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.ac_result_box.pack(fill="x", pady=(4, 8))
        self._refresh_signal_view()


    def _sync_account_mode(self) -> None:
        """GUI 账显示手改工具；CTP 账显示柜台资金表。两套界面不同时出现。"""
        from src.account.workspace import is_ctp_book, load_active_id

        ctp = is_ctp_book(load_active_id(self.root_dir))
        ths_inner = getattr(self, "ac_ths_panel", None)
        ctp_inner = getattr(self, "ac_ctp_panel", None)
        if ths_inner is None or ctp_inner is None:
            return
        ths_card = ths_inner.master
        ctp_card = ctp_inner.master
        refresh = getattr(self, "ac_common_refresh", None)
        if ctp:
            ths_card.pack_forget()
            if hasattr(self, "ac_ths_manual"):
                self.ac_ths_manual.pack_forget()
            if refresh is not None:
                ctp_card.pack(fill="x", pady=(0, 4), before=refresh)
            else:
                ctp_card.pack(fill="x", pady=(0, 4))
        else:
            ctp_card.pack_forget()
            if refresh is not None:
                ths_card.pack(fill="x", pady=(0, 4), before=refresh)
            else:
                ths_card.pack(fill="x", pady=(0, 4))
            if hasattr(self, "ac_ths_manual"):
                self.ac_ths_manual.pack(fill="x", padx=4, pady=(0, 8))


    def _paint_ctp_fund_panel(self, book) -> None:
        """把已保存的柜台资金画到账户分析，字段和交易台资金页一致。"""
        from src.trader.ctp_display import account_pairs

        snap = (book or {}).get("ctp_snapshot") if isinstance(book, dict) else None
        if not isinstance(snap, dict):
            snap = {}
        user = str(snap.get("account_id") or snap.get("user_id") or "").strip() or "—"
        if hasattr(self, "ac_ctp_user_label"):
            self.ac_ctp_user_label.configure(text=f"资金账号  {user}")
        if hasattr(self, "ac_ctp_time_label"):
            stamp = str(snap.get("time") or "").strip()
            self.ac_ctp_time_label.configure(text=f"更新于 {stamp}" if stamp else "还没有查询记录")
        values = getattr(self, "_ac_fund_values", None)
        if not isinstance(values, dict) or not values:
            return
        for title, value in account_pairs(snap if snap else None):
            label = values.get(title)
            if label is not None:
                label.configure(text=value)


    def _paint_account_hint(self) -> None:
        """账户分析左边说明现在记的是哪一本，以及交易软件该开哪边。"""
        from src.account.signal_book import load_book
        from src.account.workspace import display_label, profile_of

        info = profile_of(root=self.root_dir)
        shown_label = display_label(info["id"], self.root_dir)
        extra = ""
        book = load_book(self.root_dir)
        snap = (book or {}).get("ctp_snapshot") if isinstance(book, dict) else None
        if isinstance(snap, dict) and snap.get("time"):
            extra = (
                f"\n柜台 {snap.get('account_id') or snap.get('user_id') or ''} "
                f"动态权益 {float(snap.get('balance') or 0):.2f}，"
                f"可用 {float(snap.get('available') or 0):.2f}，"
                f"更新于 {snap.get('time')}。"
            )
        self._sync_account_mode()
        self._paint_ctp_fund_panel(book)
        hint = getattr(self, "ac_account_hint", None)
        if hint is not None:
            hint.configure(text=info["hint"] + extra)
        if hasattr(self, "ac_account_var"):
            shown = self.ac_account_var.get()
            if shown != shown_label:
                busy = self._account_switch_busy
                self._account_switch_busy = True
                try:
                    self.ac_account_var.set(shown_label)
                finally:
                    self._account_switch_busy = busy


    def _apply_ctp_book_align(self, note: str) -> None:
        """配置页切仿真/真实后，账户分析下拉框跟到同一本账。"""
        if not note:
            return
        from src.account.workspace import display_label, load_active_id

        if hasattr(self, "ac_account_var"):
            busy = bool(getattr(self, "_account_switch_busy", False))
            self._account_switch_busy = True
            try:
                self.ac_account_var.set(display_label(load_active_id(self.root_dir), self.root_dir))
            finally:
                self._account_switch_busy = busy
        self._signal_book_mtime = 0.0
        if hasattr(self, "_refresh_signal_view"):
            self._refresh_signal_view()
        if hasattr(self, "_refresh_status_cards"):
            self._refresh_status_cards()
        self._append_log(note)

    def _on_account_switch(self, choice: str) -> None:
        """
        换成另一本账。常驻必须先停，否则内存里的仓还是上一本的。

        只改当前记号。实盘账和模拟账的文件互不覆盖，也不会把成交抄到另一本。
        """
        from tkinter import messagebox

        from src.account.signal_book import load_book, multiplier_map, shown_equity
        from src.account.workspace import (
            display_label,
            id_from_label,
            is_ctp_book,
            load_active_id,
            profile_of,
            set_active_id,
        )

        if self._account_switch_busy:
            return
        wanted = id_from_label(choice)
        current = load_active_id(self.root_dir)
        if wanted == current:
            self._paint_account_hint()
            return
        if self.worker.running:
            messagebox.showwarning(
                "先停常驻",
                "换账会换资金和持仓记录。请先在运行页点「停止常驻」，再换到另一本账。",
            )
            self._account_switch_busy = True
            try:
                self.ac_account_var.set(display_label(current, self.root_dir))
            finally:
                self._account_switch_busy = False
            return
        target = profile_of(wanted)
        shown = display_label(wanted, self.root_dir)
        if not messagebox.askyesno(
            "换账",
            f"确定换成「{shown}」吗？\n\n"
            f"{target['hint']}\n\n"
            "另一本账的成交和持仓原样留着，不会被这本改掉。",
        ):
            self._account_switch_busy = True
            try:
                self.ac_account_var.set(display_label(current, self.root_dir))
            finally:
                self._account_switch_busy = False
            return
        set_active_id(self.root_dir, wanted)
        self._signal_book_mtime = 0.0
        self._paint_account_hint()
        self._refresh_signal_view()
        book = load_book(self.root_dir)
        if book is not None:
            self._apply_book_balance(shown_equity(book, multiplier_map(self.cfg)))
        self._refresh_status_cards()
        self._append_log(f"账户分析已换成 {shown}")
        if wanted == "paper":
            messagebox.showinfo(
                "GUI模拟账",
                "程序这边已经换成模拟账。\n"
                "请到交易软件左上角打开「模拟交易」，再点启动常驻。\n"
                "这样点单走模拟盘，不会动真实资金，也不会改实盘账。\n"
                "这本账的余额仍要手工记入金、出金或每日结算。",
            )
        elif is_ctp_book(wanted):
            messagebox.showinfo(
                shown,
                "这本账的权益以 CTP 柜台查询为准。\n"
                "请打开交易台，用对应账号登录后点「刷新持仓」，\n"
                "或在本页点「从柜台刷新资金」。\n"
                "GUI 登录号不要填进 CTP 资金账号。",
            )


    def _on_ctp_refresh_funds(self) -> None:
        """向已经登录的柜台再查一次资金，写入当前这本 CTP 账。"""
        from tkinter import messagebox

        from src.account.ctp_sync import can_pull_from_trader
        from src.account.workspace import is_ctp_book, load_active_id

        aid = load_active_id(self.root_dir)
        if not is_ctp_book(aid):
            messagebox.showinfo(
                "这本账不接柜台",
                "GUI模拟账仍要手工改余额。\n"
                "GUI实盘账仍是程序开平仓加手工结算。\n"
                "请换到「CTP仿真」或「CTP真实」再刷新。",
            )
            return
        trader = None
        probe = getattr(self, "_ctp_probe", None)
        if probe is not None and getattr(probe, "winfo_exists", lambda: False)() and getattr(probe, "trader", None):
            trader = probe.trader
        worker = getattr(self, "worker", None)
        bot = getattr(worker, "bot", None) if worker is not None else None
        if trader is None and bot is not None:
            trader = getattr(bot, "trader", None)
        if not can_pull_from_trader(trader, aid):
            messagebox.showwarning(
                "还没连上对应柜台",
                "请先打开交易台，用这一本账对应的账号登录，再点「刷新持仓」或本按钮。",
            )
            return

        def work() -> None:
            ok, text = trader.fetch_account_snapshot()
            self._post_to_main(lambda: self._append_log(text if ok else ("查询失败：" + text)))
            self._post_to_main(self._paint_account_hint)
            self._post_to_main(self._refresh_signal_view)

        threading.Thread(target=work, daemon=True).start()
        self._append_log("正在向柜台查询资金。")


    def _signal_book_or_new(self):
        """读信号账户。还没有就开始一本，本金用左边的初始资金，没有就用配置里的账户权益。"""
        from src.account.signal_book import load_book, new_book
        from src.version import read_version

        book = load_book(self.root_dir)
        if book is not None:
            return book
        return new_book(self._signal_initial_amount(), version=read_version())


    def _signal_initial_amount(self) -> float:
        """初始资金输入框。空着时用配置页的账户权益。"""
        text = self.ac_capital_var.get().strip()
        if not text:
            text = self.capital_var.get().strip()
        amount = float(text)
        if amount < 0:
            raise ValueError("初始资金不能是负数")
        return amount


    def _signal_positive_amount(self, raw: str, label: str) -> float:
        """入金、出金、结算金额。必须是数，入金和出金还要大于 0。"""
        amount = float(str(raw).strip())
        if label != "实际余额" and amount <= 0:
            raise ValueError(f"{label}要大于 0")
        return amount


    def _commit_signal_book(self, book, note: str) -> None:
        """保存账本，并把新余额写进配置和正在跑的程序。"""
        from src.account.signal_book import save_manual, write_capital_to_config

        balance = save_manual(self.root_dir, book)
        write_capital_to_config(balance)
        self._apply_book_balance(balance)
        self._refresh_signal_view()
        self._append_log(note)


    def _apply_book_balance(self, balance: float) -> None:
        """
        让配置页上的账户权益显示这笔余额。

        配置页不能手改这个数。正在跑的程序下一笔按这个余额算手数。
        常驻已经连上 CTP 时，手数仍跟期货公司数字，不用账本里估的成交盈亏去改。
        运行中的手数和权益只经 bot.set_capital 改，和下单共用交易锁。
        """
        target = float(balance)
        bot = getattr(self.worker, "bot", None)
        if bot is not None:
            raw = getattr(getattr(bot, "trader", None), "last_broker_equity", None)
            try:
                broker = float(raw) if raw is not None else 0.0
            except (TypeError, ValueError):
                broker = 0.0
            if broker > 0:
                target = broker
            bot.set_capital(target)
            # bot.cfg 与配置页通常是同一个字典；不是同一个时，配置页自己留一份。
            if self.cfg is not getattr(bot, "cfg", None):
                self.cfg.setdefault("risk", {})["capital"] = target
        else:
            self.cfg.setdefault("risk", {})["capital"] = target
        self.capital_var.set(str(int(target)) if target == int(target) else f"{target:.2f}")


    def _on_signal_initial(self) -> None:
        """
        记下初始资金，并把它当作新账期的起点。

        原来的账先整份存进 data/signal_periods，再另建一本新账。
        存档没成功就不会清掉当前账。交易软件里的持仓不动。
        """
        from tkinter import messagebox

        from src.account.signal_book import load_book, start_new_period
        from src.version import read_version

        try:
            amount = self._signal_initial_amount()
        except ValueError:
            messagebox.showerror("还不能记", "初始资金请填一个大于等于 0 的数。")
            return
        if load_book(self.root_dir) is not None:
            if not messagebox.askyesno(
                "开始新账期",
                "记下初始资金会先把现在这本账整份存档，再另建一本新账。\n\n"
                "存档在 data/signal_periods，里面仍有原来的交易、入金、出金、结算和盈亏。\n"
                "新账期用新的存档，从现在按时间重新统计。\n"
                f"余额会改成 {amount:g} 元，配置页的账户权益一起改。\n\n"
                "交易软件里的真实持仓不会被平掉。\n\n确定开始？",
            ):
                return
        try:
            book, archived = start_new_period(self.root_dir, amount, version=read_version())
        except OSError as exc:
            messagebox.showerror("没能存档", f"上一本账还没备份成功，所以没有开始新账期。\n{exc}")
            return
        shown = str(int(amount)) if amount == int(amount) else f"{amount:.2f}"
        self.ac_capital_var.set(shown)
        note = f"已开始新账期，初始资金 {amount:g} 元。新存档 data/signal_periods/{book.get('period_file')}"
        if archived is not None:
            note += f"。上一账期已存档 {archived.name}"
        self._commit_signal_book(book, note)


    def _on_signal_deposit(self) -> None:
        """按你填的正数记一笔入金。"""
        self._on_signal_cash("deposit", "入金")


    def _on_signal_withdraw(self) -> None:
        """按你填的正数记一笔出金。余额不够时先问一句。"""
        self._on_signal_cash("withdraw", "出金")


    def _on_signal_cash(self, kind: str, label: str) -> None:
        """入金和出金共用。账本还没有就先用当前初始资金建一本。"""
        from tkinter import messagebox

        from src.account.signal_book import add_cash, balance_of
        from src.account.workspace import is_ctp_book, load_active_id

        if is_ctp_book(load_active_id(self.root_dir)):
            messagebox.showinfo("这本账用柜台资金", "CTP仿真和CTP真实的余额请点「从柜台刷新资金」，不要手工记入金出金。")
            return

        try:
            amount = self._signal_positive_amount(self.ac_cash_var.get(), label)
            book = self._signal_book_or_new()
            if kind == "withdraw" and amount > balance_of(book) + 0.001:
                if not messagebox.askyesno("出金", "这笔出金比当前余额还大，余额会变成负数。仍要记吗？"):
                    return
            add_cash(book, kind, amount)
        except ValueError:
            messagebox.showerror("还不能记", f"{label}请填一个大于 0 的数。")
            return
        self.ac_cash_var.set("")
        self._commit_signal_book(book, f"信号账户已记{label} {amount:g} 元")


    def _on_signal_settle(self) -> None:
        """每日结算：差额单独记一笔，开平仓价格不动。"""
        from tkinter import messagebox

        from src.account.signal_book import settle_to
        from src.account.workspace import is_ctp_book, load_active_id

        if is_ctp_book(load_active_id(self.root_dir)):
            messagebox.showinfo("这本账用柜台资金", "CTP仿真和CTP真实请点「从柜台刷新资金」，不要用每日结算手改。")
            return

        try:
            target = float(self.ac_settle_var.get().strip())
        except ValueError:
            messagebox.showerror("还不能结算", "实际余额请填一个数。")
            return
        book = self._signal_book_or_new()
        delta = settle_to(book, target)
        if abs(delta) < 0.005:
            messagebox.showinfo("不用调整", "当前余额已经和你填的数一样。")
            self._refresh_signal_view()
            return
        self.ac_settle_var.set("")
        self._commit_signal_book(book, f"每日结算已把余额调到 {target:g} 元，差额 {delta:g} 元")


    def _account_action_row(self, parent, label: str, var, buttons: list[tuple[str, Any, bool]]) -> None:
        """账户分析里的一行：名称、金额框、按钮并排，不再各占一排。"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_SMALL, text_color=T.TEXT_DIM, width=64, anchor="w"
        ).pack(side="left")
        ctk.CTkEntry(
            row,
            textvariable=var,
            width=96,
            height=30,
            fg_color=T.BG,
            border_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 6))
        for text, command, primary in buttons:
            ctk.CTkButton(
                row,
                text=text,
                width=72,
                height=30,
                fg_color=T.ACCENT if primary else T.BG,
                hover_color=T.ACCENT_HOVER if primary else T.BORDER,
                text_color=T.ACCENT_ON if primary else T.TEXT,
                command=command,
            ).pack(side="left", padx=(0, 4))


    def _account_stat_card(self, parent, title: str) -> ctk.CTkLabel:
        """右边四个汇总数字中的一个。数字以后由刷新填上。"""
        card = ctk.CTkFrame(parent, fg_color=T.BG, corner_radius=8)
        card.pack(side="left", expand=True, fill="x", padx=3)
        ctk.CTkLabel(card, text=title, font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(anchor="w", padx=8, pady=(6, 0))
        value = ctk.CTkLabel(card, text="—", font=T.FONT_TITLE, text_color=T.TEXT)
        value.pack(anchor="w", padx=8, pady=(0, 6))
        return value


    def _on_account_span(self, name: str) -> None:
        """换时间段后，胜率、回撤、品种和权益图都按这一段重算。"""
        self.ac_span_var.set(name)
        for key, button in self._ac_span_buttons.items():
            selected = key == name
            button.configure(
                fg_color=T.ACCENT if selected else T.BG,
                text_color=T.ACCENT_ON if selected else T.TEXT,
            )
        self._refresh_signal_view()


    def _on_account_chart(self, name: str) -> None:
        """权益看余额曲线，每日盈亏看每天赚亏的柱子。"""
        self._ac_chart = name
        for key, button in self._ac_chart_buttons.items():
            selected = key == name
            button.configure(
                fg_color=T.ACCENT if selected else T.BG,
                text_color=T.ACCENT_ON if selected else T.TEXT,
            )
        self._draw_account_chart()


    def _account_margin_settings(self) -> tuple[dict[str, float], dict[str, float], float]:
        """读配置里的保证金比例和兜底金额。"""
        risk = self.cfg.get("risk") or {}
        try:
            default_margin = float(risk.get("margin_per_lot") or 0)
        except (TypeError, ValueError):
            default_margin = 0.0
        margins: dict[str, float] = {}
        rates: dict[str, float] = {}
        for key, info in (risk.get("contracts") or {}).items():
            if not isinstance(info, dict):
                continue
            code = str(key).lower()
            try:
                margins[code] = float(info.get("margin") or 0)
            except (TypeError, ValueError):
                pass
            try:
                rates[code] = float(info.get("margin_rate_pct") or 0)
            except (TypeError, ValueError):
                pass
        return margins, rates, default_margin


    def _paint_account_stats(self, book) -> None:
        """按当前时间段重算右边的数字、图、品种和日历。"""
        from src.account.signal_stats import (
            analyze_book,
            format_factor,
            format_utilization,
            format_win_rate,
            span_bounds,
        )

        if not hasattr(self, "ac_win_label"):
            return
        start, end = span_bounds(self.ac_span_var.get() or "全部", futures_trading_day())
        margins, rates, default_margin = self._account_margin_settings()
        report = analyze_book(
            book or {"trades": [], "changes": []},
            start=start,
            end=end,
            margins=margins,
            default_margin=default_margin,
            margin_rates=rates,
        )
        self._ac_report = report
        if book and str(book.get("funding") or "") == "ctp":
            from src.account.signal_book import ctp_history_equity

            series = []
            for stamp, value in ctp_history_equity(book):
                try:
                    moment = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    continue
                if start is not None and moment.date() < start:
                    continue
                if moment.date() > end:
                    continue
                series.append((moment, value))
            if series:
                report.equity = series
                peak = series[0][1]
                max_dd = 0.0
                for _moment, point in series:
                    peak = max(peak, point)
                    max_dd = max(max_dd, peak - point)
                report.account.max_drawdown = max_dd
                snap = book.get("ctp_snapshot") if isinstance(book.get("ctp_snapshot"), dict) else {}
                try:
                    usage = float(snap.get("usage") or 0)
                except (TypeError, ValueError):
                    usage = 0.0
                if usage > 0:
                    report.account.utilization = usage
        stat = report.account
        self.ac_win_label.configure(text=format_win_rate(stat.win_rate))
        self.ac_factor_label.configure(text=format_factor(stat.profit_factor))
        self.ac_dd_label.configure(text=f"{stat.max_drawdown:.2f}")
        self.ac_use_label.configure(text=format_utilization(stat.utilization))
        if report.symbols:
            lines = []
            for item in report.symbols:
                one = item.stat
                lines.append(
                    f"{item.symbol}   {one.closed}笔   胜率 {format_win_rate(one.win_rate)}"
                    f"   盈利因子 {format_factor(one.profit_factor)}"
                    f"   盈亏 {one.net_pnl:+.2f}"
                    f"   回撤 {one.max_drawdown:.2f}"
                    f"   使用率 {format_utilization(one.utilization)}"
                )
            self.ac_symbol_label.configure(text="\n".join(lines), text_color=T.TEXT)
        else:
            self.ac_symbol_label.configure(text="这段时间还没有平仓。", text_color=T.TEXT_DIM)
        self._draw_account_chart()
        self._draw_account_calendar()


    def _draw_account_chart(self) -> None:
        """权益画成一条线，每日盈亏画成柱子。红是赚，绿是亏。"""
        canvas = getattr(self, "ac_chart", None)
        report = self._ac_report
        if canvas is None or report is None:
            return
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 520)
        height = 150
        canvas.delete("all")
        if self._ac_chart == "每日盈亏":
            points = sorted(report.daily_pnl.items())
            if self.ac_span_var.get() != "全部":
                from src.account.signal_stats import span_bounds

                start, end = span_bounds(self.ac_span_var.get(), futures_trading_day())
                points = [(day, value) for day, value in points if (start is None or day >= start) and day <= end]
            self._draw_pnl_bars(canvas, width, height, points)
            return
        self._draw_equity_line(canvas, width, height, report.equity)


    def _draw_equity_line(self, canvas, width: int, height: int, points) -> None:
        """把余额按时间连成一条线。"""
        if len(points) < 1:
            canvas.create_text(16, height / 2, anchor="w", fill=T.TEXT_DIM, text="还没有资金变动")
            return
        values = [value for _moment, value in points]
        # 标签用真实高低。线太平时只把画布上下撑开 1 元，避免一个点贴边，不改显示的数字。
        shown_low = min(values)
        shown_high = max(values)
        low = shown_low
        high = shown_high
        if high - low < 0.01:
            low -= 1
            high += 1
        pad = 16
        usable_w = max(width - pad * 2, 10)
        usable_h = height - pad * 2

        def xy(index: int, value: float) -> tuple[float, float]:
            x = pad + usable_w * (index / max(len(points) - 1, 1))
            y = pad + usable_h * (1 - (value - low) / (high - low))
            return x, y

        coords = []
        for index, (_moment, value) in enumerate(points):
            x, y = xy(index, value)
            coords.extend((x, y))
        if len(coords) >= 4:
            canvas.create_line(*coords, fill=T.ACCENT, width=2)
        else:
            canvas.create_oval(coords[0] - 3, coords[1] - 3, coords[0] + 3, coords[1] + 3, fill=T.ACCENT, outline="")
        canvas.create_text(pad, 8, anchor="nw", fill=T.TEXT_DIM, text=f"高 {shown_high:.2f}")
        canvas.create_text(pad, height - 8, anchor="sw", fill=T.TEXT_DIM, text=f"低 {shown_low:.2f}    现 {values[-1]:.2f}")


    def _draw_pnl_bars(self, canvas, width: int, height: int, points) -> None:
        """每一天一根柱子。没有平仓的日子不画。"""
        if not points:
            canvas.create_text(16, height / 2, anchor="w", fill=T.TEXT_DIM, text="这段时间还没有平仓盈亏")
            return
        values = [value for _day, value in points]
        span = max(max(abs(value) for value in values), 1)
        pad = 16
        mid = height / 2
        gap = max((width - pad * 2) / len(points), 2)
        for index, (day, value) in enumerate(points):
            x = pad + index * gap
            bar_h = (abs(value) / span) * (mid - 18)
            color = "#E85D4C" if value > 0 else T.OK
            if value >= 0:
                canvas.create_rectangle(x, mid - bar_h, x + max(gap - 2, 1), mid, fill=color, outline="")
            else:
                canvas.create_rectangle(x, mid, x + max(gap - 2, 1), mid + bar_h, fill=color, outline="")
            if len(points) <= 12:
                canvas.create_text(x, height - 8, anchor="n", fill=T.TEXT_DIM, text=f"{day.month}/{day.day}", font=T.FONT_SMALL)
        canvas.create_text(pad, 8, anchor="nw", fill=T.TEXT_DIM, text="红赚 绿亏")


    def _draw_account_calendar(self) -> None:
        """按月画出每天的盈亏。没有平仓的日子只显示日期。有盈亏时，金额写在日期右边同一行。"""
        import calendar
        import tkinter.font as tkfont
        from datetime import date as today_date

        canvas = getattr(self, "ac_calendar", None)
        report = self._ac_report
        if canvas is None or report is None:
            return
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 520)
        shown = self._ac_cal
        self.ac_cal_label.configure(text=f"{shown.year}年{shown.month}月")
        month_total = 0.0
        for day, value in report.daily_pnl.items():
            if day.year == shown.year and day.month == shown.month:
                month_total = round(month_total + value, 2)
        color = "#E85D4C" if month_total > 0 else (T.OK if month_total < 0 else T.TEXT_DIM)
        self.ac_month_pnl.configure(text=f"本月盈亏 {month_total:+.2f}", text_color=color)
        names = ["日", "一", "二", "三", "四", "五", "六"]
        cell_w = width / 7
        # 一行放下日期和盈亏。格子加高一点，字不会贴住上下边。
        cell_h = 32
        header_h = 22
        weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(shown.year, shown.month)
        height = header_h + len(weeks) * cell_h + 8
        canvas.configure(height=height)
        canvas.delete("all")
        for index, name in enumerate(names):
            canvas.create_text(index * cell_w + cell_w / 2, 10, text=name, fill=T.TEXT_DIM)
        # 日期用稍大的字，盈亏紧跟在右边，中间留一截空隙，避免和日期粘成一团。
        day_font = tkfont.Font(family="Segoe UI", size=11)
        pnl_font = tkfont.Font(family="Segoe UI", size=10)
        date_pnl_gap = 8
        for row, week in enumerate(weeks):
            for col, day_number in enumerate(week):
                if day_number == 0:
                    continue
                day = today_date(shown.year, shown.month, day_number)
                value = report.daily_pnl.get(day)
                # 左右各留一点空白，色块不会贴到旁边那一天的数字上。
                x0 = col * cell_w + 4
                y0 = header_h + row * cell_h
                x1 = (col + 1) * cell_w - 4
                y1 = y0 + cell_h - 6
                mid_y = (y0 + y1) / 2
                if value is None:
                    canvas.create_text((x0 + x1) / 2, mid_y, text=str(day_number), fill=T.TEXT_DIM, font=day_font)
                    continue
                fill = "#5A2A2A" if value > 0 else ("#1E4A38" if value < 0 else T.BG_ELEVATED)
                canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")
                day_text = str(day_number)
                pnl_text = f"{value:+.0f}"
                day_w = day_font.measure(day_text)
                use_font = pnl_font
                pnl_w = use_font.measure(pnl_text)
                gap = date_pnl_gap
                side_pad = 8
                room = (x1 - x0) - side_pad * 2
                # 金额特别长时把字缩小，仍然留在这一格里，不写到后一天上。
                if day_w + gap + pnl_w > room:
                    use_font = tkfont.Font(family="Segoe UI", size=8)
                    pnl_w = use_font.measure(pnl_text)
                    gap = 5
                group_w = day_w + gap + pnl_w
                start = x0 + max(side_pad, ((x1 - x0) - group_w) / 2)
                if start + group_w > x1 - side_pad:
                    start = max(x0 + 2, x1 - side_pad - group_w)
                canvas.create_text(start, mid_y, anchor="w", text=day_text, fill=T.TEXT, font=day_font)
                pnl_color = "#E85D4C" if value > 0 else T.OK
                canvas.create_text(start + day_w + gap, mid_y, anchor="w", text=pnl_text, fill=pnl_color, font=use_font)


    def _refresh_signal_view(self) -> None:
        """右边重画成绩。账本还没有时说明从这一版才开始记，不把旧的手工账显示出来。"""
        from src.account.signal_book import multiplier_map
        from src.account.signal_book import book_path, describe, load_book, shown_equity

        if not hasattr(self, "ac_result_box"):
            return
        from src.account.workspace import profile_of

        info = profile_of(root=self.root_dir)
        self._paint_account_hint()
        book = load_book(self.root_dir)
        if book is None:
            if hasattr(self, "ac_balance_label"):
                self.ac_balance_label.configure(text="当前余额  还没开始")
            self._paint_account_stats(None)
            self._paint_ctp_fund_panel(None)
            self._sync_account_mode()
            from src.account.workspace import is_ctp_book

            if is_ctp_book(info["id"]):
                if hasattr(self, "ac_ledger_title"):
                    self.ac_ledger_title.configure(text="柜台资金与程序成交")
                empty = (
                    f"「{info['label']}」还没有期货公司资金回报。\n"
                    "请先打开交易台，用这一本账对应的账号登录，点「刷新持仓」或左边「从柜台刷新资金」。\n"
                    "查到的账号、权益、可用资金会原样存到本地，不能手改。\n"
                    "GUI 点单请换到 GUI实盘账 或 GUI模拟账。"
                )
            else:
                if hasattr(self, "ac_ledger_title"):
                    self.ac_ledger_title.configure(text="资金变动记录")
                empty = (
                    f"「{info['label']}」还没有开始记账。\n"
                    "程序按信号真正开仓或平仓后会自动记在这里。\n"
                    "也可以先点「初始化」。以前手工记的成交不带过来。\n"
                    "另一本账的记录不会出现在这里。\n"
                    "运行页的「模拟（不点单）」只看信号、不点击，那种单也不会记进这本账。"
                )
            self._set_result_text(self.ac_result_box, empty)
            return
        path = book_path(self.root_dir)
        try:
            self._signal_book_mtime = path.stat().st_mtime
        except OSError:
            self._signal_book_mtime = 0.0
        balance = shown_equity(book, multiplier_map(self.cfg))
        shown = str(int(balance)) if float(balance) == int(balance) else f"{float(balance):.2f}"
        if hasattr(self, "ac_balance_label"):
            self.ac_balance_label.configure(text=f"当前余额  {shown} 元")
        if not self.ac_capital_var.get().strip():
            initial = float(book.get("initial_capital") or 0)
            self.ac_capital_var.set(str(int(initial)) if initial == int(initial) else f"{initial:.2f}")
        if str(book.get("funding") or "") == "ctp":
            from src.account.signal_book import describe_ctp_funds, repair_ctp_daily, save_book

            # 结算后空壳快照会把日历盖成 0；打开本页时按历史纠正并写回。
            if repair_ctp_daily(book):
                try:
                    save_book(self.root_dir, book)
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(self, "ac_ledger_title"):
                self.ac_ledger_title.configure(text="柜台资金与程序成交")
            self._set_result_text(self.ac_result_box, describe_ctp_funds(book))
        else:
            if hasattr(self, "ac_ledger_title"):
                self.ac_ledger_title.configure(text="资金变动记录")
            self._set_result_text(self.ac_result_box, describe(book, multiplier_map(self.cfg)))
        self._paint_ctp_fund_panel(book)
        self._sync_account_mode()
        self._paint_account_stats(book)


    def _pull_signal_balance(self) -> None:
        """
        常驻做完一笔后，把配置页的账户权益改成新余额。

        这个数字不能在配置页手改，所以余额一变就更新显示。
        账本文件没变时，不反复刷账户分析右边的文字。
        """
        from src.account.signal_book import multiplier_map
        from src.account.signal_book import book_path, load_book, shown_equity

        path = book_path(self.root_dir)
        if not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        # 账本文件没变，余额和右边文字都不会变，不必每 800ms 全量读一遍 JSON。
        if mtime == self._signal_book_mtime:
            return
        book = load_book(self.root_dir)
        if book is None:
            return
        balance = shown_equity(book, multiplier_map(self.cfg))
        try:
            shown = float(self.capital_var.get())
        except (TypeError, ValueError):
            shown = None
        if shown is None or abs(balance - shown) > 0.01:
            self._apply_book_balance(balance)
        self._refresh_signal_view()

    def _shift_account_month(self, step: int) -> None:
        """盈亏日历翻到上一个月或下一个月。"""
        year = self._ac_cal.year
        month = self._ac_cal.month + step
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        self._ac_cal = self._ac_cal.replace(year=year, month=month, day=1)
        self._draw_account_calendar()


    def _on_export_account_part(self, part: str) -> None:
        """只导出账户分析的一张表。交易记录是开平仓，资金变动记录是五种余额变动。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_account_part

        title = "导出交易记录" if part == "trades" else "导出资金变动记录"
        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title=title,
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, sheet, rows = export_account_part(self.root_dir, path, part)
        except Exception as exc:  # noqa: BLE001
            logger.exception("导出账户分析失败")
            messagebox.showerror("导出失败", self._export_fail_text(path, exc))
            return
        messagebox.showinfo(
            "已导出",
            f"已写入工作表「{sheet}」，{rows} 行。\n\n文件：\n{out}"
            + self._export_saved_note(path, out),
        )
        self._append_log(f"{title}已写入 {out} 的「{sheet}」")


    def _on_export_all(self) -> None:
        """把优化参数、交易记录、资金变动记录、复盘、最近回测、交叉测试和最近优化写进同一个 Excel 的不同表。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_all

        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title="导出全部到一个 Excel",
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, counts = export_all(self.root_dir, path)
        except Exception as e:  # noqa: BLE001
            logger.exception("导出全部失败")
            messagebox.showerror("导出失败", self._export_fail_text(path, e))
            return
        lines = [f"{name}：{n} 行" for name, n in counts.items()]
        messagebox.showinfo(
            "已导出",
            "各功能已写入不同的工作表：\n\n"
            + "\n".join(lines)
            + "\n\n某一表如果只有一句「还没有…」，表示这项还没做过。\n"
            + "界面上的交叉测试结果已清空。\n\n"
            + f"文件：\n{out}"
            + self._export_saved_note(path, out),
        )
        self._clear_cross_results()
        self._append_log(f"已导出全部到 {out}。界面上的交叉测试结果已清空")

