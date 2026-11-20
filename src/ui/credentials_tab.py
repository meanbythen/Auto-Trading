"""
账号页：号码、密码、执行通道和柜台前置。

功能：登录与连柜台的设置集中在这一页；策略开平仓在「交易配置」。
原理：号码写进 config.yaml（仿真号码写仿真档案）；密码只写 secrets.local.yaml。
打包 exe 时密钥文件不会带走，换电脑后到这一页再填一次即可。
"""

from __future__ import annotations

from tkinter import messagebox, simpledialog
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


_STATUS_LABELS = {"enabled": "启用", "disabled": "停用", "archived": "归档"}
_ENV_LABELS = {"sim": "仿真", "live": "真实"}


def _load_registry(app: AutoTradingApp):
    """读注册表；文件缺失时按旧配置迁移一次。失败或已关闭返回 None。"""
    from src.account.registry import ensure_registry, registry_disabled
    from src.utils.config_loader import load_secrets

    if registry_disabled(app.cfg):
        return None
    try:
        return ensure_registry(app.root_dir, app.cfg, load_secrets())
    except Exception:  # noqa: BLE001 - 账号页不能因为注册表坏而打不开
        return None


def _selected_account(app: AutoTradingApp):
    registry = _load_registry(app)
    if registry is None:
        return None, None
    labels = getattr(app, "_account_labels", {})
    account_id = labels.get(app.account_pick_var.get(), "")
    if account_id:
        return registry, registry.account(account_id)
    return registry, (registry.accounts[0] if registry.accounts else None)


def _apply_registry(app: AutoTradingApp, registry) -> None:
    """把注册表物化进当前内存配置，并刷新「券商档案」下拉。"""
    try:
        from src.utils.config_loader import load_secrets

        registry.materialize(app.cfg, load_secrets())
    except Exception as exc:  # noqa: BLE001
        app._append_log(f"账号注册表物化失败：{exc}")
    fill = getattr(app, "_fill_ctp_profile_vars", None)
    if callable(fill):
        fill()


def _refresh_accounts(app: AutoTradingApp) -> None:
    """重画账号下拉；标签里带环境与状态，选中后回填显示名/资金号。"""
    registry = _load_registry(app)
    labels: list[str] = []
    mapping: dict[str, str] = {}
    if registry is not None:
        for account in registry.accounts:
            label = (
                f"{account.display_name} · {_ENV_LABELS.get(account.environment, account.environment)}"
                f" · {_STATUS_LABELS.get(account.status, account.status)}"
            )
            if label in mapping:
                label = f"{label} ({account.account_id})"
            labels.append(label)
            mapping[label] = account.account_id
    app._account_labels = mapping
    values = labels or ["（还没有账号）"]
    app.account_menu.configure(values=values)
    current = app.account_pick_var.get()
    app.account_pick_var.set(current if current in values else values[0])
    _on_account_pick(app)


def _on_account_pick(app: AutoTradingApp) -> None:
    _registry, account = _selected_account(app)
    if account is None:
        app.account_name_var.set("")
        app.account_investor_var.set("")
        app.account_meta_var.set("还没有账号。先在配置页保存一次，会自动迁移现有券商档案。")
        return
    app.account_name_var.set(account.display_name)
    app.account_investor_var.set(account.investor_id)
    parts = [
        f"ID {account.account_id}",
        f"渠道 {account.channel}",
        f"环境 {_ENV_LABELS.get(account.environment, account.environment)}",
        f"状态 {_STATUS_LABELS.get(account.status, account.status)}",
    ]
    if account.profile_id:
        parts.append(f"模板 {account.profile_id}")
    if account.book_id:
        parts.append(f"账本 {account.book_id}")
    app.account_meta_var.set(" · ".join(parts))


def _persist_accounts(app: AutoTradingApp, registry, action: str, account_id: str, detail: str = "") -> bool:
    """写注册表 + 审计 + 刷新界面。返回是否成功。"""
    from src.account.registry import record_audit

    try:
        registry.save(app.root_dir)
    except OSError as exc:
        messagebox.showerror("保存失败", f"账号注册表写不进去：\n{exc}", parent=app)
        return False
    record_audit(app.root_dir, action, account_id=account_id, detail=detail)
    _apply_registry(app, registry)
    _refresh_accounts(app)
    app._append_log(f"账号注册表已更新：{action} {account_id}")
    return True


def _template_from_ui(app: AutoTradingApp, name: str, environment: str):
    """把当前「券商档案」输入框的内容复制成新账号的连接模板。"""
    from src.domain.trading_account import BrokerProfileTemplate
    from src.trader.ctp_fronts import split_fronts

    ctp = (app.cfg.get("trade") or {}).get("ctp") or {}
    try:
        limit = int(float(app.ctp_max_volume_var.get()))
    except (TypeError, ValueError):
        limit = 20
    env_name = str(app.ctp_profile_pwd_env_var.get() or "").strip()
    if not env_name:
        env_name = "CTP_LIVE_PASSWORD" if environment == "live" else "CTP_PASSWORD"
    return BrokerProfileTemplate(
        profile_id="placeholder",
        label=name,
        broker=str(app.broker_var.get() or "").strip(),
        environment=environment,
        broker_id=str(app.ctp_profile_broker_var.get() or "").strip(),
        td_fronts=split_fronts(app.ctp_profile_td_var.get()),
        md_fronts=split_fronts(app.ctp_profile_md_var.get()),
        app_id=str(ctp.get("app_id") or ""),
        flow_dir=str(ctp.get("flow_dir") or ""),
        password_env=env_name,
        auth_code_env=str(app.ctp_profile_auth_env_var.get() or "").strip(),
        max_order_volume=max(1, min(1000, limit)),
    )


def _confirm_live(app: AutoTradingApp, investor_id: str) -> bool:
    """真实账号二次确认：输入资金账号后四位。"""
    if len(investor_id) < 4:
        messagebox.showerror("资金账号不对", "真实资金账号至少 4 位，才能做后四位确认。", parent=app)
        return False
    entered = simpledialog.askstring(
        "实盘确认",
        f"即将使用真实账号 {investor_id}。\n请输入资金账号后四位确认：",
        parent=app,
    )
    if entered is None:
        return False
    if entered.strip() != investor_id[-4:]:
        messagebox.showerror("已取消", "后四位不一致，没有保存任何改动。", parent=app)
        return False
    return True


def _show_problems(app: AutoTradingApp, problems: list[str]) -> None:
    messagebox.showerror(
        "账号配置有问题",
        "下面这些必须先改好再保存：\n\n" + "\n".join(f"· {item}" for item in problems[:8]),
        parent=app,
    )


def _save_account_fields(app: AutoTradingApp) -> None:
    registry, account = _selected_account(app)
    if registry is None or account is None:
        messagebox.showwarning("还没有账号", "请先在配置页保存一次配置，生成账号注册表。", parent=app)
        return
    old_name, old_investor = account.display_name, account.investor_id
    account.display_name = app.account_name_var.get().strip() or old_name
    account.investor_id = app.account_investor_var.get().strip()
    account.touch()
    problems = registry.validate()
    if problems:
        account.display_name, account.investor_id = old_name, old_investor
        _show_problems(app, problems)
        return
    _persist_accounts(app, registry, "update", account.account_id, f"{old_investor}→{account.investor_id}")


def _new_account(app: AutoTradingApp, environment: str) -> None:
    registry = _load_registry(app)
    if registry is None:
        messagebox.showwarning("还没有账号", "请先在配置页保存一次配置，生成账号注册表。", parent=app)
        return
    name = simpledialog.askstring("新建账号", "显示名（例如 华泰仿真-2号）：", parent=app)
    if name is None:
        return
    name = name.strip()
    if not name:
        return
    hint = "仿真号码（可先留空，账号会先停用）：" if environment == "sim" else "真实资金账号："
    investor = simpledialog.askstring("新建账号", hint, parent=app)
    if investor is None:
        return
    investor = investor.strip()
    if environment == "live" and not _confirm_live(app, investor):
        return
    account = registry.add_account(
        name,
        environment,
        investor,
        channel="ctp",
        broker=str(app.broker_var.get() or "").strip(),
        template=_template_from_ui(app, name, environment),
    )
    problems = registry.validate()
    if problems:
        _show_problems(app, problems)
        return
    from src.account.registry import record_audit

    if _persist_accounts(app, registry, "add", account.account_id, f"{environment}:{name}"):
        if environment == "live":
            record_audit(app.root_dir, "env_change", account_id=account.account_id, detail="新建真实账号，已确认后四位")
        app._append_log(f"已新增账号 {name}（{account.account_id}），可在「券商档案」里选中")


def _duplicate_account(app: AutoTradingApp) -> None:
    registry, source = _selected_account(app)
    if registry is None or source is None:
        messagebox.showwarning("还没有账号", "请先选一个账号再复制。", parent=app)
        return
    name = simpledialog.askstring(
        "复制账号",
        "新账号显示名：",
        initialvalue=f"{source.display_name} 副本",
        parent=app,
    )
    if name is None or not name.strip():
        return
    investor = simpledialog.askstring(
        "复制账号",
        "资金账号（留空沿用原账号）：",
        initialvalue=source.investor_id,
        parent=app,
    )
    if investor is None:
        return
    investor = investor.strip() or source.investor_id
    if source.environment == "live":
        if investor == source.investor_id:
            messagebox.showerror("需要新资金号", "复制真实账号必须换一个资金账号。", parent=app)
            return
        if not _confirm_live(app, investor):
            return
    try:
        account = registry.duplicate_account(source.account_id, name.strip(), investor)
    except (KeyError, ValueError) as exc:
        messagebox.showerror("复制失败", str(exc), parent=app)
        return
    problems = registry.validate()
    if problems:
        _show_problems(app, problems)
        return
    from src.account.registry import record_audit

    if _persist_accounts(app, registry, "duplicate", account.account_id, f"源 {source.account_id}"):
        if source.environment == "live":
            record_audit(app.root_dir, "env_change", account_id=account.account_id, detail="复制真实账号，已确认后四位")


def _toggle_account_status(app: AutoTradingApp) -> None:
    registry, account = _selected_account(app)
    if registry is None or account is None:
        return
    new_status = "disabled" if account.status == "enabled" else "enabled"
    old_status = account.status
    account.status = new_status
    account.touch()
    problems = registry.validate()
    if problems:
        account.status = old_status
        _show_problems(app, problems)
        return
    _persist_accounts(app, registry, "status", account.account_id, f"{old_status}→{new_status}")


def _archive_account(app: AutoTradingApp) -> None:
    registry, account = _selected_account(app)
    if registry is None or account is None:
        return
    if not messagebox.askyesno(
        "归档账号",
        f"归档后 {account.display_name} 只读，账本保留。继续吗？",
        parent=app,
    ):
        return
    registry.set_status(account.account_id, "archived")
    _persist_accounts(app, registry, "status", account.account_id, "→archived")


def _delete_account(app: AutoTradingApp) -> None:
    registry, account = _selected_account(app)
    if registry is None or account is None:
        return
    if not messagebox.askyesno(
        "删除账号",
        f"删除 {account.display_name}？默认只做软删除：账号归档、账本保留。",
        parent=app,
    ):
        return
    registry.remove_account(account.account_id)
    _persist_accounts(app, registry, "delete", account.account_id, "软删除")


def _pack_account_registry_section(app: AutoTradingApp, scroll: ctk.CTkFrame) -> None:
    """账号注册表：一个账号一本账，新增/复制/停用/归档不用改代码。"""
    app._section(scroll, "账号注册表")
    box = app._card(scroll)
    _note(
        box,
        "账号是身份，渠道只是属性。新增账号后会自动生成一份连接模板，"
        "保存后出现在上面的「券商档案」里；停用只影响新开仓，归档只读，删除默认保留账本。",
    )
    if not hasattr(app, "account_pick_var"):
        app.account_pick_var = ctk.StringVar(value="")
        app.account_name_var = ctk.StringVar(value="")
        app.account_investor_var = ctk.StringVar(value="")
        app.account_meta_var = ctk.StringVar(value="")
    app.account_menu = app._option_row(
        box, "选择账号", app.account_pick_var, ["（还没有账号）"], width=320
    )
    app.account_menu.configure(command=lambda _value: _on_account_pick(app))
    app._entry_row(box, "显示名", app.account_name_var, width=220)
    app._entry_row(box, "资金账号", app.account_investor_var, width=220)
    ctk.CTkLabel(
        box,
        textvariable=app.account_meta_var,
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 4))

    buttons = ctk.CTkFrame(box, fg_color="transparent")
    buttons.pack(fill="x", pady=(6, 4))

    def _btn(text: str, command, accent: bool = False) -> None:
        ctk.CTkButton(
            buttons,
            text=text,
            width=102,
            height=32,
            fg_color=T.ACCENT if accent else T.BG_PANEL,
            hover_color=T.ACCENT_HOVER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.ACCENT_ON if accent else T.TEXT,
            command=command,
        ).pack(side="left", padx=(0, 6))

    _btn("保存账号", lambda: _save_account_fields(app), accent=True)
    _btn("新增仿真", lambda: _new_account(app, "sim"))
    _btn("新增真实", lambda: _new_account(app, "live"))
    _btn("复制当前", lambda: _duplicate_account(app))
    _btn("停用/启用", lambda: _toggle_account_status(app))
    _btn("归档", lambda: _archive_account(app))
    _btn("删除", lambda: _delete_account(app))
    _refresh_accounts(app)


def build_credentials_tab(app: AutoTradingApp, tab: ctk.CTkFrame) -> None:
    """账号页：号码密码 + 通道前置。保存按钮与交易配置同一套写盘。"""
    scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=8, pady=8)

    app._section(scroll, "怎么用")
    intro = app._card(scroll)
    _note(
        intro,
        "这一页管「用哪个号、走哪条通道、连哪台柜台」。策略、品种、开仓止损在「交易配置」。"
        "密码只保存在本机密钥文件（Windows 凭据加密），不会打进 exe。"
        "某一格密码留空再保存，表示不改已经存过的密码。",
    )

    _pack_channel_section(app, scroll)
    _pack_account_registry_section(app, scroll)

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
