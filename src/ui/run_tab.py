"""Run-console construction, logging, resident execution, manual trades, and window lifecycle."""

from __future__ import annotations

from loguru import logger
from pathlib import Path
from typing import Callable
from src.ui import theme as T
from src.ui.app_common import QueueLogHandler, bind_wrap_to_parent
from src.utils.ui_log import ui_sink_filter
from src.utils import save_config
from tkinter import messagebox
import customtkinter as ctk
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk


class RunTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_run_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=2)
        tab.grid_columnconfigure(1, weight=3)
        tab.grid_rowconfigure(0, weight=1)

        # 可滚动：窗口较矮时，手工单录入下面的导出和保存仍能滚出来，不会被裁掉。
        left = ctk.CTkScrollableFrame(
            tab,
            fg_color="transparent",
            border_width=0,
            scrollbar_fg_color=T.BG,
            scrollbar_button_color=T.BORDER,
            scrollbar_button_hover_color=T.TEXT_DIM,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="控制台", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            left,
            text="当前关注的品种看顶部。要换合约请到「配置」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=8, pady=(0, 8))

        # 只留顶部状态栏没有的内容：信号周期、止损周期。
        route = ctk.CTkFrame(left, fg_color=T.BG_ELEVATED, corner_radius=10, border_width=1, border_color=T.BORDER)
        route.pack(fill="x", padx=8, pady=(0, 8))
        self.route_summary = ctk.CTkLabel(
            route,
            text="-",
            font=T.FONT_SMALL,
            text_color=T.TEXT,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        self.route_summary.pack(anchor="w", fill="x", padx=12, pady=(4, 4))

        form = ctk.CTkFrame(left, fg_color="transparent")
        form.pack(fill="x", padx=8)
        # 模拟开关保留在运行页：启动前最容易忘记，单独放这里更安全
        dry = ctk.CTkFrame(form, fg_color="transparent")
        dry.pack(fill="x", pady=(4, 6))
        ctk.CTkLabel(dry, text="模拟模式 dry_run", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w").pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkSwitch(dry, text="", variable=self.dry_var, progress_color=T.OK, fg_color=T.BORDER).pack(side="left")

        btn_row = ctk.CTkFrame(left, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(6, 8))
        # 启动常驻 / 跑着时变成停止常驻（结束扫描循环）。暂停报单不停扫描。
        self.btn_start = ctk.CTkButton(
            btn_row,
            text="启动常驻",
            height=38,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            text_color_disabled=T.ACCENT_TEXT_DISABLED,
            command=self._on_start,
        )
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.btn_stop = ctk.CTkButton(
            btn_row,
            text="暂停报单",
            height=38,
            fg_color=T.DANGER_MUTED,
            hover_color=T.DANGER_HOVER,
            text_color=T.DANGER_TEXT,
            text_color_disabled=T.DANGER_TEXT_DISABLED,
            command=self._on_pause_orders,
            state="disabled",
        )
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(6, 0))
        # 核对委托：点单和记账之间崩溃后的熔断。和持仓卡「认领/拒绝仓单」不是一回事。
        self.btn_confirm = ctk.CTkButton(
            btn_row,
            text="核对委托",
            width=86,
            height=38,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.TEXT_DIM,
            command=self._on_confirm_positions,
        )
        self.btn_confirm.pack(side="left", padx=(10, 0))


        pause_hint = ctk.CTkLabel(
            left,
            text="「暂停报单」后扫描还在跑，只停开新仓；止损和收盘清仓仍会发；要结束常驻请点「停止常驻」；「核对委托」只解除崩溃后的熔断，不改持仓。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        )
        pause_hint.pack(anchor="w", padx=8, pady=(0, 8))
        bind_wrap_to_parent(pause_hint)

        # 当前账号只读展示：执行通道/柜台/账本由「交易账号」页统一决定。
        session_row = ctk.CTkFrame(left, fg_color="transparent")
        session_row.pack(fill="x", padx=8, pady=(0, 4))
        self.session_summary_var = ctk.StringVar(value=self._session_summary_text())
        ctk.CTkLabel(
            session_row,
            textvariable=self.session_summary_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
            wraplength=300,
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            session_row,
            text="去交易账号",
            width=86,
            height=28,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=lambda: self.tabs.set("交易账号"),
        ).pack(side="right")

        self.go_live_menu = ctk.CTkOptionMenu(
            left,
            variable=self.go_live_var,
            values=self.go_live_choices,
            command=lambda _v: self._on_go_live_change(),
            width=360,
            height=32,
        )
        self.go_live_menu.pack(fill="x", padx=8, pady=(0, 0))
        stage_hint_label = ctk.CTkLabel(
            left,
            textvariable=self.go_live_hint_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        )
        stage_hint_label.pack(anchor="w", padx=8, pady=(4, 4))
        bind_wrap_to_parent(stage_hint_label)
        stage_btns = ctk.CTkFrame(left, fg_color="transparent")
        stage_btns.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(
            stage_btns,
            text="打开交易台",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_ctp_probe,
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            stage_btns,
            text="本阶段完成",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_go_live_done,
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        # 试跑一轮 / 点击下单 / 完整自检 / 仿真柜台收进一个入口。
        self.btn_test_tools = ctk.CTkButton(
            left,
            text="测试工具",
            height=36,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            text_color_disabled=T.TEXT_DIM,
            command=self._on_test_tools,
        )
        self.btn_test_tools.pack(fill="x", padx=8, pady=(0, 4))
        tools_hint = ctk.CTkLabel(
            left,
            text="日常先「试跑一轮」；验坐标用「点击下单」；程序自检用「完整自检」；随时测 CTP 链路用「仿真柜台」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        )
        tools_hint.pack(anchor="w", padx=8, pady=(0, 8))
        bind_wrap_to_parent(tools_hint)

        ctk.CTkButton(
            left,
            text="手工单（只改程序账本）",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_manual_trade,
        ).pack(fill="x", padx=8, pady=(0, 8))

        ctk.CTkButton(
            left,
            text="导出全部到一个 Excel",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_all,
        ).pack(fill="x", padx=8, pady=(0, 8))

        # 日志
        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        ctk.CTkLabel(head, text="运行日志", font=T.FONT_TITLE, text_color=T.TEXT).pack(side="left")
        ctk.CTkLabel(
            head,
            text="只显示下单、告警和扫描结论变化；拉K线、对账通过写在 logs 文件夹",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(
            head, text="清空", width=64, height=28, fg_color=T.BG_PANEL, hover_color=T.BORDER,
            command=lambda: self.log_box.delete("1.0", "end"),
        ).pack(side="right")
        self.log_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word",
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._append_log(
            "控制台就绪。同花顺通道：启动或到点会先准备交易软件再扫描。"
            "CTP 通道：到点直接启动常驻连柜台，不打开同花顺。"
        )


    def _hook_logger(self) -> None:
        def emit(text: str) -> None:
            # 后台线程只入队，不碰 Tk。
            self._log_queue.put_nowait(text)

        logger.add(
            QueueLogHandler(emit).write,
            format="{time:HH:mm:ss} | {level} | {message}",
            level="INFO",
            filter=ui_sink_filter,
        )
        self._log_after_id = self.after(100, self._drain_logs)


    def _drain_logs(self) -> None:
        """主线程定时把队列里的日志写进运行页。内部任何一步异常都不能让日志泵断掉。"""
        if self._closing:
            return
        try:
            try:
                while True:
                    text = self._log_queue.get_nowait()
                    self._append_log(text)
            except queue.Empty:
                pass
            from src.trader.gui_trader import order_click_in_progress

            if not order_click_in_progress():
                self._flush_held_logs()
        except Exception as exc:  # noqa: BLE001
            logger.warning("日志刷新跳过一轮: {}", exc)
        finally:
            if not self._closing:
                try:
                    self._log_after_id = self.after(100, self._drain_logs)
                except Exception:  # noqa: BLE001
                    pass


    def _flush_held_logs(self) -> None:
        """把点击期间攒下的日志一次性补上。"""
        pending = self._held_logs
        self._held_logs = []
        for line in pending:
            self.log_box.insert("end", line + "\n")
        if pending:
            self.log_box.see("end")
            self._trim_log_box()


    def _trim_log_box(self) -> None:
        """运行日志只留最近若干行，避免长时间挂机把文本框和内存越撑越大。"""
        try:
            total = int(self.log_box.index("end-1c").split(".")[0])
            if total <= self._LOG_MAX_LINES:
                return
            self.log_box.delete("1.0", f"{total - self._LOG_MAX_LINES}.0")
        except Exception as exc:  # noqa: BLE001
            logger.debug("运行日志裁剪跳过: {}", exc)


    def _append_log(self, text: str) -> None:
        """
        把一行日志写进运行页。

        正在点击交易软件时先攒着，不插入文本框。文本框一滚动，窗口就会抢到最前，
        后面的价格和买卖按钮就点到本程序上。点击结束后再一次性补上。
        """
        from src.trader.gui_trader import order_click_in_progress

        if order_click_in_progress():
            self._held_logs.append(text)
            return
        self._flush_held_logs()
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self._trim_log_box()


    def _on_ctp_probe(self) -> None:
        """打开 CTP 交易窗口。不改当前执行通道，也不碰交易软件。"""
        from src.ui.ctp_probe import open_ctp_probe

        open_ctp_probe(self)


    def _session_summary_text(self) -> str:
        """运行页只读：当前账号 / 连接 / 适配器 / 账本。"""
        try:
            from src.account.session import resolve_session

            session = resolve_session(self.root_dir, self.cfg, apply=False)
            if session is not None:
                return session.summary
            from src.domain.accounts import load_active_id, profile_of

            info = profile_of(load_active_id(self.root_dir), root=self.root_dir)
            return f"旧账本 · {info['label']}（未绑定账号）"
        except Exception:  # noqa: BLE001 - 只是展示
            return "当前账号读取失败"

    def _on_go_live_change(self) -> None:
        """换上线阶段。没走完前一步时退回原来的选择。"""
        from tkinter import messagebox

        from src.trader.go_live import (
            apply_stage_to_cfg,
            load_state as load_go_live,
            normalize_stage,
            save_state,
            stage_hint,
            stage_label,
            why_cannot_enter,
        )

        target = normalize_stage(self.go_live_var.get())
        state = load_go_live(self.root_dir)
        kept_exec = str(self.exec_var.get() or "sim").strip() or "sim"
        if target == normalize_stage(state.get("stage")):
            apply_stage_to_cfg(self.cfg, target)
            self.cfg.setdefault("trade", {})["executor"] = kept_exec
            self.go_live_hint_var.set(stage_hint(target))
            return
        blocked = why_cannot_enter(state, target)
        if blocked:
            messagebox.showwarning("还不能进入这一步", blocked)
            self.go_live_var.set(stage_label(state.get("stage")))
            self.go_live_hint_var.set(stage_hint(state.get("stage")))
            return
        state["stage"] = target
        save_state(self.root_dir, state)
        apply_stage_to_cfg(self.cfg, target)
        self.cfg.setdefault("trade", {})["executor"] = kept_exec
        self.exec_var.set(kept_exec)
        from src.trader.ctp_logic import ctp_kind_label

        if hasattr(self, "_ctp_kind_syncing"):
            self._ctp_kind_syncing = True
        self.ctp_kind_var.set(ctp_kind_label(((self.cfg.get("trade") or {}).get("ctp") or {}).get("account_kind")))
        if hasattr(self, "_ctp_kind_syncing"):
            self._ctp_kind_syncing = False
        if hasattr(self, "_fill_ctp_profile_vars"):
            from src.trader.ctp_profile import profile_name_for_kind

            ctp_now = ((self.cfg.get("trade") or {}).get("ctp") or {})
            name = profile_name_for_kind(ctp_now, ctp_now.get("account_kind"))
            if name and hasattr(self, "ctp_profile_var"):
                self._ctp_profile_syncing = True
                self.ctp_profile_var.set(name)
                self._ctp_profile_syncing = False
            self._fill_ctp_profile_vars()
        self.go_live_hint_var.set(stage_hint(target))
        self._append_log(f"上线阶段已改为{stage_label(target)}")


    def _on_go_live_done(self) -> None:
        """勾选当前阶段完成，能进下一步就问要不要换过去。"""
        from tkinter import messagebox

        from src.trader.go_live import (
            apply_stage_to_cfg,
            load_state as load_go_live,
            mark_stage_passed,
            next_stage,
            normalize_stage,
            save_state,
            stage_hint,
            stage_label,
            why_cannot_enter,
        )

        current = normalize_stage(self.go_live_var.get())
        state = load_go_live(self.root_dir)
        ok, message = mark_stage_passed(state, current)
        if not ok:
            messagebox.showwarning("还不能勾完成", message)
            return
        state["stage"] = current
        save_state(self.root_dir, state)
        self._append_log(message)
        nxt = next_stage(current)
        if not nxt:
            messagebox.showinfo("上线流程", "四步都已走到策略自动真实账户。请确认真实前置和资金账号后再启动常驻。")
            return
        if why_cannot_enter(state, nxt):
            messagebox.showinfo("本阶段已勾完成", message)
            return
        if messagebox.askyesno("进入下一步？", f"{message}\n\n要进入{stage_label(nxt)}吗？"):
            state["stage"] = nxt
            save_state(self.root_dir, state)
            apply_stage_to_cfg(self.cfg, nxt)
            kept_exec = str(self.exec_var.get() or "sim").strip() or "sim"
            self.cfg.setdefault("trade", {})["executor"] = kept_exec
            self.go_live_var.set(stage_label(nxt))
            self.go_live_hint_var.set(stage_hint(nxt))
            self.exec_var.set(kept_exec)
            from src.trader.ctp_logic import ctp_kind_label

            if hasattr(self, "_ctp_kind_syncing"):
                self._ctp_kind_syncing = True
            self.ctp_kind_var.set(
                ctp_kind_label(((self.cfg.get("trade") or {}).get("ctp") or {}).get("account_kind"))
            )
            if hasattr(self, "_ctp_kind_syncing"):
                self._ctp_kind_syncing = False
            self._append_log(f"已进入{stage_label(nxt)}")


    def _on_start(self) -> None:
        from tkinter import messagebox

        if self._prep_busy:
            messagebox.showwarning("正在准备", "交易软件还在登录或改价格。这几步做完之前不会启动常驻。")
            return
        if self._block_resident_if_research():
            return
        if self.worker.running:
            self._on_halt_resident()
            return

        from src.trader.go_live import (
            allows_strategy_orders,
            apply_stage_to_cfg,
            load_state as load_go_live,
            normalize_stage,
            stage_label,
            why_cannot_enter,
        )

        stage_now = normalize_stage(self.go_live_var.get())
        chosen_exec = str(self.exec_var.get() or "sim").strip() or "sim"
        apply_stage_to_cfg(self.cfg, stage_now)
        # 上线③④只表示「CTP 可以策略下单」，不能把已经选好的同花顺 gui 改掉。
        self.cfg.setdefault("trade", {})["executor"] = chosen_exec
        self.exec_var.set(chosen_exec)
        from src.trader.base import capabilities_for

        caps = capabilities_for(self.exec_var.get())
        if caps.orders_via_broker and not allows_strategy_orders(stage_now):
            messagebox.showinfo(
                "这一步请用交易台",
                f"当前是{stage_label(stage_now)}。请点「打开交易台」手工下单。\n"
                "策略自动下单要等到③策略自动仿真或④策略自动真实账户。",
            )
            return
        if allows_strategy_orders(stage_now):
            blocked = why_cannot_enter(load_go_live(self.root_dir), stage_now)
            if blocked:
                messagebox.showerror("还不能让策略自动下单", blocked)
                return
        if caps.orders_via_broker:
            from src.account.ctp_sync import align_active_book_to_ctp, resident_book_guard

            self._apply_ctp_book_align(
                align_active_book_to_ctp(self.root_dir, self.cfg, running=False)
            )
            book_block = resident_book_guard(self.root_dir, self.cfg)
            if book_block:
                messagebox.showerror("先核对账户", book_block)
                self._append_log("未启动常驻：" + book_block)
                return
            from src.trader.go_live import STAGE_AUTO_LIVE

            from src.ui.ctp_probe import probe_login_block_reason, shutdown_probe_window

            desk_block = probe_login_block_reason(self)
            if desk_block:
                if not messagebox.askyesno(
                    "交易台还登着",
                    desk_block + "\n\n是 = 关掉交易台再启动常驻\n否 = 不启动",
                ):
                    self._append_log("未启动常驻：交易台还登着柜台")
                    return
                close_fail = shutdown_probe_window(self)
                if close_fail:
                    messagebox.showwarning("还不能启动", close_fail)
                    self._append_log("未启动常驻：" + close_fail)
                    return
            if stage_now == STAGE_AUTO_LIVE:
                from src.trader.protective import overnight_unprotected_notice

                live_notice = overnight_unprotected_notice(
                    live=True, dry_run=bool(self.dry_var.get())
                )
                extra = ("\n\n" + live_notice) if live_notice else ""
                if not messagebox.askyesno(
                    "真实资金",
                    "策略将用和交易台同一套接口，向真实资金账户报单。\n"
                    "确认前置、资金账号都填对了吗？"
                    + extra,
                ):
                    return
        if caps.orders_via_broker:
            from src.trader.ctp_logic import ctp_kind_label

            kind_txt = ctp_kind_label(self.ctp_kind_var.get())
            self._append_log(
                f"上线阶段{stage_label(stage_now)}。策略走和交易台同一套 CTP 接口（{kind_txt}）。"
            )
            if self.dry_var.get():
                choice = messagebox.askyesnocancel(
                    "模拟模式还开着",
                    "执行通道已经是 CTP，但「模拟模式」还开着：策略不会向柜台发单。\n\n"
                    "是 = 关掉模拟，允许策略用当前操作账报单\n"
                    "否 = 继续只监测、不报单\n"
                    "取消 = 不启动",
                )
                if choice is None:
                    self._skip_next_bot_login = False
                    self._append_log("已取消启动。")
                    return
                if choice:
                    self.dry_var.set(False)
                    self._append_log("已关闭模拟模式，策略将向 CTP 报单。")
                else:
                    self._append_log("保持模拟模式：只监测，不向柜台发单。")

        # 启动时若仍开着模拟，而通道是窗口点击：明确问要不要真下单
        # 原理：点启动会把界面开关写回 config；开关忘关就会又变成 dry_run=True
        if caps.window_driven and self.dry_var.get():
            choice = messagebox.askyesnocancel(
                "模拟模式还开着",
                "当前「模拟模式」是开的：会先打开交易软件、登录并改成对手价，但不会点买开、卖开、平仓。\n\n"
                "是 = 关掉模拟，允许自动点单（模拟账户也可）\n"
                "否 = 继续只监测、不点买卖\n"
                "取消 = 不启动",
            )
            if choice is None:
                self._skip_next_bot_login = False
                self._append_log("已取消启动。")
                return
            if choice:
                self.dry_var.set(False)
                self._append_log("已关闭模拟模式，将真实点击交易软件下单。")
            else:
                self._append_log("保持模拟模式：只监测，不点击。")
        elif caps.window_driven and not self.dry_var.get():
            self._append_log("警告：窗口点击通道 + 关闭模拟 会真实点击交易软件。")

        # 窗口点击通道先做完打开、登录、确认结算单和对手价。失败就不启动，避免没准备好就去扫描。
        if caps.window_driven:
            self._begin_prepare(automatic=True)
            return
        self._launch_resident()


    def _launch_resident(self) -> None:
        """真正启动常驻扫描。调用前，准备动作应该已经做完。"""
        try:
            self._launch_resident_inner()
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动常驻失败")
            from tkinter import messagebox

            messagebox.showerror(
                "启动常驻失败",
                str(exc) + "\n\n请检查配置（保证金表、品种、风险参数等），或看日志定位。",
            )
            self._status = "待命"
            try:
                self._refresh_status_cards()
            except Exception:  # noqa: BLE001
                pass


    def _launch_resident_inner(self) -> None:
        cfg = self._cfg_for_live_scan()
        if self._skip_next_bot_login:
            # 准备阶段已经登录过。这个标记只留在内存副本里，不写进配置文件。
            cfg["_startup_login_done"] = True
            self._skip_next_bot_login = False
        # 单品种挂机更安全：多品种窗口点击通道不会自动改合约框
        syms = (cfg.get("strategy") or {}).get("symbols") or []
        from src.trader.base import capabilities_for

        if capabilities_for((cfg.get("trade") or {}).get("executor")).window_driven and len(syms) > 1:
            self._append_log(
                f"警告：窗口点击模式关注了多个品种 {syms}，程序不会自动切换合约，"
                "请只保留一个（如 bu2612）或确保盘面合约正确。"
            )
        # 磁盘上保存的是配置页本身。交给机器人的是副本，后面回测改副本不会牵动正在跑的程序。
        save_config(self.cfg)
        self._status = "启动中"
        self._refresh_status_cards()

        def on_stopped() -> None:
            self._post_to_main(self._after_stopped)

        self.worker.start(cfg, on_stopped=on_stopped)
        self._refresh_status_cards()
        mode = "模拟(不点单)" if cfg.get("dry_run") else "实盘点击"
        from src.account.workspace import profile_of

        book_name = profile_of(root=self.root_dir)["label"]
        self._append_log(f"已启动常驻扫描 | dry_run={cfg.get('dry_run')} | {mode} | {book_name}")
        if cfg.get("_startup_login_done"):
            self._append_log("刚才已经登录过，常驻不再登录第二次。只再看一眼对手价。")
        else:
            self._append_log("常驻会先登录并核对对手价，这两步不点买卖。价格没改成功时，后面也不会点买卖。")
        self.tabs.set("运行")
        # 不再把自己拉到最前。一拉到前面，后面要点交易软件的那一下会点到本程序的按钮上，
        # 比如下单模拟器。常驻只该操作交易软件，下单模拟器留给你自己点。
        try:
            self.log_box.focus_set()
        except Exception:  # noqa: BLE001
            pass


    def _on_prepare_then_start(self, *, automatic: bool = False) -> None:
        """
        先打开交易软件、登录、改对手价，全部成功后再启动常驻。

        准备过程不点买开、卖开、平仓。失败就停在准备，不启动扫描。
        """
        from tkinter import messagebox

        if self._prep_busy:
            if not automatic:
                messagebox.showwarning("正在准备", "上一次准备还没做完。")
            return
        if self.worker.running:
            if not automatic:
                messagebox.showinfo("已经在跑", "常驻已经启动，不用再准备一次。")
            return
        if (
            not automatic
            and self.exec_var.get() == "gui"
            and not self.dry_var.get()
        ):
            if not messagebox.askyesno(
                "准备后会启动常驻",
                "接下来只做三件事：打开交易软件、登录、把价格改成对手价。\n"
                "这三步不点买开、卖开、平仓。\n\n"
                "都成功以后才会启动常驻。当前模拟是关的，"
                "启动后如果策略给出信号，就会真实点击下单。\n\n"
                "确定开始？",
            ):
                return
        self._begin_prepare(automatic=automatic)


    def _begin_prepare(self, *, automatic: bool) -> None:
        """在后台做同花顺准备。界面保持可点，但常驻要等准备结果回来才启动。"""
        from src.trader.base import capabilities_for

        chosen_exec = str(self.exec_var.get() or "").strip() or "gui"
        if not capabilities_for(chosen_exec).window_driven:
            self._append_log(f"当前是 {chosen_exec} 通道，跳过同花顺准备。")
            if automatic:
                self._auto_launch_resident_at_login(chosen_exec)
            else:
                self._launch_resident()
            return
        self._prep_busy = True
        self._append_log("开始准备交易软件：打开、登录、改对手价。这几步不下单。")
        cfg = self._clone_cfg()
        cfg.setdefault("trade", {})["executor"] = "gui"

        def _work() -> None:
            from src.login.prepare import prepare_trading_desk

            try:
                ok, lines = prepare_trading_desk(cfg)
            except Exception as exc:  # noqa: BLE001
                logger.exception("准备交易软件失败")
                ok, lines = False, [f"准备失败：{exc}"]

            def _done() -> None:
                self._prep_busy = False
                for line in lines:
                    self._append_log(line)
                if not ok:
                    from tkinter import messagebox

                    messagebox.showwarning(
                        "还不能启动",
                        "准备没有完成，常驻没有启动，所以不会下单。\n\n" + "\n".join(lines),
                    )
                    return
                self._append_log("准备完成，现在启动常驻。这次不再登录第二遍。")
                self._skip_next_bot_login = True
                if automatic:
                    self._launch_resident()
                else:
                    self._on_start()

            self._post_to_main(_done)

        threading.Thread(target=_work, name="ths-prepare", daemon=True).start()


    def _tick_login_schedule(self) -> None:
        """
        到了配置里的登录时间：同花顺通道先准备软件；CTP 通道直接启动常驻连柜台。

        常驻已经在跑时，不在这里再开一次。
        """
        if self._closing:
            return
        try:
            self._maybe_start_at_login_time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查定时登录失败: {}", exc)
        if not self._closing:
            self._login_tick_after_id = self.after(20000, self._tick_login_schedule)


    def _warn_unprotected_resume(self) -> None:
        """上次退出时还有持仓/挂单：启动先说清楚，现在不会自动再开新仓。"""
        if getattr(self, "_unprotected_warned", False):
            return
        path = self.root_dir / "data" / "unprotected_positions.json"
        if not path.exists():
            return
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self._unprotected_warned = True
        self._append_log("上次退出时仍有持仓/挂单，启动后会先对账并评估止损，恢复前不报单。")
        messagebox.showwarning(
            "退出保护",
            "上次退出时仍有持仓或挂单。\n\n"
            "程序会先与柜台对账并评估止损，完成前暂停报单。请确认账户实际持仓。",
        )


    def _maybe_start_at_login_time(self) -> None:
        """当前这一分钟如果写在 login_times 里，而且今天还没触发过，就去启动。

        同花顺点击通道：先拉软件、登录、改对手价，再启动常驻。
        CTP / 模拟通道：不打开同花顺，直接启动常驻；CTP 由常驻自己连柜台。
        """
        from datetime import datetime

        times = [str(item).strip() for item in ((self.cfg.get("schedule") or {}).get("login_times") or [])]
        now = datetime.now()
        current = now.strftime("%H:%M")
        if current not in times:
            return
        key = now.strftime("%Y-%m-%d ") + current
        if key in self._login_fired:
            return
        self._login_fired.add(key)
        if self.worker.running or self._prep_busy:
            self._append_log(f"到了登录时间 {current}，常驻已在运行，不重复启动。")
            return
        chosen_exec = str(self.exec_var.get() or "").strip() or str(
            (self.cfg.get("trade") or {}).get("executor") or "sim"
        )
        from src.trader.base import capabilities_for

        caps = capabilities_for(chosen_exec)
        if caps.window_driven:
            self._append_log(f"到了登录时间 {current}。同花顺通道：先准备交易软件，再开始扫描。")
            self._begin_prepare(automatic=True)
            return
        self._append_log(
            f"到了登录时间 {current}。当前是 {chosen_exec} 通道：不打开同花顺，直接启动常驻"
            + ("并连接 CTP。" if caps.orders_via_broker else "。")
        )
        self._auto_launch_resident_at_login(chosen_exec)


    def _auto_launch_resident_at_login(self, chosen_exec: str) -> None:
        """定时启动：非同花顺通道无人值守启动常驻，尽量不弹确认框。"""
        from src.trader.go_live import (
            allows_strategy_orders,
            apply_stage_to_cfg,
            load_state as load_go_live,
            normalize_stage,
            stage_label,
            why_cannot_enter,
        )
        from src.trader.base import capabilities_for

        stage_now = normalize_stage(self.go_live_var.get())
        apply_stage_to_cfg(self.cfg, stage_now)
        self.cfg.setdefault("trade", {})["executor"] = chosen_exec
        self.exec_var.set(chosen_exec)
        caps = capabilities_for(chosen_exec)
        if caps.orders_via_broker and not allows_strategy_orders(stage_now):
            self._append_log(
                f"未自动启动：当前是{stage_label(stage_now)}，策略还不能自动下单。"
                "请先把上线阶段调到③或④，或白天用交易台。"
            )
            return
        if allows_strategy_orders(stage_now):
            blocked = why_cannot_enter(load_go_live(self.root_dir), stage_now)
            if blocked:
                self._append_log("未自动启动：" + blocked)
                return
        if caps.orders_via_broker:
            from src.account.ctp_sync import align_active_book_to_ctp, resident_book_guard

            self._apply_ctp_book_align(
                align_active_book_to_ctp(self.root_dir, self.cfg, running=False)
            )
            book_block = resident_book_guard(self.root_dir, self.cfg)
            if book_block:
                self._append_log("未自动启动：" + book_block)
                return
            from src.ui.ctp_probe import probe_login_block_reason, shutdown_probe_window

            desk_block = probe_login_block_reason(self)
            if desk_block:
                close_fail = shutdown_probe_window(self)
                if close_fail:
                    self._append_log("未自动启动：交易台还登着柜台，自动关掉失败。" + close_fail)
                    return
                self._append_log("定时启动前已关掉交易台，避免和常驻抢同一柜台。")
            from src.trader.ctp_logic import ctp_kind_label

            self._append_log(
                f"定时启动常驻 | 上线{stage_label(stage_now)} | "
                f"{ctp_kind_label(self.ctp_kind_var.get())} | "
                f"模拟={'开' if self.dry_var.get() else '关'}"
            )
        # 定时启动不经过同花顺准备，常驻里自己连 CTP / 跑模拟。
        self._skip_next_bot_login = False
        self._launch_resident()


    def _bring_self_front(self) -> None:
        """把本程序窗口拉回最前（启动后避免被交易软件/命令行盖住）。下单点击还没结束时不拉。"""
        from src.trader.gui_trader import order_click_in_progress

        if order_click_in_progress():
            return
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass


    def _after_stopped(self) -> None:
        self._status = "已停止"
        self._refresh_status_cards()


    def _on_confirm_positions(self) -> None:
        """人工核对交易软件持仓后，清掉遗留委托意图并解除对应熔断。"""
        from tkinter import messagebox

        bot = getattr(self.worker, "bot", None)
        if bot is None:
            messagebox.showinfo("核对委托", "常驻没有在跑，没有待核对的委托记录。")
            return
        if not messagebox.askyesno(
            "核对委托",
            "这和右上「认领仓单 / 拒绝仓单」不是同一件事。\n\n"
            "这里只清掉「点单后程序崩溃、还没记上账」留下的告警，不改持仓。\n"
            "柜台手数和程序账对不上，请用程序持仓卡下的认领或拒绝。\n\n"
            "请先在交易软件里核对实际持仓与委托。如果这笔其实已成交，先用「手工单录入」补进程序账。\n\n"
            "确认无误后解除熔断并继续扫描？",
        ):
            return
        ok, msg = bot.confirm_positions_reviewed()
        if ok:
            messagebox.showinfo("核对委托", msg)
        else:
            messagebox.showwarning("核对委托", msg)


    def _reconcile_pending_symbols(self) -> list[str]:
        """当前要对账的品种：柜台多出来的、或程序账有柜台没有的。"""
        bot = getattr(self.worker, "bot", None)
        if bot is None:
            return []
        rec = (bot.status_snapshot() or {}).get("reconcile") or {}
        return list(dict.fromkeys([*(rec.get("unclaimed") or []), *(rec.get("phantom") or [])]))

    def _on_claim_lots(self) -> None:
        """认领仓单：以华泰柜台为准，改程序账。"""
        bot = getattr(self.worker, "bot", None)
        if bot is None:
            messagebox.showinfo("认领仓单", "常驻没有在跑。启动常驻后再认领。")
            return
        pending = self._reconcile_pending_symbols()
        rec = (bot.status_snapshot() or {}).get("reconcile") or {}
        unclaimed = {str(item).strip().lower() for item in (rec.get("unclaimed") or [])}
        if not pending:
            messagebox.showinfo("认领仓单", "现在柜台和程序账是对齐的，不用认领。")
            return
        names = "、".join(pending)
        if not messagebox.askyesno(
            "认领仓单",
            "将以华泰柜台为准，改程序持仓和本地账本。\n"
            f"涉及：{names}\n\n"
            "柜台有仓、账本没有：把柜台的仓收进程序。\n"
            "柜台已空、账本还有：按现价把账本也改成空（手机刚平完应点「是」）。\n\n"
            "不想改账本、仍让程序记着仓，请点「否」，改用「拒绝仓单」。",
        ):
            return
        for symbol in pending:
            if str(symbol).strip().lower() in unclaimed:
                ok, msg = bot.adopt_broker_position(symbol)
            else:
                ok, msg = bot.close_broker_position(symbol)
            self._append_log(msg)
            if not ok:
                messagebox.showwarning("认领仓单", msg)
        self._refresh_status_cards()

    def _on_reject_lots(self) -> None:
        """拒绝仓单：以程序账为准，不跟柜台改。柜台已空时也不会把账本平掉。"""
        bot = getattr(self.worker, "bot", None)
        if bot is None:
            messagebox.showinfo("拒绝仓单", "常驻没有在跑。启动常驻后再拒绝。")
            return
        pending = self._reconcile_pending_symbols()
        if not pending:
            messagebox.showinfo("拒绝仓单", "现在柜台和程序账是对齐的，不用拒绝。")
            return
        names = "、".join(pending)
        if not messagebox.askyesno(
            "拒绝仓单",
            "将以程序账为准，不按柜台改持仓。\n"
            f"涉及：{names}\n\n"
            "柜台有仓、账本没有：不把柜台的仓收进来。\n"
            "柜台已空、账本还有：账本仍记着仓，不会替你平掉；该品种暂停自动开仓。\n\n"
            "手机已经平完、要把账本也改成空，请点「否」，改用「认领仓单」。",
        ):
            return
        reject = getattr(bot, "reject_broker_lots", None)
        for symbol in pending:
            if callable(reject):
                ok, msg = reject(symbol)
            else:
                ok, msg = bot.ignore_reconcile_symbol(symbol)
            self._append_log(msg)
            if not ok:
                messagebox.showwarning("拒绝仓单", msg)
        self._refresh_status_cards()

    def _on_reconcile_actions(self) -> None:
        """旧入口转到认领仓单。"""
        self._on_claim_lots()


    def _maybe_release_intent_halt(
        self, bot: object, symbol: str, side: str, volume: int
    ) -> None:
        """
        手工录入的这笔如果正好是上次没核对完的委托，顺手问一句要不要解除熔断。

        只在品种、方向、手数都对得上时提示；用户点「否」就保持熔断。
        """
        from tkinter import messagebox

        if bot is None:
            return
        matcher = getattr(bot, "pending_intent_matches", None)
        if matcher is None:
            return
        intent = matcher(symbol, side, volume)
        if not intent:
            return
        side_cn = {"buy_open": "买开", "sell_open": "卖开", "close": "平仓"}.get(side, side)
        if not messagebox.askyesno(
            "顺带解除熔断",
            "这笔和上次没核对完的委托一致：\n"
            f"{intent.get('symbol')} {side_cn} {intent.get('volume')} 手（{intent.get('ts')}）\n\n"
            "现在把它标记为已核对，并解除熔断吗？",
        ):
            return
        ok, msg = bot.confirm_positions_reviewed()
        self._append_log(("已解除熔断：" if ok else "解除熔断失败：") + msg)
        self._refresh_status_cards()
        if ok:
            messagebox.showinfo("已解除熔断", msg)


    def _on_halt_resident(self) -> None:
        """结束常驻主循环。暂停报单不够时用这个：扫描、清仓钟点、防休眠都会停。"""
        self.worker.stop()
        self._status = "正在停止常驻…"
        self._append_log("已发送停止常驻请求…")
        self._refresh_status_cards()


    def _on_pause_orders(self) -> None:
        """拨交易台同一套暂停标志。扫描不停。"""
        if not self.worker.running:
            return
        paused = not self.worker.orders_paused()
        self.worker.set_orders_paused(paused)
        self._append_log("已暂停开新仓，止损和清仓仍会发。" if paused else "已恢复报单。")
        self._refresh_status_cards()


    def _on_test_tools(self) -> None:
        """打开测试工具：试跑一轮 / 点击下单 / 完整自检 / 仿真柜台。"""
        from src.ui.test_tools import open_test_tools

        open_test_tools(
            self,
            on_once=self._on_once,
            on_order=self._on_order_simulator,
            on_full=self._on_sim_test,
            on_counter=self._on_sim_counter,
        )


    def _on_sim_counter(self) -> None:
        """打开独立本机柜台控制面板；关闭面板不会中断交易台自测。"""
        from src.ui.sim_counter import open_sim_counter

        open_sim_counter(self)


    def _on_once(self) -> None:
        if self.worker.running:
            messagebox.showwarning(
                "常驻还在跑",
                "请先点「停止常驻」，再试跑一轮。两套一起扫会重复下单。",
            )
            return
        started = self.worker.run_once(self._cfg_for_live_scan())
        if not started:
            messagebox.showwarning(
                "常驻还在跑",
                "请先点「停止常驻」，再试跑一轮。两套一起扫会重复下单。",
            )
            return
        self._append_log("已触发试跑一轮信号（单次扫描）。")


    def _on_sim_test(self) -> None:
        script = self.root_dir / "tools" / "sim_full_test.py"
        self._append_log("开始完整自检…")

        def _run() -> None:
            code = subprocess.call([sys.executable, str(script)])
            self._post_to_main(lambda: self._append_log(f"完整自检结束，exit={code}"))

        threading.Thread(target=_run, daemon=True).start()


    def _on_manual_trade(self) -> None:
        """
        统一的手工单录入：弹窗里填品种、价格、手数，按「买多」「卖空」或「平仓」。

        完全仿照真实下单，只是把动作记进程序的账，不会去点交易软件。
        用于你手动在交易软件上开仓/平仓后，把这笔回填进程序的持仓和信号账户。
        """
        from tkinter import messagebox

        import customtkinter as ctk

        from src.account.signal_book import write_capital_to_config
        from src.risk.sizing import PositionSizer
        from src.utils.state_store import (
            default_state_path,
            format_position_text,
            load_state,
            manual_close_without_bot,
            manual_open_without_bot,
            open_position_rows,
        )

        bot = getattr(self.worker, "bot", None)

        # 先读程序当前账上的仓位，弹窗里会显示"程序已记的仓位：xxx"
        if bot is not None and getattr(bot, "strategy", None) is not None:
            pos_rows = open_position_rows(bot.positions_snapshot())
        else:
            state = load_state(default_state_path(self.root_dir))
            pos_rows = open_position_rows(state.get("positions") or {})

        pos_text = (
            "程序账上："
            + "、".join(
                f"{r['symbol']} {'多' if r['side']=='long' else '空'}{r['volume']}手@{r['entry_price']:g}"
                for r in pos_rows
            )
            if pos_rows
            else "程序账上目前是空仓"
        )

        # 弹窗：品种、价格、手数 + 买多/卖空/平仓三个按钮
        dialog = ctk.CTkToplevel(self)
        dialog.title("手工单录入")
        dialog.geometry("420x520")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="手动在交易软件上开/平仓后，在这里录入。只改程序的账，不会去点交易软件。",
            justify="left",
            text_color=T.TEXT_DIM,
        ).pack(padx=16, pady=(16, 4))
        ctk.CTkLabel(
            dialog,
            text=pos_text,
            text_color=T.ACCENT,
            font=T.FONT_SMALL,
        ).pack(padx=16, pady=(0, 12))

        ctk.CTkLabel(dialog, text="品种（如 fu2701）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 6))
        symbol_entry.focus_set()

        ctk.CTkLabel(dialog, text="价格（成交价）").pack(anchor="w", padx=16)
        price_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="4960")
        price_entry.pack(fill="x", padx=16, pady=(0, 6))

        ctk.CTkLabel(dialog, text="手数").pack(anchor="w", padx=16)
        volume_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="1")
        volume_entry.pack(fill="x", padx=16, pady=(0, 6))

        ctk.CTkLabel(
            dialog,
            text="止损价（可选，开仓后策略按此自动止损；留空只靠反向信号和收盘清仓）",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=16)
        stop_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="留空=只靠反向信号和收盘清仓")
        stop_entry.pack(fill="x", padx=16, pady=(0, 2))

        rec_label = ctk.CTkLabel(
            dialog, text="", font=T.FONT_SMALL, text_color=T.TEXT_DIM,
        )
        rec_label.pack(anchor="w", padx=16, pady=(0, 16))

        # --- 止损价推荐：品种填完后自动算一次 ---
        rec_stops: dict[str, float] = {}  # {"long": X, "short": Y}

        def _refresh_stop_rec(*_args: object) -> None:
            sym = symbol_entry.get().strip().lower()
            if not sym or len(sym) < 4:
                rec_label.configure(text="")
                rec_stops.clear()
                return

            def _worker() -> None:
                try:
                    from src.strategy.daily_body import suggest_stop_both
                    bot_now = bot if bot is not None and getattr(bot, "kline", None) is not None else None
                    if bot_now is None:
                        # 常驻没跑，临时建一个 kline provider
                        from src.data import KlineProvider
                        kp = KlineProvider(self.cfg)
                        bars = kp.fetch(sym, period="5")
                        price_tick = float(
                            ((self.cfg.get("trade") or {}).get("ctp") or {}).get("price_tick", 1)
                            or (self.cfg.get("trade") or {}).get("price_tick", 1)
                            or 1
                        )
                        stop_extra_ticks = 1
                        eth_minutes = 30
                        session_open_h = 9
                    else:
                        bars = bot_now.kline.fetch(sym, period=str(bot_now.strategy.timeframe))
                        price_tick = float(getattr(bot_now.strategy, "price_tick", 1) or 1)
                        stop_extra_ticks = int(getattr(bot_now.strategy, "stop_extra_ticks", 1) or 1)
                        eth_minutes = int(getattr(bot_now.strategy, "eth_minutes", 30) or 30)
                        from src.strategy.daily_body import _parse_hhmm
                        so_raw = getattr(bot_now.strategy, "session_open", None)
                        session_open_h = _parse_hhmm(str(so_raw)).hour if so_raw else 9

                    from datetime import time as _dtime
                    long_stop, short_stop = suggest_stop_both(
                        bars or (),
                        price_tick=price_tick,
                        stop_extra_ticks=stop_extra_ticks,
                        eth_minutes=eth_minutes,
                        timeframe=str(getattr(bot_now.strategy, "timeframe", "5") if bot_now else "5"),
                        session_open=_dtime(session_open_h, 0),
                    )
                    rec_stops.clear()
                    parts = []
                    if long_stop is not None:
                        rec_stops["long"] = float(long_stop)
                        parts.append(f"买多建议 {long_stop:g}")
                    if short_stop is not None:
                        rec_stops["short"] = float(short_stop)
                        parts.append(f"卖空建议 {short_stop:g}")
                    hint = "  |  ".join(parts) if parts else "（暂无推荐，可留空）"
                    self._post_to_main(lambda: rec_label.configure(text=hint))
                except Exception:
                    self._post_to_main(lambda: rec_label.configure(text="（暂无推荐，可留空）"))

            import threading
            threading.Thread(target=_worker, daemon=True).start()

        symbol_entry.bind("<FocusOut>", _refresh_stop_rec)
        symbol_entry.bind("<Return>", _refresh_stop_rec)
        price_entry.bind("<FocusOut>", _refresh_stop_rec)
        price_entry.bind("<Return>", _refresh_stop_rec)

        # --- 点击买多/卖空时，如果止损空着，自动填入推荐值 ---
        def _fill_stop_if_empty(side_key: str) -> None:
            if stop_entry.get().strip():
                return
            rec = rec_stops.get(side_key)
            if rec is not None:
                stop_entry.insert(0, f"{rec:g}")

        ctk.CTkLabel(
            dialog,
            text="选择操作：",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=16, pady=(0, 4))

        action_taken: dict[str, str] = {}

        def _take(action: str) -> None:
            action_taken["action"] = action
            action_taken["symbol"] = symbol_entry.get().strip()
            action_taken["price"] = price_entry.get().strip()
            action_taken["volume"] = volume_entry.get().strip()
            action_taken["stop"] = stop_entry.get().strip()
            dialog.destroy()

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))
        btn_w = 110
        ctk.CTkButton(
            btn_frame, text="买多", width=btn_w, height=44,
            fg_color="#E53935", hover_color="#F44336", text_color="#FFFFFF",
            command=lambda: (_fill_stop_if_empty("long"), _take("买多")),
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            btn_frame, text="卖空", width=btn_w, height=44,
            fg_color="#2ECC8A", hover_color="#3ED99A", text_color="#FFFFFF",
            command=lambda: (_fill_stop_if_empty("short"), _take("卖空")),
        ).pack(side="left", expand=True, fill="x", padx=4)
        ctk.CTkButton(
            btn_frame, text="平仓", width=btn_w, height=44,
            fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER, text_color=T.ACCENT_ON,
            command=lambda: _take("平仓"),
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        ctk.CTkButton(dialog, text="取消", command=dialog.destroy, height=32).pack(padx=16, pady=(0, 16))

        # 按内容实际需要的高度定窗口。写死 520 在 125%/150% 缩放下控件被放大，取消按钮会被压扁。
        dialog.update_idletasks()
        dlg_w = max(420, dialog.winfo_reqwidth())
        dlg_h = max(520, dialog.winfo_reqheight())
        dialog.geometry(f"{dlg_w}x{dlg_h}")

        self.wait_window(dialog)
        if not action_taken:
            return

        symbol = action_taken["symbol"]
        if not symbol:
            messagebox.showerror("品种不对", "请填写品种代码，例如 fu2701。")
            return

        try:
            price = float(action_taken["price"])
        except ValueError:
            messagebox.showerror("价格不对", "请填写数字，例如 4960。")
            return
        if price <= 0:
            messagebox.showerror("价格不对", "价格要大于 0。")
            return

        try:
            volume = int(action_taken["volume"])
        except ValueError:
            messagebox.showerror("手数不对", "请填写整数，例如 1。")
            return
        if volume <= 0:
            messagebox.showerror("手数不对", "手数要大于 0。")
            return

        action = action_taken["action"]
        sizer = PositionSizer(self.cfg)
        multiplier = float(sizer.multiplier_for(symbol))

        stop_price = 0.0
        stop_raw = action_taken.get("stop", "").strip()
        if stop_raw:
            try:
                stop_price = float(stop_raw)
            except ValueError:
                messagebox.showerror("止损价不对", "请填写数字，或留空。")
                return
            if stop_price < 0:
                messagebox.showerror("止损价不对", "止损价不能是负数。")
                return

        if action in ("买多", "卖空"):
            side = "long" if action == "买多" else "short"
            side_word = "多" if side == "long" else "空"

            stop_hint = f"，策略止损 {stop_price:g}" if stop_price > 0 else "（不填止损，只靠反向信号和收盘清仓）"
            if not messagebox.askyesno(
                "确认手工开仓",
                f"手工录入：{symbol} {side_word}{volume}手，开仓价 {price:g}{stop_hint}。\n不会去点交易软件。\n\n确定？",
            ):
                return

            if bot is not None and getattr(bot, "strategy", None) is not None:
                opened, balance = bot.manual_open(
                    symbol, side, volume, price, initial_stop=stop_price,
                )
            else:
                opened, balance = manual_open_without_bot(
                    self.root_dir,
                    symbol=symbol,
                    side=side,
                    volume=volume,
                    entry_price=price,
                    multiplier=multiplier,
                    initial_capital=float(self.cfg.get("risk", {}).get("capital") or 0),
                    initial_stop=stop_price,
                )

            if opened is None:
                messagebox.showerror(
                    "录入失败",
                    f"{symbol} 不在程序关注列表里，或已有同方向仓位。\n请先清掉程序账上的旧仓再录入。",
                )
                return

            if balance is not None:
                if bot is None or getattr(bot, "strategy", None) is None:
                    write_capital_to_config(balance)
                self._apply_book_balance(balance)

            self._refresh_signal_view()
            self._refresh_status_cards()
            shown = self._position_card_text(None if bot is None else bot.status_snapshot())
            stop_note = f"，策略止损 {stop_price:g}" if stop_price > 0 else ""
            self._append_log(
                f"手工录入开仓：{symbol} {side_word}{volume}手，开仓价 {price:g}{stop_note}。当前持仓：{shown}"
            )
            messagebox.showinfo(
                "已录入",
                f"已把 {symbol} {side_word} {volume} 手记成开仓，开仓价 {price:g}{stop_note}。\n没有去点交易软件。",
            )
            self._maybe_release_intent_halt(
                bot, symbol, "buy_open" if side == "long" else "sell_open", volume
            )

        elif action == "平仓":
            if not messagebox.askyesno(
                "确认手工平仓",
                f"手工录入平仓：{symbol}，平仓价 {price:g}。\n"
                f"会按程序账上 {symbol} 的持仓手数平仓，盈亏按开仓价算。\n不会去点交易软件。\n\n确定？",
            ):
                return

            if bot is not None and getattr(bot, "strategy", None) is not None:
                closed, balance = bot.manual_close(symbol, price)
            else:
                closed, balance = manual_close_without_bot(
                    self.root_dir,
                    symbol=symbol,
                    exit_price=price,
                    multiplier=multiplier,
                    initial_capital=float(self.cfg.get("risk", {}).get("capital") or 0),
                )

            if closed is None:
                messagebox.showinfo("已经是空仓", f"{symbol} 在程序账上已经没有仓了。")
                return

            direction = "多" if closed["side"] == "long" else "空"
            sign = 1.0 if closed["side"] == "long" else -1.0
            pnl = (price - float(closed["entry_price"])) * sign * multiplier * int(closed["volume"])

            if balance is not None:
                if bot is None or getattr(bot, "strategy", None) is None:
                    write_capital_to_config(balance)
                self._apply_book_balance(balance)

            self._refresh_signal_view()
            self._refresh_status_cards()
            shown = self._position_card_text(None if bot is None else bot.status_snapshot())
            self._append_log(
                f"手工录入平仓：{symbol} {direction}{closed['volume']}手，平仓价 {price:g}，"
                f"盈亏 {pnl:+.0f} 元。当前持仓：{shown or format_position_text({})}"
            )
            messagebox.showinfo(
                "已录入",
                f"已按 {price:g} 把 {symbol} {direction} {closed['volume']} 手记成平仓。\n"
                f"这笔盈亏 {pnl:+.0f} 元。没有去点交易软件。",
            )
            self._maybe_release_intent_halt(bot, symbol, "close", int(closed["volume"]))


    def _on_order_simulator(self) -> None:
        """打开下单模拟器。点击走启动常驻的同一套代码，价格按窗口里填的限价。"""
        from tkinter import messagebox

        if self.worker.running:
            messagebox.showwarning("请先停止", "请先停止常驻扫描，再打开下单模拟器，避免两处抢鼠标。")
            return
        from src.ui.order_simulator import open_order_simulator

        symbol = self._watch_symbols[0] if self._watch_symbols else "bu2612"
        open_order_simulator(self, self.cfg, symbol, self._append_log, self._bring_self_front)


    def _on_pick_coords(self) -> None:
        """打开坐标标定小工具。打包后的 exe 会再开一个自己，只弹标定窗口。"""
        from src.ui.coord_picker import picker_launch_command

        self._append_log("正在打开坐标标定窗口…")
        command = picker_launch_command()

        def _run() -> None:
            # python.exe 会带出一个黑框。pythonw 和打包后的程序本身没有黑框。
            launcher_name = Path(command[0]).name.lower()
            flags = 0
            if os.name == "nt" and launcher_name not in {"pythonw.exe", "auto-trading.exe"}:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            code = subprocess.call(command, creationflags=flags)
            self.after(
                0,
                lambda: self._append_log(
                    f"坐标标定结束 exit={code}。如果这是另一块屏幕，点「保存当前坐标」留一份，再改成家里或笔记本。"
                ),
            )

        threading.Thread(target=_run, daemon=True).start()


    def _post_to_main(self, callback: Callable[[], None]) -> None:
        """
        后台线程安全地把回调排到 Tk 主线程队列。

        self.after(0, ...) 官方就是线程安全的；唯一风险是窗口或其中某个子控件已
        destroy 后，排队的 lambda 才被主线程取出来执行，此时对已销毁控件调
        configure/insert 等会抛 TclError: invalid command name "...".

        这里除了吞 self.after 自己可能抛的异常，还在执行期再包一层，把「已销毁
        控件」的 TclError 也吞掉——后台线程慢半拍、弹窗先关了都算正常边界情况，
        不值得写 ERROR 到日志。
        """

        def _safe() -> None:
            try:
                callback()
            except tk.TclError:
                # 99% 的情况是 "invalid command name '.!ctktoplevel...!label'"，
                # 也就是 callback 闭包里抓着某个已经被 destroy 的子控件不放
                # （比如手工平仓弹窗已关、后台线程才跑完推荐止损计算）。
                # 静默丢弃即可，不要把正常边界情况写成 ERROR。
                pass
            except Exception:  # noqa: BLE001
                # 其他真正的 bug 还是写日志，别让它静默。
                logger.exception("_post_to_main 回调异常")

        try:
            self.after(0, _safe)
        except tk.TclError:
            # self 自己已经 destroy，after 队列也没意义了。
            pass
        except Exception:  # noqa: BLE001
            pass


    def _on_close(self) -> None:
        self._closing = True
        stop_event = getattr(self, "_tick_download_stop", None)
        if stop_event is not None:
            stop_event.set()
        try:
            from src.ui.sim_counter import shutdown_owned_counter

            shutdown_owned_counter(self)
        except Exception as exc:  # noqa: BLE001 - 关窗不能因柜台回收失败被拦住
            logger.debug("关闭运行页时回收自测柜台失败：{}", exc)
        recorder = getattr(self, "_tick_recorder", None)
        if recorder is not None:
            try:
                recorder.stop(timeout=3)
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭窗口时停止 tick 采集失败：{}", exc)
            self._tick_recorder = None
        for name in ("_tick_after_id", "_log_after_id", "_login_tick_after_id"):
            after_id = getattr(self, name, None)
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, name, None)
        if self.worker.running:
            bot = getattr(self.worker, "bot", None)
            snap = self.worker.status_snapshot() or {}
            from src.utils.state_store import format_position_text

            held = format_position_text(snap.get("positions"))
            work = False
            trader = getattr(bot, "trader", None)
            if trader is not None and hasattr(trader, "has_working_order"):
                try:
                    work = bool(trader.has_working_order())
                except Exception:  # noqa: BLE001
                    work = True
            if held != "空仓" or work:
                extra = "还有未完成委托。" if work else ""
                if not messagebox.askyesno(
                    "确认关闭",
                    f"当前仍有持仓：{held}\n{extra}"
                    "关闭后本程序不再执行止损，持仓只能由柜台或人工处理。\n\n确定要关闭吗？",
                ):
                    self._resume_ui_timers()
                    return
            ok = self.worker.stop(wait=True, timeout=15.0)
            if not ok:
                if not messagebox.askyesno(
                    "后台没有按时退出",
                    "后台线程 15 秒内没有退出，强制关闭可能导致成交未记账或状态未落盘。\n"
                    "仍要强制关闭吗？",
                    default="no",
                ):
                    self._resume_ui_timers()
                    return
                try:
                    from src.ops.alerts import emit_alert

                    emit_alert("force_close", "关窗时后台线程未退出，用户选择强制关闭", level="error")
                except Exception:  # noqa: BLE001
                    pass
                if bot is not None and hasattr(bot, "note_force_close"):
                    try:
                        bot.note_force_close()
                    except Exception:  # noqa: BLE001
                        pass
        from src.ui.sim_counter import shutdown_owned_counter

        shutdown_owned_counter(self)
        self.destroy()

    def _resume_ui_timers(self) -> None:
        """用户取消关闭：恢复刚才为关窗停掉的界面定时器。"""
        self._closing = False
        if getattr(self, "_tick_after_id", None) is None:
            self._tick_after_id = self.after(500, self._tick)
        if getattr(self, "_log_after_id", None) is None:
            self._log_after_id = self.after(100, self._drain_logs)
        if getattr(self, "_login_tick_after_id", None) is None:
            self._login_tick_after_id = self.after(20000, self._tick_login_schedule)


    def report_callback_exception(self, exc, val, tb) -> None:
        """
        界面回调里的异常也写进日志文件。

        默认实现只把 traceback 打到 stderr；用 pythonw 或双击 exe 启动时看不见，
        控制台一关就没了。这里补一份到 logs，下次出错能直接翻日志定位。
        """
        import traceback

        text = str(val or "")
        # CustomTkinter 标签被挤得太窄时会画圆角失败。不影响下单，不当成程序坏了。
        if "wrong # coordinates" in text:
            logger.debug("界面绘制跳过（窗口太窄）: {}", text)
            return
        # 切换页签或重建列表时，CustomTkinter 的延时重绘可能落在已销毁的控件上。
        # 控件已经没了，这次重绘本来就没意义，不当成程序坏了。
        if "invalid command name" in text:
            logger.debug("界面控件已销毁，跳过这次重绘: {}", text)
            return
        detail = "".join(traceback.format_exception(exc, val, tb))
        logger.error("界面回调异常:\n{}", detail)
        try:
            self._append_log(f"界面出错：{val}（完整堆栈已写入 logs）")
        except Exception:  # noqa: BLE001
            pass

