"""配置页中相互独立的开止损、风控、软件与存档区块。账号密码在 credentials_tab。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.ui import theme as T
from src.ui.config_tab_controls import choice_row
from src.ui.config_tab_controls import param_entry

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def build_entry_stop_sections(app: AutoTradingApp, scroll: ctk.CTkFrame) -> None:
    """所有策略共用的开仓与止损设置。"""
    # —— 开仓设置（所有策略共用）：信号周期定开仓用哪根K线；触发方式定收不收线就进 ——
    app._section(scroll, "开仓设置（所有策略共用）")
    box_entry = app._card(scroll)
    from src.strategy.types import ENTRY_MODE_LABELS

    app._timeframe_row(box_entry, "信号周期", app.tf_var, app.tf_custom_var, width=160)
    choice_row(
        app,
        box_entry,
        "开仓触发方式",
        app.entry_mode_var,
        list(ENTRY_MODE_LABELS.values()),
        width=220,
    )
    ctk.CTkLabel(
        box_entry,
        text="回测里逐根 K 线独立跑，天然只有已收盘口径，选哪档都等价「收盘价确认」。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=460,
        justify="left",
    ).pack(anchor="w", pady=(4, 0))

    # —— 止损设置：止损周期 / 触发方式 / 通用止损；通用止损参数放右边，和策略引擎一致 ——
    app._section(scroll, "止损设置（所有策略共用）")
    box_stop = app._card(scroll)

    stop_split = ctk.CTkFrame(box_stop, fg_color="transparent")
    stop_split.pack(fill="x", pady=(0, 4))
    stop_split.grid_columnconfigure(0, weight=0, minsize=app._strategy_left_min_width)
    stop_split.grid_columnconfigure(1, weight=1, minsize=app._strategy_param_min_width)
    stop_left = ctk.CTkFrame(stop_split, fg_color="transparent")
    stop_left.grid(row=0, column=0, sticky="nw", padx=(0, 10))

    stop_right = ctk.CTkFrame(
        stop_split,
        fg_color=T.BG_PANEL,
        corner_radius=10,
        border_width=1,
        border_color=T.ACCENT,
    )
    # sticky=new：横向铺满右列，竖直贴顶；右侧高度跟着当前止损方式的参数伸缩。
    stop_right.grid(row=0, column=1, sticky="new")
    app._generic_stop_detail_shell = stop_right

    app._generic_stop_detail_title = ctk.CTkLabel(
        stop_right,
        text="通用止损参数",
        font=T.FONT_BODY,
        text_color=T.ACCENT,
        anchor="w",
    )
    app._generic_stop_detail_title.pack(fill="x", padx=12, pady=(10, 0))
    app._generic_stop_detail_note = ctk.CTkLabel(
        stop_right,
        text="",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=320,
        justify="left",
        anchor="w",
    )
    app._generic_stop_detail_note.pack(fill="x", padx=12, pady=(2, 4))
    app._generic_stop_detail_host = ctk.CTkFrame(stop_right, fg_color="transparent")
    app._generic_stop_detail_host.pack(fill="x", padx=12, pady=(0, 12))
    app._generic_stop_detail_panels = {}
    app._generic_stop_detail_meta = {}

    def _stop_panel(kind: str, title: str, note: str) -> ctk.CTkFrame:
        """在右侧宿主里建一块通用止损参数面板，默认先藏着，选中对应方式再显示。"""
        panel = ctk.CTkFrame(app._generic_stop_detail_host, fg_color="transparent")
        app._generic_stop_detail_panels[kind] = panel
        app._generic_stop_detail_meta[kind] = (title, note)
        return panel

    off_box = _stop_panel(
        "off",
        "通用止损参数 · 关闭",
        "只用各策略自身的止损，不额外加公共止损。",
    )
    ctk.CTkLabel(
        off_box,
        text="当前关闭。上面选 SAR / 前高低点 / Chandelier 吊灯 / 盈利1ATR后保本，右边会出现对应参数。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=320,
        justify="left",
        anchor="w",
    ).pack(anchor="w", pady=(4, 0))

    sar_box = _stop_panel(
        "sar",
        "通用止损参数 · SAR 移动止损",
        "按止损周期的K线算 SAR。刚开仓时 SAR 若还在持仓反方向，先不启用，等它转向后再生效。",
    )
    param_entry(app, sar_box, "SAR初始加速", app.generic_stop_sar_start_var, hint="默认 0.02，常用 0.01~0.04")
    param_entry(app, sar_box, "SAR加速步长", app.generic_stop_sar_step_var, hint="默认 0.02；越大越灵敏")
    param_entry(app, sar_box, "SAR加速上限", app.generic_stop_sar_max_var, hint="默认 0.2，常用 0.1~0.3")

    prior_box = _stop_panel(
        "prior_extreme",
        "通用止损参数 · 前高低点",
        "多单守最近 N 根最低，空单守最近 N 根最高；窗口不含当前这根。",
    )
    param_entry(app, prior_box, "回看根数", app.generic_stop_lookback_var, hint="最近 N 根，2~200，默认 20")

    ch_box = _stop_panel(
        "chandelier",
        "通用止损参数 · Chandelier 吊灯跟踪",
        "多单止损=最近 N 根最高−k×ATR，空单=最近 N 根最低+k×ATR；"
        "随新高/新低移动，旧高低点滑出窗口时这条线会跟着回退。",
    )
    param_entry(app, ch_box, "回看根数", app.generic_stop_lookback_var, hint="最近 N 根，2~200，默认 20")
    param_entry(app, ch_box, "ATR根数", app.generic_stop_atr_period_var, hint="默认 14，常用 10~20")
    param_entry(app, ch_box, "ATR倍数", app.generic_stop_atr_mult_var, hint="默认 3.0，常用 2.5~4")

    be_box = _stop_panel(
        "breakeven",
        "通用止损参数 · 盈利1ATR后保本",
        "开仓后浮盈达到 k×ATR，就把止损抬到开仓价；"
        "没赚到 k×ATR 之前这条线不生效，仍由策略自身止损兜着。",
    )
    param_entry(
        app,
        be_box,
        "浮盈触发倍数",
        app.generic_stop_be_atr_var,
        hint="单位 ATR，默认 1.0；越小越早保本，也越容易被正常回踩扫掉",
    )

    from src.strategy.types import STOP_MODE_LABELS
    from src.strategy.generic_stop import GENERIC_STOP_LABELS

    app._timeframe_row(stop_left, "止损周期", app.trail_var, app.trail_custom_var, width=160)
    choice_row(
        app,
        stop_left,
        "止损触发方式",
        app.stop_mode_var,
        list(STOP_MODE_LABELS.values()),
        hint="触及=碰线就走；收盘=收线确认",
        width=220,
    )
    choice_row(
        app,
        stop_left,
        "通用止损",
        app.generic_stop_var,
        list(GENERIC_STOP_LABELS.values()),
        hint="独立于策略止损，谁先触发就平",
        width=220,
    )
    ctk.CTkLabel(
        stop_left,
        text=(
            "止损周期同时供策略自身止损和通用止损使用；双均线、SuperTrend、均线波段、自定义规则也跟这一格。\n"
            "通用止损本身也在优化网格里：精简/标准只比类型，完整再搜回看根数、SAR 步长和 ATR 参数。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(4, 0))

    try:
        app.generic_stop_var.trace_add("write", app._refresh_generic_stop_param_panel)
    except Exception:  # noqa: BLE001
        pass
    app._refresh_generic_stop_param_panel()


def build_tail_sections(app: AutoTradingApp, scroll: ctk.CTkFrame) -> None:
    """风控、软件、坐标、配置存档与保存按钮。回测成交与策略评估在历史回测页。"""
    # —— 风控与资金管理：算几手、最多亏多少、开仓频率（排在同花顺软件之前，方便先调仓位）——
    app._section(scroll, "风控与资金管理")
    box4 = app._card(scroll)
    from src.risk.kelly import FRACTION_LABELS

    kelly_sw = ctk.CTkFrame(box4, fg_color="transparent")
    kelly_sw.pack(fill="x", pady=4)
    ctk.CTkLabel(
        kelly_sw, text="凯利公式", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
    ).pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        kelly_sw,
        text="打开后由凯利算意图手数",
        variable=app.kelly_enabled_var,
        progress_color=T.OK,
        font=T.FONT_SMALL,
        text_color=T.TEXT,
        command=app._refresh_kelly_status,
    ).pack(side="left")
    app._option_row(
        box4,
        "凯利占用",
        app.kelly_frac_var,
        list(FRACTION_LABELS.keys()),
        width=220,
    )
    app._entry_row(
        box4,
        "凯利每笔最多%",
        app.kelly_max_var,
        hint="默认2。全凯利再大，每笔也不超过本金的这么多",
    )
    app._entry_row(
        box4,
        "凯利胜率%",
        app.kelly_win_var,
        hint="从历史回测摘要抄，例如 46 表示 46%",
    )
    app._entry_row(
        box4,
        "凯利盈亏比",
        app.kelly_payoff_var,
        hint="抄历史回测摘要里单独一行「盈亏比」；不要填盈利因子",
    )
    ctk.CTkLabel(
        box4,
        textvariable=app.kelly_status_var,
        font=T.FONT_SMALL,
        text_color=T.TEXT,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(4, 8))
    try:
        app.kelly_frac_var.trace_add("write", app._refresh_kelly_status)
        app.kelly_max_var.trace_add("write", app._refresh_kelly_status)
        app.kelly_win_var.trace_add("write", app._refresh_kelly_status)
        app.kelly_payoff_var.trace_add("write", app._refresh_kelly_status)
    except Exception:  # noqa: BLE001
        pass
    app._entry_row(
        box4,
        "每笔风险%",
        app.risk_pct_var,
        hint="推荐0.5~1，止损远时自动少开；凯利打开时不用这项",
    )
    app._entry_row(
        box4,
        "仓位上限%",
        app.margin_usage_pct_var,
        hint="权益里最多拿出这么多交保证金，推荐70~80；0=按全部权益",
    )
    app._entry_row(box4, "日最大亏损(元)", app.max_loss_var)
    switch_row = ctk.CTkFrame(box4, fg_color="transparent")
    switch_row.pack(fill="x", pady=4)
    ctk.CTkLabel(
        switch_row, text="动态减仓", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
    ).pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        switch_row,
        text="账户回撤 / 波动放大时少开",
        variable=app.dyn_enabled_var,
        progress_color=T.OK,
        font=T.FONT_SMALL,
        text_color=T.TEXT,
    ).pack(side="left")
    app._entry_row(
        box4,
        "回撤减半(%)",
        app.dyn_dd_half_var,
        hint="默认5。从账户最高点往下掉到这里，下一笔手数减半",
    )
    app._entry_row(
        box4,
        "回撤停手(%)",
        app.dyn_dd_pause_var,
        hint="默认10。再往下掉就暂停开新仓；赚回来低于减半线后恢复",
    )
    app._entry_row(
        box4,
        "波动几倍减半",
        app.dyn_atr_half_var,
        hint="当前波动相对近20根中位，默认1.8倍",
    )
    app._entry_row(
        box4,
        "波动几倍暂停",
        app.dyn_atr_pause_var,
        hint="默认2.5倍。不会因为晃就把最后1手强平",
    )
    app._entry_row(
        box4,
        "下单最小间隔秒",
        app.min_interval_var,
        hint="平仓后再开新仓也要等够（含手机平仓记进账本）。止损和收盘清仓不耽误",
    )
    app._entry_row(box4, "每日最多下单", app.max_orders_var)

    # 下一根开盘 / 镜像闸门 / 滑点、以及策略评估门槛，都只在历史回测页。
    # 点「保存交易配置」仍会把回测页上的这些值写进 config.yaml（config_collect）。

    # —— 软件 ——
    app._section(scroll, "同花顺软件")
    box3 = app._card(scroll)
    app._entry_row(box3, "启动程序路径", app.app_path_var, width=360)
    app._entry_row(box3, "窗口标题关键字", app.app_title_var)
    app._entry_row(box3, "启动等待秒", app.startup_wait_var)

    # 同花顺实盘点击准备
    app._section(scroll, "同花顺自动下单准备")
    box5 = app._card(scroll)
    ctk.CTkLabel(
        box5,
        text=(
            "要用鼠标自动控制同花顺下单，请按顺序：\n"
            "1) 同花顺已打开并登录，下单面板露出来，窗口不要再挪\n"
            "2) 点下方「标定下单坐标」，录完按钮位置后保存。换显示器需重新标定。\n"
            "3) 执行通道改为 gui，先保持模拟 dry_run=开 做空点测试\n"
            "4) 确认点的位置对了，再关掉 dry_run（关闭模拟）才会真下单\n"
            "真下单时请不要自己动鼠标。同花顺里的仓去同花顺平，程序账用「手工单录入」"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        justify="left",
        wraplength=560,
    ).pack(anchor="w", pady=(0, 10))
    ctk.CTkButton(
        box5,
        text="标定下单坐标（新开窗口）",
        height=40,
        fg_color=T.BG,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.ACCENT,
        text_color=T.ACCENT,
        command=app._on_pick_coords,
    ).pack(anchor="w", pady=(0, 10))
    ctk.CTkLabel(
        box5,
        text="常驻正在跑时不能切换，停掉再选，然后重新启动。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        justify="left",
        wraplength=560,
    ).pack(anchor="w", pady=(0, 8))
    app.coord_preset_var = ctk.StringVar(value="（还没有保存过坐标）")
    app._coord_preset_ids: dict[str, str] = {}
    app.coord_preset_menu = ctk.CTkOptionMenu(
        box5,
        variable=app.coord_preset_var,
        values=["（还没有保存过坐标）"],
        width=420,
        fg_color=T.BG_ELEVATED,
        button_color=T.ACCENT,
        button_hover_color=T.ACCENT_HOVER,
        dropdown_fg_color=T.BG_PANEL,
        text_color=T.TEXT,
        dropdown_text_color=T.TEXT,
        command=app._sync_coord_name_box,
    )
    app.coord_preset_menu.pack(anchor="w", pady=(0, 8))
    name_row = ctk.CTkFrame(box5, fg_color="transparent")
    name_row.pack(anchor="w", pady=(0, 8))
    app.coord_name_var = ctk.StringVar(value="")
    app.coord_name_entry = ctk.CTkEntry(
        name_row,
        textvariable=app.coord_name_var,
        width=280,
        fg_color=T.BG_ELEVATED,
        border_color=T.BORDER,
        text_color=T.TEXT,
        placeholder_text="给这套坐标起个名字",
    )
    app.coord_name_entry.pack(side="left", padx=(0, 8))
    app.coord_rename_btn = ctk.CTkButton(
        name_row,
        text="改名",
        width=72,
        height=32,
        fg_color=T.BG,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.BORDER,
        text_color=T.TEXT,
        command=app._on_rename_coord_preset,
    )
    app.coord_rename_btn.pack(side="left")
    coord_btns = ctk.CTkFrame(box5, fg_color="transparent")
    coord_btns.pack(anchor="w")
    ctk.CTkButton(
        coord_btns,
        text="保存当前坐标",
        height=36,
        fg_color=T.BG,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.ACCENT,
        text_color=T.ACCENT,
        command=app._on_save_coord_preset,
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        coord_btns,
        text="用选中的坐标",
        height=36,
        fg_color=T.BG,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.ACCENT,
        text_color=T.ACCENT,
        command=app._on_apply_coord_preset,
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        coord_btns,
        text="删除",
        height=36,
        width=72,
        fg_color=T.BG,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.BORDER,
        text_color=T.TEXT,
        command=app._on_delete_coord_preset,
    ).pack(side="left")

    # 整页存档：数字和开关一起留下，下次一键应用
    app._section(scroll, "配置存档")
    box_preset = app._card(scroll)
    app.cfg_preset_var = ctk.StringVar(value="（还没有保存过配置）")
    app._cfg_preset_ids: dict[str, str] = {}
    app.cfg_preset_menu = ctk.CTkOptionMenu(
        box_preset,
        variable=app.cfg_preset_var,
        values=["（还没有保存过配置）"],
        width=420,
        fg_color=T.BG_ELEVATED,
        button_color=T.ACCENT,
        button_hover_color=T.ACCENT_HOVER,
        dropdown_fg_color=T.BG_PANEL,
        text_color=T.TEXT,
        dropdown_text_color=T.TEXT,
    )
    app.cfg_preset_menu.pack(anchor="w", pady=(0, 8))
    preset_btns = ctk.CTkFrame(box_preset, fg_color="transparent")
    preset_btns.pack(anchor="w")
    ctk.CTkButton(
        preset_btns,
        text="一键应用选中的配置",
        height=40,
        fg_color=T.BG,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.ACCENT,
        text_color=T.ACCENT,
        command=app._on_apply_config_preset,
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        preset_btns,
        text="删除选中的存档",
        height=40,
        fg_color=T.BG,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.BORDER,
        text_color=T.TEXT,
        command=app._on_delete_config_preset,
    ).pack(side="left")

    # 保存按钮
    actions = ctk.CTkFrame(scroll, fg_color="transparent")
    actions.pack(fill="x", pady=(16, 24))
    ctk.CTkButton(
        actions, text="保存交易配置", height=44, fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER,
        text_color=T.ACCENT_ON, font=T.FONT_BODY, command=app._on_save,
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        actions, text="重新加载", height=44, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
        border_width=1, border_color=T.BORDER, text_color=T.TEXT,             command=app._on_reload,
    ).pack(side="left")
    app._refresh_config_presets()
    app._refresh_coord_presets()
