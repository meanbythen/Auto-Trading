"""Historical-backtest controls, execution, sensitivity, local data tools, and exports."""

from __future__ import annotations

from src.ui import theme as T
from src.ui.app_common import ENGINE_CHOICES
from src.ui.backtest_actions import BacktestActionsMixin
from src.ui.backtest_config import BacktestConfigMixin
import customtkinter as ctk


class BacktestTabMixin(BacktestConfigMixin, BacktestActionsMixin):
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
            text="1 分钟复核",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_minute_review,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="用 1 分钟只检查上一回测已经开出的仓，止损会不会更早碰到。没有 1 分钟就标明「不是精确复盘」，不会在 1 分钟上另找开仓。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
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
            text="开仓限制、保证金、收盘清仓开跑前跟配置页对齐。账户权益可在本页改成你准备拿来交易的资金，只影响回测和策略优化，不会改实盘余额。点「按配置页重新填入」会抄回账户当前权益。关掉「启用限制」后固定 1 手。",
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
            hint="可改成准备投入的资金；点「按配置页重新填入」抄回当前余额",
            width=160,
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
