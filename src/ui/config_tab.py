"""
配置页（从 app.py 拆出）
功能：策略/执行/行情/账号/软件路径等全部配置控件。
原理：build_config_tab(app, tab) 直接读写 AutoTradingApp 上的变量与回调，页面状态仍挂在 app 上。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.trader.factory import list_executors
from src.ui import theme as T

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def build_config_tab(
    app: AutoTradingApp,
    tab: ctk.CTkFrame,
    engine_choices: list[str],
) -> None:
    """配置选项卡：账号、策略、软件、风控。"""
    scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=8, pady=8)

    # —— 账号 ——
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

    # —— 策略 ——
    # 真·左右两列：左侧连续排「选策略 + 共用下单/仓位」；右侧整块只给当前策略参数。
    # 两列都顶对齐，右侧变高不会把左侧中间顶出空白。其它策略的数仍保存在变量里。
    app._section(scroll, "策略与交易")
    box2 = app._card(scroll)
    from src.data.quote_check import SOURCE_LABELS

    # 左侧按内容定宽；右侧 grid 铺满剩余横向空间。不用 pack_propagate(False) 手改宽高，
    # 避免宽/高算成 0 或异常值时 Tk 画圆角框触发 CreateDIBSection「参数错误」。
    app._strategy_left_min_width = 560  # 关注品种那一行最宽
    app._strategy_param_min_width = 360
    app._strategy_param_hint_wrap = 200
    app._strategy_param_entry_w = 120

    split = ctk.CTkFrame(box2, fg_color="transparent")
    split.pack(fill="x", pady=(0, 4))
    # grid：左列不抢余量，右列 weight=1 铺满；两列都顶对齐，高度互不影响。
    split.grid_columnconfigure(0, weight=0, minsize=app._strategy_left_min_width)
    split.grid_columnconfigure(1, weight=1, minsize=app._strategy_param_min_width)
    split.grid_rowconfigure(0, weight=0)
    app._strategy_split = split

    left = ctk.CTkFrame(split, fg_color="transparent")
    left.grid(row=0, column=0, sticky="nw", padx=(0, 10))
    app._strategy_left_col = left

    right_shell = ctk.CTkFrame(
        split,
        fg_color=T.BG_PANEL,
        corner_radius=10,
        border_width=1,
        border_color=T.ACCENT,
    )
    # sticky=new：横向铺满右列，竖直只贴顶；高度随当前策略参数内容自然伸缩。
    right_shell.grid(row=0, column=1, sticky="new")
    app._strategy_detail_shell = right_shell

    app._option_row(left, "行情数据源", app.source_var, list(SOURCE_LABELS), width=160)
    ctk.CTkLabel(
        left,
        text="只拉选中的这一家。开仓前不再另问一家。同花顺图不在这里：读图会抢鼠标，只给历史回测。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))
    app.tq_hint_label = ctk.CTkLabel(
        left,
        text="",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
        anchor="w",
    )
    app.tq_hint_label.pack(anchor="w", pady=(0, 8))
    # 换来源、换周期、换策略时都重新看一眼天勤能不能用。
    for _var in (
        app.source_var,
        app.tf_var,
        app.tf_custom_var,
        app.trail_var,
        app.trail_custom_var,
        app.engine_var,
    ):
        try:
            _var.trace_add("write", lambda *_: app._refresh_tq_hint())
        except Exception:  # noqa: BLE001
            pass
    try:
        # 换成天勤才去查主力；换走新浪/东财立刻停掉天勤登录提示。
        app.source_var.trace_add("write", lambda *_: app._refresh_main_contract_hint())
    except Exception:  # noqa: BLE001
        pass
    app._refresh_tq_hint()
    app._option_row(left, "策略引擎", app.engine_var, list(engine_choices), width=220)
    ctk.CTkLabel(
        left,
        text=(
            "右边整块是当前策略参数；换策略后右边跟着换。左侧下面是各策略共用设置。"
            "「策略优化」会按深度从右边这块和左侧公共设置里挑参数。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(6, 0))

    app._strategy_detail_title = ctk.CTkLabel(
        right_shell,
        text="当前策略参数",
        font=T.FONT_BODY,
        text_color=T.ACCENT,
        anchor="w",
    )
    app._strategy_detail_title.pack(fill="x", padx=12, pady=(10, 0))
    app._strategy_detail_note = ctk.CTkLabel(
        right_shell,
        text="",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
        anchor="w",
    )
    app._strategy_detail_note.pack(fill="x", padx=12, pady=(2, 4))
    app._strategy_detail_host = ctk.CTkFrame(right_shell, fg_color="transparent")
    app._strategy_detail_host.pack(fill="both", expand=True, padx=8, pady=(0, 10))
    app._strategy_detail_panels: dict[str, ctk.CTkFrame] = {}
    app._strategy_detail_meta: dict[str, tuple[str, str]] = {}

    def _note_block(parent: ctk.CTkFrame, text: str, *, pady: tuple[int, int] = (4, 4)) -> None:
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

    def _detail_panel(engine_key: str, title: str, note: str) -> ctk.CTkFrame:
        """在右侧宿主里建一块面板，默认先藏着，选中对应引擎再显示。"""
        panel = ctk.CTkFrame(app._strategy_detail_host, fg_color="transparent")
        app._strategy_detail_panels[engine_key] = panel
        app._strategy_detail_meta[engine_key] = (title, note)
        return panel

    def _param_entry(
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

    def _ema_num_row(
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

    def _switch_row(
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.BooleanVar,
        hint: str = "",
        *,
        hint_wrap: int | None = None,
    ) -> None:
        """策略开关行：和 _param_entry 同一版式，右侧换成开关。"""
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

    def _cta_param(parent: ctk.CTkFrame, label: str, var: ctk.StringVar, hint: str = "") -> None:
        """多因子参数行：说明宽度两套页面相同。"""
        _param_entry(parent, label, var, hint=hint, hint_wrap=_CTA_HINT)

    def _cta_switch(parent: ctk.CTkFrame, label: str, var: ctk.BooleanVar, hint: str = "") -> None:
        """多因子开关行：说明宽度和 _cta_param 对齐。"""
        _switch_row(parent, label, var, hint=hint, hint_wrap=_CTA_HINT)

    def _choice_row(
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        values: list[str],
        hint: str = "",
        *,
        hint_wrap: int | None = None,
        width: int | None = None,
    ) -> None:
        """策略下拉行：和 _param_entry 同一版式，右侧换成几个固定选项。"""
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

    body_box = _detail_panel(
        "daily_body",
        "日内实体",
        "ETH 开盘箱体 + 收盘突破。改左边引擎才会用到这里。",
    )
    _param_entry(
        body_box,
        "第一ETH窗口（分钟）",
        app.eth_min_var,
        hint="从这段开盘起算，观察这么多分钟再画第一口箱子。和第二个窗口分开判断",
    )
    _param_entry(
        body_box,
        "第二ETH窗口（分钟）",
        app.body_eth2_var,
        hint="也从这段开盘起算，另画一口箱子。过了这个时间就丢掉第一窗口的判断，只认第二口。日盘止损后截止前也可按这口再开。夜盘不用。默认 30，填 0 关闭",
    )
    _param_entry(
        body_box,
        "日盘开仓截止",
        app.body_day_cutoff_var,
        hint="过了这个钟点就停止开日盘新仓，默认 10:30；10:15～10:30 是上午休盘，截止 10:15 和 10:30 等效",
    )
    _param_entry(
        body_box,
        "夜盘开仓截止",
        app.body_night_cutoff_var,
        hint="过了这个钟点就停止开夜盘新仓，默认 22:15",
    )
    _param_entry(
        body_box,
        "箱沿外再让几跳",
        app.body_stop_ticks_var,
        hint="多守箱底、空守箱顶，再往外让几跳；0=贴着箱沿",
    )
    _param_entry(
        body_box,
        "跳空超过几倍ATR不开",
        app.body_skip_gap_var,
        hint="开盘相对上一段收盘的缺口，超过开盘前ATR这么多倍就不开；0=关。出厂 2.5。尺子是信号周期、开盘前的ATR，不含今天",
    )
    _param_entry(
        body_box,
        "箱体超过几倍ATR不开",
        app.body_max_box_var,
        hint="开盘箱子最高-最低超过开盘前ATR这么多倍就不开；0=关。出厂 5。箱子越宽，箱沿止损离进场越远",
    )
    _param_entry(
        body_box,
        "急破超过几倍ATR不追",
        app.body_max_chase_var,
        hint="信号收盘离破位超过开盘前ATR这么多倍就不当场开，等回到破位附近再按原方向开；0=关。出厂 1",
    )
    _param_entry(
        body_box,
        "回抽看哪条 EMA",
        app.body_ema_period_var,
        hint="等回抽时看这条 EMA；反抽收盘有效打穿它，这次突破作废；0=关闭这道确认",
    )
    _param_entry(
        body_box,
        "有效打穿EMA几跳",
        app.body_ema_buffer_var,
        hint="收盘超过 EMA 这么多跳才算有效打穿；默认 2 跳",
    )
    _note_block(
        body_box,
        "止损固定在开盘箱体对沿。触发方式在左侧公共设置里选；没触发就拿到收盘。不做回撤止损。\n"
        "策略优化会按深度搜这一块：精简只比第一ETH窗口、信号周期、止损触发方式和通用止损类型，"
        "标准再加日盘/夜盘截止钟点和交易时段，完整才放开箱沿外扩、跳空、箱宽、急破、EMA 回抽过滤，"
        "以及通用止损的回看根数、SAR 步长和 ATR 参数。\n"
        "优化日盘截止时用 09:45 / 10:15 / 10:45 三档，避开 10:15～10:30 的上午休盘。",
    )
    ctk.CTkLabel(
        body_box,
        text="本策略行情过滤",
        font=T.FONT_BODY,
        text_color=T.ACCENT,
        anchor="w",
    ).pack(fill="x", padx=4, pady=(10, 0))
    _note_block(
        body_box,
        "箱子突破怕已经走出单边：ADX 高于上限就不开。盘面拧来拧去反而放行。\n"
        "极端波动仍可拦。已有持仓仍按箱沿止损。每一轮用当时 K 线重算，不是开盘判一次就整天停手。",
        pady=(0, 4),
    )
    _switch_row(
        body_box,
        "启用行情过滤",
        app.body_filter_enabled_var,
            hint="开=每一轮看 ADX 是否已经单边、波动是否太极端；关=这一层不看。默认开",
    )
    _param_entry(
        body_box,
        "强单边ADX上限",
        app.body_filter_adx_upper_var,
        hint="ADX 高于这个数当成已经单边，不开。默认 30。盘面拧来拧去会放行。填 0 关掉这一条",
    )
    _param_entry(
        body_box,
        "极端波动ATR高分位%",
        app.body_filter_atr_high_var,
        hint="ATR 落在近 60 根最高这一档（默认 80%）就不开。填 0 关闭。不拦波动压缩",
    )
    _param_entry(
        body_box,
        "过滤ADX周期",
        app.body_filter_adx_period_var,
        hint="默认 14",
    )
    _param_entry(
        body_box,
        "过滤ATR周期",
        app.body_filter_atr_period_var,
        hint="默认 14",
    )
    _param_entry(
        body_box,
        "过滤ATR回看根数",
        app.body_filter_lookback_var,
        hint="默认 60，用来算 ATR 处于历史几分位",
    )

    hold_box = _detail_panel(
        "float_hold",
        "日内浮盈飘单",
        "等开盘噪音走完再定当天方向。止损和保本都按开盘前波动留宽度，否则拿到收盘。",
    )
    _note_block(
        hold_box,
        "和日内实体分开。这套现在尽量简单：等观察窗口走完，"
        "EMA60 在 EMA120 上面就做多、下面就做空，错了用较宽的止损认赔。"
        "跳空、倾角、急破这些闸默认关掉，好让单边日能开到仓。"
        "还可以改成「观察结束看昨日密集区」、箱子或 MACD。"
        "手数仍用左边公共的凯利或每笔风险。",
        pady=(0, 4),
    )
    app._option_row(
        hold_box,
        "方向选择器",
        app.float_direction_var,
        getattr(app, "_float_direction_labels", ["开盘箱体突破"]),
        width=220,
    )
    app._option_row(
        hold_box,
        "MACD周期",
        app.float_macd_tf_var,
        getattr(app, "_float_macd_tf_labels", ["1分钟", "5分钟"]),
        width=220,
    )
    app._option_row(
        hold_box,
        "MACD参数组合",
        app.float_macd_combo_var,
        getattr(app, "_float_macd_combo_labels", ["12 / 26 / 9（常用）"]),
        width=220,
    )

    def _sync_macd_combo(*_args: object) -> None:
        """下拉常见组合时，同步改下面三格数字。"""
        from src.strategy.float_hold import parse_macd_combo

        fast, slow, signal = parse_macd_combo(app.float_macd_combo_var.get())
        app.float_macd_fast_var.set(str(fast))
        app.float_macd_slow_var.set(str(slow))
        app.float_macd_signal_var.set(str(signal))

    if not getattr(app, "_float_macd_combo_traced", False):
        app.float_macd_combo_var.trace_add("write", _sync_macd_combo)
        app._float_macd_combo_traced = True
    _param_entry(
        hold_box,
        "MACD快线",
        app.float_macd_fast_var,
        hint="出厂 12。选上面常见组合会改这里，也可手填",
    )
    _param_entry(
        hold_box,
        "MACD慢线",
        app.float_macd_slow_var,
        hint="出厂 26，须大于快线",
    )
    _param_entry(
        hold_box,
        "MACD平滑",
        app.float_macd_signal_var,
        hint="出厂 9，柱子用这一根平滑",
    )
    app._option_row(
        hold_box,
        "定向均线对",
        app.float_ema_pair_var,
        getattr(app, "_float_ema_pair_labels", ["EMA5 / EMA20"]),
        width=220,
    )
    _switch_row(
        hold_box,
        "大方向过滤",
        app.float_trend_align_var,
        hint="只给箱子/MACD 用。默认方向已经顺着 EMA60/120，不必再开。出厂关",
    )
    _param_entry(
        hold_box,
        "最小倾角(度)",
        app.float_trend_slope_var,
        hint="出厂 0=只看均线谁在上面。填大了会很少开仓",
    )
    _param_entry(
        hold_box,
        "斜率回看根数",
        app.float_trend_lookback_var,
        hint="看 EMA60、EMA120 自己往回几根，比均线现在和当时差多少。不是 K 线实体的斜。出厂 5",
    )
    _param_entry(
        hold_box,
        "观察几分钟再定方向",
        app.float_eth_var,
        hint="对「观察结束顺大方向」、昨日密集区和箱子有效。MACD循环不等。出厂 60",
    )
    _param_entry(
        hold_box,
        "密集区最宽点数",
        app.float_cluster_width_var,
        hint="上一交易日被反复摸到的窄价格带，最高减最低不超过这么多。出厂 10。只给「昨日密集区」用",
    )
    _param_entry(
        hold_box,
        "日盘开仓截止",
        app.float_day_cutoff_var,
        hint="过了这个钟点就停止开日盘新仓。出厂 14:45，好让观察结束后还能开",
    )
    _param_entry(
        hold_box,
        "夜盘开仓截止",
        app.float_night_cutoff_var,
        hint="过了这个钟点就停止开夜盘新仓。出厂 22:50",
    )
    _param_entry(
        hold_box,
        "止损几倍开盘前ATR",
        app.float_stop_atr_var,
        hint="从进场价往回留这么多倍开盘前波动。出厂 1.5。尺子不含今天早盘",
    )
    _param_entry(
        hold_box,
        "跳空超过几倍ATR不开",
        app.float_skip_gap_var,
        hint="缺口太大整段不做。出厂关（0）",
    )
    _param_entry(
        hold_box,
        "跳空超过几倍ATR只顺向",
        app.float_gap_align_var,
        hint="缺口够大时，高开只做多、低开只做空。出厂关（0）",
    )
    _param_entry(
        hold_box,
        "箱体超过几倍ATR不开",
        app.float_max_box_var,
        hint="箱子太宽不开。出厂关（0），默认方向不用箱子",
    )
    _param_entry(
        hold_box,
        "急破超过几倍ATR不追",
        app.float_max_chase_var,
        hint="收盘离破位太远就等回抽。出厂关（0），默认方向不用箱子",
    )
    _param_entry(
        hold_box,
        "浮盈几倍ATR后保本",
        app.float_be_atr_var,
        hint="收盘浮盈达到开盘前波动的这么多倍，止损提到开仓价。出厂 2，0=不做保本",
    )

    trend_box = _detail_panel(
        "trend_follow",
        "趋势跟随",
        "收盘突破前 N 根高低点跟上；默认可隔夜。",
    )
    _param_entry(
        trend_box,
        "趋势进场根数",
        app.trend_entry_bars_var,
        hint="收盘突破前多少根的高低点",
    )
    _param_entry(
        trend_box,
        "趋势止损倍数",
        app.trend_atr_mult_var,
        hint="保护止损用几倍波动",
    )

    def _pack_trend_filter(parent: ctk.CTkFrame, section: str, note: str) -> None:
        """趋势策略自己的 ADX/ATR 过滤，数字写在该策略小节。"""
        vars_ = app._trend_filter_vars[section]
        ctk.CTkLabel(
            parent,
            text="本策略行情过滤",
            font=T.FONT_BODY,
            text_color=T.ACCENT,
            anchor="w",
        ).pack(fill="x", padx=4, pady=(10, 0))
        _note_block(parent, note, pady=(0, 4))
        _switch_row(
            parent,
            "启用趋势过滤",
            vars_["enabled"],
            hint="开=ADX 太低或波动压缩就不开新仓；关=这一套不再看这层。已有持仓仍按止损走",
        )
        _param_entry(parent, "ADX周期", vars_["adx_period"], hint="默认 14")
        _param_entry(parent, "ATR周期", vars_["atr_period"], hint="默认 14")
        _param_entry(parent, "ATR回看根数", vars_["lookback"], hint="默认 60，算 ATR 处于历史几分位")
        _param_entry(parent, "ADX趋势门槛", vars_["adx_flat"], hint="默认 20，低于它算没趋势")
        _param_entry(parent, "ATR低分位(%)", vars_["atr_low_pct"], hint="默认 20，填百分数")

    _pack_trend_filter(
        trend_box,
        "trend_follow",
        "海龟怕假突破。没趋势或波动缩着时，突破容易是假的。默认关，需要再开。",
    )

    st_box = _detail_panel(
        "supertrend",
        "SuperTrend 趋势",
        "ATR 通道翻转开仓；默认可隔夜。",
    )
    _param_entry(st_box, "ST的ATR根数", app.st_atr_period_var, hint="算通道用几根K线")
    _param_entry(st_box, "ST通道倍数", app.st_multiplier_var, hint="通道半宽几倍波动，常用 3")
    _pack_trend_filter(
        st_box,
        "supertrend",
        "震荡里通道会来回翻。没趋势时过滤能少挨鞭子。默认关，需要再开。",
    )

    dual_box = _detail_panel(
        "dual_ma",
        "双均线趋势",
        "短 EMA 与长 EMA 交叉；长根数须大于短根数。",
    )
    _param_entry(dual_box, "双均线短根数", app.dual_short_var, hint="默认 20，指数均线")
    _param_entry(dual_box, "双均线长根数", app.dual_long_var, hint="默认 60，指数均线，须大于短根数")
    _switch_row(
        dual_box,
        "反向时反手",
        app.dual_reverse_var,
        hint="开=均线反向交叉就反手开新仓；关=只平仓，等下一次顺向交叉",
    )
    _pack_trend_filter(
        dual_box,
        "dual_ma",
        "震荡里交叉特别多。没趋势时过滤能少做来回。默认关，需要再开。",
    )

    macd_swing_box = _detail_panel(
        "ema_swing",
        "均线波段",
        "方向均线定多空，信号三均线排好就开。建议左边信号/止损周期都用 15 分钟。多空分界线、止损、倾角可分开填根数。",
    )
    _note_block(
        macd_swing_box,
        "方向选择器金叉后只做多、死叉后只做空，没再叉就不改。框里填根数，比如 21 和 122。"
        "开仓：三条信号均线按从快到慢排好，且收盘在多空分界线正确一侧。"
        "做多要收盘站在分界线上面，做空要在下面；贴着也不开。"
        "信号三根前面要比后面小，保存时会自动排好。"
        "不用 MACD 柱子。跳空错过交叉那根也可以开。倾角均线太平或斜反了不开新仓。"
        "「倾角不够就平仓」出厂关；打开后手里的仓也会平（不反手），用来对照收益。"
        "多空分界线、止损、倾角出厂都是 120，可以分开填。"
        "空仓时日盘/夜盘收盘前若干分钟不再开（出厂 30，可改；午休不算）。"
        "仓位、信号周期都用配置页公共项。这套适合更大级别：建议信号和止损都用 15 分钟，5 分钟容易被影线扫止损。"
        "主力连续换月时，旧仓还在会在新主力同方向移过去。"
        "任何合约都不进入自己的交割月：交割月前一个月的日盘收盘必须平仓，之后改下一手。",
        pady=(0, 4),
    )
    _ema_num_row(
        macd_swing_box,
        "方向选择器",
        [app.macd_swing_dir_fast_var, app.macd_swing_dir_slow_var],
        hint="出厂 60 / 120。前面要比后面小",
    )
    _ema_num_row(
        macd_swing_box,
        "信号发生器",
        [app.macd_swing_sig_a_var, app.macd_swing_sig_b_var, app.macd_swing_sig_c_var],
        hint="出厂 5 / 10 / 30。从快到慢",
    )
    _ema_num_row(
        macd_swing_box,
        "多空分界线",
        [app.macd_swing_side_ema_var],
        hint="做多要站上面、做空要在下面。出厂 120",
    )
    _ema_num_row(
        macd_swing_box,
        "止损均线",
        [app.macd_swing_stop_ema_var],
        hint="开仓后止损跟着它走。出厂 120",
    )
    _ema_num_row(
        macd_swing_box,
        "倾角均线",
        [app.macd_swing_slope_ema_var],
        hint="只看这根斜不斜。出厂 120",
    )
    _param_entry(
        macd_swing_box,
        "最小倾角(度)",
        app.macd_swing_slope_var,
        hint="看倾角均线自己斜不斜，不是 K 线的斜。出厂 3。填 0 关掉",
    )
    _param_entry(
        macd_swing_box,
        "斜率回看根数",
        app.macd_swing_slope_lookback_var,
        hint="用最近几根算倾角均线倾角。出厂 20",
    )
    _switch_row(
        macd_swing_box,
        "倾角不够就平仓",
        app.macd_swing_slope_exit_var,
        hint="关=只拦新开仓。开=倾角不够也平掉已有仓，不反手。用来对照收益。出厂关",
    )
    _param_entry(
        macd_swing_box,
        "尾盘不开仓(分钟)",
        app.macd_swing_close_guard_var,
        hint="空仓时收盘前这么多分钟不开。出厂 30。填 0 关掉。午休不算",
    )

    boll_box = _detail_panel(
        "bollinger",
        "布林带突破",
        "中轨是 EMA；只做顺势突破，不做轨道反转；跌回中轨离场。",
    )
    _param_entry(boll_box, "布林轨道周期", app.boll_window_var, hint="默认 20；中轨用 EMA")
    _param_entry(boll_box, "标准差倍数", app.boll_dev_var, hint="默认 2.0；越大通道越宽、信号越少")
    _param_entry(boll_box, "止损倍数", app.boll_atr_mult_var, hint="开仓价外几倍 ATR，默认 1.5")
    _pack_trend_filter(
        boll_box,
        "bollinger",
        "通道缩窄时假突破多。波动压缩时建议拦一下。默认关，需要再开。",
    )

    sar_box = _detail_panel(
        "sar_trend",
        "SAR波段趋势",
        "SAR 翻转 + EMA 方向过滤；SAR 线本身就是移动止损。",
    )
    _param_entry(sar_box, "方向过滤均线", app.sar_ma_filter_var, hint="默认 20 根 EMA；站上方只做多、下方只做空")
    _param_entry(sar_box, "加速步长", app.sar_af_step_var, hint="默认 0.02；越大翻转越快、也越容易被打")
    _pack_trend_filter(
        sar_box,
        "sar_trend",
        "震荡里 SAR 容易来回翻。没趋势时过滤能少挨扫。默认关，需要再开。",
    )

    def _pack_cta_shared(parent: ctk.CTkFrame) -> None:
        """多因子加权打分的因子参数，信号周期仍用左边公共项。"""
        _cta_param(parent, "总分阈值", app.multi_threshold_var, hint="默认 0.6；加权打分过这条才开")
        _cta_param(parent, "短均线根数", app.multi_ma_short_var, hint="默认 20 根 EMA；跟左边信号周期走，不是写死 5 分钟")
        _cta_param(parent, "长均线根数", app.multi_ma_long_var, hint="默认 60 根 EMA，须大于短均线")
        _cta_param(parent, "唐奇安通道根数", app.multi_channel_var, hint="默认 20；比前 20 根高低点")
        _cta_param(parent, "止损倍数", app.multi_atr_mult_var, hint="开仓价外几倍 ATR，默认 2；只往有利方向收")
        _cta_param(parent, "因子分歧上限", app.multi_disagree_var, hint="四个因子分数差太大（默认 0.7）就不开；受下面「过滤因子分歧」开关管")
        _cta_switch(
            parent,
            "启用 SAR 因子",
            app.multi_use_sar_var,
            hint="关=不算 SAR，权重摊给均线/唐奇安/布林",
        )

    def _pack_cta_filter(
        parent: ctk.CTkFrame,
        kind: str,
        *,
        show_score: bool,
        show_size: bool,
        note: str,
    ) -> None:
        """每套多因子自己的行情闸门。"""
        vars_ = app._cta_filter_vars[kind]
        ctk.CTkLabel(
            parent,
            text="本策略行情过滤",
            font=T.FONT_BODY,
            text_color=T.ACCENT,
            anchor="w",
        ).pack(fill="x", padx=4, pady=(10, 0))
        _note_block(parent, note, pady=(0, 4))
        _cta_switch(
            parent,
            "启用行情过滤",
            vars_["enabled"],
            hint="开=按下面几条拦新开仓；关=这一套自己不再看行情",
        )
        _cta_switch(parent, "过滤ADX", vars_["use_adx"], hint="开=没趋势就停手；弱趋势还要加严")
        _cta_switch(parent, "过滤ATR", vars_["use_atr"], hint="开=波动缩在最低分位就不开")
        _cta_switch(parent, "过滤因子分歧", vars_["use_disagree"], hint="开=四个因子打架就不开")
        _cta_param(parent, "ADX没趋势门槛", vars_["adx_flat"], hint="默认 20，低于它不开")
        _cta_param(parent, "ADX明确趋势", vars_["adx_trend"], hint="默认 25，夹在中间算弱趋势")
        _cta_param(parent, "ATR回看根数", vars_["atr_lookback"], hint="默认 60，按当前信号周期的根数算")
        _cta_param(parent, "ATR低分位(%)", vars_["atr_low_pct"], hint="默认 20，填百分数")
        if show_score:
            _cta_param(
                parent,
                "弱趋势高分门槛",
                vars_["weak_score"],
                hint="ADX 20~25 时总分至少这么高才开，默认 0.75",
            )
        if show_size:
            _cta_param(
                parent,
                "弱趋势仓位成数",
                vars_["weak_size"],
                hint="ADX 20~25 时按几成风控手数，默认 0.5；1 手账户仍是 1 手",
            )

    multi_box = _detail_panel(
        "multi_factor",
        "多因子CTA（加权打分）",
        "完整版固定权重。日线收盘相对 EMA 定方向，信号周期跟左边公共项；SAR 开关只对这一族生效。平仓后分摊盈亏，导出写入衰减表。",
    )
    _pack_cta_shared(multi_box)
    _pack_cta_filter(
        multi_box,
        "multi_factor",
        show_score=True,
        show_size=True,
        note="加权打分：ADX 太低或波动太缩就不开；弱趋势只收更高总分，并且仓位打成数。",
    )

    swing_box = _detail_panel(
        "swing_trend",
        "长线趋势加减仓",
        "大周期定方向 + 信号周期回调；可加可减。回调跟左边信号周期，止损跟左边止损周期。",
    )
    app._option_row(
        swing_box,
        "长线大周期",
        app.swing_scale_var,
        list(app._swing_label_by_scale.values()),
        width=260,
    )
    _note_block(
        swing_box,
        "上面只选「用日线还是 60 分钟来定方向」。\n"
        "回调进出跟左边「信号周期」，止损跟左边「止损周期」。想用 60 分钟进出，把信号周期改成 60 分钟即可。",
        pady=(0, 4),
    )
    _param_entry(
        swing_box,
        "长线大周期均线",
        app.swing_daily_ma_var,
        hint="大周期几根 EMA 定方向，默认 20",
    )
    _param_entry(
        swing_box,
        "长线单元手数",
        app.swing_unit_var,
        hint="首开/每次加仓/每次减仓的手数",
    )
    _param_entry(
        swing_box,
        "长线最多加仓",
        app.swing_max_adds_var,
        hint="浮盈后再回调，最多加几次",
    )
    _param_entry(
        swing_box,
        "长线加仓门槛",
        app.swing_add_atr_var,
        hint="相对均价至少浮盈几倍波动才允许加",
    )
    _param_entry(
        swing_box,
        "长线减仓伸展",
        app.swing_reduce_atr_var,
        hint="离小周期均线冲出几倍波动时先减仓",
    )
    _pack_trend_filter(
        swing_box,
        "swing_trend",
        "大周期没趋势时加减仓没意义。默认关，需要再开。",
    )

    sbox = _detail_panel(
        "swing_box",
        "长线箱体突破",
        "按左边信号周期画震荡箱子，收盘走出才顺着做。止损周期跟左边那一格。默认不收盘清仓。",
    )
    _note_block(
        sbox,
        "和日内实体一样：只认收盘、开仓后箱子冻住、假突破收回箱子就走。\n"
        "周期跟左边「信号周期 / 止损周期」。想测日线就把信号周期改成日线；想测 60 分钟就改成 60 分钟。\n"
        "「箱子回看根数」是这个周期上的根数：日线 40 根大约两个月，60 分钟 40 根大约两周。\n"
        "手数仍用左边的每笔风险%。ADX 只用来挡住完全没方向的行情，不会等趋势走完再追。",
        pady=(0, 4),
    )
    _param_entry(sbox, "箱子回看根数", app.sbox_lookback_var, hint="按信号周期计，默认 40 根")
    _param_entry(sbox, "箱子最矮几倍ATR", app.sbox_height_atr_var, hint="太窄的箱子假突破多，默认 1.2")
    _param_entry(sbox, "ADX下限", app.sbox_adx_min_var, hint="低于这个数当没方向，不开。默认 20。填 0 关掉")
    _param_entry(sbox, "止损缓冲ATR", app.sbox_stop_atr_var, hint="多单止损在箱顶下面这么多倍 ATR，默认 0.5")
    _param_entry(sbox, "跟踪ATR倍数", app.sbox_trail_atr_var, hint="浮盈够了以后，止损离新高/新低几倍 ATR，默认 2")
    _param_entry(sbox, "几倍箱高才跟踪", app.sbox_trail_h_var, hint="浮盈达到箱子高度的几倍才开始跟踪，默认 1")

    try:
        app.engine_var.trace_add("write", app._refresh_strategy_param_panel)
    except Exception:  # noqa: BLE001
        pass
    # 窗口变宽/变窄时微调右侧说明换行，但不改变参数框目标宽度。
    try:
        split.bind("<Configure>", app._on_strategy_split_configure, add="+")
    except Exception:  # noqa: BLE001
        pass
    app._refresh_strategy_param_panel()

    # 共用设置挂在左侧列里，跟「选策略」连成一条；右侧多高也不会把这里顶下去。
    ctk.CTkLabel(
        left,
        text="—— 各策略共用的下单与仓位设置 ——",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        anchor="w",
    ).pack(anchor="w", pady=(12, 4))
    app._option_row(left, "执行通道", app.exec_var, list_executors(), width=220)
    app._option_row(
        left,
        "CTP操作账",
        app.ctp_kind_var,
        getattr(app, "ctp_kind_choices", ["仿真账号", "真实账号"]),
        width=220,
    )
    ctk.CTkLabel(
        left,
        text="仿真=28077+仿真前置。真实=上面的资金账号+下面两个实盘前置。账号不用填两遍。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 4))
    app._entry_row(
        left,
        "实盘交易前置",
        app.ctp_live_td_var,
        hint="只给 CTP 真实资金用。华泰给你的实盘交易地址，例如 tcp://主机:端口。仿真前置不要填这里",
        width=160,
    )
    app._entry_row(
        left,
        "实盘行情前置",
        app.ctp_live_md_var,
        hint="只给 CTP 真实资金用。实盘行情地址。留空时测试窗口看不到真实盘口",
        width=160,
    )
    app._entry_row(
        left,
        "开仓滑点上限(×ATR)",
        app.max_slip_var,
        hint="信号价和现价差超过它就不开这一根，等下一根；0=关闭",
        width=160,
    )
    app._entry_row(
        left,
        "开仓滑点上限(点)",
        app.max_slip_points_var,
        hint="直接写死多少点；0=不用。和上一行同时开，取更严的那个",
        width=160,
    )
    from src.strategy.session_scope import SCOPE_LABELS

    app._option_row(
        left,
        "交易时段",
        app.session_scope_var,
        list(SCOPE_LABELS.values()),
        width=200,
    )
    ctk.CTkLabel(
        left,
        text="全时段 / 只做日盘 / 只做夜盘：只决定能不能开新仓，已经拿着的仓仍按止损和收盘清仓走。"
        "夜日连续（跳空小）：只建议隔夜跳空不大的品种用。日内实体会把夜盘拿到日盘 14:55，"
        "跳空大时最大回撤会明显变大。燃油这类常大幅跳空的，继续用全时段，让夜盘自己清仓。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 6))
    app._entry_row(
        left,
        "扫描间隔秒",
        app.scan_var,
        hint="一轮扫描之间隔多久，5~600 秒；止损发现快慢跟着它",
        width=160,
    )
    app._pack_watch_editor(left)
    app._option_row(left, "仓位模式", app.sizing_mode_var, ["risk_pct", "fixed"], width=220)
    app._entry_row(
        left,
        "账户权益(元)",
        app.capital_var,
        hint="当前账户余额，要改去账户分析",
        readonly=True,
        width=160,
    )
    app._entry_row(
        left,
        "兜底每手保证金(元)",
        app.margin_var,
        hint="品种表没填保证金比例时用这个固定金额；填了比例就按价格×每手数量×比例，0=不限制",
        width=160,
    )
    # 品种保证金+乘数只认 Excel，不再手打长串，避免和配置文件各改各的对不齐。
    app._pack_margin_table_row(left)
    # 每笔风险%、仓位上限% 放在下面「风控与资金管理」，和日亏损等放一起。
    app._entry_row(
        left,
        "固定手数(后备)",
        app.volume_var,
        hint="仓位模式=fixed 时用；或缺止损价时",
        width=160,
    )
    app._entry_row(
        left,
        "清仓时刻",
        app.flatten_var,
        hint="如 14:55,22:55，开关关掉时不使用",
        width=160,
    )
    flat_row = ctk.CTkFrame(left, fg_color="transparent")
    flat_row.pack(fill="x", pady=8)
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
        variable=app.flatten_at_close_var,
        progress_color=T.OK,
        fg_color=T.BORDER,
    ).pack(side="left")
    ctk.CTkLabel(
        flat_row,
        text="开=到点强平；关=仓可以留过午盘、夜盘和周末，只靠止损。周五临收仍不开新仓",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=280,
        justify="left",
        anchor="w",
    ).pack(side="left", padx=(10, 0))

    # 模拟开关只放在运行台，避免两处开关互相覆盖。

    # 收盘后关机
    sd_row = ctk.CTkFrame(left, fg_color="transparent")
    sd_row.pack(fill="x", pady=8)
    ctk.CTkLabel(
        sd_row,
        text="收盘后关机",
        font=T.FONT_BODY,
        text_color=T.TEXT_DIM,
        width=140,
        anchor="w",
    ).pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        sd_row,
        text="",
        variable=app.auto_shutdown_var,
        progress_color=T.OK,
        fg_color=T.BORDER,
    ).pack(side="left")
    ctk.CTkLabel(
        sd_row,
        text="开=当日最后清仓后再关电脑（有夜盘会等到夜里；倒计时内可 shutdown /a 取消）",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=280,
        justify="left",
        anchor="w",
    ).pack(side="left", padx=(10, 0))

    # —— 开仓设置（所有策略共用）：信号周期定开仓用哪根K线；触发方式定收不收线就进 ——
    app._section(scroll, "开仓设置（所有策略共用）")
    box_entry = app._card(scroll)
    from src.strategy.types import ENTRY_MODE_LABELS

    app._timeframe_row(box_entry, "信号周期", app.tf_var, app.tf_custom_var, width=160)
    _choice_row(
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
    _param_entry(sar_box, "SAR初始加速", app.generic_stop_sar_start_var, hint="默认 0.02，常用 0.01~0.04")
    _param_entry(sar_box, "SAR加速步长", app.generic_stop_sar_step_var, hint="默认 0.02；越大越灵敏")
    _param_entry(sar_box, "SAR加速上限", app.generic_stop_sar_max_var, hint="默认 0.2，常用 0.1~0.3")

    prior_box = _stop_panel(
        "prior_extreme",
        "通用止损参数 · 前高低点",
        "多单守最近 N 根最低，空单守最近 N 根最高；窗口不含当前这根。",
    )
    _param_entry(prior_box, "回看根数", app.generic_stop_lookback_var, hint="最近 N 根，2~200，默认 20")

    ch_box = _stop_panel(
        "chandelier",
        "通用止损参数 · Chandelier 吊灯跟踪",
        "多单止损=最近 N 根最高−k×ATR，空单=最近 N 根最低+k×ATR；"
        "随新高/新低移动，旧高低点滑出窗口时这条线会跟着回退。",
    )
    _param_entry(ch_box, "回看根数", app.generic_stop_lookback_var, hint="最近 N 根，2~200，默认 20")
    _param_entry(ch_box, "ATR根数", app.generic_stop_atr_period_var, hint="默认 14，常用 10~20")
    _param_entry(ch_box, "ATR倍数", app.generic_stop_atr_mult_var, hint="默认 3.0，常用 2.5~4")

    be_box = _stop_panel(
        "breakeven",
        "通用止损参数 · 盈利1ATR后保本",
        "开仓后浮盈达到 k×ATR，就把止损抬到开仓价；"
        "没赚到 k×ATR 之前这条线不生效，仍由策略自身止损兜着。",
    )
    _param_entry(
        be_box,
        "浮盈触发倍数",
        app.generic_stop_be_atr_var,
        hint="单位 ATR，默认 1.0；越小越早保本，也越容易被正常回踩扫掉",
    )

    from src.strategy.types import STOP_MODE_LABELS
    from src.strategy.generic_stop import GENERIC_STOP_LABELS

    app._timeframe_row(stop_left, "止损周期", app.trail_var, app.trail_custom_var, width=160)
    _choice_row(
        stop_left,
        "止损触发方式",
        app.stop_mode_var,
        list(STOP_MODE_LABELS.values()),
        hint="触及=碰线就走；收盘=收线确认",
        width=220,
    )
    _choice_row(
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
