"""策略编辑器页（独立顶层页签）

把规则编辑器从「策略参数」面板里搬出来，独占一个页签：

    左：规则文件列表（程序内置 / 我的规则）
    中：文件名、保存 / 仅校验 / 重命名 / 删除、YAML 编辑区、状态行
    右：指标库与计算公式可视化配置、语法速查

配置页只留当前策略对应的规则摘要和「打开策略编辑器」入口。保存规则不再顺手
切换运行策略，避免编辑动作产生运行副作用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.strategy.rule_dsl import is_builtin_rule, list_rule_files
from src.ui import theme as T
from src.ui.config_tab_controls import note_block
from src.ui.rule_indicator_builder import build_indicator_builder

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp

_DSL_NOTE = (
    "条件只按已收盘的 K 线判断，成交价仍由回测/实盘引擎决定，不会出现未来函数。\n"
    "左侧带 [内置] 的模板只读：改文件名后点「保存」即另存为自己的规则。\n"
    "支持：开仓、离场、ATR/百分比/摆动点初始止损；可加 trail 移动止损、breakeven 保本、"
    "max_hold_bars 限持仓根数。暂不支持加减仓和反手。\n"
    "指标可用 expr 公式（+ - * /、括号、max/min/abs/sqrt/log/sign/pow/clip，"
    "以及 avg/std/hhv/llv/sum(序列, 根数) 滚动计算）组合行情字段和已有指标。\n"
    "还可以写 filters_htf（用「止损周期」的 K 线做高周期过滤）和 time 条件（限定交易时段）。\n"
    "保存规则不会自动切换运行策略；要让它在运行/回测里生效，去「配置」页的策略引擎下拉里选。\n"
    "规则写错时程序会拒绝开仓并写一条告警，不会悄悄换成别的策略。\n"
    "现成模板：dualthrust / r_breaker / turtle_adx / adx_atr_break / dual_ma_equiv。"
)


class RuleEditorPage:
    """三栏布局的规则编辑器。"""

    def __init__(self, app: AutoTradingApp, parent: ctk.CTkFrame) -> None:
        self.app = app
        self.parent = parent
        self.list_host: ctk.CTkScrollableFrame | None = None
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        self.parent.grid_columnconfigure(0, weight=0, minsize=250)
        self.parent.grid_columnconfigure(1, weight=1)
        self.parent.grid_columnconfigure(2, weight=0, minsize=440)
        self.parent.grid_rowconfigure(0, weight=1)

        self._build_file_list()
        self._build_editor()
        self._build_side()

    def _build_file_list(self) -> None:
        left = ctk.CTkFrame(
            self.parent,
            fg_color=T.BG_PANEL,
            corner_radius=10,
            border_width=1,
            border_color=T.BORDER,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=2)
        ctk.CTkLabel(
            left, text="规则文件", font=T.FONT_BODY, text_color=T.TEXT, anchor="w"
        ).pack(fill="x", padx=10, pady=(10, 0))
        ctk.CTkLabel(
            left,
            text="config/rules/*.yaml",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        ).pack(fill="x", padx=10)
        self.list_host = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.list_host.pack(fill="both", expand=True, padx=6, pady=8)
        ctk.CTkButton(
            left,
            text="新建规则",
            height=30,
            command=self.app._on_rule_new,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
        ).pack(fill="x", padx=10, pady=(0, 10))

    def _build_editor(self) -> None:
        mid = ctk.CTkFrame(self.parent, fg_color="transparent")
        mid.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=2)

        ctk.CTkLabel(
            mid,
            textvariable=self.app.rule_title_var,
            font=T.FONT_BODY,
            text_color=T.TEXT,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        file_row = ctk.CTkFrame(mid, fg_color="transparent")
        file_row.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(
            file_row,
            text="文件名",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=52,
            anchor="w",
        ).pack(side="left")
        ctk.CTkEntry(
            file_row,
            textvariable=self.app.rule_name_var,
            width=160,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            file_row,
            text="小写字母/数字/下划线；填新名字再保存 = 另存为",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left", padx=(8, 0))

        btn_row = ctk.CTkFrame(mid, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(
            btn_row,
            text="保存",
            width=84,
            command=self.app._on_rule_save,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row,
            text="仅校验",
            width=76,
            command=self.app._on_rule_check,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row,
            text="回测当前规则",
            width=104,
            command=self.app._on_rule_backtest,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row,
            text="重命名",
            width=76,
            command=self.app._on_rule_rename,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row,
            text="删除",
            width=64,
            command=self.app._on_rule_delete,
            fg_color=T.BG_ELEVATED,
            hover_color="#8B3A3A",
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            btn_row,
            text="Ctrl+S 保存",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left")

        self.app.rule_editor = ctk.CTkTextbox(
            mid,
            font=T.FONT_MONO,
            fg_color=T.BG_ELEVATED,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            wrap="none",
        )
        self.app.rule_editor.pack(fill="both", expand=True, pady=(0, 4))
        self.app.rule_editor.bind("<<Modified>>", lambda _e: self.app._on_rule_modified())
        self.app.rule_editor.bind("<Control-s>", self._on_ctrl_s)

        ctk.CTkLabel(
            mid,
            textvariable=self.app.rule_status_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=560,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(0, 2))

        ctk.CTkLabel(
            mid,
            text="绩效报告：品种、周期、日期、滑点和开仓限制沿用「历史回测」页；"
            "报告同时写入 data/backtest_last.json，可到历史回测页导出 Excel。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=560,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(6, 2))
        self.app.rule_bt_result_box = ctk.CTkTextbox(
            mid,
            height=190,
            font=T.FONT_MONO,
            fg_color=T.BG_ELEVATED,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            wrap="word",
        )
        self.app.rule_bt_result_box.pack(fill="x", pady=(0, 2))
        self.app.rule_bt_result_box.insert(
            "end",
            "点「回测当前规则」后，这里显示胜率、盈亏比、收益期望、最大回撤、"
            "评估结论和成交明细。\n",
        )

    def _build_side(self) -> None:
        right = ctk.CTkFrame(self.parent, fg_color="transparent")
        right.grid(row=0, column=2, sticky="nsew", pady=2)

        builder = build_indicator_builder(self.app, right)
        # 独立页签空间足够，指标库默认展开。
        builder.toggle()

        note_box = ctk.CTkScrollableFrame(right, fg_color="transparent")
        note_box.pack(fill="both", expand=True, pady=(6, 0))
        note_block(note_box, _DSL_NOTE, pady=(0, 4))

    def _on_ctrl_s(self, _event: object) -> str:
        self.app._on_rule_save()
        return "break"

    # ------------------------------------------------------------ 列表状态
    def refresh_list(self, highlight: str = "") -> None:
        """重建左侧规则列表；内置与自己的规则分组显示。"""
        if self.list_host is None:
            return
        for child in self.list_host.winfo_children():
            child.destroy()
        infos = list_rule_files()
        groups = (
            ("程序内置", [info for info in infos if info["builtin"]]),
            ("我的规则", [info for info in infos if not info["builtin"]]),
        )
        for title, items in groups:
            ctk.CTkLabel(
                self.list_host,
                text=f"{title}（{len(items)}）",
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                anchor="w",
            ).pack(fill="x", padx=6, pady=(8, 2))
            if not items:
                ctk.CTkLabel(
                    self.list_host,
                    text="（空）",
                    font=T.FONT_SMALL,
                    text_color=T.TEXT_DIM,
                    anchor="w",
                ).pack(fill="x", padx=14)
                continue
            for info in items:
                stem = str(info["stem"])
                label = f"{stem}.yaml  {info['name']}"
                if info["builtin"]:
                    label += "  [内置]"
                ctk.CTkButton(
                    self.list_host,
                    text=label,
                    height=26,
                    anchor="w",
                    font=T.FONT_SMALL,
                    fg_color=T.ACCENT if stem == highlight else "transparent",
                    hover_color=T.BORDER,
                    text_color=T.TEXT,
                    command=lambda s=stem: self.app._on_rule_open(s),
                ).pack(fill="x", padx=4, pady=1)

    def set_dirty(self, dirty: bool, stem: str) -> None:
        """标题上标出当前文件、是否内置、有没有未保存修改。"""
        name = f"{stem}.yaml" if stem else "未命名"
        tag = "  [内置]" if stem and is_builtin_rule(stem) else ""
        mark = "  *" if dirty else ""
        self.app.rule_title_var.set(f"当前：{name}{tag}{mark}")


def build_rule_editor_tab(app: AutoTradingApp, parent: ctk.CTkFrame) -> RuleEditorPage:
    """挂上策略编辑器页签，并立刻刷新文件列表与摘要。"""
    page = RuleEditorPage(app, parent)
    app.rule_page = page
    app._refresh_rule_editor()
    try:
        app.engine_var.trace_add("write", lambda *_: app._update_rule_summary())
    except Exception:  # noqa: BLE001
        pass
    app._update_rule_summary()
    return page
