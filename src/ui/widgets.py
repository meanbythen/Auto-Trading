"""Reusable CustomTkinter rows, cards, period selectors, and file helpers."""

from __future__ import annotations

from loguru import logger
from src.data.periods import PERIOD_MENU
from src.data.periods import ui_parts
from src.ui import theme as T
import customtkinter as ctk
import os
import subprocess
import sys


class WidgetsMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _section(self, parent: ctk.CTkFrame, title: str) -> None:
        ctk.CTkLabel(parent, text=title, font=T.FONT_TITLE, text_color=T.ACCENT).pack(anchor="w", pady=(14, 6))


    def _card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=T.BG_ELEVATED, corner_radius=12, border_width=1, border_color=T.BORDER)
        card.pack(fill="x", pady=(0, 4))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=12)
        return inner


    def _option(
        self,
        parent: ctk.CTkFrame,
        var: ctk.StringVar,
        values: list[str],
        width: int = 220,
    ) -> ctk.CTkOptionMenu:
        """创建下拉菜单；调用方必须把 parent 设为「当前这一行」的 Frame，再 pack 进去。"""
        return ctk.CTkOptionMenu(
            parent,
            values=values,
            variable=var,
            width=width,
            fg_color=T.BG_ELEVATED,
            button_color=T.ACCENT,
            button_hover_color=T.ACCENT_HOVER,
            dropdown_fg_color=T.BG_PANEL,
            text_color=T.TEXT,                 # 框内已选文字：浅色，深蓝底上清晰
            dropdown_text_color=T.TEXT,        # 展开列表里的文字
        )


    def _option_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        values: list[str],
        width: int = 220,
    ) -> None:
        """
        一行：左侧固定宽标签 + 右侧下拉框。
        原理：控件必须挂在同一 row 上再 pack(side=left)，
        若挂在外层 parent 再 side=right，会挤乱整列（楼梯错位、输入框变细条）。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        menu = self._option(row, var, values, width=width)
        menu.pack(side="left")
        return menu


    def _timeframe_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        choice_var: ctk.StringVar,
        custom_var: ctk.StringVar,
        width: int = 160,
    ) -> None:
        """
        周期一行：下拉选常用档，右边小框留给「自定义」。

        下拉里有 1 到 60 分钟、日线、周线。选自定义时，小框里填分钟数，120 就是 2 小时。
        没选自定义时，小框里的字不会被用上。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        menu = self._option(row, choice_var, PERIOD_MENU, width=width)
        menu.pack(side="left")
        ent = ctk.CTkEntry(
            row,
            textvariable=custom_var,
            width=70,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
            placeholder_text="120",
        )
        ent.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            row, text="自定义填分钟", font=T.FONT_SMALL, text_color=T.TEXT_DIM
        ).pack(side="left", padx=6)


    def _period_code(self, choice_var: ctk.StringVar, custom_var: ctk.StringVar) -> str:
        """下拉框收成配置里的周期代码。日线是 D，周线是 W，分钟是数字。"""
        from src.data.periods import canonical_period

        return canonical_period(choice_var.get(), custom_var.get())


    def _set_period_vars(self, code: str, choice_var: ctk.StringVar, custom_var: ctk.StringVar) -> None:
        """把配置里的 15 或 D 填回下拉框。自定义分钟会选中「自定义」并写进小框。"""
        label, custom = ui_parts(str(code))
        choice_var.set(label)
        custom_var.set(custom)


    def _entry_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        hint: str = "",
        width: int = 280,
        readonly: bool = False,
        hint_wrap: int | None = None,
    ) -> None:
        """一行：标签 + 输入框 + 可选灰色提示；三者同属一行 Frame，宽度固定不被挤扁。

        hint_wrap：说明文字的换行宽度。策略参数框较窄时传入，避免一行把右侧撑出大片空白。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        if readonly:
            # 只显示数字。余额由账户分析改，配置页不能在这里填另一个数。
            ctk.CTkLabel(
                row, textvariable=var, font=T.FONT_BODY, text_color=T.TEXT, anchor="w", width=width
            ).pack(side="left")
        else:
            # 可编辑时才放输入框。只读行上面已经用标签显示，不能再排一个能打字的框。
            ent = ctk.CTkEntry(
                row,
                textvariable=var,
                width=width,
                fg_color=T.BG_ELEVATED,
                border_color=T.BORDER,
                text_color=T.TEXT,  # 输入内容用浅色字，避免看不清
            )
            ent.pack(side="left")
        if hint:
            hint_lbl = ctk.CTkLabel(
                row,
                text=hint,
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                justify="left",
                anchor="w",
            )
            if hint_wrap and hint_wrap > 0:
                hint_lbl.configure(wraplength=int(hint_wrap))
            hint_lbl.pack(side="left", padx=8)


    def _date_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        hint: str = "",
        allow_empty: bool = True,
    ) -> None:
        """一行：标签 + 日历选日期。选完仍是 2026-01-01 这种字，后面逻辑不用改。"""
        from src.ui.datetime_pickers import DateField

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        DateField(row, var, allow_empty=allow_empty).frame.pack(side="left")
        if hint:
            ctk.CTkLabel(
                row,
                text=hint,
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                justify="left",
                anchor="w",
            ).pack(side="left", padx=8)


    def _pick_switch_row(self, parent: ctk.CTkFrame, label: str, var: ctk.BooleanVar) -> None:
        """一行说明加一个开关。交叉测试页的基准开关、清仓和针确认都用这一行。"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(row, text="", variable=var, progress_color=T.OK, fg_color=T.BORDER).pack(side="left")


    def _set_result_text(self, box: ctk.CTkTextbox, text: str) -> None:
        box.delete("1.0", "end")
        box.insert("end", text)


    def _set_readonly_box(self, box: ctk.CTkTextbox, text: str) -> None:
        """先打开再写入，写完锁住，避免人在这里改了却没存进配置。"""
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")


    def _open_local_file(self, path) -> bool:
        """用系统默认程序打开刚写好的文件。成功返回 True。"""
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("打不开文件 {}: {}", path, exc)
            return False

    def _export_fail_text(self, path: str, exc: BaseException) -> str:
        """导出失败时给人看的话。Excel 占用时不甩 Errno 13。"""
        from src.export.book import is_excel_locked, locked_file_hint

        if is_excel_locked(exc):
            return locked_file_hint(path)
        return str(exc)


    def _export_saved_note(self, chosen: str, written) -> str:
        """写到另一份文件时，弹窗末尾补一句。"""
        from src.export.book import export_lock_note

        return export_lock_note(chosen, written)

