"""
日期日历、小时分钟选择器。

功能：界面上不用手打 2026-09-22 或 10:50，点日历选日期，下拉选小时和分钟。
原理：选中的结果仍写成原来的字符串，后面回测、删除、查看 K 线不用改算法。
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Callable

import customtkinter as ctk

from src.ui import theme as T

# 日历从周一开始，和国内看周几的习惯一致。
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")
# 小时 00–23、分钟 00–59，夜盘 21:00 也能选到。
HOUR_CHOICES = [f"{h:02d}" for h in range(24)]
MINUTE_CHOICES = [f"{m:02d}" for m in range(60)]
EMPTY_TIME = "不选"


def parse_date_text(text: str) -> date | None:
    """把 2026-09-22 收成日期；空着或看不懂就当没选。"""
    raw = str(text or "").strip().replace("/", "-").replace(".", "-")
    if not raw:
        return None
    raw = raw[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_time_text(text: str) -> tuple[int, int] | None:
    """把 10:50 或 10:50:00 收成小时和分钟。"""
    raw = str(text or "").strip()
    if not raw or raw == EMPTY_TIME:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour, minute


def format_date(day: date | None) -> str:
    """日期写成 2026-09-22；没选就是空字符串。"""
    if day is None:
        return ""
    return day.isoformat()


def format_time(hour: int | None, minute: int | None) -> str:
    """时间写成 10:50。"""
    if hour is None or minute is None:
        return ""
    return f"{hour:02d}:{minute:02d}"


def combine_datetime(date_text: str, time_text: str) -> str:
    """
    删除本地 K 线用的起止：只有日期、或日期加时间。

    没选日期就是整段都算；选了日期没选时间，按那天 00:00 起到 23:59。
    """
    day = parse_date_text(date_text)
    if day is None:
        return ""
    clock = parse_time_text(time_text)
    if clock is None:
        return format_date(day)
    hour, minute = clock
    return f"{format_date(day)} {hour:02d}:{minute:02d}:00"


def open_calendar(
    parent: ctk.CTkBaseClass,
    *,
    initial: date | None = None,
    allow_empty: bool = True,
    on_pick: Callable[[date | None], None],
) -> ctk.CTkToplevel:
    """弹出一个月历。点某一天就填进去并关掉。"""
    today = date.today()
    shown = initial or today
    win = ctk.CTkToplevel(parent.winfo_toplevel())
    win.title("选择日期")
    win.geometry("340x390")
    win.minsize(320, 360)
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    try:
        win.grab_set()
    except Exception:  # noqa: BLE001
        pass

    year_var = ctk.StringVar(value=str(shown.year))
    month_var = ctk.StringVar(value=str(shown.month))
    years = [str(y) for y in range(2015, today.year + 2)]
    months = [str(m) for m in range(1, 13)]

    head = ctk.CTkFrame(win, fg_color="transparent")
    head.pack(fill="x", padx=12, pady=(12, 6))
    ctk.CTkLabel(head, text="年", text_color=T.TEXT_DIM).pack(side="left")
    ctk.CTkOptionMenu(head, variable=year_var, values=years, width=90).pack(side="left", padx=(6, 12))
    ctk.CTkLabel(head, text="月", text_color=T.TEXT_DIM).pack(side="left")
    ctk.CTkOptionMenu(head, variable=month_var, values=months, width=70).pack(side="left", padx=6)

    grid = ctk.CTkFrame(win, fg_color="transparent")
    grid.pack(fill="both", expand=True, padx=12, pady=4)

    def _draw() -> None:
        for child in grid.winfo_children():
            child.destroy()
        for col, name in enumerate(_WEEKDAYS):
            ctk.CTkLabel(grid, text=name, width=36, text_color=T.TEXT_DIM).grid(
                row=0, column=col, padx=1, pady=1
            )
        year = int(year_var.get())
        month = int(month_var.get())
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
        picked = initial

        def _choose(day_n: int) -> None:
            on_pick(date(year, month, day_n))
            win.destroy()

        for r, week in enumerate(weeks, start=1):
            for c, day_n in enumerate(week):
                if day_n == 0:
                    ctk.CTkLabel(grid, text="", width=36, height=28).grid(row=r, column=c, padx=1, pady=1)
                    continue
                this = date(year, month, day_n)
                is_pick = picked is not None and this == picked
                is_today = this == today
                fg = T.ACCENT if is_pick else (T.BG_ELEVATED if not is_today else T.BORDER)
                ctk.CTkButton(
                    grid,
                    text=str(day_n),
                    width=36,
                    height=28,
                    corner_radius=4,
                    fg_color=fg,
                    hover_color=T.ACCENT_HOVER,
                    text_color=T.TEXT,
                    command=lambda n=day_n: _choose(n),
                ).grid(row=r, column=c, padx=1, pady=1)

    year_var.trace_add("write", lambda *_: _draw())
    month_var.trace_add("write", lambda *_: _draw())
    _draw()

    foot = ctk.CTkFrame(win, fg_color="transparent")
    foot.pack(fill="x", padx=12, pady=(4, 12))

    def _today() -> None:
        on_pick(today)
        win.destroy()

    def _clear() -> None:
        on_pick(None)
        win.destroy()

    ctk.CTkButton(foot, text="今天", width=80, command=_today).pack(side="left")
    if allow_empty:
        ctk.CTkButton(
            foot, text="清空", width=80, fg_color=T.BG_ELEVATED, command=_clear
        ).pack(side="left", padx=8)
    ctk.CTkButton(
        foot, text="取消", width=80, fg_color=T.BG_ELEVATED, command=win.destroy
    ).pack(side="right")
    return win


class DateField:
    """一行：当前日期文字 +「选日期」。点开日历，选完写回 StringVar。"""

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        variable: ctk.StringVar,
        *,
        allow_empty: bool = True,
    ) -> None:
        self.variable = variable
        self.allow_empty = allow_empty
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self._label = ctk.CTkLabel(
            self.frame,
            text="",
            width=110,
            anchor="w",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        )
        self._label.pack(side="left")
        ctk.CTkButton(
            self.frame,
            text="选日期",
            width=72,
            height=28,
            command=self._open,
        ).pack(side="left", padx=(8, 0))
        self._sync()
        self.variable.trace_add("write", lambda *_: self._sync())

    def get(self) -> str:
        return format_date(parse_date_text(self.variable.get()))

    def _sync(self) -> None:
        day = parse_date_text(self.variable.get())
        self._label.configure(text=format_date(day) if day else "未选择")

    def _open(self) -> None:
        open_calendar(
            self.frame,
            initial=parse_date_text(self.variable.get()),
            allow_empty=self.allow_empty,
            on_pick=lambda day: self.variable.set(format_date(day)),
        )


class TimeField:
    """小时、分钟两个下拉。可选「不选」，给日线或删除时留空用。"""

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        variable: ctk.StringVar,
        *,
        allow_empty: bool = True,
    ) -> None:
        self.variable = variable
        self.allow_empty = allow_empty
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        hours = ([EMPTY_TIME] + HOUR_CHOICES) if allow_empty else HOUR_CHOICES
        minutes = ([EMPTY_TIME] + MINUTE_CHOICES) if allow_empty else MINUTE_CHOICES
        parsed = parse_time_text(variable.get())
        if parsed is None and not allow_empty:
            parsed = (0, 0)
        hour_text = f"{parsed[0]:02d}" if parsed else EMPTY_TIME
        minute_text = f"{parsed[1]:02d}" if parsed else EMPTY_TIME
        if hour_text not in hours:
            hour_text = hours[0]
        if minute_text not in minutes:
            minute_text = minutes[0]
        self.hour_var = ctk.StringVar(value=hour_text)
        self.minute_var = ctk.StringVar(value=minute_text)
        ctk.CTkLabel(self.frame, text="时", text_color=T.TEXT_DIM).pack(side="left")
        ctk.CTkOptionMenu(self.frame, variable=self.hour_var, values=hours, width=78).pack(
            side="left", padx=(4, 8)
        )
        ctk.CTkLabel(self.frame, text="分", text_color=T.TEXT_DIM).pack(side="left")
        ctk.CTkOptionMenu(self.frame, variable=self.minute_var, values=minutes, width=78).pack(
            side="left", padx=(4, 0)
        )
        self.hour_var.trace_add("write", lambda *_: self._write())
        self.minute_var.trace_add("write", lambda *_: self._write())
        self._write()

    def get(self) -> str:
        return self.variable.get().strip()

    def _write(self) -> None:
        hour = self.hour_var.get()
        minute = self.minute_var.get()
        if hour == EMPTY_TIME or minute == EMPTY_TIME:
            if self.allow_empty:
                self.variable.set("")
                return
            hour = "00" if hour == EMPTY_TIME else hour
            minute = "00" if minute == EMPTY_TIME else minute
        self.variable.set(f"{hour}:{minute}")


class DateTimeField:
    """删除本地 K 线用：日历选日期，再选可选的时、分。"""

    def __init__(self, parent: ctk.CTkBaseClass) -> None:
        self.date_var = ctk.StringVar(value="")
        self.time_var = ctk.StringVar(value="")
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        DateField(self.frame, self.date_var, allow_empty=True).frame.pack(anchor="w")
        TimeField(self.frame, self.time_var, allow_empty=True).frame.pack(anchor="w", pady=(6, 0))

    def get(self) -> str:
        return combine_datetime(self.date_var.get(), self.time_var.get())
