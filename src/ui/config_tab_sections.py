"""配置页中相互独立的账号、风控、软件与存档区块。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.ui import theme as T
from src.ui.config_tab_controls import choice_row
from src.ui.config_tab_controls import param_entry

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def build_account_section(app: AutoTradingApp, scroll: ctk.CTkFrame) -> None:
    """账号与登录。"""
    app._section(scroll, "账号与登录")
    box = app._card(scroll)
    app._entry_row(box, "期货公司", app.broker_var)
    app._entry_row(box, "通道名称", app.channel_var, hint="如：华泰期货_CTP主席")
    app._entry_row(
        box,
        "资金账号",
        app.user_var,
        hint="华泰真实资金账号。同花顺登录和 CTP 真实通道都用这一号",
    )
    app._entry_row(box, "密码环境变量名", app.pwd_env_var, hint="默认 FUTURES_PASSWORD")

    pwd_row = ctk.CTkFrame(box, fg_color="transparent")
    pwd_row.pack(fill="x", pady=6)
    ctk.CTkLabel(pwd_row, text="交易密码", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=130, anchor="w").pack(side="left")
    app.pwd_entry = ctk.CTkEntry(
        pwd_row,
        textvariable=app.password_var,
        show="*",
        width=280,
        fg_color=T.BG_ELEVATED,
        border_color=T.BORDER,
        text_color=T.TEXT,
    )
    app.pwd_entry.pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        pwd_row, text="显示", variable=app._show_password, width=60,
        progress_color=T.ACCENT, command=app._toggle_password,
    ).pack(side="left")

    ctk.CTkLabel(
        box,
        text="密码保存在 config/secrets.local.yaml（不会提交到 git）。同花顺登录和 CTP 真实通道共用这一份；华泰仿真密码另写 ctp_password。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(4, 8))

    # 天勤行情要单独登录，和同花顺资金账号不是一回事。
    app._entry_row(
        box,
        "天勤行情账号",
        app.tq_user_var,
        hint="选「天勤」行情时用；TqSdk 手机号/账号",
    )
    tq_pwd_row = ctk.CTkFrame(box, fg_color="transparent")
    tq_pwd_row.pack(fill="x", pady=6)
    ctk.CTkLabel(
        tq_pwd_row,
        text="天勤行情密码",
        font=T.FONT_BODY,
        text_color=T.TEXT_DIM,
        width=130,
        anchor="w",
    ).pack(side="left")
    app.tq_pwd_entry = ctk.CTkEntry(
        tq_pwd_row,
        textvariable=app.tq_password_var,
        show="*",
        width=280,
        fg_color=T.BG_ELEVATED,
        border_color=T.BORDER,
        text_color=T.TEXT,
    )
    app.tq_pwd_entry.pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        tq_pwd_row,
        text="显示",
        variable=app._show_tq_password,
        width=60,
        progress_color=T.ACCENT,
        command=app._toggle_tq_password,
    ).pack(side="left")
    ctk.CTkLabel(
        box,
        text="天勤密码同样只写进 config/secrets.local.yaml，留空表示不改已保存的密码。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(4, 8))

    app._option_row(box, "验证码模式", app.captcha_var, ["pause", "skip"], width=280)
    login_row = ctk.CTkFrame(box, fg_color="transparent")
    login_row.pack(fill="x", pady=8)
    ctk.CTkLabel(
        login_row,
        text="自动登录",
        font=T.FONT_BODY,
        text_color=T.TEXT_DIM,
        width=140,
        anchor="w",
    ).pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        login_row,
        text="",
        variable=app.auto_login_var,
        progress_color=T.OK,
        fg_color=T.BORDER,
    ).pack(side="left")
    ctk.CTkLabel(
        login_row,
        text="开=启动时打开交易登录框，填入密码并点登录。关=请先自己登录",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        anchor="w",
    ).pack(side="left", padx=(10, 0))


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
        hint="触发即开仓：未收线影线触到开仓位就进，激进；收盘确认（默认）：等K线收完收盘价穿越才进",
        width=220,
    )
    ctk.CTkLabel(
        box_entry,
        text=(
            "信号周期：所有策略共用，决定开仓信号用哪根 K 线（SuperTrend/MA 等策略内部会再用到这条）。\n"
            "开仓触发方式：影响模拟盘 / 实盘（gui / ctp 执行器）。\n"
            "回测里逐根 K 线独立跑，天然只有已收盘口径，选哪档都等价「收盘价确认」。"
        ),
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
            "止损周期同时供策略自身止损和通用止损使用；回测、策略优化都按这里的设置跑。\n"
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
    """风控、回测成交、评估、软件、坐标、配置存档与保存按钮。"""
    # —— 风控与资金管理：算几手、最多亏多少、开仓频率（排在同花顺软件之前，方便先调仓位）——
    app._section(scroll, "风控与资金管理")
    box4 = app._card(scroll)
    ctk.CTkLabel(
        box4,
        text=(
            "开仓手数取小：①每笔风险%（或上面固定手数，或凯利公式）②仓位上限%。"
            "打开凯利后，请自己跑历史回测，把胜率和盈亏比填进下面两格，程序按凯利自动算手数。"
            "仓位上限、日亏损、下单间隔、波动暂停仍照旧。"
            "三层风控不重复：日最大亏损管当天亏多少钱；"
            "账户从最高点回撤管整段本金；波动放大管市场突然变晃。"
            "都不要求再赚一笔才开。已经拿着的 1 手不会被强平。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))
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
    app._entry_row(box4, "下单最小间隔秒", app.min_interval_var)
    app._entry_row(box4, "每日最多下单", app.max_orders_var)

    # —— 回测成交模拟：下一根开盘成交、滑点跳数。和历史回测页是同一组，只影响回测 ——
    app._section(scroll, "回测成交模拟")
    box_bt = app._card(scroll)
    ctk.CTkLabel(
        box_bt,
        text=(
            "只影响历史回测和策略优化，不影响实盘下单。"
            "滑点=每笔成交价按买卖方向各让几跳（模拟挂单排队和盘口差价）；0=不加滑点，用来复现旧口径。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))
    fill_row = ctk.CTkFrame(box_bt, fg_color="transparent")
    fill_row.pack(fill="x", pady=4)
    ctk.CTkLabel(
        fill_row, text="下一根开盘成交", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
    ).pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        fill_row,
        text="",
        variable=app.bt_fill_next_open_var,
        progress_color=T.OK,
        fg_color=T.BORDER,
    ).pack(side="left")
    ctk.CTkLabel(
        box_bt,
        text="开=信号这根收盘确认后，按下一根开盘价成交，和实盘一致；关=按当根收盘价。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 6))
    app._entry_row(
        box_bt,
        "滑点(跳)",
        app.bt_slippage_var,
        hint="0=不加滑点；每跳价格优先用品种表的「每跳价格」",
    )

    # —— 策略评估：门槛与压力测试。回测/交叉只标红，优化不达标不写入 ——
    app._section(scroll, "策略评估")
    box_ev = app._card(scroll)
    ctk.CTkLabel(
        box_ev,
        text=(
            "门槛默认值来自 docs/策略评估标准/评估指标.md。"
            "回测和交叉测试只给通过/观察/淘汰结论并标红；策略优化碰到红线会把结果标成不能写入。"
            "滑点压力=在当前滑点基础上再加 1 跳重跑；参数扰动=把策略数值参数各动 10% 看会不会崩。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))
    for label, var, hint in (
        ("启用评估", app.ev_enabled_var, "关=回测和优化都不再打评估结论"),
        ("回测跑压力测试", app.ev_stress_var, "开=回测完成后再跑滑点+1跳和参数扰动"),
        ("优化不达标不写入", app.ev_gate_var, "开=评估红线时 accepted=False，不写入参数"),
    ):
        row = ctk.CTkFrame(box_ev, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            row, text="", variable=var, progress_color=T.OK, fg_color=T.BORDER
        ).pack(side="left")
        ctk.CTkLabel(
            box_ev,
            text=hint,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=(148, 0), pady=(0, 4))
    app._entry_row(box_ev, "最低盈利因子", app.ev_pf_var, hint="默认1.3；低于它是红线")
    app._entry_row(box_ev, "最低R期望", app.ev_r_var, hint="默认0.12；低于它只给观察")
    app._entry_row(box_ev, "最低卡尔玛", app.ev_calmar_var, hint="默认1.0；低于0.8是红线")
    app._entry_row(box_ev, "最大回撤(小数)", app.ev_dd_var, hint="默认0.15=15%；超过0.20是红线")
    app._entry_row(box_ev, "样本参考线(笔)", app.ev_trades_var, hint="默认150；低于它只提示，不判淘汰")
    app._entry_row(box_ev, "样本观察线(笔)", app.ev_observe_var, hint="默认100；低于它提示统计显著性不足")
    app._entry_row(box_ev, "最大连亏(笔)", app.ev_streak_var, hint="默认8；超过只给观察")

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
            "2) 点下方「标定下单坐标」，录完按钮位置后保存。家里显示器和笔记本各存一份，换屏幕时从列表里选\n"
            "3) 执行通道改为 gui，先保持模拟 dry_run=开 做空点测试\n"
            "4) 确认点的位置对了，再关掉 dry_run（关闭模拟）才会真下单\n"
            "真下单时请不要自己动鼠标；要停扫描请用运行页「停止」。同花顺里的仓去同花顺平，程序账用「手工单录入」"
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
        text=(
            "一块屏幕标定一次，点「保存当前坐标」留下。名字可以改成「家里显示器」或「笔记本」。"
            "换地方后，在列表里选中那一份，点「用选中的坐标」。删掉的只是备份，不会把正在用的点清掉。"
            "常驻正在跑时不能切换，停掉再选，然后重新启动。"
        ),
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
    ctk.CTkLabel(
        box_preset,
        text=(
            "点下面的「保存全部配置」时，会按「关注品种-策略名称-时间」再留一份。"
            "份里包含这个页面上的数字和开关，例如收盘清仓、收盘后关机；"
            "模拟开关在运行台，也会一起存。"
            "密码不写进存档。选中一份后点「一键应用」，页面会整页换回去并写入配置文件。"
            "不想要的存档可以删除，删掉的只是这一份备份，不会改当前配置。"
            "常驻正在跑时不能应用，停掉再启动才会用上新开关。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        justify="left",
        wraplength=560,
    ).pack(anchor="w", pady=(0, 8))
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
        actions, text="保存全部配置", height=44, fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER,
        text_color=T.ACCENT_ON, font=T.FONT_BODY, command=app._on_save,
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        actions, text="重新加载", height=44, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
        border_width=1, border_color=T.BORDER, text_color=T.TEXT,             command=app._on_reload,
    ).pack(side="left")
    app._refresh_config_presets()
    app._refresh_coord_presets()
