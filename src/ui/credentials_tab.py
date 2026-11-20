"""
交易账号页：账号（身份）、连接（期货公司接入点）和当前使用账号。

功能：一个账号一本账；账号只引用连接，换期货公司 = 新增连接 + 账号改绑。
原理：账号写 config/accounts.yaml（YAML 权威），密码只写本机 DPAPI 密钥库；
      当前使用账号写 data/active_account.json，执行通道 / 柜台 / 账本一起切换。
"""

from __future__ import annotations

from tkinter import messagebox, simpledialog
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.ui import theme as T

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


_STATUS_LABELS = {"enabled": "启用", "disabled": "停用", "archived": "归档"}
_ENV_LABELS = {"sim": "仿真", "live": "真实"}
_CHANNEL_LABELS = {"ctp": "CTP", "gui": "GUI", "sim": "SIM"}


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
    """当前在左栏选中的账号；没有选中就取第一个。"""
    registry = _load_registry(app)
    if registry is None:
        return None, None
    account = registry.account(getattr(app, "account_selected_id", ""))
    if account is None and registry.accounts:
        account = registry.accounts[0]
        app.account_selected_id = account.account_id
    return registry, account


def _active_account(app: AutoTradingApp, registry):
    """当前真正在用的账号（data/active_account.json）。"""
    from src.account.session import active_account_id

    account_id = active_account_id(app.root_dir)
    if account_id and registry is not None:
        account = registry.account(account_id)
        if account is not None:
            return account
    return None


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


def _account_title(account) -> str:
    env = _ENV_LABELS.get(account.environment, account.environment)
    status = _STATUS_LABELS.get(account.status, account.status)
    return f"{account.display_name} · {env} · {status}"


def _pick_account(app: AutoTradingApp, account_id: str) -> None:
    app.account_selected_id = account_id
    _refresh_accounts(app)


def _refresh_accounts(app: AutoTradingApp) -> None:
    """重画当前账号卡片、左栏列表和右侧详情。"""
    menu = getattr(app, "ctp_profile_menu", None)
    choices = getattr(app, "ctp_profile_choices", None)
    if menu is not None and choices:
        try:
            menu.configure(values=list(choices))
        except Exception:  # noqa: BLE001 - 控件已销毁
            pass
    _refresh_current_card(app)
    _render_account_list(app)
    _render_account_detail(app)


def _refresh_current_card(app: AutoTradingApp) -> None:
    """顶部只读卡片：当前用哪个账号、哪条连接、哪本账。"""
    registry = _load_registry(app)
    account = _active_account(app, registry) if registry is not None else None
    if account is not None:
        env = "LIVE" if account.environment == "live" else "SIM"
        channel = _CHANNEL_LABELS.get(account.channel, account.channel.upper())
        parts = [account.display_name, env, channel]
        if account.profile_id:
            profile = registry.profile(account.profile_id)
            parts.append(f"连接 {(profile.label if profile else account.profile_id)}")
        if account.book_id:
            parts.append(f"账本 {account.book_id}")
        app.current_account_var.set(" · ".join(parts))
        app.current_note_var.set(
            "当前使用（只读）。切换账号会同时切换执行通道、柜台、账本和风控，"
            "不会搬动已有成交。"
        )
        return
    from src.domain.accounts import load_active_id, profile_of

    active = load_active_id(app.root_dir)
    info = profile_of(active, root=app.root_dir)
    app.current_account_var.set(f"{info['label']} · 旧账本 {active}（尚未绑定账号）")
    app.current_note_var.set(
        "还在用旧的四本账。选中左侧账号后点「设为当前账号」，之后执行通道、柜台和账本会跟着账号走。"
    )


def _render_account_list(app: AutoTradingApp) -> None:
    holder = getattr(app, "account_list_frame", None)
    if holder is None:
        return
    for child in holder.winfo_children():
        child.destroy()
    registry = _load_registry(app)
    accounts = list(registry.accounts) if registry is not None else []
    if not accounts:
        ctk.CTkLabel(
            holder,
            text="还没有账号。点下面「新增仿真」或「新增真实」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=220,
            justify="left",
        ).pack(anchor="w", padx=6, pady=8)
        return
    active = _active_account(app, registry)
    selected = getattr(app, "account_selected_id", "")
    for account in accounts:
        is_active = active is not None and active.account_id == account.account_id
        is_selected = account.account_id == selected
        env = _ENV_LABELS.get(account.environment, account.environment)
        status = _STATUS_LABELS.get(account.status, account.status)
        mention = " ●" if is_active else ""
        ctk.CTkButton(
            holder,
            text=f"{account.display_name}{mention}\n{env} · {status} · {account.book_id or '—'}",
            anchor="w",
            height=54,
            corner_radius=6,
            fg_color=T.ACCENT if is_selected else T.BG_PANEL,
            hover_color=T.ACCENT_HOVER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.ACCENT_ON if is_selected else T.TEXT,
            font=T.FONT_SMALL,
            command=lambda aid=account.account_id: _pick_account(app, aid),
        ).pack(fill="x", padx=4, pady=3)


def _ref_has_value(ref: str) -> bool:
    if not ref:
        return False
    try:
        from src.utils.secret_store import default_store

        return bool(default_store().resolve(ref))
    except Exception:  # noqa: BLE001 - 只是显示状态
        return False


def _password_saved(app: AutoTradingApp, registry, account) -> bool:
    profile = registry.profile(account.profile_id) if account.profile_id else None
    refs = [account.password_ref]
    if profile is not None:
        refs.append(profile.password_ref)
    return any(_ref_has_value(ref) for ref in refs if ref)


def _render_account_detail(app: AutoTradingApp) -> None:
    holder = getattr(app, "account_detail_frame", None)
    if holder is None:
        return
    for child in holder.winfo_children():
        child.destroy()
    registry, account = _selected_account(app)
    if registry is None or account is None:
        ctk.CTkLabel(
            holder,
            text="选中左侧账号后，这里显示详情。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=8, pady=8)
        return

    app.account_name_var.set(account.display_name)
    app.account_investor_var.set(account.investor_id)
    app.account_risk_var.set(account.risk_policy_id)
    app.account_limit_var.set(str(account.max_order_volume or ""))
    app.account_notes_var.set(account.notes)
    choices: dict[str, str] = {"（不绑定连接）": ""}
    for profile in registry.profiles:
        label = profile.label or profile.profile_id
        choices[f"{label} · {_ENV_LABELS.get(profile.environment, profile.environment)}"] = (
            profile.profile_id
        )
    current_conn = ""
    for text, pid in choices.items():
        if pid and pid == account.profile_id:
            current_conn = text
            break
    app.account_connection_var.set(current_conn or "（不绑定连接）")
    app._account_connection_choices = choices

    ctk.CTkLabel(
        holder,
        text=f"{account.account_id} · {_CHANNEL_LABELS.get(account.channel, account.channel)}"
        f" · {_ENV_LABELS.get(account.environment, account.environment)}"
        f" · {_STATUS_LABELS.get(account.status, account.status)}",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
    ).pack(anchor="w", padx=8, pady=(6, 2))
    ctk.CTkLabel(
        holder,
        text=f"账本 {account.book_id or account.account_id}（一个账号一本，只读）",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
    ).pack(anchor="w", padx=8, pady=(0, 6))

    app._entry_row(holder, "显示名", app.account_name_var, width=200)
    app._entry_row(holder, "资金账号", app.account_investor_var, width=200)
    app._option_row(
        holder,
        "连接",
        app.account_connection_var,
        list(choices),
        width=240,
    )
    app._entry_row(holder, "风控策略", app.account_risk_var, width=200)
    app._entry_row(holder, "单笔上限", app.account_limit_var, hint="0=用连接默认值", width=200)
    app._entry_row(holder, "备注", app.account_notes_var, width=200)

    pwd_row = ctk.CTkFrame(holder, fg_color="transparent")
    pwd_row.pack(fill="x", padx=8, pady=(4, 4))
    saved = _password_saved(app, registry, account)
    ctk.CTkLabel(
        pwd_row,
        text=f"密码：{'已保存' if saved else '未保存'}",
        font=T.FONT_BODY,
        text_color=T.OK if saved else T.WARN,
        width=200,
        anchor="w",
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        pwd_row,
        text="更新密码",
        width=96,
        height=30,
        fg_color=T.BG_PANEL,
        hover_color=T.BORDER,
        border_width=1,
        border_color=T.BORDER,
        text_color=T.TEXT,
        command=lambda: _update_password(app, registry, account),
    ).pack(side="left")

    buttons = ctk.CTkFrame(holder, fg_color="transparent")
    buttons.pack(fill="x", padx=8, pady=(8, 2))
    _small_button(buttons, "保存账号", lambda: _save_account_fields(app), accent=True)
    _small_button(buttons, "设为当前账号", lambda: _switch_to_selected(app))
    _small_button(buttons, "复制", lambda: _duplicate_account(app))
    buttons2 = ctk.CTkFrame(holder, fg_color="transparent")
    buttons2.pack(fill="x", padx=8, pady=(0, 2))
    _small_button(buttons2, "停用/启用", lambda: _toggle_account_status(app))
    _small_button(buttons2, "归档", lambda: _archive_account(app))
    _small_button(buttons2, "删除", lambda: _delete_account(app))


def _small_button(parent, text: str, command, accent: bool = False) -> None:
    ctk.CTkButton(
        parent,
        text=text,
        width=96,
        height=30,
        fg_color=T.ACCENT if accent else T.BG_PANEL,
        hover_color=T.ACCENT_HOVER,
        border_width=1,
        border_color=T.BORDER,
        text_color=T.ACCENT_ON if accent else T.TEXT,
        command=command,
    ).pack(side="left", padx=(0, 6))


def _update_password(app: AutoTradingApp, registry, account) -> None:
    """密码只进本机 DPAPI 密钥库，界面不回显明文。"""
    from src.utils.secret_store import SecretStoreUnavailable, default_store

    value = simpledialog.askstring(
        "更新密码",
        f"给 {account.display_name} 设置新密码：\n（只保存在本机加密文件里）",
        parent=app,
        show="*",
    )
    if value is None or not value:
        return
    ref = account.password_ref
    per_account = f"secret://ctp/{account.environment}/{account.account_id}/password"
    if not ref.startswith(f"secret://ctp/{account.environment}/{account.account_id}/"):
        # 旧账号共用环境级引用；第一次改密码就换成这个账号自己的引用。
        ref = per_account
        account.password_ref = ref
    try:
        default_store().set(ref, value)
    except SecretStoreUnavailable as exc:
        messagebox.showerror("不能保存密码", str(exc), parent=app)
        return
    except (OSError, ValueError) as exc:
        messagebox.showerror("保存密码失败", str(exc), parent=app)
        return
    registry.save(app.root_dir)
    _apply_registry(app, registry)
    _sync_projection(app)
    app._append_log(f"已更新 {account.display_name} 的密码（本机加密）")
    _refresh_accounts(app)


def _sync_projection(app: AutoTradingApp) -> None:
    """YAML 仍是权威；已有 SQLite 账本时顺手投影一份，失败不影响账号与交易。"""
    db = app.root_dir / "data" / "trading.db"
    if not db.exists():
        return
    try:
        from src.account.sqlite_migrate import migrate

        migrate(app.root_dir, db)
    except Exception as exc:  # noqa: BLE001
        app._append_log(f"账号 SQLite 投影失败（不影响交易）：{exc}")


def _persist_accounts(
    app: AutoTradingApp, registry, action: str, account_id: str, detail: str = ""
) -> bool:
    """写注册表 + 审计 + 刷新界面。返回是否成功。"""
    from src.account.registry import record_audit

    try:
        registry.save(app.root_dir)
    except OSError as exc:
        messagebox.showerror("保存失败", f"账号注册表写不进去：\n{exc}", parent=app)
        return False
    record_audit(app.root_dir, action, account_id=account_id, detail=detail)
    _apply_registry(app, registry)
    _sync_projection(app)
    _refresh_accounts(app)
    app._append_log(f"账号注册表已更新：{action} {account_id}")
    return True


def _template_from_ui(app: AutoTradingApp, name: str, environment: str):
    """把「连接」页当前输入框复制成一条新连接。"""
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
        profile_id="",
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
    old_conn = account.profile_id
    choices = getattr(app, "_account_connection_choices", {})
    new_conn = choices.get(app.account_connection_var.get(), account.profile_id)
    try:
        limit = int(float(app.account_limit_var.get() or 0))
    except (TypeError, ValueError):
        limit = account.max_order_volume
    account.display_name = app.account_name_var.get().strip() or old_name
    account.investor_id = app.account_investor_var.get().strip()
    account.risk_policy_id = app.account_risk_var.get().strip() or account.risk_policy_id
    account.max_order_volume = max(0, limit)
    account.notes = app.account_notes_var.get().strip()
    if account.channel == "ctp":
        account.profile_id = new_conn
    if account.environment == "live" and account.investor_id != old_investor:
        if not _confirm_live(app, account.investor_id):
            account.display_name, account.investor_id = old_name, old_investor
            account.profile_id = old_conn
            return
    account.touch()
    problems = registry.validate()
    if problems:
        account.display_name, account.investor_id = old_name, old_investor
        account.profile_id = old_conn
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
            record_audit(
                app.root_dir,
                "env_change",
                account_id=account.account_id,
                detail="新建真实账号，已确认后四位",
            )
        app.account_selected_id = account.account_id
        _refresh_accounts(app)
        app._append_log(f"已新增账号 {name}（{account.account_id}），账本 {account.book_id}")


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
            record_audit(
                app.root_dir,
                "env_change",
                account_id=account.account_id,
                detail="复制真实账号，已确认后四位",
            )
        app.account_selected_id = account.account_id
        _refresh_accounts(app)


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


def _switch_to_selected(app: AutoTradingApp) -> None:
    """把选中账号设为当前使用：执行通道、柜台、账本一起切换。"""
    registry, account = _selected_account(app)
    if registry is None or account is None:
        return
    from src.account.session import set_active_account
    from src.utils.config_loader import load_secrets

    try:
        session = set_active_account(app.root_dir, app.cfg, account.account_id, load_secrets())
    except (KeyError, ValueError) as exc:
        messagebox.showerror("不能切换账号", str(exc), parent=app)
        return
    app._append_log(f"当前交易账号：{session.summary}")
    if hasattr(app, "exec_var"):
        app.exec_var.set(session.channel)
    if hasattr(app, "ctp_kind_var") and session.channel == "ctp":
        from src.trader.ctp_logic import ctp_kind_label

        app.ctp_kind_var.set(ctp_kind_label(session.environment))
    if session.channel == "gui" and hasattr(app, "user_var"):
        app.user_var.set(session.investor_id)
    fill = getattr(app, "_fill_ctp_profile_vars", None)
    if callable(fill):
        fill()
    refresh = getattr(app, "_refresh_status_cards", None)
    if callable(refresh):
        refresh()
    _refresh_accounts(app)


def _pack_connection_section(app: AutoTradingApp, parent: ctk.CTkFrame) -> None:
    """连接 = 期货公司接入点：前置、BrokerID、AppID、超时。资金号在账号上。"""
    _note(
        parent,
        "连接只描述「怎么连柜台」，不含资金号和密码。换期货公司 = 新增连接 + 账号改绑，"
        "同一条连接可以被多个账号引用。",
    )
    app.ctp_profile_menu = app._option_row(
        parent,
        "连接",
        app.ctp_profile_var,
        getattr(app, "ctp_profile_choices", ["default"]),
        width=240,
    )
    ctk.CTkLabel(
        parent,
        textvariable=app.ctp_profile_note_var,
        font=T.FONT_SMALL,
        text_color=T.WARN,
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(0, 4))
    app._entry_row(
        parent,
        "档案交易前置",
        app.ctp_profile_td_var,
        hint="可填多台，用分号隔开。连接时全部登记，断线由柜台自动换",
        width=240,
    )
    app._entry_row(
        parent,
        "档案行情前置",
        app.ctp_profile_md_var,
        hint="行情也可填多台备用，端口通常和交易前置不同",
        width=240,
    )
    app._entry_row(
        parent,
        "档案BrokerID",
        app.ctp_profile_broker_var,
        hint="柜台给的 BrokerID，例如 8080 / 9999",
        width=240,
    )
    app._entry_row(
        parent,
        "档案资金账号",
        app.ctp_profile_user_var,
        hint="旧档案兼容字段；新账号请到「账号」里填资金账号",
        width=240,
    )
    app._entry_row(
        parent,
        "档案密码变量",
        app.ctp_profile_pwd_env_var,
        hint="环境变量名，默认 CTP_PASSWORD；真实档案默认 CTP_LIVE_PASSWORD",
        width=240,
    )
    app._entry_row(
        parent,
        "档案认证码变量",
        app.ctp_profile_auth_env_var,
        hint="环境变量名，默认 CTP_AUTH_CODE；该柜台不需要认证码时留空",
        width=240,
    )
    ctk.CTkButton(
        parent,
        text="测试交易前置连通",
        width=180,
        command=app._test_ctp_front,
        fg_color=T.ACCENT,
        hover_color=T.ACCENT_HOVER,
        text_color=T.TEXT,
    ).pack(anchor="w", pady=(2, 8))


def _pack_gui_login_section(app: AutoTradingApp, scroll: ctk.CTkFrame) -> None:
    """同花顺 GUI 通道的登录参数（不是 CTP 连接）。"""
    app._section(scroll, "同花顺登录（GUI 通道）")
    live = app._card(scroll)
    app._entry_row(live, "期货公司", app.broker_var)
    app._entry_row(live, "通道名称", app.channel_var, hint="如：华泰期货_CTP主席")
    app._entry_row(
        live,
        "资金账号",
        app.user_var,
        hint="同花顺登录号；选 GUI 账号时以「账号」里的资金账号为准",
    )
    app.pwd_entry = app._password_row(live, "交易密码", app.password_var, app._show_password)
    _note(
        live,
        "只在执行通道为 GUI 时使用。CTP 账号的密码请到「账号」详情里更新，只存本机加密文件。",
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


def build_credentials_tab(app: AutoTradingApp, tab: ctk.CTkFrame) -> None:
    """交易账号页：当前账号 + 账号/连接两个子区。保存按钮与交易配置同一套写盘。"""
    scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=8, pady=8)

    ctk.CTkLabel(scroll, text="交易账号", font=T.FONT_TITLE, text_color=T.TEXT).pack(
        anchor="w", padx=4, pady=(4, 2)
    )
    _note(
        scroll,
        "这一页管「用哪个号、走哪条通道、连哪台柜台」。策略、品种、开仓止损在「交易配置」；"
        "行情数据源在「数据管理」。密码只保存在本机密钥文件（Windows 凭据加密），不会打进 exe。",
    )

    # —— 当前使用：唯一运行入口 ——
    app.current_account_var = ctk.StringVar(value="正在读取…")
    app.current_note_var = ctk.StringVar(value="")
    app._section(scroll, "当前使用")
    current = app._card(scroll)
    ctk.CTkLabel(
        current,
        textvariable=app.current_account_var,
        font=T.FONT_BODY,
        text_color=T.TEXT,
        wraplength=560,
        justify="left",
    ).pack(anchor="w", pady=(2, 2))
    ctk.CTkLabel(
        current,
        textvariable=app.current_note_var,
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=560,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))
    current_actions = ctk.CTkFrame(current, fg_color="transparent")
    current_actions.pack(fill="x")
    _small_button(current_actions, "设为当前账号", lambda: _switch_to_selected(app), accent=True)
    _small_button(current_actions, "测试连接", lambda: app._test_ctp_front())

    # —— 账号 / 连接 ——
    app._section(scroll, "账号与连接")
    area = app._card(scroll)
    sub = ctk.CTkTabview(
        area,
        fg_color="transparent",
        segmented_button_selected_color=T.ACCENT,
        segmented_button_selected_hover_color=T.ACCENT_HOVER,
    )
    sub.pack(fill="both", expand=True, pady=(4, 8))
    account_tab = sub.add("账号")
    connection_tab = sub.add("连接")

    split = ctk.CTkFrame(account_tab, fg_color="transparent")
    split.pack(fill="both", expand=True)
    left = ctk.CTkFrame(split, fg_color="transparent", width=250)
    left.pack(side="left", fill="y", padx=(0, 10))
    app.account_list_frame = ctk.CTkScrollableFrame(left, fg_color="transparent", height=330)
    app.account_list_frame.pack(fill="both", expand=True)
    new_row = ctk.CTkFrame(left, fg_color="transparent")
    new_row.pack(fill="x", pady=(6, 0))
    _small_button(new_row, "新增仿真", lambda: _new_account(app, "sim"))
    _small_button(new_row, "新增真实", lambda: _new_account(app, "live"))
    right = ctk.CTkFrame(split, fg_color="transparent")
    right.pack(side="left", fill="both", expand=True)
    app.account_detail_frame = right
    for name in (
        "account_selected_id",
        "account_name_var",
        "account_investor_var",
        "account_risk_var",
        "account_limit_var",
        "account_notes_var",
        "account_connection_var",
    ):
        if not hasattr(app, name):
            setattr(
                app,
                name,
                "" if name == "account_selected_id" else ctk.StringVar(value=""),
            )
    app._account_connection_choices = {}

    _pack_connection_section(app, connection_tab)
    _pack_gui_login_section(app, scroll)

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

    _refresh_accounts(app)
