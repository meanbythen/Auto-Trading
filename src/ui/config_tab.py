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
    app._entry_row(box, "资金账号", app.user_var)
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
        text="密码保存在 config/secrets.local.yaml（不会提交到 git），不会写入主配置。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
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
        wraplength=520,
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
        text="只拉选中的这一家。开仓前不再另问一家。",
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
    app._refresh_tq_hint()
    app._option_row(left, "策略引擎", app.engine_var, list(engine_choices), width=220)
    ctk.CTkLabel(
        left,
        text="右边整块是当前策略参数；换策略后右边跟着换。左侧下面是各策略共用设置。",
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

    def _detail_panel(engine_key: str, title: str, note: str) -> ctk.CTkFrame:
        """在右侧宿主里建一块面板，默认先藏着，选中对应引擎再显示。"""
        panel = ctk.CTkFrame(app._strategy_detail_host, fg_color="transparent")
        app._strategy_detail_panels[engine_key] = panel
        app._strategy_detail_meta[engine_key] = (title, note)
        return panel

    def _param_entry(parent: ctk.CTkFrame, label: str, var: ctk.StringVar, hint: str = "") -> None:
        """策略参数行：输入框偏窄、说明自动换行，整行能塞进右侧固定宽度。"""
        app._entry_row(
            parent,
            label,
            var,
            hint=hint,
            width=app._strategy_param_entry_w,
            hint_wrap=app._strategy_param_hint_wrap,
        )

    def _switch_row(parent: ctk.CTkFrame, label: str, var: ctk.BooleanVar, hint: str = "") -> None:
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
                wraplength=app._strategy_param_hint_wrap,
                justify="left",
                anchor="w",
            ).pack(side="left", padx=(10, 0))

    body_box = _detail_panel(
        "daily_body",
        "日内实体",
        "ETH 开盘箱体 + 收盘突破。改左边引擎才会用到这里。",
    )
    _param_entry(body_box, "ETH分钟数", app.eth_min_var, hint="开盘后观察多久再画箱体")
    _param_entry(
        body_box,
        "二次开仓ETH分钟数",
        app.body_eth2_var,
        hint="日盘第一段止损后、截止前，用这段更长的箱体再开一次；夜盘仍只开一次。默认 30，填 0 关闭",
    )
    _param_entry(
        body_box,
        "日盘开仓截止",
        app.body_day_cutoff_var,
        hint="过了这个钟点就停止开日盘新仓，默认 10:30",
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
        "跳空超过几跳不开",
        app.body_skip_gap_var,
        hint="这一段开盘相对上一段收盘跳空超过这个数就不开；0=关闭。每跳 1 点，50 就是 50 点",
    )
    _param_entry(
        body_box,
        "箱体超过几跳不开",
        app.body_max_box_var,
        hint="开盘箱体最高-最低超过这个数就不开；0=关闭。箱体越宽，箱沿止损离进场价越远",
    )
    ctk.CTkLabel(
        body_box,
        text="止损固定在开盘箱体对沿。打破对沿才平；否则拿到收盘。不做回撤止损。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", padx=4, pady=4)

    morning_box = _detail_panel(
        "morning_box_a",
        "早盘箱体 A / 高低递进 B",
        "钟点和保本用出厂默认；配置页暂无单独旋钮。",
    )
    ctk.CTkLabel(
        morning_box,
        text="暂无额外旋钮。需要改观察时段等，可在配置文件对应小节里改。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", padx=4, pady=4)
    app._strategy_detail_panels["morning_structure_b"] = morning_box
    app._strategy_detail_meta["morning_structure_b"] = app._strategy_detail_meta["morning_box_a"]

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

    st_box = _detail_panel(
        "supertrend",
        "SuperTrend 趋势",
        "ATR 通道翻转开仓；默认可隔夜。",
    )
    _param_entry(st_box, "ST的ATR根数", app.st_atr_period_var, hint="算通道用几根K线")
    _param_entry(st_box, "ST通道倍数", app.st_multiplier_var, hint="通道半宽几倍波动，常用 3")

    ma_box = _detail_panel(
        "Single_ma",
        "单均线系统",
        "收盘穿越均线开仓；根数可改。",
    )
    _param_entry(ma_box, "单均线根数", app.ma_period_var, hint="常用 20 / 60 / 80 / 120")
    _switch_row(
        ma_box,
        "反向时反手",
        app.ma_reverse_var,
        hint="开=收盘反向穿越均线就反手开新仓；关=只平仓，等下一次顺向信号",
    )
    app._strategy_detail_panels["single_ma"] = ma_box
    app._strategy_detail_meta["single_ma"] = app._strategy_detail_meta["Single_ma"]

    dual_box = _detail_panel(
        "dual_ma",
        "双均线趋势",
        "短均线与长均线交叉；长根数须大于短根数。",
    )
    _param_entry(dual_box, "双均线短根数", app.dual_short_var, hint="默认 20")
    _param_entry(dual_box, "双均线长根数", app.dual_long_var, hint="默认 60，须大于短根数")
    _switch_row(
        dual_box,
        "反向时反手",
        app.dual_reverse_var,
        hint="开=均线反向交叉就反手开新仓；关=只平仓，等下一次顺向交叉",
    )

    swing_box = _detail_panel(
        "swing_trend",
        "长线趋势加减仓",
        "大周期定方向 + 小周期回调；可加可减。不跟左侧的「信号周期」走。",
    )
    app._option_row(
        swing_box,
        "长线周期搭配",
        app.swing_scale_var,
        list(app._swing_label_by_scale.values()),
        width=260,
    )
    _param_entry(
        swing_box,
        "长线大周期均线",
        app.swing_daily_ma_var,
        hint="大周期几根均线定方向，默认 20",
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
    # 左侧说明换行，避免一行说明把左列撑太宽、中间又留出空白。
    _left_hint = 180
    app._option_row(left, "执行通道", app.exec_var, list_executors(), width=220)
    app._timeframe_row(left, "信号周期", app.tf_var, app.tf_custom_var, width=160)
    app._timeframe_row(left, "止损周期", app.trail_var, app.trail_custom_var, width=160)
    from src.strategy.session_scope import SCOPE_LABELS

    app._option_row(
        left,
        "交易时段",
        app.session_scope_var,
        list(SCOPE_LABELS.values()),
        width=160,
    )
    ctk.CTkLabel(
        left,
        text="只决定能不能开新仓。已经拿着的仓仍按各自的止损和收盘清仓走。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 6))
    app._pack_watch_editor(left)
    app._option_row(left, "仓位模式", app.sizing_mode_var, ["risk_pct", "fixed"], width=220)
    app._entry_row(
        left,
        "账户权益(元)",
        app.capital_var,
        hint="当前账户余额，要改去账户分析",
        readonly=True,
        width=160,
        hint_wrap=_left_hint,
    )
    app._entry_row(
        left,
        "每手保证金(元)",
        app.margin_var,
        hint="表里没写的品种用这个，0=不限制",
        width=160,
        hint_wrap=_left_hint,
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
        hint_wrap=_left_hint,
    )
    app._entry_row(left, "扫描间隔秒", app.scan_var, width=160)
    app._entry_row(
        left,
        "清仓时刻",
        app.flatten_var,
        hint="如 14:55,22:55，开关关掉时不使用",
        width=160,
        hint_wrap=_left_hint,
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

    dry2 = ctk.CTkFrame(left, fg_color="transparent")
    dry2.pack(fill="x", pady=8)
    ctk.CTkLabel(dry2, text="模拟模式 dry_run", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w").pack(
        side="left", padx=(0, 8)
    )
    ctk.CTkSwitch(dry2, text="", variable=app.dry_var, progress_color=T.OK, fg_color=T.BORDER).pack(side="left")

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

    # —— 风控与资金管理：算几手、最多亏多少、开仓频率（排在同花顺软件之前，方便先调仓位）——
    app._section(scroll, "风控与资金管理")
    box4 = app._card(scroll)
    ctk.CTkLabel(
        box4,
        text=(
            "开仓手数取小：①每笔风险%（或上面固定手数）②仓位上限%。"
            "日最大亏损只停开新仓，不自动平仓。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))
    app._entry_row(
        box4,
        "每笔风险%",
        app.risk_pct_var,
        hint="推荐0.5~1，止损远时自动少开",
    )
    app._entry_row(
        box4,
        "仓位上限%",
        app.margin_usage_pct_var,
        hint="权益里最多拿出这么多交保证金，推荐70~80；0=按全部权益",
    )
    app._entry_row(box4, "日最大亏损(元)", app.max_loss_var)
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
            "紧急停止：把鼠标甩到屏幕左上角"
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
            "份里包含这个页面上的数字和开关，例如收盘清仓、模拟模式。"
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
