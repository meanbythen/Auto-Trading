"""Trading-review tab construction and report generation."""

from __future__ import annotations

from loguru import logger
from src.ui import theme as T
import customtkinter as ctk
import threading


class ReviewTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_review_tab(self, tab: ctk.CTkFrame) -> None:
        """
        收盘复盘：用当天真实行情检验策略有效性、稳定性、可信度，并给出缺陷与改进建议。
        原理：和回测页一样左选条件、右看长文；生成放后台，避免拉行情时界面卡住。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="收盘复盘", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="用当天真实行情给策略打分：有效性、稳定性、可信度。会算 MAE/MFE/R、对照挂机记录，标出缺陷，并和近几日复盘、最近回测对照。用来补充历史回测与策略优化。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        self._date_row(box, "复盘日期", self.rv_date_var, hint="点日历选择，默认今天", allow_empty=False)
        # 和配置页关注品种用同一份合约，不用再手填代码。
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.rv_symbol_var.get() not in symbol_values:
            self.rv_symbol_var.set(symbol_values[0])
        self.rv_symbol_menu = self._option_row(box, "品种", self.rv_symbol_var, symbol_values, width=160)
        ctk.CTkLabel(
            box,
            text="和配置页的关注品种是同一份。要改成配置页当前选中的合约，点下面「按配置页重新填入」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._option_row(box, "看哪段", self.rv_scope_var, ["日盘", "夜盘", "前夜盘+日盘"], width=180)
        self._timeframe_row(box, "信号周期", self.rv_tf_var, self.rv_tf_custom_var, width=140)
        self._timeframe_row(box, "止损周期", self.rv_trail_var, self.rv_trail_custom_var, width=140)
        from src.review.replay import ENGINE_BY_LABEL

        self._option_row(
            box,
            "策略对照",
            self.rv_engine_var,
            list(ENGINE_BY_LABEL.keys()),
            width=200,
        )

        ctk.CTkLabel(
            left,
            text="当日基本面补充（可选）",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=4, pady=(8, 2))
        ctk.CTkLabel(
            left,
            text="自动拉的仓单/基差/快讯经常不全。按下面清单打开网站核对后，把一两句贴进框里。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))
        self.rv_note_box = ctk.CTkTextbox(
            left, height=90, font=T.FONT_BODY, fg_color=T.BG_ELEVATED, text_color=T.TEXT, corner_radius=8
        )
        self.rv_note_box.pack(fill="x", padx=4, pady=(0, 4))
        # 随品种变化的推荐来源；换合约时刷新，避免一直显示沥青说明。
        self.rv_sources_label = ctk.CTkLabel(
            left,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
            anchor="w",
        )
        self.rv_sources_label.pack(anchor="w", fill="x", padx=4, pady=(0, 8))
        self.rv_symbol_var.trace_add("write", lambda *_: self._refresh_review_sources_hint())
        self._refresh_review_sources_hint()

        ctk.CTkButton(
            left,
            text="生成复盘",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_review,
        ).pack(fill="x", padx=4, pady=(4, 8))
        ctk.CTkButton(
            left,
            text="保存当天基线",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_save_baseline,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="把这一天「策略会不会开、程序有没有下单、账本有没有记」收成一份对照。以后改策略可以拿同一天重跑，看出信号笔数有没有变。不是买卖点。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))
        ctk.CTkButton(
            left,
            text="生成上线评审单",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_release_sheet,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="一页看完数据门禁、样本外、镜像差异率、仿真滑点。签字只写「允许小资金挂机」或「继续观察」，不改配置，也不自动开仓。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))
        ctk.CTkButton(
            left,
            text="按配置页重新填入",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._fill_review_from_config,
        ).pack(fill="x", padx=4, pady=(0, 8))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="复盘报告", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.rv_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.rv_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.rv_result_box.insert(
            "end",
            "选好日期和品种后点「生成复盘」。\n"
            "报告开头是成绩单（有效性/稳定性/可信度），后面依次是：\n"
            "盘面 → 技术 → 基本面 → 策略对照 → 信号质量(MAE/MFE/R) →\n"
            "真实记录 → 信号与实盘对照 → 缺陷与改进 → 近几日/回测基准 → 综合。\n",
        )


    def _fill_review_from_config(self) -> None:
        """把配置页当前合约和周期抄到复盘页。不改复盘日期，也不写文件。"""
        symbol = self._config_symbol_now()
        self._sync_symbol_choice("rv_symbol_menu", self.rv_symbol_var, prefer=symbol, force=True)
        self._set_period_vars(self._period_code(self.tf_var, self.tf_custom_var), self.rv_tf_var, self.rv_tf_custom_var)
        self._set_period_vars(
            self._period_code(self.trail_var, self.trail_custom_var),
            self.rv_trail_var,
            self.rv_trail_custom_var,
        )
        shown = self.rv_symbol_var.get().strip() or "（还没有）"
        self._refresh_review_sources_hint()
        self._append_log(f"已按配置页填好复盘的品种和周期，品种 {shown}")


    def _review_symbol_now(self) -> str:
        """复盘页当前品种；占位文字「（还没有）」当空处理。"""
        symbol = str(self.rv_symbol_var.get() or "").strip()
        return "" if symbol in {"", "（还没有）"} else symbol


    def _on_release_sheet(self) -> None:
        """生成上线评审单：五项证据一页，签字另存，不改任何交易参数。"""
        from tkinter import messagebox

        from src.review.release_sheet import build_release_sheet

        try:
            sheet = build_release_sheet(self.root_dir, self.cfg, symbol=self._review_symbol_now())
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成上线评审单失败")
            messagebox.showerror("生成失败", f"评审单没生成出来：{exc}")
            return
        self._show_release_sheet_dialog(sheet)


    def _show_release_sheet_dialog(self, sheet) -> None:
        """预览评审单；只有填了签字人才写 release_sheet.json。"""
        from tkinter import messagebox

        from src.review.release_sheet import (
            DECISIONS,
            format_release_sheet,
            save_release_sheet,
            sign_release_sheet,
        )

        dialog = ctk.CTkToplevel(self)
        dialog.title("上线评审单")
        dialog.geometry("780x720")
        dialog.minsize(640, 520)
        dialog.transient(self)
        dialog.grab_set()

        preview = ctk.CTkTextbox(
            dialog, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, wrap="word"
        )
        preview.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        preview.insert("end", format_release_sheet(sheet))
        preview.configure(state="disabled")

        form = ctk.CTkFrame(dialog, fg_color="transparent")
        form.pack(fill="x", padx=12)
        name_var = ctk.StringVar()
        decision_var = ctk.StringVar(value=DECISIONS[1])
        note_var = ctk.StringVar()
        ctk.CTkLabel(form, text="签字人", text_color=T.TEXT_DIM).pack(side="left")
        ctk.CTkEntry(form, textvariable=name_var, width=120, placeholder_text="姓名").pack(
            side="left", padx=(4, 14)
        )
        ctk.CTkLabel(form, text="决定", text_color=T.TEXT_DIM).pack(side="left")
        ctk.CTkOptionMenu(
            form, variable=decision_var, values=list(DECISIONS), width=150
        ).pack(side="left", padx=(4, 14))
        ctk.CTkEntry(form, textvariable=note_var, placeholder_text="备注（可空）").pack(
            side="left", fill="x", expand=True
        )

        def refresh() -> None:
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("end", format_release_sheet(sheet))
            preview.configure(state="disabled")
            self.rv_result_box.delete("1.0", "end")
            self.rv_result_box.insert("end", format_release_sheet(sheet))

        def on_save() -> None:
            path = save_release_sheet(self.root_dir, format_release_sheet(sheet))
            self._append_log(f"上线评审单已保存：{path}")
            messagebox.showinfo("已保存", f"评审单已写到：\n{path}", parent=dialog)

        def on_sign() -> None:
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("还差签字人", "请先填签字人名字。", parent=dialog)
                return
            if sheet.verdict.startswith("不通过") and not messagebox.askyesno(
                "评审没通过",
                "这份评审单有「不通过」项。仍要签字留档吗？签字不会改配置或放宽风控。",
                parent=dialog,
            ):
                return
            try:
                sign_release_sheet(
                    self.root_dir,
                    sheet,
                    signed_by=name,
                    decision=decision_var.get(),
                    note=note_var.get(),
                )
            except ValueError as exc:
                messagebox.showwarning("证据未齐", str(exc), parent=dialog)
                return
            refresh()
            self._append_log(
                f"上线评审单已签字：{name} / {decision_var.get()}（只留档，不改配置）"
            )

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=(8, 12))
        ctk.CTkButton(buttons, text="只保存", height=34, command=on_save).pack(side="left")
        ctk.CTkButton(
            buttons,
            text="签字并保存",
            height=34,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=on_sign,
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            buttons, text="关闭", height=34, fg_color=T.BG_ELEVATED, command=dialog.destroy
        ).pack(side="right")


    def _refresh_review_sources_hint(self) -> None:
        """
        按复盘页当前品种刷新「推荐手动来源」小字。
        原理：自动仓单/快讯经常不全；换合约时换成该品种该看的网站清单。
        """
        label = getattr(self, "rv_sources_label", None)
        if label is None:
            return
        symbol = (self.rv_symbol_var.get() or "").strip()
        if not symbol or symbol == "（还没有）":
            label.configure(text="请先在配置页关注品种里选一个合约，再看推荐来源。")
            return
        from src.review.sources import sources_hint_lines

        label.configure(text="\n".join(sources_hint_lines(symbol)))


    def _on_review(self) -> None:
        """后台生成收盘复盘，结果写在「复盘」页，并保存到 data/reviews。"""
        from datetime import date
        from tkinter import messagebox

        from src.review.replay import ENGINE_BY_LABEL, engine_from_label

        raw_day = self.rv_date_var.get().strip()
        try:
            trade_date = date.fromisoformat(raw_day)
        except ValueError:
            messagebox.showerror("日期不对", "请按 YYYY-MM-DD 填写，例如 2026-10-14。")
            return
        label = self.rv_engine_var.get().strip()
        if label not in ENGINE_BY_LABEL and engine_from_label(label) is None:
            messagebox.showerror("策略不对", "请重新选择「策略对照」。")
            return
        engine = ENGINE_BY_LABEL[label] if label in ENGINE_BY_LABEL else engine_from_label(label)
        symbol = self.rv_symbol_var.get().strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加要复盘的合约。")
            return
        scope = self.rv_scope_var.get().strip() or "前夜盘+日盘"
        note = self.rv_note_box.get("1.0", "end").strip() if hasattr(self, "rv_note_box") else ""

        cfg = self._clone_cfg()
        st = cfg.setdefault("strategy", {})
        st["timeframe"] = self._period_code(self.rv_tf_var, self.rv_tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.rv_trail_var, self.rv_trail_custom_var)
        st["symbols"] = [symbol]
        if engine:
            st["engine"] = engine

        self._open_tab("复盘")
        self._set_result_text(
            self.rv_result_box,
            "正在生成复盘…\n需要拉取行情，如果还查库存和快讯，可能要半分钟。\n",
        )
        self._append_log(f"开始复盘 {symbol} {raw_day} {scope} | {label}")

        def _run() -> None:
            try:
                from src.review import build_review

                text = build_review(
                    cfg,
                    symbol=symbol,
                    trade_date=trade_date,
                    scope=scope,
                    engine=engine,
                    user_note=note,
                    root=self.root_dir,
                )
                # 尾部附上实盘滑点校准段：真实成交攒出来的滑点分布，回测该填几跳看它。
                try:
                    from src.account.slippage_log import quality_text, summarize_text

                    text = (
                        f"{text}\n\n{summarize_text(self.root_dir)}"
                        f"\n\n{quality_text(self.root_dir, self.cfg)}"
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("滑点汇总失败，已忽略", exc_info=True)
                try:
                    from src.trader.order_timeline import format_recent_text

                    text = f"{text}\n\n{format_recent_text(self.root_dir)}"
                except Exception:  # noqa: BLE001
                    logger.debug("订单时间线读取失败，已忽略", exc_info=True)
                self._post_to_main(lambda t=text: self._set_result_text(self.rv_result_box, t))
                self._post_to_main(lambda: self._append_log(f"复盘完成 {symbol} {raw_day}"))
            except Exception as e:  # noqa: BLE001
                logger.exception("复盘失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"复盘失败: {err}"))
                self._post_to_main(lambda m=err: self._set_result_text(self.rv_result_box, f"复盘失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("复盘失败", m))

        threading.Thread(target=_run, daemon=True).start()

    def _on_save_baseline(self) -> None:
        """把当天复盘对照写成可重跑的基线文件。"""
        from datetime import date
        from tkinter import messagebox

        from src.review.replay import ENGINE_BY_LABEL, engine_from_label
        from src.review.system_baseline import CATEGORY_LABELS, load_pack, save_current_baseline

        raw_day = self.rv_date_var.get().strip()
        try:
            trade_date = date.fromisoformat(raw_day)
        except ValueError:
            messagebox.showerror("日期不对", "请按 YYYY-MM-DD 填写，例如 2026-10-14。")
            return
        symbol = self.rv_symbol_var.get().strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加要复盘的合约。")
            return
        label = self.rv_engine_var.get().strip()
        engine = ENGINE_BY_LABEL.get(label) if label in ENGINE_BY_LABEL else engine_from_label(label)
        scope = self.rv_scope_var.get().strip() or "前夜盘+日盘"
        cfg = self._clone_cfg()
        st = cfg.setdefault("strategy", {})
        st["timeframe"] = self._period_code(self.rv_tf_var, self.rv_tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.rv_trail_var, self.rv_trail_custom_var)
        st["symbols"] = [symbol]
        if engine:
            st["engine"] = engine
        self._append_log(f"开始保存基线 {symbol} {raw_day}")
        self._set_result_text(self.rv_result_box, "正在保存当天基线…\n会再对照一次策略重放、事件和账本。\n")

        def work() -> None:
            try:
                path = save_current_baseline(
                    cfg,
                    symbol=symbol,
                    trade_date=trade_date,
                    scope=scope,
                    engine=engine,
                    root=self.root_dir,
                )
                pack = load_pack(path)
                case = (pack.get("cases") or [{}])[0]
                cats = [
                    CATEGORY_LABELS.get(str(code), str(code))
                    for code in (case.get("categories") or [])
                ]
                text = (
                    f"已保存当天基线：{path}\n"
                    f"合约 {case.get('symbol')}  策略 {case.get('engine') or '只看盘面'}  {case.get('trade_date')}\n"
                    f"重放开仓 {case.get('replay_opens')} 笔；事件 {case.get('event_orders')} 条；账本 {case.get('book_fills')} 笔。\n"
                    f"分类：{'、'.join(cats) or '—'}\n"
                    "这是对照样本，不是买卖建议。以后改策略可拿同一天重跑，看笔数有没有变。"
                )
                self._post_to_main(lambda t=text: self._set_result_text(self.rv_result_box, t))
                self._post_to_main(lambda: self._append_log(f"基线已保存 {path}"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("保存基线失败")
                self._post_to_main(lambda m=str(exc): messagebox.showerror("保存基线失败", m))

        threading.Thread(target=work, daemon=True).start()

