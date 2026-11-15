"""
指标库可视化配置面板

功能：
    策略编辑器下方的「指标库 / 计算公式」面板：选指标类型 → 填参数（周期、快慢线、
    标准差等）→ 实时看到代入当前参数的计算公式和 YAML 片段 → 一键插进编辑框的
    indicators: 段。

原理：
    数据与拼装逻辑都在 rule_dsl（indicator_snippet / indicator_formula_text /
    validate_indicator / insert_indicator_line），这里只负责控件和交互，不重复实现
    规则校验，保证「面板里能通过」和「保存时能通过」是同一套判定。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from src.strategy.rule_dsl import (
    INDICATOR_LIBRARY,
    RuleConfigError,
    indicator_defaults,
    indicator_formula_text,
    indicator_name_hint,
    indicator_param_type,
    indicator_snippet,
    insert_indicator_line,
    validate_expr_formula,
    validate_indicator,
)
from src.ui import theme as T

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp

_KIND_ITEMS = [(str(item["kind"]), str(item["label"])) for item in INDICATOR_LIBRARY]
_KIND_LABELS = {kind: f"{label}（{kind}）" for kind, label in _KIND_ITEMS}
_LABEL_TO_KIND = {label: kind for kind, label in _KIND_LABELS.items()}

_PARAM_LABELS = {
    "period": "周期",
    "atr_period": "ATR周期",
    "fast": "快线",
    "slow": "慢线",
    "signal": "信号线",
    "std": "标准差倍数",
    "field": "取值字段",
    "source": "取值字段",
    "smooth": "平滑根数",
    "d": "D周期",
    "mult": "ATR倍数",
    "af_start": "起始AF",
    "af_step": "AF步长",
    "af_max": "AF上限",
    "formula": "公式",
}

_TOKEN_INSERT_TEXT = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}


class IndicatorBuilder:
    """指标库面板：选类型、填参数、看公式、插入编辑框。"""

    def __init__(self, app: AutoTradingApp, parent: ctk.CTkFrame) -> None:
        self.app = app
        self.parent = parent
        self.kind_var = ctk.StringVar(value=_KIND_LABELS[_KIND_ITEMS[0][0]])
        self.name_var = ctk.StringVar(value="")
        self.formula_var = ctk.StringVar(value="")
        self.snippet_var = ctk.StringVar(value="")
        self.status_var = ctk.StringVar(value="")
        self.param_vars: dict[str, ctk.StringVar] = {}
        self._syncing = False
        self._body_on = False
        self._build()
        self._load_kind(_KIND_ITEMS[0][0])

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        head = ctk.CTkFrame(self.parent, fg_color="transparent")
        head.pack(fill="x")
        self.toggle_btn = ctk.CTkButton(
            head,
            text="▶ 指标库与计算公式（可视化配置）",
            width=240,
            anchor="w",
            command=self.toggle,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.TEXT,
        )
        self.toggle_btn.pack(side="left")
        ctk.CTkLabel(
            head,
            text="选指标填参数，自动生成公式和 YAML，点「插入」即写进编辑框",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left", padx=(10, 0))

        self.body = ctk.CTkScrollableFrame(
            self.parent,
            height=330,
            fg_color=T.BG_ELEVATED,
            border_width=1,
            border_color=T.BORDER,
        )

        pick = ctk.CTkFrame(self.body, fg_color="transparent")
        pick.pack(fill="x", padx=6, pady=(6, 2))
        ctk.CTkLabel(
            pick, text="指标类型", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=64, anchor="w"
        ).pack(side="left")
        self.kind_menu = ctk.CTkOptionMenu(
            pick,
            values=list(_LABEL_TO_KIND),
            variable=self.kind_var,
            width=200,
            fg_color=T.BG_ELEVATED,
            button_color=T.ACCENT,
            button_hover_color=T.ACCENT_HOVER,
            dropdown_fg_color=T.BG_PANEL,
            text_color=T.TEXT,
            dropdown_text_color=T.TEXT,
        )
        self.kind_menu.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            pick, text="名字", font=T.FONT_BODY, text_color=T.TEXT_DIM, anchor="w"
        ).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(
            pick,
            textvariable=self.name_var,
            width=92,
            fg_color=T.BG_PANEL,
            border_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left")

        self.param_host = ctk.CTkFrame(self.body, fg_color="transparent")
        self.param_host.pack(fill="x", padx=6, pady=(2, 4))

        ctk.CTkLabel(
            self.body,
            text="计算公式（按当前参数代入）",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            self.body,
            textvariable=self.formula_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT,
            wraplength=440,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=8)

        ctk.CTkLabel(
            self.body,
            text="写入 indicators: 的内容",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(
            self.body,
            textvariable=self.snippet_var,
            font=T.FONT_MONO,
            text_color=T.TEXT,
            wraplength=440,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=8)

        actions = ctk.CTkFrame(self.body, fg_color="transparent")
        actions.pack(fill="x", padx=6, pady=(6, 2))
        ctk.CTkButton(
            actions,
            text="自动起名",
            width=72,
            command=self._suggest_name,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="校验",
            width=64,
            command=self.validate,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="插入到 indicators",
            width=132,
            command=self.insert,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
        ).pack(side="left")
        self.status_label = ctk.CTkLabel(
            self.body,
            textvariable=self.status_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=450,
            justify="left",
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=8, pady=(0, 6))

        self.kind_var.trace_add("write", lambda *_: self._on_kind_var())
        self.name_var.trace_add("write", lambda *_: self._refresh_preview())

    def toggle(self) -> None:
        """展开 / 收起指标库，默认收起，避免把编辑框挤出可视区。"""
        if self._body_on:
            self.body.pack_forget()
            self.toggle_btn.configure(text="▶ 指标库与计算公式（可视化配置）")
            self._body_on = False
        else:
            self.body.pack(fill="x", pady=(4, 0))
            self.toggle_btn.configure(text="▼ 指标库与计算公式（可视化配置）")
            self._body_on = True

    # ------------------------------------------------------------ 参数区
    def _on_kind_var(self) -> None:
        if not self._syncing:
            self._load_kind(self.current_kind())

    def current_kind(self) -> str:
        return _LABEL_TO_KIND.get(str(self.kind_var.get()), _KIND_ITEMS[0][0])

    def _load_kind(self, kind: str) -> None:
        spec = next((item for item in INDICATOR_LIBRARY if item["kind"] == kind), None)
        if spec is None:
            return
        for child in self.param_host.winfo_children():
            child.destroy()
        self.param_vars = {}
        defaults = indicator_defaults(kind)
        self.formula_entry: ctk.CTkEntry | None = None
        for param in spec.get("params", []):
            name = str(param["name"])
            var = ctk.StringVar(value=str(defaults.get(name, "")))
            self.param_vars[name] = var
            row = ctk.CTkFrame(self.param_host, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row,
                text=f"{_PARAM_LABELS.get(name, name)} {name}",
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                width=84,
                anchor="w",
            ).pack(side="left")
            entry = ctk.CTkEntry(
                row,
                textvariable=var,
                width=170 if name == "formula" else 72,
                fg_color=T.BG_PANEL,
                border_color=T.BORDER,
                text_color=T.TEXT,
            )
            entry.pack(side="left")
            if name == "formula":
                self.formula_entry = entry
            ctk.CTkLabel(
                row,
                text=str(param.get("desc") or ""),
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                wraplength=200,
                justify="left",
                anchor="w",
            ).pack(side="left", padx=(8, 0))
            var.trace_add("write", lambda *_: self._refresh_preview())
        if kind == "expr":
            self._build_token_row()
        self._syncing = True
        self.name_var.set(self._default_name(kind))
        self._syncing = False
        self._refresh_preview()
        self.status_var.set("")
        self.status_label.configure(text_color=T.TEXT_DIM)

    def _build_token_row(self) -> None:
        """expr 专用：字段、函数、已有指标各占一行，点一下就插进公式输入框。"""
        groups: list[tuple[str, list[str]]] = [
            ("行情字段", list(_TOKEN_INSERT_TEXT)),
            ("函数", ["max(", "min(", "abs(", "avg(", "std(", "sqrt(", "clip("]),
        ]
        defined = self._defined_names()
        if defined:
            groups.append(("已有指标", defined[:5]))
        for title, tokens in groups:
            row = ctk.CTkFrame(self.param_host, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(
                row,
                text=title,
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                width=84,
                anchor="w",
            ).pack(side="left")
            for token in tokens:
                ctk.CTkButton(
                    row,
                    text=token,
                    width=max(40, 7 * len(token) + 14),
                    height=22,
                    font=T.FONT_SMALL,
                    command=lambda t=token: self._insert_token(t),
                    fg_color=T.BG_ELEVATED,
                    hover_color=T.BORDER,
                    text_color=T.TEXT,
                ).pack(side="left", padx=2)

    def _insert_token(self, token: str) -> None:
        try:
            self.formula_entry.insert("insert", token)
            self.formula_entry.focus_set()
        except Exception:  # noqa: BLE001
            var = self.param_vars.get("formula")
            if var is not None:
                var.set(var.get() + token)

    def _default_name(self, kind: str) -> str:
        hint = indicator_name_hint(kind)
        defaults = indicator_defaults(kind)
        period = defaults.get("period")
        if period:
            return f"{hint}{period}"
        return hint

    def _suggest_name(self) -> None:
        """按编辑框里已有的指标名避重，生成 ma20、ma20_2 这样的名字。"""
        kind = self.current_kind()
        base = self._default_name(kind)
        used = set(self._defined_names())
        name = base
        index = 2
        while name in used:
            name = f"{base}_{index}"
            index += 1
        self.name_var.set(name)

    # ------------------------------------------------------------ 预览与校验
    def _params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for name, var in self.param_vars.items():
            raw = str(var.get()).strip()
            if not raw:
                continue
            ptype = indicator_param_type(name)
            try:
                if ptype == "int":
                    params[name] = int(raw)
                elif ptype == "float":
                    params[name] = float(raw)
                else:
                    params[name] = raw
            except ValueError:
                # 原样传下去，让规则校验给出「必须是整数」这类明确提示。
                params[name] = raw
        return params

    def current_name(self) -> str:
        return self.name_var.get().strip()

    def _refresh_preview(self) -> None:
        kind = self.current_kind()
        params = self._params()
        name = self.current_name() or self._default_name(kind)
        try:
            self.formula_var.set(indicator_formula_text(kind, params))
        except Exception:  # noqa: BLE001
            self.formula_var.set("")
        self.snippet_var.set(indicator_snippet(name, kind, params))

    def _defined_names(self) -> list[str]:
        """从编辑框的 indicators: 段里扫出已定义的指标名。"""
        try:
            text = self.app.rule_editor.get("1.0", "end")
        except Exception:  # noqa: BLE001
            return []
        names: list[str] = []
        in_block = False
        for line in text.splitlines():
            if line.strip() == "indicators:":
                in_block = True
                continue
            if not in_block:
                continue
            if not line.strip():
                continue
            if line[:1] not in (" ", "\t"):
                break
            match = re.match(r"\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if match:
                names.append(match.group(1))
        return names

    def validate(self) -> bool:
        kind = self.current_kind()
        name = self.current_name()
        if not name:
            self._say("先起个指标名，比如 ma20 / atr14。", T.WARN)
            return False
        params = self._params()
        try:
            if kind == "expr":
                formula = str(params.get("formula") or "").strip()
                if not formula:
                    self._say("expr 需要先在「公式」里写内容。", T.WARN)
                    return False
                validate_expr_formula(formula, self._defined_names())
                validate_indicator(name, kind, {})
            else:
                validate_indicator(name, kind, {k: v for k, v in params.items() if k != "formula"})
        except RuleConfigError as exc:
            self._say(f"校验没通过：{exc}", T.DANGER)
            return False
        self._say(f"{name} 的写法没问题，可以插入。", T.OK)
        return True

    def insert(self) -> None:
        if not self.validate():
            return
        kind = self.current_kind()
        name = self.current_name()
        try:
            text = self.app.rule_editor.get("1.0", "end").rstrip("\n")
        except Exception:  # noqa: BLE001
            self._say("取不到编辑框内容，先点开策略编辑器试试。", T.DANGER)
            return
        if name in self._defined_names():
            self._say(f"编辑框里已经有叫 {name} 的指标了，换个名字或点「自动起名」。", T.WARN)
            return
        snippet = indicator_snippet(name, kind, self._params())
        new_text = insert_indicator_line(text, snippet)
        self.app.rule_editor.delete("1.0", "end")
        self.app.rule_editor.insert("1.0", new_text)
        self._say(f"已插入 {name}，接着在 entry / exit 条件里引用它。", T.OK)

    def _say(self, text: str, color: str) -> None:
        self.status_var.set(text)
        try:
            self.status_label.configure(text_color=color)
        except Exception:  # noqa: BLE001
            pass


def build_indicator_builder(app: AutoTradingApp, parent: ctk.CTkFrame) -> IndicatorBuilder:
    """在策略编辑器下方挂一块指标库面板，并把实例存到 app 上便于测试/其他面板引用。"""
    builder = IndicatorBuilder(app, parent)
    app.rule_indicator_builder = builder
    return builder
