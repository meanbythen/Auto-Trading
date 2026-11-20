"""Cross-test construction, batch execution, results, and exports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from loguru import logger
from src.kernel.events import CROSS_TEST_DONE
from src.ui import theme as T
import customtkinter as ctk
import threading


class PickerTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _pack_pick_limits(self, parent: ctk.CTkFrame) -> None:
        """
        交叉测试留下的开仓限制。

        持仓手数决定开几手，不看账户权益。开仓间隔和每天最多开仓会限制开仓次数。
        每笔风险记在这一轮里，不拿它重算手数。不写配置文件，也不改回测页和实盘。
        """
        box = self._card(parent)
        ctk.CTkLabel(box, text="开仓和止损限制", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            box,
            text="账户权益、收盘清仓以本页为准，点开始不会被交易配置改回去。手数、间隔、每天次数、每笔风险跟本页显示的数字；要跟交易配置一样，点「按配置页重新填入」。开几手仍固定。主力换月跟历史回测页。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._entry_row(
            box,
            "账户权益(元)",
            self.pk_capital_var,
            hint="可改成准备投入的资金；点「按配置页重新填入」抄回当前余额",
            width=160,
        )
        self._entry_row(
            box, "每笔风险%", self.pk_risk_pct_var, hint="本页显示值；点「按配置页重新填入」才从交易配置抄来", width=160, readonly=True
        )
        self._entry_row(
            box, "持仓手数", self.pk_volume_var, hint="本页显示值；各组合同手数", width=160, readonly=True
        )
        self._entry_row(box, "开仓最小间隔秒", self.pk_interval_var, hint="本页显示值", width=160, readonly=True)
        self._entry_row(box, "每天最多开仓", self.pk_max_orders_var, hint="本页显示值", width=160, readonly=True)
        ctk.CTkButton(
            box,
            text="按配置页重新填入",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._fill_picker_from_config,
        ).pack(fill="x", pady=(8, 0))


    def _build_picker_tab(self, tab: ctk.CTkFrame) -> None:
        """
        交叉测试：勾选的策略、合约、周期交叉匹配，每组跑一遍历史回测。

        信号和止损用同一个周期。全品种主力连续只写在备查文件里，这一轮不用那份。
        日期、手数和开仓限制仍用本页。不改正在用的策略。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)
        ctk.CTkLabel(left, text="交叉测试", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="这是「策略×合约×周期」对照筛选：左边三份名单可复选，点开始后把勾上的项交叉匹配，每组跑一遍历史回测。信号和止损用同一个周期。要验优化参数稳不稳，用下面「用最近优化参数做多品种验收」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        lists = self._card(left)
        ctk.CTkLabel(lists, text="这一轮测这些", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w")
        ctk.CTkLabel(
            lists,
            text="三份都可以复选。点开始后按「策略 × 合约 × 周期」交叉匹配。合约来自配置页关注品种。全品种主力连续写在 config/全品种备查.txt，只供查看。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(4, 6))
        self.pk_engine_list = self._pack_cross_check_group(lists, "参与策略", "engine", height=200)
        self.pk_engine_hint = ctk.CTkLabel(
            lists,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        )
        self.pk_engine_hint.pack(anchor="w", pady=(0, 6))
        self.pk_symbol_list = self._pack_cross_check_group(lists, "品种合约", "symbol")
        self.pk_period_list = self._pack_cross_check_group(lists, "周期（信号和止损相同）", "period")
        self.pk_combo_label = ctk.CTkLabel(
            lists,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        )
        self.pk_combo_label.pack(anchor="w", pady=(4, 2))
        self._refresh_cross_lists()

        box = self._card(left)
        from src.data.quote_check import SOURCE_LABELS

        # 这一轮只下载这里选中的那一家，不混用另一家的 K 线。
        self._option_row(box, "行情数据源", self.pk_source_var, list(SOURCE_LABELS), width=160)
        ctk.CTkLabel(
            box,
            text="K线按这里选的来源下载。同一个品种、同一个周期下载一次后，后面的策略接着用。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        self._option_row(
            box,
            "时间段",
            self.pk_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.pk_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.pk_end_var, hint="点日历；清空=到最新")
        self._pick_switch_row(box, "收盘清仓", self.pk_flatten_var)
        ctk.CTkLabel(
            box,
            text="每个组合都用上面勾选的周期（信号=止损），以及下面的开仓限制。点开始就按本页收盘清仓，不会被交易配置改回去。趋势跟随、SuperTrend、均线波段仍用它们自己的设置。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))

        self._pack_pick_limits(left)
        ctk.CTkButton(
            left,
            text="开始交叉测试",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_pick_start,
        ).pack(fill="x", padx=4, pady=(8, 8))
        ctk.CTkButton(
            left,
            text="用最近优化参数做多品种验收",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_pick_opt_cross,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="先在策略优化页跑完一轮。这里把当时挑出的参数，原样拿到关注品种上再跑一遍，不再重新搜参数。这才是多品种样本外验收。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))
        ctk.CTkButton(
            left,
            text="停止测试",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_pick_stop,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkButton(
            left,
            text="导出本次交叉测试",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_cross,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="导出追加到「交叉测试」表。第一列是这一轮的完成时间，同一轮再导出不会重复。品种和策略各占一列，较好的排在前面。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))
        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="测试结果", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.pick_summary = ctk.CTkLabel(
            right,
            text="点「开始交叉测试」后，每个策略、合约、周期的组合占一行。较好的排在前面。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            justify="left",
            anchor="w",
            wraplength=640,
        )
        self.pick_summary.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        # 表头固定，下面的品种行可以上下滚动。列多了以后，这一块还可以左右拖，避免「结果」被挤出窗口。
        # 宽度和表头、数据行共用。实际测试时段要放下「2026-10-01 ~ 2026-10-22」。
        # 列顺序和导出「交叉测试」表一致：盈亏比后是收益期望；净盈亏后是盈利覆盖；交易频率后是笔数、每笔。
        # 「当下行情」只在这里显示（这一轮跑的时候现算），不写进导出表。
        self._pick_col_widths = (
            108, 132, 64, 48, 64, 56, 72, 72, 56, 64, 72, 72, 64, 120, 40, 72,
            176, 88, 88, 40, 72, 72, 48, 96, 180, 96, 52, 64,
        )
        self._pick_headers = [
            "品种",
            "策略",
            "周期",
            "胜率",
            "盈利因子",
            "盈亏比",
            "收益期望",
            "最大回撤",
            "卡尔玛",
            "最大连亏",
            "净盈亏",
            "盈利覆盖",
            "收益率",
            "交易频率",
            "笔数",
            "每笔收益",
            "实际测试时段",
            "开始时间",
            "结束时间",
            "天数",
            "日均",
            "K线根数",
            "得分",
            "评估",
            "未通过项",
            "当下行情",
            "流动性",
            "结果",
        ]
        right.grid_rowconfigure(1, weight=0)
        right.grid_rowconfigure(2, weight=1)
        table_width = sum(self._pick_col_widths) + 8 + 4 * (len(self._pick_col_widths) - 1) + 8
        xscroll = ctk.CTkScrollableFrame(
            right,
            orientation="horizontal",
            fg_color=T.BG,
            corner_radius=8,
            scrollbar_fg_color=T.BG,
            scrollbar_button_color=T.BORDER,
            scrollbar_button_hover_color=T.TEXT_DIM,
        )
        xscroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        header = ctk.CTkFrame(xscroll, fg_color=T.BG, corner_radius=8, width=table_width)
        header.pack(fill="x", pady=(4, 4))
        self._pick_header_labels = []
        self._pack_pick_metric_line(header, self._pick_headers, header=True)
        self.pick_table_body = ctk.CTkScrollableFrame(
            xscroll,
            fg_color=T.BG,
            corner_radius=8,
            scrollbar_fg_color=T.BG,
            scrollbar_button_color=T.BORDER,
            scrollbar_button_hover_color=T.TEXT_DIM,
            width=table_width,
        )
        self.pick_table_body.pack(fill="both", expand=True)
        ctk.CTkLabel(
            right,
            text="提示：双击一行，看这个合约、这个周期的K线。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        ).grid(row=3, column=0, sticky="w", padx=12, pady=(0, 10))


    def _picks_path(self) -> Path:
        """交叉测试自己的记录。和回测报告分开，避免优选把回测结果盖掉。"""
        return self.root_dir / "data" / "symbol_picks.json"


    def _pack_pick_metric_line(
        self,
        parent: ctk.CTkFrame,
        texts: list[str],
        *,
        header: bool = False,
        picked: str = "",
        on_open: Callable[[], None] | None = None,
    ) -> None:
        """
        结果表的一行。

        各列用固定宽度，表头和每一行同一套数字，品种再多也不会左右错开。
        结果这一列按文字上色：入选和第 1 名用绿色，没测成用黄色，基准用蓝色。
        给了 on_open 时，双击这一行打开对应合约、周期的K线。
        """
        line = ctk.CTkFrame(parent, fg_color="transparent")
        line.pack(fill="x")
        widgets: list[Any] = [line]
        if str(picked).startswith("第"):
            picked_color = T.OK if picked == "第1名" else T.TEXT
        else:
            picked_color = {
                "入选": T.OK,
                "较好": T.OK,
                "没测成": T.WARN,
                "基准": T.ACCENT,
            }.get(picked, T.TEXT)
        for index, text in enumerate(texts):
            color = T.TEXT_DIM if header else T.TEXT
            if not header and text == "偏少":
                color = T.WARN
            elif not header and "不适配" in text:
                color = T.WARN
            elif not header and text in {"通过", "观察", "淘汰", "未评估", "评估失败"}:
                # 评估列：淘汰标红，观察标黄，通过标绿。
                if text == "淘汰":
                    color = T.DANGER
                elif text in {"观察", "评估失败"}:
                    color = T.WARN
                elif text == "通过":
                    color = T.OK
            elif not header and index == len(texts) - 1:
                color = picked_color if picked != "未入选" else T.TEXT_DIM
            label = ctk.CTkLabel(
                line,
                text=text,
                width=self._pick_col_widths[index],
                anchor="w",
                font=T.FONT_SMALL,
                text_color=color,
            )
            label.pack(side="left", padx=(8 if index == 0 else 4, 0), pady=5)
            widgets.append(label)
            if header:
                self._pick_header_labels.append(label)
        if on_open is not None and not header:
            for widget in widgets:
                widget.bind("<Double-Button-1>", lambda _event, callback=on_open: callback())


    def _render_pick_board(self, summary: str, rows: list[dict[str, str]], *, empty_text: str = "") -> None:
        """刷新右上角说明，并按行重画全部品种。旧行先清掉，避免上一轮的成绩留在表里。"""
        if hasattr(self, "pick_summary"):
            self.pick_summary.configure(text=summary)
        body = getattr(self, "pick_table_body", None)
        if body is None:
            return
        for child in body.winfo_children():
            child.destroy()
        if not rows:
            ctk.CTkLabel(
                body,
                text=empty_text or "测完后，每个品种和策略的组合占一行。较好的排在前面。",
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                anchor="w",
            ).pack(anchor="w", padx=8, pady=8)
            return
        # 上次如果测了上千个组合，一次画出来会让窗口打不开。这里只画前面几十行。
        from src.screen.picker import window_rows

        visible, extra = window_rows(rows)
        for item in visible:
            self._pack_pick_metric_line(
                body,
                [
                    item.get("symbol") or "-",
                    item.get("strategy") or "—",
                    item.get("period") or "—",
                    item.get("win_rate") or "—",
                    item.get("profit_factor") or "—",
                    item.get("payoff") or "—",
                    item.get("expectancy") or "—",
                    item.get("max_drawdown") or "—",
                    item.get("calmar") or "—",
                    item.get("loss_streak") or "—",
                    item.get("net_pnl") or "—",
                    item.get("profit_cover") or "—",
                    item.get("return_rate") or "—",
                    item.get("frequency") or "—",
                    item.get("trade_count") or "—",
                    item.get("pnl_per_trade") or "—",
                    item.get("span") or "—",
                    item.get("span_start") or "—",
                    item.get("span_end") or "—",
                    item.get("span_days") or "—",
                    item.get("daily_avg") or "—",
                    item.get("bars") or "—",
                    item.get("score") or "—",
                    item.get("evaluation") or "—",
                    item.get("fail_items") or "—",
                    item.get("market") or "—",
                    item.get("liquidity") or "—",
                    item.get("picked") or "—",
                ],
                picked=str(item.get("picked") or ""),
                on_open=lambda row=item: self._open_pick_chart(row),
            )
        if extra:
            ctk.CTkLabel(
                body,
                text=extra,
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                anchor="w",
                justify="left",
                wraplength=640,
            ).pack(anchor="w", padx=8, pady=8)


    def _open_pick_chart(self, row: dict[str, str]) -> None:
        """双击结果行：按这一行的合约和周期打开K线窗口。"""
        symbol = str(row.get("code") or "").strip()
        period = str(row.get("timeframe") or "").strip()
        if not symbol or not period:
            self._append_log("这一行没有记下合约或周期，看不了K线")
            return
        from src.ui.kline_chart import open_kline_chart

        cfg = dict(self.cfg)
        cfg.setdefault("_project_root", str(self.root_dir))
        open_kline_chart(self, cfg, symbol, period)


    def _show_pick_result(self, result: dict[str, Any]) -> None:
        """用这一轮的组合画结果表。较好的排在前面。"""
        from src.screen.picker import board_summary, list_rows

        self._render_pick_board(board_summary(result), list_rows(result))


    def _load_saved_picks(self) -> None:
        """打开窗口时恢复上一次的基准和备选，免得每次都要重测。"""
        from src.screen.picker import liquidity_text, load_picks

        def _drop_thin(rows: list) -> list:
            # 上次结果里如果还留着流动性偏少的品种，打开窗口时不再显示。
            kept = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("liquidity") or "") == "偏少":
                    continue
                if liquidity_text(str(row.get("symbol") or "")) == "偏少":
                    continue
                kept.append(row)
            return kept

        try:
            data = load_picks(self._picks_path())
        except Exception as exc:  # noqa: BLE001
            # 上次的结果读不出来时，窗口仍要能打开，不能卡在这里。
            logger.warning("上次交叉测试结果读不出来，先打开空表: {}", exc)
            return
        baseline = data.get("baseline") if isinstance(data.get("baseline"), dict) else None
        self._pick_baseline = baseline or None
        rows = _drop_thin(data.get("rows") or [])
        if baseline or data.get("rows"):
            shown = {
                "baseline": baseline or {},
                "rows": rows,
                "volume": data.get("volume"),
                "mode": data.get("mode") or "baseline",
                "settings": data.get("settings") or {},
                "stopped": False,
            }
            if "skipped_thin" in data:
                shown["skipped_thin"] = data.get("skipped_thin")
            self._show_pick_result(shown)


    def _align_picker_trading_params_with_live(self) -> None:
        """只给「按配置页重新填入」用：把交易配置的手数、风险、间隔、次数抄到本页。"""
        if not hasattr(self, "pk_volume_var"):
            return
        self.pk_risk_pct_var.set(self.risk_pct_var.get())
        self.pk_volume_var.set(self.volume_var.get())
        self.pk_interval_var.set(self.min_interval_var.get())
        self.pk_max_orders_var.set(self.max_orders_var.get())


    def _fill_picker_from_config(self) -> None:
        """把配置页当前的周期、行情、手数、风险、开仓次数和账户当前权益抄到交叉测试页。不写文件。"""
        self.pk_source_var.set(self.source_var.get())
        live_tf = self._period_code(self.tf_var, self.tf_custom_var)
        self._set_period_vars(live_tf, self.pk_tf_var, self.pk_tf_custom_var)
        self._set_period_vars(live_tf, self.pk_trail_var, self.pk_trail_custom_var)
        self.pk_capital_var.set(self.capital_var.get())
        self.pk_flatten_var.set(bool(self.flatten_at_close_var.get()))
        self._align_picker_trading_params_with_live()
        self._refresh_cross_lists(prefer_period=live_tf)
        self._append_log(f"已按配置页填好交叉测试的周期、行情、开仓限制和账户权益，行情 {self.pk_source_var.get()}")


    def _picker_settings(self) -> dict[str, Any]:
        """
        读交叉测试用的周期和开仓限制。

        收盘清仓、手数、风险、间隔、次数、账户权益都听本页，不回头改格子。
        要跟交易配置一样，请点「按配置页重新填入」。数字填错就停下来。
        """
        preset = self.pk_range_var.get()
        start, end = self._resolve_date_range(preset, self.pk_start_var.get(), self.pk_end_var.get())
        if preset == "全部可用" and (start or end):
            self.pk_range_var.set("自定义")
        if start:
            self.pk_start_var.set(start)
        if end:
            self.pk_end_var.set(end)
        try:
            volume = int(float(self.pk_volume_var.get()))
            risk_pct = float(self.pk_risk_pct_var.get())
            interval = int(float(self.pk_interval_var.get()))
            max_orders = int(float(self.pk_max_orders_var.get()))
            capital = float(self.pk_capital_var.get())
        except ValueError as exc:
            raise ValueError("交叉测试的账户权益、每笔风险、持仓手数、开仓间隔和每天最多开仓要填数字") from exc
        if volume < 1:
            raise ValueError("持仓手数至少是 1")
        if risk_pct < 0:
            raise ValueError("每笔风险不能是负数")
        if interval < 0:
            raise ValueError("开仓最小间隔不能是负数")
        if max_orders < 1:
            raise ValueError("每天最多开仓至少是 1")
        if capital <= 0:
            raise ValueError("账户权益要大于 0")
        periods = self._pk_checked_periods()
        tf = periods[0] if periods else self._period_code(self.tf_var, self.tf_custom_var)
        return {
            "timeframe": tf,
            "trail_timeframe": tf,
            "start_date": start or "",
            "end_date": end or "",
            "flatten_at_close": bool(self.pk_flatten_var.get()),
            "has_flatten": True,
            "fixed_volume": volume,
            "risk_per_trade_pct": risk_pct,
            "min_order_interval_sec": interval,
            "max_orders_per_day": max_orders,
            "capital": capital,
            "data_source": self.pk_source_var.get(),
        }


    def _on_pick_opt_cross(self) -> None:
        """
        用策略优化刚选出的参数，在关注品种上做多品种样本外验收。

        不再重新搜参数。这和「开始交叉测试」不同：交叉测试是配置固定参数的对照筛选。
        """
        from tkinter import messagebox

        if self._pick_running:
            messagebox.showinfo("正在测试", "上一轮还没结束。可以点「停止测试」。")
            return
        params = dict(getattr(self, "_last_opt_params", None) or {})
        engine = str(getattr(self, "_last_opt_engine", "") or "")
        if not params or not engine:
            messagebox.showwarning(
                "还没有优化结果",
                "请先到「策略优化」页跑完一轮，再回到这里做多品种验收。",
            )
            return
        symbols = [str(x).strip() for x in self._watch_symbols if str(x).strip()]
        if not symbols:
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加合约。")
            return
        try:
            settings = self._picker_settings()
        except ValueError as exc:
            messagebox.showerror("数字不对", str(exc))
            return
        cfg = self._clone_cfg()
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.pk_source_var.get())
        st = cfg.setdefault("strategy", {})
        st["timeframe"] = str(settings.get("timeframe") or self._period_code(self.pk_tf_var, self.pk_tf_custom_var))
        st["trail_timeframe"] = str(
            settings.get("trail_timeframe") or self._period_code(self.pk_trail_var, self.pk_trail_custom_var)
        )
        if "flatten_at_close" in settings:
            st["flatten_at_close"] = bool(settings.get("flatten_at_close"))
        start = str(settings.get("start_date") or getattr(self, "_last_opt_start", "") or "")
        end = str(settings.get("end_date") or getattr(self, "_last_opt_end", "") or "")
        self._append_log(
            f"开始多品种验收：参数来自最近一次策略优化（{engine}），共 {len(symbols)} 个关注合约"
        )
        self._pick_running = True
        self._pick_cancel.clear()
        self._render_pick_board("正在用最近优化参数做多品种验收…", [])

        def _run() -> None:
            from src.backtest.optimizer import describe_params, format_perf_block, validate_params_on_symbols
            from src.screen.picker import save_picks

            try:
                rows = validate_params_on_symbols(
                    cfg,
                    params=params,
                    engine=engine,
                    symbols=symbols,
                    skip_symbol="",
                    start_date=start or None,
                    end_date=end or None,
                )
                if self._pick_cancel.is_set():

                    def _stopped() -> None:
                        self._pick_running = False
                        self._render_pick_board("多品种验收已停止。表里是已经测完的部分。", [])

                    self._post_to_main(_stopped)
                    return
                # 这里只展示各品种成绩，不替用户决定能不能写入配置，所以不拿训练净盈亏去卡。
                reason = "各品种成绩已列出。要不要写入配置，仍以策略优化页的检验结论为准。"
                from src.backtest.metrics import unit_expectancy
                from src.screen.picker import span_cover_fields

                board_rows: list[dict[str, str]] = []
                for row in rows:
                    err = str(row.get("error") or "")
                    code = str(row.get("symbol") or "")
                    cover = span_cover_fields(
                        row.get("net_pnl"),
                        row.get("max_drawdown"),
                        row.get("trades"),
                        start or "",
                        end or "",
                    )
                    if err:
                        cover_cells = {
                            "profit_cover": "—",
                            "trade_count": "—",
                            "pnl_per_trade": "—",
                            "span_start": "—",
                            "span_end": "—",
                            "span_days": "—",
                            "daily_avg": "—",
                        }
                    else:
                        per = cover["pnl_per_trade"]
                        daily = cover["daily_avg"]
                        days = cover["span_days"]
                        cover_cells = {
                            "profit_cover": f"{cover['profit_cover']:.0f}",
                            "trade_count": str(int(cover["trade_count"])),
                            "pnl_per_trade": f"{per:.2f}" if per is not None else "—",
                            "span_start": cover["span_start"] or "—",
                            "span_end": cover["span_end"] or "—",
                            "span_days": str(int(days)) if days is not None else "—",
                            "daily_avg": f"{daily:.2f}" if daily is not None else "—",
                        }
                    try:
                        avg_loss = float(row.get("avg_loss") or 0)
                    except (TypeError, ValueError):
                        avg_loss = 0.0
                    if err or int(row.get("trades") or 0) <= 0 or avg_loss <= 1e-9:
                        expect_text = "—"
                    else:
                        expect_text = f"{unit_expectancy(float(row.get('win_rate') or 0), float(row.get('avg_win') or 0), avg_loss):.2f}"
                    board_rows.append(
                        {
                            "symbol": code,
                            "strategy": engine,
                            "period": str(settings.get("timeframe") or "—"),
                            "win_rate": "-" if err else f"{float(row.get('win_rate') or 0):.0%}",
                            "profit_factor": "-" if err else f"{float(row.get('profit_factor') or 0):.2f}",
                            "payoff": "—" if err else "—",
                            "expectancy": expect_text,
                            "max_drawdown": "-" if err else f"{float(row.get('max_drawdown') or 0):.0f}",
                            "net_pnl": err if err else f"{float(row.get('net_pnl') or 0):.0f}",
                            "profit_cover": cover_cells["profit_cover"],
                            "return_rate": "—",
                            "frequency": f"{int(row.get('trades') or 0)}笔" if not err else "—",
                            "trade_count": cover_cells["trade_count"],
                            "pnl_per_trade": cover_cells["pnl_per_trade"],
                            "span": f"{start or '不限'}~{end or '不限'}",
                            "span_start": cover_cells["span_start"],
                            "span_end": cover_cells["span_end"],
                            "span_days": cover_cells["span_days"],
                            "daily_avg": cover_cells["daily_avg"],
                            "bars": str(int(row.get("bars") or 0)),
                            "score": "-" if err else f"{float(row.get('score') or 0):.2f}",
                            "liquidity": "优化参数验收",
                            "picked": "没测成" if err else "一般",
                        }
                    )
                lines = [
                    f"多品种验收完成（策略 {engine}）",
                    f"参数  {describe_params(params)}",
                    f"区间  {start or '不限'} ~ {end or '不限'}",
                    "说明  参数不再重搜，这是样本外多品种验收，不是交叉测试对照筛选。",
                    "",
                ]
                for row in rows:
                    if row.get("error"):
                        lines.append(f"{row.get('symbol')}  失败：{row.get('error')}")
                    else:
                        lines.extend(
                            format_perf_block(
                                str(row.get("symbol") or ""),
                                row,
                                bars=int(row.get("bars") or 0),
                            )
                        )
                        lines.append("")
                lines.append(reason or "各品种成绩已列出，请自己对照。")
                text = "\n".join(lines)
                save_picks(
                    self._picks_path(),
                    {
                        "baseline": {},
                        "candidates": [],
                        "rows": board_rows,
                        "report": text,
                        "ran_at": "",
                        "volume": settings.get("fixed_volume") or 1,
                        "mode": "opt_cross",
                        "settings": settings,
                        "skipped_thin": 0,
                    },
                )

                def _done() -> None:
                    self._pick_running = False
                    self._render_pick_board(
                        f"多品种验收完成：{len(rows)} 个合约。这是优化参数的样本外成绩，不是交叉测试的「较好」名单。",
                        board_rows,
                    )
                    self._append_log(f"多品种验收完成，测了 {len(rows)} 个合约")

                self._post_to_main(_done)
            except Exception as exc:  # noqa: BLE001
                logger.exception("多品种验收失败")
                err = str(exc)

                def _fail() -> None:
                    self._pick_running = False
                    self._render_pick_board(f"多品种验收失败：{err}", [])
                    self._append_log(f"多品种验收失败: {err}")
                    messagebox.showerror("多品种验收失败", err)

                self._post_to_main(_fail)

        threading.Thread(target=_run, daemon=True).start()


    def _on_pick_start(self) -> None:
        """后台把配置里的合约和参与策略配着回测。界面只负责显示进度，不自己算指标。"""
        from tkinter import messagebox

        from src.screen.picker import contracts_for_cross, engines_from_codes

        if self._pick_running:
            messagebox.showinfo("正在测试", "上一轮还没结束。可以点「停止测试」。")
            return
        engine_codes = self._pk_checked_engines()
        engines = engines_from_codes(engine_codes)
        symbol_codes = self._pk_checked_symbols()
        contracts = contracts_for_cross(symbol_codes)
        periods = self._pk_checked_periods()
        if not engines:
            messagebox.showwarning("还没有策略", "请在左边「参与策略」里至少勾一套。")
            return
        if not contracts:
            messagebox.showwarning("还没有合约", "请先到配置页添加关注品种，并在左边「品种合约」里勾选。")
            return
        if not periods:
            messagebox.showwarning("还没有周期", "请在左边「周期」里至少勾一档。信号和止损用同一个。")
            return
        try:
            settings = self._picker_settings()
        except ValueError as exc:
            messagebox.showerror("数字不对", str(exc))
            return
        cfg = self._clone_cfg()
        bt = cfg.setdefault("backtest", {})
        roll_var = getattr(self, "bt_rollover_var", None)
        chain_var = getattr(self, "bt_rollover_chain_var", None)
        bt["rollover_main"] = bool(roll_var.get()) if roll_var is not None else True
        bt["chain_capital"] = bool(chain_var.get()) if chain_var is not None else False
        engine_names = [code for code, _label in engines]
        live_st = self.cfg.setdefault("strategy", {})
        live_st["cross_engines"] = list(engine_names)
        live_st["cross_periods"] = list(periods)
        st = cfg.setdefault("strategy", {})
        st["cross_engines"] = list(engine_names)
        st["cross_periods"] = list(periods)
        combo_n = len(engines) * len(contracts) * len(periods)
        self._append_log(
            f"开始交叉测试：{len(engines)} 套策略 × {len(contracts)} 个合约 × {len(periods)} 个周期 = {combo_n} 组，"
            f"每个组合 {settings['fixed_volume']} 手（跟实盘手数），"
            f"间隔 {settings['min_order_interval_sec']} 秒，每天最多 {settings['max_orders_per_day']} 次，"
            f"收盘清仓={'开' if settings.get('flatten_at_close') else '关'}，"
            f"主力换月={'开' if bt.get('rollover_main') else '关'}，"
            f"评估本金 {settings.get('capital'):g} 元（本页账户权益），行情 {self.pk_source_var.get()}"
        )
        # 只用交叉测试页选中的这一家。配置页当时存的来源不参与这一轮。
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.pk_source_var.get())
        self._pick_running = True
        self._pick_cancel.clear()
        self._reset_pick_progress()
        self._render_pick_board("正在把勾选的策略、合约、周期交叉匹配，请稍候…", [])

        def _run() -> None:
            from src.screen.picker import (
                cross_worker_count,
                format_report,
                save_picks,
                screen_cross_parallel,
            )

            def on_progress(index: int, total: int, label: str) -> None:
                # 每测一个组合记一条日志；进度文案交给主线程合并刷新。
                self._post_to_main(lambda i=index, n=total, name=label: self._note_pick_progress(i, n, name))

            try:
                workers = cross_worker_count(cfg)
                self._post_to_main(
                    lambda: self._append_log(f"交叉测试最多用 {workers} 个进程（组合少时自动单进程）")
                )
                result = screen_cross_parallel(
                    cfg,
                    settings,
                    universe=contracts,
                    engines=engines,
                    periods=periods,
                    on_progress=on_progress,
                    cancel=self._pick_cancel.is_set,
                )
                text = format_report(result)
                save_picks(
                    self._picks_path(),
                    {
                        "baseline": {},
                        "candidates": result["candidates"],
                        "rows": result["rows"],
                        "report": text,
                        "ran_at": result["ran_at"],
                        "volume": result["volume"],
                        "mode": result.get("mode") or "baseline",
                        "settings": result.get("settings") or {},
                        "skipped_thin": result.get("skipped_thin") or 0,
                    },
                )
                def _done() -> None:
                    self._reset_pick_progress()
                    self._pick_running = False
                    self._show_pick_result(result)
                    better = sum(1 for row in result.get("rows") or [] if row.get("picked") == "较好")
                    self.events.publish(
                        CROSS_TEST_DONE,
                        {"rows": len(result.get("rows") or []), "better": better},
                    )

                self._post_to_main(_done)
            except Exception as exc:  # noqa: BLE001
                logger.exception("交叉测试失败")
                err = str(exc)

                def _fail() -> None:
                    self._reset_pick_progress()
                    self._pick_running = False
                    self._append_log(f"交叉测试失败: {err}")
                    self._render_pick_board(f"交叉测试失败：{err}", [])
                    messagebox.showerror("交叉测试失败", err)

                self._post_to_main(_fail)

        threading.Thread(target=_run, daemon=True).start()


    def _show_pick_progress(self, index: int, total: int, label: str) -> None:
        """优选进行中时，右边只留当前进度，测完再换成完整成绩。"""
        if not self._pick_running:
            return
        self._render_pick_board(
            f"正在测 {index}/{total}：{label}\n拉过的K线会留在电脑里，下一次会快一些。",
            [],
        )


    def _note_pick_progress(self, index: int, total: int, label: str) -> None:
        """记一条进度日志，并把最新的进度攒着；界面最多每 100 毫秒重画一次。"""
        if not self._pick_running:
            return
        self._append_log(f"交叉测试 {index}/{total} {label}")
        self._pick_progress_pending = (index, total, label)
        if self._pick_progress_after_id is None:
            self._pick_progress_after_id = self.after(100, self._flush_pick_progress)


    def _flush_pick_progress(self) -> None:
        """把攒着的最后一条进度画出来。中间那些已经被后一条盖掉了。"""
        self._pick_progress_after_id = None
        pending = self._pick_progress_pending
        self._pick_progress_pending = None
        if pending is None:
            return
        self._show_pick_progress(*pending)


    def _reset_pick_progress(self) -> None:
        """停掉挂起的刷新。开跑、完成、失败、停止都要调，免得旧进度盖住新结果。"""
        if self._pick_progress_after_id is not None:
            try:
                self.after_cancel(self._pick_progress_after_id)
            except Exception:  # noqa: BLE001
                # 窗口已经在关的时候 after_cancel 会抛 TclError，忽略即可。
                pass
        self._pick_progress_after_id = None
        self._pick_progress_pending = None


    def _on_pick_stop(self) -> None:
        """停在两个组合之间。已经测完的仍会标出较好的。"""
        if not self._pick_running:
            self._append_log("现在没有在做交叉测试")
            return
        self._pick_cancel.set()
        self._append_log("将在当前组合测完后停止")


    def _pack_cross_check_group(
        self,
        parent: ctk.CTkFrame,
        title: str,
        kind: str,
        height: int = 108,
    ) -> ctk.CTkScrollableFrame:
        """交叉测试左边一组可复选名单：标题、全选/全不选、可滚动勾选。"""
        ctk.CTkLabel(parent, text=title, font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(anchor="w")
        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", pady=(0, 2))
        ctk.CTkButton(
            btns,
            text="全选",
            width=64,
            height=24,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=lambda k=kind: self._pk_check_all(k, True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btns,
            text="全不选",
            width=64,
            height=24,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=lambda k=kind: self._pk_check_all(k, False),
        ).pack(side="left")
        box = ctk.CTkScrollableFrame(parent, height=height, fg_color=T.BG, corner_radius=8)
        box.pack(fill="x", pady=(0, 8))
        return box


    def _pk_vars_for(self, kind: str) -> dict[str, ctk.BooleanVar]:
        if kind == "engine":
            return self._pk_engine_vars
        if kind == "symbol":
            return self._pk_symbol_vars
        return self._pk_period_vars


    def _pk_check_all(self, kind: str, value: bool) -> None:
        """一组名单全部勾上或全部去掉。"""
        for var in self._pk_vars_for(kind).values():
            var.set(value)
        self._update_pk_combo_count()


    def _fill_check_frame(
        self,
        frame: ctk.CTkScrollableFrame | None,
        vars_map: dict[str, ctk.BooleanVar],
        items: list[tuple[str, str]],
        checked: set[str],
        *,
        default_all: bool,
    ) -> None:
        """按最新名单重画勾选框。已经勾过的尽量记住。"""
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        old = dict(vars_map)
        vars_map.clear()
        for code, label in items:
            if code in old:
                var = old[code]
            else:
                on = (code in checked) if checked else default_all
                var = ctk.BooleanVar(value=on)
            vars_map[code] = var
            text = label if label == code else f"{label}    {code}"
            ctk.CTkCheckBox(
                frame,
                text=text,
                variable=var,
                font=T.FONT_SMALL,
                text_color=T.TEXT,
                command=self._update_pk_combo_count,
            ).pack(anchor="w", padx=4, pady=1)
        try:
            frame._parent_canvas.yview_moveto(0)
        except Exception:
            pass


    def _pk_checked_engines(self) -> list[str]:
        return [code for code, var in self._pk_engine_vars.items() if var.get()]


    def _pk_checked_symbols(self) -> list[str]:
        return [code for code, var in self._pk_symbol_vars.items() if var.get()]


    def _pk_checked_periods(self) -> list[str]:
        from src.screen.picker import normalize_cross_periods

        return normalize_cross_periods([code for code, var in self._pk_period_vars.items() if var.get()])


    def _update_pk_combo_count(self) -> None:
        """左边提示这一轮会跑几组。"""
        label = getattr(self, "pk_combo_label", None)
        if label is None:
            return
        n_e = len(self._pk_checked_engines())
        n_s = len(self._pk_checked_symbols())
        n_p = len(self._pk_checked_periods())
        n = n_e * n_s * n_p
        if n <= 0:
            text = "请各至少勾一项。信号和止损用同一个周期。"
        else:
            text = f"将跑 {n} 组（{n_e} 套策略 × {n_s} 个合约 × {n_p} 个周期）。信号和止损用同一个周期。"
            if n > 80:
                text += " 组数较多，可能要跑比较久。"
        label.configure(text=text)


    def _refresh_cross_lists(self, prefer_period: str | None = None) -> None:
        """
        刷新交叉测试左边三份可复选名单。

        策略按当前程序能交叉测试的全部列出（含自定义规则），勾选跟配置里的 cross_engines。
        合约跟当前关注品种。周期勾选跟配置里的 cross_periods，没有就勾当前信号周期。
        """
        engine_box = getattr(self, "pk_engine_list", None)
        symbol_box = getattr(self, "pk_symbol_list", None)
        period_box = getattr(self, "pk_period_list", None)
        if engine_box is None or symbol_box is None or period_box is None:
            return
        from src.screen.picker import CROSS_PERIODS, contracts_for_cross, listed_cross_engines

        st = self.cfg.get("strategy") or {}
        raw_engines = st.get("cross_engines")
        engine_checked: set[str] = set()
        if isinstance(raw_engines, list) and raw_engines:
            engine_checked = {str(x).strip() for x in raw_engines if str(x).strip()}
        engine_items = listed_cross_engines()
        self._fill_check_frame(
            engine_box,
            self._pk_engine_vars,
            engine_items,
            engine_checked,
            default_all=not engine_checked,
        )
        hint = getattr(self, "pk_engine_hint", None)
        if hint is not None:
            hint.configure(
                text=(
                    f"共 {len(engine_items)} 套，跟当前程序能用的策略走，可滚动。"
                    "已删的不会再出现。以损定开、凯利突破参数太多，交叉测试不测这两套。"
                )
            )

        contracts = contracts_for_cross(self._watch_symbols)
        symbol_items = [(code, name) for code, name, _mult, _thin in contracts]
        self._fill_check_frame(
            symbol_box,
            self._pk_symbol_vars,
            symbol_items,
            set(),
            default_all=True,
        )

        raw_periods = st.get("cross_periods")
        period_checked: set[str] = set()
        if isinstance(raw_periods, list) and raw_periods:
            period_checked = set(raw_periods)
        elif prefer_period:
            period_checked = {prefer_period}
        else:
            live = self._period_code(self.tf_var, self.tf_custom_var)
            period_checked = {live} if live else {"5"}
        self._fill_check_frame(
            period_box,
            self._pk_period_vars,
            list(CROSS_PERIODS),
            period_checked,
            default_all=False,
        )
        if prefer_period:
            for code, var in self._pk_period_vars.items():
                var.set(code == prefer_period)
        self._update_pk_combo_count()


    def _on_export_cross(self) -> None:
        """把最近一轮交叉测试追加到统一 Excel 的「交叉测试」表，不盖掉以前的轮次。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_cross

        report = self._picks_path()
        if not report.exists():
            messagebox.showwarning("还不能导出", "请先点「开始交叉测试」，跑完后再导出。")
            return
        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title="导出交叉测试到统一 Excel",
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, added, total = export_cross(self.root_dir, path)
        except ValueError as exc:
            messagebox.showwarning("还不能导出", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("导出交叉测试失败")
            messagebox.showerror("导出失败", self._export_fail_text(path, exc))
            return
        self._clear_cross_results()
        messagebox.showinfo(
            "已导出",
            f"已追加到工作表「交叉测试」。\n这次新增 {added} 行，表里共 {total} 行。\n"
            "界面上的结果已清空，下次打开不会再画这些行。\n\n"
            f"文件：\n{out}"
            + self._export_saved_note(path, out),
        )
        self._append_log(f"交叉测试已导出到 {out}，新增 {added} 行，共 {total} 行。界面上的结果已清空")


    def _clear_cross_results(self) -> None:
        """
        导出成功后清掉交叉测试表。

        界面上的行删掉，保存的那份结果也删掉，下次打开不用再画。
        Excel 里的记录不动。这一轮如果还在测，进度提示先留着，测完仍会显示新结果。
        """
        from src.screen.picker import clear_saved_picks

        try:
            clear_saved_picks(self._picks_path())
        except OSError as exc:
            logger.warning("清掉交叉测试结果失败: {}", exc)
            return
        self._pick_baseline = None
        if self._pick_running:
            return
        self._render_pick_board(
            "结果已导出，界面上的表已清空。要再看，打开导出的 Excel。",
            [],
            empty_text="结果已导出，这里先空着。要再看，打开导出的 Excel。",
        )

