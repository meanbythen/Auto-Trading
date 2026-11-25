"""数据管理页：本地 K 线库的建立、补数、库存与质量检查。"""

from __future__ import annotations

from datetime import datetime
import threading
from pathlib import Path

import customtkinter as ctk
from loguru import logger

from src.data.periods import period_label
from src.ui import theme as T
from src.ui.datetime_pickers import DateTimeField

# 自动采集启动失败（多为锁文件撞车）后按间隔重试，别一次失败就整场不采。
_TICK_AUTOSTART_RETRY_DELAY_MS = 30_000
_TICK_AUTOSTART_RETRY_LIMIT = 6


class DataTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_data_tab(self, tab: ctk.CTkFrame) -> None:
        """数据管理：按时段读同花顺图，维护本地 K 线库。"""
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(
            left, text="数据管理", font=T.FONT_TITLE, text_color=T.TEXT
        ).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text=(
                "本地K线和tick的增加、删除、查看，不修改已存储基础数据。"
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        symbols = list(self._watch_symbols) or ["（还没有）"]
        if self.dm_symbol_var.get() not in symbols:
            self.dm_symbol_var.set(symbols[0])
        self._option_row(box, "合约", self.dm_symbol_var, symbols, width=180)
        ctk.CTkLabel(
            box,
            text="和配置页的关注品种是同一份。期货通主图请先点成同一个合约。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        self._timeframe_row(box, "K线周期", self.dm_period_var, self.dm_period_custom_var, width=140)
        ctk.CTkLabel(
            box,
            text="期货通主图的周期也要点成同一档，程序读完会校验；不一致时不存库。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        range_row = ctk.CTkFrame(box, fg_color="transparent")
        range_row.pack(fill="x", pady=(2, 4))
        start_col = ctk.CTkFrame(range_row, fg_color="transparent")
        start_col.pack(side="left", anchor="n")
        ctk.CTkLabel(
            start_col, text="开始时间", font=T.FONT_BODY, text_color=T.TEXT_DIM
        ).pack(anchor="w", pady=(0, 2))
        self.dm_start_field = DateTimeField(
            start_col,
            allow_empty=False,
            date_var=self.dm_start_date_var,
            time_var=self.dm_start_time_var,
        )
        self.dm_start_field.frame.pack(anchor="w")

        end_col = ctk.CTkFrame(range_row, fg_color="transparent")
        end_col.pack(side="left", anchor="n", padx=(24, 0))
        ctk.CTkLabel(
            end_col, text="结束时间", font=T.FONT_BODY, text_color=T.TEXT_DIM
        ).pack(anchor="w", pady=(0, 2))
        self.dm_end_field = DateTimeField(
            end_col,
            allow_empty=False,
            date_var=self.dm_end_date_var,
            time_var=self.dm_end_time_var,
        )
        self.dm_end_field.frame.pack(anchor="w")

        self.dm_estimate = ctk.CTkLabel(
            box,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        )
        self.dm_estimate.pack(anchor="w", pady=(6, 8))
        for var in (
            self.dm_period_var,
            self.dm_period_custom_var,
            self.dm_start_date_var,
            self.dm_start_time_var,
            self.dm_end_date_var,
            self.dm_end_time_var,
        ):
            try:
                var.trace_add("write", lambda *_: self._dm_refresh_estimate())
            except Exception:  # noqa: BLE001
                pass
        self._dm_refresh_estimate()

        ths_row = ctk.CTkFrame(box, fg_color="transparent")
        ths_row.pack(fill="x", pady=(0, 4))
        self.dm_scrape_btn = ctk.CTkButton(
            ths_row,
            text="从同花顺读取K线",
            height=36,
            command=self._on_scrape_ths_chart,
        )
        self.dm_scrape_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.dm_stop_btn = ctk.CTkButton(
            ths_row,
            text="停止读图",
            height=36,
            width=96,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_stop_ths_chart,
            state="disabled",
        )
        self.dm_stop_btn.pack(side="left")
        self.dm_progress = ctk.CTkProgressBar(
            box, height=8, progress_color=T.ACCENT, fg_color=T.BORDER
        )
        self.dm_progress.set(0)
        self.dm_progress.pack(fill="x", pady=(4, 2))
        self.dm_progress_label = ctk.CTkLabel(
            box,
            text="读图进度会显示在这里",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        )
        self.dm_progress_label.pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(
            left,
            text=(
                "打开合约K线主图，主图选好合约和周期。结束时间是现在，程序自动推到最新一根；"
                "补历史时，手动把光标停到结束时间K线，再点按钮（点完3秒内把鼠标放回主图）。"
                "按ESC或点「停止读图」可停，已读到的会先存下来。"
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))

        ctk.CTkLabel(
            left,
            text="天勤 tick",
            font=T.FONT_TITLE,
            text_color=T.TEXT,
        ).pack(anchor="w", padx=4, pady=(8, 4))
        auto_row = ctk.CTkFrame(left, fg_color="transparent")
        auto_row.pack(fill="x", padx=4, pady=(0, 2))
        ctk.CTkSwitch(
            auto_row,
            text="启动程序时自动采集",
            variable=self.tick_auto_var,
            progress_color=T.OK,
            font=T.FONT_SMALL,
            text_color=T.TEXT,
            command=self._on_toggle_tick_auto,
        ).pack(side="left")
        ctk.CTkLabel(
            auto_row,
            text="只影响下次启动；本次采集用下面的按钮",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            left,
            textvariable=self.tick_status_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))
        tick_row = ctk.CTkFrame(left, fg_color="transparent")
        tick_row.pack(fill="x", padx=4, pady=(0, 4))
        self.tick_toggle_btn = ctk.CTkButton(
            tick_row,
            text="启动 tick 采集",
            height=34,
            command=self._on_toggle_tick_record,
        )
        self.tick_toggle_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(
            tick_row,
            text="合成K线",
            height=34,
            width=110,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_synth_tick_bars,
        ).pack(side="left")
        ctk.CTkButton(
            left,
            text="下载历史 tick",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_download_history_tick,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            left,
            text="补齐历史 tick（按回看天数）",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_backfill_history_tick,
        ).pack(fill="x", padx=4, pady=(0, 4))
        self.tick_backfill_label = ctk.CTkLabel(
            left,
            textvariable=self.tick_backfill_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        )
        self.tick_backfill_label.pack(anchor="w", padx=4, pady=(0, 4))
        ctk.CTkButton(
            left,
            text="查看本地tick库存",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_show_tick_inventory,
        ).pack(fill="x", padx=4, pady=(0, 4))
        tick_io = ctk.CTkFrame(left, fg_color="transparent")
        tick_io.pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            tick_io,
            text="查看这段tick",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_inspect_ticks,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(
            tick_io,
            text="删除本地tick",
            height=34,
            width=120,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_purge_ticks,
        ).pack(side="left")
        ctk.CTkLabel(
            left,
            textvariable=self.tick_inventory_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkLabel(
            left,
            text=(
                "合约名单空，自动用配置页关注品种。打开上面的「启动程序时自动采集」"
                "（写入 data.tick.enabled）后，程序启动会自动采集："
                "开盘前几十秒连接、收盘后断开并合成K线，非交易时段不占天勤连接。"
                "「补齐历史tick」按回看天数一键补，自动暂停实时采集、可断点续传；"
                "盘中暂停后收盘自动续传，再点一次按钮可停止。"
                "「下载历史tick」需天勤专业版/试用。tick只增删查、不修改；"
                "「删除本地tick」清掉乱段后重新下载。"
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))

        ctk.CTkLabel(
            left,
            text="行情数据源",
            font=T.FONT_TITLE,
            text_color=T.TEXT,
        ).pack(anchor="w", padx=4, pady=(8, 4))
        quote_box = self._card(left)
        self._entry_row(
            quote_box,
            "天勤行情账号",
            self.tq_user_var,
            hint="TqSdk 手机号/账号；只拉行情，和交易账号无关",
            width=210,
        )
        self.tq_pwd_entry = self._password_row(
            quote_box, "天勤行情密码", self.tq_password_var, self._show_tq_password
        )
        ctk.CTkLabel(
            quote_box,
            text=(
                "数据源与交易账号完全解耦：换行情源不影响下单。"
                "「交易配置」里行情来源选天勤时才会登录天勤。"
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(4, 8))
        ctk.CTkButton(
            quote_box,
            text="保存行情源",
            height=34,
            command=self._on_save,
        ).pack(fill="x", pady=(0, 4))

        ctk.CTkLabel(
            left, text="本地库存", font=T.FONT_TITLE, text_color=T.TEXT
        ).pack(anchor="w", padx=4, pady=(8, 4))
        inv_row = ctk.CTkFrame(left, fg_color="transparent")
        ctk.CTkLabel(
            left,
            text="K 线和 tick 都只做增加、删除、查看。已经存下的基础数据不改；乱了就删掉再拉。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))
        inv_row.pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            inv_row,
            text="查看本地K线库存",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_show_kline_inventory,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(
            inv_row,
            text="删除本地K线",
            height=34,
            width=120,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_purge_local_klines,
        ).pack(side="left")
        ctk.CTkButton(
            left,
            text="清除拼合K线（保留1分钟/tick）",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_clear_rebuilt_klines,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            left,
            text="查看这根K线",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_inspect_local_kline,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            left,
            text="检查行情质量",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_check_quote_quality,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            left,
            text="持仓量覆盖审计",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_audit_oi_coverage,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text=(
                "「查看本地K线库存」直接打开Excel，查看「读取记录」。"
                "「查看这根K线」：选数据源、合约、周期、日期和时间，对照同花顺K线检查是否有误。"
                "K线质量差时，可按数据源、合约、时间段删掉再重新拉。"
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            right,
            text="读图与库存状态",
            font=T.FONT_TITLE,
            text_color=T.TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        self.dm_log_box = ctk.CTkTextbox(
            right,
            font=T.FONT_MONO,
            fg_color=T.BG,
            text_color=T.TEXT,
            wrap="word",
        )
        self.dm_log_box.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self._dm_write("本地 K 线库状态会显示在这里。")

    def _dm_write(self, text: str) -> None:
        """写进数据管理页自己的状态框，再记一条运行日志。"""
        box = getattr(self, "dm_log_box", None)
        if box is not None:
            box.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
            box.see("end")
        try:
            self._append_log(text)
        except Exception:  # noqa: BLE001
            logger.info(text)

    def _dm_progress_reset(self) -> None:
        """开读前把进度条清空。"""
        bar = getattr(self, "dm_progress", None)
        if bar is not None:
            bar.set(0)
        label = getattr(self, "dm_progress_label", None)
        if label is not None:
            label.configure(text="正在准备读图…")
        self._dm_progress_logged = -1

    def _dm_progress(self, read_n: int, total: int, latest: str = "") -> None:
        """刷新读图进度条，并隔几根往状态框写一行实时日志。"""
        total = max(1, int(total or 0))
        read_n = max(0, int(read_n))
        bar = getattr(self, "dm_progress", None)
        if bar is not None:
            bar.set(min(1.0, read_n / total))
        detail = f"已读 {read_n} / {total} 根" + (f" · 读到 {latest}" if latest else "")
        label = getattr(self, "dm_progress_label", None)
        if label is not None:
            label.configure(text=detail)
        logged = int(getattr(self, "_dm_progress_logged", -1))
        if read_n == 0 or read_n >= total or read_n - logged >= 5:
            self._dm_progress_logged = read_n
            self._dm_write(f"读图进度 {detail}")

    def _dm_period(self) -> str:
        return self._period_code(self.dm_period_var, self.dm_period_custom_var)

    def _dm_range(self) -> tuple[datetime, datetime]:
        """把两个日历控件收成开始、结束时间。没选或顺序反了直接报错。"""
        from src.ui.datetime_pickers import combine_datetime

        start_text = combine_datetime(self.dm_start_date_var.get(), self.dm_start_time_var.get())
        end_text = combine_datetime(self.dm_end_date_var.get(), self.dm_end_time_var.get())
        if not start_text or not end_text:
            raise ValueError("请把开始时间和结束时间都选上（日期 + 时 + 分）。")
        try:
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)
        except ValueError as exc:
            raise ValueError(f"时间格式看不懂：{exc}") from exc
        if start >= end:
            raise ValueError("开始时间必须早于结束时间。")
        return start, end

    def _dm_cap(self) -> int:
        """单次读图最多读多少根。配置没写时用 5000。"""
        ths_cfg = (self.cfg.get("data") or {}).get("ths_chart") or {}
        try:
            cap = int(ths_cfg.get("max_bars", 5000))
        except (TypeError, ValueError):
            cap = 5000
        return max(10, min(5000, cap))

    def _dm_refresh_estimate(self) -> None:
        """按当前周期和时段显示预计根数，以及单次上限。"""
        label = getattr(self, "dm_estimate", None)
        if label is None:
            return
        try:
            start, end = self._dm_range()
            from src.data.coverage import expected_bar_count

            need = expected_bar_count(start, end, self._dm_period())
        except Exception as exc:  # noqa: BLE001
            label.configure(text=str(exc))
            return
        cap = self._dm_cap()
        span = f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}"
        if need > cap:
            text = (
                f"{span} 约需 {need} 根，超过单次上限 {cap} 根。"
                f"这次读最近 {cap} 根；剩下的可以改时段分几次读，库会按时间接上。"
            )
        else:
            text = f"{span} 按{period_label(self._dm_period())}约需 {need} 根（单次上限 {cap} 根）。"
        label.configure(text=text)

    def _set_dm_scraping(self, busy: bool) -> None:
        """读图进行中：禁止再点读取，允许点停止。"""
        self._dm_scraping = bool(busy)
        scrape = getattr(self, "dm_scrape_btn", None)
        stop = getattr(self, "dm_stop_btn", None)
        if scrape is not None:
            scrape.configure(state="disabled" if busy else "normal")
        if stop is not None:
            stop.configure(state="normal" if busy else "disabled")

    def _on_escape_key(self, _event=None):
        """读同花顺图时 ESC 当停止。其它时候不拦。"""
        if getattr(self, "_dm_scraping", False):
            self._on_stop_ths_chart()
            return "break"
        return None

    def _on_stop_ths_chart(self) -> None:
        """点停止或按 ESC：读图循环下一拍就停，已读到的会存盘。"""
        from src.data.ths_chart import request_ths_stop

        if not getattr(self, "_dm_scraping", False):
            return
        request_ths_stop()
        self._dm_write("已请求停止读图。按 ESC 也可以。已读到的会先存下来。")

    def _dm_confirm(self) -> bool:
        """读图前请用户确认期货通已经打开。取消就不读。"""
        from tkinter import messagebox

        try:
            start, end = self._dm_range()
        except ValueError as exc:
            messagebox.showwarning("时间不对", str(exc))
            return False
        from src.data.ths_chart import _target_is_latest

        manual = not _target_is_latest(end, self._dm_period())
        if manual:
            guide = (
                f"目标时段：{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}\n"
                "结束时间在历史里：点确定后有 3 秒，请把鼠标移到主图上、"
                "把光标停在这个结束时间那根K线上，程序会先预读确认，"
                "再向右多读几根做接续余量，然后往回读。\n"
            )
        else:
            guide = (
                f"目标时段：{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}\n"
                "结束时间就是现在：点确定后先等 3 秒，程序会自己把光标推到最新一根。\n"
            )
        return bool(
            messagebox.askokcancel(
                "从同花顺读取K线",
                "请先打开期货通并进入合约的 K 线主图，"
                "主图选好这个合约和这个周期。\n"
                + guide
                + "读的时候请不要动鼠标键盘。按 ESC 或点「停止读图」可停。\n"
                "信息框顶部是这根K线的结束时间。不会点买卖。\n\n继续？",
            )
        )

    def _on_scrape_ths_chart(self) -> None:
        """按周期和时段读同花顺图，写本地库并更新库存 Excel。"""
        from tkinter import messagebox

        from src.data.kline import KlineProvider
        from src.data.ths_chart import ThsScrapeStopped

        symbol = (self.dm_symbol_var.get() or "").strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约。")
            return
        if getattr(self, "_dm_scraping", False):
            messagebox.showinfo("正在读图", "正在读同花顺图。要停请按 ESC 或点「停止读图」。")
            return
        try:
            start, end = self._dm_range()
        except ValueError as exc:
            messagebox.showwarning("时间不对", str(exc))
            return
        period = self._dm_period()
        cap = self._dm_cap()
        if not self._dm_confirm():
            return
        start_text = start.strftime("%Y-%m-%d %H:%M:%S")
        end_text = end.strftime("%Y-%m-%d %H:%M:%S")
        from src.data.ths_chart import _target_is_latest

        manual = not _target_is_latest(end, period)
        self._dm_progress_reset()
        self._set_dm_scraping(True)
        self._dm_write(
            f"准备读取 {symbol} {period_label(period)}：{start_text} ~ {end_text}，"
            f"单次上限 {cap} 根。"
            + (
                "3 秒后开始，请把光标停在结束时间那根K线上。"
                if manual
                else "3 秒后开始，请把主图放到前面。"
            )
        )

        def work() -> None:
            try:
                cfg = self._clone_cfg()
                data = cfg.setdefault("data", {})
                data["source"] = "ths"
                ths_cfg = dict(data.get("ths_chart") or {})
                ths_cfg.setdefault("ready_sec", 3)
                ths_cfg["max_bars"] = cap
                data["ths_chart"] = ths_cfg
                provider = KlineProvider(cfg)
                result = provider.import_ths_chart_batch(
                    symbol,
                    period,
                    start=start_text,
                    end=end_text,
                    limit=cap,
                    progress=lambda n, total, ts: self._post_to_main(
                        lambda n=n, total=total, ts=ts: self._dm_progress(n, total, ts)
                    ),
                )
                cover = result.get("coverage") or {}
                expected = int(cover.get("expected_n") or 0)
                missing = int(cover.get("missing_n") or 0)
                lines = [
                    f"读图完成：实读 {result.get('read_n', 0)} 根，"
                    f"{result.get('bar_from', '')} ~ {result.get('bar_to', '')}",
                    f"新增入库 {result.get('added', 0)} 根",
                ]
                if expected:
                    lines.append(f"目标时段应有 {expected} 根，还缺 {missing} 根")
                    ranges = cover.get("missing_ranges") or []
                    if ranges:
                        lines.append(
                            "缺口：" + "；".join(f"{a} ~ {b}（{n} 根）" for a, b, n in ranges)
                        )
                if result.get("inventory_path"):
                    lines.append(f"库存 Excel：{result['inventory_path']}")
                text = "\n".join(lines)
                self._post_to_main(lambda t=text: self._dm_write(t))
            except ThsScrapeStopped as exc:
                err = str(exc)
                self._post_to_main(lambda m=err: self._dm_write(m))
                self._post_to_main(lambda m=err: messagebox.showinfo("已停止", m))
            except Exception as exc:  # noqa: BLE001
                # Python 3.12 结束后会清掉 except 里的 exc，after 里再 str(exc) 会变成 NameError。
                err = str(exc)
                logger.exception("从同花顺图读取失败")
                self._post_to_main(lambda m=err: self._dm_write(f"读取失败：{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("没读到", m))
            finally:
                self._post_to_main(lambda: self._set_dm_scraping(False))

        threading.Thread(target=work, daemon=True).start()

    def _on_show_kline_inventory(self) -> None:
        """写出并直接打开 Excel 统计表，成功时不再弹提示框。"""
        from tkinter import messagebox

        from src.data.inventory import collect_kline_inventory, inventory_message, write_kline_inventory

        try:
            book = collect_kline_inventory(self.root_dir)
            path = write_kline_inventory(self.root_dir, book)
        except Exception as exc:  # noqa: BLE001
            logger.exception("查看本地K线库存失败")
            messagebox.showerror("打不开库存", str(exc))
            return
        opened = self._open_local_file(path)
        self._dm_write(f"本地K线库存 {inventory_message(book)} | {path}")
        if not opened:
            messagebox.showerror(
                "打不开统计表",
                f"表已经写好，但系统没能打开 Excel。\n请自己打开：\n{path}",
            )

    def _on_clear_rebuilt_klines(self) -> None:
        """一键清除 2 分钟及以上的拼合 K 线，保留 1 分钟、日线周线和 tick 原始数据。"""
        from tkinter import messagebox

        from src.data.inventory import preview_rebuilt_klines, purge_rebuilt_klines

        tick_store = str(((self.cfg.get("data") or {}).get("tick") or {}).get("bar_store") or "")
        try:
            seen = preview_rebuilt_klines(self.root_dir, tick_store=tick_store or None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("预览拼合K线失败")
            messagebox.showerror("打不开本地库", str(exc))
            return
        if not seen["clean_n"] and not seen["tick_n"] and not seen["csv_keys"]:
            messagebox.showinfo("没有可清的", "本地没有 2 分钟及以上的拼合K线。")
            return
        periods = "、".join(period_label(period) for period in seen["periods"]) or "无"
        ask = (
            f"将清除本地拼合K线：{periods}。\n\n"
            f"清洗库 {seen['clean_n']} 根，tick 合成库 {seen['tick_n']} 根。\n"
            "1 分钟 K 线、日线/周线、tick 原始数据都不会删除。\n\n"
            "清完后需要重新拉取或合成这些周期，并重跑回测。继续？"
        )
        if not messagebox.askokcancel("清除拼合K线", ask):
            return
        try:
            result = purge_rebuilt_klines(self.root_dir, tick_store=tick_store or None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("清除拼合K线失败")
            messagebox.showerror("清除失败", str(exc))
            return
        msg = (
            f"已清除拼合K线：清洗库 {result['clean_deleted']} 根，"
            f"tick 合成库 {result['tick_deleted']} 根。\n"
            "1 分钟 K 线和 tick 原始数据已保留。"
        )
        if result["csv_locked"]:
            msg += (
                "\n以下 CSV 正被占用，已标记跳过；关掉 Excel 后再清一次：\n"
                + "\n".join(result["csv_locked"])
            )
        self._dm_write(msg.replace("\n", " | "))
        messagebox.showinfo("已清除", msg)

    def _on_purge_local_klines(self) -> None:
        """弹出删除窗口：可按数据源、合约、周期、时间段删，先数再删。"""
        from tkinter import messagebox

        from src.data.periods import PERIOD_MENU, canonical_period
        from src.data.quote_check import BACKTEST_SOURCE_LABELS, normalize_source
        from src.data.inventory import preview_purge, purge_klines

        dialog = ctk.CTkToplevel(self)
        dialog.title("删除本地K线")
        dialog.geometry("520x760")
        dialog.minsize(480, 640)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="某一家质量差时，只删这一家，再重新拉。不会点买卖。\n"
            "至少选一个数据源，或填一个合约。时间段留空表示这一组全部时间。\n"
            "若 CSV 正用 Excel 打开，请先关掉，否则清洗库删了还会被旧表补回来。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=420,
        ).pack(anchor="w", padx=16, pady=(16, 10))

        source_var = ctk.StringVar(value="不限")
        ctk.CTkLabel(dialog, text="数据源").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog, variable=source_var, values=["不限", *list(BACKTEST_SOURCE_LABELS)], width=240
        ).pack(anchor="w", padx=16, pady=(0, 8))

        ctk.CTkLabel(dialog, text="合约（如 fu2701，留空=该数据源全部合约）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 8))
        current = (self.dm_symbol_var.get() or "").strip()
        if current and current != "（还没有）":
            symbol_entry.insert(0, current)

        period_var = ctk.StringVar(value="不限")
        ctk.CTkLabel(dialog, text="周期").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog, variable=period_var, values=["不限", *[x for x in PERIOD_MENU if x != "自定义"]], width=240
        ).pack(anchor="w", padx=16, pady=(0, 8))

        from src.ui.datetime_pickers import DateTimeField

        ctk.CTkLabel(dialog, text="从（可空：日历选日期，时分可选）").pack(anchor="w", padx=16)
        from_field = DateTimeField(dialog)
        from_field.frame.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(dialog, text="到（可空）").pack(anchor="w", padx=16)
        to_field = DateTimeField(dialog)
        to_field.frame.pack(anchor="w", padx=16, pady=(0, 8))

        layer_var = ctk.StringVar(value="两份都删")
        ctk.CTkLabel(dialog, text="删哪一份").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog,
            variable=layer_var,
            values=["两份都删", "只删清洗后（开仓看的）", "只删接口原样"],
            width=260,
        ).pack(anchor="w", padx=16, pady=(0, 10))

        hint = ctk.CTkLabel(dialog, text="", text_color=T.TEXT_DIM, wraplength=420, justify="left")
        hint.pack(anchor="w", padx=16, pady=(0, 8))

        def _args() -> dict:
            src_label = source_var.get().strip()
            source = None if src_label in {"", "不限"} else normalize_source(src_label)
            period_text = period_var.get().strip()
            period = None if period_text in {"", "不限"} else canonical_period(period_text)
            layer_label = layer_var.get()
            layers = "both"
            if "清洗" in layer_label:
                layers = "clean"
            elif "原样" in layer_label:
                layers = "raw"
            return {
                "source": source,
                "symbol": symbol_entry.get().strip() or None,
                "period": period,
                "ts_from": from_field.get() or None,
                "ts_to": to_field.get() or None,
                "layers": layers,
            }

        def _preview() -> None:
            try:
                seen = preview_purge(self.root_dir, **_args())
            except Exception as exc:  # noqa: BLE001
                hint.configure(text=str(exc))
                return
            hint.configure(
                text=f"将删除清洗后 {seen['clean_n']} 根、接口原样 {seen['raw_n']} 根。"
                "确认后不可恢复，请再点下面的删除。"
            )

        def _do_delete() -> None:
            try:
                seen = preview_purge(self.root_dir, **_args())
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("还不能删", str(exc), parent=dialog)
                return
            if seen["clean_n"] + seen["raw_n"] <= 0:
                messagebox.showinfo("没有可删的", "按现在的条件，库里没有匹配的K线。", parent=dialog)
                return
            if not messagebox.askokcancel(
                "确认删除",
                f"将删除清洗后 {seen['clean_n']} 根、接口原样 {seen['raw_n']} 根。\n"
                "删完若要再用，请重新拉这一家。不可恢复。",
                parent=dialog,
            ):
                return
            try:
                result = purge_klines(self.root_dir, **_args())
            except Exception as exc:  # noqa: BLE001
                logger.exception("删除本地K线失败")
                messagebox.showerror("删除失败", str(exc), parent=dialog)
                return
            msg = (
                f"已删除清洗后 {result['clean_deleted']} 根、接口原样 {result['raw_deleted']} 根。"
            )
            locked = list(result.get("csv_locked") or [])
            if locked:
                files = "\n".join(locked)
                msg += (
                    "\n\n有 CSV 正被 Excel 打开，没能一起改掉。\n"
                    "请先关掉这些表，再点一次删除（否则下次读图会把旧 CSV 补回库里）：\n"
                    f"{files}"
                )
            self._dm_write(f"删除本地K线 {msg.replace(chr(10), ' ')}")
            messagebox.showinfo("已删除" if not locked else "库已删，CSV 还被占用", msg, parent=dialog)
            if not locked:
                dialog.destroy()

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(8, 16))
        ctk.CTkButton(btn_row, text="先看要删多少", command=_preview, width=140).pack(side="left")
        ctk.CTkButton(btn_row, text="确认删除", command=_do_delete, width=140, fg_color="#8B2E2E").pack(
            side="right"
        )

    def _on_inspect_local_kline(self) -> None:
        """抽查某一根本地K线：选数据源、合约、周期、日期、时间，对照同花顺开高低收。"""
        from src.data.periods import PERIOD_MENU, canonical_period, period_label
        from src.data.quote_check import BACKTEST_SOURCE_LABELS, normalize_source
        from src.data.bar_inspect import format_kline_inspect, inspect_kline

        dialog = ctk.CTkToplevel(self)
        dialog.title("查看这根K线")
        dialog.geometry("520x800")
        dialog.minsize(480, 620)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="对照同花顺信息框时，开盘、最高、最低、收盘、成交量都能对。\n"
            "今结、涨跌、日增仓本地没有；持仓量只有同花顺读图存的那批才有。时间是这根K线的结束时刻。不下单。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=480,
        ).pack(anchor="w", padx=16, pady=(16, 10))

        default_src = (self.dm_source_var.get() or "新浪").strip()
        if default_src not in BACKTEST_SOURCE_LABELS:
            default_src = "新浪"
        source_var = ctk.StringVar(value=default_src)
        ctk.CTkLabel(dialog, text="数据源").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog, variable=source_var, values=list(BACKTEST_SOURCE_LABELS), width=240
        ).pack(anchor="w", padx=16, pady=(0, 8))

        ctk.CTkLabel(dialog, text="合约（如 fu2701）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 8))
        current = (self.dm_symbol_var.get() or "").strip()
        if current and current != "（还没有）":
            symbol_entry.insert(0, current)

        period_values = [x for x in PERIOD_MENU if x != "自定义"]
        try:
            default_period = period_label(self._dm_period())
        except Exception:
            default_period = "5分钟"
        if default_period not in period_values:
            default_period = period_values[0] if period_values else "5分钟"
        period_var = ctk.StringVar(value=default_period)
        ctk.CTkLabel(dialog, text="周期").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(dialog, variable=period_var, values=period_values, width=240).pack(
            anchor="w", padx=16, pady=(0, 8)
        )

        from src.ui.datetime_pickers import DateField, TimeField

        ctk.CTkLabel(dialog, text="日期").pack(anchor="w", padx=16)
        date_var = ctk.StringVar(value=self.dm_end_date_var.get() or datetime.now().strftime("%Y-%m-%d"))
        DateField(dialog, date_var, allow_empty=False).frame.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(dialog, text="时间（日线、周线可不选；分钟线要选到这根结束时刻）").pack(
            anchor="w", padx=16
        )
        time_var = ctk.StringVar(value="")
        TimeField(dialog, time_var, allow_empty=True).frame.pack(anchor="w", padx=16, pady=(0, 8))

        result = ctk.CTkTextbox(
            dialog, height=220, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, wrap="word"
        )
        result.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        result.insert("end", "选好日期和时间后点「查看」。不会改库，也不会下单。")
        last_paths: dict[str, str] = {}

        def _show(book: dict) -> None:
            last_paths.clear()
            last_paths.update(book.get("paths") or {})
            text = format_kline_inspect(book)
            result.delete("1.0", "end")
            result.insert("end", text)
            self._dm_write(
                f"查看K线 {book.get('source_label')} {book.get('symbol')} "
                f"{book.get('period_label')} {book.get('when')}"
            )

        def _lookup() -> None:
            try:
                book = inspect_kline(
                    self.root_dir,
                    source=normalize_source(source_var.get()),
                    symbol=symbol_entry.get().strip(),
                    period=canonical_period(period_var.get()),
                    date_text=date_var.get(),
                    time_text=time_var.get(),
                )
            except ValueError as exc:
                result.delete("1.0", "end")
                result.insert("end", str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("查看这根K线失败")
                result.delete("1.0", "end")
                result.insert("end", f"查看失败：{exc}")
                return
            _show(book)

        def _open_csv() -> None:
            path = last_paths.get("csv") or ""
            if not path:
                _lookup()
            path = last_paths.get("csv") or ""
            if not path:
                return
            if not Path(path).exists():
                result.delete("1.0", "end")
                result.insert("end", f"还没有这份 CSV。\n{path}\n可改点「打开库存文件夹」。")
                return
            if not self._open_local_file(path):
                result.insert("end", f"\n系统没能打开：{path}")

        def _open_folder() -> None:
            folder = last_paths.get("folder") or str(self.root_dir / "data" / "klines")
            Path(folder).mkdir(parents=True, exist_ok=True)
            if not self._open_local_file(folder):
                result.insert("end", f"\n打不开文件夹：{folder}")

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkButton(btn_row, text="查看", command=_lookup, width=100).pack(side="left")
        ctk.CTkButton(btn_row, text="打开对应CSV", command=_open_csv, width=120).pack(
            side="left", padx=(8, 0)
        )
        ctk.CTkButton(btn_row, text="打开库存文件夹", command=_open_folder, width=130).pack(
            side="right"
        )

    def _on_check_quote_quality(self) -> None:
        """
        读本地各家原样库和清洗库，写成 Excel 分析报表。

        只读 sqlite，不访问网络，也不下单。点一下才跑，不跟扫描绑在一起。
        """
        from tkinter import messagebox

        from src.data.quality import inspect_quote_book, write_quality_report

        symbol = (self.dm_symbol_var.get() or "").strip()
        period = self._dm_period()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还不能检查", "请先选一个合约。")
            return
        try:
            report = inspect_quote_book(self.root_dir, symbol, period, cfg=self.cfg)
            path = write_quality_report(self.root_dir, report)
        except Exception as exc:  # noqa: BLE001
            logger.exception("检查行情质量失败")
            messagebox.showerror("检查失败", str(exc))
            return
        title = {"ok": "行情能用", "watch": "该看一眼", "empty": "还没有库存"}.get(
            report.status, "行情质量"
        )
        messagebox.showinfo(title, f"{report.message}\n\n已保存 {path}")
        self._open_local_file(path)
        self._dm_write(f"行情质量 {symbol} {report.status} | {path}")

    def _on_audit_oi_coverage(self) -> None:
        """按合约和周期统计持仓量覆盖率，写成 CSV。只读本地库，不下载。"""
        import csv
        from pathlib import Path
        from tkinter import messagebox

        from src.data.bar_store import BarStore

        data_cfg = self.cfg.get("data") or {}
        store = BarStore(
            Path(self.root_dir) / data_cfg.get("cache_dir", "data/klines") / "market.sqlite"
        )
        rows = [store.coverage(item["symbol"], item["period"]) for item in store.summarize()]
        if not rows:
            messagebox.showinfo("还没有K线", "本地K线库是空的，先补齐K线再审计持仓量。")
            return
        out = Path(self.root_dir) / "data" / "factor_mining" / "持仓量覆盖审计.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["合约", "周期", "根数", "持仓量非空", "覆盖率", "首根", "末根"])
            for row in rows:
                writer.writerow(
                    [
                        row["symbol"],
                        row["period"],
                        row["n"],
                        row["oi_n"],
                        f"{row['oi_ratio']:.2%}",
                        row["ts_from"],
                        row["ts_to"],
                    ]
                )
        ok = sum(1 for row in rows if row["oi_ratio"] >= 0.90)
        messagebox.showinfo(
            "持仓量覆盖",
            f"{len(rows)} 组（合约+周期）中有 {ok} 组达到 90%。\n已保存 {out}",
        )
        self._open_local_file(out)
        self._dm_write(f"持仓量覆盖审计 {ok}/{len(rows)} 组达标 | {out}")

    # ---------- 天勤 tick ----------
    def _on_show_tick_inventory(self) -> None:
        """写出 tick 统计 Excel 并打开。逐笔不进表，也不写进 K 线库存。"""
        from tkinter import messagebox

        from src.data.tick.downloader import download_settings
        from src.data.tick.inventory import (
            collect_tick_inventory,
            tick_inventory_message,
            write_tick_inventory,
        )

        try:
            settings = download_settings(self.cfg)
            book = collect_tick_inventory(
                self.root_dir,
                database=settings["database"],
                history_dir=settings["out_dir"],
            )
            path = write_tick_inventory(self.root_dir, book)
        except Exception as exc:  # noqa: BLE001
            logger.exception("查看本地tick库存失败")
            messagebox.showerror("打不开库存", str(exc))
            return
        opened = self._open_local_file(path)
        self._dm_write(f"本地tick库存 {tick_inventory_message(book)} | {path}")
        self._refresh_tick_card()
        if not opened:
            messagebox.showerror(
                "打不开统计表",
                f"表已经写好，但系统没能打开 Excel。\n请自己打开：\n{path}",
            )

    def _on_inspect_ticks(self) -> None:
        """抽查一段 tick：根数、起止、头尾几笔。只读不改。"""
        from src.data.tick.downloader import download_settings
        from src.data.tick.manage import format_tick_inspect, inspect_ticks
        from src.ui.datetime_pickers import DateTimeField

        settings = download_settings(self.cfg)
        dialog = ctk.CTkToplevel(self)
        dialog.title("查看这段tick")
        dialog.geometry("520x720")
        dialog.minsize(480, 560)
        dialog.transient(self)
        dialog.grab_set()
        ctk.CTkLabel(
            dialog,
            text="只看本地已经存下的 tick，不拉网、不改数、不下单。\n"
            "时间段留空表示这一合约全部。夜盘过零点算第二天，抽查夜盘请把「到」多选一天。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=460,
        ).pack(anchor="w", padx=16, pady=(16, 10))
        ctk.CTkLabel(dialog, text="合约（fu2701 或 SHFE.fu2701）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 8))
        current = (self.dm_symbol_var.get() or "").strip()
        if current and current != "（还没有）":
            symbol_entry.insert(0, current)
        ctk.CTkLabel(dialog, text="从（可空）").pack(anchor="w", padx=16)
        from_field = DateTimeField(dialog)
        from_field.frame.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(dialog, text="到（可空）").pack(anchor="w", padx=16)
        to_field = DateTimeField(dialog)
        to_field.frame.pack(anchor="w", padx=16, pady=(0, 8))
        result = ctk.CTkTextbox(
            dialog, height=260, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, wrap="word"
        )
        result.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        result.insert("end", "选好合约和时间后点「查看」。不会改库。")

        def _lookup() -> None:
            try:
                book = inspect_ticks(
                    database=settings["database"],
                    symbol=symbol_entry.get().strip(),
                    ts_from=from_field.get() or None,
                    ts_to=to_field.get() or None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("查看这段tick失败")
                result.delete("1.0", "end")
                result.insert("end", f"查看失败：{exc}")
                return
            result.delete("1.0", "end")
            result.insert("end", format_tick_inspect(book))

        def _open_folder() -> None:
            folder = Path(settings["database"]).parent
            folder.mkdir(parents=True, exist_ok=True)
            if not self._open_local_file(folder):
                result.insert("end", f"\n打不开文件夹：{folder}")

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkButton(btn_row, text="查看", command=_lookup, width=100).pack(side="left")
        ctk.CTkButton(btn_row, text="打开tick文件夹", command=_open_folder, width=140).pack(side="right")

    def _on_purge_ticks(self) -> None:
        """按时段删除 tick，并清掉续传，才能重新下载。先数再删。"""
        from tkinter import messagebox

        from src.data.tick.downloader import download_settings
        from src.data.tick.manage import preview_purge_ticks, purge_ticks
        from src.ui.datetime_pickers import DateTimeField

        recorder = getattr(self, "_tick_recorder", None)
        if recorder is not None and recorder.running:
            if not messagebox.askokcancel(
                "删除本地tick",
                "实时采集还在跑，删库可能和正在写入打架。建议先停采集再删。仍要继续？",
            ):
                return

        settings = download_settings(self.cfg)
        dialog = ctk.CTkToplevel(self)
        dialog.title("删除本地tick")
        dialog.geometry("520x640")
        dialog.minsize(480, 560)
        dialog.transient(self)
        dialog.grab_set()
        ctk.CTkLabel(
            dialog,
            text="某一段 tick 很乱时，整段删掉再点「下载历史 tick」重下。\n"
            "不会改留下的 tick，只整段删除。时间段留空表示这一合约全部。\n"
            "会同时忘掉这段的下载续传，并删对应历史 csv，否则下次还会跳过。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=460,
        ).pack(anchor="w", padx=16, pady=(16, 10))
        ctk.CTkLabel(dialog, text="合约（fu2701 或 SHFE.fu2701，必填）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 8))
        current = (self.dm_symbol_var.get() or "").strip()
        if current and current != "（还没有）":
            symbol_entry.insert(0, current)
        ctk.CTkLabel(dialog, text="从（可空：日历选日期，时分可选）").pack(anchor="w", padx=16)
        from_field = DateTimeField(dialog)
        from_field.frame.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(dialog, text="到（可空）").pack(anchor="w", padx=16)
        to_field = DateTimeField(dialog)
        to_field.frame.pack(anchor="w", padx=16, pady=(0, 8))
        synth_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(dialog, text="同时删这段 tick 合成的K线", variable=synth_var).pack(
            anchor="w", padx=16, pady=(0, 10)
        )
        hint = ctk.CTkLabel(dialog, text="", text_color=T.TEXT_DIM, wraplength=460, justify="left")
        hint.pack(anchor="w", padx=16, pady=(0, 8))

        def _args() -> dict:
            return {
                "symbol": symbol_entry.get().strip(),
                "ts_from": from_field.get() or None,
                "ts_to": to_field.get() or None,
            }

        def _preview() -> None:
            try:
                seen = preview_purge_ticks(database=settings["database"], **_args())
            except Exception as exc:  # noqa: BLE001
                hint.configure(text=str(exc))
                return
            hint.configure(
                text=(
                    f"将删除 {seen['exchange']}.{seen['symbol']} 共 {seen['count']} 笔"
                    f"（{seen['start'] or '起'} ~ {seen['end'] or '止'}）。"
                    "确认后不可恢复，请再点下面的删除。"
                )
            )

        def _do_delete() -> None:
            try:
                seen = preview_purge_ticks(database=settings["database"], **_args())
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("还不能删", str(exc), parent=dialog)
                return
            if int(seen.get("count") or 0) <= 0:
                messagebox.showinfo("没有可删的", "按现在的条件，库里没有匹配的 tick。", parent=dialog)
                return
            if not messagebox.askokcancel(
                "确认删除",
                f"将删除 {seen['exchange']}.{seen['symbol']} 共 {seen['count']} 笔。\n"
                "对应历史 csv 和续传标记也会清掉，然后才能重新下载。不可恢复。",
                parent=dialog,
            ):
                return
            try:
                result = purge_ticks(
                    self.root_dir,
                    database=settings["database"],
                    history_dir=settings["out_dir"],
                    also_synth_bars=bool(synth_var.get()),
                    **_args(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("删除本地tick失败")
                messagebox.showerror("删除失败", str(exc), parent=dialog)
                return
            bars = result.get("synth_bars") or {}
            msg = (
                f"已删除 {result['exchange']}.{result['symbol']} {result['deleted']} 笔。"
                f"续传忘掉 {result.get('manifest_cleared') or 0} 块，"
                f"csv 删掉 {len(result.get('csv_deleted') or [])} 个。"
            )
            if bars.get("clean_deleted") or bars.get("raw_deleted"):
                msg += (
                    f" 合成K线删了清洗 {bars.get('clean_deleted') or 0} 根、"
                    f"原样 {bars.get('raw_deleted') or 0} 根。"
                )
            locked = list(result.get("csv_locked") or [])
            if locked:
                msg += "\n有 csv 正被占用，请关掉后再删一次，否则重下仍可能跳过：\n" + "\n".join(locked)
            self._dm_write(f"删除本地tick {msg.replace(chr(10), ' ')}")
            self._refresh_tick_card()
            messagebox.showinfo("已删除" if not locked else "库已删，csv 还被占用", msg, parent=dialog)
            if not locked:
                dialog.destroy()

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(8, 16))
        ctk.CTkButton(btn_row, text="先看要删多少", command=_preview, width=140).pack(side="left")
        ctk.CTkButton(btn_row, text="确认删除", command=_do_delete, width=140, fg_color="#8B2E2E").pack(
            side="right"
        )


    def _set_tick_backfill_status(self, text: str, tone: str = "normal") -> None:
        """历史补齐状态：权限不足持续黄灯，暂停黄灯，失败红灯，成功绿灯。"""
        self.tick_backfill_var.set(text)
        label = getattr(self, "tick_backfill_label", None)
        if label is not None:
            color = {
                "normal": T.TEXT_DIM,
                "success": T.OK,
                "warning": T.WARN,
                "error": T.DANGER,
            }.get(tone, T.TEXT_DIM)
            label.configure(text_color=color)

    def _request_stop_tick_download(self) -> None:
        """下载中再点一次：停止自动续传，已完成块和断点保留。"""
        event = getattr(self, "_tick_download_stop", None)
        if event is None:
            self._dm_write("历史 tick 还在下载，等这一轮结束再点")
            return
        if not event.is_set():
            event.set()
            self._dm_write("已请求停止历史 tick：当前块结束后退出，断点会保留")
            self._set_tick_backfill_status("历史 tick：正在停止，断点会保留…", "warning")

    def _refresh_tick_card(self) -> None:
        """刷 tick 采集心跳和本地库存摘要。每 2 秒由主心跳调用，只读文件。"""
        from src.data.tick.downloader import download_settings
        from src.data.tick.recorder import read_status
        from src.data.tick.vnpy_store import tick_overview

        try:
            settings = download_settings(self.cfg)
            status = read_status(settings["status_file"])
            history_status = read_status(Path(settings["out_dir"]) / "download_status.json")
        except Exception as exc:  # noqa: BLE001
            self.tick_status_var.set(f"tick 采集：状态读取失败 {exc}")
            return

        recorder = getattr(self, "_tick_recorder", None)
        running = bool(recorder is not None and recorder.running)
        button = getattr(self, "tick_toggle_btn", None)
        if button is not None:
            button.configure(text="停止 tick 采集" if running else "启动 tick 采集")

        if status:
            names = {
                "running": "采集中",
                "reconnecting": "重连中",
                "waiting_open": "等开盘",
                "closed": "已收盘",
                "stopped": "已停止",
                "idle": "未启动",
            }
            state = names.get(str(status.get("state")), str(status.get("state") or "未知"))
            text = (
                f"tick 采集：{state}｜今日 {status.get('ticks_today', 0)} 条"
                f"｜缓冲 {status.get('buffered', 0)}｜最后 {status.get('last_tick_at') or '—'}"
            )
            if status.get("session_only"):
                text += "｜自动：仅交易时段"
                if status.get("next_open"):
                    text += f"｜下次开盘 {str(status['next_open']).replace('T', ' ')}"
            if status.get("last_synth_day"):
                text += f"\n最近合成交易日：{status['last_synth_day']}"
            if status.get("symbols"):
                text += f"\n订阅：{'、'.join(str(s) for s in status['symbols'])}"
            if status.get("error"):
                text += f"\n错误：{status['error']}"
        else:
            text = "tick 采集：未启动（还没有心跳文件）"
        self.tick_status_var.set(text)
        history_state = str(history_status.get("state") or "")
        if not getattr(self, "_tick_download_busy", False) and history_state == "permission_denied":
            self._set_tick_backfill_status(
                "历史补齐：需专业版权限；免费账号请保持实时 tick 累积",
                "warning",
            )
        elif not getattr(self, "_tick_download_busy", False) and history_state == "waiting_resume":
            self._set_tick_backfill_status(
                "历史补齐：上次在等待收盘续传时中断，点「补齐历史 tick」继续",
                "warning",
            )

        try:
            overview = tick_overview(database=settings["database"])
        except Exception as exc:  # noqa: BLE001
            self.tick_inventory_var.set(f"tick 库存：读不了（{exc}）")
            return
        if overview:
            total = sum(int(item["count"]) for item in overview)
            latest = max((str(item["end"]) for item in overview if item.get("end")), default="")
            self.tick_inventory_var.set(
                f"tick 库存：{len(overview)} 个合约，{total} 条，最新 {latest or '—'}。"
                "点「查看本地tick库存」看连续时段和来源"
            )
        else:
            self.tick_inventory_var.set("tick 库存：空，先启动采集或下载历史 tick")

    def _on_toggle_tick_record(self) -> None:
        """启动/停止天勤 tick 常驻采集。免费账号也能用，只存 tick 不下单。"""
        from tkinter import messagebox

        from src.data.tick.recorder import TickRecorder

        recorder = getattr(self, "_tick_recorder", None)
        if recorder is not None and recorder.running:
            recorder.stop(timeout=5)
            self._tick_recorder = None
            self._dm_write("天勤 tick 采集已停止")
            self._refresh_tick_card()
            return
        try:
            recorder = TickRecorder(self.cfg)
            recorder.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动 tick 采集失败")
            messagebox.showerror("tick 采集", str(exc))
            return
        self._tick_recorder = recorder
        self._dm_write("天勤 tick 采集已启动：盘中挂机即可攒 tick，断线会自动重连")
        self._refresh_tick_card()

    def _on_toggle_tick_auto(self) -> None:
        """「启动程序时自动采集」写进 data.tick.enabled；打开时当前也立即开始采集。"""
        from src.utils.config_loader import save_config

        enabled = bool(self.tick_auto_var.get())
        data_cfg = self.cfg.setdefault("data", {})
        tick_cfg = data_cfg.get("tick")
        if not isinstance(tick_cfg, dict):
            tick_cfg = {}
            data_cfg["tick"] = tick_cfg
        tick_cfg["enabled"] = enabled
        try:
            path = save_config(self.cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("自动采集开关写配置失败")
            tick_cfg["enabled"] = not enabled
            self.tick_auto_var.set(not enabled)
            self._dm_write(f"自动采集开关没保存：{exc}")
            return
        if enabled:
            self._auto_start_tick_if_enabled()
            self._dm_write(f"已打开启动自动采集（写入 {path}），本次也已开始")
        else:
            self._dm_write(f"已关闭启动自动采集（写入 {path}）；本次采集不受影响")
        self._refresh_tick_card()

    def _auto_start_tick_if_enabled(self) -> None:
        """程序启动后按 data.tick.enabled 自动开采集；非交易时段只等开盘，不连天勤。

        启动失败（尤其锁文件撞车）不放弃，按 30 秒间隔重试，最多 6 次。
        """
        from src.data.tick.recorder import TickRecorder, tick_settings

        if getattr(self, "_tick_recorder", None) is not None:
            self._tick_autostart_tries = 0
            return
        try:
            settings = tick_settings(self.cfg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("自动 tick 采集配置读取失败：{}", exc)
            return
        if not settings.get("enabled"):
            return
        try:
            recorder = TickRecorder(self.cfg)
            recorder.start()
        except Exception as exc:  # noqa: BLE001
            tries = int(getattr(self, "_tick_autostart_tries", 0)) + 1
            self._tick_autostart_tries = tries
            logger.warning("自动 tick 采集未启动（第 {} 次）：{}", tries, exc)
            self._dm_write(f"自动 tick 采集未启动（第 {tries} 次）：{exc}")
            if tries <= _TICK_AUTOSTART_RETRY_LIMIT and hasattr(self, "after"):
                self.after(_TICK_AUTOSTART_RETRY_DELAY_MS, self._auto_start_tick_if_enabled)
            return
        self._tick_autostart_tries = 0
        self._tick_recorder = recorder
        self._dm_write("自动 tick 采集已启动：仅交易时段连接，收盘后自动合成K线")
        self._refresh_tick_card()

    def _on_backfill_history_tick(self) -> None:
        """按 lookback_days 一键补齐历史 tick：暂停实时采集、断点续传、完成后恢复。"""
        from datetime import date, timedelta
        from tkinter import messagebox

        from src.data.tick.downloader import (
            DownloadPermissionError,
            download_settings,
            main_cont_list_from_symbols,
            run_download_auto_resume,
            trading_pause_reason,
        )

        if getattr(self, "_tick_download_busy", False):
            self._request_stop_tick_download()
            return

        settings = download_settings(self.cfg)
        symbols = [str(item).strip() for item in (settings.get("symbols") or []) if str(item).strip()]
        if not symbols:
            messagebox.showwarning("还不能补齐", "配置页还没有关注品种。")
            return

        auto = bool(settings.get("auto_resume", True))
        reason = trading_pause_reason(settings)
        if reason and not auto:
            self._set_tick_backfill_status(f"历史补齐：{reason}", "warning")
            messagebox.showwarning("交易时段不补齐", reason)
            return

        lookback = max(1, int(settings.get("lookback_days") or 120))
        end = date.today()
        start = end - timedelta(days=lookback)
        settings["enabled"] = True
        settings["start"] = start.isoformat()
        settings["end"] = end.isoformat()
        mains = [str(item) for item in (settings.get("main_cont_list") or []) if str(item).strip()]
        if not mains:
            mains = main_cont_list_from_symbols(symbols)
        settings["main_cont_list"] = mains
        if not mains:
            messagebox.showwarning("还不能补齐", "关注品种转不出天勤主连代码，无法查历史主力。")
            return

        recorder = getattr(self, "_tick_recorder", None)
        pause = bool(settings.get("pause_record_during_download", True))
        was_running = bool(recorder is not None and recorder.running)
        resume = pause and was_running and recorder is not None

        stop_event = threading.Event()
        self._tick_download_stop = stop_event
        self._tick_download_busy = True
        span = f"{settings['start']} ~ {settings['end']}"
        if reason:
            self._set_tick_backfill_status(
                f"历史补齐：{span}，交易时段先排队，收盘后自动开始", "warning"
            )
            self._dm_write(f"补齐历史 tick 已排队：{span}，收盘后自动开始")
        else:
            self._set_tick_backfill_status(f"历史补齐：{span}，准备中…")
            self._dm_write(f"开始补齐历史 tick：{span}，回看 {lookback} 天，{'、'.join(mains)}")

        def pause_record() -> None:
            """真正开始下载前才停实时采集，避免等待收盘的几个小时丢 tick。"""
            if not (resume and recorder is not None):
                return
            try:
                if recorder.running:
                    recorder.stop(timeout=10)
                self._post_to_main(
                    lambda: self._dm_write("补齐历史 tick：实时采集已暂停，下载完自动恢复")
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("暂停实时 tick 采集失败：{}", exc)

        def restore_record() -> None:
            if not (resume and recorder is not None):
                return
            try:
                if not recorder.running:
                    recorder.start()
                    logger.info("历史 tick 补齐暂停等待，实时采集已恢复")
            except Exception as exc:  # noqa: BLE001
                logger.warning("恢复实时 tick 采集失败：{}", exc)

        def work() -> None:
            try:
                if not reason:
                    pause_record()
                summary = run_download_auto_resume(
                    settings,
                    on_progress=lambda text: (
                        logger.info("历史 tick {}", text),
                        self._post_to_main(
                            lambda t=text: self._set_tick_backfill_status(f"历史补齐：{t}")
                        ),
                    ),
                    stop=stop_event.is_set,
                    on_pause=lambda _message: restore_record(),
                    on_wake=pause_record,
                )
                logger.info("历史 tick 补齐结束：{}", summary)
                rounds = int(summary.get("rounds") or 1)
                suffix = f"（分 {rounds} 轮）" if rounds > 1 else ""
                if summary.get("resume_stopped"):
                    text = f"历史补齐已停止{suffix}：断点已保留，收盘后再点继续"
                elif summary.get("status") == "paused":
                    text = f"历史补齐已暂停：{summary.get('message') or '当前处于交易时段'}"
                    remaining = int(summary.get("remaining") or 0)
                    if remaining:
                        text += f"；剩余 {remaining} 块"
                    text += "，收盘后自动继续" if auto else "，收盘后再点继续"
                else:
                    text = (
                        f"历史补齐完成{suffix}：计划 {summary.get('total', 0)} 块，"
                        f"新下 {summary.get('downloaded', 0)}，跳过 {summary.get('skipped', 0)}，"
                        f"复用 {summary.get('reused', 0)}，失败 {summary.get('failed', 0)}"
                    )
                tone = "warning" if summary.get("status") == "paused" or summary.get("failed") else "success"
                self._post_to_main(
                    lambda t=text, color=tone: self._set_tick_backfill_status(t, color)
                )
                self._post_to_main(lambda t=text: self._dm_write(t))
            except DownloadPermissionError as exc:
                logger.warning("历史 tick 没有权限：{}", exc)
                msg = str(exc)
                text = "历史补齐：需专业版权限；免费账号请保持实时 tick 累积"
                self._post_to_main(
                    lambda t=text: self._set_tick_backfill_status(t, "warning")
                )
                self._post_to_main(lambda m=msg: messagebox.showerror("没有下载权限", m))
                self._post_to_main(lambda m=msg: self._dm_write(m))
            except Exception as exc:  # noqa: BLE001
                logger.exception("历史 tick 补齐失败")
                msg = str(exc)
                self._post_to_main(
                    lambda m=msg: self._set_tick_backfill_status(f"历史补齐失败：{m}", "error")
                )
                self._post_to_main(lambda m=msg: messagebox.showerror("补齐失败", m))
                self._post_to_main(lambda m=msg: self._dm_write(f"历史 tick 补齐失败：{m}"))
            finally:
                self._tick_download_stop = None
                self._tick_download_busy = False
                restore_record()
                self._post_to_main(self._refresh_tick_card)

        threading.Thread(target=work, name="TickBackfill", daemon=True).start()

    def _on_synth_tick_bars(self) -> None:
        """把本地 tick 合成为各周期 K 线。耗时长，放后台线程，日志进运行日志。"""
        from tkinter import messagebox

        from src.data.tick.synthesize import synthesize_symbol

        if getattr(self, "_tick_synth_busy", False):
            self._dm_write("tick 合成还在跑，等这一轮结束再点")
            return
        symbols = [str(item) for item in (self._watch_symbols or []) if str(item).strip()]
        if not symbols:
            messagebox.showwarning("还不能合成", "配置页还没有关注品种。")
            return
        from src.data.tick.recorder import tick_settings

        settings = tick_settings(self.cfg)
        self._tick_synth_busy = True
        self._dm_write(f"开始合成 tick K 线：{'、'.join(symbols)}")

        def work() -> None:
            try:
                for symbol in symbols:
                    try:
                        report = synthesize_symbol(
                            symbol,
                            database=settings["database"],
                            bar_store=settings["bar_store"],
                            periods=settings["synth_periods"],
                        )
                        logger.info(
                            "tick 合成 {}：tick {} 条（丢 {}），写库 {}",
                            symbol,
                            report["ticks"],
                            report["dropped"],
                            report["bars"],
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.opt(exception=exc).error("tick 合成 {} 失败", symbol)
                        logger.warning("tick 合成 {} 失败：{}", symbol, exc)
            finally:
                self._tick_synth_busy = False

        threading.Thread(target=work, name="TickSynth", daemon=True).start()

    def _on_download_history_tick(self) -> None:
        """专业版历史 tick：弹窗选起止日期，后台下载入库。免费账号会得到没权限的说明。"""
        from datetime import date, timedelta
        from tkinter import messagebox

        from src.data.tick.downloader import (
            DownloadPermissionError,
            download_settings,
            main_cont_list_from_symbols,
            run_download_auto_resume,
        )
        from src.ui.datetime_pickers import DateField

        if getattr(self, "_tick_download_busy", False):
            self._request_stop_tick_download()
            return
        recorder = getattr(self, "_tick_recorder", None)
        if recorder is not None and recorder.running:
            if not messagebox.askokcancel(
                "下载历史 tick",
                "实时采集还在跑，下载会再开一条天勤连接。免费账号可能被挤掉。建议先停采集再下。仍要继续？",
            ):
                return

        dialog = ctk.CTkToplevel(self)
        dialog.title("下载历史 tick")
        dialog.geometry("420x360")
        dialog.transient(self)
        dialog.grab_set()
        ctk.CTkLabel(
            dialog,
            text="需要天勤专业版或试用权限。先选一小段日期验证，成功后再拉长区间。\n"
            "合约用配置页关注品种转成主连。已下过的天会跳过，可断点续传。\n"
            "盘中开始会先排队，收盘后自动继续；下载中再点一次按钮可停止。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=380,
        ).pack(anchor="w", padx=16, pady=(16, 10))
        start_var = ctk.StringVar(value=(date.today() - timedelta(days=7)).isoformat())
        end_var = ctk.StringVar(value=date.today().isoformat())
        ctk.CTkLabel(dialog, text="开始日期").pack(anchor="w", padx=16)
        DateField(dialog, start_var, allow_empty=False).frame.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(dialog, text="结束日期").pack(anchor="w", padx=16)
        DateField(dialog, end_var, allow_empty=False).frame.pack(anchor="w", padx=16, pady=(0, 8))
        dry_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(dialog, text="只列计划，不真正下载", variable=dry_var).pack(anchor="w", padx=16, pady=(0, 12))

        def _start() -> None:
            settings = download_settings(self.cfg)
            settings["enabled"] = True
            settings["start"] = start_var.get().strip()
            settings["end"] = end_var.get().strip()
            mains = list(settings.get("main_cont_list") or [])
            if not mains:
                mains = main_cont_list_from_symbols(list(self._watch_symbols or []))
            settings["main_cont_list"] = mains
            if not mains:
                messagebox.showwarning("还不能下载", "配置页还没有关注品种，也无法从配置读到主连名单。")
                return
            dry = bool(dry_var.get())
            auto = bool(settings.get("auto_resume", True))
            dialog.destroy()
            stop_event = threading.Event()
            self._tick_download_stop = stop_event
            self._tick_download_busy = True
            self._set_tick_backfill_status("历史 tick：准备中…")
            self._dm_write(
                f"{'只列计划' if dry else '开始下载'}历史 tick：{settings['start']} ~ {settings['end']} "
                f"{'、'.join(mains)}"
            )

            def work() -> None:
                try:
                    summary = run_download_auto_resume(
                        settings,
                        dry_run=dry,
                        on_progress=lambda text: (
                            logger.info("历史 tick {}", text),
                            self._post_to_main(
                                lambda t=text: self._set_tick_backfill_status(f"历史 tick：{t}")
                            ),
                        ),
                        stop=stop_event.is_set,
                    )
                    logger.info("历史 tick 结束：{}", summary)
                    rounds = int(summary.get("rounds") or 1)
                    suffix = f"（分 {rounds} 轮）" if rounds > 1 else ""
                    if summary.get("resume_stopped"):
                        text = f"历史下载已停止{suffix}：断点已保留，收盘后再点继续"
                        tone = "warning"
                    elif summary.get("status") == "paused":
                        text = f"历史下载已暂停：{summary.get('message') or '当前处于交易时段'}"
                        remaining = int(summary.get("remaining") or 0)
                        if remaining:
                            text += f"；剩余 {remaining} 块"
                        text += "，收盘后自动继续" if auto else "，收盘后再点继续"
                        tone = "warning"
                    elif dry:
                        text = f"历史 tick 计划：共 {summary.get('total', 0)} 块，未下载"
                        tone = "normal"
                    else:
                        text = (
                            f"历史 tick 完成{suffix}：计划 {summary.get('total', 0)} 块，"
                            f"新下 {summary.get('downloaded', 0)}，跳过 {summary.get('skipped', 0)}，"
                            f"复用 {summary.get('reused', 0)}，失败 {summary.get('failed', 0)}"
                        )
                        tone = "warning" if summary.get("failed") else "success"
                    self._post_to_main(
                        lambda t=text, color=tone: self._set_tick_backfill_status(t, color)
                    )
                    self._post_to_main(lambda t=text: self._dm_write(t))
                except DownloadPermissionError as exc:
                    logger.warning("历史 tick 没有权限：{}", exc)
                    msg = str(exc)
                    text = "历史补齐：需专业版权限；免费账号请保持实时 tick 累积"
                    self._post_to_main(
                        lambda t=text: self._set_tick_backfill_status(t, "warning")
                    )
                    self._post_to_main(lambda m=msg: messagebox.showerror("没有下载权限", m))
                    self._post_to_main(lambda m=msg: self._dm_write(m))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("历史 tick 下载失败")
                    msg = str(exc)
                    self._post_to_main(
                        lambda m=msg: self._set_tick_backfill_status(f"历史 tick 失败：{m}", "error")
                    )
                    self._post_to_main(lambda m=msg: messagebox.showerror("下载失败", m))
                    self._post_to_main(lambda m=msg: self._dm_write(f"历史 tick 失败：{m}"))
                finally:
                    self._tick_download_stop = None
                    self._tick_download_busy = False
                    self._post_to_main(self._refresh_tick_card)

            threading.Thread(target=work, name="TickDownload", daemon=True).start()

        ctk.CTkButton(dialog, text="开始", height=36, command=_start).pack(fill="x", padx=16, pady=(8, 16))
