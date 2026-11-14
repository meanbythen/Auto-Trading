"""配置页共用控件：只负责创建控件，页面状态仍挂在 app 上。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.ui import theme as T

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def note_block(parent: ctk.CTkFrame, text: str, *, pady: tuple[int, int] = (4, 4)) -> None:
    """右侧策略框里的长说明：固定 420 宽换行，不随窗口拉宽，避免画出蓝框。"""
    ctk.CTkLabel(
        parent,
        text=text,
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
        anchor="w",
    ).pack(anchor="w", padx=4, pady=pady)


def detail_panel(app: AutoTradingApp, engine_key: str, title: str, note: str) -> ctk.CTkFrame:
    """在右侧宿主里建一块面板，默认先藏着，选中对应引擎再显示。"""
    panel = ctk.CTkFrame(app._strategy_detail_host, fg_color="transparent")
    app._strategy_detail_panels[engine_key] = panel
    app._strategy_detail_meta[engine_key] = (title, note)
    return panel


def param_entry(
    app: AutoTradingApp,
    parent: ctk.CTkFrame,
    label: str,
    var: ctk.StringVar,
    hint: str = "",
    *,
    hint_wrap: int | None = None,
) -> None:
    """策略参数行：输入框偏窄、说明自动换行，整行能塞进右侧固定宽度。"""
    app._entry_row(
        parent,
        label,
        var,
        hint=hint,
        width=app._strategy_param_entry_w,
        hint_wrap=int(hint_wrap) if hint_wrap else app._strategy_param_hint_wrap,
    )


def ema_num_row(
    app: AutoTradingApp,
    parent: ctk.CTkFrame,
    label: str,
    vars_: list[ctk.StringVar],
    hint: str = "",
) -> None:
    """均线波段：每个数字框前面写 EMA，框里填几就算几根指数均线。不加括号。"""
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=5, padx=4)
    ctk.CTkLabel(
        row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
    ).pack(side="left", padx=(0, 8))
    box_w = app._strategy_param_entry_w if len(vars_) == 1 else 56
    for i, var in enumerate(vars_):
        if i:
            # 多根均线并排时用斜杠隔开，方便看出快慢顺序。
            ctk.CTkLabel(
                row, text="/", font=T.FONT_BODY, text_color=T.TEXT_DIM
            ).pack(side="left", padx=4)
        ctk.CTkLabel(
            row, text="EMA", font=T.FONT_BODY, text_color=T.TEXT
        ).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(
            row,
            textvariable=var,
            width=box_w,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left")
    if hint:
        ctk.CTkLabel(
            row,
            text=hint,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=app._strategy_param_hint_wrap,
            justify="left",
            anchor="w",
        ).pack(side="left", padx=(10, 0))


def switch_row(
    app: AutoTradingApp,
    parent: ctk.CTkFrame,
    label: str,
    var: ctk.BooleanVar,
    hint: str = "",
    *,
    hint_wrap: int | None = None,
) -> None:
    """策略开关行：和 param_entry 同一版式，右侧换成开关。"""
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=5, padx=4)
    ctk.CTkLabel(
        row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
    ).pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        row, text="", variable=var, progress_color=T.OK, fg_color=T.BORDER
    ).pack(side="left")
    if hint:
        ctk.CTkLabel(
            row,
            text=hint,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=int(hint_wrap) if hint_wrap else app._strategy_param_hint_wrap,
            justify="left",
            anchor="w",
        ).pack(side="left", padx=(10, 0))


# 多因子两套对照：说明比默认 200 宽一截，同一右边界；不铺满整框，避免再画出界面。
_CTA_HINT = 320


def cta_param(
    app: AutoTradingApp,
    parent: ctk.CTkFrame,
    label: str,
    var: ctk.StringVar,
    hint: str = "",
) -> None:
    """多因子参数行：说明宽度两套页面相同。"""
    param_entry(app, parent, label, var, hint=hint, hint_wrap=_CTA_HINT)


def cta_switch(
    app: AutoTradingApp,
    parent: ctk.CTkFrame,
    label: str,
    var: ctk.BooleanVar,
    hint: str = "",
) -> None:
    """多因子开关行：说明宽度和 cta_param 对齐。"""
    switch_row(app, parent, label, var, hint=hint, hint_wrap=_CTA_HINT)


def choice_row(
    app: AutoTradingApp,
    parent: ctk.CTkFrame,
    label: str,
    var: ctk.StringVar,
    values: list[str],
    hint: str = "",
    *,
    hint_wrap: int | None = None,
    width: int | None = None,
) -> None:
    """策略下拉行：和 param_entry 同一版式，右侧换成几个固定选项。"""
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=5, padx=4)
    ctk.CTkLabel(
        row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
    ).pack(side="left", padx=(0, 8))
    ctk.CTkOptionMenu(
        row,
        variable=var,
        values=values,
        width=width or app._strategy_param_entry_w,
        fg_color=T.BG_ELEVATED,
        button_color=T.ACCENT,
        button_hover_color=T.ACCENT_HOVER,
        text_color=T.TEXT,
    ).pack(side="left")
    if hint:
        ctk.CTkLabel(
            row,
            text=hint,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=hint_wrap or app._strategy_param_hint_wrap,
            justify="left",
            anchor="w",
        ).pack(side="left", padx=(10, 0))
