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
                "本地 K 线库的建立和管理都在这里。从同花顺图按「周期 + 时段」读 K 线，"
                "每根带时间戳，分几次读会按时间接上；每次读完自动更新库存 Excel。"
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
                "先打开合约的 K 线主图（能看到蜡烛图），主图选好合约和周期。"
                "结束时间是现在时，程序自己推到最新一根；补历史时，先用手把光标停到"
                "结束时间那根K线上，再点按钮（点完 3 秒内把鼠标放回主图）。"
                "读的时候按 ESC 或点「停止读图」可停，已读到的会先存下来。不会点买卖。"
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))

        ctk.CTkLabel(
            left, text="本地库存", font=T.FONT_TITLE, text_color=T.TEXT
        ).pack(anchor="w", padx=4, pady=(8, 4))
        inv_row = ctk.CTkFrame(left, fg_color="transparent")
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
        ctk.CTkLabel(
            left,
            text=(
                "库存表直接打开 Excel，其中有「读取记录」页，能看到每批读了哪段、新增多少、还缺多少。"
                "抽查某一根：选数据源、合约、周期、日期和时间，对照同花顺开高低收。"
                "某一家质量差时，可按数据源、合约、时间段删掉再重新拉。"
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
