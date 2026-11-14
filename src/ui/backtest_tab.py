"""Historical-backtest controls, execution, sensitivity, local data tools, and exports."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from loguru import logger
from pathlib import Path
from src.data.periods import period_label
from src.ui import theme as T
from src.ui.app_common import ENGINE_CHOICES
from src.ui.app_common import ENGINE_LABELS
from src.ui.app_common import _ui_engine
from src.utils import save_config
from src.utils.trading_day import futures_trading_day
from tkinter import messagebox
import customtkinter as ctk
import os
import subprocess
import sys
import threading


class BacktestTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_backtest_tab(self, tab: ctk.CTkFrame) -> None:
        """历史回测：可选策略、品种、周期、时间段，结果写在本页。"""
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="历史回测", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="用历史K线模拟开平仓。分钟、日线、周线都可以选。开始日期比网上行情更早时，会先下载新K线，再接到电脑里已经存好的K线上；这段本地已经齐，就直接用本地的。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        engines = list(ENGINE_CHOICES)
        self._option_row(box, "策略引擎", self.bt_engine_var, engines, width=240)
        from src.data.quote_check import BACKTEST_SOURCE_LABELS

        self._option_row(box, "行情数据源", self.bt_source_var, list(BACKTEST_SOURCE_LABELS), width=160)
        self.bt_source_hint = ctk.CTkLabel(
            box,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        )
        self.bt_source_hint.pack(anchor="w", pady=(0, 4))
        try:
            self.bt_source_var.trace_add("write", lambda *_: self._refresh_bt_source_hint())
        except Exception:  # noqa: BLE001
            pass
        self._refresh_bt_source_hint()
        # 和配置页关注品种用同一份合约。先放进下拉，避免打开时选中的合约不在列表里。
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.bt_symbol_var.get() not in symbol_values:
            self.bt_symbol_var.set(symbol_values[0])
        self.bt_symbol_menu = self._option_row(box, "历史回测品种", self.bt_symbol_var, symbol_values, width=160)
        try:
            self.bt_symbol_var.trace_add("write", lambda *_: self._sync_bt_margin_from_table())
        except Exception:  # noqa: BLE001
            pass
        self._sync_bt_margin_from_table()
        ctk.CTkLabel(
            box,
            text="和配置页的关注品种是同一份。在那边添加或移出，这里的列表一起变。要改成配置页当前选中的合约，点下面「按配置页重新填入」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._timeframe_row(box, "信号周期", self.bt_tf_var, self.bt_tf_custom_var, width=140)
        self._timeframe_row(box, "止损周期", self.bt_trail_var, self.bt_trail_custom_var, width=140)
        self._option_row(
            box,
            "时间段",
            self.bt_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.bt_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.bt_end_var, hint="点日历；清空=到最新")
        flat_row = ctk.CTkFrame(box, fg_color="transparent")
        flat_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            flat_row,
            text="收盘清仓",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            flat_row,
            text="",
            variable=self.bt_flatten_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=到点强平。关=仓可以留过收盘。趋势跟随不听这个开关，仍用它自己的设置。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        roll_row = ctk.CTkFrame(box, fg_color="transparent")
        roll_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            roll_row,
            text="主力合约换月",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            roll_row,
            text="",
            variable=self.bt_rollover_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=按「主力时段」分段回测。换月日当天仍用旧合约，次日切新主力：若旧仓还在，就在新主力同方向移过去（止损改挂新主力均线）。任何合约都不进入自己的交割月，到点改用下一手。关=只测当前选的合约（交割月同样不开、有仓会平）。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        chain_row = ctk.CTkFrame(box, fg_color="transparent")
        chain_row.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            chain_row,
            text="真实账户口径",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            chain_row,
            text="",
            variable=self.bt_rollover_chain_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=手数跟着账户剩余权益走：换月时上一段接到下一段，同一段里赚了可能多开、亏多了可能开不了。关=每一笔都按同一份起始资金，便于和单合约、和当时实盘对照。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        table_row = ctk.CTkFrame(box, fg_color="transparent")
        table_row.pack(fill="x", pady=(0, 4))
        self.bt_table_btn = ctk.CTkButton(
            table_row,
            text="更新主力合约表",
            width=150,
            height=30,
            command=self._on_update_main_table,
        )
        self.bt_table_btn.pack(side="left", padx=(148, 8))
        self.bt_table_open_btn = ctk.CTkButton(
            table_row,
            text="打开表格",
            width=84,
            height=30,
            fg_color=T.BG_ELEVATED,
            hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
            border_width=1,
            border_color=T.BORDER,
            command=self._on_open_main_table,
        )
        self.bt_table_open_btn.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            box,
            text="表只在点「更新」时重算。要改某品种的换月：点「打开表格」，在「手动修正」页加行，更新时该行保留。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 2))
        self.bt_table_state = ctk.CTkLabel(
            box,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        )
        self.bt_table_state.pack(anchor="w", padx=(148, 0), pady=(0, 6))
        self._refresh_main_table_state()

        fill_row = ctk.CTkFrame(box, fg_color="transparent")
        fill_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            fill_row,
            text="下一根开盘成交",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            fill_row,
            text="",
            variable=self.bt_fill_next_open_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=信号这根收盘确认后，按下一根开盘价成交，和实盘一致；关=按当根收盘价。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._entry_row(
            box,
            "滑点(跳)",
            self.bt_slippage_var,
            hint="0=不加滑点；每跳价格优先用品种表的「每跳价格」",
            width=160,
        )
        ctk.CTkButton(
            box,
            text="用实盘滑点建议填入",
            height=28,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_apply_slippage_hint,
        ).pack(anchor="w", padx=(148, 0), pady=(0, 6))
        ctk.CTkLabel(
            box,
            text="从成交滑点记录算出建议跳数，点一下才填进上面的框，不会自动改。没有真实成交时请不要用。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", padx=(148, 0), pady=(0, 6))

        self._pack_open_limits(left)

        ctk.CTkButton(
            left,
            text="开始历史回测",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_backtest,
        ).pack(fill="x", padx=4, pady=(12, 6))
        ctk.CTkButton(
            left,
            text="滑点灵敏度",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_slippage_scan,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="同一份参数，只换滑点跳数各跑一遍：看利润边缘有多厚、滑点加到几跳由盈转亏。K线只拉一次。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkButton(
            left,
            text="导出本次历史回测",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_backtest,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="导出追加到「回测摘要」「回测成交」「回测信号」。第一列都是回测时间，同一次的行时间相同，用来分清是哪一次。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkButton(
            left,
            text="画出因子衰减图",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_plot_factor_decay,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="用最近一次回测和模拟盘平仓记录，画出四个因子最近 20 笔滚动盈亏。只画图，不会改开平仓。没有多因子分数的成交不会进图。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
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
        ctk.CTkLabel(
            left,
            text="库存表直接打开 Excel。抽查某一根：选数据源、合约、周期、日期和时间，对照同花顺开高低收。对应 CSV 可用 Excel 打开；sqlite 请用文件夹入口，不要拿 Excel 硬开。某一家质量差时，可按数据源、合约、时间段删掉再重新拉。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
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
            text="先换新浪、东方财富、天勤各拉一次；同花顺图请点下面「从同花顺图读取K线」。再点这里生成 Excel：总览、问题K线、各家开高低收可筛选。只读本地、不下单。同花顺图不会进实盘。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        self._entry_row(
            left,
            "同花顺图读几根",
            self.bt_ths_bars_var,
            hint="默认 800，最多 5000。点按钮后先等 3 秒。",
            width=80,
        )
        ths_row = ctk.CTkFrame(left, fg_color="transparent")
        ths_row.pack(fill="x", padx=4, pady=(0, 4))
        self.bt_ths_scrape_btn = ctk.CTkButton(
            ths_row,
            text="从同花顺图读取K线",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_scrape_ths_chart,
        )
        self.bt_ths_scrape_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.bt_ths_stop_btn = ctk.CTkButton(
            ths_row,
            text="停止读图",
            height=34,
            width=96,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_stop_ths_chart,
            state="disabled",
        )
        self.bt_ths_stop_btn.pack(side="left")
        ctk.CTkLabel(
            left,
            text="打开期货通，主图选好合约和周期。点按钮后先等几秒再接管鼠标。读的时候按 ESC 或点「停止读图」可停，已读到的会先存下来。存到 @ths，只给回测。不会点买卖。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkButton(
            left,
            text="把条件同步到「策略优化」页",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._sync_bt_to_opt,
        ).pack(fill="x", padx=4, pady=(0, 8))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="历史回测结果", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.bt_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.bt_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.bt_result_box.insert("end", "点左侧「开始历史回测」后，这里显示胜率、盈亏和成交明细摘要。\n")


    def _refresh_bt_source_hint(self) -> None:
        """选同花顺图时说明怎么读、为什么不能实盘用。"""
        label = getattr(self, "bt_source_hint", None)
        if label is None:
            return
        from src.data.quote_check import is_ths_source

        if is_ths_source(self.bt_source_var.get()):
            label.configure(
                text="同花顺图：回测只用已经点「从同花顺图读取K线」存进本地的那一份，"
                "不会再去期货通抢鼠标。没有库存时请先读图。只给历史回测，不会拿去扫描下单。"
            )
        else:
            label.configure(text="")


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


    def _pack_open_limits(self, parent) -> None:
        """
        历史回测页和策略优化页共用同一组开仓限制。

        两边绑的是同一批输入框变量，所以在策略优化页改权益或关掉开关，历史回测页会一起变。
        策略优化的每一组参数都按这套限制打分，避免选出实盘根本下不出去的参数。
        """
        box_risk = self._card(parent)
        ctk.CTkLabel(
            box_risk,
            text="开仓和止损限制",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            box_risk,
            text="开仓限制、保证金、收盘清仓与实盘配置一致：开跑前自动按配置页和品种表对齐。本页可改日期和「启用限制」；关掉限制后固定 1 手。点「按配置页重新填入」可连策略品种一起抄过来。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        limit_row = ctk.CTkFrame(box_risk, fg_color="transparent")
        limit_row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            limit_row,
            text="启用限制",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            limit_row,
            text="",
            variable=self.bt_limits_on_var,
            progress_color=T.OK,
        ).pack(side="left")
        menu = self._option_row(box_risk, "仓位模式", self.bt_sizing_var, ["risk_pct", "fixed"], width=160)
        # 开跑前会按配置页覆盖，这里不让改，避免白填一遍。
        menu.configure(state="disabled")
        self._entry_row(
            box_risk,
            "账户权益(元)",
            self.bt_capital_var,
            hint="跟配置页一致，开跑前自动对齐",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk,
            "兜底每手保证金(元)",
            self.bt_margin_var,
            hint="表里填了保证金比例时按开仓价现算，不用这个数。没填比例才用。0=不限制",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk,
            "每笔风险%",
            self.bt_risk_pct_var,
            hint="跟配置页一致，2 表示权益的 2%",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk,
            "仓位上限%",
            self.bt_margin_usage_pct_var,
            hint="跟配置页一致；权益×此比例内交保证金",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk, "固定手数", self.bt_volume_var, hint="跟配置页一致", width=160, readonly=True
        )
        self._entry_row(
            box_risk, "最少手数", self.bt_min_vol_var, hint="跟配置文件 risk.min_volume 一致", width=160, readonly=True
        )
        self._entry_row(
            box_risk, "日最大亏损(元)", self.bt_max_loss_var, hint="跟配置页一致，当天亏到这里就停开", width=160, readonly=True
        )
        self._entry_row(box_risk, "开仓最小间隔秒", self.bt_interval_var, hint="跟配置页一致", width=160, readonly=True)
        self._entry_row(
            box_risk, "每天最多开仓", self.bt_max_orders_var, hint="跟配置页一致，次数换日清零", width=160, readonly=True
        )
        ctk.CTkButton(
            box_risk,
            text="按配置页重新填入",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=lambda: self._copy_risk_to_backtest(full=True),
        ).pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(
            box_risk,
            text="从配置页抄到本页：策略、品种、周期、行情来源、余额与开仓数字，以及收盘清仓。优化/复盘的品种也会改成这个合约。启用限制仍由本页开关决定。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkButton(
            box_risk,
            text="把本页参数写入配置页",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._write_backtest_to_config_page,
        ).pack(fill="x", pady=(0, 0))
        ctk.CTkLabel(
            box_risk,
            text="把本页测完的内容写回配置并保存：策略、品种、信号/止损周期、行情来源、三个开关，以及开仓手数等限制。不会改实盘余额，也不会写回测日期。启用限制开关只影响回测，不写进配置。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))


    def _format_limit_lines(self, snap: dict) -> str:
        """结果里写明这次有没有套用开仓限制，方便和上一把对比。"""
        if not snap.get("apply_limits", True):
            return "开仓限制  关（固定1手，不看止损距离、日亏损和开仓次数）"
        skipped = self._format_skipped(snap.get("skipped_opens") or {})
        return (
            f"开仓限制  开\n"
            f"仓位      {snap.get('sizing_mode')}  起始权益{float(snap.get('capital') or 0):.0f}元"
            f"  每笔{snap.get('risk_per_trade_pct')}%"
            f"  仓位上限{snap.get('margin_usage_pct', '—')}%"
            f"  固定{snap.get('fixed_volume')}手"
            f"{'  手数跟剩余权益走（真实账户口径开）' if snap.get('chain_capital') or snap.get('rollover_chain') else '  手数按起始权益（真实账户口径关）'}\n"
            f"限制      最少{snap.get('min_volume')}手"
            f"  日亏停{float(snap.get('max_daily_loss') or 0):.0f}元"
            f"  间隔{snap.get('min_order_interval_sec')}秒"
            f"  每天最多开{snap.get('max_orders_per_day')}次"
            f"  兜底每手保证金{float(snap.get('margin_per_lot') or 0):.0f}元（有比例时按价格现算）\n"
            f"凯利      {'按手填胜率/盈亏比算手数' if snap.get('kelly_in_backtest') else '关（按仓位模式和每笔风险%）'}\n"
            f"{skipped}"
        )


    def _format_skipped(self, skipped: dict) -> str:
        """把没开成的原因收成一行。没有跳过就写明这次限制没有挡掉信号。"""
        if not skipped:
            return "跳过开仓  无"
        parts = [f"{name}{count}次" for name, count in skipped.items()]
        total = sum(int(v) for v in skipped.values())
        return f"跳过开仓  {total} 次（" + "，".join(parts) + "）"


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

        策略引擎 / 周期 / 品种仍可由回测页单独选，方便对比；真正影响手数和保证金的数字跟实盘走。
        """
        if not hasattr(self, "bt_capital_var"):
            return
        risk = self.cfg.get("risk") or {}
        self.bt_sizing_var.set(self.sizing_mode_var.get() or "risk_pct")
        self.bt_capital_var.set(self.capital_var.get())
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
            return
        try:
            # 一律读配置页（实盘）数字，避免回测/优化页旧值跟实盘不一致。
            risk["sizing_mode"] = self.sizing_mode_var.get() or "risk_pct"
            risk["capital"] = float(self.capital_var.get())
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
            raise ValueError("配置页的权益、风险和手数要填数字") from e
        code = (symbol or self.bt_symbol_var.get() or "").strip()
        # 有保证金比例时按每笔开仓价现算，不要绑死一个金额把比例清掉。
        risk["margin_per_lot"] = generic_margin
        logger.info(
            "开仓限制已按实盘对齐 | 权益={:g} 风险%={} 模式={} 品种={} 兜底保证金{:g}",
            risk["capital"],
            risk["risk_per_trade_pct"],
            risk["sizing_mode"],
            code or "?",
            generic_margin,
        )


    def _prepare_backtest_run(self) -> tuple[dict, str, str | None, str | None] | None:
        """把回测页的选项收成一份可跑的回测配置。参数不对时弹窗并返回 None。"""
        from tkinter import messagebox

        # 先对齐开仓限制，再克隆，保证副本里就是实盘数字。
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


    def _ths_bar_limit(self) -> int:
        """读图根数。写错数字时用 800，最少 10，最多 5000。"""
        raw = (self.bt_ths_bars_var.get() or "").strip() or "800"
        try:
            n = int(float(raw))
        except ValueError:
            n = 800
        return max(10, min(5000, n))


    def _set_ths_scraping(self, busy: bool) -> None:
        """读图进行中：禁止再点读取，允许点停止。"""
        self._ths_scraping = bool(busy)
        scrape = getattr(self, "bt_ths_scrape_btn", None)
        stop = getattr(self, "bt_ths_stop_btn", None)
        if scrape is not None:
            scrape.configure(state="disabled" if busy else "normal")
        if stop is not None:
            stop.configure(state="normal" if busy else "disabled")


    def _on_escape_key(self, _event=None):
        """读同花顺图时 ESC 当停止。其它时候不拦。"""
        if self._ths_scraping:
            self._on_stop_ths_chart()
            return "break"
        return None


    def _on_stop_ths_chart(self) -> None:
        """点停止或按 ESC：读图循环下一拍就停，已读到的会存盘。"""
        from src.data.ths_chart import request_ths_stop

        if not self._ths_scraping:
            return
        request_ths_stop()
        self._append_log("已请求停止读图。按 ESC 也可以。已读到的会先存下来。")


    def _confirm_ths_chart(self) -> bool:
        """读图前请用户确认期货通已经打开。取消就不读。"""
        from tkinter import messagebox

        n = self._ths_bar_limit()
        return bool(
            messagebox.askokcancel(
                "从同花顺图读K线",
                "请先打开期货通，主图选好这个合约和这个周期。\n"
                f"点确定后先等 3 秒，再开始读，最多 {n} 根。\n"
                "读的时候请不要动鼠标键盘。按 ESC 或点「停止读图」可停。\n"
                "信息框顶部是这根K线的结束时间。不会点买卖。\n\n继续？",
            )
        )


    def _on_scrape_ths_chart(self) -> None:
        """只把同花顺图读进本地库，不跑策略。方便再点检查行情质量。"""
        import threading
        from tkinter import messagebox

        from src.data.kline import KlineProvider
        from src.data.ths_chart import ThsScrapeStopped

        symbol = (self.bt_symbol_var.get() or "").strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约。")
            return
        if self._ths_scraping:
            messagebox.showinfo("正在读图", "正在读同花顺图。要停请按 ESC 或点「停止读图」。")
            return
        period = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        limit = self._ths_bar_limit()
        if not self._confirm_ths_chart():
            return
        self._set_ths_scraping(True)
        self._append_log(
            f"准备从同花顺图读取 {symbol} {period}，最多 {limit} 根。"
            "3 秒后开始，请把主图放到前面。按 ESC 可取消。"
        )

        def work() -> None:
            try:
                cfg = self._clone_cfg()
                data = cfg.setdefault("data", {})
                data["source"] = "ths"
                ths_cfg = dict(data.get("ths_chart") or {})
                ths_cfg["max_bars"] = limit
                ths_cfg.setdefault("ready_sec", 3)
                data["ths_chart"] = ths_cfg
                provider = KlineProvider(cfg)
                # 直接读图再入库。回测 fetch 只用本地库，不会再抢鼠标。
                frame = provider.import_ths_chart(symbol, period, limit=limit)
                n = 0 if frame is None else len(frame)
                last = "" if frame is None or n == 0 else str(frame.index[-1])
                self.after(
                    0,
                    lambda n=n, last=last: self._append_log(
                        f"同花顺图已存 {n} 根，最新 {last or '无'}（@ths）"
                    ),
                )
            except ThsScrapeStopped as exc:
                err = str(exc)
                self.after(0, lambda m=err: self._append_log(m))
                self.after(0, lambda m=err: messagebox.showinfo("已停止", m))
            except Exception as exc:  # noqa: BLE001
                # Python 3.12 结束后会清掉 except 里的 exc，after 里再 str(exc) 会变成 NameError。
                err = str(exc)
                logger.exception("从同花顺图读取失败")
                self.after(0, lambda m=err: messagebox.showerror("没读到", m))
            finally:
                self.after(0, lambda: self._set_ths_scraping(False))

        threading.Thread(target=work, daemon=True).start()


    def _on_backtest(self) -> None:
        """后台跑历史回测，结果写本页 + data/backtest_last.json。"""
        prepared = self._prepare_backtest_run()
        if prepared is None:
            return
        cfg, symbol, start, end = prepared
        st = cfg["strategy"]

        self._append_log(
            f"开始历史回测… engine={st['engine']} symbol={symbol} "
            f"行情={self.bt_source_var.get()} "
            f"区间={start or '起'}~{end or '止'} "
            f"换月={'按主力时段' if self.bt_rollover_var.get() else '否'} "
            f"口径={'真实账户' if self.bt_rollover_chain_var.get() else '固定起始资金'}"
        )
        self.tabs.set("历史回测")
        if hasattr(self, "bt_result_box"):
            self._set_result_text(
                self.bt_result_box,
                "历史回测进行中，请稍候…\n正在下载行情。网络慢时最多大约 20 秒，然后改用电脑里已经有的K线。\n",
            )

        def _run() -> None:
            try:
                from src.backtest.engine import run_backtest, save_backtest_report
                from src.backtest.rollover import run_backtest_rollover, format_rollover_lines

                chain = bool(self.bt_rollover_chain_var.get())
                if self.bt_rollover_var.get():
                    result = run_backtest_rollover(
                        cfg,
                        symbol=symbol,
                        start_date=start,
                        end_date=end,
                        engine=st["engine"],
                        chain_capital=chain,
                    )
                else:
                    result = run_backtest(
                        cfg,
                        symbol=symbol,
                        start_date=start,
                        end_date=end,
                        engine=st["engine"],
                        chain_capital=chain,
                    )
                path = self.root_dir / "data" / "backtest_last.json"
                save_backtest_report(result, path)
                p = result.performance
                snap = result.params_snapshot or {}
                rollover_text = format_rollover_lines(snap)
                note = ""
                if p.trades < 3:
                    note = (
                        f"\n注意：K线 {result.bars} 根、成交仅 {p.trades} 笔，"
                        "胜率参考价值低（可改成「全部可用」再试）。"
                    )
                from src.backtest.metrics import format_expectancy_line, format_payoff_lines
                from src.screen.picker import format_cover_lines

                # 摘要 + 最近几笔成交。盈利覆盖等和交叉测试同一套算法，方便对照。
                lines = [
                    f"历史回测完成  {result.symbol}",
                    f"跑完时间  {result.ran_at}",
                    f"策略      {snap.get('engine', st['engine'])}",
                    "收盘清仓  开（到点强平）"
                    if snap.get("flatten_at_close", True)
                    else "收盘清仓  关（可留过收盘；区间最后一根仍会按收盘价结算未平的仓）",
                    f"周期      信号{period_label(result.timeframe)} / 止损{period_label(result.trail_timeframe)}",
                    self._format_limit_lines(snap),
                    f"你选的区间  {snap.get('start_date') or '不限'} ~ {snap.get('end_date') or '到最新'}",
                    f"行情能给的  {str(snap.get('available_from', ''))[:16]}  →  {str(snap.get('available_to', ''))[:16]}",
                    f"实际区间    {str(snap.get('data_from', ''))[:16]}  →  {str(snap.get('data_to', ''))[:16]}",
                    str(snap.get("coverage_note") or "").strip(),
                    *([rollover_text] if rollover_text else []),
                    str(snap.get("fill_note") or "").strip(),
                    f"K线根数   {result.bars}",
                    f"成交      {p.trades} 笔（赢{p.wins} / 亏{p.losses}）",
                    f"没开成    {len(result.rejected)} 条开仓信号（被策略过滤或风控挡住，明细在下面）"
                    if result.rejected
                    else "没开成    无（每个开仓信号都成交了）",
                    f"胜率      {p.win_rate:.0%}",
                    *format_payoff_lines(p.avg_win, p.avg_loss, p.payoff),
                    format_expectancy_line(p.win_rate, p.avg_win, p.avg_loss, p.expectancy),
                    f"净盈亏    {p.net_pnl:.0f} 元",
                    f"盈利因子  {p.profit_factor:.2f}",
                    f"最大回撤  {p.max_drawdown:.0f} 元（{p.max_drawdown_pct:.2%}）",
                    *format_cover_lines(
                        p.net_pnl,
                        p.max_drawdown,
                        p.trades,
                        str(snap.get("data_from") or ""),
                        str(snap.get("data_to") or ""),
                    ),
                    f"报告      {path}",
                    note.strip(),
                    "",
                    "—— 成交明细（最多显示最近 15 笔）——",
                ]
                for t in result.trades[-15:]:
                    lines.append(
                        f"{t.entry_time[:16]}→{t.exit_time[:16]} {t.side}{t.volume}手 "
                        f"{t.entry_price:.1f}->{t.exit_price:.1f} "
                        f"pnl={t.pnl:.0f} | {t.reason_exit[:40]}"
                    )
                if result.rejected:
                    lines.append("")
                    lines.append(
                        f"—— 有开仓信号但没做（同一交易时段同类原因只记一次，共 "
                        f"{len(result.rejected)} 条，最多显示最近 20 条）——"
                    )
                    for item in result.rejected[-20:]:
                        price = f"{item.price:.2f}" if item.price else "—"
                        stop = f"{item.stop:.2f}" if item.stop else "—"
                        lines.append(
                            f"{item.time[:16]} {item.side} 价{price} 止损{stop} "
                            f"| {item.stage}：{item.reason[:80]}"
                        )
                text = "\n".join(x for x in lines if x is not None)
                self._post_to_main(lambda: self._append_log(
                    f"历史回测完成 {result.symbol} | {result.bars}根 | "
                    f"{p.trades}笔 胜率{p.win_rate:.0%} 盈亏比{p.payoff:.2f} "
                    f"期望{p.expectancy:.2f} 净盈亏{p.net_pnl:.0f}"
                ))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("历史回测失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"历史回测失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.bt_result_box, f"历史回测失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("历史回测失败", m))

        threading.Thread(target=_run, daemon=True).start()


    def _main_table_path(self) -> Path:
        """当前回测会用的主力合约表路径。"""
        from src.backtest.rollover import resolve_main_table

        found = resolve_main_table({"_project_root": str(self.root_dir)})
        return found if found else (self.root_dir / "config" / "主力合约表.xlsx")


    def _refresh_main_table_state(self) -> None:
        """按钮旁显示表的最后更新时间，方便确认表是不是旧的。"""
        if not hasattr(self, "bt_table_state"):
            return
        path = self._main_table_path()
        if path.exists():
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self.bt_table_state.configure(text=f"更新于 {stamp}")
        else:
            self.bt_table_state.configure(text="还没有表，点「更新」生成")


    def _on_open_main_table(self) -> None:
        """用系统默认程序打开主力合约表，方便直接改「手动修正」页。"""
        from tkinter import messagebox

        path = self._main_table_path()
        if not path.exists():
            messagebox.showinfo(
                "还没有主力合约表",
                "先点「更新主力合约表」生成，再打开。\n生成后请在「手动修正」页里加行。",
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法打开", f"打不开主力合约表。\n\n{exc}")


    def _on_update_main_table(self) -> None:
        """手动更新主力合约表：只有点这个按钮才会重新拉行情，平时不会自动更新。"""
        from tkinter import messagebox

        if getattr(self, "_main_table_busy", False):
            return
        path = self._main_table_path()
        if not messagebox.askyesno(
            "更新主力合约表",
            f"现在重新下载日行情并重算主力时段？\n\n输出：{path}\n\n"
            "「手动修正」页会原样保留，不会被覆盖。\n"
            "大商所品种要逐个合约拉日线，会慢一些，期间可以切到其它页面。",
        ):
            return
        self._main_table_busy = True
        if hasattr(self, "bt_table_btn"):
            self.bt_table_btn.configure(state="disabled", text="更新中…")
        if hasattr(self, "bt_table_state"):
            self.bt_table_state.configure(text="正在更新，请稍候…")
        self._append_log("开始更新主力合约表（手动触发）…")
        start = f"{date.today().year}-01-01"

        def _progress(msg: str) -> None:
            self._post_to_main(lambda m=msg: self._append_log(m))

        def _run() -> None:
            ok = False
            try:
                from src.backtest.main_table import update_table

                summary = update_table(self.root_dir, start=start, out=path, progress=_progress)
                self._post_to_main(lambda s=summary: self._append_log(
                    f"主力合约表已更新：{s['products']} 个品种、{s['periods']} 段 | "
                    f"区间 {s['start']}~{s['end']}"
                ))
                ok = True
            except Exception as e:  # noqa: BLE001
                logger.exception("主力合约表更新失败")
                err = str(e)
                if isinstance(e, PermissionError):
                    err = f"写不进表，文件正被占用。请先关闭 Excel 里打开的{path.name}再更新。\n（{e}）"
                self._post_to_main(lambda m=err: self._append_log(f"主力合约表更新失败: {m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("主力合约表更新失败", m))
            finally:
                self._post_to_main(lambda good=ok: self._finish_main_table_update(good))

        threading.Thread(target=_run, daemon=True).start()


    def _finish_main_table_update(self, ok: bool) -> None:
        """恢复按钮，并刷新表的时间戳。"""
        self._main_table_busy = False
        if hasattr(self, "bt_table_btn"):
            self.bt_table_btn.configure(state="normal", text="更新主力合约表")
        if not ok and hasattr(self, "bt_table_state"):
            self.bt_table_state.configure(text="更新失败，表没有改动")
            return
        self._refresh_main_table_state()


    def _on_apply_slippage_hint(self) -> None:
        """把实盘滑点 70 分位建议填进回测滑点框。要点确认，不会自己改。"""
        from tkinter import messagebox

        from src.account.slippage_log import summarize

        info = summarize(self.root_dir)
        if not info["n"]:
            messagebox.showinfo(
                "实盘滑点",
                "还没有滑点记录。有真实成交（尤其是 CTP 回报）之后才会给出建议。",
            )
            return
        suggested = float(info["suggested"] or 0)
        scope = "真实成交" if info["used_real"] else "全部记录（含点击假定成交，仅供参考）"
        if not messagebox.askyesno(
            "填入回测滑点",
            f"样本 {info['n']} 笔（{scope}）。\n"
            f"中位 {info['median']:+.2f} 跳，70分位 {info['p70']:+.2f} 跳。\n"
            f"建议回测填 {suggested:g} 跳。\n\n"
            "只改历史回测页这个数字，不改开平仓规则。要填入吗？",
        ):
            return
        self.bt_slippage_var.set(str(suggested))
        self.cfg.setdefault("backtest", {})["slippage_ticks"] = suggested
        self._append_log(f"回测滑点已改成建议值 {suggested:g} 跳（未自动保存配置文件）")


    def _on_slippage_scan(self) -> None:
        """滑点灵敏度：同一份K线换几档滑点重跑，看利润边缘和盈亏分水岭。"""
        from tkinter import messagebox

        prepared = self._prepare_backtest_run()
        if prepared is None:
            return
        cfg, symbol, start, end = prepared
        st = cfg["strategy"]

        self._append_log(f"开始滑点灵敏度… {symbol} 区间={start or '起'}~{end or '止'}")
        self.tabs.set("历史回测")
        if hasattr(self, "bt_result_box"):
            self._set_result_text(
                self.bt_result_box,
                "滑点灵敏度进行中…\nK线只拉一次，之后每档滑点各跑一遍，大约比单次回测多几倍时间。\n",
            )

        def _run() -> None:
            try:
                from src.backtest.engine import FillModel
                from src.backtest.sensitivity import format_slip_scan, run_slippage_scan
                from src.risk.sizing import PositionSizer

                scan = run_slippage_scan(
                    cfg,
                    symbol=symbol,
                    start_date=start,
                    end_date=end,
                    engine=st.get("engine"),
                )
                try:
                    current = float(self.bt_slippage_var.get() or 0)
                except (TypeError, ValueError):
                    current = 1.0
                text = format_slip_scan(
                    scan,
                    symbol=symbol,
                    current_slip=current,
                    tick=FillModel.from_cfg(cfg, symbol).price_tick,
                    mult=PositionSizer(cfg).multiplier_for(symbol),
                )
                self._post_to_main(lambda: self._append_log(f"滑点灵敏度完成 {symbol}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("滑点灵敏度失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"滑点灵敏度失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.bt_result_box, f"滑点灵敏度失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("滑点灵敏度失败", m))

        threading.Thread(target=_run, daemon=True).start()


    def _on_export_backtest(self) -> None:
        """把最近一次回测追加到统一 Excel 的「回测摘要」「回测成交」「回测信号」，不盖掉以前的行。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_backtest

        report = self.root_dir / "data" / "backtest_last.json"
        if not report.exists():
            messagebox.showwarning("还不能导出", "请先点「开始历史回测」，跑完后再导出。")
            return
        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title="导出历史回测到统一 Excel",
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, summary_added, trade_added, summary_total, trade_total = export_backtest(self.root_dir, path)
        except ValueError as e:
            messagebox.showwarning("还不能导出", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("导出历史回测失败")
            messagebox.showerror("导出失败", self._export_fail_text(path, e))
            return
        messagebox.showinfo(
            "已导出",
            f"已追加到三张表，原来的记录还在：\n"
            f"回测摘要 新增 {summary_added} 行，合计 {summary_total} 行\n"
            f"回测成交 新增 {trade_added} 行，合计 {trade_total} 行\n"
            f"回测信号 已追加本次没开成的信号和原因\n"
            f"第一列「回测时间」相同、而且内容也相同的一行不会重复写。\n"
            f"多因子还会在 data 里另存一份「多因子衰减监控-策略-时间.xlsx」，不覆盖以前的。\n\n"
            f"文件：\n{out}"
            + self._export_saved_note(path, out),
        )
        self._append_log(
            f"历史回测已追加到 {out} | 摘要新增{summary_added}合计{summary_total} | 成交新增{trade_added}合计{trade_total}"
        )


    def _on_plot_factor_decay(self) -> None:
        """按资料第二节的思路画衰减图：只读已有成交，不改开平仓。"""
        from tkinter import messagebox

        from src.export.factor_decay_plot import factor_summary, write_factor_decay_plot

        try:
            path, df = write_factor_decay_plot(self.root_dir)
        except ImportError as e:
            messagebox.showwarning("还不能画图", str(e))
            return
        except ValueError as e:
            messagebox.showwarning("还不能画图", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("画出因子衰减图失败")
            messagebox.showerror("画图失败", str(e))
            return
        lines = [f"已保存 {path}", f"一共 {len(df)} 笔带四因子分数的成交。"]
        summary = factor_summary(df)
        for row in summary.to_dict(orient="records"):
            lines.append(
                f"{row['因子']}：总盈亏 {row['总盈亏']:.2f}，有分摊 {row['交易次数']} 笔，"
                f"赚钱 {row['盈利次数']} 笔"
            )
        messagebox.showinfo("因子衰减图", "\n".join(lines))
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("打不开衰减图 {}: {}", path, exc)
        self._append_log(f"已画出因子衰减图 {path} | {len(df)} 笔")


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
        self._append_log(f"本地K线库存 {inventory_message(book)} | {path}")
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
        current = (self.bt_symbol_var.get() or "").strip()
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
            period_label = period_var.get().strip()
            period = None if period_label in {"", "不限"} else canonical_period(period_label)
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
            self._append_log(f"删除本地K线 {msg.replace(chr(10), ' ')}")
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
            text="对照同花顺信息框时，只对开盘、最高、最低、收盘、成交量。\n"
            "今结、涨跌、持仓本地没有。时间是这根K线的结束时刻。不下单。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=480,
        ).pack(anchor="w", padx=16, pady=(16, 10))

        default_src = (self.bt_source_var.get() or "新浪").strip()
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
        current = (self.bt_symbol_var.get() or "").strip()
        if current and current != "（还没有）":
            symbol_entry.insert(0, current)

        period_values = [x for x in PERIOD_MENU if x != "自定义"]
        try:
            default_period = period_label(
                self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
            )
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
        date_var = ctk.StringVar(value=date.today().isoformat())
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
            self._append_log(
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
        入口只在历史回测页，用本页选的合约和周期。
        """
        from tkinter import messagebox

        from src.data.quality import inspect_quote_book, write_quality_report

        symbol = (self.bt_symbol_var.get() or "").strip()
        period = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        if not symbol:
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
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("打不开质量报告 {}: {}", path, exc)
        self._append_log(f"行情质量 {symbol} {report.status} | {path}")

