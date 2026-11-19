"""
账号页：号码、密码、执行通道和柜台前置。

功能：登录与连柜台的设置集中在这一页；策略开平仓在「交易配置」。
原理：号码写进 config.yaml（仿真号码写仿真档案）；密码只写 secrets.local.yaml。
打包 exe 时密钥文件不会带走，换电脑后到这一页再填一次即可。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.ui import theme as T

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def _note(parent: ctk.CTkFrame, text: str) -> None:
    """卡片底下的灰色说明，给不懂代码的人看。"""
    ctk.CTkLabel(
        parent,
        text=text,
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(4, 8))


def _pack_channel_section(app: AutoTradingApp, scroll: ctk.CTkFrame) -> None:
    """执行通道、仿真/真实、券商前置。原来在配置页左列，整段搬到这里。"""
    from src.trader.factory import list_executors

    app._section(scroll, "下单通道与柜台")
    box = app._card(scroll)
    app._option_row(box, "执行通道", app.exec_var, list_executors(), width=220)
    _note(
        box,
        "sim=只算账不下真单；gui=点同花顺；ctp=华泰柜台。选 ctp 后下面填仿真或真实。",
    )
    app._option_row(
        box,
        "CTP操作账",
        app.ctp_kind_var,
        getattr(app, "ctp_kind_choices", ["仿真账号", "真实账号"]),
        width=220,
    )
    _note(
        box,
        "和交易台一样只换账号：仿真=上面的仿真账号，真实=资金账号。策略同一套。"
        "选完请保存，账户分析会自动跟到同一本账。",
    )
    app._option_row(
        box,
        "券商档案",
        app.ctp_profile_var,
        getattr(app, "ctp_profile_choices", ["default"]),
        width=220,
    )
    _note(box, "换期货公司只改档案，不改代码。没有档案时这里显示 default。")
    ctk.CTkLabel(
        box,
        textvariable=app.ctp_profile_note_var,
        font=T.FONT_SMALL,
        text_color=T.WARN,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 4))
    app._entry_row(
        box,
        "档案交易前置",
        app.ctp_profile_td_var,
        hint="可填多台，用分号隔开。连接时全部登记，断线由柜台自动换",
        width=220,
    )
    app._entry_row(
        box,
        "档案行情前置",
        app.ctp_profile_md_var,
        hint="行情也可填多台备用，端口通常和交易前置不同",
        width=220,
    )
    app._entry_row(
        box,
        "档案BrokerID",
        app.ctp_profile_broker_var,
        hint="柜台给的 BrokerID，例如 8080 / 9999",
        width=220,
    )
    app._entry_row(
        box,
        "档案资金账号",
        app.ctp_profile_user_var,
        hint="当前档案号码。仿真请改下面「仿真账号」；真实档案留空则用真实资金账号",
        width=220,
    )
    app._entry_row(
        box,
        "档案密码变量",
        app.ctp_profile_pwd_env_var,
        hint="环境变量名，默认 CTP_PASSWORD；真实档案默认 CTP_LIVE_PASSWORD",
        width=220,
    )
    app._entry_row(
        box,
        "档案认证码变量",
        app.ctp_profile_auth_env_var,
        hint="环境变量名，默认 CTP_AUTH_CODE；该柜台不需要认证码时留空",
        width=220,
    )
    ctk.CTkButton(
        box,
        text="测试交易前置连通",
        width=180,
        command=app._test_ctp_front,
        fg_color=T.ACCENT,
        hover_color=T.ACCENT_HOVER,
        text_color=T.TEXT,
    ).pack(anchor="w", pady=(2, 8))


def build_credentials_tab(app: AutoTradingApp, tab: ctk.CTkFrame) -> None:
    """账号页：号码密码 + 通道前置。保存按钮与交易配置同一套写盘。"""
    scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=8, pady=8)

    app._section(scroll, "怎么用")
    intro = app._card(scroll)
    _note(
        intro,
        "这一页管「用哪个号、走哪条通道、连哪台柜台」。策略、品种、开仓止损在「交易配置」。"
        "密码只保存在本机 config/secrets.local.yaml，不会打进 exe。"
        "某一格密码留空再保存，表示不改已经存过的密码。",
    )

    _pack_channel_section(app, scroll)

    # —— 同花顺登录 + 华泰真实资金 ——
    app._section(scroll, "同花顺 / 真实柜台")
    live = app._card(scroll)
    app._entry_row(live, "期货公司", app.broker_var)
    app._entry_row(live, "通道名称", app.channel_var, hint="如：华泰期货_CTP主席")
    app._entry_row(
        live,
        "资金账号",
        app.user_var,
        hint="华泰真实资金账号。同花顺登录和 CTP 真实通道都用这一号",
    )
    app.pwd_entry = app._password_row(live, "交易密码", app.password_var, app._show_password)
    _note(
        live,
        "交易密码给同花顺自动登录用，CTP 真实通道默认也用这一份。"
        "若柜台密码和同花顺不一样，请在密钥文件里另写 ctp_live_password。",
    )
    app._option_row(live, "验证码模式", app.captcha_var, ["pause", "skip"], width=280)
    login_row = ctk.CTkFrame(live, fg_color="transparent")
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
    app._entry_row(live, "密码环境变量名", app.pwd_env_var, hint="一般不用改，默认 FUTURES_PASSWORD")

    # —— 华泰仿真：号码和密码以前只能手改文件 ——
    app._section(scroll, "华泰仿真")
    sim = app._card(scroll)
    app._entry_row(
        sim,
        "仿真账号",
        app.ctp_sim_user_var,
        hint="例如 28077，写进券商档案 huatai_sim，不是上面的真实资金号",
    )
    app.ctp_sim_pwd_entry = app._password_row(
        sim, "仿真密码", app.ctp_sim_password_var, app._show_ctp_sim_password
    )
    app.ctp_auth_entry = app._password_row(
        sim, "认证码", app.ctp_auth_code_var, app._show_ctp_auth
    )
    _note(
        sim,
        "仿真密码写密钥文件的 ctp_password，认证码写 ctp_auth_code。"
        "上面「CTP操作账」选仿真后，用的就是这一号。真实资金号不要填到这里。",
    )

    # —— 天勤只拉行情，和下单账号分开 ——
    app._section(scroll, "天勤行情")
    tq = app._card(scroll)
    app._entry_row(
        tq,
        "天勤行情账号",
        app.tq_user_var,
        hint="选「天勤」行情时用；TqSdk 手机号/账号",
    )
    app.tq_pwd_entry = app._password_row(
        tq, "天勤行情密码", app.tq_password_var, app._show_tq_password
    )
    _note(tq, "和下单无关。「交易配置」里行情来源选天勤时才会登录天勤。")

    actions = ctk.CTkFrame(scroll, fg_color="transparent")
    actions.pack(fill="x", pady=(16, 24))
    ctk.CTkButton(
        actions,
        text="保存账号与通道",
        height=44,
        fg_color=T.ACCENT,
        hover_color=T.ACCENT_HOVER,
        text_color=T.ACCENT_ON,
        font=T.FONT_BODY,
        command=app._on_save,
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        actions,
        text="重新加载",
        height=44,
        fg_color=T.BG_ELEVATED,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.BORDER,
        text_color=T.TEXT,
        command=app._on_reload,
    ).pack(side="left")
